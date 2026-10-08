"""Optimized mode: the source cap (never a lossless file bigger than its source deserves), the sampler that checks what
a source really is, and the Apple / Windows·Android choice (AAC·ALAC versus MP3·FLAC)."""
import os
import threading
import time
import unittest

from helpers import FFMPEG, FileServer, fresh_dir, make_audio, make_lossy_origin, make_noise, noise_cover

from musicdl.audio import sampler
from musicdl.config import (APPLE_PRESETS, DEVICE_ORDER, DEVICES, FORMATS, PRESET_ORDER, PRESETS, OutFmt, Settings,
                            preset)
from musicdl.core import catalog, engine, netio, quality, sources
from musicdl.core.models import Track
from musicdl.meta.tags import read_info

MP3_320, MP3_192, FLAC = OutFmt("mp3", 320), OutFmt("mp3", 192), OutFmt("flac", 0, bit_depth=0)
AAC_256, AAC_160, ALAC = OutFmt("aac", 256), OutFmt("aac", 160), OutFmt("alac", 0, bit_depth=0)


class FitTests(unittest.TestCase):
    """quality.fit: the format a song is really written in, given what its source can fill."""

    def test_a_lossless_source_keeps_whatever_was_chosen(self):
        for fmt in (FLAC, ALAC, MP3_320, AAC_160):
            self.assertIs(quality.fit(fmt, True, 1411), fmt)

    def test_lossless_choice_with_a_lossy_source_becomes_lossy_at_the_source_quality(self):
        self.assertEqual(quality.fit(FLAC, False, 128), OutFmt("mp3", 128, bit_depth=16))
        self.assertEqual(quality.fit(FLAC, False, 240), OutFmt("mp3", 256, bit_depth=16))      # a 160 kbps Opus stream
        self.assertEqual(quality.fit(ALAC, False, 153), OutFmt("aac", 128, bit_depth=16))      # AAC 128 ≈ MP3 153
        self.assertEqual(quality.fit(ALAC, False, 240), OutFmt("aac", 224, bit_depth=16))

    def test_a_stand_in_is_never_written_above_its_ceiling(self):
        self.assertEqual(quality.fit(FLAC, False, 900).kbps, 320)
        self.assertEqual(quality.fit(ALAC, False, 900).kbps, 256)

    def test_lossy_choice_is_lowered_to_what_the_source_has_never_raised(self):
        self.assertEqual(quality.fit(MP3_320, False, 240).kbps, 256)                           # 320 would be padding
        self.assertEqual(quality.fit(MP3_320, False, 128).kbps, 128)
        self.assertIs(quality.fit(MP3_192, False, 240), MP3_192, "the source has more than 192: nothing to change")
        self.assertIs(quality.fit(MP3_320, False, 320), MP3_320)
        self.assertEqual(quality.fit(AAC_256, False, 153).kbps, 128)
        self.assertIs(quality.fit(AAC_160, False, 400), AAC_160)

    def test_unknown_source_quality(self):
        self.assertIs(quality.fit(MP3_320, False, 0), MP3_320, "a lossy choice with nothing known stays as chosen")
        self.assertEqual(quality.fit(FLAC, False, 0), OutFmt("mp3", 192, bit_depth=16), "but a lossless one does not")

    def test_the_result_is_always_a_selectable_bitrate(self):
        for q in range(1, 1500, 7):
            for fmt in (FLAC, ALAC, MP3_320, MP3_192, AAC_256, AAC_160):
                out = quality.fit(fmt, False, q)
                self.assertIn(out.kbps, FORMATS[out.key]["bitrates"])
                self.assertFalse(out.lossless)


