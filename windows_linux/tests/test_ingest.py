"""Link resolvers: parsers run against saved real pages (tests/fixtures); the network is faked."""
import csv
import json
import os
import threading
import unittest
from unittest import mock

from helpers import fresh_dir

from musicdl import ingest
from musicdl.core import netio
from musicdl.ingest import amazon, apple, generic, pandora, search, sheet, spotify, youtube
from musicdl.ingest.base import Ctx, ResolveError
from musicdl.ingest.clean import clean_title, split_video_title

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def fixture(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as fh:
        return fh.read()


def spotify_html(name):
    return '<html><script id="__NEXT_DATA__" type="application/json">%s</script></html>' % fixture(name)


class SpotifyTests(unittest.TestCase):
    def test_link_forms(self):
        for text, want in [
            ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
            ("https://open.spotify.com/intl-de/album/0ETFjACtuP2ADo6LFhL6HN", ("album", "0ETFjACtuP2ADo6LFhL6HN")),
            ("open.spotify.com/embed/track/4cOdK2wGLETKBW3PvgPWqT", ("track", "4cOdK2wGLETKBW3PvgPWqT")),
            ("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", ("playlist", "37i9dQZF1DXcBWIGoYBM5M")),
            ("https://open.spotify.com/user/someone/playlist/3cEYpjA9oz9GiPac4AsH4n", ("playlist", "3cEYpjA9oz9GiPac4AsH4n")),
        ]:
            self.assertEqual(spotify.match(text), want, text)
        self.assertIsNone(spotify.match("https://example.com/playlist/xyz"))

    def test_playlist(self):
        col = spotify.parse(spotify_html("spotify_playlist.json"), "playlist")
        self.assertEqual((col.title, col.kind, col.service), ("Today’s Top Hits", "playlist", "spotify"))
        self.assertEqual(len(col.tracks), 5)
        t = col.tracks[0]
        self.assertEqual((t.title, t.artist), ("Patient Zero", "Taylor Swift"))
        self.assertAlmostEqual(t.duration, 225.868, places=2)
        self.assertTrue(col.artwork.startswith("https://") and "ab67706f" in col.artwork)
        self.assertEqual(t.artwork, "")                     # playlist art is not each song's cover
        self.assertEqual(col.notes, [])

    def test_playlist_cap_is_reported(self):
        data = json.loads(fixture("spotify_playlist.json"))
        ent = data["props"]["pageProps"]["state"]["data"]["entity"]
        ent["trackList"] = [dict(ent["trackList"][0], uri=f"spotify:track:{i:022d}", title=f"S{i}") for i in range(100)]
        col = spotify.parse('<script id="__NEXT_DATA__">%s</script>' % json.dumps(data), "playlist")
        self.assertEqual(len(col.tracks), 100)
        self.assertTrue(col.notes and "100" in col.notes[0])

    def test_album_carries_album_data(self):
        col = spotify.parse(spotify_html("spotify_album.json"), "album")
        self.assertEqual(col.kind, "album")
        self.assertTrue(all(t.album == "Abbey Road (Remastered)" for t in col.tracks))
        self.assertEqual([t.track_no for t in col.tracks], [1, 2, 3, 4, 5])
        self.assertEqual(col.tracks[0].artist, "The Beatles")
        self.assertIn("b273", col.artwork)                    # the 640 px version

    def test_single_track(self):
        col = spotify.parse(spotify_html("spotify_track.json"), "track")
        self.assertEqual(col.kind, "track")
        t = col.tracks[0]
        self.assertEqual((t.title, t.artist, t.year), ("Never Gonna Give You Up", "Rick Astley", "1987"))
        self.assertAlmostEqual(t.duration, 213.573, places=2)

    def test_unreadable_page(self):
        with self.assertRaises(ResolveError):
            spotify.parse("<html>nothing here</html>", "playlist")


class AppleTests(unittest.TestCase):
    def test_album(self):
        col = apple.parse(fixture("apple_album.html"))
        self.assertEqual((col.title, col.kind, col.subtitle), ("Abbey Road (Remastered)", "album", "The Beatles"))
        self.assertEqual(len(col.tracks), 5)                  # the saved page is trimmed to five songs
        t = col.tracks[0]
        self.assertEqual((t.title, t.album, t.year, t.genre, t.track_no), ("Come Together", "Abbey Road (Remastered)",
                                                                           "1969", "Rock", 1))
        self.assertAlmostEqual(t.duration, 258.947, places=2)
        self.assertNotIn("{", col.artwork)
        self.assertIn("1200x1200", col.artwork)
        self.assertEqual(t.artwork, col.artwork)
        self.assertTrue(all(x.artist for x in col.tracks))

    def test_playlist_has_per_track_art_and_album(self):
        col = apple.parse(fixture("apple_playlist.html"))
        self.assertEqual((col.title, col.kind), ("Today’s Hits", "playlist"))
        self.assertEqual(len(col.tracks), 5)
        t = col.tracks[0]
        self.assertEqual((t.title, t.album), ("Solar Eclipse", "HABIBTI (FOMO)"))
        self.assertIn("Drake", t.artist)
        self.assertTrue(t.artwork.startswith("https://") and "{" not in t.artwork)
        self.assertNotEqual(t.artwork, col.artwork)

    def test_song_picked_out_of_album(self):
        col = apple.parse(fixture("apple_album.html"), only_song="1441164430")
        self.assertEqual((col.kind, len(col.tracks), col.tracks[0].title), ("track", 1, "Come Together"))
        with self.assertRaises(ResolveError):
            apple.parse(fixture("apple_album.html"), only_song="999")

    def test_links(self):
        self.assertTrue(apple.match("https://music.apple.com/us/album/abbey-road-remastered/1441164426"))
        self.assertTrue(apple.match("https://music.apple.com/gb/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb"))
        self.assertTrue(apple.match("https://apple.co/3xyz"))
        self.assertFalse(apple.match("https://example.com/album/1"))

    def test_station_is_explained(self):
        with self.assertRaises(ResolveError) as cm:
            apple.resolve("https://music.apple.com/us/station/some-station/ra.123", Ctx())
        self.assertIn("station", str(cm.exception).lower())

    def test_resolve_uses_song_param(self):
        with mock.patch.object(apple, "page", return_value=fixture("apple_album.html")) as pg:
            col = apple.resolve("https://music.apple.com/us/album/come-together/1441164426?i=1441164430", Ctx())
        self.assertEqual(col.tracks[0].title, "Come Together")
        self.assertEqual(pg.call_args[0][0], "https://music.apple.com/us/album/come-together/1441164426")


class PandoraTests(unittest.TestCase):
    def test_album(self):
        col = pandora.parse(fixture("pandora_album.html"), "https://www.pandora.com/artist/the-beatles/abbey-road-remastered/ALnVpzZh7zwhn3k")
        self.assertEqual((col.title, col.kind, col.subtitle), ("Abbey Road (Remastered)", "album", "The Beatles"))
        self.assertEqual(len(col.tracks), 17)
        self.assertEqual([t.track_no for t in col.tracks], list(range(1, 18)))
        t = col.tracks[0]
        self.assertTrue(t.title.startswith("Come Together"))
        self.assertEqual((t.artist, t.album, t.year), ("The Beatles", "Abbey Road (Remastered)", "1969"))
        self.assertTrue(t.isrc and t.duration > 100)
        self.assertIn("1080W_1080H", col.artwork)
        self.assertTrue(col.artwork.startswith("https://content-images.p-cdn.com/"))

    def test_region_block_is_explained(self):
        page = "<html><body><h1>Pandora is unavailable in this country or region</h1></body></html>"
        with mock.patch.object(pandora, "page", return_value=page):
            with self.assertRaises(ResolveError) as cm:
                pandora.resolve("https://www.pandora.com/artist/x/AR1", Ctx())
        self.assertIn("United States", str(cm.exception))

    def test_private_station(self):
        with mock.patch.object(pandora, "page", return_value="<html>login</html>"), \
                mock.patch.object(pandora.browser, "render", side_effect=ResolveError("no browser")):
            with self.assertRaises(ResolveError) as cm:
                pandora.resolve("https://www.pandora.com/station/12345", Ctx())
        self.assertIn("account", str(cm.exception))

    def test_album_page_end_to_end(self):
        with mock.patch.object(pandora, "page", return_value=fixture("pandora_album.html")):
            col = pandora.resolve("https://www.pandora.com/artist/the-beatles/abbey-road-remastered/ALnVpzZh7zwhn3k", Ctx())
        self.assertEqual(len(col.tracks), 17)


class AmazonTests(unittest.TestCase):
    def test_rendered_album(self):
        col = amazon.parse(fixture("amazon_album.html"))
        self.assertEqual((col.title, col.subtitle, col.kind), ("The Day", "Babyface", "album"))
        self.assertEqual(len(col.tracks), 13)
        t = col.tracks[0]
        self.assertEqual((t.title, t.artist, t.album, t.year, t.track_no),
                         ("Every Time I Close My Eyes", "Babyface feat. Kenny G", "The Day", "1996", 1))
        self.assertEqual(col.tracks[1].artist, "Babyface")     # empty credit falls back to the album artist
        self.assertTrue(t.extra["asin"].startswith("B00"))
        self.assertTrue(col.artwork.startswith("https://m.media-amazon.com/"))
        self.assertEqual(col.notes, [])

    def test_missing_rows_are_reported(self):
        html = fixture("amazon_album.html")
        short = html.replace("13 SONGS", "20 SONGS")
        col = amazon.parse(short)
        self.assertTrue(col.notes and "13 of 20" in col.notes[0])

    def test_no_browser_message(self):
        with mock.patch("musicdl.platform_.browser_candidates", return_value=[]):
            with self.assertRaises(ResolveError) as cm:
                amazon.resolve("https://music.amazon.com/albums/B00136LK9S", Ctx())
        self.assertIn("browser", str(cm.exception))

    def test_shopping_link_is_rejected(self):
        with self.assertRaises(ResolveError):
            amazon.resolve("https://www.amazon.com/dp/B00136LK9S", Ctx())


class TitleCleaningTests(unittest.TestCase):
    def test_video_titles(self):
        self.assertEqual(split_video_title("Rick Astley - Never Gonna Give You Up (Official Video)", "Rick Astley"),
                         ("Rick Astley", "Never Gonna Give You Up"))
        self.assertEqual(split_video_title("Do I Wanna Know?", "Arctic Monkeys - Topic"), ("Arctic Monkeys", "Do I Wanna Know?"))
        self.assertEqual(split_video_title("Bad Guy [Official Audio]", "Billie Eilish"), ("Billie Eilish", "Bad Guy"))
        self.assertEqual(split_video_title("Adele - Skyfall (Lyric Video) | HD", "AdeleVEVO"), ("Adele", "Skyfall"))

    def test_keeps_meaningful_brackets(self):
        self.assertEqual(clean_title("Hey Jude (Remastered 2015)"), "Hey Jude (Remastered 2015)")
        self.assertEqual(clean_title("Song (Live at Wembley) (Official Video)"), "Song (Live at Wembley)")


class YouTubeTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(youtube.normalize("https://youtu.be/dQw4w9WgXcQ?t=5"),
                         ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", False))
        self.assertEqual(youtube.normalize("https://www.youtube.com/watch?v=abc12345678&list=PLxyz")[0],
                         "https://www.youtube.com/playlist?list=PLxyz")
        self.assertEqual(youtube.normalize("https://www.youtube.com/watch?v=abc12345678&list=RDabc12345678")[0],
                         "https://www.youtube.com/watch?v=abc12345678")           # endless auto-mix: take the video
        self.assertEqual(youtube.normalize("https://music.youtube.com/playlist?list=OLAK5uy_abc")[0],
                         "https://music.youtube.com/playlist?list=OLAK5uy_abc")

    def test_music_album_playlist(self):
        info = {"_type": "playlist", "id": "OLAK5uy_xyz", "title": "Album - AM", "uploader": "Arctic Monkeys",
                "thumbnails": [{"url": "a.jpg", "width": 100, "height": 100}, {"url": "big.jpg", "width": 1200, "height": 1200}],
                "entries": [{"id": "aaaaaaaaaaa", "title": "Do I Wanna Know?", "channel": "Arctic Monkeys", "duration": 272},
                            {"id": "bbbbbbbbbbb", "title": "R U Mine?", "channel": "Arctic Monkeys", "duration": 201},
                            {"id": "ccccccccccc", "title": "[Private video]", "channel": None}]}
        col = youtube.to_collection(info, True)
        self.assertEqual((col.title, col.kind, col.service, len(col.tracks)), ("AM", "album", "ytmusic", 2))
        t = col.tracks[0]
        self.assertEqual((t.title, t.artist, t.album, t.track_no, t.duration), ("Do I Wanna Know?", "Arctic Monkeys", "AM", 1, 272))
        self.assertEqual(t.url, "https://www.youtube.com/watch?v=aaaaaaaaaaa")
        self.assertEqual(t.artwork, "big.jpg")
        self.assertTrue(col.notes and "1 private" in col.notes[0])

    def test_video_playlist_splits_titles(self):
        info = {"_type": "playlist", "id": "PL1", "title": "Road trip", "uploader": "Sam",
                "entries": [{"id": "aaaaaaaaaaa", "title": "Queen - Don't Stop Me Now (Official Video)", "channel": "Queen Official", "duration": 211}]}
        col = youtube.to_collection(info, False)
        self.assertEqual((col.kind, col.tracks[0].artist, col.tracks[0].title), ("playlist", "Queen", "Don't Stop Me Now"))
        self.assertEqual(col.tracks[0].artwork, "")

    def test_single_video(self):
        info = {"id": "dQw4w9WgXcQ", "title": "Rick Astley - Never Gonna Give You Up", "channel": "Rick Astley",
                "duration": 213, "webpage_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"}
        col = youtube.to_collection(info, False)
        self.assertEqual((col.kind, col.tracks[0].artist, col.tracks[0].title), ("track", "Rick Astley", "Never Gonna Give You Up"))
        self.assertEqual(col.tracks[0].url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    def test_channel_tabs_use_videos_tab(self):
        info = {"_type": "playlist", "id": "UC1", "title": "Chan", "entries": [
            {"_type": "playlist", "title": "Chan - Shorts", "entries": [{"id": "sssssssssss", "title": "short"}]},
            {"_type": "playlist", "title": "Chan - Videos", "entries": [{"id": "vvvvvvvvvvv", "title": "A - B", "duration": 100}]}]}
        col = youtube.to_collection(info, False)
        self.assertEqual([t.title for t in col.tracks], ["B"])

    def test_friendly_errors(self):
        self.assertIn("private", youtube._friendly(Exception("ERROR: [youtube] abc: Private video. Sign in")).lower())
        self.assertIn("isn’t one", youtube._friendly(Exception("ERROR: Unsupported URL: http://x")))


class GenericTests(unittest.TestCase):
    def test_durations(self):
        self.assertEqual(generic.iso_seconds("PT3M45S"), 225)
        self.assertEqual(generic.iso_seconds("PT1H2M"), 3720)
        self.assertEqual(generic.iso_seconds("3:41"), 221)
        self.assertEqual(generic.iso_seconds(None), 0)

    def test_jsonld_album(self):
        html = '<script type="application/ld+json">%s</script>' % json.dumps({
            "@type": "MusicAlbum", "name": "Blue", "byArtist": {"@type": "MusicGroup", "name": "Joni Mitchell"},
            "image": "https://x/y.jpg", "track": [
                {"@type": "MusicRecording", "name": "All I Want", "duration": "PT3M32S"},
                {"@type": "MusicRecording", "name": "River", "byArtist": {"name": "Joni Mitchell"}}]})
        col = generic.from_jsonld(html)
        self.assertEqual((col.title, col.kind, len(col.tracks)), ("Blue", "album", 2))
        self.assertEqual((col.tracks[0].artist, col.tracks[0].duration, col.tracks[1].track_no), ("Joni Mitchell", 212, 2))

    def test_direct_audio_file(self):
        col = generic.resolve("https://example.com/files/Daft%20Punk%20-%20Around%20the%20World.mp3", Ctx())
        t = col.tracks[0]
        self.assertEqual((t.artist, t.title), ("Daft Punk", "Around the World"))
        self.assertTrue(t.direct)

    def test_non_music_page_is_a_clear_error(self):
        with mock.patch.object(generic.youtube, "extract", side_effect=ResolveError("Unsupported URL")), \
                mock.patch.object(generic, "page", return_value="<html><title>Recipe</title></html>"):
            with self.assertRaises(ResolveError) as cm:
                generic.resolve("https://example.com/recipes", Ctx())
        self.assertIn("No songs", str(cm.exception))


class SheetTests(unittest.TestCase):
    def test_csv(self):
        d = fresh_dir("sheets")
        path = os.path.join(d, "My Songs.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Track", "Artist", "Album", "mb_recording_id", "mb_date"])
            w.writerow(["Yesterday", "The Beatles", "Help!", "abc-123", "1965-08-06"])
            w.writerow(["", "Nobody", "", "", ""])
            w.writerow(["Hey Jude", "The Beatles", "", "", ""])
        col = sheet.load(path)
        self.assertEqual((col.title, col.kind, len(col.tracks)), ("My Songs", "sheet", 2))
        self.assertEqual((col.tracks[0].mbid, col.tracks[0].year, col.tracks[0].album), ("abc-123", "1965", "Help!"))

    def test_wrong_columns(self):
        d = fresh_dir("sheets")
        path = os.path.join(d, "bad.csv")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("foo,bar\n1,2\n")
        with self.assertRaises(ResolveError):
            sheet.load(path)


class FakeDeezer:
    """Stands in for api.deezer.com."""

    def __init__(self):
        self.calls = []

    def __call__(self, url, params=None, **kw):
        self.calls.append((url, params))
        path = url.split("api.deezer.com", 1)[1]
        if path == "/search":
            q = (params or {}).get("q", "")
            if "Nonexistent" in q:
                return {"data": []}
            return {"data": [{"title": "Skyfall", "duration": 286, "artist": {"name": "Adele"},
                              "album": {"title": "Skyfall", "cover_xl": "https://cdn/skyfall.jpg", "release_date": "2012-10-05"}}]}
        if path == "/search/album":
            return {"data": [{"id": 7, "title": "Abbey Road", "artist": {"name": "The Beatles"}}]}
        if path == "/album/7":
            return {"title": "Abbey Road", "artist": {"name": "The Beatles"}, "cover_xl": "https://cdn/ar.jpg",
                    "release_date": "1969-09-26", "genres": {"data": [{"name": "Rock"}]},
                    "tracks": {"data": [{"title": "Come Together", "duration": 259, "artist": {"name": "The Beatles"}},
                                        {"title": "Something", "duration": 182, "artist": {"name": "The Beatles"}}]}}
        if path == "/search/artist":
            return {"data": [{"id": 1, "name": "Radiohead"}]}
        if path == "/artist/1/top":
            return {"data": [{"title": f"R{i}", "duration": 200, "artist": {"name": "Radiohead"}, "album": {}} for i in range(3)]}
        if path == "/artist/1/related":
            return {"data": [{"id": 2, "name": "Muse"}, {"id": 3, "name": "Coldplay"}]}
        if path in ("/artist/2/top", "/artist/3/top"):
            n = "Muse" if path.startswith("/artist/2") else "Coldplay"
            return {"data": [{"title": f"{n}{i}", "duration": 200, "artist": {"name": n}, "album": {}} for i in range(2)]}
        return None


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeDeezer()
        p = mock.patch.object(search.netio, "get_json", self.fake)
        p.start()
        self.addCleanup(p.stop)

    def test_artist_dash_song(self):
        col = search.resolve_text("Adele - Skyfall", Ctx())
        t = col.tracks[0]
        self.assertEqual((col.kind, t.title, t.artist, t.album, t.year), ("track", "Skyfall", "Adele", "Skyfall", "2012"))

    def test_album_text(self):
        col = search.resolve_text("abbey road beatles", Ctx())
        self.assertEqual((col.kind, col.title, len(col.tracks)), ("album", "Abbey Road", 2))
        self.assertEqual((col.tracks[0].track_no, col.tracks[1].track_no, col.tracks[0].genre), (1, 2, "Rock"))

    def test_verify_pairs_drops_unknowns(self):
        got = search.verify_pairs([("Adele", "Skyfall"), ("Adele", "Skyfall (Remastered)"), ("Nobody", "Nonexistent Song")], Ctx())
        self.assertEqual([t.title for t in got], ["Skyfall"])          # duplicate and invented song removed

    def test_radio_interleaves_similar_artists(self):
        tracks = search.artist_radio("Radiohead", Ctx(), count=40)
        self.assertEqual(tracks[0].artist, "Radiohead")
        self.assertEqual([t.artist for t in tracks[:3]], ["Radiohead", "Muse", "Coldplay"])
        self.assertEqual(len(tracks), 7)                                  # 3 seed songs + 2 each from two similar artists
        self.assertEqual(len(search.artist_radio("Radiohead", Ctx(), count=4)), 4)

    def test_song_list_lines(self):
        self.assertEqual(search.line_to_track("1. Queen - Bohemian Rhapsody", Ctx()).title, "Bohemian Rhapsody")
        t = search.line_to_track("Hurt by Johnny Cash", Ctx())
        self.assertEqual((t.title, t.artist), ("Hurt", "Johnny Cash"))
        self.assertEqual(search.line_to_track("Skyfall", Ctx()).artist, "Adele")


class DispatchTests(unittest.TestCase):
    def test_classify(self):
        cases = {
            "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M": "spotify",
            "https://music.apple.com/us/album/x/1": "apple",
            "https://music.youtube.com/playlist?list=OLAK5uy_x": "ytmusic",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "youtube",
            "https://youtu.be/dQw4w9WgXcQ": "youtube",
            "https://music.amazon.com/albums/B00136LK9S": "amazon",
            "https://www.pandora.com/artist/x/AR1": "pandora",
            "https://soundcloud.com/a/b": "web",
            "Adele - Skyfall": "search",
            "": None,
        }
        for text, want in cases.items():
            self.assertEqual(ingest.classify(text), want, text)
        self.assertEqual(ingest.classify("Queen - A\nQueen - B"), "search")

    def test_classify_spreadsheet(self):
        p = os.path.join(fresh_dir("sheets2"), "x.csv")
        with open(p, "w") as fh:
            fh.write("track,artist\na,b\n")
        self.assertEqual(ingest.classify(p), "sheet")
        self.assertEqual(ingest.classify('"%s"' % p), "sheet")

    def test_resolve_dispatches_by_service(self):
        with mock.patch.object(spotify, "page", return_value=spotify_html("spotify_album.json")):
            col = ingest.resolve("https://open.spotify.com/album/0ETFjACtuP2ADo6LFhL6HN")
        self.assertEqual((col.service, len(col.tracks)), ("spotify", 5))

    def test_song_list_becomes_a_queue(self):
        col = ingest.resolve("Queen - Bohemian Rhapsody\n2. Adele - Skyfall\n\nPink Floyd - Time")
        self.assertEqual([t.title for t in col.tracks], ["Bohemian Rhapsody", "Skyfall", "Time"])
        self.assertEqual(col.title, "3 songs")

    def test_several_links_merge_and_failures_are_reported(self):
        def fake(line, ctx):
            if "bad" in line:
                raise ResolveError("nope")
            return spotify.parse(spotify_html("spotify_album.json"), "album")
        with mock.patch.object(ingest, "_one", side_effect=fake):
            col = ingest.resolve("https://a.com/1\nhttps://bad.com/2\nhttps://a.com/3")
        self.assertEqual(len(col.tracks), 5)                              # the same album twice is de-duplicated
        self.assertTrue(any("nope" in n for n in col.notes))
        self.assertEqual(col.title, "2 links")

    def test_all_links_failing_raises(self):
        with mock.patch.object(ingest, "_one", side_effect=ResolveError("offline")):
            with self.assertRaises(ResolveError):
                ingest.resolve("https://a.com/1\nhttps://b.com/2")

    def test_empty_input(self):
        with self.assertRaises(ResolveError):
            ingest.resolve("   ")

    def test_stop_cancels_a_long_list(self):
        ctx = Ctx(stop=threading.Event())
        ctx.stop.set()
        with self.assertRaises(ResolveError):
            ingest.resolve("A - B\nC - D", ctx)


class NetworkFailureTests(unittest.TestCase):
    def test_unreachable_site_is_friendly(self):
        from musicdl.core.models import EngineError
        with mock.patch.object(netio, "get_text", side_effect=EngineError("open.spotify.com: timed out")):
            with self.assertRaises(ResolveError) as cm:
                spotify.resolve("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", Ctx())
        self.assertIn("open.spotify.com", str(cm.exception))

    def test_missing_page_is_friendly(self):
        with mock.patch.object(netio, "get_text", return_value=None):
            with self.assertRaises(ResolveError) as cm:
                spotify.resolve("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", Ctx())
        self.assertIn("doesn’t exist", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
