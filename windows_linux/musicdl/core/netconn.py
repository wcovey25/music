"""
netconn.py — how connections are made and kept.

* One connection pool for the whole program (a single `requests.Session`), not one per worker thread: a keep-alive
  connection that one worker opened is reused by the next worker that talks to the same host, instead of every thread
  paying its own TCP and TLS set-up (two or three round trips each). The session keeps no cookies, so sharing it between
  threads shares nothing else.
* Connecting is "happy eyeballs" (RFC 8305): the addresses a name resolves to are tried one after another with a short
  head start for each, the first to connect wins and the rest are dropped. A route that is broken (the classic case: IPv6
  announced but not working) costs a fraction of a second instead of the whole connect timeout, and the family that
  worked is tried first next time.
* Name look-ups are remembered for a minute, so reconnecting to a host does not ask the resolver again.
* `prewarm()` opens connections to the hosts a run is about to use, in the background, so the first real request finds
  one waiting.

Windows: a non-blocking connect that is under way answers WSAEWOULDBLOCK (10035), and one that fails is reported by
select() in its "exceptional" set, which Python's selectors module hands back as writable — so the same race works.
"""
import errno
import http.cookiejar
import selectors
import socket
import threading
import time
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NameResolutionError, NewConnectionError

from .. import USER_AGENT
from .netstats import STATS

STAGGER = 0.25                  # seconds one address is given before the next is started alongside it
STAGGER_MIN, STAGGER_MAX = 0.08, 0.30
DNS_TTL = 60.0
POOL_HOSTS, POOL_SIZE = 32, 16  # hosts kept, connections kept per host (more are made when needed and dropped afterwards)
_PENDING = {errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK, errno.EAGAIN, 10035}
_DEFAULT = socket._GLOBAL_DEFAULT_TIMEOUT


def host_key(url):
    """The name statistics for a URL are kept under: the host, with the port when it is not the usual one."""
    p = urlsplit(url if "//" in url else "//" + url)
    host = (p.hostname or p.netloc or "").lower()
    return f"{host}:{p.port}" if p.port not in (None, 80, 443) else host


# ---------------------------------------------------------------- name look-ups

_dns, _dns_lock = {}, threading.Lock()


def resolve(host, port):
    """[(family, type, proto, canonname, sockaddr)] for a name, remembered for DNS_TTL seconds."""
    key, now = (host, port), time.monotonic()
    hit = _dns.get(key)
    if hit and hit[0] > now:
        return hit[1]
    infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    with _dns_lock:
        if len(_dns) > 256:
            _dns.clear()
        _dns[key] = (now + DNS_TTL, infos)
    return infos


def forget_names():
    with _dns_lock:
        _dns.clear()


def _interleave(infos, first):
    """Addresses alternating between families, starting with `first` (the family that worked last) or else with whatever
    the system lists first."""
    groups = {}
    for info in infos:
        groups.setdefault(info[0], []).append(info)
    if len(groups) < 2:
        return list(infos)
    order = sorted(groups, key=lambda f: 0 if f == (first if first in groups else infos[0][0]) else 1)
    out = []
    for i in range(max(len(g) for g in groups.values())):
        for f in order:
            if i < len(groups[f]):
                out.append(groups[f][i])
    return out


# ---------------------------------------------------------------- connecting

def _tune(sock, timeout, options):
    for opt in options or ():
        try:
            sock.setsockopt(*opt)
        except OSError:
            pass
    sock.settimeout(socket.getdefaulttimeout() if timeout is _DEFAULT else timeout)
    return sock


def _one(info, timeout, source, options):
    af, st, proto, _c, sa = info
    sock = socket.socket(af, st, proto)
    try:
        if timeout is not _DEFAULT:
            sock.settimeout(timeout)
        if source:
            sock.bind(source)
        sock.connect(sa)
        return _tune(sock, timeout, options)
    except BaseException:
        sock.close()
        raise


def _race(infos, timeout, source, options, stagger):
    """Connect to the addresses with a head start of `stagger` seconds for each; return the first socket that connects."""
    limit = 60.0 if timeout is _DEFAULT or timeout is None else float(timeout)
    deadline = time.monotonic() + limit
    sel = selectors.DefaultSelector()
    live, nxt, next_at, last = {}, 0, 0.0, None
    try:
        while True:
            now = time.monotonic()
            if nxt < len(infos) and (now >= next_at or not live):
                af, st, proto, _c, sa = infos[nxt]
                nxt += 1
                next_at = now + stagger
                try:
                    s = socket.socket(af, st, proto)
                except OSError as e:
                    last = e
                    continue
                try:
                    s.setblocking(False)
                    if source:
                        s.bind(source)
                    code = s.connect_ex(sa)
                except OSError as e:
                    s.close()
                    last = e
                    continue
                if code == 0:
                    return _tune(s, timeout, options)
                if code in _PENDING:
                    live[s] = af
                    sel.register(s, selectors.EVENT_WRITE)
                else:
                    s.close()
                    last = OSError(code, errno.errorcode.get(code, "connect failed"))
                continue
            if not live and nxt >= len(infos):
                raise last or OSError("no address to connect to")
            if now >= deadline:
                raise socket.timeout("timed out")
            wake = deadline if nxt >= len(infos) else min(deadline, next_at)
            for key, _ev in sel.select(max(0.0, wake - now)):
                s = key.fileobj
                sel.unregister(s)
                af = live.pop(s)
                err = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if err == 0:
                    for other in live:
                        other.close()
                    live.clear()
                    s.setblocking(True)
                    return _tune(s, timeout, options)
                s.close()
                last = OSError(err, errno.errorcode.get(err, "connect failed"))
                next_at = 0.0                                       # that one failed: start the next at once
    finally:
        for s in live:
            s.close()
        sel.close()


