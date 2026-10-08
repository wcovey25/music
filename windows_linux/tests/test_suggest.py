"""The source bar as a search bar: suggestions from a fake Deezer (a local server), how they are ranked, the list on this
computer, what a pick turns into, and that being offline changes nothing."""
import http.server
import json
import os
import socketserver
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from helpers import dead_url, fresh_dir

from musicdl.core import netio
from musicdl.ingest import search, suggest
from musicdl.ingest.base import Ctx, ResolveError


def song(i, title, artist, rank=500_000, album="Album", **kw):
    return {"id": i, "title": title, "artist": {"name": artist}, "rank": rank, "duration": 200,
            "album": {"title": album, "cover_medium": "", "cover_xl": "", "release_date": "2015-10-23"}, **kw}


class FakeDeezer:
    """Answers the handful of Deezer endpoints the suggestions use."""

    def __init__(self):
        self.asked = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                outer.asked.append((u.path, q))
                body = json.dumps(outer.answer(u.path, q)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def answer(self, path, q):
        text = q.get("q", "")
        if path == "/search":
            if text.startswith('artist:"adele"'):
                return {"data": [song(1, "Hello", "Adele", 950_000, "25")]}
            if "hello" in text.lower():
                return {"data": [song(9, "Hello (Reggae Cover)", "Cover Band", 300_000),
                                 song(8, "Hello I'm Adele", "Somebody", 200_000),
                                 song(7, "Hello (Karaoke Version)", "Sing Along", 250_000),
                                 song(1, "Hello", "Adele", 950_000, "25")]}
            return {"data": []}
        if path == "/search/album":
            if "hello" in text.lower():
                return {"data": [{"id": 5, "title": "25", "artist": {"name": "Adele"}, "nb_tracks": 11, "nb_fan": 100_000}]}
            return {"data": []}
        if path == "/search/artist":
            if "adele" in text.lower():
                return {"data": [{"id": 3, "name": "Adele", "nb_fan": 5_000_000}]}
            return {"data": []}
        if path == "/search/playlist":
            if "chill" in text.lower():
                return {"data": [{"id": 11, "title": "Chill Evenings", "nb_tracks": 450, "user": {"name": "Deezer"}},
                                 {"id": 12, "title": "Chill tiny", "nb_tracks": 2, "user": {"name": "x"}}]}
            return {"data": []}
        if path == "/genre":
            return {"data": [{"id": 152, "name": "Rock"}, {"id": 129, "name": "Jazz"}]}
        if path == "/artist/3/top":
            return {"data": [song(100 + i, f"Top {i}", "Adele") for i in range(10)], "total": 10}
        if path == "/playlist/11/tracks":
            start, n = int(q.get("index", 0)), int(q.get("limit", 25))
            rows = [song(1000 + i, f"Chill {i}", "Various") for i in range(start, min(450, start + n))]
            return {"data": rows, "total": 450}
        if path == "/chart/152/tracks":
            return {"data": [song(2000 + i, f"Rock {i}", "Band") for i in range(30)], "total": 30}
        if path == "/album/5":
            return {"id": 5, "title": "25", "artist": {"name": "Adele"}, "cover_xl": "", "release_date": "2015-11-20",
                    "tracks": {"data": [song(300 + i, f"Track {i}", "Adele") for i in range(11)]}}
        if path.startswith("/chart/0/"):
            return {"data": []}
        return {"data": []}

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class SuggestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dz = FakeDeezer()
        cls.saved = (suggest.API, search.API)
        suggest.API = search.API = cls.dz.url

    @classmethod
    def tearDownClass(cls):
        suggest.API, search.API = cls.saved
        cls.dz.close()

    def setUp(self):
        netio.CACHE.clear()
        netio.reset_health()
        suggest._genres = None
        suggest.INDEX = suggest.LocalIndex()

    def test_the_real_song_beats_covers_and_lookalikes(self):
        got = suggest.search_all("adele hello")
        songs = [s for s in got if s.kind == "song"]
        self.assertEqual((songs[0].title, songs[0].subtitle), ("Hello", "Adele"))
        cover = next((i for i, s in enumerate(songs) if "Cover" in s.title), len(songs))
        karaoke = next((i for i, s in enumerate(songs) if "Karaoke" in s.title), len(songs))
        self.assertGreater(cover, 0)
        self.assertGreater(karaoke, 0)

    def test_a_typed_word_is_not_held_against_a_result(self):
        typed = suggest._typed("hello karaoke", {"hello"})
        self.assertEqual(suggest._alternate_penalty(typed, "Hello (Karaoke Version)"), 1.5,
                         "'version' was not typed")
        self.assertEqual(suggest._alternate_penalty(suggest._typed("hello karaoke version", {"hello"}),
                                                    "Hello (Karaoke Version)"), 0.0)

    def test_every_kind_appears_and_the_list_is_short(self):
        got = suggest.search_all("adele hello")
        kinds = {s.kind for s in got}
        self.assertTrue({"song", "album", "artist"} <= kinds, kinds)
        self.assertLessEqual(len(got), suggest.SHOWN)
        for kind, cap in suggest.CAPS.items():
            self.assertLessEqual(sum(1 for s in got if s.kind == kind), cap)

    def test_a_genre_is_suggested_by_its_name(self):
        got = suggest.search_all("rock")
        self.assertIn(("genre", "Rock"), [(s.kind, s.title) for s in got])

    def test_small_playlists_are_left_out(self):
        got = suggest.search_all("chill")
        self.assertEqual([s.title for s in got if s.kind == "playlist"], ["Chill Evenings"])
        self.assertEqual(next(s for s in got if s.kind == "playlist").detail, "Deezer · 450 songs")

    def test_partial_lists_arrive_as_answers_come_in(self):
        seen = []
        final = suggest.search_all("adele hello", partial=seen.append)
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual([s.title for s in seen[-1]], [s.title for s in final])

    def test_too_short_to_ask(self):
        before = len(self.dz.asked)
        self.assertEqual(suggest.search_all("a"), [])
        self.assertEqual(len(self.dz.asked), before, "one letter is not worth a question")

    def test_what_was_seen_is_suggested_at_once_next_time(self):
        suggest.search_all("adele hello")
        got = suggest.instant("ade")
        self.assertIn(("artist", "Adele"), [(s.kind, s.title) for s in got], "no network needed for this list")

    def test_the_local_list_survives_a_restart(self):
        path = os.path.join(fresh_dir("suggest_index"), suggest.INDEX_FILE)
        idx = suggest.LocalIndex(path)
        idx.add([suggest.Suggestion("song", "Hello", "Adele", "1", data={"pop": 0.9})])
        idx.save()
        again = suggest.LocalIndex(path)
        again.load()
        self.assertEqual([(s.title, s.subtitle) for s in again.match("hel")], [("Hello", "Adele")])
        self.assertEqual(again.match("xyz"), [])

    def test_the_local_list_is_bounded(self):
        idx = suggest.LocalIndex()
        idx.add([suggest.Suggestion("song", f"Song {i}", "A", str(i)) for i in range(suggest.INDEX_MAX + 50)])
        self.assertEqual(len(idx.items), suggest.INDEX_MAX)

    # ---- picking
    def test_a_song_becomes_one_track(self):
        sg = next(s for s in suggest.search_all("adele hello") if s.kind == "song")
        col = suggest.collect(sg, Ctx())
        self.assertEqual((len(col.tracks), col.tracks[0].title, col.tracks[0].artist), (1, "Hello", "Adele"))
        self.assertEqual(sg.label, "Hello — Adele")

    def test_an_artist_becomes_their_top_songs(self):
        col = suggest.collect(suggest.Suggestion("artist", "Adele", "", "3"), Ctx())
        self.assertEqual((col.kind, len(col.tracks)), ("artist", 10))

    def test_a_playlist_is_read_in_pages_up_to_three_hundred(self):
        col = suggest.collect(suggest.Suggestion("playlist", "Chill Evenings", "Deezer", "11"), Ctx())
        self.assertEqual(len(col.tracks), suggest.PLAYLIST_MAX)
        self.assertEqual(col.notes, ["First 300 of 450 songs"])

    def test_a_genre_is_its_chart(self):
        col = suggest.collect(suggest.Suggestion("genre", "Rock", "", "152"), Ctx())
        self.assertEqual((col.title, len(col.tracks)), ("Top Rock", 30))

    def test_an_album_is_the_whole_album(self):
        col = suggest.collect(suggest.Suggestion("album", "25", "Adele", "5"), Ctx())
        self.assertEqual(len(col.tracks), 11)


class OfflineTests(unittest.TestCase):
    def setUp(self):
        netio.CACHE.clear()
        netio.reset_health()
        self.saved = suggest.API
        suggest.API = dead_url().rstrip("/")
        suggest._genres = None
        suggest.INDEX = suggest.LocalIndex()

    def tearDown(self):
        suggest.API = self.saved
        suggest._genres = None

    def test_offline_is_a_silent_no_op(self):
        self.assertEqual([s for s in suggest.search_all("adele hello") if s.kind != "genre"], [])
        self.assertEqual(suggest.genres(), list(suggest._GENRES), "the built-in genre list stands in")

    def test_offline_warm_up_is_harmless(self):
        suggest.warm()
        self.assertEqual(suggest.INDEX.items, {})


if __name__ == "__main__":
    unittest.main()
