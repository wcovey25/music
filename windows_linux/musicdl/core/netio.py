"""
netio.py — the single place that talks HTTP.

* one `requests.Session` shared by every thread (netconn.py: no cookies, so sharing it shares nothing else)
* per-host rate limiters shared by all workers; they adapt: a host that answers 429 / "slow down" makes every worker
  back off together (and honours Retry-After), a host that keeps answering gets asked a little more often
  (MusicBrainz asks for one request per second and is never sped up). A host may name its own "slow down" statuses
  (`throttle=`): iTunes answers 403 when it has had enough
* a circuit breaker per host: when a host has failed several times in a row, calls fail at once for a while instead
  of every worker burning its own retries and back-off sleeps on a dead server; one test request then decides
* every call feeds the telemetry (bytes, latency, searches) and honours the Stop event
* retries with jittered back-off on throttling / transient errors (and no pointless sleep after the last try); a
  keep-alive connection the server closed while it sat idle is replaced and the call repeated at once, for free
* one shared connection pool for every thread, happy-eyeballs connecting and name caching (netconn.py), and a model of
  each host's round-trip time (netstats.py) that sets timeouts to fit the host and shows how it has been behaving
* `hedge=True` calls that run slower than the host's usual tail send a second copy and keep whichever answers first;
  `ttl=` remembers an answer for a while and merges identical calls made at the same moment
* downloads resume where they broke off, are checked against the size the server announced, and — when the host
  limits every single connection — are split into byte ranges fetched side by side
* `Memo` computes a value once even when several workers ask for it at the same moment (album art shared by a
  whole album is downloaded and processed once)
"""
import collections
import concurrent.futures as futures
import copy
import email.utils
import json
import os
import random
import threading
import time
from urllib.parse import urlsplit

import requests
import urllib3.exceptions as urllib3_exc

from ..telemetry import T
from . import netconn
from .models import EngineError, Stopped
from .netstats import STATS

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")

# ---- circuit breaker
TRIP_AFTER = 4                  # consecutive failures that open the circuit
COOLDOWN = 3.0                  # seconds the first time; doubles each time it trips again
COOLDOWN_MAX = 45.0

# ---- segmented downloads
SEG_MIN_BYTES = 3 * 2 ** 20     # smaller files are not worth splitting
SEG_BLOCK = 1 * 2 ** 20         # size of one range request
SEG_MAX = 8                     # most connections for one file (including the first); it only gets as many as pay off
SEG_START = 4                   # connections it starts with once the single one is seen to be held back
SEG_SLOW = 2.0e6                # bytes/second: one connection slower than this looks like a per-connection limit
SEG_PROBE = 0.6                 # seconds spent measuring before deciding
SEG_STEP = 0.9                  # seconds a new connection is given to show it helps before the next is tried
SEG_GAIN = 0.15                 # a new connection stays when it raised the file's speed by at least this much
EXTRA_CONNECTIONS = threading.BoundedSemaphore(12)      # extra connections across every running download

# ---- a connection that collapses (the speed falls to a sliver of what it had) is dropped and picked up again
STALL_SECONDS = 6.0
STALL_FRACTION = 0.06
STALL_FLOOR = 150_000           # bytes/second the connection must have reached before a fall counts as a collapse

BLOCK_CRAWL_AFTER = 5.0         # a piece of a file that has been coming for this long ...
BLOCK_CRAWL = 25_000            # ... at less than this many bytes/second is given up on and asked for again

# ---- hedged calls
HEDGE_DEFAULT = 1.5             # seconds before a second copy when the host has not been measured yet
HEDGE_MIN, HEDGE_MAX = 0.35, 3.0


class HostDown(EngineError):
    """The host has been failing; the call was not even tried."""


class HttpStatus(EngineError):
    """A response with an error status."""

    def __init__(self, status, host=""):
        super().__init__(f"{host + ': ' if host else ''}HTTP {status}")
        self.status = status

    @property
    def permanent(self):
        """Asking again will not help (gone, forbidden, not found…)."""
        return self.status in (400, 401, 403, 404, 410, 451)


def http():
    """The one session every thread shares (see netconn)."""
    return netconn.session()


def close_thread_session():
    """Kept for workers that tidy up when they end: the pool is shared now, so there is nothing per-thread to close."""