def connect(address, timeout=_DEFAULT, source_address=None, socket_options=None):
    """Like socket.create_connection, with address racing and the statistics of how long it took."""
    host, port = address
    host = host.strip("[]")
    key = f"{host}:{port}" if port not in (None, 80, 443) else host
    stat = STATS.get(key)
    t0 = time.perf_counter()
    infos = resolve(host, port)
    infos = _interleave(infos, stat.family)
    if len(infos) == 1:
        sock = _one(infos[0], timeout, source_address, socket_options)
    else:
        head = stat.connect
        gap = STAGGER if head is None else min(STAGGER_MAX, max(STAGGER_MIN, 2.5 * head / 1000.0))
        sock = _race(infos, timeout, source_address, socket_options, gap)
    stat.connected((time.perf_counter() - t0) * 1000.0, sock.family)
    return sock


class _Plain(HTTPConnection):
    def _new_conn(self):
        try:
            return connect((getattr(self, "_dns_host", self.host), self.port), self.timeout,
                           self.source_address, self.socket_options)
        except socket.gaierror as e:
            raise NameResolutionError(self.host, self, e) from e
        except socket.timeout as e:
            raise ConnectTimeoutError(self, f"Connection to {self.host} timed out. (connect timeout={self.timeout})") from e
        except OSError as e:
            raise NewConnectionError(self, f"Failed to establish a new connection: {e}") from e


class _Secure(HTTPSConnection):
    _new_conn = _Plain._new_conn


class _HTTPPool(HTTPConnectionPool):
    ConnectionCls = _Plain


class _HTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _Secure


class _Adapter(HTTPAdapter):
    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        super().init_poolmanager(connections, maxsize, block, **pool_kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _HTTPPool, "https": _HTTPSPool}


class _NoCookies(http.cookiejar.CookiePolicy):
    """Nothing is stored or sent: every call stands alone, so the one session can serve every thread."""
    netscape, rfc2965, hide_cookie2 = True, False, False

    def set_ok(self, cookie, request):
        return False

    def return_ok(self, cookie, request):
        return False

    def domain_return_ok(self, domain, request):
        return False

    def path_return_ok(self, path, request):
        return False


# ---------------------------------------------------------------- the shared session

_session, _session_lock = None, threading.Lock()


def session():
    global _session
    s = _session
    if s is None:
        with _session_lock:
            if _session is None:
                new = requests.Session()
                new.headers["User-Agent"] = USER_AGENT
                new.cookies.set_policy(_NoCookies())
                adapter = _Adapter(pool_connections=POOL_HOSTS, pool_maxsize=POOL_SIZE, max_retries=0)
                new.mount("https://", adapter)
                new.mount("http://", adapter)
                _session = new
            s = _session
    return s


def close_all():
    """Close every idle connection (and let the next call make a new session)."""
    global _session
    with _session_lock:
        s, _session = _session, None
    if s is not None:
        s.close()


def prewarm(urls, timeout=4.0):
    """Open (and park in the pool) a connection to each URL's host in the background. Best effort: a failure is ignored,
    the real request will simply connect for itself."""
    seen, todo = set(), []
    for u in urls:
        k = host_key(u)
        if k and k not in seen:
            seen.add(k)
            todo.append(u)

    def one(url):
        try:
            sess = session()
            adapter = sess.get_adapter(url)
            prep = requests.Request("GET", url).prepare()
            # what a real request would use (environment proxies, certificate bundle); yt-dlp swaps in a select_proxy
            # that fails when it is given no proxies at all, so they are always passed
            settings = sess.merge_environment_settings(url, {}, None, True, None)
            proxies, verify = settings["proxies"] or {}, settings["verify"]
            getter = getattr(adapter, "get_connection_with_tls_context", None)
            pool = getter(prep, verify, proxies=proxies) if getter else adapter.get_connection(url, proxies)
            conn = pool._get_conn()
            try:
                conn.timeout = timeout
                if conn.sock is None:
                    conn.connect()
            except BaseException:
                conn.close()
                raise
            finally:
                pool._put_conn(conn)
        except Exception:                                          # noqa: BLE001 — a warm-up is only ever a favour
            pass

    for u in todo:
        threading.Thread(target=one, args=(u,), name="prewarm", daemon=True).start()
    return len(todo)
