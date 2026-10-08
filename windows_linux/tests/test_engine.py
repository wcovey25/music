"""End-to-end engine tests against a local fake server (synthetic audio, no real services)."""
import errno
import os
import shutil
import threading
import time
import unittest

from helpers import FileServer, RangeServer, fresh_dir, make_audio, noise_cover, HOME

from musicdl.audio import transcode
from musicdl.config import Settings
from musicdl.core import catalog, engine, netio, sources
from musicdl.core.disk import DiskGuard, is_disk_full
from musicdl.core.library import STATE_NAME
from musicdl.core.models import DiskFull, EngineError, Track
from musicdl.meta import lyrics as lyrics_mod
from musicdl.meta.naming import full_path, relative_name
from musicdl.meta.tags import read_info

WWW = fresh_dir("www")
SRV = None
PLAN = {}


def cand(f, secs, kbps, lossless=False):
    return {"source": "archive.org", "id": f"t:{f}", "url": SRV.url + f, "ext": os.path.splitext(f)[1], "seconds": secs,
            "kbps": 1411 if lossless else kbps, "lossless": lossless, "score": 1, "title": f}


_ORIGINAL = {}


def setUpModule():
    global SRV
    _ORIGINAL.update(archive=sources.archive_candidates, youtube=sources.youtube_candidates, lookup=catalog.lookup,
                     limits=[(l, l.interval) for l in (netio.DEEZER_LIMIT, netio.WEB_LIMIT, netio.CAA_LIMIT)])
    make_audio(os.path.join(WWW, "good128.mp3"), 60, "-b:a", "128k")
    make_audio(os.path.join(WWW, "good64.mp3"), 60, "-b:a", "64k")
    make_audio(os.path.join(WWW, "good_ogg.ogg"), 60, "-c:a", "libvorbis", "-q:a", "6")
    make_audio(os.path.join(WWW, "lossless.flac"), 60, "-c:a", "flac")
    make_audio(os.path.join(WWW, "long.mp3"), 900, "-b:a", "32k")           # "75 minute podcast" stand-in
    make_audio(os.path.join(WWW, "short.mp3"), 20, "-b:a", "128k")          # preview clip
    noise_cover(os.path.join(WWW, "cover.png"))
    SRV = FileServer(WWW)
    netio.DEEZER_LIMIT.interval = netio.WEB_LIMIT.interval = netio.CAA_LIMIT.interval = 0.01
    sources.archive_candidates = lambda rc, stop, fits, deep: [c for c in PLAN.get(rc.track.title, []) if fits(rc, c["seconds"], deep)]
    sources.youtube_candidates = lambda *a, **k: []
    catalog.lookup = lambda lib, track, stop, want_genre=False: {
        "durations": [], "covers": [SRV.url + "cover.png"] if track.title != "Echo" else [], "album": "Test Album",
        "year": "2001", "track_no": 3, "disc_no": 1, "isrc": "", "genre": "", "t": time.time()}


def tearDownModule():
    SRV.close()
    sources.archive_candidates, sources.youtube_candidates = _ORIGINAL["archive"], _ORIGINAL["youtube"]
    catalog.lookup = _ORIGINAL["lookup"]
    for lim, interval in _ORIGINAL["limits"]:
        lim.interval = interval


def run(tracks, out, st, stop=None, ask=None):
    ev = []
    counts = engine.Job(tracks, out, st, emit=ev.append, stop=stop, ask=ask).run()
    return ev, counts


class Asker:
    """Stands in for the window's 'replace with higher quality?' question."""

    def __init__(self, answer=True):
        self.answer, self.calls = answer, []

    def __call__(self, summary):
        self.calls.append(summary)
        return self.answer


def results(ev):
    return {e["result"].track: e["result"] for e in ev if e["type"] == "result"}


def tracks(*names):
    return [Track(n, "Tester", duration=60.0) for n in names]


def settings(**kw):
    st = Settings()
    st.auto, st.parallel, st.min_kbps, st.verify = False, 3, 128, True
    st.device = "other"                      # MP3/FLAC on every OS (a Mac's default would be AAC/ALAC)
    st.match_source = False                  # these tests check that Advanced writes what it is told; see test_match_source
    for k, v in kw.items():
        setattr(st, k, v)
    return st


