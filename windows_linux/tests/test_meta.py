"""Artwork and metadata: cover art every player shows (Apple Music, Windows, Android), complete accurate tags per
format, and the catalogue logic that decides which release of a song is the right one."""
import io
import os
import random
import tempfile
import threading
import time
import unittest

from helpers import FFMPEG, NO_WINDOW, RangeServer, fresh_dir, make_audio, make_noise

from PIL import Image, ImageCms, ImageDraw, ImageOps
from mutagen.flac import FLAC
from mutagen.id3 import ID3
from mutagen.mp4 import MP4

from musicdl.config import Settings
from musicdl.core import catalog, engine, netio, release, sources
from musicdl.core.library import Library
from musicdl.core.models import EngineError, Track
from musicdl.meta import artwork
from musicdl.meta.tags import Tagset, extract_cover, image_size, read_info, write


# ---------------------------------------------------------------- pictures

def photo(w, h, hue=0):
    """A compressible, photo-like picture (a colourised fractal) of any size."""
    gray = Image.effect_mandelbrot((w, h), (-2.2 + hue * 0.01, -1.4, 1.0 + hue * 0.01, 1.4), 80)
    return ImageOps.colorize(gray.convert("L"), black=(40, 30, 120), white=(255, 200, 60)).convert("RGB")


def jpeg(im, **kw):
    b = io.BytesIO()
    im.save(b, "JPEG", **{"quality": 92, **kw})
    return b.getvalue()


def opened(data):
    return Image.open(io.BytesIO(data))


def near(px, rgb, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(px[:3], rgb))


