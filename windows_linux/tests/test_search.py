"""Search tests: how songs are matched, scored and ranked, how many queries a song costs, and what the engine does with
the result. No network: YouTube and archive.org answers are canned fixtures."""
import os
import sys
import threading
import time
import types
import unittest

from helpers import fresh_dir

from musicdl.config import Settings
from musicdl.core import engine, netio, sources, text
from musicdl.core.models import EngineError, Track


def mkrc(title, artist, secs=240.0, **kw):
    t = Track(title, artist, duration=secs, **kw)
    rc = engine.Ctx(index=0, track=t, title=text.core_title(title), artist=text.first_artist(artist))
    rc.refs = [float(secs)] if secs else []
    rc.track_toks, rc.artist_toks = text.toks(rc.title), text.toks(rc.artist)
    return rc


def fits(rc, seconds, deep=False):
    return not rc.refs or any(abs(seconds - r) <= 0.15 * r for r in rc.refs)


def yt(title, secs, channel="SomeChannel", views=1_000_000, verified=False, vid=None, **kw):
    return {"id": vid or str(abs(hash((title, channel))) % 10 ** 11).zfill(11), "title": title, "duration": secs,
            "channel": channel, "view_count": views, "channel_is_verified": verified, **kw}


def score(rc, e, deep=False):
    return sources._yt_score(e, rc, fits, deep, 0.15)


class TextTests(unittest.TestCase):
    def test_apostrophes_do_not_split_words(self):
        for variant in ("Don't Stop Believin'", "Don’t Stop Believin’", "Dont Stop Believin"):
            self.assertEqual(text.norm(variant), "dont stop believin")
        self.assertEqual(text.norm_keep_parens("Gangsta's Paradise (Live)"), "gangstas paradise live")

    def test_number_words_are_numbers(self):
        self.assertEqual(text.toks("Four Minutes"), text.toks("4 Minutes"))
        self.assertEqual(text.toks("Seven Nation Army"), {"7", "nation", "army"})

    def test_fuzzy_overlap_forgives_small_slips_but_not_other_words(self):
        self.assertGreaterEqual(text.fuzzy_overlap({"hallelujah"}, {"halleluja"}), 0.8)
        self.assertGreaterEqual(text.fuzzy_overlap({"colour", "me"}, {"color", "me"}), 0.8)
        self.assertLess(text.fuzzy_overlap({"love"}, {"live"}), 0.5)
        self.assertLess(text.fuzzy_overlap({"lover"}, {"love"}), 0.8)
        self.assertEqual(text.fuzzy_overlap({"cat"}, {"cot"}), 0.0, "short words must match exactly")
        self.assertEqual(text.fuzzy_overlap(set(), {"x"}), 1.0)

    def test_artist_inside_a_channel_name(self):
        self.assertTrue(text.name_in("Ariana Grande", "ArianaGrandeVevo"))
        self.assertTrue(text.name_in("Jay-Z", "JAYZ Official"))
        self.assertFalse(text.name_in("Sia", "Siaaaa"), "three-letter names are only trusted as whole words")
        self.assertFalse(text.name_in("Queen", "Quest Records"))

    def test_title_fit(self):
        exact = text.title_fit("Bohemian Rhapsody", "Queen", "Queen - Bohemian Rhapsody (Official Video Remastered)")
        cover = text.title_fit("Bohemian Rhapsody", "Queen", "Queen - Bohemian Rhapsody Piano Cover by Someone")
        self.assertEqual(exact, 1.0)
        self.assertLess(cover, 0.7)
        self.assertLess(text.title_fit("Yesterday", "The Beatles", "Yesterday Once More"), 0.7)

    def test_title_fit_works_for_other_alphabets_too(self):
        self.assertEqual(text.title_fit("夜に駆ける", "YOASOBI", "YOASOBI - 夜に駆ける (Official Video)"), 1.0)
        self.assertLess(text.title_fit("夜に駆ける", "YOASOBI", "YOASOBI - 怪物"), 0.5)


