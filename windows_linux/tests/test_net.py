"""Networking tests: limiter, circuit breaker, retries, resumable and segmented downloads, compute-once cache.
Everything runs against local servers (see helpers.RangeServer); nothing touches the internet."""
import os
import threading
import time
import unittest

from helpers import RangeServer, dead_url, fresh_dir

from musicdl.core import netio
from musicdl.core.models import EngineError, Stopped

WWW = fresh_dir("net_www")
SIZE = 6 * 2 ** 20
DATA = bytes((i * 31 + (i >> 8)) & 0xFF for i in range(SIZE))             # not compressible, not periodic at block size
SRV = None


def setUpModule():
    global SRV
    with open(os.path.join(WWW, "big.bin"), "wb") as fh:
        fh.write(DATA)
    with open(os.path.join(WWW, "small.bin"), "wb") as fh:
        fh.write(DATA[:200_000])


def tearDownModule():
    pass


def serve(**kw):
    return RangeServer(WWW, **kw)


class LimiterTests(unittest.TestCase):
    def test_slow_doubles_interval_up_to_the_ceiling_and_good_recovers_to_the_floor(self):
        lim = netio.Limiter(0.25, floor=0.125, ceil=1.0)
        lim.slow()
        self.assertAlmostEqual(lim.interval, 0.5)
        lim.slow(); lim.slow(); lim.slow()
        self.assertAlmostEqual(lim.interval, 1.0)
        for _ in range(200):
            lim.good()
        self.assertAlmostEqual(lim.interval, 0.125)

    def test_a_fixed_limiter_never_changes(self):
        lim = netio.Limiter(1.1, floor=1.1, ceil=1.1)
        for _ in range(50):
            lim.good()
        lim.slow()
        self.assertAlmostEqual(lim.interval, 1.1)

    def test_pause_holds_back_every_thread(self):
        lim = netio.Limiter(0.01)
        lim.wait()
        lim.slow(pause=0.4)
        t0 = time.monotonic()
        waited = []
        ts = [threading.Thread(target=lambda: (lim.wait(), waited.append(time.monotonic() - t0))) for _ in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertGreaterEqual(min(waited), 0.35, "all threads wait out the host's Retry-After, not just the one that was told")

    def test_retry_after_numbers_and_dates(self):
        self.assertEqual(netio.retry_after_seconds("7"), 7.0)
        self.assertIsNone(netio.retry_after_seconds(""))
        self.assertIsNone(netio.retry_after_seconds("soon"))
        soon = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() + 30))
        self.assertTrue(20 <= netio.retry_after_seconds(soon) <= 31)
        past = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(time.time() - 3600))
        self.assertEqual(netio.retry_after_seconds(past), 0.0)

    def test_429_with_retry_after_slows_the_limiter_and_the_retry_succeeds(self):
        srv = serve()
        try:
            srv.fail["/small.bin"] = [429, 1, "1"]
            lim = netio.Limiter(0.05)
            t0 = time.monotonic()
            r = netio.request("GET", srv.url + "small.bin", limiter=lim, retries=3)
            self.assertEqual(r.status_code, 200)
            self.assertGreaterEqual(time.monotonic() - t0, 0.9, "waited as asked")
            self.assertGreater(lim.interval, 0.05, "and asks less often afterwards")
        finally:
            srv.close()


