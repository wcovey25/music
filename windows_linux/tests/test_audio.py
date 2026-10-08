"""The four app sounds: format, level, cleanliness, musical content; and the player that plays them."""
import math
import os
import threading
import time
import unittest
import wave
from array import array
from unittest import mock

from helpers import fresh_dir

from musicdl.audio import sounds, synth
from musicdl.config import Settings


def load(path):
    with wave.open(path, "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (2, 2, 44100)
        pcm = array("h")
        pcm.frombytes(w.readframes(w.getnframes()))
    left, right = pcm[0::2], pcm[1::2]
    return [v / 32768 for v in left], [v / 32768 for v in right]


def goertzel(x, freq, sr=44100):
    """Energy of one frequency in a signal."""
    w = 2 * math.pi * freq / sr
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s1, s2 = v + c * s1 - s2, s1
    return (s1 * s1 + s2 * s2 - c * s1 * s2) / len(x)


def rms(x):
    return math.sqrt(sum(v * v for v in x) / max(1, len(x)))


class SynthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = fresh_dir("sounds")
        cls.paths = synth.build(cls.dir)
        cls.audio = {n: load(p) for n, p in cls.paths.items()}

    def test_levels_are_gentle_and_never_clip(self):
        limits = {"startup": (0.35, 0.60), "click": (0.15, 0.35), "nav": (0.2, 0.4), "complete": (0.35, 0.60)}
        for name, (left, right) in self.audio.items():
            peak = max(max(map(abs, left)), max(map(abs, right)))
            lo, hi = limits[name]
            self.assertTrue(lo <= peak <= hi, f"{name} peak {peak:.2f}")

    def test_durations(self):
        want = {"startup": (2.0, 4.0), "click": (0.03, 0.12), "nav": (0.15, 0.7), "complete": (2.0, 4.5)}
        for name, (left, _r) in self.audio.items():
            secs = len(left) / 44100
            self.assertTrue(want[name][0] <= secs <= want[name][1], f"{name} {secs:.2f}s")

    def test_no_dc_offset_and_clean_ends(self):
        for name, (left, right) in self.audio.items():
            for ch in (left, right):
                self.assertLess(abs(sum(ch) / len(ch)), 0.002, name)
                self.assertLess(abs(ch[0]), 0.01, f"{name} starts with a pop")
                self.assertLess(abs(ch[-1]), 0.002, f"{name} ends with a pop")

    def test_stereo_has_width_but_is_balanced(self):
        for name in ("startup", "complete"):
            left, right = self.audio[name]
            self.assertNotEqual(left[:5000], right[:5000])
            ratio = rms(left) / rms(right)
            self.assertTrue(0.7 < ratio < 1.4, f"{name} balance {ratio:.2f}")

    def test_startup_contains_its_notes(self):
        mono = [(a + b) / 2 for a, b in zip(*self.audio["startup"])][:int(1.2 * 44100)]
        d5, a5 = goertzel(mono, 587.33), goertzel(mono, 880.0)
        off_note = goertzel(mono, 700.0)
        self.assertGreater(d5, off_note * 20)
        self.assertGreater(a5, off_note * 20)

    def test_complete_rises_then_settles_on_a_chord(self):
        left, right = self.audio["complete"]
        mono = [(a + b) / 2 for a, b in zip(left, right)]
        early = mono[:int(0.14 * 44100)]
        self.assertGreater(goertzel(early, 783.99), goertzel(early, 1318.51) * 20)       # first note is G5
        settled = mono[int(1.0 * 44100):int(1.8 * 44100)]
        for f in (523.25, 659.25):                                                          # C5 + E5 of the chord ring on
            self.assertGreater(goertzel(settled, f), goertzel(settled, 600.0) * 5)

    def test_click_is_short_and_bright_not_boomy(self):
        mono = self.audio["click"][0]
        self.assertGreater(goertzel(mono, 2300), goertzel(mono, 100) * 20)
        # nearly all of the energy is in the first 25 ms
        head = sum(v * v for v in mono[:int(0.025 * 44100)])
        self.assertGreater(head / sum(v * v for v in mono), 0.9)

    def test_deterministic(self):
        again = synth.make_click()
        self.assertEqual(again.tobytes(), synth.make_click().tobytes())

    def test_shipped_assets_match_the_generator(self):
        for name in synth.NAMES:
            shipped = os.path.join(synth.ASSETS, name + ".wav")
            self.assertTrue(os.path.exists(shipped), f"{name}.wav is missing from audio/assets")
            fresh = load(self.paths[name])
            have = load(shipped)
            self.assertEqual(len(fresh[0]), len(have[0]), name)


class PlayerTests(unittest.TestCase):
    def setUp(self):
        self.played = []
        self.lock = threading.Lock()

        def fake_play(path):
            with self.lock:
                self.played.append(os.path.basename(path))

        p1 = mock.patch("musicdl.platform_.play_wav", fake_play)
        p2 = mock.patch("musicdl.platform_.play_wav_if_idle", fake_play)
        p1.start(), p2.start()
        self.addCleanup(p1.stop), self.addCleanup(p2.stop)
        self.settings = Settings(sounds=True, volume=60)
        self.player = sounds.Sounds(lambda: self.settings)
        self.addCleanup(self.player.close)

    def wait(self, n, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end and len(self.played) < n:
            time.sleep(0.01)

    def test_plays_each_named_sound(self):
        for name in ("startup", "nav", "complete"):
            self.player.play(name)
            self.wait(len(self.played) + 1)
        self.assertEqual(len(self.played), 3)
        self.assertTrue(all(p.endswith(".wav") for p in self.played))
        self.assertTrue(self.played[0].startswith("startup"))

    def test_off_switch(self):
        self.settings.sounds = False
        self.player.play("startup")
        time.sleep(0.2)
        self.assertEqual(self.played, [])

    def test_volume_makes_a_quieter_copy(self):
        self.settings.volume = 30
        self.player.play("complete")
        self.wait(1)
        self.assertEqual(len(self.played), 1)
        quiet = os.path.join(sounds.cache_dir_for_sounds(), self.played[0])
        full = load(os.path.join(synth.ASSETS, "complete.wav"))[0]
        soft = load(quiet)[0]
        ratio = max(map(abs, soft)) / max(map(abs, full))
        self.assertAlmostEqual(ratio, (30 / 100) ** 2, delta=0.02)

    def test_full_volume_uses_the_original_file(self):
        self.settings.volume = 100
        self.player.play("nav")
        self.wait(1)
        self.assertEqual(self.played, ["nav.wav"])

    def test_rapid_clicks_are_thinned_not_queued_forever(self):
        for _ in range(200):
            self.player.play("click")
        time.sleep(0.5)
        self.assertLess(len(self.played), 15)
        self.assertGreaterEqual(len(self.played), 1)

    def test_unknown_name_is_ignored(self):
        self.player.play("kaboom")
        time.sleep(0.1)
        self.assertEqual(self.played, [])

    def test_missing_assets_are_regenerated(self):
        with mock.patch.object(sounds, "ASSETS", fresh_dir("noassets")):
            p = sounds.Sounds(lambda: self.settings)
            self.addCleanup(p.close)
            p.play("click")
            self.wait(1)
        self.assertEqual(len(self.played), 1)


if __name__ == "__main__":
    unittest.main()
