"""Close matches: what is offered for a song the strict search could not place, how each option is labelled, what is
never offered, the search ladder, and what the engine does with them (Ask / Auto / Off, a recording the user picked).
YouTube is faked; downloads come from a local file server."""
import os
import threading
import time
import unittest

from helpers import FileServer, fresh_dir, make_audio

from musicdl.config import Settings
from musicdl.core import catalog, closematch, engine, netio, sources, text
from musicdl.core.models import Track


def mkrc(title, artist, secs=240.0):
    t = Track(title, artist, duration=secs)
    rc = engine.Ctx(index=0, track=t, title=text.core_title(title), artist=text.first_artist(artist))
    rc.refs = [float(secs)] if secs else []
    rc.track_toks, rc.artist_toks = text.toks(rc.title), text.toks(rc.artist)
    return rc


def yt(title, secs, channel="SomeChannel", views=1_000_000, vid=None, **kw):
    return {"id": vid or str(abs(hash((title, channel, secs))) % 10 ** 11).zfill(11), "title": title, "duration": secs,
            "channel": channel, "view_count": views, **kw}


def judge(rc, e):
    return closematch.judge(rc, e, rc.refs, 0.15)


class JudgeTests(unittest.TestCase):
    def setUp(self):
        self.rc = mkrc("Harbour Lights", "Mara Quill", 240)

    def test_never_offered(self):
        for title in ("Mara Quill - Harbour Lights (Karaoke Version)", "Harbour Lights - Mara Quill [Nightcore]",
                      "Mara Quill - Harbour Lights (slowed + reverb)", "Mara Quill - Harbour Lights 8D Audio",
                      "Harbour Lights x Other Song Mashup - Mara Quill", "Mara Quill Harbour Lights REACTION",
                      "Harbour Lights guitar tutorial - Mara Quill", "Mara Quill - Harbour Lights 1 hour loop"):
            self.assertIsNone(judge(self.rc, yt(title, 240, "Mara Quill")), title)

    def test_an_hour_long_upload_is_another_piece(self):
        self.assertIsNone(judge(self.rc, yt("Mara Quill - Harbour Lights", 3600, "Mara Quill")))

    def test_a_live_take_is_offered_with_a_label_and_never_safe(self):
        o = judge(self.rc, yt("Mara Quill - Harbour Lights (Live at the Roundhouse)", 250, "Mara Quill"))
        self.assertIsNotNone(o)
        self.assertIn("Live version", o["why"])
        self.assertFalse(o["safe"])

    def test_a_word_in_the_songs_own_name_is_not_a_label(self):
        rc = mkrc("Live Forever", "Oasis", 276)
        o = judge(rc, yt("Oasis - Live Forever", 330, "Oasis"))
        self.assertNotIn("Live version", o["why"])

    def test_a_longer_cut_says_how_much_longer_and_is_safe(self):
        o = judge(self.rc, yt("Mara Quill - Harbour Lights", 282, "Mara Quill - Topic"))
        self.assertIn("0:42 longer", o["why"])
        self.assertTrue(o["safe"], "same song, same artist, within a quarter of the length")

    def test_more_than_a_quarter_off_is_offered_but_not_safe(self):
        o = judge(self.rc, yt("Mara Quill - Harbour Lights", 330, "Mara Quill - Topic"))
        self.assertIsNotNone(o)
        self.assertFalse(o["safe"])

    def test_another_artists_upload_says_so(self):
        o = judge(self.rc, yt("Harbour Lights", 241, "Dana Fell"))
        self.assertIsNotNone(o)
        self.assertIn("Uploaded by Dana Fell", o["why"])
        self.assertFalse(o["safe"])

    def test_another_song_by_another_artist_is_noise(self):
        self.assertIsNone(judge(self.rc, yt("Harbour Nights", 241, "Dana Fell")))


class LadderTests(unittest.TestCase):
    class Fake:
        def __init__(self, answers):
            self.answers, self.asked = answers, []

        def extract_info(self, q, download=False):
            self.asked.append(q)
            ans = self.answers.get(len(self.asked), [])
            if isinstance(ans, Exception):
                raise ans
            return {"entries": ans}

    def setUp(self):
        self._ydl = sources._ydl
        self.stop = threading.Event()

    def tearDown(self):
        sources._ydl = self._ydl

    def use(self, **answers):
        fake = self.Fake({int(k[1:]): v for k, v in answers.items()})
        sources._ydl = lambda kind: fake
        return fake

    def test_the_strict_searchs_results_are_used_first(self):
        rc = mkrc("Harbour Lights", "Mara Quill", 240)
        rc.raw["yt"] = {"entries": {e["id"]: e for e in (
            yt("Mara Quill - Harbour Lights (Live)", 250, "Mara Quill", vid="aaaaaaaaaaa"),
            yt("Mara Quill - Harbour Lights", 282, "Mara Quill - Topic", vid="bbbbbbbbbbb"),
            yt("Mara Quill - Harbour Lights (Acoustic)", 236, "Mara Quill", vid="ccccccccccc"))}}
        fake = self.use()
        got = closematch.around(rc, self.stop, rc.refs, 0.15)
        self.assertEqual(fake.asked, [], "enough good options already: no new search")
        self.assertEqual(got[0]["vid"], "bbbbbbbbbbb", "the unlabelled cut first")

    def test_the_ladder_title_alone_then_lyrics_then_plain_words(self):
        rc = mkrc("Harbour Lights!", "Mara Quill", 240)
        fake = self.use()
        self.assertEqual(closematch.around(rc, self.stop, rc.refs, 0.15), [])
        self.assertEqual(len(fake.asked), 3)
        self.assertEqual(fake.asked[0], "ytsearch8:Harbour Lights!")
        self.assertIn("lyrics", fake.asked[1])
        self.assertNotIn("!", fake.asked[2])

    def test_two_failures_end_it_and_asking_again_is_free(self):
        rc = mkrc("Harbour Lights", "Mara Quill", 240)
        fake = self.use(q1=RuntimeError("429"), q2=RuntimeError("429"))
        closematch.around(rc, self.stop, rc.refs, 0.15)
        closematch.around(rc, self.stop, rc.refs, 0.15)
        self.assertEqual(len(fake.asked), 2)

    def test_at_most_two_by_other_artists(self):
        rc = mkrc("Harbour Lights", "Mara Quill", 240)
        others = [yt("Harbour Lights", 240 + i, f"Singer {i}", vid=f"{i:011d}") for i in range(5)]
        self.use(q1=others)
        got = closematch.around(rc, self.stop, rc.refs, 0.15)
        self.assertEqual(len(got), 2)
        self.assertLessEqual(len(got), closematch.MAX_OPTIONS)


