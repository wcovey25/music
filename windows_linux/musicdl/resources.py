"""
resources.py — what this computer can do, and how hard to push it while a long run is going.

    detect() / plan()   the hardware scan: cores, memory, and the measured connection -> how many songs to work on at
                        once and how many may encode at once.
    Budget              the places those numbers are enforced: a gate for songs in flight and a gate for CPU-heavy steps
                        (encoding, listening to a source). The first few encodes run at normal priority; the rest run at
                        below-normal priority, so the window and the rest of the computer stay responsive.
    Governor            while a long run is going, once every couple of seconds: how busy are the CPUs, is the computer
                        on battery or in battery saver, is the connection full or are sites pushing back? Then more or
                        fewer songs at once, normal or gentle priority. Short runs aren't worth it and don't get one.

Nothing here talks to the operating system directly except through platform_. (Windows gives an ordinary program no
temperature reading, so platform_.thermal_state() is always 'nominal' there and the heat rules below never fire; they
stay because they are the same code as the macOS edition's and are tested with simulated readings.)
"""
import functools
import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass

from . import platform_

log = logging.getLogger("musicdl.resources")

MIN_GOVERNED = 10                # songs: a shorter run finishes before there is anything to learn
MAX_SONGS = 12                   # most songs in flight the automatic setting will ever use
MBPS_PER_STREAM = 10             # connection each song in flight is assumed to need
MAX_THREADS_PER_ENCODE = 4       # ffmpeg stops getting faster long before this

TICK = 2.0                       # seconds between the governor's looks at the machine
WINDOW = 20.0                    # shortest stretch a pace change is judged over (by the bytes that moved in it)
COOL_TICKS = 5                   # readings in a row below the held temperature before it steps down (hysteresis)
PLATEAU = 240.0                  # after a change didn't help, wait this long before trying again
GAIN = 1.05                      # one more song at once must raise throughput by this much to be kept
CPU_ROOM = 0.82                  # above this share of the CPUs busy there is no point adding work
CPU_FULL = 0.96                  # sustained at this level the machine is saturated: back off
NET_FULL = 0.85                  # share of the measured connection that counts as full
CRITICAL_PAUSE = 12.0            # seconds a critically hot machine gets with no new song started


# ---------------------------------------------------------------- the hardware scan

@dataclass(frozen=True)
class Hardware:
    chip: str
    logical: int
    tiers: tuple                 # ((name, cores), ...) fastest first
    ram_gb: float = None

    @property
    def cores(self):
        return sum(n for _, n in self.tiers)

    @property
    def efficiency(self):
        """Cores that sip power and are slower (E-cores); 0 on a machine with one kind (every Windows PC, as read here)."""
        return self.tiers[-1][1] if len(self.tiers) > 1 else 0

    @property
    def fast(self):
        return self.cores - self.efficiency

    @classmethod
    def from_info(cls, info):
        logical = max(1, int(info.get("logical") or info.get("cores") or 1))
        tiers = tuple((str(name), max(1, int(n))) for name, n in (info.get("tiers") or ())) or (("Cores", logical),)
        return cls(str(info.get("chip") or "CPU"), logical, tiers, info.get("ram_gb"))

    def info(self):
        """The record autotune keeps in the settings file."""
        return {"cores": self.cores, "logical": self.logical, "chip": self.chip, "ram_gb": self.ram_gb,
                "tiers": [list(t) for t in self.tiers]}

    def describe(self):
        """'Intel Core i7-1360P · 12 cores · 16 GB'."""
        cores = f"{self.cores} cores" + (f" ({'+'.join(str(n) for _, n in self.tiers)})" if len(self.tiers) > 1 else "")
        return " · ".join(p for p in (self.chip if self.chip != "CPU" else "", cores,
                                      f"{self.ram_gb:g} GB" if self.ram_gb else "") if p)