class YouTubeScoreTests(unittest.TestCase):
    def test_curly_apostrophes_and_official_audio(self):
        rc = mkrc("Don't Stop Believin'", "Journey", 251)
        s = score(rc, yt("Journey - Don’t Stop Believin’ (Official Audio)", 251, "journey", 390_000_000, True))
        self.assertIsNotNone(s)
        self.assertGreater(s, sources.CONFIDENT, "an official upload of exactly the right length is convincing at once")

    def test_four_minutes_is_4_minutes(self):
        rc = mkrc("4 Minutes", "Madonna", 228)
        self.assertIsNotNone(score(rc, yt("Madonna - Four Minutes (Official Audio)", 228, "Madonna")))

    def test_the_artist_may_only_be_in_the_channel_name(self):
        rc = mkrc("7 rings", "Ariana Grande", 179)
        self.assertIsNotNone(score(rc, yt("7 rings (Official Video)", 179, "ArianaGrandeVevo", 900_000_000)))
        self.assertIsNone(score(rc, yt("7 rings (Official Video)", 179, "SomeoneElseVevo")),
                          "another artist's '7 rings' is not this song")

    def test_other_alphabets_and_accents(self):
        rc = mkrc("夜に駆ける", "YOASOBI", 261)
        self.assertIsNotNone(score(rc, yt("YOASOBI「夜に駆ける」 Official Music Video", 261, "Ayase / YOASOBI")))
        self.assertIsNone(score(rc, yt("YOASOBI - 怪物 (Official Video)", 261, "YOASOBI")), "another song of theirs")
        self.assertEqual(text.toks("Beyoncé"), text.toks("Beyonce"))
        rc = mkrc("Halo", "Beyoncé", 261)
        self.assertIsNotNone(score(rc, yt("Beyonce - Halo", 261, "BeyonceVEVO")))

    def test_a_small_spelling_slip_in_the_title_is_forgiven(self):
        rc = mkrc("Hallelujah", "Jeff Buckley", 413)
        self.assertIsNotNone(score(rc, yt("Jeff Buckley - Halleluja", 413, "Jeff Buckley - Topic")))

    def test_covers_karaoke_and_friends_are_never_the_song(self):
        rc = mkrc("Hallelujah", "Jeff Buckley", 413)
        for bad in ("Jeff Buckley - Hallelujah (Cover)", "Hallelujah - Jeff Buckley (Karaoke Version)",
                    "Hallelujah Jeff Buckley 8D", "Hallelujah Jeff Buckley piano tutorial", "Jeff Buckley Hallelujah Reaction"):
            self.assertIsNone(score(rc, yt(bad, 413)), bad)

    def test_a_live_take_is_allowed_but_ranks_below_the_studio_one(self):
        rc = mkrc("Creep", "Radiohead", 238)
        studio = score(rc, yt("Radiohead - Creep", 238, "Radiohead", 1_000_000_000, True))
        live = score(rc, yt("Radiohead - Creep (Live at Glastonbury)", 238, "Radiohead", 1_000_000_000, True))
        self.assertIsNotNone(live)
        self.assertGreater(studio - live, sources.BAND, "the live take falls out of the equally-good band")

    def test_acoustic_and_unplugged_versions_are_marked_down(self):
        rc = mkrc("Riptide", "Vance Joy", 204)
        studio = score(rc, yt("Vance Joy - Riptide (Official Video)", 204, "Vance Joy", 900_000_000, True))
        for other in ("Vance Joy - Riptide [Acoustic]", "Vance Joy - Riptide (Unplugged)", "Vance Joy - Riptide (Demo)"):
            s = score(rc, yt(other, 204, "Vance Joy", 900_000_000, True))
            self.assertGreater(studio - s, sources.BAND, other)
        wanted = mkrc("Riptide (Acoustic)", "Vance Joy", 204)
        self.assertGreater(score(wanted, yt("Vance Joy - Riptide (Acoustic)", 204, "Vance Joy", 900_000_000, True)), 12,
                           "…unless that is the version asked for")

    def test_length_that_matches_to_the_second_beats_one_that_is_merely_allowed(self):
        rc = mkrc("Fix You", "Coldplay", 295)
        exact = score(rc, yt("Coldplay - Fix You", 295, "Coldplay"))
        loose = score(rc, yt("Coldplay - Fix You", 320, "Coldplay"))
        self.assertGreater(exact - loose, 1.5)

    def test_wrong_length_is_rejected(self):
        rc = mkrc("Fix You", "Coldplay", 295)
        self.assertIsNone(score(rc, yt("Coldplay - Fix You", 3600, "Coldplay")))
        self.assertIsNone(score(rc, yt("Coldplay - Fix You", 295, "Coldplay", live_status="is_live")))

    def test_topic_channel_and_official_beat_a_fan_upload(self):
        rc = mkrc("Africa", "Toto", 295)
        topic = score(rc, yt("Africa", 295, "Toto - Topic", 100_000_000))
        fan = score(rc, yt("Toto - Africa lyrics", 295, "Some Fan", 2_000_000))
        self.assertGreater(topic, fan)

    def test_extra_words_in_the_heading_cost_a_little(self):
        rc = mkrc("Yesterday", "The Beatles", 125)
        plain = score(rc, yt("The Beatles - Yesterday", 125, "Fan"))
        padded = score(rc, yt("The Beatles - Yesterday guitar chords strumming pattern easy", 125, "Fan"))
        self.assertGreater(plain, padded)

    def test_another_song_with_the_same_title_is_not_accepted_without_the_artist(self):
        rc = mkrc("Yesterday", "The Beatles", 125)
        self.assertIsNone(score(rc, yt("Yesterday - Boyz II Men", 125, "Boyz II Men")))