# ---------------------------------------------------------------- the engine

WWW = fresh_dir("close_www")
SRV = None
SAVED = {}


def setUpModule():
    global SRV
    make_audio(os.path.join(WWW, "song.mp3"), 75, "-b:a", "128k")
    SRV = FileServer(WWW)
    SAVED.update(archive=sources.archive_candidates, youtube=sources.youtube_candidates, lookup=catalog.lookup,
                 around=closematch.around, web=netio.WEB_LIMIT.interval)
    netio.WEB_LIMIT.interval = 0.01
    sources.archive_candidates = lambda *a, **k: []
    sources.youtube_candidates = lambda *a, **k: []
    catalog.lookup = lambda lib, track, stop, want_genre=False: {
        "durations": [], "covers": [], "album": "", "year": "", "track_no": 0, "disc_no": 0, "isrc": "", "genre": "",
        "t": time.time()}


def tearDownModule():
    SRV.close()
    sources.archive_candidates, sources.youtube_candidates = SAVED["archive"], SAVED["youtube"]
    catalog.lookup, closematch.around = SAVED["lookup"], SAVED["around"]
    netio.WEB_LIMIT.interval = SAVED["web"]


def option(safe=True, why=("1:15 longer",), secs=75.0):
    return {"source": "archive.org", "id": f"close:{safe}", "url": SRV.url + "song.mp3", "ext": ".mp3", "vid": "",
            "seconds": secs, "kbps": 128, "lossless": False, "score": 9.0, "title": "Harbour Lights (extended)",
            "channel": "Mara Quill", "why": list(why), "safe": safe, "close": True}


def settings(**kw):
    st = Settings()
    st.auto, st.parallel, st.verify, st.mode, st.fmt, st.bitrate = False, 1, True, "advanced", "mp3", 128
    st.embed_art = False
    for k, v in kw.items():
        setattr(st, k, v)
    return st


def run(track, st):
    ev = []
    out = fresh_dir("close_out")
    engine.Job([track], out, st, emit=ev.append).run()
    return [e["result"] for e in ev if e["type"] == "result"][0], out


class EngineCloseTests(unittest.TestCase):
    def setUp(self):
        self.asked = []

        def around(rc, stop, refs, tol, youtube=True):
            self.asked.append(rc.track.title)
            return self.options
        closematch.around = around
        self.options = [option()]

    def tearDown(self):
        closematch.around = SAVED["around"]

    def test_ask_keeps_the_options_with_the_result(self):
        res, _ = run(Track("Harbour Lights", "Mara Quill", duration=200.0), settings(close_match="ask"))
        self.assertEqual(res.status, "no-file")
        self.assertEqual(len(res.close), 1)
        self.assertIn("1 close option", res.note)
        self.assertTrue(res.attention)

    def test_auto_takes_a_safe_one_and_says_how_it_differs(self):
        res, out = run(Track("Harbour Lights", "Mara Quill", duration=60.0), settings(close_match="auto"))
        self.assertEqual(res.status, "ok")
        self.assertIn("Close match: 1:15 longer", res.note)
        self.assertTrue(os.path.exists(os.path.join(out, "Harbour Lights - Mara Quill.mp3")))

    def test_auto_never_takes_a_labelled_one(self):
        self.options = [option(safe=False, why=("Live version",))]
        res, _ = run(Track("Harbour Lights", "Mara Quill", duration=60.0), settings(close_match="auto"))
        self.assertEqual(res.status, "no-file")
        self.assertEqual(len(res.close), 1, "left for the user to pick")

    def test_off_does_not_look(self):
        res, _ = run(Track("Harbour Lights", "Mara Quill", duration=60.0), settings(close_match="skip"))
        self.assertEqual((res.status, res.close, self.asked), ("no-file", [], []))

    def test_without_youtube_there_is_nothing_to_look_through(self):
        res, _ = run(Track("Harbour Lights", "Mara Quill", duration=60.0), settings(youtube=False))
        self.assertEqual(self.asked, [])

    def test_a_recording_the_user_picked_is_accepted_whatever_its_length(self):
        track = Track("Harbour Lights", "Mara Quill", duration=200.0, url=SRV.url + "song.mp3", extra={"picked": True})
        st = settings()
        saved = sources.fetch
        sources.fetch = lambda cand, tmpdir, stop: saved(dict(cand, source="archive.org", ext=".mp3", kbps=128), tmpdir, stop)
        try:
            res, _ = run(track, st)
        finally:
            sources.fetch = saved
        self.assertEqual(res.status, "ok", res.note)

    def test_settings_value_is_checked(self):
        st = Settings()
        st.close_match = "sometimes"
        self.assertEqual(st.clamp().close_match, "ask")


if __name__ == "__main__":
    unittest.main()
