"""The hardware scan, the plan, the gates, the governor (with simulated readings), the machine monitor, the telemetry
numbers, and the Windows readings in platform_ (simulated where this machine is not Windows)."""
import os
import struct
import sys
import threading
import time
import unittest
from unittest import mock

import helpers  # noqa: F401  (sets MUSICDL_HOME)

from musicdl import autotune, platform_, resources
from musicdl.config import Settings
from musicdl.core import engine
from musicdl.core.models import Result, Track
from musicdl.telemetry import monitor as monitor_mod
from musicdl.telemetry.stats import Telemetry

WINDOWS = sys.platform == "win32"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def hw(cores=8, ram=16.0, logical=None, tiers=None):
    return resources.Hardware.from_info({"chip": "Test CPU", "logical": logical or cores * 2,
                                         "tiers": tiers or [("Cores", cores)], "ram_gb": ram})


# ---------------------------------------------------------------- the scan and the plan

class PlanTests(unittest.TestCase):
    def test_describe_and_info_round_trip(self):
        h = hw(8, 16.0)
        self.assertEqual(h.describe(), "Test CPU · 8 cores · 16 GB")
        self.assertEqual(resources.Hardware.from_info(h.info()), h)
        self.assertEqual(hw(12, tiers=[("P", 4), ("E", 8)]).describe(), "Test CPU · 12 cores (4+8) · 16 GB")

    def test_plan_is_bounded_by_cpu_memory_and_connection(self):
        self.assertEqual(resources.plan(hw(8), 200).ceiling, 8)                 # CPU bound
        self.assertEqual(resources.plan(hw(8), 30).ceiling, 3)                  # ~10 Mbps a song
        self.assertEqual(resources.plan(hw(32), 10_000).ceiling, resources.MAX_SONGS)
        self.assertEqual(resources.plan(hw(8, ram=3.0), 200).ceiling, 2, "little memory: two at most")
        self.assertEqual(resources.plan(hw(8, ram=6.0), 200).ceiling, 4)
        self.assertEqual(resources.plan(hw(8), None).ceiling, 3, "connection not measured: careful")

    def test_a_run_starts_below_the_ceiling(self):
        p = resources.plan(hw(10), 500)
        self.assertEqual((p.ceiling, p.start), (10, 6))
        self.assertEqual(resources.plan(hw(2), 500).start, 2)

    def test_detect_never_fails(self):
        with mock.patch.object(platform_, "hardware", side_effect=RuntimeError("no")), \
                self.assertLogs("musicdl.resources", level="ERROR"):
            resources.detect.cache_clear()
            try:
                h = resources.detect()
            finally:
                resources.detect.cache_clear()
        self.assertGreaterEqual(h.cores, 1)

    def test_autotune_uses_the_plan_and_keeps_its_fallback(self):
        info = hw(8).info()
        self.assertEqual(autotune.recommend(info, {"mbps": 200, "latency_ms": 20})["parallel"],
                         resources.plan(hw(8), 200).start)
        with mock.patch.object(resources, "plan", side_effect=ValueError("broken")):
            self.assertEqual(autotune.recommend({"cores": 8, "ram_gb": 16}, {"mbps": 200, "latency_ms": 20})["parallel"], 4)
        self.assertIn("8 cores", autotune.describe(info))


# ---------------------------------------------------------------- gates and the budget