class Limiter:
    """At most one request per `interval` seconds across all threads. The interval adapts between `floor` and `ceil`:
    `slow()` (the host said "too many requests") doubles it and can park every thread for a while; `good()` shaves it
    after a run of successes. With floor == ceil it never changes."""

    def __init__(self, interval, floor=None, ceil=None, hedge_ok=True):
        self.interval, self.base, self.hedge_ok = interval, interval, hedge_ok
        self.floor = interval * 0.5 if floor is None else floor
        self.ceil = interval * 8 if ceil is None else ceil
        self._lock, self._next, self._run, self._adapted = threading.Lock(), 0.0, 0, False

    def wait(self, stop=None):
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.interval
        if delay:
            _sleep(stop, delay)

    def good(self):
        with self._lock:
            self._run += 1
            if self._run >= 8:
                self._run = 0
                if self.interval > self.floor:
                    self.interval, self._adapted = max(self.floor, self.interval * 0.85), True

    def slow(self, pause=0.0):
        """The host asked for fewer requests: slow everyone down and, if it said for how long, hold everyone back."""
        with self._lock:
            self._run = 0
            self.interval, self._adapted = min(self.ceil, max(self.interval, 0.05) * 2), True
            if pause:
                self._next = max(self._next, time.monotonic() + min(60.0, pause))

    def reset(self):
        """Forget what a previous run taught the limiter (an interval set by hand is left alone)."""
        with self._lock:
            if self._adapted:
                self.interval, self._adapted = self.base, False
            self._next, self._run = 0.0, 0


MB_LIMIT = Limiter(1.1, floor=1.1, ceil=4.0, hedge_ok=False)             # MusicBrainz: one per second, never faster
CAA_LIMIT = Limiter(0.25, floor=0.15)
DEEZER_LIMIT = Limiter(0.25, floor=0.125)                   # Deezer allows ~10 requests/s
ITUNES_LIMIT = Limiter(1.6, floor=1.2, ceil=14.0, hedge_ok=False)         # iTunes: 60 requests at ~43/min went through
#                                     unchallenged; it answers 403 when it has had enough, which slows everyone down
ARCHIVE_LIMIT = Limiter(0.15, floor=0.08)
LYRICS_LIMIT = Limiter(0.3, floor=0.2)
WEB_LIMIT = Limiter(0.2, floor=0.1)
SUGGEST_LIMIT = Limiter(0.1, floor=0.1, ceil=2.0)           # type-ahead in the search bar: a few quick calls per pause


def _sleep(stop, seconds):
    if stop is not None:
        if stop.wait(seconds):
            raise Stopped()
    else:
        time.sleep(seconds)


def _backoff(attempt):
    """1 s, 2 s, 4 s … (at most 20 s), each spread ±25% so workers that failed together do not retry together."""
    return min(20.0, 2.0 ** attempt) * random.uniform(0.75, 1.25)


def retry_after_seconds(value):
    """Retry-After is either a number of seconds or an HTTP date. None when absent or unreadable."""
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
        return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


class _Health:
    """Circuit breaker state for one host."""

    def __init__(self):
        self._lock = threading.Lock()
        self.fails, self.trips, self.until, self.testing = 0, 0, 0.0, False

    def check(self, host):
        with self._lock:
            if not self.until:
                return
            if time.monotonic() < self.until or self.testing:
                raise HostDown(f"{host} is not responding right now")
            self.testing = True                              # cooldown over: let exactly one request through

    def success(self):
        with self._lock:
            self.fails = self.trips = 0
            self.until, self.testing = 0.0, False

    def failure(self):
        with self._lock:
            self.fails += 1
            self.testing = False
            if self.fails >= TRIP_AFTER:
                self.trips += 1
                self.until = time.monotonic() + min(COOLDOWN_MAX, COOLDOWN * 2 ** (self.trips - 1))

    def release(self):
        with self._lock:
            self.testing = False


_health, _health_lock = {}, threading.Lock()


def health(host):
    with _health_lock:
        return _health.setdefault(host, _Health())


_LIMITERS = (MB_LIMIT, CAA_LIMIT, DEEZER_LIMIT, ITUNES_LIMIT, ARCHIVE_LIMIT, LYRICS_LIMIT, WEB_LIMIT, SUGGEST_LIMIT)


def reset_health():
    with _health_lock:
        _health.clear()
    for lim in _LIMITERS:
        lim.reset()


