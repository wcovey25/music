"""
monitor.py — what the computer itself is doing while a run goes: CPU load (overall and core by core), heat, memory and
power.

The telemetry thread calls sample() once a second. Every reading comes from platform_ and costs microseconds; the
battery state is only asked every POWER_EVERY seconds. Windows has no heat reading (always 'nominal'). Only readings are taken here;
nothing is ever changed (the Governor in resources.py is what acts on them).
"""
import os
import threading
import time
from collections import deque

from .. import platform_

HISTORY = 120                    # readings kept (two minutes at one a second)
POWER_EVERY = 20.0               # seconds between asking whether the computer is on battery


def load_between(prev, now):
    """Share of the time that was busy between two (busy, total) readings, 0..1; None when there is nothing to compare
    (no earlier reading, counters that wrapped, no time passed)."""
    if not prev or not now:
        return None
    busy, total = now[0] - prev[0], now[1] - prev[1]
    if busy < 0 or total <= 0:
        return None
    return min(1.0, busy / total)


class Monitor:
    def __init__(self):
        self._lock = threading.Lock()
        self._tiers = None                                   # [(name, [cpu numbers])] once looked up (False: none)
        self._warming = False
        self.reset()

    def reset(self):
        with self._lock:
            self.cpu = deque(maxlen=HISTORY)                 # share of all CPUs that were busy, 0..1
            self.heat = deque(maxlen=HISTORY)                # thermal state: 0 nominal, 1 fair, 2 serious, 3 critical
            self.rss = deque(maxlen=HISTORY)                 # this program's memory, MB
            self.cores = []                                  # the latest load of every logical CPU, 0..1
            self.battery = self.low_power = False
            self.peak_cpu, self.peak_heat, self.peak_rss = 0.0, 0, 0.0
            self._ticks, self._core_ticks, self._power_at = None, None, None

    def _kinds(self):
        """The core kinds (looked up once; Windows reads every core as one kind)."""
        if self._tiers is None:
            try:
                self._tiers = platform_.core_tiers() or False
            except Exception:
                self._tiers = False
        return self._tiers or None

    def warm(self):
        """Look the core kinds up in the background, so the dashboard can draw them before the first run."""
        if self._tiers is None and not self._warming:
            self._warming = True
            threading.Thread(target=self._kinds, name="core-kinds", daemon=True).start()

    def shape(self):
        """[(kind, number of cores)] as far as it is known: the kinds when they have been looked up, else all the cores as
        one group, else nothing."""
        kinds = self._tiers or None
        if kinds:
            return [(name, len(cpus)) for name, cpus in kinds]
        with self._lock:
            n = len(self.cores)
        n = n or (os.cpu_count() or 0)
        return [("Cores", n)] if n else []

    # ---- sampling (telemetry thread)

    def sample(self, now=None):
        now = time.monotonic() if now is None else now
        self._kinds()
        ticks, cores = platform_.cpu_ticks(), platform_.cpu_cores()
        heat, rss = platform_.thermal_state(), platform_.process_rss_mb()
        power = None
        if self._power_at is None or now - self._power_at >= POWER_EVERY:
            self._power_at = now
            power = platform_.power_source()
        with self._lock:
            load = load_between(self._ticks, ticks)
            self._ticks = ticks
            if load is not None:
                self.cpu.append(load)
                self.peak_cpu = max(self.peak_cpu, load)
            if cores:
                prev = self._core_ticks
                if prev and len(prev) == len(cores):
                    self.cores = [load_between(a, b) or 0.0 for a, b in zip(prev, cores)]
                self._core_ticks = cores
            if heat is not None:
                self.heat.append(heat)
                self.peak_heat = max(self.peak_heat, heat)
            if rss is not None:
                self.rss.append(rss)
                self.peak_rss = max(self.peak_rss, rss)
            if power:
                self.battery, self.low_power = bool(power["battery"]), bool(power["low_power"])

    # ---- readings (any thread)

    def snapshot(self):
        kinds = self._tiers or None                          # (looked up by sample(), never here: this runs on the UI thread)
        with self._lock:
            cores = list(self.cores)
            groups = None
            if kinds and cores and sum(len(c) for _n, c in kinds) == len(cores):
                groups = [(name, [cores[i] for i in cpus]) for name, cpus in kinds]
            return {"cpu": list(self.cpu), "cpu_now": self.cpu[-1] if self.cpu else None, "cores": cores,
                    "groups": groups, "heat": self.heat[-1] if self.heat else 0, "heat_history": list(self.heat),
                    "rss": self.rss[-1] if self.rss else None, "rss_history": list(self.rss),
                    "battery": self.battery, "low_power": self.low_power, "peak_cpu": self.peak_cpu,
                    "peak_heat": self.peak_heat, "peak_rss": self.peak_rss}


MONITOR = Monitor()