class GateTests(unittest.TestCase):
    def test_places_are_numbered_lowest_first(self):
        g = resources.Gate(3)
        a, b = g.acquire(), g.acquire()
        self.assertEqual((a, b), (0, 1))
        g.release(0)
        self.assertEqual(g.acquire(), 0)
        self.assertEqual(g.used, 2)

    def test_shrinking_never_interrupts_and_growing_lets_waiters_in(self):
        g = resources.Gate(2)
        g.acquire(), g.acquire()
        g.resize(1)
        self.assertEqual(g.used, 2, "work that holds a place carries on")
        got = []
        t = threading.Thread(target=lambda: got.append(g.acquire()))
        t.start()
        time.sleep(0.1)
        self.assertEqual(got, [])
        g.resize(3)
        t.join(2)
        self.assertEqual(got, [2])

    def test_stop_while_waiting(self):
        g = resources.Gate(1)
        g.acquire()
        stop = threading.Event()
        threading.Timer(0.2, stop.set).start()
        self.assertIsNone(g.acquire(stop))

    def test_pause_holds_new_places(self):
        clock = Clock()
        g = resources.Gate(4, clock=clock)
        g.pause(5)
        self.assertTrue(g.held())
        stop = threading.Event()
        threading.Timer(0.3, stop.set).start()
        self.assertIsNone(g.acquire(stop))
        clock.t += 6
        self.assertEqual(g.acquire(), 0)

    def test_extra_encodes_run_gently(self):
        pl = resources.Plan(start=4, ceiling=4, encodes=3, fast_slots=1, cores=8)
        b = resources.Budget(pl)
        levels, calls = [], []
        with mock.patch.object(platform_, "thread_priority", lambda lvl: calls.append(lvl) or True):
            with b.cpu() as first:
                levels.append((first, resources.level()))
                with b.cpu() as second:
                    levels.append((second, resources.level()))
        self.assertEqual(levels, [(0, 0), (1, 1)], "the first encode at normal priority, the next gently")
        self.assertEqual(calls, [1, 0], "and the thread is put back afterwards")
        self.assertEqual(resources.level(), 0)

    def test_ffmpeg_starts_at_the_threads_priority(self):
        with mock.patch.object(platform_, "compute_priority", lambda lvl: ([], 0x4000 if lvl else 0)):
            resources._local.level = 1
            try:
                cmd, flags = resources.launch(["ffmpeg", "-i", "x"])
            finally:
                resources._local.level = 0
        self.assertEqual((cmd, flags), (["ffmpeg", "-i", "x"], 0x4000))

    def test_threads_per_encode(self):
        b = resources.Budget(resources.Plan(start=2, ceiling=4, encodes=4, fast_slots=4, cores=8))
        self.assertEqual(b.threads(), 4)
        b.songs.resize(8)
        self.assertEqual(b.threads(), 1)


# ---------------------------------------------------------------- the governor

class FakeSensors:
    def __init__(self):
        self.r = {"thermal": 0, "battery": False, "low_power": False, "cpu": 0.4, "speed": 0.0, "peak": 0.0,
                  "requests": 0, "errors": 0, "songs": 0, "bytes": 0, "open": 0, "slowed": 0}

    def read(self):
        return dict(self.r)