class EngineTests(unittest.TestCase):
    def setUp(self):
        PLAN.clear()
        PLAN.update({
            "Alpha": [cand("long.mp3", 900, 32), cand("good128.mp3", 60, 128)],     # first is too long -> rejected
            "Bravo": [cand("good_ogg.ogg", 60, 190)],                                 # needs re-encode
            "Charlie": [cand("short.mp3", 60, 128)],                                  # listing says 60 s, the file is a 20 s preview
            "Delta": [cand("good64.mp3", 60, 64)],                                    # only a low-quality source
            "Echo": [cand("good128.mp3", 60, 128)],                                   # no artwork anywhere
            "Foxtrot": [cand("long.mp3", 60, 32)],                                    # listing says 60 s, the file is 15 minutes
            "Golf": [cand("long.mp3", 900, 32)],                                      # 15-minute listing: rejected before downloading
        })
        self.out = fresh_dir("out")

    def test_mp3_run_validation_and_rerun(self):
        st = settings(mode="advanced", fmt="mp3", bitrate=320)
        names = list(PLAN)
        ev, counts = run(tracks(*names), self.out, st)
        r = results(ev)
        self.assertEqual(r["Alpha"].status, "ok")
        self.assertAlmostEqual(r["Alpha"].seconds, 60, delta=2)
        self.assertEqual(r["Bravo"].status, "ok")
        self.assertEqual(r["Bravo"].src_kbps, 190)
        self.assertGreaterEqual(r["Bravo"].kbps, 300)                 # ogg re-encoded to 320
        self.assertEqual(r["Charlie"].status, "bad-length")
        self.assertTrue(r["Delta"].low_quality)
        self.assertTrue(r["Echo"].no_cover and r["Echo"].status == "ok")
        self.assertEqual(r["Foxtrot"].status, "bad-length")
        self.assertEqual(r["Golf"].status, "no-file", "a too-long listing is never even downloaded")
        self.assertFalse(os.path.exists(os.path.join(self.out, "Charlie - Tester.mp3")))
        self.assertFalse(os.path.exists(os.path.join(self.out, ".musicdl_tmp")), "temp folder cleaned up")
        a = read_info(os.path.join(self.out, "Alpha - Tester.mp3"))
        self.assertTrue(a.cover)
        self.assertEqual((a.source, a.src_kbps, a.album, a.year), ("archive.org", 128, "Test Album", "2001"))
        # re-run recognises everything it already has
        ev2, _ = run(tracks(*names), self.out, st)
        began = [e["track"].title for e in ev2 if e["type"] == "begin"]
        self.assertNotIn("Alpha", began)
        self.assertNotIn("Bravo", began)
        self.assertNotIn("Delta", began, "low-quality file whose upgrade failed is not retried every run")
        self.assertEqual(results(ev2)["Alpha"].status, "skipped")

    def test_wrong_length_file_is_replaced(self):
        st = settings(mode="advanced", fmt="mp3", bitrate=128)
        run(tracks("Alpha"), self.out, st)
        path = os.path.join(self.out, "Alpha - Tester.mp3")
        shutil.copyfile(os.path.join(WWW, "long.mp3"), path)                # like Skyfall at 75 minutes
        ev, _ = run(tracks("Alpha"), self.out, st)
        self.assertEqual(results(ev)["Alpha"].status, "upgraded")
        self.assertAlmostEqual(read_info(path).seconds, 60, delta=2)

    def test_cover_only_repair(self):
        st = settings(mode="advanced", fmt="mp3", bitrate=128)
        run(tracks("Echo"), self.out, st)
        path = os.path.join(self.out, "Echo - Tester.mp3")
        before = read_info(path)
        self.assertFalse(before.cover)
        catalog.lookup = lambda lib, track, stop, want_genre=False: {
            "durations": [], "covers": [SRV.url + "cover.png"], "album": "", "year": "", "track_no": 0, "disc_no": 0,
            "isrc": "", "genre": "", "t": time.time()}
        lib = engine.Library(self.out)
        rec = lib.track("Echo - Tester.mp3")
        rec["cover_tried"] = 0                                              # last attempt "long ago"
        lib.set_track("Echo - Tester.mp3", rec)
        lib.save(force=True)
        try:
            ev, _ = run(tracks("Echo"), self.out, st)
        finally:
            setUpModule_catalog()
        self.assertEqual(results(ev)["Echo"].status, "fixed")
        after = read_info(path)
        self.assertTrue(after.cover)
        self.assertAlmostEqual(after.seconds, before.seconds, delta=0.01)

    def test_every_output_format(self):
        for key, ext, lossless in (("flac", ".flac", True), ("wav", ".wav", True), ("aac", ".m4a", False),
                                   ("alac", ".m4a", True), ("mp3", ".mp3", False)):
            with self.subTest(fmt=key):
                out = fresh_dir("fmt_" + key)
                PLAN["Alpha"] = [cand("good128.mp3", 60, 128)]
                st = settings(mode="advanced", fmt=key, bitrate=192)
                ev, _ = run([Track("Alpha", "Tester", album="LP", year="1999", track_no=2, duration=60.0)], out, st)
                res = results(ev)["Alpha"]
                self.assertEqual(res.status, "ok", res.note)
                info = read_info(os.path.join(out, "Alpha - Tester" + ext))
                self.assertIsNotNone(info)
                self.assertTrue(info.cover, "artwork embedded")
                self.assertEqual((info.title, info.artist, info.album), ("Alpha", "Tester", "LP"))
                self.assertEqual(info.lossless, lossless)
                self.assertEqual(info.src_kbps, 128)
                self.assertAlmostEqual(info.seconds, 60, delta=2)

    def test_lossless_source_to_flac_is_copied(self):
        out = fresh_dir("flac_copy")
        PLAN["Alpha"] = [cand("lossless.flac", 60, 1411, lossless=True)]
        ev, _ = run(tracks("Alpha"), out, settings(preset="best"))
        res = results(ev)["Alpha"]
        self.assertEqual(res.status, "ok")
        self.assertEqual(res.src_kbps, 1411)
        self.assertTrue(os.path.exists(os.path.join(out, "Alpha - Tester.flac")))

    def test_naming_template_and_folders(self):
        out = fresh_dir("tpl")
        PLAN["Alpha"] = [cand("good128.mp3", 60, 128)]
        t = Track("Alpha", "Ar/tist", album="Al:bum", year="1999", track_no=2, duration=60.0)
        st = settings(mode="advanced", fmt="mp3", bitrate=192, template="{artist}/{album} ({year})/{track:02d} {title}")
        run([t], out, st)
        expect = os.path.join(out, "Ar-tist", "Al -bum (1999)", "02 Alpha.mp3")
        self.assertTrue(os.path.exists(expect), expect)
        # path tricks can never escape the output folder
        self.assertEqual(relative_name(Track("../x", "..", album=""), "{artist}/{album}/{title}", ".mp3"),
                         "Unknown/Unknown/..-x.mp3")

    def test_lyrics_embedded_and_lrc(self):
        out = fresh_dir("lyr")
        PLAN["Alpha"] = [cand("good128.mp3", 60, 128)]
        orig = lyrics_mod.fetch
        lyrics_mod.fetch = lambda *a, **k: lyrics_mod.Lyrics("la la la", "[00:01.00] la la la")
        try:
            st = settings(mode="advanced", fmt="mp3", bitrate=192, fetch_lyrics=True, lrc_files=True)
            ev, _ = run(tracks("Alpha"), out, st)
        finally:
            lyrics_mod.fetch = orig
        self.assertTrue(results(ev)["Alpha"].lyrics)
        self.assertTrue(read_info(os.path.join(out, "Alpha - Tester.mp3")).lyrics)
        self.assertTrue(os.path.exists(os.path.join(out, "Alpha - Tester.lrc")))

    def test_direct_track_skips_search_and_length_rules(self):
        out = fresh_dir("direct")
        PLAN.clear()
        calls = []
        orig = sources.fetch

        def fake_fetch(c, tmp, stop):                       # pretend the pasted link downloads our test tone
            calls.append(c["id"])
            path = orig({"source": "archive.org", "url": SRV.url + "good128.mp3", "ext": ".mp3", "kbps": 128}, tmp, stop)[0]
            return path, 128, "mp3"
        sources.fetch = fake_fetch
        try:
            t = Track("Mystery Mix", "DJ Test", url="https://example.invalid/watch?v=abcdefghijk", duration=0)
            ev, _ = run([t], out, settings(mode="advanced", fmt="mp3", bitrate=128))
        finally:
            sources.fetch = orig
        self.assertEqual(results(ev)["Mystery Mix"].status, "ok")
        self.assertTrue(calls and calls[0].startswith("direct:"))

    def test_parallel_is_faster(self):
        PLAN.clear()
        names = [f"S{i}" for i in range(6)]
        for n in names:
            PLAN[n] = [cand("good128.mp3", 60, 128)]
        SRV.delay = 1.0
        try:
            out1 = fresh_dir("p1")
            t0 = time.time()
            run(tracks(*names), out1, settings(parallel=1, mode="advanced", fmt="mp3", bitrate=128))
            t1 = time.time() - t0
            out3 = fresh_dir("p3")
            t0 = time.time()
            run(tracks(*names), out3, settings(parallel=3, mode="advanced", fmt="mp3", bitrate=128))
            t3 = time.time() - t0
        finally:
            SRV.delay = 0.0
        self.assertLess(t3, t1 * 0.7, f"parallel=1 {t1:.1f}s vs parallel=3 {t3:.1f}s")

    def test_stop_is_prompt_and_clean(self):
        out = fresh_dir("stop")
        PLAN.clear()
        for n in ("A", "B", "C", "D", "E", "F"):
            PLAN[n] = [cand("good128.mp3", 60, 128)]
        SRV.delay = 3.0
        stop = threading.Event()
        ev = []

        def emit(e):
            ev.append(e)
            if e["type"] == "begin":
                threading.Timer(0.3, stop.set).start()
        try:
            t0 = time.time()
            engine.Job(tracks("A", "B", "C", "D", "E", "F"), out, settings(parallel=3), emit=emit, stop=stop).run()
            dt = time.time() - t0
        finally:
            SRV.delay = 0.0
        self.assertLess(dt, 6, "stop returns promptly")
        self.assertFalse([f for f in os.listdir(out) if f.endswith(".mp3")])
        self.assertFalse(os.path.exists(os.path.join(out, ".musicdl_tmp")))