class QueryTests(unittest.TestCase):
    """How many YouTube queries does a song cost?"""

    class Fake:
        def __init__(self, answers):
            self.answers, self.asked = answers, []

        def extract_info(self, q, download=False):
            self.asked.append(q)
            ans = self.answers[min(len(self.asked), len(self.answers)) - 1]
            if isinstance(ans, Exception):
                raise ans
            return {"entries": ans}

    def setUp(self):
        self._ydl = sources._ydl
        self.stop = threading.Event()

    def tearDown(self):
        sources._ydl = self._ydl

    def use(self, *answers):
        fake = self.Fake(answers)
        sources._ydl = lambda kind: fake
        return fake

    def find(self, rc):
        return sources.youtube_candidates(rc, self.stop, fits, False, 0.15)

    def test_one_query_when_the_first_answer_is_convincing(self):
        rc = mkrc("Fix You", "Coldplay", 295)
        fake = self.use([yt("Coldplay - Fix You (Official Audio)", 295, "Coldplay", 800_000_000, True, vid="aaaaaaaaaaa")])
        got = self.find(rc)
        self.assertEqual(len(fake.asked), 1)
        self.assertEqual(got[0]["vid"], "aaaaaaaaaaa")

    def test_more_queries_only_while_nothing_convincing_has_turned_up(self):
        rc = mkrc("Fix You", "Coldplay", 295)
        fake = self.use([yt("Some video about Coldplay", 295)],
                        [yt("Coldplay - Fix You", 312, "Some Fan", 50_000)],
                        [yt("Coldplay - Fix You (Official Audio)", 295, "Coldplay", 800_000_000, True, vid="bbbbbbbbbbb")])
        got = self.find(rc)
        self.assertEqual(len(fake.asked), 3)
        self.assertEqual(got[0]["vid"], "bbbbbbbbbbb", "the later, better hit comes first")
        self.assertIn("official audio", fake.asked[1])
        self.assertIn("topic", fake.asked[2])

    def test_a_good_name_with_the_wrong_length_is_not_convincing(self):
        rc = mkrc("Riptide", "Vance Joy", 204)
        fake = self.use([yt("Vance Joy - Riptide (Official Video)", 188, "Vance Joy", 900_000_000, True, vid="aaaaaaaaaaa")],
                        [yt("Vance Joy - Riptide (Official Audio)", 204, "Vance Joy", 900_000_000, True, vid="bbbbbbbbbbb")])
        got = self.find(rc)
        self.assertEqual(len(fake.asked), 2, "a cut 16 s shorter than the song is another cut: keep looking")
        self.assertEqual(got[0]["vid"], "bbbbbbbbbbb")

    def test_a_fixed_number_of_queries_at_most(self):
        rc = mkrc("Fix You", "Coldplay", 295)
        fake = self.use([], [], [], [])
        self.assertEqual(self.find(rc), [])
        self.assertEqual(len(fake.asked), 3)

    def test_two_failing_queries_end_the_search(self):
        rc = mkrc("Fix You", "Coldplay", 295)
        fake = self.use(RuntimeError("HTTP 429"), RuntimeError("HTTP 429"), [])
        self.assertEqual(self.find(rc), [])
        self.assertEqual(len(fake.asked), 2)

    def test_asking_again_does_not_repeat_queries(self):
        rc = mkrc("Fix You", "Coldplay", 295)
        fake = self.use([yt("Coldplay - Fix You (Official Audio)", 295, "Coldplay", 800_000_000, True)])
        self.find(rc)
        self.find(rc)
        self.assertEqual(len(fake.asked), 1)

    def test_a_gone_video_is_left_out_once_it_is_known(self):
        rc = mkrc("Fix You", "Coldplay", 295)
        self.use([yt("Coldplay - Fix You (Official Audio)", 295, "Coldplay", 800_000_000, True, vid="aaaaaaaaaaa"),
                  yt("Coldplay - Fix You", 295, "Coldplay", 5_000_000, vid="bbbbbbbbbbb")])
        first = self.find(rc)
        sources._ydl = lambda kind: types.SimpleNamespace(
            extract_info=lambda url, download=False: (_ for _ in ()).throw(RuntimeError("ERROR: Video unavailable")))
        sources.probe(first[0], self.stop, rc.raw["probe"])
        self.assertTrue(first[0]["dead"])
        self.assertEqual([c["vid"] for c in self.find(rc)], ["bbbbbbbbbbb"])

    def test_the_probe_remembers_what_the_site_offers(self):
        rc = mkrc("Fix You", "Coldplay", 295)
        self.use([yt("Coldplay - Fix You (Official Audio)", 295, "Coldplay", 800_000_000, True, vid="aaaaaaaaaaa")])
        c = self.find(rc)[0]
        info = {"abr": 130.0, "acodec": "opus", "formats": [{"format_id": "251"}]}
        sources._ydl = lambda kind: types.SimpleNamespace(extract_info=lambda url, download=False: info)
        sources.probe(c, self.stop, rc.raw.setdefault("probe", {}))
        again = self.find(rc)[0]
        self.assertEqual((again["kbps"], again["probed"], again["_info"]), (195, True, info),
                         "a rebuilt candidate list keeps the probe, so nothing is asked twice")


