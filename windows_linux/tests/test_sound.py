"""Finishing the sound (audio/process.py) and clean versions: what ffmpeg's listening pass is read as, where a song is
cut, how much it is turned up, what the settings ask for, a whole job with sound options on, and which upload is
taken (and how it is tagged) when the clean edit is wanted. Audio is made with ffmpeg; downloads come from a local
file server."""
import os
import subprocess
import time
import unittest

from helpers import FFMPEG, NO_WINDOW, FileServer, fresh_dir, make_audio

from musicdl.audio import process
from musicdl.config import Settings
from musicdl.core import catalog, engine, sources, text
from musicdl.core.models import Track
from musicdl.meta.tags import read_info


def lavfi(path, graph, *args):
    subprocess.run([FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", graph, *args, path], check=True,
                   creationflags=NO_WINDOW)


def padded_tone(path, lead, tone, tail, *args, volume_db=0):
    """`lead` s of silence, `tone` s of a 440 Hz tone, `tail` s of silence."""
    af = [f"volume={volume_db}dB"] if volume_db else []
    if lead:
        af.append(f"adelay={int(lead * 1000)}:all=1")
    if tail:
        af.append(f"apad=pad_dur={tail}")
    lavfi(path, f"sine=frequency=440:duration={tone}", *(["-af", ",".join(af)] if af else []), *args)


PROBE = """Input #0, wav, from 'x.wav':
  Duration: 00:01:05.00, bitrate: 1411 kb/s
[silencedetect @ 0x1] silence_start: 0
[silencedetect @ 0x1] silence_end: 3.012 | silence_duration: 3.012
[silencedetect @ 0x1] silence_start: 61.5
size=N/A time=00:01:05.00 bitrate=N/A speed= 900x
[Parsed_ebur128_1 @ 0x2] Summary:

  Integrated loudness:
    I:         -23.4 LUFS
    Threshold: -33.6 LUFS

  Sample peak:
    Peak:       -6.2 dBFS
"""


class ProbeTests(unittest.TestCase):
    def test_what_ffmpeg_printed_is_read(self):
        p = process.parse_probe(PROBE)
        self.assertEqual(p.seconds, 65.0)
        self.assertEqual(p.silences, [(0.0, 3.012), (61.5, None)])
        self.assertEqual((p.loudness, p.peak), (-23.4, -6.2))

    def test_digital_silence_has_no_loudness(self):
        p = process.parse_probe(PROBE.replace("-23.4 LUFS", "-70.0 LUFS"))
        self.assertIsNone(p.loudness, "-70 is the meter's floor: nothing was heard")


class PlanTests(unittest.TestCase):
    def probe(self, seconds=65.0, silences=((0.0, 3.0), (61.5, None)), loudness=-23.0):
        return process.Probe(seconds=seconds, silences=list(silences), loudness=loudness)

    def test_silence_is_cut_leaving_a_breath_and_the_last_ring(self):
        start, end = process.trim_bounds(self.probe(), process.Spec(trim=True))
        self.assertAlmostEqual(start, 3.0 - process.LEAD_KEEP)
        self.assertAlmostEqual(end, 61.5 + process.TAIL_KEEP)

    def test_a_fraction_of_a_second_is_not_worth_a_cut(self):
        self.assertIsNone(process.trim_bounds(self.probe(silences=[(0.0, 0.2)]), process.Spec(trim=True)))

    def test_never_more_than_half_a_song(self):
        self.assertIsNone(process.trim_bounds(self.probe(silences=[(0.0, 40.0)]), process.Spec(trim=True)))

    def test_turned_up_to_the_target_with_a_limiter(self):
        fx = process.plan(self.probe(silences=[]), process.Spec(level=True, target=-14))
        self.assertAlmostEqual(fx.gain_db, 9.0)
        self.assertIn("volume=9.00dB", fx.filters)
        self.assertTrue(any(f.startswith("alimiter") for f in fx.filters), "a boost is always limited")
        self.assertEqual(fx.note(), "Volume +9.0 dB")

    def test_a_boost_has_a_ceiling(self):
        fx = process.plan(self.probe(silences=[], loudness=-50.0), process.Spec(level=True, target=-14))
        self.assertEqual(fx.gain_db, process.MAX_BOOST_DB)

    def test_already_at_the_target_is_left_alone(self):
        fx = process.plan(self.probe(silences=[], loudness=-14.1), process.Spec(level=True, target=-14))
        self.assertFalse(fx.active)

    def test_a_trimmed_end_always_fades(self):
        fx = process.plan(self.probe(), process.Spec(trim=True))
        self.assertTrue(any(f.startswith("afade=t=out") for f in fx.filters), "a cut never clicks")
        self.assertAlmostEqual(fx.removed, 65.0 - (61.8 - 2.9), places=3)

    def test_dither_only_when_writing_16_bit(self):
        fx = process.plan(self.probe(silences=[]), process.Spec(level=True))
        self.assertNotIn("aresample", fx.chain())
        self.assertTrue(fx.chain(dither=True).endswith("aresample=osf=s16:dither_method=triangular_hp"))

    def test_eq_and_dynamics_are_named(self):
        fx = process.plan(self.probe(silences=[]), process.Spec(enhance="warmth", dynamics="gentle"))
        self.assertEqual(fx.note(), "Warmth + gentle dynamics")


class AnalyzeTests(unittest.TestCase):
    """The real listening pass, on files made for it."""

    def setUp(self):
        self.d = fresh_dir("sound_analyze")

    def test_silence_around_a_song_is_found(self):
        src = os.path.join(self.d, "padded.wav")
        padded_tone(src, 4, 20, 5)
        fx = process.analyze(src, process.Spec(trim=True))
        self.assertTrue(fx.active)
        self.assertAlmostEqual(fx.seconds, 29.0, delta=0.2)
        self.assertAlmostEqual(fx.removed, 9.0 - process.LEAD_KEEP - process.TAIL_KEEP, delta=0.3)
        self.assertIn("Trimmed", fx.note())

    def test_a_quiet_song_is_turned_up(self):
        src = os.path.join(self.d, "quiet.wav")
        padded_tone(src, 0, 10, 0, volume_db=-30)
        fx = process.analyze(src, process.Spec(level=True, target=-14))
        self.assertGreater(fx.gain_db, 5)

    def test_nothing_asked_nothing_done(self):
        self.assertIsNone(process.analyze("whatever.wav", process.Spec()))
        self.assertIsNone(process.analyze("whatever.wav", None))

    def test_an_unreadable_file_is_none_not_an_error(self):
        bad = os.path.join(self.d, "bad.mp3")
        with open(bad, "wb") as fh:
            fh.write(b"not audio" * 100)
        self.assertIsNone(process.analyze(bad, process.Spec(trim=True, level=True)))

    def test_this_ffmpeg_has_every_filter(self):
        self.assertEqual(process.missing_filters(), [])


class SettingsTests(unittest.TestCase):
    def test_nothing_on_means_no_spec(self):
        st = Settings()
        st.mode = "advanced"
        self.assertIsNone(st.audio_spec())
        st.mode = "easy"
        self.assertIsNone(st.audio_spec())

    def test_advanced_builds_it_from_the_options(self):
        st = Settings()
        st.mode, st.level, st.level_target, st.fade = "advanced", True, -11, "short"
        spec = st.audio_spec()
        self.assertEqual((spec.level, spec.target, spec.trim, spec.fade), (True, -11.0, False, "short"))

    def test_polish_is_the_playlist_profile_in_optimized(self):
        st = Settings()
        st.mode, st.polish = "easy", True
        spec = st.audio_spec()
        self.assertEqual((spec.level, spec.trim, spec.enhance), (True, True, "off"))
        st.mode = "advanced"
        self.assertIsNone(st.audio_spec(), "Advanced has its own options; polish is the Optimized switch")

    def test_values_are_kept_in_range(self):
        st = Settings()
        st.level_target, st.trim_db, st.fade, st.enhance, st.dynamics = 5, -200, "forever", "sparkle", "max"
        st.clamp()
        self.assertEqual((st.level_target, st.trim_db, st.fade, st.enhance, st.dynamics), (-8, -70, "off", "off", "off"))

    def test_profiles_round_trip(self):
        st = Settings()
        for key in process.PROFILES:
            process.apply_profile(st, key)
            self.assertEqual(process.profile_of(st), key)
        st.fade = "long"
        process.apply_profile(st, "car")
        st.enhance = "bass"
        self.assertEqual(process.profile_of(st), "", "changed by hand: custom")


# ---------------------------------------------------------------- clean versions

def mkrc(title, artist, secs=200.0, clean=False, explicit=0, cat=None):
    t = Track(title, artist, duration=secs, explicit=explicit)
    rc = engine.Ctx(index=0, track=t, title=text.core_title(title), artist=text.first_artist(artist))
    rc.refs = [float(secs)]
    rc.track_toks, rc.artist_toks = text.toks(rc.title), text.toks(rc.artist)
    rc.clean, rc.cat = clean, cat
    return rc


class CleanVersionTests(unittest.TestCase):
    def test_what_an_upload_says_about_itself(self):
        rc = mkrc("Harbour Lights", "Mara Quill")
        self.assertEqual(sources.version_of(rc, "Mara Quill - Harbour Lights (Clean)"), "clean")
        self.assertEqual(sources.version_of(rc, "Harbour Lights [Radio Edit]"), "clean")
        self.assertEqual(sources.version_of(rc, "Harbour Lights (Explicit)"), "explicit")
        self.assertEqual(sources.version_of(rc, "Mara Quill - Harbour Lights"), "")

    def test_a_word_in_the_songs_own_name_does_not_count(self):
        rc = mkrc("Clean", "Taylor Swift")
        self.assertEqual(sources.version_of(rc, "Taylor Swift - Clean"), "")
        self.assertEqual(sources.version_of(rc, "Taylor Swift - Clean (Explicit)"), "explicit")

    def test_the_asked_for_version_is_preferred(self):
        explicit, clean = mkrc("A", "B"), mkrc("A", "B", clean=True)
        self.assertGreater(sources.version_bias(clean, "A (Clean)"), 0)
        self.assertLess(sources.version_bias(clean, "A (Explicit)"), 0)
        self.assertLess(sources.version_bias(explicit, "A (Clean)"), -3, "a radio edit only when it is all there is")
        self.assertGreater(sources.version_bias(explicit, "A (Explicit)"), 0)

    def test_the_clean_search_comes_first_only_when_asked(self):
        self.assertEqual(sources.queries_for(mkrc("A", "B")), sources.YT_QUERIES)
        self.assertEqual(sources.queries_for(mkrc("A", "B", clean=True))[0], sources.CLEAN_QUERY)

    def test_a_convincing_explicit_upload_is_not_enough_when_clean_is_wanted(self):
        ranked = [(20.0, {"title": "Mara Quill - Harbour Lights", "duration": 200})]
        rc = mkrc("Harbour Lights", "Mara Quill", clean=True, explicit=1)
        self.assertFalse(sources._convincing(rc, ranked, False, asked=1))
        self.assertTrue(sources._convincing(rc, ranked, False, asked=2), "a second search already looked for it")
        ranked_clean = [(20.0, {"title": "Mara Quill - Harbour Lights (Clean)", "duration": 200})]
        self.assertTrue(sources._convincing(rc, ranked_clean, False, asked=1))

    def test_a_song_the_catalogue_calls_not_explicit_has_no_clean_edit_to_find(self):
        ranked = [(20.0, {"title": "Mara Quill - Harbour Lights", "duration": 200})]
        rc = mkrc("Harbour Lights", "Mara Quill", clean=True, cat={"explicit": 0})
        self.assertFalse(sources._may_have_clean(rc))
        self.assertTrue(sources._convincing(rc, ranked, False, asked=1))
        self.assertTrue(sources._may_have_clean(mkrc("A", "B", cat=None)), "not looked up yet: it may")
        self.assertTrue(sources._may_have_clean(mkrc("A", "B", cat={"failed": True})))

    def test_the_tag_follows_the_file_not_the_setting(self):
        job = engine.Job([], fresh_dir("clean_tag"), Settings(), emit=lambda e: None)
        rc = mkrc("Harbour Lights", "Mara Quill", clean=True, explicit=1)
        self.assertEqual(job._explicit(rc, {"title": "Harbour Lights (Clean)"}), 2)
        self.assertEqual(job._explicit(rc, {"title": "Harbour Lights (Explicit)"}), 1)
        self.assertEqual(job._explicit(rc, {"title": "Harbour Lights"}), 1, "explicit is never labelled clean")
        self.assertEqual(rc.raw.get("version_note"), "No clean version found")
        plain = mkrc("Harbour Lights", "Mara Quill", explicit=0)
        self.assertEqual(job._explicit(plain, {"title": "Harbour Lights"}), 0)
        self.assertNotIn("version_note", plain.raw)


# ---------------------------------------------------------------- a whole job with sound options

WWW = fresh_dir("sound_www")
SRV = None
SAVED = {}


def setUpModule():
    global SRV
    padded_tone(os.path.join(WWW, "lead.mp3"), 12, 48, 0, "-b:a", "128k")      # 60 s, the first 12 of them silent
    make_audio(os.path.join(WWW, "plain.mp3"), 60, "-b:a", "128k")
    SRV = FileServer(WWW)
    SAVED.update(archive=sources.archive_candidates, youtube=sources.youtube_candidates, lookup=catalog.lookup)
    sources.youtube_candidates = lambda *a, **k: []
    sources.archive_candidates = lambda rc, stop, fits, deep: [c for c in [{
        "source": "archive.org", "id": "t:" + rc.track.title, "url": SRV.url + rc.track.extra["file"], "ext": ".mp3",
        "seconds": 60.0, "kbps": 128, "lossless": False, "score": 1, "title": rc.track.title}] if fits(rc, c["seconds"], deep)]
    catalog.lookup = lambda lib, track, stop, want_genre=False: {
        "durations": [], "covers": [], "album": "", "year": "", "track_no": 0, "disc_no": 0, "isrc": "", "genre": "",
        "t": time.time()}


def tearDownModule():
    SRV.close()
    sources.archive_candidates, sources.youtube_candidates = SAVED["archive"], SAVED["youtube"]
    catalog.lookup = SAVED["lookup"]


def job_settings(**kw):
    st = Settings()
    st.auto, st.parallel, st.verify, st.mode, st.fmt, st.bitrate = False, 1, True, "advanced", "mp3", 128
    st.device, st.match_source, st.embed_art = "other", False, False
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def run(names_files, out, st):
    ev = []
    tracks = [Track(n, "Tester", duration=60.0, extra={"file": f}) for n, f in names_files]
    engine.Job(tracks, out, st, emit=ev.append).run()
    return {e["result"].track: e["result"] for e in ev if e["type"] == "result"}, ev


class SoundJobTests(unittest.TestCase):
    def test_trimmed_silence_still_counts_toward_the_songs_length(self):
        out = fresh_dir("sound_out")
        st = job_settings(trim=True)
        r, _ = run([("Lead", "lead.mp3")], out, st)
        self.assertEqual(r["Lead"].status, "ok", r["Lead"].note)
        self.assertIn("Trimmed", r["Lead"].note)
        path = os.path.join(out, "Lead - Tester.mp3")
        self.assertAlmostEqual(read_info(path).seconds, 48.1, delta=1.0,
                               msg="48 s on its own is 20 % short of 60, yet the song was kept")
        rec = engine.Library(out).track("Lead - Tester.mp3")
        self.assertGreater(rec.get("removed", 0), 11)
        r2, ev2 = run([("Lead", "lead.mp3")], out, st)
        self.assertEqual(r2["Lead"].status, "skipped", "the next run does not take the trimmed file for a wrong cut")

    def test_a_song_already_right_is_saved_untouched_with_no_note(self):
        out = fresh_dir("sound_out2")
        r, _ = run([("Plain", "plain.mp3")], out, job_settings(trim=True))
        self.assertEqual(r["Plain"].status, "ok")
        self.assertNotIn("Trimmed", r["Plain"].note)
        self.assertNotIn("skipped", r["Plain"].note)

    def test_an_analysis_that_fails_never_fails_the_song(self):
        out = fresh_dir("sound_out3")
        saved = process.analyze
        process.analyze = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            r, _ = run([("Plain", "plain.mp3")], out, job_settings(level=True))
        finally:
            process.analyze = saved
        self.assertEqual(r["Plain"].status, "ok")
        self.assertIn("Sound options skipped", r["Plain"].note)


if __name__ == "__main__":
    unittest.main()