class BreakerTests(unittest.TestCase):
    def setUp(self):
        netio.reset_health()
        self._cool = netio.COOLDOWN
        netio.COOLDOWN = 0.4

    def tearDown(self):
        netio.COOLDOWN = self._cool
        netio.reset_health()

    def test_a_failing_host_fails_fast_after_a_few_tries(self):
        srv = serve()
        try:
            srv.fail["/small.bin"] = [503, 10 ** 6, None]
            for _ in range(netio.TRIP_AFTER):
                with self.assertRaises(EngineError):
                    netio.request("GET", srv.url + "small.bin", retries=1)
            before = len(srv.log)
            t0 = time.monotonic()
            for _ in range(30):
                with self.assertRaises(netio.HostDown):
                    netio.request("GET", srv.url + "small.bin", retries=3)
            self.assertLess(time.monotonic() - t0, 0.5, "30 calls to a host that is down cost nothing")
            self.assertEqual(len(srv.log), before, "and nothing reached the server")
        finally:
            srv.close()

    def test_a_refused_connection_counts_as_a_failure_too(self):
        url = dead_url() + "x"                       # (Windows takes ~2 s to report each refusal)
        for _ in range(netio.TRIP_AFTER):
            with self.assertRaises(EngineError):
                netio.request("GET", url, retries=1, timeout=(1, 1))
        with self.assertRaises(netio.HostDown):
            netio.request("GET", url, retries=1)

    def test_no_back_off_sleep_after_the_last_try(self):
        srv = serve()
        sleeps, real = [], netio._sleep
        try:
            srv.fail["/small.bin"] = [500, 10 ** 6, None]
            netio._sleep = lambda stop, s: sleeps.append(s)
            with self.assertRaises(EngineError):
                netio.request("GET", srv.url + "small.bin", retries=3)
            self.assertEqual(len(sleeps), 2, "a pause between tries, none after the last one")
            self.assertTrue(0.7 <= sleeps[0] <= 1.3 and 1.4 <= sleeps[1] <= 2.6, f"1 s then 2 s, jittered: {sleeps}")
        finally:
            netio._sleep = real
            srv.close()

    def test_the_host_is_tried_again_after_the_cooldown_and_forgiven_when_it_answers(self):
        srv = serve()
        try:
            srv.fail["/small.bin"] = [503, netio.TRIP_AFTER, None]
            for _ in range(netio.TRIP_AFTER):
                with self.assertRaises(EngineError):
                    netio.request("GET", srv.url + "small.bin", retries=1)
            with self.assertRaises(netio.HostDown):
                netio.request("GET", srv.url + "small.bin", retries=1)
            time.sleep(0.5)
            r = netio.request("GET", srv.url + "small.bin", retries=1)          # the test request: the host is back
            self.assertEqual(r.status_code, 200)
            for _ in range(10):
                self.assertEqual(netio.request("GET", srv.url + "small.bin", retries=1).status_code, 200)
        finally:
            srv.close()

    def test_while_testing_only_one_request_goes_out(self):
        srv = serve()
        try:
            srv.fail["/small.bin"] = [503, netio.TRIP_AFTER, None]
            for _ in range(netio.TRIP_AFTER):
                with self.assertRaises(EngineError):
                    netio.request("GET", srv.url + "small.bin", retries=1)
            time.sleep(0.5)
            h = netio.health(srv.url.split("//")[1].rstrip("/"))
            h.check("x")                                                          # this caller is the test request
            with self.assertRaises(netio.HostDown):
                h.check("x")                                                      # everyone else still waits
        finally:
            srv.close()

    def test_404_and_429_do_not_count_as_a_host_failing(self):
        srv = serve()
        try:
            for _ in range(netio.TRIP_AFTER + 2):
                self.assertIsNone(netio.request("GET", srv.url + "nothing.bin", retries=1))
            srv.fail["/small.bin"] = [429, 6, None]
            for _ in range(6):
                with self.assertRaises(EngineError):
                    netio.request("GET", srv.url + "small.bin", retries=1)
            self.assertEqual(netio.request("GET", srv.url + "small.bin", retries=1).status_code, 200)
        finally:
            srv.close()