class ArchiveTests(unittest.TestCase):
    def files(self, *rows):
        return [{"name": n, "length": str(sec), "size": str(size), "title": t, "source": "original", **extra}
                for n, sec, size, t, extra in rows]

    def test_a_drum_stem_in_an_item_named_after_the_song_is_not_the_song(self):
        rc = mkrc("Bohemian Rhapsody", "Queen", 355)
        item = {"identifier": "queen-multitracks", "title": "Queen - Bohemian Rhapsody (multitrack)", "creator": "Queen"}
        got = sources._archive_files(rc, item, self.files(("001_Kick_01.wav", 355, 50_000_000, "", {}),
                                                         ("002_Snare_01.wav", 355, 50_000_000, "", {}),
                                                         ("010_Vocal.wav", 355, 50_000_000, "", {})))
        self.assertEqual(got, [])

    def test_a_single_file_item_named_after_the_song_is(self):
        rc = mkrc("HUMBLE.", "Kendrick Lamar", 177)
        item = {"identifier": "humble-kl", "title": "HUMBLE.", "creator": "Kendrick Lamar"}
        got = sources._archive_files(rc, item, self.files(("Humble.mp3", 177, 7_000_000, "", {"bitrate": "320"})))
        self.assertEqual(len(got), 1)
        self.assertEqual((got[0]["kbps"], got[0]["lossless"]), (320, False))

    def test_a_track_in_an_album_item_is_found_by_its_own_name(self):
        rc = mkrc("Africa", "Toto", 295)
        item = {"identifier": "toto-iv", "title": "Toto IV", "creator": "Toto"}
        got = sources._archive_files(rc, item, self.files(("04 - Africa.flac", 295, 30_000_000, "Africa", {}),
                                                         ("05 - Rosanna.flac", 330, 33_000_000, "Rosanna", {})))
        self.assertEqual([c["title"] for c in got], ["Africa"])
        self.assertTrue(got[0]["lossless"])

    def test_a_wav_made_from_a_video_is_not_lossless(self):
        rc = mkrc("Smells Like Teen Spirit", "Nirvana", 278)
        item = {"identifier": "nirvana-sltS", "title": "Nirvana", "creator": "Nirvana"}
        name = "Nirvana - Smells Like Teen Spirit (Official Music Video) (online-audio-converter.com).wav"
        got = sources._archive_files(rc, item, self.files((name, 278, 49_000_000, "", {})))
        self.assertEqual(len(got), 1)
        self.assertFalse(got[0]["lossless"])
        self.assertTrue(got[0]["ripped"])
        self.assertLessEqual(got[0]["kbps"], sources.RIPPED_KBPS)

    def test_a_file_named_like_a_video_id_is_a_rip_too(self):
        rc = mkrc("Levitating", "Dua Lipa", 203)
        item = {"identifier": "dua", "title": "Dua Lipa - Levitating", "creator": "Dua Lipa"}
        got = sources._archive_files(rc, item, self.files(("-3gkan9wSaQ.m4a", 203, 5_000_000, "Levitating", {})))
        self.assertTrue(got and got[0]["ripped"])

    def test_listings_are_read_side_by_side(self):
        rc = mkrc("Hotel California", "Eagles", 391)
        docs = [{"identifier": f"eagles-{i}", "title": "Eagles - Hotel California", "creator": "Eagles"} for i in range(8)]
        calls, real = [], netio.get_json

        def fake(url, params=None, **kw):
            if "advancedsearch" in url:
                return {"response": {"docs": docs}}
            time.sleep(0.3)
            calls.append(url)
            return {"files": [{"name": "Hotel California.mp3", "length": "391", "size": "9000000", "source": "original"}]}
        netio.get_json = fake
        try:
            t0 = time.monotonic()
            out = sources._archive_search(rc, threading.Event(), None, False)
            took = time.monotonic() - t0
        finally:
            netio.get_json = real
        self.assertEqual(len(calls), 8)
        self.assertEqual(len(out), 8)
        self.assertLess(took, 8 * 0.3 * 0.6, f"8 listings of 0.3 s each took {took:.1f}s: they should overlap")

    def test_the_search_asks_for_the_artist_first_and_widens_only_if_empty(self):
        rc = mkrc("Hotel California", "Eagles", 391)
        qs, real = [], netio.get_json

        def fake(url, params=None, **kw):
            qs.append(params["q"])
            return {"response": {"docs": []}}
        netio.get_json = fake
        try:
            self.assertEqual(sources._archive_search(rc, threading.Event(), None, False), [])
        finally:
            netio.get_json = real
        self.assertEqual(len(qs), 2)
        self.assertIn("creator", qs[0])
        self.assertNotIn("creator", qs[1])