def pressure():
    """How hard the hosts are pushing back right now: {'open': hosts whose circuit breaker is open, 'slowed': limiters
    running slower than their normal pace because a host asked for fewer requests}. Zeros mean there is room to speed up."""
    now = time.monotonic()
    with _health_lock:
        states = list(_health.values())
    return {"open": sum(1 for h in states if h.until and now < h.until),
            "slowed": sum(1 for lim in _LIMITERS if lim.interval > lim.base * 1.2)}


_hedges = None
_hedges_lock = threading.Lock()


def _hedge_pool():
    global _hedges
    if _hedges is None:
        with _hedges_lock:
            if _hedges is None:
                _hedges = futures.ThreadPoolExecutor(max_workers=12, thread_name_prefix="hedge")
    return _hedges


def _wait(fs, timeout, stop, when=futures.ALL_COMPLETED):
    """futures.wait that notices the Stop event (it looks every quarter second)."""
    end = None if timeout is None else time.monotonic() + timeout
    while True:
        slice_ = 0.25 if end is None else max(0.0, min(0.25, end - time.monotonic()))
        done, pending = futures.wait(fs, timeout=slice_, return_when=when)
        if done and (when == futures.FIRST_COMPLETED or not pending):
            return done, pending
        if stop is not None and stop.is_set():
            raise Stopped()
        if end is not None and time.monotonic() >= end:
            return done, pending


def _discard(f):
    """The copy that lost the race: close its response when it arrives so the connection is not left open."""
    try:
        f.result().close()
    except Exception:                                        # noqa: BLE001 — nobody is waiting for it
        pass


def _send(method, url, kw, hedge_after, st, limiter, stop):
    """One HTTP call. With `hedge_after` seconds given, a call still unanswered after that long is sent a second time and
    the first answer to come back is used (the other is discarded). A slow answer is usually a bad connection or a busy
    server instance; the second copy goes out on another connection."""
    sess = http()
    if not hedge_after:
        return sess.request(method, url, **kw)
    pool = _hedge_pool()
    first = pool.submit(sess.request, method, url, **kw)
    done, _ = _wait([first], hedge_after, stop)
    if done:
        return first.result()
    st.count("hedges")
    try:
        if limiter:
            limiter.wait(stop)
    except BaseException:
        first.add_done_callback(_discard)
        raise
    second = pool.submit(sess.request, method, url, **kw)
    pending, err = {first, second}, None
    while pending:
        try:
            done, pending = _wait(pending, None, stop, futures.FIRST_COMPLETED)
        except BaseException:
            for f in pending:
                f.add_done_callback(_discard)
            raise
        for f in done:
            try:
                r = f.result()
            except Exception as e:                           # noqa: BLE001 — the other copy may still answer
                err = e
                continue
            if f is second:
                st.count("hedge_wins")
            for other in pending:
                other.add_done_callback(_discard)
            return r
    raise err


def _stale(e):
    """The error a reused keep-alive connection gives when the server closed it while it sat idle."""
    if not isinstance(e, requests.ConnectionError):
        return False
    text = f"{type(e).__name__} {e}"
    return any(w in text for w in ("RemoteDisconnected", "Connection reset", "Connection aborted", "BrokenPipe"))