class DuplicateTests(unittest.TestCase):
    """A song already in the folder is never downloaded twice — unless a clearly better version is wanted."""

    def setUp(self):
        PLAN.clear()
        self.out = fresh_dir("dup")

    def path(self, name):
        return os.path.join(self.out, name)

    @staticmethod
    def began(ev):
        return [e["track"].title for e in ev if e["type"] == "begin"]

    def first_run(self, kind="lossless", **fmt):
        """Save 'Alpha' the way an earlier session would have: a 96/128 kbps MP3 made from a lossless original."""
        PLAN["Alpha"] = [cand("lossless.flac", 60, 1411, lossless=True)] if kind == "lossless" \
            else [cand("good128.mp3", 60, 128)]
        st = settings(mode="advanced", **{"fmt": "mp3", "bitrate": 128, **fmt})
        ev, _ = run(tracks("Alpha"), self.out, st)
        self.assertEqual(results(ev)["Alpha"].status, "ok")

    # ---- plain duplicates

    def test_same_song_in_another_format_is_not_downloaded(self):
        PLAN["Alpha"] = [cand("lossless.flac", 60, 1411, lossless=True)]
        run(tracks("Alpha"), self.out, settings(preset="best"))
        self.assertTrue(os.path.exists(self.path("Alpha - Tester.flac")))
        PLAN["Alpha"] = [cand("good128.mp3", 60, 128)]
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="better"), ask=Asker())
        self.assertEqual(self.began(ev), [], "nothing is downloaded")
        res = results(ev)["Alpha"]
        self.assertEqual(res.status, "skipped")
        self.assertIn("Already in your library", res.note)
        self.assertFalse(os.path.exists(self.path("Alpha - Tester.mp3")), "no duplicate in the other format")

    def test_song_under_another_file_name_is_recognised(self):
        PLAN["Alpha"] = [cand("good128.mp3", 60, 128)]
        run(tracks("Alpha"), self.out, settings(mode="advanced", fmt="mp3", bitrate=128, template="{artist}/{title}"))
        self.assertTrue(os.path.exists(self.path(os.path.join("Tester", "Alpha.mp3"))))
        ev, _ = run(tracks("Alpha"), self.out, settings(mode="advanced", fmt="mp3", bitrate=128))
        self.assertEqual(self.began(ev), [])
        self.assertFalse(os.path.exists(self.path("Alpha - Tester.mp3")))

    def test_second_run_after_new_files_only_fetches_the_new_songs(self):
        PLAN.update({n: [cand("good128.mp3", 60, 128)] for n in ("A1", "A2", "A3", "A4")})
        st = settings(mode="advanced", fmt="mp3", bitrate=128)
        run(tracks("A1", "A2"), self.out, st)
        ev, _ = run(tracks("A1", "A2", "A3", "A4"), self.out, st, ask=Asker())
        self.assertEqual(sorted(self.began(ev)), ["A3", "A4"])

    # ---- better versions

    def test_replace_with_better_version(self):
        self.first_run(bitrate=128)
        mp3 = self.path("Alpha - Tester.mp3")
        self.assertAlmostEqual(read_info(mp3).kbps, 128, delta=12)
        ask = Asker(True)
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="best"), ask=ask)
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(ask.calls[0]["count"], 1)
        res = results(ev)["Alpha"]
        self.assertEqual(res.status, "upgraded", res.note)
        self.assertIn("Replaced MP3 128 kbps", res.note)
        self.assertFalse(os.path.exists(mp3), "the old copy goes once the new one is saved")
        info = read_info(self.path("Alpha - Tester.flac"))
        self.assertTrue(info.lossless and info.cover)
        self.assertEqual([f for f in os.listdir(self.out) if not f.startswith(".")], ["Alpha - Tester.flac"])
        self.assertFalse([k for k in engine.Library(self.out).files() if k.endswith(".mp3")], "no stale library entry")
        # and the next run, at the same quality, is quiet
        ask2 = Asker(True)
        ev2, _ = run(tracks("Alpha"), self.out, settings(preset="best"), ask=ask2)
        self.assertEqual((self.began(ev2), ask2.calls), ([], []))

    def test_question_is_asked_once_for_many_songs(self):
        PLAN.update({n: [cand("lossless.flac", 60, 1411, lossless=True)] for n in ("A1", "A2", "A3")})
        run(tracks("A1", "A2", "A3"), self.out, settings(mode="advanced", fmt="mp3", bitrate=128))
        ask = Asker(True)
        ev, _ = run(tracks("A1", "A2", "A3"), self.out, settings(preset="best"), ask=ask)
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(ask.calls[0]["count"], 3)
        self.assertEqual(sorted(self.began(ev)), ["A1", "A2", "A3"])
        self.assertEqual(sorted(f for f in os.listdir(self.out) if not f.startswith(".")),
                         ["A1 - Tester.flac", "A2 - Tester.flac", "A3 - Tester.flac"])

    def test_keep_existing_downloads_nothing(self):
        self.first_run(bitrate=128)
        before = read_info(self.path("Alpha - Tester.mp3"))
        ask = Asker(False)
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="best"), ask=ask)
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(self.began(ev), [])
        self.assertEqual(results(ev)["Alpha"].status, "skipped")
        self.assertEqual(sorted(f for f in os.listdir(self.out) if not f.startswith(".")), ["Alpha - Tester.mp3"])
        self.assertEqual(read_info(self.path("Alpha - Tester.mp3")).size, before.size)

    def test_no_question_without_a_window_or_when_the_setting_says_so(self):
        self.first_run(bitrate=128)
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="best"))                     # headless: nobody to ask
        self.assertEqual(self.began(ev), [])
        ask = Asker(True)
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="best", upgrade="keep"), ask=ask)
        self.assertEqual((self.began(ev), ask.calls), ([], []))
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="best", upgrade="replace"), ask=ask)
        self.assertEqual(ask.calls, [], "'Replace' in Settings never asks")
        self.assertEqual(results(ev)["Alpha"].status, "upgraded")
        self.assertTrue(os.path.exists(self.path("Alpha - Tester.flac")))

    def test_same_file_name_192_to_320(self):
        self.first_run(bitrate=192)
        ask = Asker(True)
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="better"), ask=ask)
        self.assertEqual(len(ask.calls), 1)
        self.assertEqual(results(ev)["Alpha"].status, "upgraded")
        self.assertGreaterEqual(read_info(self.path("Alpha - Tester.mp3")).kbps, 300)
        self.assertEqual([f for f in os.listdir(self.out) if not f.startswith(".")], ["Alpha - Tester.mp3"])

    def test_a_low_quality_source_is_not_worth_asking_about(self):
        self.first_run(kind="lossy", bitrate=192)                       # 192 kbps file made from a 128 kbps source
        ask = Asker(True)
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="better"), ask=ask)
        self.assertEqual((self.began(ev), ask.calls), ([], []))

    def test_next_best_when_no_lossless_version_exists(self):
        self.first_run(bitrate=96)
        PLAN["Alpha"] = [cand("good128.mp3", 60, 128)]                   # lossless is wanted, only 128 kbps exists now
        ask = Asker(True)
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="best"), ask=ask)
        res = results(ev)["Alpha"]
        self.assertEqual(res.status, "upgraded", res.note)
        self.assertIn("no lossless version found", res.note)
        self.assertFalse(os.path.exists(self.path("Alpha - Tester.flac")), "a lossless copy of a lossy file would be a fake")
        self.assertGreaterEqual(read_info(self.path("Alpha - Tester.mp3")).kbps, 120)
        ask2 = Asker(True)
        ev2, _ = run(tracks("Alpha"), self.out, settings(preset="best"), ask=ask2)    # nothing better exists: stay quiet
        self.assertEqual((self.began(ev2), ask2.calls), ([], []))

    def test_nothing_better_found_keeps_the_file_and_stops_searching(self):
        self.first_run(bitrate=96)
        PLAN["Alpha"] = []
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="best"), ask=Asker(True))
        res = results(ev)["Alpha"]
        self.assertEqual(res.status, "kept", res.note)
        self.assertTrue(os.path.exists(self.path("Alpha - Tester.mp3")))
        ask = Asker(True)
        ev2, _ = run(tracks("Alpha"), self.out, settings(preset="best"), ask=ask)
        self.assertEqual((self.began(ev2), ask.calls), ([], []), "not searched again on every run")

    def test_failed_better_download_keeps_the_old_file(self):
        self.first_run(bitrate=128)
        PLAN["Alpha"] = [cand("missing-file.flac", 60, 1411, lossless=True)]          # 404 on the server
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="best", retries=0), ask=Asker(True))
        self.assertTrue(os.path.exists(self.path("Alpha - Tester.mp3")), "never lose the old copy")
        self.assertFalse(os.path.exists(self.path("Alpha - Tester.flac")))
        self.assertEqual(results(ev)["Alpha"].status, "kept")

    def test_closing_the_window_during_the_question_changes_nothing(self):
        self.first_run(bitrate=128)
        stop = threading.Event()

        def ask(summary):
            stop.set()
            raise engine.Stopped()
        ev, _ = run(tracks("Alpha"), self.out, settings(preset="best"), stop=stop, ask=ask)
        self.assertEqual(self.began(ev), [])
        self.assertEqual(sorted(f for f in os.listdir(self.out) if not f.startswith(".")), ["Alpha - Tester.mp3"])


