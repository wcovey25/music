"""The faster networking layer: one shared connection pool (netconn), happy-eyeballs connecting, the name cache,
prewarming, per-host round-trip statistics (netstats), host-fitted timeouts, hedged calls, merged and cached answers,
the free retry on a stale keep-alive connection, and stall detection. Local servers only, no internet."""
import http.server
import json
import socket
import socketserver
import threading
import time
import unittest
from unittest import mock

import requests

import helpers  # noqa: F401  (sets MUSICDL_HOME)

from musicdl.core import netconn, netio, netstats
from musicdl.core.models import EngineError, Stopped


class ScriptServer:
    """HTTP/1.1 keep-alive server whose answer to the n-th request for a path is `script(path, n)` ->
    (delay seconds, status, body bytes, headers dict). Counts requests and accepted connections."""

    def __init__(self, script):
        self.script, self.counts, self.accepts, self.cookies = script, {}, 0, []
        self.lock = threading.Lock()
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def setup(self):
                with outer.lock:
                    outer.accepts += 1
                super().setup()

            def handle(self):
                try:
                    super().handle()
                except OSError:
                    pass

            def do_GET(self):
                path = self.path.split("?")[0]
                with outer.lock:
                    n = outer.counts[path] = outer.counts.get(path, 0) + 1
                    outer.cookies.append(self.headers.get("Cookie", ""))
                delay, status, body, headers = outer.script(path, n)
                time.sleep(delay)
                try:
                    self.send_response(status)
                    for k, v in (headers or {}).items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    self.close_connection = True

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.port = self.srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def count(self, path):
        with self.lock:
            return self.counts.get(path, 0)

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def ok(body=b'{"ok": 1}', delay=0.0, headers=None):
    return delay, 200, body, headers or {"Content-Type": "application/json"}


class Fresh(unittest.TestCase):
    def setUp(self):
        netio.reset_health()
        netio.CACHE.clear()
        netstats.STATS.reset()
        netconn.forget_names()


# ---------------------------------------------------------------- netstats