def request(method, url, *, params=None, headers=None, json_body=None, limiter=None, timeout=None, retries=3,
            stop=None, search=False, stream=False, ok_404=True, hedge=False, throttle=()):
    """One HTTP call with retries. Returns the Response (or None on 404 when ok_404). Raises EngineError
    (HostDown when the host has been failing and was not even tried).

    `timeout` (connect, read) defaults to what the host has shown it needs (netstats); `hedge` sends a second copy of
    a GET that is slower than the host's usual tail (never to a host whose limiter says it is strict). `throttle`:
    extra statuses that mean "too many requests" for this host (iTunes says 403), treated like 429."""
    last = None
    host = urlsplit(url).netloc
    st = STATS.get(netconn.host_key(url))
    h = health(host)
    hedging = bool(hedge) and method == "GET" and not stream and (limiter is None or limiter.hedge_ok)
    attempt, free = 0, 1
    while attempt < max(1, retries):
        if stop is not None and stop.is_set():
            raise Stopped()
        h.check(host)
        try:
            if limiter:
                limiter.wait(stop)
        except BaseException:
            h.release()
            raise
        if search:
            T.search()
        tail = st.tail() if hedging else None
        after = min(HEDGE_MAX, max(HEDGE_MIN, 1.25 * tail / 1000.0)) if tail else HEDGE_DEFAULT
        kw = dict(params=params, headers=headers, json=json_body, timeout=timeout or st.timeouts(), stream=stream)
        t0 = time.perf_counter()
        try:
            r = _send(method, url, kw, after if hedging else 0, st, limiter, stop)
        except requests.RequestException as e:
            last = e
            T.error()
            st.failed(type(e).__name__)
            if free and method in ("GET", "HEAD") and _stale(e):
                free -= 1                                    # an idle connection the server had closed: just reconnect
                h.release()
                continue
            h.failure()
        except BaseException:
            h.release()                                      # (an odd error must not leave the host stuck "being tested")
            raise
        else:
            ms = (time.perf_counter() - t0) * 1000
            st.observe(ms)
            if not stream:
                T.latency(host, ms)
                T.add_bytes(len(r.content))
                st.add_bytes(len(r.content))
            status = r.status_code
            if status == 404 and ok_404:
                h.success()
                r.close()
                return None
            if status in (429, 500, 502, 503, 504) or status in throttle:
                last = HttpStatus(status)
                T.error()
                st.failed(f"HTTP {status}")
                wait = retry_after_seconds(r.headers.get("Retry-After"))
                r.close()
                if status == 429 or status in throttle or (status == 503 and wait is not None):
                    h.success()                              # the host is alive; it is only asking for patience
                    if limiter:
                        limiter.slow(wait or (15.0 if status in throttle else 0.0))
                    if wait is not None and attempt + 1 < retries:
                        _sleep(stop, min(30.0, wait))
                        attempt += 1
                        continue
                else:
                    h.failure()
                    if limiter and status == 503:
                        limiter.slow()
            else:
                h.success()
                if limiter:
                    limiter.good()
                return r
        if attempt + 1 < retries:
            _sleep(stop, _backoff(attempt))
        attempt += 1
    raise EngineError(f"{host}: {last}")


# ---------------------------------------------------------------- answers worth keeping

class _Flight:
    __slots__ = ("event", "value", "error")

    def __init__(self):
        self.event, self.value, self.error = threading.Event(), None, None


class Cache:
    """Answers kept for `ttl` seconds, newest `limit` of them. Calls for the same key made while the first is still
    running wait for it instead of asking again. Each caller gets its own copy (they are free to change it)."""

    def __init__(self, limit=512):
        self.limit = limit
        self._lock = threading.Lock()
        self._d = collections.OrderedDict()                  # key -> (expires, value)
        self._flights = {}
        self.hits = self.misses = 0

    def get(self, key, make, ttl, stop=None, on_hit=None):
        while True:
            now = time.monotonic()
            with self._lock:
                hit = self._d.get(key)
                if hit and hit[0] > now:
                    self._d.move_to_end(key)
                    self.hits += 1
                    value, found = hit[1], True
                else:
                    found = False
                    flight = self._flights.get(key)
                    owner = flight is None
                    if owner:
                        flight = self._flights[key] = _Flight()
                        self.misses += 1
            if found:
                if on_hit:
                    on_hit()
                return copy.deepcopy(value)
            if owner:
                try:
                    flight.value = make()
                    with self._lock:
                        self._d[key] = (time.monotonic() + ttl, flight.value)
                        self._d.move_to_end(key)
                        while len(self._d) > self.limit:
                            self._d.popitem(last=False)
                except BaseException as e:                   # noqa: BLE001 — handed to everyone who waited
                    flight.error = e
                    raise
                finally:
                    with self._lock:
                        self._flights.pop(key, None)
                    flight.event.set()
                return copy.deepcopy(flight.value)
            while not flight.event.wait(0.25):
                if stop is not None and stop.is_set():
                    raise Stopped()
            if isinstance(flight.error, Stopped) and not (stop is not None and stop.is_set()):
                continue                                     # the one who was asking was stopped, this caller was not
            if flight.error is not None:
                raise flight.error
            return copy.deepcopy(flight.value)

    def clear(self):
        with self._lock:
            self._d.clear()
            self.hits = self.misses = 0


CACHE = Cache()


def _cached(kind, url, params, headers, ttl, stop, make):
    if not ttl:
        return make()
    key = (kind, url, json.dumps(params or {}, sort_keys=True, default=str),
           json.dumps(headers or {}, sort_keys=True, default=str))
    st = STATS.get(netconn.host_key(url))
    return CACHE.get(key, make, ttl, stop, on_hit=lambda: st.count("cache_hits"))