class DownloadTests(unittest.TestCase):
    def setUp(self):
        netio.reset_health()
        self.dir = fresh_dir("net_dl")
        self.dest = os.path.join(self.dir, "out.bin")

    def check(self, size=SIZE):
        with open(self.dest, "rb") as fh:
            self.assertEqual(fh.read(), DATA[:size])

    def test_plain_download(self):
        srv = serve()
        try:
            n = netio.download(srv.url + "big.bin", self.dest)
            self.assertEqual(n, SIZE)
            self.check()
            self.assertEqual(len(srv.log), 1, "a fast connection is left alone: one request")
        finally:
            srv.close()

    def test_a_dropped_connection_resumes_from_the_byte_it_reached(self):
        srv = serve()
        try:
            srv.cuts = [1_500_000]
            self.assertEqual(netio.download(srv.url + "big.bin", self.dest), SIZE)
            self.check()
            ranges = srv.requests_for("big.bin")
            self.assertEqual(len(ranges), 2)
            start = int(ranges[1].split("=")[1].rstrip("-"))
            self.assertTrue(1_000_000 <= start <= 1_500_000, f"resumed at {start}, not from the start")
        finally:
            srv.close()

    def test_a_server_without_ranges_is_started_over_not_corrupted(self):
        srv = serve(ranges=False)
        try:
            srv.cuts = [700_000]
            self.assertEqual(netio.download(srv.url + "big.bin", self.dest), SIZE)
            self.check()
        finally:
            srv.close()

    def test_a_short_file_is_not_accepted(self):
        srv = serve(ranges=False)
        try:
            srv.cuts = [500_000, 500_000, 500_000]
            with self.assertRaises(EngineError):
                netio.download(srv.url + "big.bin", self.dest)
        finally:
            srv.close()

    def test_missing_file_is_not_retried(self):
        srv = serve()
        try:
            with self.assertRaises(netio.HttpStatus) as cm:
                netio.download(srv.url + "nope.bin", self.dest)
            self.assertTrue(cm.exception.permanent)
            self.assertEqual(len(srv.log), 1)
        finally:
            srv.close()

    def test_too_small_is_refused(self):
        srv = serve()
        try:
            with self.assertRaises(EngineError):
                netio.download(srv.url + "small.bin", self.dest, min_bytes=300_000)
        finally:
            srv.close()

    def test_stop_ends_a_slow_download(self):
        srv = serve(rate=400_000)
        try:
            stop = threading.Event()
            threading.Timer(0.5, stop.set).start()
            t0 = time.monotonic()
            with self.assertRaises(Stopped):
                netio.download(srv.url + "big.bin", self.dest, stop=stop)
            self.assertLess(time.monotonic() - t0, 3)
        finally:
            srv.close()

    def test_a_throttled_host_is_given_several_connections(self):
        srv = serve(rate=1_200_000)
        try:
            t0 = time.monotonic()
            self.assertEqual(netio.download(srv.url + "big.bin", self.dest), SIZE)
            took = time.monotonic() - t0
            self.check()
            self.assertGreaterEqual(srv.peak, 3, "the rest of the file was fetched over several connections")
            self.assertLess(took, SIZE / 1_200_000 * 0.7, f"{took:.1f}s should beat one connection ({SIZE / 1_200_000:.1f}s)")
            self.assertEqual(sum(1 for _p, r in srv.log if not r), 1, "only the first request asked for the whole file")
        finally:
            srv.close()

    def test_segmented_download_survives_a_dropped_block(self):
        srv = serve(rate=1_200_000)
        try:
            srv.cuts = [None, 200_000]                       # the first block request is cut off half-way
            self.assertEqual(netio.download(srv.url + "big.bin", self.dest), SIZE)
            self.check()
        finally:
            srv.close()

    def test_segmented_download_when_a_block_keeps_failing_ends_with_an_error_and_no_junk(self):
        srv = serve(rate=1_200_000)
        try:
            srv.cuts = [None] + [100_000] * 30
            with self.assertRaises(EngineError):
                netio.download(srv.url + "big.bin", self.dest)
        finally:
            srv.close()

    def test_a_fast_host_is_not_split(self):
        srv = serve(rate=60_000_000)
        try:
            netio.download(srv.url + "big.bin", self.dest)
            self.assertEqual(len(srv.log), 1)
        finally:
            srv.close()

    def test_a_server_that_does_not_do_ranges_is_streamed_even_when_slow(self):
        srv = serve(rate=2_500_000, ranges=False)
        try:
            netio.download(srv.url + "big.bin", self.dest)
            self.check()
            self.assertEqual(len(srv.log), 1)
        finally:
            srv.close()

    def test_connection_budget_is_given_back(self):
        srv = serve(rate=1_500_000)
        try:
            netio.download(srv.url + "big.bin", self.dest)
            got = [netio.EXTRA_CONNECTIONS.acquire(blocking=False) for _ in range(10)]
            self.assertTrue(all(got), "every extra connection a finished download used was returned")
            for _ in range(10):
                netio.EXTRA_CONNECTIONS.release()
        finally:
            srv.close()


class MemoTests(unittest.TestCase):
    def test_computed_once_for_many_askers(self):
        m, calls = netio.Memo(8), []

        def make():
            calls.append(1)
            time.sleep(0.2)
            return "art"
        out = []
        ts = [threading.Thread(target=lambda: out.append(m.get("k", make))) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(out, ["art"] * 8)
        self.assertEqual(len(calls), 1)
        self.assertEqual((m.computed, m.reused), (1, 7))

    def test_a_failure_is_shared_and_a_stop_is_not_remembered(self):
        m = netio.Memo(8)
        with self.assertRaises(ValueError):
            m.get("a", lambda: (_ for _ in ()).throw(ValueError("x")))
        with self.assertRaises(ValueError):
            m.get("a", lambda: "never")
        with self.assertRaises(Stopped):
            m.get("b", lambda: (_ for _ in ()).throw(Stopped()))
        self.assertEqual(m.get("b", lambda: "later"), "later")

    def test_only_the_latest_are_kept(self):
        m = netio.Memo(3)
        for i in range(5):
            m.get(i, lambda i=i: i)
        calls = []
        m.get(4, lambda: calls.append(1))
        m.get(0, lambda: calls.append(1) or 0)
        self.assertEqual(len(calls), 1, "newest is still there, oldest was dropped")

    def test_a_waiting_thread_can_be_stopped(self):
        m, stop = netio.Memo(4), threading.Event()
        threading.Thread(target=lambda: m.get("slow", lambda: time.sleep(1.5) or 1), daemon=True).start()
        time.sleep(0.1)
        threading.Timer(0.3, stop.set).start()
        t0 = time.monotonic()
        with self.assertRaises(Stopped):
            m.get("slow", lambda: 2, stop)
        self.assertLess(time.monotonic() - t0, 1.0)


if __name__ == "__main__":
    unittest.main()
