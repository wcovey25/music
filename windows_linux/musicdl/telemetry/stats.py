"""
stats.py — the numbers behind the dashboard: download speed, searches per minute, API latency, time left.

One process-wide Telemetry object (T) collects from every thread (cheap, lock-protected). A sampler thread turns the
running byte counter into a speed history twice a second and, once a second, reads what the computer is doing
(monitor.py). The UI only reads.

How the numbers are made accurate
* Speed is the bytes that arrived between two samples divided by the time that really passed between them (a sample
  that came late doesn't read as a dip), smoothed with a time constant rather than a per-sample weight, so the line
  looks the same whatever the sampling jitter. The average is over the seconds something was actually downloading, not
  over the time spent planning or encoding.
* Time left comes from how songs have really been finishing: a recency-weighted mean and spread of the gaps between
  finished songs, a gap that is still open (nothing finished for a while) counting as it grows. The
  answer is a range (see eta_range), narrower the more songs there are left to average over and the steadier the pace.
"""
import math
import threading
import time
from collections import deque

from .. import platform_
from .monitor import MONITOR

SAMPLE_EVERY = 0.5            # seconds between speed samples
HISTORY = 120                 # samples kept (one minute)
TAU = 0.8                     # seconds: the smoothing time constant of the speed line
BUSY_BYTES = 4096.0           # a sample above this many bytes/second counts as "downloading" for the average
CAP = 12.0                    # a finished gap counts for at most this many times the usual one (a hiccup, not the pace)
GAP_DECAY = 0.90              # how fast older gaps between finished songs stop counting (per song)
Z80 = 1.28                    # the multiplier of the spread for an 80% range
DRIFT_WEIGHT = 1.2            # how much a recent change of pace widens the range
REGULARITY = 0.5              # songs of a pool finish more evenly than at random, so their gaps add up to less spread
FLOOR = 0.08                  # the least relative spread: no estimate of a run is better than this


def _weighted_median(values, weights):
    pairs = sorted(zip(values, weights))
    half, run = sum(weights) / 2.0, 0.0
    for v, w in pairs:
        run += w
        if run >= half:
            return v
    return pairs[-1][0]