def get_json(url, params=None, *, ttl=None, **kw):
    """GET -> parsed JSON (None on 404 or an empty body). `ttl` seconds: the answer is remembered for that long."""
    def make():
        r = request("GET", url, params=params, **kw)
        if r is None:
            return None
        try:
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise EngineError(f"{urlsplit(url).netloc}: {e}") from None
        finally:
            r.close()
    return _cached("json", url, params, kw.get("headers"), ttl, kw.get("stop"), make)


def post_json(url, body, headers=None, **kw):
    r = request("POST", url, json_body=body, headers=headers, **kw)
    if r is None:
        return None
    try:
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = r.text[:200]
        except Exception:
            pass
        raise EngineError(f"{urlsplit(url).netloc}: {e} {detail}".strip()) from None
    except (requests.RequestException, ValueError) as e:
        raise EngineError(f"{urlsplit(url).netloc}: {e}") from None
    finally:
        r.close()


def get_text(url, headers=None, browser=False, *, ttl=None, **kw):
    """GET a page as text (always decoded as UTF-8; many sites omit the charset)."""
    h = dict(headers or {})
    if browser:
        h.setdefault("User-Agent", BROWSER_UA)
        h.setdefault("Accept-Language", "en-US,en;q=0.9")

    def make():
        r = request("GET", url, headers=h, **kw)
        if r is None:
            return None
        try:
            r.raise_for_status()
            return r.content.decode("utf-8", "replace")
        except requests.RequestException as e:
            raise EngineError(f"{urlsplit(url).netloc}: {e}") from None
        finally:
            r.close()
    return _cached("text", url, None, h, ttl, kw.get("stop"), make)


SEARCH_HOSTS = ("https://api.deezer.com/",)
RUN_HOSTS = ("https://api.deezer.com/", "https://itunes.apple.com/", "https://musicbrainz.org/",
             "https://coverartarchive.org/")


def prewarm(urls):
    """Connections to these hosts are opened in the background so the first real call finds one waiting.
    MUSICDL_NO_PREWARM=1 turns it off (the tests set it: they must never reach the real services)."""
    if os.environ.get("MUSICDL_NO_PREWARM"):
        return 0
    return netconn.prewarm(urls)


def report(limit=8, since=None):
    """How the busiest hosts have been answering (see netstats.HostStat.snapshot)."""
    return STATS.snapshot(limit, since)


# ---------------------------------------------------------------- downloads

class _Got:
    """What a download has safely on disk so far."""
    __slots__ = ("n", "total", "tag", "no_ranges")

    def __init__(self):
        self.n, self.total, self.tag, self.no_ranges = 0, None, None, False


def _check_status(r):
    if r.status_code >= 400:
        raise HttpStatus(r.status_code, urlsplit(r.url).netloc)


def _content_range(r):
    """(first byte, total size or None) from 'Content-Range: bytes 100-199/1000'."""
    try:
        unit, rest = r.headers.get("Content-Range", "").split(None, 1)
        span, total = rest.split("/", 1)
        return int(span.split("-", 1)[0]), (int(total) if total.strip().isdigit() else None)
    except (ValueError, AttributeError):
        return None, None


def _announced_size(r):
    """The whole file's size as the server announces it (None when unknown or when the body is compressed)."""
    if r.headers.get("Content-Encoding", "").lower() not in ("", "identity"):
        return None
    if r.status_code == 206:
        return _content_range(r)[1]
    n = r.headers.get("Content-Length", "")
    return int(n) if n.isdigit() else None


def _pieces(r, size):
    """The body of a streamed response in the pieces it arrives in (at most `size` bytes each). iter_content waits until
    `size` bytes have come, which on a connection that has dwindled to a trickle is minutes: the speed checks that
    give up on such a connection would only run once the wait was over."""
    raw = getattr(r, "raw", None)
    read1 = getattr(raw, "read1", None)
    if read1 is None:
        yield from r.iter_content(size)
        return
    while True:
        try:
            data = read1(size, decode_content=True)
        except TypeError:                                    # (an older urllib3 without the keyword)
            yield from r.iter_content(size)
            return
        except urllib3_exc.ProtocolError as e:               # the same translations iter_content makes
            raise requests.exceptions.ChunkedEncodingError(e) from e
        except urllib3_exc.DecodeError as e:
            raise requests.exceptions.ContentDecodingError(e) from e
        except urllib3_exc.ReadTimeoutError as e:
            raise requests.exceptions.ConnectionError(e) from e
        except urllib3_exc.SSLError as e:
            raise requests.exceptions.SSLError(e) from e
        if not data:
            return
        yield data


