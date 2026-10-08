"""
netio.py — the single place that talks HTTP.

* one `requests.Session` per thread (connection reuse without sharing a session across threads)
* per-host rate limiters shared by all workers; they adapt: a host that answers 429 / "slow down" makes every worker
  back off together (and honours Retry-After), a host that keeps answering gets asked a little more often
  (MusicBrainz asks for one request per second and is never sped up)
* a circuit breaker per host: when a host has failed several times in a row, calls fail at once for a while instead
  of every worker burning its own retries and back-off sleeps on a dead server; one test request then decides
* every call feeds the telemetry (bytes, latency, searches) and honours the Stop event
* retries with jittered back-off on throttling / transient errors (and no pointless sleep after the last try)
* downloads resume where they broke off, are checked against the size the server announced, and — when the host
  limits every single connection — are split into byte ranges fetched side by side
* `Memo` computes a value once even when several workers ask for it at the same moment (album art shared by a
  whole album is downloaded and processed once)
"""
import collections
import email.utils
import os
import random
import threading
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from .. import USER_AGENT
from ..telemetry import T
from .models import EngineError, Stopped

BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")
_local = threading.local()

# ---- circuit breaker
TRIP_AFTER = 4                  # consecutive failures that open the circuit
COOLDOWN = 3.0                  # seconds the first time; doubles each time it trips again
COOLDOWN_MAX = 45.0

# ---- segmented downloads
SEG_MIN_BYTES = 3 * 2 ** 20     # smaller files are not worth splitting
SEG_BLOCK = 1 * 2 ** 20         # size of one range request
SEG_MAX = 4                     # connections for one file (including the first)
SEG_SLOW = 2.0e6                # bytes/second: one connection slower than this looks like a per-connection limit
SEG_PROBE = 0.6                 # seconds spent measuring before deciding
EXTRA_CONNECTIONS = threading.BoundedSemaphore(10)      # extra connections across every running download


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
    s = getattr(_local, "s", None)
    if s is None:
        s = _local.s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        adapter = HTTPAdapter(pool_connections=6, pool_maxsize=6, max_retries=0)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
    return s


def close_thread_session():
    s = getattr(_local, "s", None)
    if s is not None:
        s.close()
        _local.s = None


class Limiter:
    """At most one request per `interval` seconds across all threads. The interval adapts between `floor` and `ceil`:
    `slow()` (the host said "too many requests") doubles it and can park every thread for a while; `good()` shaves it
    after a run of successes. With floor == ceil it never changes."""

    def __init__(self, interval, floor=None, ceil=None):
        self.interval, self.base = interval, interval
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


MB_LIMIT = Limiter(1.1, floor=1.1, ceil=4.0)               # MusicBrainz: one per second, never faster
CAA_LIMIT = Limiter(0.25, floor=0.15)
DEEZER_LIMIT = Limiter(0.25, floor=0.125)                   # Deezer allows ~10 requests/s
ITUNES_LIMIT = Limiter(1.6, floor=1.2, ceil=14.0)           # iTunes: 60 requests at ~43/min went through unchallenged; it
#                                                             answers 403 when it has had enough, which slows everyone down
ARCHIVE_LIMIT = Limiter(0.15, floor=0.08)
LYRICS_LIMIT = Limiter(0.3, floor=0.2)
WEB_LIMIT = Limiter(0.2, floor=0.1)


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


def reset_health():
    with _health_lock:
        _health.clear()
    for lim in (MB_LIMIT, CAA_LIMIT, DEEZER_LIMIT, ITUNES_LIMIT, ARCHIVE_LIMIT, LYRICS_LIMIT, WEB_LIMIT):
        lim.reset()


def request(method, url, *, params=None, headers=None, json_body=None, limiter=None, timeout=(8, 30), retries=3,
            stop=None, search=False, stream=False, ok_404=True, throttle=()):
    """One HTTP call with retries. Returns the Response (or None on 404 when ok_404). Raises EngineError
    (HostDown when the host has been failing and was not even tried). `throttle`: extra statuses that mean "too many
    requests" for this host (iTunes says 403), treated like 429."""
    last = None
    host = urlsplit(url).netloc
    h = health(host)
    for attempt in range(max(1, retries)):
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
        t0 = time.perf_counter()
        try:
            r = http().request(method, url, params=params, headers=headers, json=json_body, timeout=timeout,
                               stream=stream)
        except requests.RequestException as e:
            last = e
            T.error()
            h.failure()
        except BaseException:
            h.release()                                      # (an odd error must not leave the host stuck "being tested")
            raise
        else:
            if not stream:
                T.latency(host, (time.perf_counter() - t0) * 1000)
                T.add_bytes(len(r.content))
            status = r.status_code
            if status == 404 and ok_404:
                h.success()
                r.close()
                return None
            if status in (429, 500, 502, 503, 504) or status in throttle:
                last = HttpStatus(status)
                T.error()
                wait = retry_after_seconds(r.headers.get("Retry-After"))
                r.close()
                if status == 429 or status in throttle or (status == 503 and wait is not None):
                    h.success()                              # the host is alive; it is only asking for patience
                    if limiter:
                        limiter.slow(wait or (15.0 if status in throttle else 0.0))
                    if wait is not None and attempt + 1 < retries:
                        _sleep(stop, min(30.0, wait))
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
    raise EngineError(f"{host}: {last}")