class Telemetry:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.reset()

    def reset(self):
        with self._lock:
            now = self._clock()
            self.t_start = now
            self.t_end = None                                        # set when the run ends: the clock stops there
            self.bytes_total = 0
            self._last_bytes, self._last_t = 0, now
            self._ema = None
            self.speed = deque(maxlen=HISTORY)                       # bytes/second, smoothed
            self.raw = deque(maxlen=HISTORY)                         # bytes/second between two samples, as measured
            self.peak = 0.0
            self.active_s = 0.0                                      # seconds in which data was arriving
            self.searches = deque(maxlen=2000)                       # monotonic timestamps
            self.search_total = 0
            self.latencies = deque(maxlen=240)                       # (timestamp, ms)
            self.latency_by_host = {}
            self.completions = deque(maxlen=64)                      # monotonic times songs finished
            self.songs_done = 0
            self.requests = 0
            self.errors = 0
            self._ticks = 0
        MONITOR.reset()

    # ---- recording (called from worker threads)

    def add_bytes(self, n):
        with self._lock:
            self.bytes_total += n

    def search(self):
        with self._lock:
            self.searches.append(self._clock())
            self.search_total += 1

    def latency(self, host, ms):
        with self._lock:
            self.requests += 1
            self.latencies.append((self._clock(), float(ms)))
            prev = self.latency_by_host.get(host)
            self.latency_by_host[host] = ms if prev is None else prev * 0.7 + ms * 0.3

    def error(self):
        with self._lock:
            self.errors += 1

    def song_done(self):
        with self._lock:
            self.songs_done += 1
            self.completions.append(self._clock())

    # ---- sampling

    def sample(self, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            dt = now - self._last_t
            if dt <= 0:
                return
            inst = max(0.0, (self.bytes_total - self._last_bytes) / dt)
            self._ema = inst if self._ema is None else self._ema + (inst - self._ema) * (1.0 - math.exp(-dt / TAU))
            self.raw.append(inst)
            self.speed.append(self._ema)
            self.peak = max(self.peak, self._ema)
            if inst >= BUSY_BYTES:
                self.active_s += dt
            self._last_bytes, self._last_t = self.bytes_total, now

    def start(self):
        """Begin sampling in the background. A new run starts from nothing."""
        self.stop()
        self.reset()
        stop = self._stop = threading.Event()                        # (each sampler has its own, so a late one can't revive)
        self._thread = threading.Thread(target=self._run, args=(stop,), name="telemetry", daemon=True)
        self._thread.start()

    def _run(self, stop):
        platform_.thread_priority(1)                                 # a reading is never urgent: leave the fast cores alone
        due = time.monotonic() + SAMPLE_EVERY
        while not stop.wait(max(0.0, due - time.monotonic())):
            due += SAMPLE_EVERY
            now = time.monotonic()
            if due < now:                                            # fell behind (the PC slept): don't catch up in a burst
                due = now + SAMPLE_EVERY
            try:
                self.sample()
                self._ticks += 1
                if self._ticks % 2 == 0:
                    MONITOR.sample()
            except Exception:                                        # noqa: BLE001 — a reading must never take anything down
                pass

    def stop(self):
        """Stop sampling; the numbers stay as they were (the clock stops too)."""
        self._stop.set()
        with self._lock:
            if self.t_end is None:
                self.t_end = self._clock()

    # ---- readings (called from the UI thread)

    def elapsed(self):
        with self._lock:
            return (self.t_end or self._clock()) - self.t_start

    def speed_history(self):
        with self._lock:
            return list(self.speed)

    def raw_history(self):
        with self._lock:
            return list(self.raw)

    def speed_now(self):
        with self._lock:
            return self.speed[-1] if self.speed else 0.0

    def avg_speed(self):
        """Bytes per second over the time data was arriving (0 before any has)."""
        with self._lock:
            return self.bytes_total / self.active_s if self.active_s > 0.5 else 0.0

    def searches_per_min(self):
        now = self._clock()
        with self._lock:
            recent = sum(1 for t in self.searches if now - t <= 60)
            span = min(60.0, max(5.0, (self.t_end or now) - self.t_start))
        return recent * 60.0 / span

    def latency_ms(self):
        """Mean API round-trip over the last ~20 calls (None until something has been measured)."""
        with self._lock:
            vals = [ms for _, ms in list(self.latencies)[-20:]]
        return sum(vals) / len(vals) if vals else None

    def latency_history(self, n=60):
        with self._lock:
            return [ms for _, ms in list(self.latencies)[-n:]]

    def latency_percentiles(self, n=120):
        """(median, 95th percentile) milliseconds over the last `n` calls, or None."""
        with self._lock:
            vals = sorted(ms for _, ms in list(self.latencies)[-n:])
        if not vals:
            return None
        pick = lambda q: vals[min(len(vals) - 1, int(round((len(vals) - 1) * q)))]        # noqa: E731
        return pick(0.5), pick(0.95)

    # ---- time left

    def eta_range(self, remaining):
        """(expected, soonest, latest) seconds until `remaining` more songs are done, the last two being an 80% range;
        None until three songs have finished (nothing to go on)."""
        if remaining <= 0:
            return 0.0, 0.0, 0.0
        now = self._clock()
        with self._lock:
            c = list(self.completions)
            n = self.songs_done
        if n < 3 or len(c) < 3:
            return None
        decay = lambda k: [GAP_DECAY ** i for i in range(k - 1, -1, -1)]                  # noqa: E731  (the newest weighs most)
        gaps = [b - a for a, b in zip(c, c[1:])]
        # one song that took minutes is not the pace: a finished gap counts for at most CAP times the usual one
        usual = _weighted_median(gaps, decay(len(gaps)))
        gaps = [min(g, CAP * usual) for g in gaps]
        stall = now - c[-1]
        if stall > sum(gaps) / len(gaps):                            # nothing has finished for longer than usual:
            gaps.append(stall)                                       # a stall in progress is evidence the pace has dropped
        weights = decay(len(gaps))
        mean = sum(w * g for w, g in zip(weights, gaps)) / sum(weights)
        if mean <= 0:
            return None
        var = sum(w * (g - mean) ** 2 for w, g in zip(weights, gaps)) / sum(weights)
        recent = gaps[-6:]
        drift = abs(sum(recent) / len(recent) - mean) / mean         # has the pace changed lately?
        expected = remaining * mean
        # the spread of a sum of `remaining` gaps shrinks with their number; a changing pace and plain unpredictability add to it
        rel = math.sqrt(REGULARITY * (math.sqrt(var) / mean) ** 2 / max(1.0, remaining)
                        + (DRIFT_WEIGHT * drift) ** 2 + FLOOR ** 2)
        return expected, expected * max(0.35, 1.0 - Z80 * rel), expected * (1.0 + Z80 * rel)

    def eta(self, remaining):
        """Seconds until `remaining` songs are done (None = not enough data yet)."""
        got = self.eta_range(remaining)
        return None if got is None else got[0]

    def snapshot(self):
        return {"speed": self.speed_now(), "peak": self.peak, "bytes": self.bytes_total, "avg": self.avg_speed(),
                "searches_per_min": self.searches_per_min(), "latency_ms": self.latency_ms(),
                "requests": self.requests, "errors": self.errors, "songs": self.songs_done}


T = Telemetry()