def _fetch(url, dest, got, stop, headers, timeout):
    """One go at the file (or at the rest of it). True when `dest` is complete; raises when the connection breaks."""
    hdr = dict(headers or {})
    if got.n:
        hdr["Range"] = f"bytes={got.n}-"
        if got.tag:
            hdr["If-Range"] = got.tag
    r = request("GET", url, headers=hdr, stream=True, timeout=timeout, retries=2, stop=stop, ok_404=False)
    split, final = False, r.url                              # (the address after redirects: archive.org hands out storage nodes)
    try:
        if r.status_code == 416 and got.n and got.total == got.n:
            return True
        _check_status(r)
        if got.n and not (r.status_code == 206 and _content_range(r)[0] == got.n):
            got.n = 0                                        # the server sent the whole file again: start over
        total = _announced_size(r)
        if got.n == 0:
            got.total, got.tag = total, r.headers.get("ETag") or r.headers.get("Last-Modified")
        elif total:
            got.total = total
        ranged = bool(got.total) and not got.no_ranges and (
            r.status_code == 206 or "bytes" in r.headers.get("Accept-Ranges", "").lower())
        probing = ranged and got.total - got.n >= SEG_MIN_BYTES
        pulse, rate0 = _Pulse(), 0.0
        with open(dest, "r+b" if got.n else "wb") as fh:
            fh.seek(got.n)
            fh.truncate()
            t0, n0 = time.monotonic(), got.n
            for chunk in _pieces(r, 262144):
                if stop is not None and stop.is_set():
                    raise Stopped()
                fh.write(chunk)
                got.n += len(chunk)
                T.add_bytes(len(chunk))
                if pulse.feed(len(chunk)):
                    STATS.get(netconn.host_key(url)).count("stalls")
                    raise _Stalled(f"the connection slowed to a crawl after {got.n} bytes")
                if probing and time.monotonic() - t0 >= SEG_PROBE:
                    probing = False
                    rate0 = (got.n - n0) / (time.monotonic() - t0)
                    if rate0 < SEG_SLOW and got.total - got.n >= SEG_BLOCK:
                        split = True                         # this one connection is held back: use several
                        break
        if not split:
            if got.total and got.n < got.total:
                raise EngineError(f"the connection closed after {got.n} of {got.total} bytes")
            return True
    finally:
        r.close()
    return _segments(url, final, dest, got, stop, headers, timeout, rate0)


def _block(src, fh, a, b, stop, abort, headers, timeout, counter=None):
    """Bytes a..b (inclusive) of the file into `fh` at their place. `src[0]` is where to ask (see _segments)."""
    hdr = dict(headers or {})
    hdr["Range"] = f"bytes={a}-{b}"
    r = request("GET", src[0], headers=hdr, stream=True, timeout=timeout, retries=2, stop=stop, ok_404=False)
    try:
        _check_status(r)
        if r.status_code != 206 or _content_range(r)[0] != a:
            raise _NoRanges("the server does not do byte ranges")
        fh.seek(a)
        left, t0 = b - a + 1, time.monotonic()
        for chunk in _pieces(r, 65536):
            if abort.is_set():
                raise _Aborted()
            if stop is not None and stop.is_set():
                raise Stopped()
            chunk = chunk[:left]
            fh.write(chunk)
            left -= len(chunk)
            T.add_bytes(len(chunk))
            if counter is not None:
                counter[0] += len(chunk)
            took = time.monotonic() - t0
            if took > BLOCK_CRAWL_AFTER and (b - a + 1 - left) / took < BLOCK_CRAWL:
                STATS.get(netconn.host_key(src[0])).count("stalls")
                raise _Stalled("a piece of the file is crawling")
        if left:
            raise EngineError("a piece of the file came back short")
    finally:
        r.close()


class _NoRanges(EngineError):
    pass


class _Stalled(EngineError):
    """A connection that had been fast has all but stopped; asking again (on a new connection) usually fixes it."""