class GovernorTests(unittest.TestCase):
    def make(self, start=2, cap=8, manual=False, plan=None):
        self.clock, self.sensors, self.events = Clock(), FakeSensors(), []
        pl = plan or resources.Plan(start=start, ceiling=cap, encodes=8, fast_slots=8, cores=16)
        self.budget = resources.Budget(pl, songs=start, clock=self.clock)
        self.held = []
        gov = resources.Governor(self.budget, cap=cap, start=start, manual=manual, sensors=self.sensors,
                                 clock=self.clock, emit=self.events.append)
        gov.tick()
        return gov

    def busy(self):
        """Every place is taken (the run has more songs than places)."""
        while self.budget.songs.used < self.budget.songs.limit:
            self.held.append(self.budget.songs.acquire())

    def window(self, gov, rate, seconds=22, **reading):
        """Simulate `seconds` of a run moving `rate` bytes/second, a song finishing every tick."""
        for _ in range(int(seconds / resources.TICK)):
            self.busy()
            self.clock.t += resources.TICK
            r = self.sensors.r
            r["bytes"] += int(rate * resources.TICK)
            r["songs"] += 1
            r["requests"] += 4
            r["speed"] = r["peak"] = rate
            r.update(reading)
            gov.tick()

    def test_it_climbs_while_more_songs_at_once_pay(self):
        gov = self.make(start=2)
        self.window(gov, 1_000_000)
        self.assertEqual(gov.target, 3, "everything busy, room to spare: one more is tried")
        self.window(gov, 1_300_000)
        self.assertGreaterEqual(gov.target, 4, "it paid, so it is kept and the next one tried")

    def test_a_step_that_did_not_pay_is_taken_back_and_rests_four_minutes(self):
        gov = self.make(start=2)
        self.window(gov, 1_000_000)
        self.assertEqual(gov.target, 3)
        self.window(gov, 1_010_000)
        self.assertEqual(gov.target, 2, "throughput did not really rise: back to 2")
        self.window(gov, 1_000_000, seconds=200)
        self.assertEqual(gov.target, 2, "resting")
        tried = []
        for _ in range(30):
            self.window(gov, 1_000_000, seconds=2)
            tried.append(gov.target)
        self.assertIn(3, tried, "after the four-minute rest it tries again")

    def test_errors_and_pushback_take_one_away_and_say_so(self):
        gov = self.make(start=4)
        self.window(gov, 1_000_000, slowed=1)
        self.assertEqual(gov.target, 3)
        self.assertTrue(gov.easing())
        self.assertIn("Easing off", self.events[-1]["text"])
        self.assertTrue(self.events[-1]["easing"])

    def test_a_saturated_processor_takes_one_away(self):
        gov = self.make(start=4)
        for _ in range(3):                                     # two judgements in a row at a saturated CPU
            self.window(gov, 1_000_000, cpu=0.99)
        self.assertLess(gov.target, 4)
        self.assertIn("processor", self.events[-1]["text"])

    def test_on_battery_fewer_and_battery_saver_two_at_most(self):
        gov = self.make(start=6, cap=8, plan=resources.Plan(start=6, ceiling=8, encodes=8, fast_slots=6, cores=12))
        self.sensors.r["battery"] = True
        gov.tick()
        self.assertEqual(gov.allowed, 4)
        self.assertEqual(self.budget.songs.limit, 4)
        self.assertEqual(self.budget.level, 1, "encodes run gently on battery")
        self.assertEqual(self.events[-1]["text"], "Easing off while on battery — 4 at once")
        self.sensors.r["low_power"] = True
        gov.tick()
        self.assertEqual(gov.allowed, 2)
        self.assertIn("battery saver", self.events[-1]["text"])
        self.sensors.r.update(battery=False, low_power=False)
        gov.tick()
        self.assertEqual(gov.allowed, 6, "back to full pace when the charger is back")
        self.assertFalse(self.events[-1]["easing"])

    def test_a_manual_number_is_a_ceiling(self):
        gov = self.make(start=3, cap=3, manual=True)
        for _ in range(5):
            self.window(gov, 1_000_000 * (1 + _))
        self.assertEqual(gov.target, 3)
        self.assertLessEqual(self.budget.songs.limit, 3)

    def test_simulated_heat_halves_then_recovers(self):
        gov = self.make(start=6)
        self.sensors.r["thermal"] = 2
        gov.tick()
        self.assertEqual(gov.allowed, 3)
        self.sensors.r["thermal"] = 0
        for _ in range(resources.COOL_TICKS * 2 + 1):
            gov.tick()
        self.assertEqual(gov.allowed, 6)

    def test_windows_reports_no_heat(self):
        self.assertEqual(platform_.thermal_state(), 0)

    def test_close_releases_any_hold(self):
        gov = self.make(start=2)
        self.budget.songs.pause(100)
        gov.close()
        self.assertFalse(self.budget.songs.held())
        self.assertEqual(gov.stats()["thermal"], 0)