def get_json(url, params=None, **kw):
    """GET -> parsed JSON (None on 404 or an empty body)."""
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


def get_text(url, headers=None, browser=False, **kw):
    """GET a page as text (always decoded as UTF-8; many sites omit the charset)."""
    h = dict(headers or {})
    if browser:
        h.setdefault("User-Agent", BROWSER_UA)
        h.setdefault("Accept-Language", "en-US,en;q=0.9")
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
        with open(dest, "r+b" if got.n else "wb") as fh:
            fh.seek(got.n)
            fh.truncate()
            t0, n0 = time.monotonic(), got.n
            for chunk in r.iter_content(262144):
                if stop is not None and stop.is_set():
                    raise Stopped()
                fh.write(chunk)
                got.n += len(chunk)
                T.add_bytes(len(chunk))
                if probing and time.monotonic() - t0 >= SEG_PROBE:
                    probing = False
                    if (got.n - n0) / (time.monotonic() - t0) < SEG_SLOW and got.total - got.n >= SEG_BLOCK:
                        split = True                         # this one connection is held back: use several
                        break
        if not split:
            if got.total and got.n < got.total:
                raise EngineError(f"the connection closed after {got.n} of {got.total} bytes")
            return True
    finally:
        r.close()
    return _segments(url, final, dest, got, stop, headers, timeout)


def _block(src, fh, a, b, stop, abort, headers, timeout):
    """Bytes a..b (inclusive) of the file into `fh` at their place. `src[0]` is where to ask (see _segments)."""
    hdr = dict(headers or {})
    hdr["Range"] = f"bytes={a}-{b}"
    r = request("GET", src[0], headers=hdr, stream=True, timeout=timeout, retries=2, stop=stop, ok_404=False)
    try:
        _check_status(r)
        if r.status_code != 206 or _content_range(r)[0] != a:
            raise _NoRanges("the server does not do byte ranges")
        fh.seek(a)
        left = b - a + 1
        for chunk in r.iter_content(65536):
            if abort.is_set():
                raise _Aborted()
            if stop is not None and stop.is_set():
                raise Stopped()
            chunk = chunk[:left]
            fh.write(chunk)
            left -= len(chunk)
            T.add_bytes(len(chunk))
        if left:
            raise EngineError("a piece of the file came back short")
    finally:
        r.close()


class _NoRanges(EngineError):
    pass


class _Aborted(Exception):
    """Another connection of the same download has failed for good; this one gives up quietly."""


def _segments(url, final, dest, got, stop, headers, timeout):
    """Fetch the rest of the file (got.n .. got.total) in blocks, several connections at once. On success the file is
    complete. On failure `got.n` is the length of what is certainly good (the unbroken start) and the error is raised.
    Blocks are asked for at `final` (the address the first request ended up at, so each block does not redirect
    again); if that stops working they go back to `url`."""
    src = [final]
    start, total = got.n, got.total
    blocks = [(a, min(a + SEG_BLOCK, total) - 1) for a in range(start, total, SEG_BLOCK)]
    extra = 0
    while extra < min(SEG_MAX, len(blocks)) - 1 and EXTRA_CONNECTIONS.acquire(blocking=False):
        extra += 1
    lock, nxt, done, errors, abort = threading.Lock(), [0], set(), [], threading.Event()
    with open(dest, "r+b") as fh:
        fh.truncate(total)                                   # room for every block to land in place

    def work():
        fh = open(dest, "r+b")
        try:
            while not abort.is_set():
                if stop is not None and stop.is_set():
                    raise Stopped()
                with lock:
                    i = nxt[0]
                    nxt[0] += 1
                if i >= len(blocks):
                    return
                for attempt in range(3):
                    try:
                        _block(src, fh, blocks[i][0], blocks[i][1], stop, abort, headers, timeout)
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
                    T.error()
                    _sleep(stop, _backoff(attempt))
                with lock:
                    done.add(i)
        finally:
            fh.close()

    def guarded():
        try:
            work()
        except BaseException as e:                           # noqa: BLE001 — reported to the caller below
            errors.append(e)
            abort.set()

    threads = [threading.Thread(target=guarded, daemon=True, name="range") for _ in range(extra)]
    try:
        for t in threads:
            t.start()
        guarded()
        for t in threads:
            t.join()
    finally:
        for _ in range(extra):
            EXTRA_CONNECTIONS.release()
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