class DeviceTests(unittest.TestCase):
    def test_each_step_has_an_apple_and_a_universal_form(self):
        self.assertEqual([(preset(k, "other")["fmt"], preset(k, "other")["kbps"]) for k in PRESET_ORDER],
                         [("mp3", 192), ("mp3", 320), ("flac", 0)])
        self.assertEqual([(preset(k, "apple")["fmt"], preset(k, "apple")["kbps"]) for k in PRESET_ORDER],
                         [("aac", 160), ("aac", 256), ("alac", 0)])
        for k in PRESET_ORDER:                                                  # the wording and the picture are shared
            self.assertEqual(preset(k, "apple")["label"], preset(k, "other")["label"])
            self.assertEqual(preset(k, "apple")["quality"], PRESETS[k]["quality"])

    def test_apple_steps_sound_like_the_universal_ones(self):
        """AAC 160 ≈ MP3 192 and AAC 256 ≈ MP3 320 on the app's one quality scale."""
        for k in ("good", "better"):
            a = quality.target(OutFmt("aac", preset(k, "apple")["kbps"]))
            m = quality.target(OutFmt("mp3", preset(k, "other")["kbps"]))
            self.assertLess(abs(a - m) / m, 0.08, k)
        for p in APPLE_PRESETS.values():
            self.assertTrue(p["fmt"] in ("alac",) or p["kbps"] in FORMATS["aac"]["bitrates"])

    def test_settings_follow_the_device(self):
        for device, fmt in (("apple", "alac"), ("other", "flac")):
            s = Settings(preset="best", device=device)
            self.assertEqual(s.out_format().key, fmt)
            self.assertTrue(s.out_format().lossless)
        self.assertEqual(Settings(preset="good", device="apple").out_format().label, "AAC 160 kbps")
        self.assertEqual(Settings(preset="better", device="other").out_format().label, "MP3 320 kbps")
        self.assertEqual(Settings(preset="best", device="apple").out_format().ext, ".m4a")

    def test_device_choice_is_kept_and_checked(self):
        s = Settings(device="apple")
        self.assertEqual(s.clamp().device, "apple")
        s.device = "toaster"
        self.assertIn(s.clamp().device, DEVICES)
        self.assertEqual(sorted(DEVICE_ORDER), sorted(DEVICES))

    def test_the_cap_belongs_to_optimized_mode(self):
        self.assertTrue(Settings(mode="easy").capped)
        self.assertFalse(Settings(mode="advanced").capped)

    def test_the_mac_edition_defaults_to_apple_formats(self):
        from musicdl import config
        old = config.platform_.OS_NAME
        try:
            config.platform_.OS_NAME = "macos"
            self.assertEqual(config.default_device(), "apple")
            config.platform_.OS_NAME = "windows"
            self.assertEqual(config.default_device(), "other")
        finally:
            config.platform_.OS_NAME = old


# ---------------------------------------------------------------- the sampler