class DiskFullTests(unittest.TestCase):
    """A full disk pauses the run (and says so) instead of failing songs; it resumes by itself."""

    def setUp(self):
        PLAN.clear()
        for n in ("A1", "A2", "A3"):
            PLAN[n] = [cand("good128.mp3", 60, 128)]
        self.out = fresh_dir("diskfull")
        self.space = {"free": 10 ** 12}                       # what the pretend drive reports

    def job(self, names=("A1", "A2", "A3"), stop=None, parallel=2):
        ev = []
        job = engine.Job(tracks(*names), self.out, settings(mode="advanced", fmt="mp3", bitrate=128, parallel=parallel),
                         emit=ev.append, stop=stop)
        job.disk = DiskGuard(self.out, job.stop, job.emit, reserve=1000, poll=0.05, free=lambda: self.space["free"])
        return job, ev

    def freed_later(self, seconds):
        t = threading.Timer(seconds, lambda: self.space.update(free=10 ** 12))
        t.start()
        return t

    def test_disk_already_full_pauses_then_carries_on(self):
        self.space["free"] = 0
        job, ev = self.job()
        timer = self.freed_later(0.6)
        counts = job.run()
        timer.join()
        kinds = [e["type"] for e in ev if e["type"] in ("paused", "resumed")]
        self.assertEqual(kinds, ["paused", "resumed"], "one pause, one resume — not one per song")
        paused = next(e for e in ev if e["type"] == "paused")
        self.assertEqual((paused["reason"], paused["free"]), ("disk", 0))
        self.assertEqual(counts, {"ok": 3})
        self.assertEqual(sorted(f for f in os.listdir(self.out) if f.endswith(".mp3")),
                         ["A1 - Tester.mp3", "A2 - Tester.mp3", "A3 - Tester.mp3"])

    def test_no_space_error_mid_download_is_not_a_failure(self):
        real, state = sources.fetch, {"hit": 0}

        def flaky(c, tmp, stop):
            if state["hit"] == 0:
                state["hit"] = 1
                with open(os.path.join(tmp, "partial.bin"), "wb") as fh:
                    fh.write(b"x" * 1000)
                self.space["free"] = 0                          # the drive filled up while this song was downloading
                self.freed_later(0.5)
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(c, tmp, stop)
        sources.fetch = flaky
        try:
            job, ev = self.job(("A1",), parallel=1)
            counts = job.run()
        finally:
            sources.fetch = real
        self.assertEqual(counts, {"ok": 1})
        self.assertEqual([e["type"] for e in ev if e["type"] in ("paused", "resumed")], ["paused", "resumed"])
        self.assertTrue(os.path.exists(os.path.join(self.out, "A1 - Tester.mp3")))
        self.assertFalse(os.path.exists(os.path.join(self.out, ".musicdl_tmp")), "half-written files are cleaned up")

    def test_ffmpeg_out_of_space_message_is_recognised(self):
        real, state = transcode.encode, {"hit": 0}

        def flaky(*a, **k):
            if state["hit"] == 0:
                state["hit"] = 1
                self.space["free"] = 0
                self.freed_later(0.4)
                raise EngineError("Error writing trailer: No space left on device")
            return real(*a, **k)
        transcode.encode = flaky
        try:
            job, ev = self.job(("A1",), parallel=1)
            counts = job.run()
        finally:
            transcode.encode = real
        self.assertEqual(counts, {"ok": 1})
        self.assertIn("paused", [e["type"] for e in ev])

    def test_stop_while_paused_is_prompt_and_nothing_fails(self):
        self.space["free"] = 0
        stop = threading.Event()
        job, ev = self.job(stop=stop)
        threading.Timer(0.5, stop.set).start()
        t0 = time.time()
        counts = job.run()
        self.assertLess(time.time() - t0, 3)
        self.assertEqual(counts, {})
        self.assertFalse([e for e in ev if e["type"] == "result"], "no song was marked failed")

    def test_false_alarm_does_not_loop_forever(self):
        real = sources.fetch
        sources.fetch = lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device"))
        try:
            job, ev = self.job(("A1",), parallel=1)
            job.run()                                           # the drive reports plenty of room, so waiting is pointless
        finally:
            sources.fetch = real
        res = results(ev)["A1"]
        self.assertEqual(res.status, "error")
        self.assertIn("disk", res.note.lower())

    def test_detecting_full_disk_errors(self):
        self.assertTrue(is_disk_full(OSError(errno.ENOSPC, "x")))
        self.assertTrue(is_disk_full(DiskFull("x")))
        self.assertTrue(is_disk_full("[Errno 28] No space left on device"))
        self.assertTrue(is_disk_full(EngineError("There is not enough space on the disk")))
        self.assertTrue(is_disk_full(RuntimeError("wrapped") if False else _chained()))
        self.assertFalse(is_disk_full(OSError(errno.EACCES, "Permission denied")))
        self.assertFalse(is_disk_full(EngineError("download too small")))
        self.assertFalse(is_disk_full(None))


