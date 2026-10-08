"""
stats.py — live numbers for the dashboard: download speed, searches per minute, API latency, ETA.

One process-wide Telemetry object collects from every thread (cheap, lock-protected) and a small sampler
thread turns the running byte counter into a speed history twice a second. The UI only reads snapshots.
"""
import threading
import time
from collections import deque

SAMPLE_EVERY = 0.5            # seconds between speed samples
HISTORY = 120                 # samples kept (one minute)


class Telemetry:
    def __init__(self):
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.reset()

    def reset(self):
        with self._lock:
            self.t_start = time.monotonic()
            self.bytes_total = 0
            self._last_bytes, self._last_t = 0, time.monotonic()
            self.speed = deque(maxlen=HISTORY)                     # bytes/second, smoothed
            self.peak = 0.0
            self.searches = deque(maxlen=2000)                       # monotonic timestamps
            self.search_total = 0
            self.latencies = deque(maxlen=240)                       # (timestamp, ms)
            self.latency_by_host = {}
            self.completions = deque(maxlen=48)                      # monotonic times songs finished
            self.songs_done = 0
            self.requests = 0
            self.errors = 0

    # ---- recording (called from worker threads)

    def add_bytes(self, n):
        with self._lock:
            self.bytes_total += n

    def search(self):
        with self._lock:
            self.searches.append(time.monotonic())
            self.search_total += 1

    def latency(self, host, ms):
        with self._lock:
            self.requests += 1
            self.latencies.append((time.monotonic(), float(ms)))
            prev = self.latency_by_host.get(host)
            self.latency_by_host[host] = ms if prev is None else prev * 0.7 + ms * 0.3

    def error(self):
        with self._lock:
            self.errors += 1

    def song_done(self):
        with self._lock:
            self.songs_done += 1
            self.completions.append(time.monotonic())

    # ---- sampling

    def sample(self):
        now = time.monotonic()
        with self._lock:
            dt = now - self._last_t
            if dt <= 0:
                return
            inst = (self.bytes_total - self._last_bytes) / dt
            prev = self.speed[-1] if self.speed else inst
            smooth = prev * 0.45 + inst * 0.55
            self.speed.append(smooth)
            self.peak = max(self.peak, smooth)
            self._last_bytes, self._last_t = self.bytes_total, now

    def start(self):
        """Begin sampling in the background (idempotent)."""
        if self._thread and self._thread.is_alive():
            return
        self.reset()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="telemetry", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(SAMPLE_EVERY):
            self.sample()

    def stop(self):
        self._stop.set()

    # ---- readings (called from the UI thread)

    def speed_history(self):
        with self._lock:
            return list(self.speed)

    def speed_now(self):
        with self._lock:
            return self.speed[-1] if self.speed else 0.0

    def searches_per_min(self):
        now = time.monotonic()
        with self._lock:
            recent = sum(1 for t in self.searches if now - t <= 60)
            span = min(60.0, max(5.0, now - self.t_start))
        return recent * 60.0 / span

    def latency_ms(self):
        """Mean API round-trip over the last ~20 calls (None until something has been measured)."""
        with self._lock:
            vals = [ms for _, ms in list(self.latencies)[-20:]]
        return sum(vals) / len(vals) if vals else None

    def latency_history(self, n=60):
        with self._lock:
            return [ms for _, ms in list(self.latencies)[-n:]]

    def eta(self, remaining):
        """Seconds until `remaining` songs are done, from how fast songs have been finishing (None = not enough data)."""
        if remaining <= 0:
            return 0.0
        now = time.monotonic()
        with self._lock:
            c = list(self.completions)
            elapsed = now - self.t_start
            n = self.songs_done
        if n < 3 or len(c) < 3:
            return None
        recent = (len(c) - 1) / max(0.5, c[-1] - c[0])
        overall = n / max(1.0, elapsed)
        rate = (recent + overall) / 2
        return remaining / rate if rate > 0 else None

    def snapshot(self):
        return {"speed": self.speed_now(), "peak": self.peak, "bytes": self.bytes_total,
                "searches_per_min": self.searches_per_min(), "latency_ms": self.latency_ms(),
                "requests": self.requests, "errors": self.errors, "songs": self.songs_done}


T = Telemetry()
