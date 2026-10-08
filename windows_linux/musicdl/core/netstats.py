"""
netstats.py — what the network has been like, per host.

Every request that finishes tells this module how long the server took. From that it keeps, for each host:

* a smoothed round-trip time and its variation (the estimator TCP uses for its retransmit timer, RFC 6298), which
  turns into timeouts that fit the host: a server that usually answers in 120 ms is given seconds, not half a minute,
  before a request is given up on and tried again on a fresh connection; a slow one is given what it needs;
* the last few dozen latencies, for the median and the 95th percentile (the dashboard shows both, and a request that
  has taken longer than the host's usual tail is the one worth sending a second copy of: see netio's hedging);
* how long connecting took, the address family that worked last (so a broken IPv6 route is skipped the next time),
  and counts of calls, failures, hedges and cache hits.

Nothing here talks to the network. All of it is cheap and lock-protected; any thread may record and the UI may read.
"""
import threading
import time
from collections import deque

WINDOW = 64                      # latencies kept per host
MIN_SAMPLES = 3                  # before the estimate is trusted


class HostStat:
    def __init__(self, host):
        self.host = host
        self.calls = self.errors = self.hedges = self.hedge_wins = self.cache_hits = self.stalls = 0
        self.srtt = None                                    # smoothed round-trip, ms
        self.rttvar = 0.0                                   # its mean deviation, ms
        self.connect = None                                 # smoothed time to open a connection, ms
        self.family = None                                  # socket family that connected last
        self.recent = deque(maxlen=WINDOW)
        self.bytes = 0
        self.first = time.monotonic()
        self.last = 0.0
        self.last_error = ""
        self._lock = threading.Lock()

    # ---- recording
    def observe(self, ms):
        """One finished call took `ms` (time to the full response, or to the headers for a streamed one)."""
        with self._lock:
            self.calls += 1
            self.last = time.monotonic()
            self.recent.append(ms)
            if self.srtt is None:
                self.srtt, self.rttvar = ms, ms / 2.0
            else:
                self.rttvar = 0.75 * self.rttvar + 0.25 * abs(self.srtt - ms)
                self.srtt = 0.875 * self.srtt + 0.125 * ms

    def failed(self, what=""):
        with self._lock:
            self.errors += 1
            self.last = time.monotonic()
            if what:
                self.last_error = what[:120]

    def connected(self, ms, family=None):
        with self._lock:
            self.connect = ms if self.connect is None else 0.7 * self.connect + 0.3 * ms
            if family is not None:
                self.family = family

    def add_bytes(self, n):
        self.bytes += n

    def count(self, what):
        with self._lock:
            setattr(self, what, getattr(self, what) + 1)

    # ---- reading
    def percentile(self, q):
        with self._lock:
            vals = sorted(self.recent)
        if not vals:
            return None
        k = (len(vals) - 1) * q
        lo = int(k)
        hi = min(len(vals) - 1, lo + 1)
        return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)

    def rto(self):
        """Milliseconds a call should normally be done within (srtt + 4 x variation); None while there is too little to go on."""
        with self._lock:
            if self.srtt is None or len(self.recent) < MIN_SAMPLES:
                return None
            return self.srtt + 4.0 * self.rttvar

    def tail(self):
        """Milliseconds after which a call is slower than this host usually is (its 95th percentile), None when unknown."""
        with self._lock:
            enough = len(self.recent) >= 8
        return self.percentile(0.95) if enough else None

    def timeouts(self, base=(8.0, 30.0)):
        """(connect, read) in seconds for a call to this host: the defaults until the host has been measured, then
        what it needs with room to spare, never beyond the defaults."""
        rto = self.rto()
        with self._lock:
            conn = self.connect
        c, r = base
        if conn is not None:
            c = min(base[0], max(2.0, 4.0 * conn / 1000.0))
        if rto is not None:
            r = min(base[1], max(8.0, 8.0 * rto / 1000.0))
        return c, r

    def snapshot(self):
        with self._lock:
            n = len(self.recent)
        return {"host": self.host, "calls": self.calls, "errors": self.errors, "hedges": self.hedges,
                "hedge_wins": self.hedge_wins, "cache_hits": self.cache_hits, "stalls": self.stalls,
                "p50": self.percentile(0.5), "p95": self.percentile(0.95), "srtt": self.srtt, "connect": self.connect,
                "samples": n, "bytes": self.bytes, "last": self.last, "family": self.family}


class Registry:
    def __init__(self):
        self._lock = threading.Lock()
        self._d = {}

    def get(self, host):
        s = self._d.get(host)
        if s is None:
            with self._lock:
                s = self._d.setdefault(host, HostStat(host))
        return s

    def hosts(self):
        with self._lock:
            return list(self._d.values())

    def snapshot(self, limit=8, since=None):
        """The busiest hosts first (those used within `since` seconds when given)."""
        now = time.monotonic()
        rows = [h.snapshot() for h in self.hosts() if h.calls or h.errors]
        if since is not None:
            rows = [r for r in rows if now - r["last"] <= since]
        rows.sort(key=lambda r: (r["last"], r["calls"]), reverse=True)
        return rows[:limit]

    def reset(self):
        with self._lock:
            self._d.clear()


STATS = Registry()