class RankingTests(unittest.TestCase):
    def job(self, preset="good"):
        st = Settings()
        st.preset, st.device = preset, "other"
        return engine.Job([], fresh_dir("rank_out"), st)

    def c(self, source, score, kbps, lossless=False, cid=None):
        return {"source": source, "id": cid or f"{source}{score}{kbps}", "kbps": kbps, "lossless": lossless, "score": score,
                "seconds": 240.0, "title": "t"}

    def test_among_equal_matches_the_better_audio_first(self):
        j = self.job("better")
        a, b = self.c("youtube", 14.0, 195), self.c("archive.org", 13.0, 320)
        self.assertEqual(j._rank([a, b])[0], b)

    def test_quality_beyond_what_the_output_can_hold_does_not_count(self):
        j = self.job("good")                           # MP3 192: nothing above 192 is of any use
        a, b = self.c("youtube", 14.0, 195), self.c("archive.org", 13.0, 320)
        self.assertEqual(j._rank([b, a])[0], a, "320 kbps is no better for a 192 kbps file: the better match wins")

    def test_a_poorer_match_never_wins_on_bitrate(self):
        j = self.job("best")
        studio, live = self.c("youtube", 15.0, 195), self.c("archive.org", 15.0 - 3.5, 1411, lossless=True)
        self.assertEqual(j._rank([live, studio])[0], studio)

    def test_lossless_wins_among_equal_matches_when_lossless_is_wanted(self):
        j = self.job("best")
        flac, tube = self.c("archive.org", 13.5, 1411, True), self.c("youtube", 14.5, 195)
        self.assertEqual(j._rank([tube, flac])[0], flac)

    def test_empty(self):
        self.assertEqual(self.job()._rank([]), [])


if __name__ == "__main__":
    unittest.main()