@functools.lru_cache(maxsize=1)
def detect():
    """Scan this computer (once per process: the answer doesn't change while the program runs)."""
    try:
        return Hardware.from_info(platform_.hardware())
    except Exception:
        log.exception("hardware scan failed")
        import os
        return Hardware("CPU", os.cpu_count() or 1, (("Cores", max(1, (os.cpu_count() or 2) // 2)),), None)


@dataclass(frozen=True)
class Plan:
    start: int                   # songs in flight when a run begins
    ceiling: int                 # most the governor may ever allow
    encodes: int                 # CPU-heavy steps at once
    fast_slots: int              # of those, how many run at normal priority (the fast cores); the rest run gently
    cores: int = 1


def plan(hw, mbps=None):
    """The hardware scan (and the measured connection, if known) -> how to share the work out.

    Songs at once is bounded by what the CPUs can encode (fast cores, plus half the efficiency cores: they are slower
    and the machine still has a screen to draw) and by what the connection can feed (about 10 Mbps a song); a run
    starts at 60% of that and the governor grows it while it pays."""
    by_cpu = max(2, hw.fast + hw.efficiency // 2)
    if hw.ram_gb is not None and hw.ram_gb < 4:
        by_cpu = min(by_cpu, 2)
    elif hw.ram_gb is not None and hw.ram_gb < 8:
        by_cpu = min(by_cpu, 4)
    by_net = 3 if mbps is None else max(1, int(mbps // MBPS_PER_STREAM))
    ceiling = max(1, min(by_cpu, by_net, MAX_SONGS))
    start = ceiling if ceiling <= 3 else max(2, round(ceiling * 0.6))
    return Plan(start=start, ceiling=ceiling, encodes=by_cpu, fast_slots=max(1, hw.fast), cores=hw.cores)


# ---------------------------------------------------------------- priority of the work a thread starts

_local = threading.local()


def level():
    """The priority (0 normal, 1 gentle, 2 minimal) the calling thread's work runs at (see Budget.cpu)."""
    return getattr(_local, "level", 0)


def launch(cmd):
    """(command, extra creationflags) for starting a heavy child process (ffmpeg) at the calling thread's priority."""
    prefix, flags = platform_.compute_priority(level())
    return list(prefix) + list(cmd), flags


# ---------------------------------------------------------------- gates

class Gate:
    """A counting semaphore whose size can change while it is in use (a smaller size never interrupts work that already
    holds a place), whose places are numbered (the lowest free one is handed out first), and that can be held shut for a
    while. acquire() returns the place number, or None once `stop` is set."""

    def __init__(self, limit, clock=time.monotonic):
        self._cond = threading.Condition()
        self._clock = clock
        self.limit = max(1, int(limit))
        self._busy = set()
        self._hold = 0.0

    @property
    def used(self):
        return len(self._busy)

    def acquire(self, stop=None):
        with self._cond:
            while True:
                if stop is not None and stop.is_set():
                    return None
                now = self._clock()
                if len(self._busy) < self.limit and now >= self._hold:
                    place = next(i for i in range(len(self._busy) + 1) if i not in self._busy)
                    self._busy.add(place)
                    return place
                self._cond.wait(0.25 if now >= self._hold else max(0.01, min(0.25, self._hold - now)))

    def release(self, place):
        with self._cond:
            self._busy.discard(place)
            self._cond.notify()

    def resize(self, limit):
        with self._cond:
            self.limit = max(1, int(limit))
            self._cond.notify_all()

    def pause(self, seconds):
        """No new place is handed out for `seconds` (places already held carry on)."""
        with self._cond:
            self._hold = max(self._hold, self._clock() + seconds)

    def resume(self):
        with self._cond:
            self._hold = 0.0
            self._cond.notify_all()

    def held(self):
        return self._clock() < self._hold


class Budget:
    """The shared limits of one run: songs in flight, CPU-heavy steps at once, and how gently both are to be done."""

    def __init__(self, plan_, songs=None, cores=None, clock=time.monotonic):
        self.plan = plan_
        self.songs = Gate(songs or plan_.start, clock)
        self.encodes = Gate(plan_.encodes, clock)
        self.level = 0                       # 0 normal, 1 gentle, 2 minimal: set by the governor
        self.scouts_on = threading.Event()   # cleared while the machine is too hot for look-ahead work
        self.scouts_on.set()
        self.cores = cores or plan_.cores

    @classmethod
    def unlimited(cls, n=64):
        """No limits worth speaking of: what a Job uses when nothing has set it up (and what tests get)."""
        return cls(Plan(start=n, ceiling=n, encodes=n, fast_slots=n, cores=n))

    def threads(self):
        """ffmpeg threads for one encode: the cores divided over the songs in flight, at most four."""
        return max(1, min(MAX_THREADS_PER_ENCODE, self.cores // max(1, self.songs.limit)))

    @contextmanager
    def cpu(self, stop=None):
        """Hold a place for one CPU-heavy step. Yields the priority level it runs at (the calling thread and the child
        processes it starts through launch() both take it), or None when `stop` was set while waiting."""
        place = self.encodes.acquire(stop)
        if place is None:
            yield None
            return
        lvl = max(self.level, 1 if place >= self.plan.fast_slots else 0)
        _local.level = lvl
        if lvl:
            platform_.thread_priority(lvl)
        try:
            yield lvl
        finally:
            _local.level = 0
            if lvl:
                platform_.thread_priority(0)
            self.encodes.release(place)


# ---------------------------------------------------------------- what the machine says

class Sensors:
    """One reading of everything the governor looks at. The numbers come from platform_ (temperature, CPU load, power),
    the telemetry (bytes, errors, finished songs) and netio (hosts pushing back)."""

    def __init__(self):
        self._ticks = None

    def cpu(self):
        """Share of all CPUs that were busy since the last call (None the first time, or when the OS can't say)."""
        now = platform_.cpu_ticks()
        prev, self._ticks = self._ticks, now
        if not now or not prev:
            return None
        busy, total = now[0] - prev[0], now[1] - prev[1]
        busy, total = busy + (2 ** 32 if busy < 0 else 0), total + (2 ** 32 if total < 0 else 0)     # 32-bit counters wrap
        return min(1.0, max(0.0, busy / total)) if total > 0 else None

    def read(self):
        from .core import netio
        from .telemetry.stats import T
        snap = T.snapshot()
        push = netio.pressure()
        power = platform_.power_source()
        return {"thermal": platform_.thermal_state() or 0, "battery": power["battery"], "low_power": power["low_power"],
                "cpu": self.cpu(), "speed": snap["speed"], "peak": snap["peak"], "requests": snap["requests"], "errors": snap["errors"],
                "songs": snap["songs"], "bytes": snap["bytes"], "open": push["open"], "slowed": push["slowed"]}


# ---------------------------------------------------------------- the governor

class Governor:
    """Keeps a long run fast without cooking the machine. Call tick() every couple of seconds (start() does that on a
    thread).

    Songs at once (`target`) is hill-climbed: when every place is busy, the CPUs have room, the connection isn't full and
    nothing is failing, one more is tried; it stays only if throughput rose by GAIN, otherwise it goes back and the
    climb rests for PLATEAU seconds. Throughput is the bytes that arrived over the window, not the songs that finished
    in it: bytes are a continuous count, where a window of twenty seconds holds only a handful of finished songs. Errors, an open circuit breaker or a host slowing us down take one away; so does a
    saturated CPU. On top of that, the machine's own condition limits what is allowed right now: 'serious' heat halves
    it and puts the work on the efficiency cores, 'critical' heat stops starting songs for a moment, battery and Low
    Power Mode hold it down. When that passes the target comes back by itself. A manual number from the user is a
    ceiling: it is never exceeded, only reduced for those reasons."""

    def __init__(self, budget, cap, start, manual=False, link_mbps=None, backlog=None, sensors=None,
                 clock=time.monotonic, emit=None):
        self.budget, self.cap, self.manual = budget, max(1, int(cap)), manual
        self.target = max(1, min(int(start), self.cap))
        self.capacity = link_mbps * 125000.0 if link_mbps else None          # bytes per second the link can carry
        self.backlog = backlog or (lambda: 1 << 30)                          # songs not started yet
        self.sensors = sensors or Sensors()
        self.clock = clock
        self.emit = emit or (lambda ev: None)
        self.hot, self._cool = 0, 0                                          # held thermal state and its cooldown count
        self.allowed = self.target                                           # songs at once right now
        self.state = "normal"
        self._probe = None                                                   # (target before the last increase, its rate, unit)
        self._rest_until = 0.0
        self._cpu_hits = 0
        self.reason = ""                                                     # why the last step was down: 'sites' | 'cpu' | ''
        self._w = None                                                       # the window being measured (set by tick)
        self._paused = False
        self._last = None                                                    # what was last announced
        self._stop = threading.Event()
        self._thread = None
        self._eased_since, self.eased_s = None, 0.0                          # time spent limited by heat or power
        self.peak = self.allowed
        self.thermal_peak = 0
        self.apply()

    # ---------------------------------------------------------------- the pieces of one decision

    def _reset_window(self, r):
        self._w = {"t": self.clock(), "songs": r["songs"], "bytes": r.get("bytes", 0), "errors": r["errors"],
                   "requests": r["requests"], "cpu": [], "speed": [], "busy": [], "push": 0, "disturbed": False}

    def _heat(self, thermal):
        """Held temperature: up at once, down one step after COOL_TICKS cooler readings in a row."""
        if thermal >= self.hot:
            self.hot, self._cool = thermal, 0
        else:
            self._cool += 1
            if self._cool >= COOL_TICKS:
                self.hot, self._cool = self.hot - 1, 0
        self.thermal_peak = max(self.thermal_peak, self.hot)

    def apply(self, r=None):
        """Turn the held state into the gates' sizes and the priority level."""
        plan_, b = self.budget.plan, self.budget
        power_cap = self.cap
        battery, low = (r["battery"], r["low_power"]) if r else (False, False)
        if low:
            power_cap = min(power_cap, 2)
        elif battery:
            power_cap = min(power_cap, max(2, plan_.fast_slots // 2 + 1))
        allowed = min(self.target, power_cap)
        if self.hot == 2:
            allowed = max(1, allowed // 2)
        elif self.hot >= 3:
            allowed = 1
        encodes = min(plan_.encodes, allowed)
        if self.hot >= 2:
            encodes = max(1, min(encodes, plan_.fast_slots // 2))
        lvl = 2 if self.hot >= 3 else 1 if (self.hot == 2 or low or battery) else 0
        self.allowed = allowed
        b.songs.resize(allowed)
        b.encodes.resize(encodes)
        b.level = lvl
        if self.hot >= 3:
            b.songs.pause(CRITICAL_PAUSE)
            b.scouts_on.clear()
            self._paused = True
        elif self._paused:
            b.songs.resume()
            b.scouts_on.set()
            self._paused = False
        self.state = ("critical" if self.hot >= 3 else "hot" if self.hot == 2 else "low_power" if low
                      else "battery" if battery else "warm" if self.hot == 1 else "normal")
        self.peak = max(self.peak, allowed)

    def easing(self):
        """Is the run being held below what it would otherwise do (heat, power, or a step back after pushback)?"""
        return self.state != "normal" or bool(self.reason)

    def _announce(self):
        key = (self.allowed, self.state, self.reason)
        if key == self._last:
            return
        self._last = key
        n = f"{self.allowed} at once"
        text = {"critical": "Your computer is too hot — pausing for a moment",
                "hot": f"Easing off so your computer can cool down — {n}",
                "low_power": f"Easing off for battery saver — {n}",
                "battery": f"Easing off while on battery — {n}"}.get(self.state)
        if text is None:
            text = {"sites": f"Easing off — a site asked for fewer requests — {n}",
                    "cpu": f"Easing off — the processor is fully busy — {n}"}.get(
                        self.reason, f"{self.allowed} song{'s' if self.allowed != 1 else ''} at once")
        self.emit({"type": "pace", "songs": self.allowed, "state": self.state, "text": text, "easing": self.easing()})

    def _judge(self, r, now):
        """Look back over the finished window and decide whether to climb, hold or retreat."""
        w = self._w
        dt = max(0.001, now - w["t"])
        moved = r.get("bytes", 0) - w["bytes"]
        rate, unit = (moved / dt, "bytes") if moved > 0 else ((r["songs"] - w["songs"]) / dt, "songs")
        errors, requests = r["errors"] - w["errors"], r["requests"] - w["requests"]
        cpu = sum(w["cpu"]) / len(w["cpu"]) if w["cpu"] else None
        speed = sum(w["speed"]) / len(w["speed"]) if w["speed"] else 0.0
        busy = sum(w["busy"]) / len(w["busy"]) if w["busy"] else 0.0
        failing = errors >= max(3, 0.25 * max(1, requests)) or w["push"] > 0
        self._reset_window(r)
        if w["disturbed"]:                                                   # heat or power was limiting: nothing to learn
            self._probe = None
            return
        probe, self._probe = self._probe, None
        if failing:                                                          # back off and give the host room
            if self.target > 1:
                self.target -= 1
                self.reason = "sites"
                log.info("pace: %d at once (errors %d/%d, hosts pushing back %d)", self.target, errors, requests, w["push"])
            self._rest_until = now + PLATEAU * 1.25
            return
        if cpu is not None and cpu >= CPU_FULL:
            self._cpu_hits += 1
            if self._cpu_hits >= 2 and self.target > 1:
                self.target -= 1
                self.reason = "cpu"
                self._cpu_hits = 0
                self._rest_until = now + PLATEAU
                log.info("pace: %d at once (CPUs saturated)", self.target)
                return
        else:
            self._cpu_hits = 0
        if probe is not None:
            if probe[2] != unit or rate >= probe[1] * GAIN:
                self.reason = ""
                log.info("pace: %d at once kept (%.3g -> %.3g %s/s)", self.target, probe[1], rate, unit)
            else:
                self.target = probe[0]
                self._rest_until = now + PLATEAU
                log.info("pace: %d at once is as far as it helps (%.3g -> %.3g %s/s)", self.target, probe[1], rate, unit)
                return
        if self.target >= self.cap:
            return
        if self.manual:                                                      # the user's number is where it belongs: recover
            self.target += 1
            if self.target >= self.cap:
                self.reason = ""
            return
        full = self.capacity and r.get("peak", 0) <= self.capacity * 1.1 and speed >= NET_FULL * self.capacity
        room = (cpu is None or cpu < CPU_ROOM) and not full
        if busy >= 0.9 * self.target and room and now >= self._rest_until and self.backlog() > self.target + 2:
            self._probe = (self.target, rate, unit)
            self.target += 1

    # ---------------------------------------------------------------- one look

    def tick(self):
        r = self.sensors.read()
        now = self.clock()
        if self._w is None:
            self._reset_window(r)
        self._heat(r["thermal"])
        self.apply(r)
        w = self._w
        if r["cpu"] is not None:
            w["cpu"].append(r["cpu"])
        w["speed"].append(r["speed"])
        w["busy"].append(self.budget.songs.used)
        w["push"] = max(w["push"], r["open"] + r["slowed"])
        if self.allowed < self.target or self.hot or r["thermal"]:
            w["disturbed"] = True
        limited = self.allowed < self.target or self.state != "normal"
        if limited and self._eased_since is None:
            self._eased_since = now
        elif not limited and self._eased_since is not None:
            self.eased_s += now - self._eased_since
            self._eased_since = None
        dt = now - w["t"]
        done = r["songs"] - w["songs"]
        if (dt >= WINDOW and done >= 2) or dt >= WINDOW * 3:
            if done > 0 or w["disturbed"]:
                self._judge(r, now)
            else:
                self._reset_window(r)
            self.apply(r)
        self._announce()

    # ---------------------------------------------------------------- thread

    def start(self):
        """Take the first reading now (so a machine that is already hot is known before the first song starts, and the
        CPU load has something to be compared with), then keep watching on a thread."""
        self.tick()
        self._thread = threading.Thread(target=self._loop, name="governor", daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception:
                log.exception("governor tick failed")

    def close(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
        if self._eased_since is not None:
            self.eased_s += self.clock() - self._eased_since
            self._eased_since = None
        self.budget.songs.resume()
        self.budget.scouts_on.set()

    def stats(self):
        """What the run looked like, for the summary: {'peak', 'eased_s', 'thermal'}."""
        return {"peak": self.peak, "eased_s": round(self.eased_s), "thermal": self.thermal_peak}