class _Pulse:
    """Notices a connection whose speed has fallen to a sliver of what it had reached (see STALL_*). Call feed() with
    each piece that arrives; True means "give up on this connection"."""

    def __init__(self):
        self.mark, self.n, self.best, self.low = time.monotonic(), 0, 0.0, None

    def feed(self, nbytes):
        self.n += nbytes
        now = time.monotonic()
        span = now - self.mark
        if span < 1.0:
            return False
        rate, self.mark, self.n = self.n / span, now, 0
        self.best = max(self.best, rate)
        if self.best >= STALL_FLOOR and rate < self.best * STALL_FRACTION:
            if self.low is None:
                self.low = now
            return now - self.low >= STALL_SECONDS
        self.low = None
        return False


class _Aborted(Exception):
    """Another connection of the same download has failed for good; this one gives up quietly."""


def _segments(url, final, dest, got, stop, headers, timeout, rate0=0.0):
    """Fetch the rest of the file (got.n .. got.total) in blocks, over as many connections as pay off. On success the file
    is complete. On failure `got.n` is the length of what is certainly good (the unbroken start) and the error is raised.
    Blocks are asked for at `final` (the address the first request ended up at, so each block does not redirect
    again); if that stops working they go back to `url`.

    How many connections: it starts with SEG_START and then watches the file's overall speed. Each time it adds one
    it waits SEG_STEP seconds and keeps it only if the speed rose by SEG_GAIN or more (additive increase); when it did
    not, the host's limit is on the whole file rather than on each connection, and the extra one goes back (as does the
    whole group when the very first step brought nothing). A block that fails halves the number (multiplicative
    decrease) and growth rests for a while. The extra connections come out of a budget shared by every download."""
    src = [final]
    start, total = got.n, got.total
    blocks = [(a, min(a + SEG_BLOCK, total) - 1) for a in range(start, total, SEG_BLOCK)]
    cap = min(SEG_MAX, len(blocks))
    lock, nxt, done, errors, abort = threading.Lock(), [0], set(), [], threading.Event()
    allowed, held, fails = [1], [0], [0]
    counters = [[0] for _ in range(cap)]
    threads = {}
    with open(dest, "r+b") as fh:
        fh.truncate(total)                                   # room for every block to land in place

    def grow():
        """One more connection, if the budget has one. True when it was added."""
        if allowed[0] >= cap or not EXTRA_CONNECTIONS.acquire(blocking=False):
            return False
        held[0] += 1
        allowed[0] += 1
        return True

    def shrink(to):
        while allowed[0] > max(1, to):
            allowed[0] -= 1
            held[0] -= 1
            EXTRA_CONNECTIONS.release()

    def work(i):
        fh = open(dest, "r+b")
        try:
            while not abort.is_set():
                if stop is not None and stop.is_set():
                    raise Stopped()
                with lock:
                    if i >= allowed[0]:                      # the controller took this connection away
                        return
                    k = nxt[0]
                    nxt[0] += 1
                if k >= len(blocks):
                    return
                for attempt in range(3):
                    try:
                        _block(src, fh, blocks[k][0], blocks[k][1], stop, abort, headers, timeout, counters[i])
                        break
                    except (Stopped, _NoRanges):
                        raise
                    except HttpStatus as e:
                        if src[0] != url:                    # the storage address went stale: use the original one
                            src[0] = url
                            continue
                        if e.permanent or attempt == 2:
                            raise
                    except (requests.RequestException, EngineError):
                        if attempt == 2:
                            raise
                    fails[0] += 1
                    T.error()
                    _sleep(stop, _backoff(attempt))
                with lock:
                    done.add(k)
        finally:
            fh.close()

    def guarded(i):
        try:
            work(i)
        except BaseException as e:                           # noqa: BLE001 — reported to the caller below
            errors.append(e)
            abort.set()

    def spawn():
        for i in range(allowed[0]):
            t = threads.get(i)
            if t is None or not t.is_alive():
                threads[i] = t = threading.Thread(target=guarded, args=(i,), daemon=True, name="range")
                t.start()

    def moved():
        return sum(c[0] for c in counters)

    try:
        for _ in range(SEG_START - 1):
            if not grow():
                break
        spawn()
        trace = collections.deque([(time.monotonic(), 0)], maxlen=64)
        before, check, rest, seen_fails = 0.0, time.monotonic() + SEG_STEP + 0.3, 0.0, 0
        first, trial = allowed[0] > 1, False
        while any(t.is_alive() for t in threads.values()):
            if abort.wait(0.1):
                break
            now = time.monotonic()
            trace.append((now, moved()))
            if fails[0] > seen_fails:                        # something broke: back off, then grow again slowly
                seen_fails = fails[0]
                shrink(allowed[0] // 2)
                rest, check, first, trial = now + 8.0, now + SEG_STEP, False, False
                continue
            if now < check:
                continue
            ref = next((p for p in trace if p[0] >= now - 0.6), trace[0])
            rate = (trace[-1][1] - ref[1]) / max(0.05, trace[-1][0] - ref[0])
            if first:                                        # the first group against the one connection it replaced
                first = False
                if rate0 > 0 and rate < rate0 * (1 + SEG_GAIN):
                    shrink(1)                                # the limit is on the whole file, not on each connection
                    rest, check, before = now + 10.0, now + 1.0, rate
                    continue
            elif trial:                                      # the connection added a moment ago: did it pay?
                trial = False
                if rate < before * (1 + SEG_GAIN):
                    shrink(allowed[0] - 1)
                    rest, check = now + 6.0, now + 0.7
                    continue
            before = rate
            if now >= rest and rate > 0 and len(blocks) - nxt[0] > allowed[0] and grow():
                spawn()
                trial, check = True, now + SEG_STEP + 0.3
                continue
            check = now + 0.7
        for t in list(threads.values()):
            t.join()
    finally:
        shrink(1)
    if not errors:
        got.n = total
        return True
    k = 0
    while k in done:
        k += 1
    got.n = blocks[k][0] if k < len(blocks) else total       # the unbroken start survives; the rest is fetched again
    with open(dest, "r+b") as fh:
        fh.truncate(got.n)
    real = [e for e in errors if not isinstance(e, _Aborted)] or errors      # the cause, not the ones that gave up after it
    first = next((e for e in real if isinstance(e, Stopped)), real[0])
    if isinstance(first, _NoRanges):
        got.no_ranges = True
    raise first


def download(url, dest, stop=None, min_bytes=0, headers=None, timeout=(10, 30)):
    """Stream a file to `dest`, counting bytes for the speed graph. Returns the size written.

    A connection that breaks half-way is picked up again from the byte it reached; the finished file must be as long
    as the server said it would be; and a host that throttles each connection is given several (see _segments)."""
    got, last = _Got(), None
    for attempt in range(3):
        if attempt:
            _sleep(stop, _backoff(attempt - 1))
        try:
            if _fetch(url, dest, got, stop, headers, timeout):
                break
        except (Stopped, HostDown):
            raise
        except HttpStatus as e:
            if e.permanent:
                raise
            last = e
        except (requests.RequestException, EngineError) as e:
            last = e
        T.error()
    else:
        raise EngineError(str(last) or "download failed") from None
    size = os.path.getsize(dest)
    if got.total and size != got.total:
        raise EngineError(f"incomplete download ({size} of {got.total} bytes)")
    if size < min_bytes:
        raise EngineError("download too small")
    return size


# ---------------------------------------------------------------- compute-once cache

class Memo:
    """Compute each key once, even when several threads ask at the same moment: the others wait for the first one's
    result. Keeps the most recent `limit` results. (A computation that is stopped is not remembered.)"""

    class _Slot:
        __slots__ = ("event", "value", "error")

        def __init__(self):
            self.event, self.value, self.error = threading.Event(), None, None

    def __init__(self, limit=64):
        self.limit = limit
        self._lock = threading.Lock()
        self._d = collections.OrderedDict()
        self.computed = self.reused = 0

    def get(self, key, make, stop=None):
        with self._lock:
            slot = self._d.get(key)
            owner = slot is None
            if owner:
                slot = self._d[key] = Memo._Slot()
                self.computed += 1
            else:
                self._d.move_to_end(key)
                self.reused += 1
        if owner:
            try:
                slot.value = make()
            except Stopped:
                with self._lock:
                    self._d.pop(key, None)
                slot.error = Stopped()
                raise
            except BaseException as e:                       # noqa: BLE001 — handed to everyone who waited
                slot.error = e
                raise
            finally:
                slot.event.set()
                with self._lock:
                    while len(self._d) > self.limit:
                        self._d.popitem(last=False)
        else:
            while not slot.event.wait(0.25):
                if stop is not None and stop.is_set():
                    raise Stopped()
            if slot.error is not None:
                raise slot.error
        return slot.value

    def forget(self, key):
        """Drop one remembered result (a failure that may not happen again must not be remembered)."""
        with self._lock:
            self._d.pop(key, None)

    def clear(self):
        with self._lock:
            self._d.clear()
            self.computed = self.reused = 0