class _NoScouts:
    """Stands in for engine._Scouts to measure what searching ahead is worth."""

    def __init__(self, *a, **k):
        pass

    def start(self): pass
    def advance(self): pass
    def close(self): pass


class PipelineTests(unittest.TestCase):
    """The search/lookup pipeline: scouts, artwork shared by an album, which sources are asked, final failures."""

    def setUp(self):
        PLAN.clear()
        self.out = fresh_dir("pipeline")
        self.saved = (sources.archive_candidates, sources.youtube_candidates, catalog.lookup, sources.fetch)

    def tearDown(self):
        sources.archive_candidates, sources.youtube_candidates, catalog.lookup, sources.fetch = self.saved

    def many(self, n):
        names = [f"S{i}" for i in range(n)]
        for nm in names:
            PLAN[nm] = [cand("good128.mp3", 60, 128)]
        return names

    def test_album_artwork_is_fetched_and_made_once(self):
        names = self.many(8)
        srv = RangeServer(WWW)
        catalog.lookup = lambda lib, track, stop, want_genre=False: {
            "durations": [], "covers": [srv.url + "cover.png"], "album": "Album", "year": "2001", "track_no": 1,
            "disc_no": 1, "isrc": "", "genre": "", "t": time.time()}
        try:
            ev, counts = run(tracks(*names), self.out, settings(mode="advanced", fmt="mp3", bitrate=128, parallel=4))
            asked = len(srv.requests_for("cover.png"))
        finally:
            srv.close()
        self.assertEqual(counts, {"ok": 8})
        self.assertEqual(asked, 1, "eight songs of one album: one download of the picture")
        for r in results(ev).values():
            self.assertTrue(r.cover and r.thumb)
        for n in names:
            self.assertTrue(read_info(os.path.join(self.out, f"{n} - Tester.mp3")).cover)

    def test_searching_ahead_overlaps_the_waiting(self):
        names = self.many(6)
        archive, lookup, fetch = sources.archive_candidates, catalog.lookup, sources.fetch

        def slow_archive(rc, stop, fits, deep):
            time.sleep(0.5)                                     # a slow network
            return archive(rc, stop, fits, deep)

        def slow_lookup(lib, track, stop, want_genre=False):
            time.sleep(0.3)
            return lookup(lib, track, stop, want_genre)

        def slow_fetch(c, tmp, stop):
            time.sleep(0.6)                                     # downloading and encoding take about as long as searching
            return fetch(c, tmp, stop)
        sources.archive_candidates, catalog.lookup, sources.fetch = slow_archive, slow_lookup, slow_fetch

        def timed(ahead):
            real, engine._Scouts = engine._Scouts, (engine._Scouts if ahead else _NoScouts)
            try:
                t0 = time.time()
                _ev, counts = run(tracks(*names), fresh_dir("pipeline_t"),
                                  settings(mode="advanced", fmt="mp3", bitrate=128, parallel=2))
                return time.time() - t0, counts
            finally:
                engine._Scouts = real
        without, c0 = timed(False)
        with_, c1 = timed(True)
        self.assertEqual((c0, c1), ({"ok": 6}, {"ok": 6}))
        self.assertLess(with_, without * 0.85, f"{with_:.1f}s with scouts vs {without:.1f}s without")

    def test_a_failed_search_ahead_does_not_fail_the_song(self):
        names = self.many(1)
        base, state = sources.archive_candidates, {"n": 0}

        def flaky(rc, stop, fits, deep):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("boom")
            return base(rc, stop, fits, deep)
        sources.archive_candidates = flaky
        _ev, counts = run(tracks(*names), self.out, settings(mode="advanced", fmt="mp3", bitrate=128, parallel=1))
        self.assertEqual(counts, {"ok": 1})

    def test_archive_is_only_searched_when_it_can_matter(self):
        asked = []
        sources.archive_candidates = lambda rc, stop, fits, deep: asked.append(1) or []
        tube = {"source": "youtube", "id": "yt:aaaaaaaaaaa", "url": "x", "vid": "aaaaaaaaaaa", "seconds": 60.0, "kbps": 195,
                "lossless": False, "score": 15.0, "title": "t", "probed": True}
        strong, weak = dict(tube), dict(tube, score=5.0)

        def count(preset, youtube, found):
            asked.clear()
            sources.youtube_candidates = lambda *a, **k: [dict(found)]
            job = engine.Job(tracks("S0"), fresh_dir("pipeline_c"), settings(preset=preset, youtube=youtube))
            job.lib = engine.Library(job.outdir)
            job._candidates(job._ctx(0, job.tracks[0]), False)
            return len(asked)
        self.assertEqual(count("good", True, strong), 0, "a convincing YouTube match is enough for a 192 kbps target")
        self.assertGreater(count("good", True, weak), 0, "but a doubtful one is checked against archive.org")
        self.assertGreater(count("better", True, strong), 0, "320 kbps asks for more than a video holds")
        self.assertGreater(count("best", True, strong), 0, "and so does lossless")
        self.assertGreater(count("good", False, strong), 0, "without YouTube there is nothing else")

    def test_a_video_that_is_gone_is_not_retried(self):
        PLAN["S0"] = [cand("good128.mp3", 60, 128), cand("good64.mp3", 60, 64)]
        real, calls = sources.fetch, []

        def fake(c, tmp, stop):
            calls.append(c["id"])
            if c["id"] == "t:good128.mp3":
                raise EngineError("ERROR: [youtube] abc: Video unavailable")
            return real(c, tmp, stop)
        sources.fetch = fake
        _ev, counts = run(tracks("S0"), self.out, settings(mode="advanced", fmt="mp3", bitrate=128, retries=3, parallel=1))
        self.assertEqual(calls, ["t:good128.mp3", "t:good64.mp3"], "asked once, then on to the next source")
        self.assertEqual(counts, {"ok": 1})

    def test_other_failures_are_retried(self):
        PLAN["S0"] = [cand("good128.mp3", 60, 128)]
        real, calls = sources.fetch, []

        def fake(c, tmp, stop):
            calls.append(1)
            if len(calls) < 3:
                raise EngineError("connection reset")
            return real(c, tmp, stop)
        sources.fetch = fake
        _ev, counts = run(tracks("S0"), self.out, settings(mode="advanced", fmt="mp3", bitrate=128, retries=3, parallel=1))
        self.assertEqual((len(calls), counts), (3, {"ok": 1}))

    def test_a_link_that_names_its_recording_needs_no_search(self):
        searched = []
        sources.archive_candidates = lambda rc, stop, fits, deep: searched.append(1) or []
        sources.fetch = lambda c, tmp, stop: (_download_to(c["url"], tmp), 128, "mp3")
        t = Track("Linked", "Tester", duration=60.0, url=SRV.url + "good128.mp3")
        _ev, counts = run([t], self.out, settings(mode="advanced", fmt="mp3", bitrate=128, parallel=1))
        self.assertEqual(counts, {"ok": 1})
        self.assertEqual(searched, [], "the link is the recording: no search")

    def test_a_link_that_fails_falls_back_to_the_search(self):
        PLAN["Linked"] = [cand("good128.mp3", 60, 128)]
        real = sources.fetch

        def fetch(c, tmp, stop):
            if c.get("direct"):
                raise EngineError("ERROR: Private video")
            return real(c, tmp, stop)
        sources.fetch = fetch
        t = Track("Linked", "Tester", duration=60.0, url="https://example.invalid/watch?v=xxxxxxxxxxx")
        _ev, counts = run([t], self.out, settings(mode="advanced", fmt="mp3", bitrate=128, parallel=1))
        self.assertEqual(counts, {"ok": 1})