class HostStatTests(unittest.TestCase):
    def test_smoothed_round_trip_follows_rfc_6298(self):
        h = netstats.HostStat("x")
        h.observe(100)
        self.assertEqual((h.srtt, h.rttvar), (100, 50))
        h.observe(200)
        self.assertAlmostEqual(h.rttvar, 0.75 * 50 + 0.25 * 100)
        self.assertAlmostEqual(h.srtt, 0.875 * 100 + 0.125 * 200)

    def test_no_timeout_advice_until_there_is_something_to_go_on(self):
        h = netstats.HostStat("x")
        self.assertIsNone(h.rto())
        self.assertEqual(h.timeouts(), (8.0, 30.0))
        for _ in range(netstats.MIN_SAMPLES):
            h.observe(120)
        self.assertIsNotNone(h.rto())

    def test_timeouts_fit_a_fast_host_and_never_exceed_the_defaults(self):
        fast, slow = netstats.HostStat("fast"), netstats.HostStat("slow")
        for _ in range(10):
            fast.observe(80)
            slow.observe(20000)
        fast.connected(20)
        slow.connected(9000)
        c, r = fast.timeouts()
        self.assertEqual((c, r), (2.0, 8.0), "a quick host is given seconds, not half a minute")
        self.assertEqual(slow.timeouts(), (8.0, 30.0), "a slow one gets what it needs, up to the defaults")

    def test_percentiles_and_tail(self):
        h = netstats.HostStat("x")
        for ms in range(1, 101):
            h.observe(ms)
        self.assertAlmostEqual(h.percentile(0.5), 68.5)                 # only the last 64 are kept: 37..100
        self.assertGreater(h.tail(), h.percentile(0.5))
        self.assertIsNone(netstats.HostStat("y").tail())

    def test_counters_and_snapshot(self):
        h = netstats.HostStat("x")
        h.observe(10)
        h.failed("HTTP 503")
        h.count("hedges")
        h.count("cache_hits")
        h.add_bytes(5)
        snap = h.snapshot()
        self.assertEqual((snap["calls"], snap["errors"], snap["hedges"], snap["cache_hits"], snap["bytes"]),
                         (1, 1, 1, 1, 5))

    def test_many_threads_record_safely(self):
        reg = netstats.Registry()

        def work():
            for i in range(500):
                reg.get("h").observe(i % 50)
                reg.get("h").failed()
        ts = [threading.Thread(target=work) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        s = reg.get("h").snapshot()
        self.assertEqual((s["calls"], s["errors"]), (4000, 4000))

    def test_registry_lists_busiest_and_recent_hosts_first(self):
        reg = netstats.Registry()
        reg.get("old").observe(5)
        time.sleep(0.02)
        reg.get("new").observe(5)
        reg.get("idle")
        rows = reg.snapshot()
        self.assertEqual([r["host"] for r in rows], ["new", "old"], "hosts never used are left out")
        self.assertEqual(reg.snapshot(since=0.0), [])


# ---------------------------------------------------------------- netconn

class NameAndOrderTests(Fresh):
    def test_host_key(self):
        self.assertEqual(netconn.host_key("https://API.deezer.com/x"), "api.deezer.com")
        self.assertEqual(netconn.host_key("http://127.0.0.1:8123/a"), "127.0.0.1:8123")
        self.assertEqual(netconn.host_key("http://example.com:80/"), "example.com")

    def test_names_are_remembered_for_a_while(self):
        real = socket.getaddrinfo
        calls = []

        def counting(*a, **k):
            calls.append(a[0])
            return real(*a, **k)
        with mock.patch.object(netconn.socket, "getaddrinfo", counting):
            for _ in range(5):
                netconn.resolve("127.0.0.1", 80)
            self.assertEqual(len(calls), 1)
            with mock.patch.object(netconn, "DNS_TTL", -1.0):
                netconn.forget_names()
                netconn.resolve("127.0.0.1", 80)
                netconn.resolve("127.0.0.1", 80)
            self.assertEqual(len(calls), 3, "an expired name is asked again")

    def test_addresses_alternate_and_the_family_that_worked_goes_first(self):
        v6 = [(socket.AF_INET6, 1, 6, "", ("::1", 1, 0, 0)), (socket.AF_INET6, 1, 6, "", ("::2", 1, 0, 0))]
        v4 = [(socket.AF_INET, 1, 6, "", ("1.1.1.1", 1)), (socket.AF_INET, 1, 6, "", ("1.1.1.2", 1))]
        order = netconn._interleave(v6 + v4, None)
        self.assertEqual([i[0] for i in order], [socket.AF_INET6, socket.AF_INET] * 2)
        order = netconn._interleave(v6 + v4, socket.AF_INET)
        self.assertEqual(order[0][4][0], "1.1.1.1", "IPv4 worked last time: it is tried first")


class RaceTests(Fresh):
    def setUp(self):
        super().setUp()
        self.good = socket.socket()
        self.good.bind(("127.0.0.1", 0))
        self.good.listen(8)

    def tearDown(self):
        self.good.close()

    def test_a_dead_address_costs_a_fraction_of_a_second_not_the_timeout(self):
        # a listening socket whose queue is full and never accepted: a connect to it hangs (or is refused)
        bad = socket.socket()
        bad.bind(("127.0.0.1", 0))
        bad.listen(0)
        fillers = []
        try:
            for _ in range(4):
                s = socket.socket()
                s.setblocking(False)
                s.connect_ex(bad.getsockname())
                fillers.append(s)
            infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", bad.getsockname()),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, "", self.good.getsockname())]
            t0 = time.monotonic()
            sock = netconn._race(infos, 10.0, None, None, 0.1)
            took = time.monotonic() - t0
            try:
                self.assertEqual(sock.getpeername(), self.good.getsockname())
                self.assertLess(took, 2.0)
                self.assertTrue(sock.getblocking() or sock.gettimeout() is not None, "handed back ready to use")
            finally:
                sock.close()
        finally:
            for s in fillers:
                s.close()
            bad.close()

    def test_every_address_refused_raises_the_last_error(self):
        dead = socket.socket()
        dead.bind(("127.0.0.1", 0))
        addr = dead.getsockname()
        dead.close()
        infos = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", addr)] * 2
        with self.assertRaises(OSError):
            netconn._race(infos, 3.0, None, None, 0.05)

    def test_connect_records_how_long_it_took(self):
        host, port = self.good.getsockname()
        sock = netconn.connect((host, port), 5.0)
        sock.close()
        st = netstats.STATS.get(f"{host}:{port}")
        self.assertIsNotNone(st.connect)
        self.assertEqual(st.family, socket.AF_INET)


class SessionTests(Fresh):
    def test_one_pool_for_every_thread(self):
        seen = []
        ts = [threading.Thread(target=lambda: seen.append(netio.http())) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(len({id(s) for s in seen}), 1)

    def test_connections_are_reused_across_threads(self):
        srv = ScriptServer(lambda p, n: ok())
        try:
            netio.request("GET", srv.url + "a")
            t = threading.Thread(target=lambda: netio.request("GET", srv.url + "b"))
            t.start()
            t.join()
            netio.request("GET", srv.url + "c")
            self.assertEqual(srv.accepts, 1, "one keep-alive connection served three calls from two threads")
        finally:
            srv.close()

    def test_no_cookies_are_kept(self):
        srv = ScriptServer(lambda p, n: ok(headers={"Set-Cookie": "who=me; Path=/"}))
        try:
            netio.request("GET", srv.url + "a")
            netio.request("GET", srv.url + "a")
            self.assertEqual(srv.cookies, ["", ""])
        finally:
            srv.close()

    def test_prewarm_parks_a_connection_the_next_call_uses(self):
        srv = ScriptServer(lambda p, n: ok())
        try:
            self.assertEqual(netio.prewarm([srv.url]), 0, "switched off for tests unless asked for")
            netconn.close_all()
            self.assertEqual(netconn.prewarm([srv.url + "x", srv.url + "y"]), 1, "one host, one warm-up")
            deadline = time.monotonic() + 3
            while srv.accepts < 1 and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
            netio.request("GET", srv.url + "z")
            self.assertEqual(srv.accepts, 1, "the real call found the warm connection")
        finally:
            srv.close()

    def test_prewarm_of_a_dead_host_is_harmless(self):
        self.assertEqual(netconn.prewarm([helpers.dead_url()]), 1)


# ---------------------------------------------------------------- netio on top

class HedgeTests(Fresh):
    def test_a_call_slower_than_the_hosts_tail_gets_a_second_copy_that_wins(self):
        srv = ScriptServer(lambda p, n: ok(delay=3.0 if n == 1 else 0.0))
        try:
            with mock.patch.object(netio, "HEDGE_DEFAULT", 0.3):
                t0 = time.monotonic()
                data = netio.get_json(srv.url + "slow", hedge=True)
                took = time.monotonic() - t0
            self.assertEqual(data, {"ok": 1})
            self.assertLess(took, 2.0)
            st = netstats.STATS.get(netconn.host_key(srv.url))
            self.assertEqual((st.hedges, st.hedge_wins), (1, 1))
        finally:
            srv.close()

    def test_a_strict_host_is_never_hedged(self):
        srv = ScriptServer(lambda p, n: ok(delay=0.6))
        try:
            lim = netio.Limiter(0.01, hedge_ok=False)
            with mock.patch.object(netio, "HEDGE_DEFAULT", 0.1):
                netio.get_json(srv.url + "x", hedge=True, limiter=lim)
            self.assertEqual(srv.count("/x"), 1)
        finally:
            srv.close()

    def test_itunes_is_never_hedged(self):
        self.assertFalse(netio.ITUNES_LIMIT.hedge_ok)
        self.assertFalse(netio.MB_LIMIT.hedge_ok)

    def test_stop_while_waiting_for_a_hedged_call(self):
        srv = ScriptServer(lambda p, n: ok(delay=3.0))
        try:
            stop = threading.Event()
            threading.Timer(0.4, stop.set).start()
            t0 = time.monotonic()
            with mock.patch.object(netio, "HEDGE_DEFAULT", 0.1):
                with self.assertRaises(Stopped):
                    netio.request("GET", srv.url + "x", hedge=True, stop=stop)
            self.assertLess(time.monotonic() - t0, 1.5)
        finally:
            srv.close()


class CacheTests(Fresh):
    def test_an_answer_is_remembered_for_its_ttl(self):
        srv = ScriptServer(lambda p, n: ok(json.dumps({"n": n}).encode()))
        try:
            a = netio.get_json(srv.url + "q", params={"q": "x"}, ttl=60)
            a["changed"] = True                                           # each caller gets its own copy
            b = netio.get_json(srv.url + "q", params={"q": "x"}, ttl=60)
            self.assertEqual(b, {"n": 1})
            netio.get_json(srv.url + "q", params={"q": "other"}, ttl=60)
            self.assertEqual(srv.count("/q"), 2)
            self.assertEqual(netstats.STATS.get(netconn.host_key(srv.url)).cache_hits, 1)
        finally:
            srv.close()

    def test_identical_calls_at_the_same_moment_are_merged(self):
        srv = ScriptServer(lambda p, n: ok(delay=0.4))
        try:
            got = []
            ts = [threading.Thread(target=lambda: got.append(netio.get_json(srv.url + "m", ttl=30))) for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(got, [{"ok": 1}] * 6)
            self.assertEqual(srv.count("/m"), 1)
        finally:
            srv.close()

    def test_a_failure_is_not_remembered(self):
        srv = ScriptServer(lambda p, n: (0, 500, b"", {}) if n <= 1 else ok())
        try:
            with self.assertRaises(EngineError):
                netio.get_json(srv.url + "f", ttl=60, retries=1)
            self.assertEqual(netio.get_json(srv.url + "f", ttl=60, retries=1), {"ok": 1})
        finally:
            srv.close()

    def test_without_ttl_nothing_is_kept(self):
        srv = ScriptServer(lambda p, n: ok())
        try:
            netio.get_json(srv.url + "n")
            netio.get_json(srv.url + "n")
            self.assertEqual(srv.count("/n"), 2)
        finally:
            srv.close()


class StaleAndTimeoutTests(Fresh):
    def test_a_stale_keep_alive_connection_is_retried_at_once_for_free(self):
        srv = ScriptServer(lambda p, n: ok())
        real = requests.Session.request
        calls = []

        def flaky(self_, *a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise requests.ConnectionError("('Connection aborted.', RemoteDisconnected('closed'))")
            return real(self_, *a, **k)
        try:
            sleeps = []
            with mock.patch.object(requests.Session, "request", flaky), \
                    mock.patch.object(netio, "_sleep", lambda stop, s: sleeps.append(s)):
                r = netio.request("GET", srv.url + "s", retries=1)
            self.assertEqual(r.status_code, 200)
            self.assertEqual((len(calls), sleeps), (2, []), "repeated once, with no back-off and no retry used up")
            self.assertEqual(netio.health(netconn.host_key(srv.url)).fails, 0, "not a failure of the host")
        finally:
            srv.close()

    def test_only_a_stale_connection_looks_stale(self):
        self.assertTrue(netio._stale(requests.ConnectionError("Connection reset by peer")))
        self.assertFalse(netio._stale(requests.ConnectionError("Name or service not known")))
        self.assertFalse(netio._stale(requests.Timeout("read timed out")))

    def test_calls_use_timeouts_fitted_to_the_host(self):
        srv = ScriptServer(lambda p, n: ok())
        seen = []
        real = requests.Session.request

        def spy(self_, method, url, **k):
            seen.append(k.get("timeout"))
            return real(self_, method, url, **k)
        try:
            with mock.patch.object(requests.Session, "request", spy):
                for _ in range(6):
                    netio.request("GET", srv.url + "t")
                netio.request("GET", srv.url + "t", timeout=(3, 4))
            self.assertEqual(seen[0], (8.0, 30.0), "an unmeasured host gets the defaults")
            self.assertLessEqual(seen[5][1], 8.0, "a measured local host gets a short read timeout")
            self.assertEqual(seen[-1], (3, 4), "a timeout given by the caller wins")
        finally:
            srv.close()


class PressureTests(Fresh):
    def test_pressure_counts_slowed_limiters_and_open_breakers(self):
        self.assertEqual(netio.pressure(), {"open": 0, "slowed": 0})
        netio.DEEZER_LIMIT.slow()
        self.assertEqual(netio.pressure()["slowed"], 1)
        h = netio.health("down.example")
        for _ in range(netio.TRIP_AFTER):
            h.failure()
        self.assertEqual(netio.pressure()["open"], 1)
        netio.reset_health()
        self.assertEqual(netio.pressure(), {"open": 0, "slowed": 0})

    def test_a_403_from_itunes_is_still_pushback(self):
        srv = ScriptServer(lambda p, n: (0, 403, b"", {}) if n == 1 else ok())
        try:
            lim = netio.Limiter(0.01, floor=0.01, ceil=1.0)
            with mock.patch.object(netio, "_sleep", lambda stop, s: None):
                r = netio.request("GET", srv.url + "i", limiter=lim, throttle=(403,))
            self.assertEqual(r.status_code, 200)
            self.assertGreater(lim.interval, 0.01)
        finally:
            srv.close()


class StallTests(unittest.TestCase):
    def test_a_connection_that_collapses_is_given_up_after_a_while(self):
        clock = [0.0]
        with mock.patch.object(netio.time, "monotonic", lambda: clock[0]):
            p = netio._Pulse()
            for _ in range(5):                                     # 1 MB/s for five seconds
                clock[0] += 1.0
                self.assertFalse(p.feed(1_000_000))
            gave_up = None
            for s in range(1, 12):                                 # then a trickle
                clock[0] += 1.0
                if p.feed(1_000):
                    gave_up = s
                    break
        self.assertIsNotNone(gave_up)
        self.assertGreaterEqual(gave_up, netio.STALL_SECONDS)

    def test_a_connection_that_was_always_slow_is_not_a_stall(self):
        clock = [0.0]
        with mock.patch.object(netio.time, "monotonic", lambda: clock[0]):
            p = netio._Pulse()
            for _ in range(30):
                clock[0] += 1.0
                self.assertFalse(p.feed(20_000))

    def test_a_short_dip_is_forgiven(self):
        clock = [0.0]
        with mock.patch.object(netio.time, "monotonic", lambda: clock[0]):
            p = netio._Pulse()
            for rate in [1_000_000] * 4 + [1_000] * 3 + [1_000_000] * 3 + [1_000] * 3:
                clock[0] += 1.0
                self.assertFalse(p.feed(rate))


class MemoForgetTests(unittest.TestCase):
    def test_forget_drops_one_result(self):
        m, calls = netio.Memo(8), []
        m.get("k", lambda: calls.append(1) or 1)
        m.forget("k")
        m.get("k", lambda: calls.append(1) or 1)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