class EnginePoolTests(unittest.TestCase):
    """Short runs use the plan; ten songs or more get a governor; a manual number is never exceeded."""

    def run_pool(self, n, **kw):
        st = Settings(**kw)
        tracks = [Track(f"S{i}", "A") for i in range(n)]
        job = engine.Job(tracks, helpers.fresh_dir("pool"), st)
        peak, active, lock = [0], [0], threading.Lock()

        def work(rc, plan):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return Result("ok", rc.track.title, rc.track.artist)
        todo = [(engine.Ctx(index=i, track=t, title=t.title, artist=t.artist), engine.Plan("new")) for i, t in enumerate(tracks)]
        with mock.patch.object(job, "_work", work), mock.patch.object(job, "_scout", lambda rc, p: None), \
                mock.patch.object(job, "_finish", lambda rc, res: None):
            job._run_pool(todo)
        return job, peak[0]

    def test_a_short_run_has_no_governor(self):
        job, peak = self.run_pool(4, auto=False, parallel=3)
        self.assertIsNone(job.governor)
        self.assertLessEqual(peak, 3)

    def test_a_long_run_is_governed_and_the_manual_number_is_a_ceiling(self):
        job, peak = self.run_pool(12, auto=False, parallel=2)
        self.assertIsNotNone(job.governor)
        self.assertLessEqual(peak, 2)
        self.assertLessEqual(job.governor.peak, 2)


# ---------------------------------------------------------------- the monitor and the telemetry numbers

class MonitorTests(unittest.TestCase):
    def test_load_between_two_readings(self):
        self.assertIsNone(monitor_mod.load_between(None, (1, 2)))
        self.assertEqual(monitor_mod.load_between((10, 100), (60, 200)), 0.5)
        self.assertIsNone(monitor_mod.load_between((10, 100), (5, 100)))

    def test_sample_with_simulated_readings(self):
        ticks = iter([(0, 0), (300, 1000)])
        cores = iter([[(0, 0), (0, 0)], [(100, 500), (450, 500)]])
        m = monitor_mod.Monitor()
        with mock.patch.multiple(platform_, cpu_ticks=lambda: next(ticks), cpu_cores=lambda: next(cores),
                                 thermal_state=lambda: 0, process_rss_mb=lambda: 123.0, core_tiers=lambda: None,
                                 power_source=lambda: {"battery": True, "low_power": False}):
            m.sample(0.0)
            m.sample(1.0)
        snap = m.snapshot()
        self.assertAlmostEqual(snap["cpu_now"], 0.3)
        self.assertEqual([round(c, 2) for c in snap["cores"]], [0.2, 0.9])
        self.assertEqual((snap["rss"], snap["heat"], snap["battery"]), (123.0, 0, True))
        self.assertEqual(m.shape(), [("Cores", 2)])

    def test_sensors_cpu_handles_wrapping_counters(self):
        s = resources.Sensors()
        with mock.patch.object(platform_, "cpu_ticks", side_effect=[(2 ** 32 - 10, 2 ** 32 - 10), (10, 30)]):
            self.assertIsNone(s.cpu())
            self.assertAlmostEqual(s.cpu(), 0.5)