def _download_to(url, tmp):
    path = os.path.join(tmp, "src.mp3")
    netio.download(url, path)
    return path


class YtDownloadTests(unittest.TestCase):
    """The download starts from what the probe found (the page is read once, not twice)."""

    def setUp(self):
        import yt_dlp
        self.mod, self.real = yt_dlp, yt_dlp.YoutubeDL
        self.calls = []
        outer = self

        class Fake:
            fail = None

            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def _file(self, info):
                with open(os.path.join(os.path.dirname(self.opts["outtmpl"]), "yt.webm"), "wb") as fh:
                    fh.write(b"audio")
                return dict(info, requested_downloads=[{"acodec": "opus", "abr": 130.0}])

            def process_ie_result(self, info, download=True):
                outer.calls.append("process")
                if Fake.fail:
                    raise Fake.fail
                return self._file(info)

            def extract_info(self, url, download=False):
                outer.calls.append("extract")
                return self._file({"acodec": "opus"})
        self.Fake = Fake
        yt_dlp.YoutubeDL = Fake
        self.tmp = fresh_dir("ytdl")

    def tearDown(self):
        self.mod.YoutubeDL = self.real

    def cand(self, info):
        return {"source": "youtube", "id": "yt:a", "url": "https://example.invalid/watch?v=a", "_info": info}

    def test_probe_answer_is_used(self):
        path, kbps, codec = sources._yt_download(self.cand({"formats": [1], "acodec": "opus"}), self.tmp, threading.Event())
        self.assertEqual(self.calls, ["process"], "no second look at the page")
        self.assertEqual((kbps, codec), (195, "opus"))
        self.assertTrue(os.path.exists(path))

    def test_without_a_probe_the_page_is_read(self):
        sources._yt_download(self.cand(None), self.tmp, threading.Event())
        self.assertEqual(self.calls, ["extract"])

    def test_if_the_probe_answer_has_gone_stale_it_asks_again(self):
        self.Fake.fail = RuntimeError("HTTP Error 403: Forbidden")
        c = self.cand({"formats": [1]})
        sources._yt_download(c, self.tmp, threading.Event())
        self.assertEqual(self.calls, ["process", "extract"])
        self.assertIsNone(c["_info"])

    def test_a_gone_video_is_not_asked_about_again(self):
        self.Fake.fail = RuntimeError("ERROR: Video unavailable")
        with self.assertRaises(RuntimeError):
            sources._yt_download(self.cand({"formats": [1]}), self.tmp, threading.Event())
        self.assertEqual(self.calls, ["process"])

    def test_a_full_disk_is_reported_not_retried(self):
        self.Fake.fail = OSError(errno.ENOSPC, "No space left on device")
        with self.assertRaises(OSError):
            sources._yt_download(self.cand({"formats": [1]}), self.tmp, threading.Event())
        self.assertEqual(self.calls, ["process"])


def _chained():
    try:
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError as inner:
            raise EngineError("download failed") from inner
    except EngineError as e:
        return e


def setUpModule_catalog():
    catalog.lookup = lambda lib, track, stop, want_genre=False: {
        "durations": [], "covers": [SRV.url + "cover.png"] if track.title != "Echo" else [], "album": "Test Album",
        "year": "2001", "track_no": 3, "disc_no": 1, "isrc": "", "genre": "", "t": time.time()}


if __name__ == "__main__":
    unittest.main()