@unittest.skipUnless(FFMPEG, "needs ffmpeg")
class SamplerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = fresh_dir("sampler")
        cls.real = os.path.join(cls.d, "real.flac")
        make_noise(cls.real, 30, "-c:a", "flac")
        cls.mp3_128 = os.path.join(cls.d, "from128.flac")
        make_lossy_origin(cls.mp3_128, 30, 128)
        cls.mp3_192 = os.path.join(cls.d, "from192.wav")
        make_lossy_origin(cls.mp3_192, 30, 192)
        cls.aac_128 = os.path.join(cls.d, "fromaac128.flac")
        make_lossy_origin(cls.aac_128, 30, 128, encoder="aac")

    def test_a_real_lossless_recording_is_believed(self):
        s = sampler.sample(self.real)
        self.assertTrue(s.lossless and s.checked, s)

    def test_flac_made_from_a_128_kbps_mp3_is_caught(self):
        s = sampler.sample(self.mp3_128)
        self.assertFalse(s.lossless, s)
        self.assertEqual(s.q, 128)
        self.assertTrue(16000 <= s.cutoff <= 17500, s)

    def test_a_192_kbps_origin_is_graded_higher_and_wav_is_read_too(self):
        s = sampler.sample(self.mp3_192)
        self.assertFalse(s.lossless, s)
        self.assertIn(s.q, (160, 192, 224))

    def test_aac_origin_is_caught_as_well(self):
        s = sampler.sample(self.aac_128)
        self.assertFalse(s.lossless, s)
        self.assertLessEqual(s.q, 192)

    def test_a_natural_roll_off_is_not_mistaken_for_a_lossy_edge(self):
        for name, flt in (("gentle", "lowpass=f=16000:poles=2"), ("analog", ",".join(["lowpass=f=16000:poles=2"] * 4)),
                          ("dull", "lowpass=f=9000:poles=2")):
            path = os.path.join(self.d, f"nat_{name}.wav")
            make_noise(path, 20, "-af", flt, "-c:a", "pcm_s16le")
            s = sampler.sample(path)
            self.assertTrue(s.lossless, f"{name}: {s}")

    def test_when_it_cannot_tell_it_believes_the_file(self):
        self.assertTrue(sampler.sample(os.path.join(self.d, "nothing.flac")).lossless)      # no such file
        silent = os.path.join(self.d, "silence.flac")
        make_audio(silent, 20, "-af", "volume=0", "-c:a", "flac")
        self.assertTrue(sampler.sample(silent).lossless)
        tiny = os.path.join(self.d, "tiny.flac")
        make_noise(tiny, 0.5, "-c:a", "flac")
        self.assertTrue(sampler.sample(tiny).lossless)

    def test_a_pure_tone_has_no_edge(self):
        tone = os.path.join(self.d, "tone.flac")
        make_audio(tone, 20, "-c:a", "flac")
        self.assertTrue(sampler.sample(tone).lossless)

    def test_stop_is_honoured(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(engine.Stopped):
            sampler.sample(self.real, stop)

    def test_it_is_quick(self):
        t = time.perf_counter()
        sampler.sample(self.mp3_128)
        self.assertLess(time.perf_counter() - t, 4.0)


class EdgeMathTests(unittest.TestCase):
    """The spectrum maths on made-up band levels (no audio)."""

    @staticmethod
    def levels(edge, floor=-120.0, slope=-0.5):
        """dB per 250 Hz slice from 14 kHz: sound that fades gently, then (if `edge`) nothing above `edge`."""
        return [(-60.0 + slope * i) if (edge is None or sampler.FIRST_HZ + i * sampler.BAND_HZ < edge) else floor
                for i in range(len(range(sampler.FIRST_HZ, sampler.LAST_HZ, sampler.BAND_HZ)))]

    def test_a_hard_edge_is_found_at_its_upper_side(self):
        for edge in (16000, 17000, 18500, 19000):
            found = sampler.find_edge(self.levels(edge), -20.0)
            self.assertTrue(edge <= found <= edge + sampler.BAND_HZ, (edge, found))

    def test_a_gentle_slope_has_no_edge(self):
        self.assertEqual(sampler.find_edge(self.levels(None, slope=-1.5), -20.0), 0)
        self.assertEqual(sampler.find_edge(self.levels(None, slope=-4.0), -20.0), 0)

    def test_a_drop_with_sound_returning_above_it_is_not_an_edge(self):
        lv = self.levels(17000)
        lv[-3] = -62.0                                                    # something loud again near the top
        self.assertEqual(sampler.find_edge(lv, -20.0), 0)

    def test_edges_too_high_to_tell_from_lossless_are_ignored(self):
        self.assertEqual(sampler.find_edge(self.levels(20000), -20.0), 0)

    def test_nothing_to_compare_with(self):
        self.assertEqual(sampler.find_edge(self.levels(17000, slope=0, floor=-130), 40.0), 0)

    def test_cutoff_to_bitrate(self):
        self.assertEqual([sampler.q_from_cutoff(h) for h in (16000, 17250, 17500, 18500, 19500, 19750, 21000)],
                         [128, 128, 160, 192, 224, 0, 0])

    def test_silence_and_short_input(self):
        self.assertEqual(sampler.analyse([0.0] * 50000), 0)
        self.assertEqual(sampler.analyse([0.1] * 500), 0)

    def test_fft_finds_a_tone(self):
        import math
        n = sampler.N
        frames = [[0.5 * math.sin(2 * math.pi * 10000 * (i + k * n) / sampler.RATE) for i in range(n)] for k in range(4)]
        power = sampler.spectrum(frames)
        peak = max(range(len(power)), key=power.__getitem__)
        self.assertLessEqual(abs(peak * sampler.RATE / n - 10000), sampler.RATE / n)


# ---------------------------------------------------------------- the whole job

WWW = fresh_dir("www_cap")
SRV = None
PLAN = {}
_ORIGINAL = {}


def cand(f, secs, kbps, lossless=False):
    return {"source": "archive.org", "id": f"cap:{f}", "url": SRV.url + f, "ext": os.path.splitext(f)[1], "seconds": secs,
            "kbps": 1411 if lossless else kbps, "lossless": lossless, "score": 1, "title": f}


def setUpModule():
    global SRV
    _ORIGINAL.update(archive=sources.archive_candidates, youtube=sources.youtube_candidates, lookup=catalog.lookup,
                     limits=[(l, l.interval) for l in (netio.DEEZER_LIMIT, netio.WEB_LIMIT, netio.CAA_LIMIT)])
    make_noise(os.path.join(WWW, "real.flac"), 40, "-c:a", "flac")                # a genuinely lossless recording
    make_lossy_origin(os.path.join(WWW, "from128.flac"), 40, 128)                 # a 'FLAC' made from a 128 kbps MP3
    make_audio(os.path.join(WWW, "plain128.mp3"), 40, "-b:a", "128k")
    make_audio(os.path.join(WWW, "plain320.mp3"), 40, "-b:a", "320k")
    make_noise(os.path.join(WWW, "noise320.mp3"), 40, "-c:a", "libmp3lame", "-b:a", "320k")   # a sine encodes far below
    # the bitrate asked of AAC; noise fills it, so the written bitrate can be checked
    make_audio(os.path.join(WWW, "ogg190.ogg"), 40, "-c:a", "libvorbis", "-q:a", "6")
    noise_cover(os.path.join(WWW, "cover.png"))
    SRV = FileServer(WWW)
    netio.DEEZER_LIMIT.interval = netio.WEB_LIMIT.interval = netio.CAA_LIMIT.interval = 0.01
    sources.archive_candidates = lambda rc, stop, fits, deep: [c for c in PLAN.get(rc.track.title, []) if fits(rc, c["seconds"], deep)]
    sources.youtube_candidates = lambda *a, **k: []
    catalog.lookup = lambda lib, track, stop, want_genre=False: {
        "durations": [], "covers": [SRV.url + "cover.png"], "album": "Cap Album", "year": "2002", "track_no": 1,
        "disc_no": 1, "isrc": "", "genre": "", "t": time.time()}


def tearDownModule():
    SRV.close()
    sources.archive_candidates, sources.youtube_candidates = _ORIGINAL["archive"], _ORIGINAL["youtube"]
    catalog.lookup = _ORIGINAL["lookup"]
    for lim, interval in _ORIGINAL["limits"]:
        lim.interval = interval


def settings(**kw):
    st = Settings()
    st.auto, st.parallel, st.min_kbps, st.verify, st.device = False, 2, 128, True, "other"
    for k, v in kw.items():
        setattr(st, k, v)
    return st


class CapJobTests(unittest.TestCase):
    def setUp(self):
        PLAN.clear()
        self.out = fresh_dir("out_cap")

    def files(self):
        return sorted(f for f in os.listdir(self.out) if not f.startswith("."))

    def result(self, st, source, title="Alpha"):
        PLAN[title] = [source]
        ev = []
        job = engine.Job([Track(title, "Tester", duration=40.0)], self.out, st, emit=ev.append)
        job.run()
        self.job = job
        return [e["result"] for e in ev if e["type"] == "result"][0]

    # ---- Best

    def test_best_keeps_a_genuinely_lossless_source_lossless(self):
        r = self.result(settings(preset="best"), cand("real.flac", 40, 1411, True))
        self.assertEqual((r.status, r.src_kbps, r.note), ("ok", 1411, ""))
        self.assertEqual(self.files(), ["Alpha - Tester.flac"])
        self.assertTrue(read_info(os.path.join(self.out, "Alpha - Tester.flac")).lossless)

    def test_best_never_pads_a_lossy_source_into_flac(self):
        r = self.result(settings(preset="best"), cand("plain128.mp3", 40, 128))
        self.assertEqual(self.files(), ["Alpha - Tester.mp3"], "no .flac: it would only be a bigger copy")
        info = read_info(os.path.join(self.out, "Alpha - Tester.mp3"))
        self.assertAlmostEqual(info.kbps, 128, delta=12)
        self.assertFalse(info.lossless)
        self.assertEqual(r.src_kbps, 128)
        self.assertIn("No lossless source", r.note)
        self.assertIn("MP3 128", r.note)
        self.assertIn("1 song had no lossless version", self.job.summary())
        self.assertIn("MP3 instead of FLAC", self.job.summary())

    def test_best_catches_a_flac_that_was_made_from_a_128_kbps_mp3(self):
        r = self.result(settings(preset="best"), cand("from128.flac", 40, 1411, True))
        self.assertEqual(self.files(), ["Alpha - Tester.mp3"])
        info = read_info(os.path.join(self.out, "Alpha - Tester.mp3"))
        self.assertLessEqual(info.kbps, 140)
        self.assertEqual((r.src_kbps, info.src_kbps), (128, 128), "the file's record says what the source really was")
        self.assertIn("No lossless source", r.note)

    def test_the_same_file_in_advanced_mode_is_written_as_asked(self):
        r = self.result(settings(mode="advanced", fmt="flac"), cand("plain128.mp3", 40, 128))
        self.assertEqual(self.files(), ["Alpha - Tester.flac"], "Advanced does what it is told")
        self.assertEqual(r.status, "ok")
        self.assertEqual(self.job.summary(), "")

    # ---- Better and Good

    def test_better_is_lowered_to_what_the_source_has(self):
        self.result(settings(preset="better"), cand("plain128.mp3", 40, 128))
        info = read_info(os.path.join(self.out, "Alpha - Tester.mp3"))
        self.assertLessEqual(info.kbps, 140, "a 320 kbps copy of a 128 kbps source would be padding")

    def test_better_keeps_its_bitrate_when_the_source_has_enough(self):
        self.result(settings(preset="better"), cand("plain320.mp3", 40, 320))
        self.assertGreaterEqual(read_info(os.path.join(self.out, "Alpha - Tester.mp3")).kbps, 300)

    def test_good_is_not_raised_by_a_better_source(self):
        self.result(settings(preset="good"), cand("plain320.mp3", 40, 320))
        self.assertAlmostEqual(read_info(os.path.join(self.out, "Alpha - Tester.mp3")).kbps, 192, delta=15)

    def test_other_codecs_are_graded_by_their_mp3_equivalent(self):
        self.result(settings(preset="better"), cand("ogg190.ogg", 40, 190))
        k = read_info(os.path.join(self.out, "Alpha - Tester.mp3")).kbps
        self.assertTrue(180 <= k <= 200, k)                                   # not 320

    # ---- Apple

    def test_apple_best_writes_alac_for_a_lossless_source(self):
        r = self.result(settings(preset="best", device="apple"), cand("real.flac", 40, 1411, True))
        self.assertEqual(self.files(), ["Alpha - Tester.m4a"])
        info = read_info(os.path.join(self.out, "Alpha - Tester.m4a"))
        self.assertTrue(info.lossless and info.cover, "ALAC with artwork")
        self.assertEqual(r.src_kbps, 1411)

    def test_apple_best_falls_back_to_aac_not_alac_for_a_lossy_source(self):
        r = self.result(settings(preset="best", device="apple"), cand("plain128.mp3", 40, 128))
        self.assertEqual(self.files(), ["Alpha - Tester.m4a"])
        info = read_info(os.path.join(self.out, "Alpha - Tester.m4a"))
        self.assertFalse(info.lossless)
        self.assertLessEqual(info.kbps, 150)
        self.assertIn("No lossless source", r.note)
        self.assertIn("AAC instead of ALAC", self.job.summary())

    def test_apple_better_and_good_write_aac(self):
        self.result(settings(preset="better", device="apple"), cand("noise320.mp3", 40, 320))
        info = read_info(os.path.join(self.out, "Alpha - Tester.m4a"))
        self.assertFalse(info.lossless)
        self.assertTrue(200 <= info.kbps <= 270, info.kbps)                    # AAC 256
        self.out = fresh_dir("out_cap_good")
        self.result(settings(preset="good", device="apple"), cand("noise320.mp3", 40, 320))
        info = read_info(os.path.join(self.out, "Alpha - Tester.m4a"))
        self.assertTrue(130 <= info.kbps <= 175, info.kbps)                    # AAC 160

    def test_a_flac_from_an_mp3_is_caught_for_apple_too(self):
        r = self.result(settings(preset="best", device="apple"), cand("from128.flac", 40, 1411, True))
        info = read_info(os.path.join(self.out, "Alpha - Tester.m4a"))
        self.assertFalse(info.lossless)
        self.assertEqual(r.src_kbps, 128)

    # ---- afterwards

    def test_a_rerun_recognises_the_lossy_copy_and_does_not_ask_or_download_again(self):
        self.result(settings(preset="best"), cand("plain128.mp3", 40, 128))
        asked, began = [], []
        job = engine.Job([Track("Alpha", "Tester", duration=40.0)], self.out, settings(preset="best"),
                         emit=lambda ev: began.append(ev) if ev["type"] == "begin" else None,
                         ask=lambda s: asked.append(s) or True)
        job.run()
        self.assertEqual((began, asked), ([], []), "the source had nothing better, so nothing is offered")
        self.assertEqual(self.files(), ["Alpha - Tester.mp3"])

    def test_a_song_whose_source_is_lossless_is_still_offered_when_the_old_copy_is_lossy_without_a_record(self):
        """An old low-quality file with no source information may be improved: that question is unchanged."""
        PLAN["Alpha"] = [cand("real.flac", 40, 1411, True)]
        job = engine.Job([Track("Alpha", "Tester", duration=40.0)], self.out,
                         settings(mode="advanced", fmt="mp3", bitrate=128), emit=lambda ev: None)
        job.run()
        asked = []
        ev = []
        engine.Job([Track("Alpha", "Tester", duration=40.0)], self.out, settings(preset="best"), emit=ev.append,
                   ask=lambda s: asked.append(s) or True).run()
        self.assertEqual(len(asked), 1)
        self.assertEqual(self.files(), ["Alpha - Tester.flac"])


if __name__ == "__main__":
    unittest.main()