class TelemetryTests(unittest.TestCase):
    def test_speed_is_measured_over_real_time_and_the_average_over_busy_time(self):
        clock = Clock(0.0)
        t = Telemetry(clock=clock)
        t.reset()
        for _ in range(10):
            clock.t += 0.5
            t.add_bytes(500_000)
            t.sample()
        for _ in range(10):                                   # ten idle samples: not part of the average
            clock.t += 0.5
            t.sample()
        self.assertAlmostEqual(t.avg_speed(), 1_000_000, delta=1)
        self.assertGreater(t.peak, 900_000)
        self.assertLess(t.speed_now(), 50_000)

    def test_time_left_is_a_range_that_narrows_with_a_steady_pace(self):
        clock = Clock(0.0)
        t = Telemetry(clock=clock)
        t.reset()
        self.assertIsNone(t.eta_range(10))
        for _ in range(12):
            clock.t += 10.0
            t.song_done()
        exp, lo, hi = t.eta_range(10)
        self.assertAlmostEqual(exp, 100.0, delta=1)
        self.assertLess(lo, exp)
        self.assertGreater(hi, exp)
        self.assertLess(hi - lo, 0.6 * exp, "a steady pace gives a narrow range")
        self.assertEqual(t.eta_range(0), (0.0, 0.0, 0.0))

    def test_the_clock_stops_with_the_run(self):
        clock = Clock(0.0)
        t = Telemetry(clock=clock)
        t.reset()
        clock.t = 30.0
        t.stop()
        clock.t = 90.0
        self.assertEqual(t.elapsed(), 30.0)


# ---------------------------------------------------------------- Windows readings

class WindowsReadingTests(unittest.TestCase):
    def test_counting_core_records(self):
        rec = lambda rel, size: struct.pack("<II", rel, size) + b"\0" * (size - 8)      # noqa: E731
        raw = rec(0, 48) + rec(2, 76) + rec(0, 48) + rec(1, 32) + rec(0, 48)
        self.assertEqual(platform_.count_core_records(raw), 3)
        self.assertEqual(platform_.count_core_records(b"\0" * 4), 0)
        self.assertEqual(platform_.count_core_records(struct.pack("<II", 0, 0) + rec(0, 48)), 0, "a broken size ends the walk")

    def test_reading_the_power_status(self):
        self.assertEqual(platform_.read_power_status(0, 1, 0), {"battery": True, "low_power": False})
        self.assertEqual(platform_.read_power_status(1, 8, 0), {"battery": False, "low_power": False})
        self.assertEqual(platform_.read_power_status(0, 2, 1), {"battery": True, "low_power": True})
        self.assertEqual(platform_.read_power_status(255, 128, 0)["battery"], False, "a desktop with no battery")

    def test_priorities(self):
        with mock.patch.object(platform_, "IS_WINDOWS", True):
            self.assertEqual(platform_.compute_priority(0), ([], 0))
            self.assertEqual(platform_.compute_priority(1), ([], platform_.BELOW_NORMAL_PRIORITY_CLASS))
            self.assertEqual(platform_.compute_priority(2), ([], platform_.IDLE_PRIORITY_CLASS))

    def test_hardware_shape(self):
        info = platform_.hardware()
        self.assertGreaterEqual(info["logical"], 1)
        self.assertEqual(len(info["tiers"]), 1)
        self.assertLessEqual(info["tiers"][0][1], info["logical"])

    @unittest.skipUnless(WINDOWS, "Windows only")
    def test_real_readings_on_windows(self):
        a = platform_.cpu_ticks()
        time.sleep(0.2)
        b = platform_.cpu_ticks()
        self.assertTrue(a and b and b[1] >= a[1] and 0 <= b[0] - a[0] <= b[1] - a[1])
        cores = platform_.cpu_cores()
        self.assertTrue(cores and len(cores) == min(64, os.cpu_count()))
        self.assertIn(platform_.power_source()["battery"], (True, False))
        self.assertTrue(platform_.physical_cores() and platform_.physical_cores() <= os.cpu_count())
        self.assertTrue(platform_.thread_priority(1))
        self.assertTrue(platform_.thread_priority(0))
        self.assertIsInstance(platform_.reduce_motion(), bool)

    @unittest.skipIf(WINDOWS, "elsewhere these readings are simply absent")
    def test_other_systems_answer_cant_say(self):
        self.assertIsNone(platform_.cpu_ticks())
        self.assertIsNone(platform_.cpu_cores())
        self.assertFalse(platform_.thread_priority(1))
        self.assertEqual(platform_.power_source(), {"battery": False, "low_power": False})


if __name__ == "__main__":
    unittest.main()