class ArtworkTests(unittest.TestCase):
    def test_sized_asks_cdns_for_the_size_it_wants(self):
        self.assertEqual(artwork.sized("https://is1-ssl.mzstatic.com/image/thumb/Music/x.jpg/100x100bb.jpg", 1400),
                         "https://is1-ssl.mzstatic.com/image/thumb/Music/x.jpg/1400x1400bb.jpg")
        self.assertEqual(artwork.sized("https://is1-ssl.mzstatic.com/a/{w}x{h}bb.{f}", 1000),
                         "https://is1-ssl.mzstatic.com/a/1000x1000bb.jpg")
        self.assertEqual(artwork.sized("https://cdn-images.dzcdn.net/images/cover/ab12/1000x1000-000000-80-0-0.jpg", 1400),
                         "https://cdn-images.dzcdn.net/images/cover/ab12/1400x1400-000000-80-0-0.jpg")
        self.assertEqual(artwork.sized("https://example.com/c.jpg", 1400), "https://example.com/c.jpg")
        self.assertEqual(artwork.sized("", 1400), "")

    def test_apple_files_get_apples_size_everything_else_a_lighter_one(self):
        self.assertEqual(artwork.cover_side(".m4a"), 1400)
        self.assertEqual(artwork.cover_side(".M4A"), 1400)
        for ext in (".mp3", ".flac", ".wav"):
            self.assertEqual(artwork.cover_side(ext), 1000)

    def test_a_cover_is_a_square_of_the_asked_size_and_never_enlarged(self):
        big = artwork.prepare(jpeg(photo(1800, 1800)), side=1400)
        self.assertEqual(opened(big.jpeg).size, (1400, 1400))
        self.assertEqual(big.source_side, 1800)
        small = artwork.prepare(jpeg(photo(500, 500)), side=1400)
        self.assertEqual(opened(small.jpeg).size, (500, 500), "a 500 px original is not blown up to 1400")
        self.assertEqual(opened(artwork.prepare(jpeg(photo(1800, 1800)), side=1000).jpeg).size, (1000, 1000))

    def test_a_picture_a_hair_under_the_size_is_brought_up_to_it_but_no_further(self):
        got = artwork.prepare(jpeg(photo(1398, 1398)), side=1400)
        self.assertEqual((opened(got.jpeg).size, got.source_side), ((1400, 1400), 1398), "the 1400 px Apple asks for")
        self.assertEqual(opened(artwork.prepare(jpeg(photo(1300, 1300)), side=1400).jpeg).size, (1300, 1300))

    def test_a_picture_that_is_not_square_is_cropped_to_its_middle(self):
        wide = photo(1600, 900)
        d = ImageDraw.Draw(wide)
        d.rectangle([0, 0, 300, 900], fill=(255, 0, 0))                      # the left edge goes
        d.rectangle([1300, 0, 1600, 900], fill=(255, 0, 0))                  # so does the right
        got = artwork.prepare(jpeg(wide), side=1000)
        im = opened(got.jpeg)
        self.assertEqual(im.size, (900, 900))
        self.assertFalse(near(im.getpixel((5, 450)), (255, 0, 0), 60), "the red edge strips were cropped away")

    def test_progressive_cmyk_and_profiled_pictures_become_plain_srgb_baseline_jpegs(self):
        prog = artwork.prepare(jpeg(photo(900, 900), progressive=True), side=1000)
        self.assertFalse(opened(prog.jpeg).info.get("progressive"), "players silently fail to draw progressive JPEGs")
        cmyk = Image.new("CMYK", (800, 800), (0, 255, 255, 0))               # red
        ImageDraw.Draw(cmyk).ellipse([100, 100, 700, 700], fill=(255, 0, 0, 0))
        got = opened(artwork.prepare(jpeg(cmyk), side=1000).jpeg)
        self.assertEqual(got.mode, "RGB")
        self.assertTrue(near(got.getpixel((5, 5)), (255, 0, 0), 70), got.getpixel((5, 5)))
        icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        got = opened(artwork.prepare(jpeg(photo(900, 900), icc_profile=icc), side=1000).jpeg)
        self.assertEqual(got.mode, "RGB")
        self.assertIsNone(got.info.get("icc_profile"), "converted to sRGB, not carried around")

    def test_transparency_is_flattened_onto_white(self):
        im = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
        random.seed(2)
        for _ in range(4000):                                                # some opaque content, so it is not 'tiny'
            im.putpixel((random.randrange(100), random.randrange(100)), (random.randrange(256), 80, 40, 255))
        b = io.BytesIO()
        im.save(b, "PNG")
        got = opened(artwork.prepare(b.getvalue(), side=1000).jpeg)
        self.assertTrue(near(got.getpixel((300, 300)), (255, 255, 255), 6), got.getpixel((300, 300)))

    def test_the_exif_orientation_is_applied(self):
        im = Image.new("RGB", (700, 500), (255, 0, 0))
        ImageDraw.Draw(im).rectangle([350, 0, 700, 500], fill=(0, 0, 255))   # left red, right blue, as stored
        random.seed(3)
        for _ in range(6000):                                                # (noise so it is a real-sized file)
            im.putpixel((random.randrange(700), random.randrange(500)), (random.randrange(256),) * 3)
        exif = Image.Exif()
        exif[0x0112] = 6                                                     # displayed rotated 90° clockwise
        got = opened(artwork.prepare(jpeg(im, exif=exif, quality=98), side=1000).jpeg)
        self.assertTrue(near(got.getpixel((250, 40)), (255, 0, 0), 90), "left became top")
        self.assertTrue(near(got.getpixel((250, 460)), (0, 0, 255), 90), "right became bottom")

    def test_broken_and_tiny_pictures_are_refused(self):
        self.assertIsNone(artwork.prepare(b"not an image" * 600))
        self.assertIsNone(artwork.prepare(b""))
        self.assertIsNone(artwork.prepare(jpeg(photo(150, 150))), "a thumbnail is not a cover")
        self.assertIsNone(artwork.prepare(b"\xff\xd8\xff" + b"\x00" * 8000))

    def test_a_topic_video_frame_gives_back_the_album_cover_in_its_middle(self):
        art = photo(720, 720)
        frame = Image.new("RGB", (1280, 720), (0, 0, 0))
        frame.paste(art, (280, 0))                                           # what YouTube makes of an album cover
        got = artwork.prepare(jpeg(frame, quality=95), square=True, side=1000)
        im = opened(got.jpeg)
        self.assertAlmostEqual(im.width, 720, delta=8)
        self.assertEqual(im.width, im.height)
        self.assertFalse(near(im.getpixel((3, 3)), (0, 0, 0), 20), "no black bar left at the edge")
        self.assertFalse(near(im.getpixel((im.width - 4, im.height // 2)), (0, 0, 0), 20))

    def test_a_letterboxed_thumbnail_loses_its_bars(self):
        frame = Image.new("RGB", (480, 360), (0, 0, 0))
        frame.paste(photo(480, 270), (0, 45))                                # 16:9 picture in a 4:3 frame
        im = opened(artwork.prepare(jpeg(frame, quality=95), square=True, side=1000).jpeg)
        self.assertGreaterEqual(im.width, 240)
        self.assertFalse(near(im.getpixel((im.width // 2, 2)), (0, 0, 0), 20), "no black row at the top")
        self.assertFalse(near(im.getpixel((im.width // 2, im.height - 3)), (0, 0, 0), 20))

    def test_a_dark_picture_is_not_mistaken_for_bars(self):
        im = Image.new("RGB", (1280, 720), (0, 0, 0))
        ImageDraw.Draw(im).rectangle([490, 210, 790, 510], fill=(250, 250, 250))    # a small bright shape in the dark
        got = artwork.prepare(jpeg(im, quality=95), square=True, side=1000)
        self.assertEqual(got.source_side, 720, "kept the whole frame's middle, not just the bright shape")

    def test_album_art_with_a_black_border_keeps_it(self):
        im = Image.new("RGB", (800, 800), (0, 0, 0))
        im.paste(photo(600, 600), (100, 100))                                # a deliberate black frame is the artwork
        got = opened(artwork.prepare(jpeg(im, quality=95), square=False, side=1400).jpeg)
        self.assertEqual(got.size, (800, 800))
        self.assertTrue(near(got.getpixel((10, 10)), (0, 0, 0), 20))

    def test_a_picture_that_is_already_right_is_not_recompressed(self):
        data = jpeg(photo(1000, 1000), quality=88)
        got = artwork.prepare(data, side=1400)
        self.assertEqual(got.jpeg, data)
        self.assertNotEqual(artwork.prepare(data, side=800).jpeg, data, "…but one that has to shrink is remade")

    def test_the_thumbnail_is_small_and_square(self):
        got = artwork.prepare(jpeg(photo(1200, 1200)), side=1000)
        self.assertEqual(opened(got.thumb).size, (96, 96))
        self.assertEqual(tuple(got), (got.jpeg, got.thumb))


# ---------------------------------------------------------------- tags per format

AUDIO = fresh_dir("meta_audio")
FULL = dict(title="Gimme All Your Lovin'", artist="ZZ Top feat. Someone", album="Eliminator", year="1983", date="1983-03-23",
            genre="Rock", track_no=1, track_total=11, disc_no=1, disc_total=1, album_artist="ZZ Top", isrc="USWB10702680",
            explicit=1, compilation=False, label="Warner", source="youtube", src_kbps=160)


def make_file(name, *enc):
    p = os.path.join(AUDIO, name)
    make_audio(p, 3, *enc)
    return p


class TagTests(unittest.TestCase):
    cover = None

    @classmethod
    def setUpClass(cls):
        cls.cover = artwork.prepare(jpeg(photo(1500, 1500)), side=1400).jpeg

    def test_m4a_carries_what_apple_music_reads(self):
        for name, enc in (("aac.m4a", ("-c:a", "aac", "-b:a", "160k")), ("alac.m4a", ("-c:a", "alac"))):
            p = make_file(name, *enc)
            write(p, Tagset(**FULL), self.cover)
            t = MP4(p).tags
            self.assertEqual(t["aART"], ["ZZ Top"], "album artist: how Apple Music groups an album")
            self.assertEqual(t["©ART"], ["ZZ Top feat. Someone"])
            self.assertEqual(t["©day"], ["1983-03-23"])
            self.assertEqual(t["trkn"], [(1, 11)])
            self.assertEqual(t["disk"], [(1, 1)])
            self.assertEqual(t["rtng"], [1], "the 'E' badge")
            self.assertEqual(t["stik"], [1], "media kind: Music")
            self.assertEqual(bytes(t["----:com.apple.iTunes:ISRC"][0]), b"USWB10702680")
            self.assertEqual(bytes(t["----:com.apple.iTunes:LABEL"][0]), b"Warner")
            self.assertNotIn("cpil", t)
            self.assertEqual(MP4(p).tags["covr"][0].imageformat, 13, "a JPEG cover")
            self.assertEqual(image_size(extract_cover(p)), (1400, 1400))
            i = read_info(p)
            self.assertEqual((i.album_artist, i.date, i.track_no, i.track_total, i.disc_total, i.explicit, i.isrc, i.cover),
                             ("ZZ Top", "1983-03-23", 1, 11, 1, 1, "USWB10702680", True))
            self.assertEqual(i.lossless, name == "alac.m4a")

    def test_m4a_clean_and_compilation_flags(self):
        p = make_file("flags.m4a", "-c:a", "aac", "-b:a", "128k")
        write(p, Tagset(title="T", artist="A", album="Hits", compilation=True, explicit=2, album_artist="Various Artists"))
        t = MP4(p).tags
        self.assertIs(bool(t["cpil"]), True)
        self.assertEqual(t["rtng"], [2])
        self.assertNotIn("trkn", t, "no track number known: none is invented")
        write(p, Tagset(title="T", artist="A"))
        self.assertNotIn("rtng", MP4(p).tags, "unknown / not explicit writes no advisory at all")

    def test_mp3_is_id3v23_with_utf16_text_and_the_frames_windows_and_android_read(self):
        p = make_file("a.mp3", "-c:a", "libmp3lame", "-b:a", "192k")
        write(p, Tagset(**{**FULL, "title": "Beyoncé — Halo ✓"}), self.cover)
        raw = ID3(p, translate=False)
        self.assertEqual(raw.version, (2, 3, 0), "ID3v2.3: what Windows Explorer and old players understand")
        self.assertEqual(str(raw["TPE2"]), "ZZ Top")
        self.assertEqual(str(raw["TRCK"]), "1/11")
        self.assertEqual(str(raw["TPOS"]), "1/1")
        self.assertEqual(str(raw["TYER"]), "1983")
        self.assertEqual(str(raw["TDAT"]), "2303", "23 March, as DDMM")
        self.assertEqual(str(raw["TSRC"]), "USWB10702680")
        self.assertEqual(str(raw["TPUB"]), "Warner")
        self.assertEqual(raw["TIT2"].encoding, 1, "UTF-16")
        apic = raw.getall("APIC")[0]
        self.assertEqual((apic.type, apic.mime), (3, "image/jpeg"))
        i = read_info(p)
        self.assertEqual((i.title, i.album_artist, i.date, i.track_no, i.track_total, i.disc_no, i.disc_total),
                         ("Beyoncé — Halo ✓", "ZZ Top", "1983-03-23", 1, 11, 1, 1))

    def test_mp3_compilation_flag(self):
        p = make_file("c.mp3", "-c:a", "libmp3lame", "-b:a", "128k")
        write(p, Tagset(title="T", artist="A", album="Hits", compilation=True, album_artist="Various Artists"))
        self.assertEqual(str(ID3(p)["TCMP"]), "1")
        self.assertTrue(read_info(p).compilation)

    def test_flac_has_albumartist_totals_and_a_picture_with_its_size(self):
        p = make_file("a.flac", "-c:a", "flac")
        write(p, Tagset(**FULL), self.cover)
        f = FLAC(p)
        for key, want in (("albumartist", "ZZ Top"), ("tracknumber", "1"), ("tracktotal", "11"), ("totaltracks", "11"),
                          ("discnumber", "1"), ("disctotal", "1"), ("date", "1983-03-23"), ("isrc", "USWB10702680"),
                          ("label", "Warner")):
            self.assertEqual(f[key], [want], key)
        pic = f.pictures[0]
        self.assertEqual((pic.type, pic.mime, pic.width, pic.height, pic.depth), (3, "image/jpeg", 1400, 1400, 24))
        i = read_info(p)
        self.assertEqual((i.album_artist, i.track_total, i.date, i.lossless), ("ZZ Top", 11, "1983-03-23", True))

    def test_wav_carries_the_same_id3_tags(self):
        p = make_file("a.wav", "-c:a", "pcm_s16le")
        write(p, Tagset(**FULL), self.cover)
        i = read_info(p)
        self.assertEqual((i.album_artist, i.album, i.track_total, i.cover), ("ZZ Top", "Eliminator", 11, True))

    def test_the_date_is_written_only_when_it_is_well_formed(self):
        p = make_file("d.mp3", "-c:a", "libmp3lame", "-b:a", "128k")
        for date, year, want in (("1983-03-23", "1983", "1983-03-23"), ("1983", "1983", "1983"), ("1983-3-2", "1983", "1983"),
                                 ("", "1983", "1983"), ("garbage", "", ""), ("", "19", ""),
                                 ("2001-12", "", "2001")):                      # (v2.3 cannot hold a month without a day)
            write(p, Tagset(title="T", artist="A", year=year, date=date))
            self.assertEqual(read_info(p).date, want, (date, year))

    def test_totals_without_a_number_are_not_invented(self):
        p = make_file("n.mp3", "-c:a", "libmp3lame", "-b:a", "128k")
        write(p, Tagset(title="T", artist="A", track_total=12))
        self.assertNotIn("TRCK", ID3(p))
        write(p, Tagset(title="T", artist="A", track_no=3))
        self.assertEqual(str(ID3(p)["TRCK"]), "3", "a number without a total is just the number")

    def test_merging_keeps_what_is_already_there(self):
        p = make_file("m.m4a", "-c:a", "aac", "-b:a", "128k")
        write(p, Tagset(**FULL), self.cover)
        write(p, Tagset(lyrics="la la la"), None, merge=True)
        t = MP4(p).tags
        self.assertEqual((t["aART"], t["trkn"], t["stik"], t["©lyr"]), (["ZZ Top"], [(1, 11)], [1], ["la la la"]))
        self.assertTrue(read_info(p).cover)

    def test_adding_only_a_cover_does_not_stamp_a_media_kind(self):
        p = make_file("k.m4a", "-c:a", "aac", "-b:a", "128k")
        write(p, Tagset(), self.cover, merge=True)
        self.assertNotIn("stik", MP4(p).tags)
        self.assertTrue(read_info(p).cover)


# ---------------------------------------------------------------- which release is the right one

def hit(title, album, date, n=1, total=10, disc=1, discs=1, artist="ZZ Top", aa=None, secs=240.0, genre="Rock", cover="u"):
    return {"src": "itunes", "title": title, "artist": artist, "album": album, "album_artist": aa or artist, "date": date,
            "track_no": n, "track_total": total, "disc_no": disc, "disc_total": discs, "genre": genre, "explicit": 0,
            "seconds": secs, "cover": cover}


def best(hits, title="", album="", seconds=0):
    ranked = release.rank(hits, {"title": title, "album": album, "seconds": seconds})
    return (ranked[0].hit, release.original_date(ranked[0])) if ranked else (None, "")


class ReleaseTests(unittest.TestCase):
    """The hits are what iTunes really answered for these songs (trimmed to the fields that matter)."""

    def test_the_studio_album_beats_compilations_live_albums_and_deluxe_editions(self):
        g = "Gimme All Your Lovin'"
        hits = [hit(g, "Eliminator", "1983-03-23", 1, 11),
                hit(g + " (2019 Remaster)", "Goin' 50 (Deluxe Edition)", "1983-03-23", 1, 17, 2, 3),
                hit(g + " (Live from Texas)", "Live from Texas (Audio Version)", "2008-06-24", 11, 16),
                hit(g, "Pub Crawl Sing-A-long", "1983-03-16", 8, 25, aa="Various Artists"),
                hit(g + " (2003 Remaster)", "Chrome Smoke & BBQ: The ZZ Top Box", "1983-03-16", 5, 20, 3, 4),
                hit(g, "The Complete Studio Albums (1970 - 1990)", "1983-03-23", 1, 11),
                hit(g, "The Very Baddest of... ZZ Top", "1983-01-01", 1, 21),
                hit(g, "The Baddest", "1983-01-01", 1, 20),
                hit(g, "ZZ Top's Greatest Hits", "1983-03-16", 1, 18)]
        h, date = best(hits, g)
        self.assertEqual((h["album"], date), ("Eliminator", "1983-03-23"))

    def test_a_live_recording_is_never_the_release_of_the_studio_song(self):
        hits = [hit("Numb (Live In Texas)", "Live In Texas", "2003-03-24", 13, 17, 2, 6, artist="LINKIN PARK"),
                hit("Numb (Live)", "One More Light: Live", "2017-12-15", 14, 16, artist="LINKIN PARK"),
                hit("Numb (Instrumental)", "Papercuts: Instrumentals", "2024-04-12", 19, 20, artist="LINKIN PARK"),
                hit("Numb", "Meteora", "2003-03-25", 13, 13, artist="LINKIN PARK")]
        h, date = best(hits, "Numb")
        self.assertEqual((h["album"], h["track_no"], h["track_total"], date), ("Meteora", 13, 13, "2003-03-25"))
        only_live = hits[:3]
        self.assertEqual(release.rank(only_live, {"title": "Numb"}), [], "no studio release known: nothing is made up")

    def test_the_standard_edition_stands_in_for_its_deluxe_edition_and_the_true_date_wins(self):
        s = "She Wants to Dance with Me"
        hits = [hit(s + " (2023 Remaster)", "Hold Me in Your Arms (Deluxe Edition - 2023 Remaster)", "1987-01-01", 1, 28,
                    artist="Rick Astley"),
                hit(s + " (Bonus)", "Hold Me in Your Arms (Deluxe Edition - 2023 Remaster)", "1987-01-01", 18, 28, artist="Rick Astley"),
                hit(s, "3 Originals", "1988-09-20", 1, 10, 2, 3, artist="Rick Astley"),
                hit(s, "Hold Me in Your Arms", "1988-09-20", 1, 10, artist="Rick Astley"),
                hit(s, "The Best of Me", "1988-09-20", 7, 30, artist="Rick Astley")]
        h, date = best(hits, s)
        self.assertEqual((h["album"], h["track_no"], h["track_total"]), ("Hold Me in Your Arms", 1, 10),
                         "not the deluxe edition's track 1 of 28")
        self.assertEqual(date, "1988-09-20", "a careless 1987 on the deluxe edition does not outvote the real date")

    def test_catalogue_order_breaks_ties_between_unremarkable_albums(self):
        t = "You're No Good"
        hits = [hit(t + " (Live)", "Live In Hollywood", "2019-01-30", 9, 13, artist="Linda Ronstadt"),
                hit(t, "Heart Like a Wheel (2013 Remaster)", "1974-11-15", 1, 10, artist="Linda Ronstadt"),
                hit(t, "The Early Years", "1974-11-15", 14, 15, artist="Linda Ronstadt")]
        self.assertEqual(best(hits, t)[0]["album"], "Heart Like a Wheel (2013 Remaster)")

    def test_a_year_alone_is_a_year_alone(self):
        h, date = best([hit("Possum Kingdom", "Rubberneck", "1994-01-01", 4, 11, artist="Toadies")], "Possum Kingdom")
        self.assertEqual(date, "1994", "January 1st is how catalogues write 'sometime in 1994'")
        self.assertEqual(release.valid_date("1983-01-01"), "1983")
        self.assertEqual(release.valid_date("0000-00-00"), "")
        self.assertEqual(release.valid_date("1850-05-05"), "")
        self.assertEqual(release.valid_date("1983-03-23T08:00:00Z"), "1983-03-23")

    def test_asking_for_a_live_version_finds_the_live_album(self):
        hits = [hit("Numb", "Meteora", "2003-03-25", 13, 13, artist="LINKIN PARK"),
                hit("Numb (Live In Texas)", "Live In Texas", "2003-03-24", 13, 17, artist="LINKIN PARK")]
        self.assertEqual(best(hits, "Numb (Live)")[0]["album"], "Live In Texas")

    def test_karaoke_and_sped_up_versions_are_other_recordings(self):
        hits = [hit("Numb (Karaoke Version)", "Karaoke Hits", "2010-01-01", artist="LINKIN PARK"),
                hit("Numb (Sped Up)", "Numb (Sped Up) - Single", "2022-01-01", artist="LINKIN PARK")]
        self.assertEqual(release.rank(hits, {"title": "Numb"}), [])

    def test_a_different_length_means_a_different_recording(self):
        hits = [hit("Numb", "Meteora", "2003-03-25", 13, 13, secs=187.0), hit("Numb", "Other", "2003-03-25", 1, 5, secs=420.0)]
        self.assertEqual(best(hits, "Numb", seconds=186)[0]["album"], "Meteora")
        self.assertEqual(len(release.rank(hits, {"title": "Numb", "seconds": 186})), 1, "the 7-minute one is not this song")

    def test_the_album_the_service_named_wins(self):
        g = "Gimme All Your Lovin'"
        hits = [hit(g, "Eliminator", "1983-03-23", 1, 11), hit(g, "ZZ Top's Greatest Hits", "1992-01-01", 1, 18)]
        self.assertEqual(best(hits, g, album="ZZ Top's Greatest Hits")[0]["album"], "ZZ Top's Greatest Hits")
        self.assertEqual(best(hits, g, album="Eliminator (Remastered)")[0]["album"], "Eliminator")

    def test_family_ignores_editions_but_not_names(self):
        f = release.family
        self.assertEqual(f("Meteora (Deluxe Edition)"), f("Meteora"))
        self.assertEqual(f("Abbey Road (2019 Mix)"), f("Abbey Road"))
        self.assertEqual(f("Heart Like a Wheel (2013 Remaster)"), f("Heart Like a Wheel"))
        self.assertEqual(f("Hold Me in Your Arms - Single"), f("Hold Me in Your Arms"))
        self.assertNotEqual(f("1989"), f("1989 (Taylor's Version)"))
        self.assertTrue(f("1989"), "an album named with a number still has an identity")

    def test_a_re_recording_is_not_the_original_unless_that_is_what_was_asked_for(self):
        hits = [hit("Shake It Off (Taylor's Version)", "1989 (Taylor's Version)", "2023-10-27", 6, 21, artist="Taylor Swift"),
                hit("Shake It Off", "1989", "2014-10-27", 6, 13, artist="Taylor Swift")]
        h, date = best(hits, "Shake It Off")
        self.assertEqual((h["album"], date), ("1989", "2014-10-27"))
        h, date = best(hits, "Shake It Off (Taylor's Version)")
        self.assertEqual((h["album"], date), ("1989 (Taylor's Version)", "2023-10-27"))
        h, _ = best(hits, "Shake It Off", album="1989 (Taylor's Version)")
        self.assertEqual(h["album"], "1989 (Taylor's Version)", "the service said which one it is")

    def test_albums_called_live_but_not_live_albums_are_not_punished(self):
        self.assertNotIn("live", release.album_flags("Live Through This"))
        self.assertIn("live", release.album_flags("Live in Texas"))
        self.assertIn("live", release.album_flags("Rock Show: Live"))
        self.assertIn("live", release.album_flags("Meteora: Live Around the World"))
        self.assertNotIn("live", release.title_flags("Live Forever"), "a song called Live Forever is not a live version")
        self.assertIn("live", release.title_flags("Live Forever (Live at Knebworth)"))


# ---------------------------------------------------------------- the lookup, with the catalogues stubbed

def itunes_row(title, album, date, n, total, artist="ZZ Top", **kw):
    row = {"trackName": title, "artistName": artist, "collectionName": album, "releaseDate": date + "T08:00:00Z",
           "trackNumber": n, "trackCount": total, "discNumber": 1, "discCount": 1, "primaryGenreName": "Rock",
           "trackExplicitness": "notExplicit", "trackTimeMillis": 243000,
           "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Music/x/100x100bb.jpg"}
    row.update(kw)
    return row


class FakeNet:
    """Stands in for netio.get_json: `routes` maps a URL to a JSON answer or an exception to raise."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url, params=None, **kw):
        self.calls.append((url, params, kw))
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer(url, params) if callable(answer) else answer
        return None


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.lib = Library(tempfile.mkdtemp())
        self.stop = threading.Event()
        self.saved = catalog.get_json

    def tearDown(self):
        catalog.get_json = self.saved

    def use(self, routes):
        catalog.get_json = FakeNet(routes)
        return catalog.get_json

    def test_itunes_answers_everything_in_one_request(self):
        net = self.use({catalog.ITUNES_URL: {"results": [
            itunes_row("Gimme All Your Lovin'", "Eliminator", "1983-03-23", 1, 11, trackExplicitness="explicit"),
            itunes_row("Gimme All Your Lovin' (Live)", "Live from Texas", "2008-06-24", 11, 16)]}})
        c = catalog.lookup(self.lib, Track("Gimme All Your Lovin'", "ZZ Top"), self.stop)
        self.assertEqual(len(net.calls), 1, "no second catalogue needed")
        self.assertEqual((c["album"], c["album_artist"], c["date"], c["year"], c["track_no"], c["track_total"], c["disc_no"],
                          c["disc_total"], c["genre"], c["explicit"], c["compilation"], c["src"]),
                         ("Eliminator", "ZZ Top", "1983-03-23", "1983", 1, 11, 1, 1, "Rock", 1, False, "itunes"))
        self.assertEqual(len(c["releases"]), 1, "the live album is not a release of this song")
        self.assertTrue(c["covers"][0].endswith("100x100bb.jpg"), "stored as the CDN gave it; the caller picks the size")
        self.assertIn(243.0, c["durations"])
        self.assertTrue(catalog.fresh(c) and not c["miss"] and not c["failed"])

    def test_the_answer_is_cached_and_the_cache_is_used(self):
        net = self.use({catalog.ITUNES_URL: {"results": [itunes_row("Numb", "Meteora", "2003-03-25", 13, 13, artist="LINKIN PARK")]}})
        t = Track("Numb", "Linkin Park")
        first = catalog.lookup(self.lib, t, self.stop)
        again = catalog.lookup(self.lib, Track("Numb (Remastered)", "Linkin Park"), self.stop)
        self.assertEqual(len(net.calls), 1)
        self.assertEqual(again["album"], first["album"])

    def test_an_answer_from_the_old_layout_is_asked_again(self):
        self.lib.put("catalog", "zz top|gimme all your lovin", {"durations": [], "covers": [], "album": "Eliminator", "year": "2026",
                                                                "track_no": 1, "disc_no": 1, "isrc": "", "genre": "", "t": time.time()})
        self.assertFalse(catalog.fresh(self.lib.get("catalog", "zz top|gimme all your lovin")))
        net = self.use({catalog.ITUNES_URL: {"results": [itunes_row("Gimme All Your Lovin'", "Eliminator", "1983-03-23", 1, 11)]}})
        c = catalog.lookup(self.lib, Track("Gimme All Your Lovin'", "ZZ Top"), self.stop)
        self.assertEqual((len(net.calls), c["year"]), (1, "1983"), "the cached 2026 is not believed")

    def test_deezer_takes_over_when_itunes_cannot_be_asked_and_dates_come_from_the_album(self):
        deezer = {"data": [
            {"id": 11, "title": "Gimme All Your Lovin'", "title_short": "Gimme All Your Lovin'", "title_version": "",
             "duration": 243, "explicit_lyrics": False, "artist": {"name": "ZZ Top"},
             "album": {"id": 5, "title": "Eliminator", "cover_xl": "https://cdn-images.dzcdn.net/images/cover/x/1000x1000-000000-80-0-0.jpg"}},
            {"id": 12, "title": "Gimme All Your Lovin' (Live from Houston)", "title_short": "Gimme All Your Lovin'",
             "title_version": "(Live from Houston)", "duration": 285, "artist": {"name": "ZZ Top"},
             "album": {"id": 6, "title": "Live! Greatest Hits from Around the World"}}]}
        net = self.use({
            catalog.ITUNES_URL: EngineError("itunes.apple.com: HTTP 403"),
            catalog.DEEZER_URL + "/search": deezer,
            catalog.DEEZER_URL + "/track/11": {"release_date": "2026-01-28", "track_position": 1, "disk_number": 1,
                                               "isrc": "USWB10702680", "explicit_lyrics": False},
            catalog.DEEZER_URL + "/album/5": {"release_date": "1983-03-23", "nb_tracks": 11, "label": "BMG",
                                              "artist": {"name": "ZZ Top"}, "record_type": "album",
                                              "genres": {"data": [{"name": "Rock"}]}}})
        c = catalog.lookup(self.lib, Track("Gimme All Your Lovin'", "ZZ Top"), self.stop)
        self.assertEqual((c["src"], c["album"], c["date"], c["track_no"], c["track_total"], c["isrc"], c["genre"], c["label"]),
                         ("deezer", "Eliminator", "1983-03-23", 1, 11, "USWB10702680", "Rock", "BMG"))
        self.assertEqual(c["year"], "1983", "the album's date, not the 2026 the track says Deezer re-released it")
        self.assertFalse(any("_track_id" in r or "_album_id" in r for r in c["releases"]))

    def test_nobody_answering_is_not_a_miss_and_is_not_remembered(self):
        self.use({catalog.ITUNES_URL: EngineError("down"), catalog.DEEZER_URL: EngineError("down")})
        c = catalog.lookup(self.lib, Track("Numb", "Linkin Park"), self.stop)
        self.assertTrue(c["failed"])
        self.assertIsNone(self.lib.get("catalog", "linkin park|numb"), "asked again next time, not 'no such song' for a month")
        net = self.use({catalog.ITUNES_URL: {"results": [itunes_row("Numb", "Meteora", "2003-03-25", 13, 13, artist="LINKIN PARK")]}})
        self.assertEqual(catalog.lookup(self.lib, Track("Numb", "Linkin Park"), self.stop)["album"], "Meteora")

    def test_a_song_the_catalogues_do_not_know_is_a_miss_that_is_remembered_for_a_while(self):
        net = self.use({catalog.ITUNES_URL: {"results": []}, catalog.DEEZER_URL: {"data": []}})
        c = catalog.lookup(self.lib, Track("Nobody Sings This", "Nobody"), self.stop)
        self.assertTrue(c["miss"] and not c["failed"] and not c["releases"])
        calls = len(net.calls)
        catalog.lookup(self.lib, Track("Nobody Sings This", "Nobody"), self.stop)
        self.assertEqual(len(net.calls), calls)

    def test_unknown_artists_are_not_searched_for(self):
        net = self.use({})
        c = catalog.lookup(self.lib, Track("Some Title", "Unknown artist"), self.stop)
        self.assertEqual((len(net.calls), c["miss"]), (0, True))

    def test_a_search_hit_for_another_artist_is_not_this_song(self):
        self.use({catalog.ITUNES_URL: {"results": [itunes_row("Numb", "Collision Course", "2004-11-30", 4, 6, artist="JAY-Z")]}})
        c = catalog.lookup(self.lib, Track("Numb", "Linkin Park"), self.stop)
        self.assertTrue(c["miss"])

    def test_various_artists_albums_are_compilations(self):
        self.use({catalog.ITUNES_URL: {"results": [itunes_row(
            "Sleigh Ride", "A Christmas Gift For You", "1963-11-22", 5, 13, artist="The Ronettes",
            collectionArtistName="Various Artists", primaryGenreName="Christmas")]}})
        c = catalog.lookup(self.lib, Track("Sleigh Ride", "The Ronettes"), self.stop)
        self.assertEqual((c["album_artist"], c["compilation"]), ("Various Artists", True))


class MergeTests(unittest.TestCase):
    ANSWER = {"genre": "Rock", "isrc": "", "explicit": 1, "releases": [
        {"album": "Eliminator", "album_artist": "ZZ Top", "date": "1983-03-23", "year": "1983", "track_no": 1, "track_total": 11,
         "disc_no": 1, "disc_total": 1, "compilation": False, "label": "Warner", "cover": "u1", "genre": "Rock", "explicit": 1},
        {"album": "ZZ Top's Greatest Hits", "album_artist": "ZZ Top", "date": "1992", "year": "1992", "track_no": 3,
         "track_total": 18, "disc_no": 1, "disc_total": 1, "compilation": False, "label": "", "cover": "u2", "genre": "Rock",
         "explicit": 0}]}

    def test_an_empty_track_takes_everything_from_the_best_release(self):
        t = Track("Gimme", "ZZ Top")
        catalog.merge_details(t, self.ANSWER)
        self.assertEqual((t.album, t.album_artist, t.year, t.date, t.track_no, t.track_total, t.disc_no, t.disc_total, t.genre,
                          t.explicit, t.record_label),
                         ("Eliminator", "ZZ Top", "1983", "1983-03-23", 1, 11, 1, 1, "Rock", 1, "Warner"))

    def test_a_service_named_album_gets_that_albums_numbers_never_another_albums(self):
        t = Track("Gimme", "ZZ Top", album="ZZ Top's Greatest Hits")
        catalog.merge_details(t, self.ANSWER)
        self.assertEqual((t.album, t.track_no, t.track_total, t.year, t.date), ("ZZ Top's Greatest Hits", 3, 18, "1992", "1992"))
        t = Track("Gimme", "ZZ Top", album="Some Album The Catalogue Lacks")
        catalog.merge_details(t, self.ANSWER)
        self.assertEqual((t.album, t.track_no, t.track_total, t.year, t.date), ("Some Album The Catalogue Lacks", 0, 0, "", ""),
                         "no track number from a different album")
        self.assertEqual((t.genre, t.explicit), ("Rock", 1), "…but facts about the song still apply")
        self.assertEqual(t.album_artist, "ZZ Top", "the artist is the album artist when nothing says otherwise")

    def test_what_the_service_knows_is_kept(self):
        t = Track("Gimme", "ZZ Top", album="Eliminator", year="1984", track_no=7, genre="Hard Rock")
        catalog.merge_details(t, self.ANSWER)
        self.assertEqual((t.year, t.track_no, t.genre), ("1984", 7, "Hard Rock"))
        self.assertEqual(t.date, "", "a full date that contradicts the service's year is not added")
        self.assertEqual(t.track_total, 0, "the total belongs to a track number we did not take from it")

    def test_a_hand_made_old_style_answer_still_merges(self):
        t = Track("T", "A")
        catalog.merge_details(t, {"album": "Old", "year": "2001", "track_no": 3, "disc_no": 1, "genre": "Pop", "isrc": "X1", "covers": []})
        self.assertEqual((t.album, t.year, t.track_no, t.disc_no, t.genre, t.isrc), ("Old", "2001", 3, 1, "Pop", "X1"))

    def test_covers_come_from_the_release_that_was_chosen(self):
        self.assertEqual(catalog.covers_for(Track("T", "A"), self.ANSWER), ["u1", "u2"])
        self.assertEqual(catalog.covers_for(Track("T", "A", album="ZZ Top's Greatest Hits"), self.ANSWER), ["u2", "u1"])
        self.assertEqual(catalog.covers_for(Track("T", "A", album="Unlisted"), self.ANSWER), [],
                         "another album's cover is not this album's")
        self.assertEqual(catalog.covers_for(Track("T", "A"), {"covers": ["x"]}), ["x"])


# ---------------------------------------------------------------- iTunes says 403 when it has had enough

class ThrottleTests(unittest.TestCase):
    def setUp(self):
        self.www = fresh_dir("meta_throttle")
        with open(os.path.join(self.www, "s"), "wb") as fh:
            fh.write(b"ok")
        self.srv = RangeServer(self.www)
        self.saved = netio._backoff
        netio._backoff = lambda attempt: 0.01

    def tearDown(self):
        netio._backoff = self.saved
        self.srv.close()

    def limiter(self):
        lim = netio.Limiter(0.001)
        lim.slows = []
        lim.slow = lambda pause=0.0: lim.slows.append(pause)
        return lim

    def test_a_status_named_as_throttling_slows_everyone_and_is_retried(self):
        self.srv.fail["/s"] = [403, 1]
        lim = self.limiter()
        r = netio.request("GET", self.srv.url + "s", limiter=lim, retries=3, throttle=(403,))
        self.assertEqual(r.content, b"ok")
        self.assertEqual(len(lim.slows), 1)
        self.assertGreater(lim.slows[0], 0, "everyone is held back for a while, not only slowed")

    def test_without_the_hint_a_403_is_just_forbidden(self):
        self.srv.fail["/s"] = [403, 1]
        lim = self.limiter()
        r = netio.request("GET", self.srv.url + "s", limiter=lim, retries=3)
        self.assertEqual((r.status_code, len(lim.slows)), (403, 0))

    def test_a_host_that_keeps_saying_no_ends_in_an_error_not_a_hang(self):
        self.srv.fail["/s"] = [403, 50]
        with self.assertRaises(EngineError):
            netio.request("GET", self.srv.url + "s", limiter=self.limiter(), retries=2, throttle=(403,))

    def test_the_itunes_pace_is_what_was_measured_not_the_old_guess(self):
        self.assertLessEqual(netio.ITUNES_LIMIT.base, 2.0)
        self.assertGreaterEqual(netio.ITUNES_LIMIT.ceil, 12.0, "…and it can still back off a long way")


# ---------------------------------------------------------------- whole jobs

WWW = fresh_dir("meta_www")
SRV = None
PLAN = {}
ANSWERS = {}
_ORIGINAL = {}
RELEASES = [{"album": "Eliminator", "album_artist": "ZZ Top", "date": "1983-03-23", "year": "1983", "track_no": 1,
             "track_total": 11, "disc_no": 1, "disc_total": 1, "genre": "Rock", "explicit": 1, "compilation": False,
             "label": "", "cover": ""}]


def cand(f, secs=30, kbps=128):
    return {"source": "archive.org", "id": f"meta:{f}", "url": SRV.url + f, "ext": os.path.splitext(f)[1], "seconds": secs,
            "kbps": kbps, "lossless": False, "score": 1, "title": f}


def answer(cover="", **kw):
    rel = dict(RELEASES[0], cover=cover)
    base = {"v": catalog.RELEASE_V, "durations": [], "covers": [cover] if cover else [], "releases": [rel], "t": time.time(),
            "miss": False, "failed": False, "isrc": "", "src": "itunes", "genre": "Rock", "explicit": 1}
    base.update({k: rel[k] for k in ("album", "album_artist", "date", "year", "track_no", "track_total", "disc_no", "disc_total",
                                     "compilation", "label")})
    base.update(kw)
    return base


def setUpModule():
    global SRV
    make_audio(os.path.join(WWW, "src.mp3"), 30, "-b:a", "128k")
    cover = photo(1700, 1700)
    cover.save(os.path.join(WWW, "big.jpg"), quality=90)
    photo(600, 600).save(os.path.join(WWW, "small.png"))
    frame = Image.new("RGB", (1280, 720), (0, 0, 0))
    frame.paste(photo(720, 720, 5), (280, 0))
    frame.save(os.path.join(WWW, "topic.jpg"), quality=95)
    mid = os.path.join(WWW, "_art.mp3")
    make_audio(mid, 30, "-b:a", "128k")
    write(mid, Tagset(title="x"), jpeg(photo(900, 900, 9)))                    # a download with a picture inside it
    os.replace(mid, os.path.join(WWW, "withart.mp3"))
    SRV = RangeServer(WWW)


def tearDownModule():
    SRV.close()


def fake_lookup(lib, track, stop, want_genre=False):
    got = ANSWERS.get(track.title)
    return got() if callable(got) else dict(got or answer())


def settings(**kw):
    st = Settings()
    st.auto, st.parallel, st.min_kbps, st.verify = False, 1, 0, False
    for k, v in kw.items():
        setattr(st, k, v)
    return st


class JobMetaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        limits = (netio.DEEZER_LIMIT, netio.WEB_LIMIT, netio.CAA_LIMIT)
        _ORIGINAL.update(archive=sources.archive_candidates, youtube=sources.youtube_candidates, lookup=catalog.lookup,
                         recheck=engine.RECHECK, limits=[(l, l.interval) for l in limits])
        for lim in limits:
            lim.interval = 0.01
        engine.RECHECK = 0.05
        sources.archive_candidates = lambda rc, stop, fits, deep: [c for c in PLAN.get(rc.track.title, [])
                                                                   if fits(rc, c["seconds"], deep)]
        sources.youtube_candidates = lambda *a, **k: []
        catalog.lookup = fake_lookup

    @classmethod
    def tearDownClass(cls):
        sources.archive_candidates, sources.youtube_candidates = _ORIGINAL["archive"], _ORIGINAL["youtube"]
        catalog.lookup = _ORIGINAL["lookup"]
        engine.RECHECK = _ORIGINAL["recheck"]
        for lim, interval in _ORIGINAL["limits"]:
            lim.interval = interval

    def setUp(self):
        PLAN.clear()
        ANSWERS.clear()
        SRV.fail.clear()
        SRV.log.clear()
        self.out = fresh_dir("meta_out")

    def run_job(self, tracks, st):
        ev = []
        engine.Job(tracks, self.out, st, emit=ev.append).run()
        return {e["result"].track: e["result"] for e in ev if e["type"] == "result"}

    def path(self, name):
        return os.path.join(self.out, name)

    def test_apple_files_get_a_1400_px_sRGB_baseline_cover_and_the_tags_apple_music_groups_by(self):
        PLAN["Gimme"] = [cand("src.mp3")]
        ANSWERS["Gimme"] = answer(cover=SRV.url + "big.jpg")
        r = self.run_job([Track("Gimme", "ZZ Top feat. Someone", duration=30.0)], settings(preset="better", device="apple"))
        self.assertEqual(r["Gimme"].status, "ok")
        p = self.path("Gimme - ZZ Top feat. Someone.m4a")
        self.assertTrue(os.path.isfile(p), os.listdir(self.out))
        im = opened(extract_cover(p))
        self.assertEqual((im.size, im.mode, bool(im.info.get("progressive"))), ((1400, 1400), "RGB", False))
        t = MP4(p).tags
        self.assertEqual((t["aART"], t["©alb"], t["©day"], t["trkn"], t["disk"], t["rtng"], t["stik"], t["©gen"]),
                         (["ZZ Top"], ["Eliminator"], ["1983-03-23"], [(1, 11)], [(1, 1)], [1], [1], ["Rock"]))
        self.assertEqual(t["©ART"], ["ZZ Top feat. Someone"])

    def test_windows_and_android_files_get_a_1000_px_cover_and_complete_id3(self):
        PLAN["Gimme"] = [cand("src.mp3")]
        ANSWERS["Gimme"] = answer(cover=SRV.url + "big.jpg")
        self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="other"))
        p = self.path("Gimme - ZZ Top.mp3")
        self.assertEqual(opened(extract_cover(p)).size, (1000, 1000))
        raw = ID3(p, translate=False)
        self.assertEqual((raw.version, str(raw["TPE2"]), str(raw["TRCK"]), str(raw["TPOS"]), str(raw["TYER"])),
                         ((2, 3, 0), "ZZ Top", "1/11", "1/1", "1983"))
        self.assertLess(os.path.getsize(p), 6 * 2 ** 20)

    def test_a_picture_the_catalogue_could_only_give_small_is_not_blown_up(self):
        PLAN["Gimme"] = [cand("src.mp3")]
        ANSWERS["Gimme"] = answer(cover=SRV.url + "small.png")
        self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="apple"))
        self.assertEqual(opened(extract_cover(self.path("Gimme - ZZ Top.m4a"))).size, (600, 600))

    def test_one_failed_fetch_does_not_cost_the_album_its_artwork(self):
        PLAN["One"], PLAN["Two"] = [cand("src.mp3")], [cand("src.mp3")]
        for name in ("One", "Two"):
            ANSWERS[name] = answer(cover=SRV.url + "big.jpg")
        SRV.fail["/big.jpg"] = [500, 2]                    # the first song's two attempts fail; the picture itself is fine
        r = self.run_job([Track("One", "ZZ Top", duration=30.0), Track("Two", "ZZ Top", duration=30.0)],
                         settings(preset="better", device="other"))
        self.assertEqual({k: v.cover for k, v in r.items()}, {"One": True, "Two": True})
        for f in ("One - ZZ Top.mp3", "Two - ZZ Top.mp3"):
            self.assertTrue(read_info(self.path(f)).cover, f)

    def test_a_catalogue_that_could_not_be_asked_is_asked_again_before_giving_up(self):
        PLAN["Gimme"] = [cand("src.mp3")]
        calls = []

        def flaky():
            calls.append(1)
            if len(calls) < 2:                              # rate-limited at first
                return {"v": catalog.RELEASE_V, "failed": True, "miss": False, "t": 0, "durations": [], "covers": [],
                        "releases": [], "album": "", "year": "", "genre": "", "isrc": ""}
            return answer(cover=SRV.url + "big.jpg")
        ANSWERS["Gimme"] = flaky
        r = self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="other"))
        self.assertTrue(r["Gimme"].cover, "artwork found on the retry")
        info = read_info(self.path("Gimme - ZZ Top.mp3"))
        self.assertEqual((info.album, info.album_artist, info.track_total, info.cover), ("Eliminator", "ZZ Top", 11, True))
        self.assertGreaterEqual(len(calls), 2)

    def test_the_picture_inside_the_download_is_the_last_resort_before_none(self):
        PLAN["Gimme"] = [cand("withart.mp3")]
        ANSWERS["Gimme"] = answer(cover="")                 # no service, no catalogue picture
        r = self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="apple"))
        self.assertTrue(r["Gimme"].cover and not r["Gimme"].no_cover)
        im = opened(extract_cover(self.path("Gimme - ZZ Top.m4a")))
        self.assertEqual(im.size, (900, 900))

    def test_without_any_picture_the_song_is_flagged_not_failed(self):
        PLAN["Gimme"] = [cand("src.mp3")]
        ANSWERS["Gimme"] = answer(cover="")
        r = self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="other"))
        self.assertEqual((r["Gimme"].status, r["Gimme"].no_cover), ("ok", True))

    def test_the_service_picture_comes_first_and_is_asked_for_at_the_right_size(self):
        PLAN["Gimme"] = [cand("src.mp3")]
        ANSWERS["Gimme"] = answer(cover=SRV.url + "small.png")
        t = Track("Gimme", "ZZ Top", duration=30.0, artwork=SRV.url + "big.jpg")
        self.run_job([t], settings(preset="better", device="other"))
        self.assertEqual(opened(extract_cover(self.path("Gimme - ZZ Top.mp3"))).size, (1000, 1000))
        self.assertEqual(SRV.requests_for("small.png"), [], "service art was good, the catalogue was never asked for a picture")

    def test_a_service_album_is_not_overwritten_by_the_catalogue(self):
        PLAN["Gimme"] = [cand("src.mp3")]
        a = answer(cover=SRV.url + "big.jpg")
        a["releases"].append(dict(RELEASES[0], album="ZZ Top's Greatest Hits", year="1992", date="1992", track_no=3, track_total=18,
                                  cover=SRV.url + "small.png"))
        ANSWERS["Gimme"] = a
        t = Track("Gimme", "ZZ Top", duration=30.0, album="ZZ Top's Greatest Hits")
        self.run_job([t], settings(preset="better", device="other"))
        i = read_info(self.path("Gimme - ZZ Top.mp3"))
        self.assertEqual((i.album, i.track_no, i.track_total, i.year), ("ZZ Top's Greatest Hits", 3, 18, "1992"))
        self.assertEqual(opened(extract_cover(self.path("Gimme - ZZ Top.mp3"))).size, (600, 600), "that album's own cover")

    def test_a_complete_track_keeps_its_own_details_whatever_the_catalogue_says(self):
        PLAN["Gimme"] = [cand("src.mp3")]
        other = answer(cover=SRV.url + "small.png")
        other["releases"] = [dict(other["releases"][0], album="Some Compilation", year="2011", date="2011", track_no=9, track_total=40)]
        ANSWERS["Gimme"] = other
        t = Track("Gimme", "ZZ Top", duration=30.0, album="Eliminator", year="1983", track_no=1, track_total=11, genre="Rock",
                  album_artist="ZZ Top", artwork=SRV.url + "big.jpg")
        self.run_job([t], settings(preset="better", device="other"))
        i = read_info(self.path("Gimme - ZZ Top.mp3"))
        self.assertEqual((i.album, i.year, i.track_no, i.track_total, i.album_artist), ("Eliminator", "1983", 1, 11, "ZZ Top"))

    # ---- files made before: a re-run completes them

    def first_run_without_details(self, name="Gimme", artist="ZZ Top"):
        """A file as the program used to leave it: tagged with a title and artist only, and no picture."""
        PLAN[name] = [cand("src.mp3")]
        nothing = answer(cover="")
        nothing.update(releases=[], album="", album_artist="", date="", year="", track_no=0, track_total=0, disc_no=0,
                       disc_total=0, genre="", explicit=0, miss=True)
        ANSWERS[name] = nothing
        self.run_job([Track(name, artist, duration=30.0)], settings(preset="better", device="other"))
        p = self.path(f"{name} - {artist}.mp3")
        i = read_info(p)
        self.assertEqual((i.album, i.album_artist, i.track_total, i.cover), ("", "", 0, False))
        lib = Library(self.out)                                  # (the 'looked recently' marks would hold the re-run back a week)
        rec = lib.track(f"{name} - {artist}.mp3")
        for k in ("cover_tried", "details_tried"):
            rec[k] = 0
        lib.set_track(f"{name} - {artist}.mp3", rec)
        lib.save(force=True)
        return p

    def test_a_file_without_details_or_artwork_is_completed_when_it_is_run_again(self):
        p = self.first_run_without_details()
        ANSWERS["Gimme"] = answer(cover=SRV.url + "big.jpg")
        r = self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="other", verify=True))
        self.assertEqual((r["Gimme"].status, r["Gimme"].note), ("fixed", "Details + Artwork added"))
        i = read_info(p)
        self.assertEqual((i.title, i.artist, i.album, i.album_artist, i.date, i.track_no, i.track_total, i.disc_total, i.genre),
                         ("Gimme", "ZZ Top", "Eliminator", "ZZ Top", "1983-03-23", 1, 11, 1, "Rock"))
        self.assertEqual(opened(extract_cover(p)).size, (1000, 1000))
        self.assertEqual(str(ID3(p, translate=False)["TYER"]), "1983")
        self.assertEqual(os.listdir(self.out).count("Gimme - ZZ Top.mp3"), 1, "the same file, not a new download")
        self.assertEqual(SRV.requests_for("src.mp3") and len(SRV.requests_for("src.mp3")) > 0, True)

    def test_what_a_file_already_says_is_never_changed_only_its_blanks_are_filled(self):
        p = self.first_run_without_details()
        write(p, Tagset(album="My Own Album", year="1999", track_no=7), None, merge=True)
        ANSWERS["Gimme"] = answer(cover=SRV.url + "big.jpg")
        self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="other", verify=True))
        i = read_info(p)
        self.assertEqual((i.album, i.year, i.track_no), ("My Own Album", "1999", 7), "the file's own words stay")
        self.assertEqual((i.album_artist, i.genre, i.track_total), ("ZZ Top", "Rock", 0),
                         "blanks that are safe to fill are filled; a total from some other album is not")
        self.assertFalse(i.cover, "and neither is another album's picture")

    def test_a_year_alone_is_completed_to_the_full_date_when_the_year_agrees(self):
        p = self.first_run_without_details()
        write(p, Tagset(album="Eliminator", year="1983", track_no=1), None, merge=True)
        ANSWERS["Gimme"] = answer(cover=SRV.url + "big.jpg")
        self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="other", verify=True))
        i = read_info(p)
        self.assertEqual((i.date, i.track_total), ("1983-03-23", 11))

    def test_a_file_that_already_has_everything_is_not_looked_up_again(self):
        PLAN["Gimme"] = [cand("src.mp3")]
        ANSWERS["Gimme"] = answer(cover=SRV.url + "big.jpg")
        self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="other"))
        asked = []
        ANSWERS["Gimme"] = lambda: asked.append(1) or answer(cover=SRV.url + "big.jpg")
        before = os.path.getmtime(self.path("Gimme - ZZ Top.mp3"))
        r = self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], settings(preset="better", device="other", verify=True))
        self.assertEqual((r["Gimme"].status, asked, os.path.getmtime(self.path("Gimme - ZZ Top.mp3"))), ("skipped", [], before))

    def test_a_catalogue_that_knows_nothing_is_not_asked_again_for_a_week(self):
        p = self.first_run_without_details()
        asked = []
        nothing = ANSWERS["Gimme"]
        ANSWERS["Gimme"] = lambda: asked.append(1) or dict(nothing)
        st = settings(preset="better", device="other", verify=True)
        r = self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], st)
        self.assertEqual((r["Gimme"].status, r["Gimme"].note), ("kept", "No artwork found"), "and nothing is invented")
        first = len(asked)
        self.run_job([Track("Gimme", "ZZ Top", duration=30.0)], st)
        self.assertEqual(len(asked), first, "…and it is remembered")

    def test_video_thumbnails_are_trimmed_to_the_cover(self):
        job = engine.Job([], self.out, settings(), emit=lambda e: None)
        got = job._make_cover("youtube", SRV.url + "topic.jpg", True, 1000)
        self.assertAlmostEqual(got.source_side, 720, delta=8)
        raw = job._make_cover("catalog", SRV.url + "topic.jpg", False, 1000)
        self.assertEqual(raw.source_side, 720, "(a non-video picture is only squared, never bar-trimmed)")
        self.assertGreater(len(opened(raw.jpeg).getcolors(1 << 20) or []), 0)

    def test_a_missing_picture_is_simply_unusable_a_failing_server_is_transient(self):
        job = engine.Job([], self.out, settings(), emit=lambda e: None)
        self.assertIsNone(job._make_cover("catalog", SRV.url + "nothing-here.jpg", False, 1000))
        SRV.fail["/big.jpg"] = [500, 5]
        with self.assertRaises(engine._Transient):
            job._make_cover("catalog", SRV.url + "big.jpg", False, 1000)


if __name__ == "__main__":
    unittest.main()
