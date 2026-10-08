"""The quality scale and the 'is this song already here?' index (no network)."""
import os
import threading
import unittest
from types import SimpleNamespace as NS

from helpers import fresh_dir, make_audio

from musicdl.config import OutFmt, Settings
from musicdl.core import quality
from musicdl.core.dedupe import FolderIndex, song_key
from musicdl.core.library import Library
from musicdl.core.models import Stopped


class QualityTests(unittest.TestCase):
    def test_delivered_is_the_lower_of_container_and_source(self):
        self.assertEqual(quality.delivered(NS(ext=".mp3", kbps=192, src_kbps=1411)), 192)
        self.assertEqual(quality.delivered(NS(ext=".mp3", kbps=320, src_kbps=128)), 128)
        self.assertEqual(quality.delivered(NS(ext=".flac", kbps=900, src_kbps=128, lossless=True)), 128)
        self.assertEqual(quality.delivered(NS(ext=".flac", kbps=900, src_kbps=0, lossless=True)), quality.LOSSLESS)
        self.assertEqual(quality.delivered(NS(ext=".mp3", kbps=192, src_kbps=0)), 192)

    def test_worth_upgrading(self):
        mp3 = lambda k, s=0: NS(ext=".mp3", kbps=k, src_kbps=s)
        self.assertTrue(quality.worth_upgrading(mp3(128, 1411), 320))
        self.assertTrue(quality.worth_upgrading(mp3(192, 1411), 320))
        self.assertFalse(quality.worth_upgrading(mp3(256, 1411), 320), "256 -> 320 is not worth a re-download")
        self.assertFalse(quality.worth_upgrading(mp3(192, 128), 320), "the source was only 128 kbps")
        self.assertFalse(quality.worth_upgrading(NS(ext=".flac", kbps=900, src_kbps=1411, lossless=True), 1411))
        self.assertTrue(quality.worth_upgrading(mp3(320, 1411), quality.LOSSLESS))
        self.assertFalse(quality.worth_upgrading(mp3(128, 1411), 128), "choosing a lower quality never upgrades")

    def test_aac_counts_a_little_more_than_mp3_at_the_same_bitrate(self):
        aac, mp3 = NS(ext=".m4a", kbps=192, src_kbps=0), NS(ext=".mp3", kbps=192, src_kbps=0)
        self.assertGreater(quality.delivered(aac), quality.delivered(mp3))
        self.assertTrue(quality.is_lossless_file(".m4a", 900))
        self.assertFalse(quality.is_lossless_file(".m4a", 256))
        self.assertTrue(quality.is_lossless_file(".m4a", 0, flagged=True))

    def test_is_better_needs_a_real_improvement(self):
        self.assertTrue(quality.is_better(320, 192))
        self.assertTrue(quality.is_better(quality.LOSSLESS, 320))
        self.assertFalse(quality.is_better(200, 192))
        self.assertFalse(quality.is_better(192, 192))
        self.assertFalse(quality.is_better(128, 192))

    def test_target_and_next_best_tier(self):
        self.assertEqual(quality.target(OutFmt("mp3", kbps=320)), 320)
        self.assertEqual(quality.target(OutFmt("flac")), quality.LOSSLESS)
        self.assertEqual(quality.target(OutFmt("aac", kbps=200)), 240)
        tiers = (96, 128, 160, 192, 224, 256, 320)
        self.assertEqual(quality.lossy_tier(128, tiers), 128)
        self.assertEqual(quality.lossy_tier(130, tiers), 160)
        self.assertEqual(quality.lossy_tier(1411, tiers), 320)
        self.assertEqual(quality.lossy_tier(40, tiers), 96)

    def test_words(self):
        self.assertEqual(quality.describe(NS(ext=".mp3", kbps=192, src_kbps=1411)), "MP3 192 kbps")
        self.assertEqual(quality.describe(NS(ext=".flac", kbps=900, src_kbps=0, lossless=True)), "FLAC lossless")

    def test_settings_quality_name(self):
        self.assertEqual(Settings(preset="best", device="other").quality_name(), "Best — FLAC · lossless")
        self.assertEqual(Settings(preset="best", device="apple").quality_name(), "Best — ALAC · lossless")
        self.assertEqual(Settings(mode="advanced", fmt="mp3", bitrate=320).quality_name(), "MP3 320 kbps")


class SongKeyTests(unittest.TestCase):
    def test_same_song_however_it_is_decorated(self):
        k = song_key("Adele", "Skyfall")
        self.assertTrue(k)
        self.assertEqual(k, song_key("adele", "SKYFALL"))
        self.assertEqual(k, song_key("Adele feat. Someone", "Skyfall"))
        self.assertEqual(k, song_key("Adele", "Skyfall - Remastered 2011"))

    def test_different_versions_are_different_songs(self):
        self.assertNotEqual(song_key("Adele", "Skyfall"), song_key("Adele", "Skyfall (Live)"))
        self.assertNotEqual(song_key("Adele", "Skyfall"), song_key("Adele", "Hello"))
        self.assertNotEqual(song_key("Adele", "Hello"), song_key("Lionel Richie", "Hello"))

    def test_unknown_parts_give_no_key(self):
        self.assertEqual(song_key("", "Skyfall"), "")
        self.assertEqual(song_key("Adele", ""), "")


class FolderIndexTests(unittest.TestCase):
    def setUp(self):
        self.out = fresh_dir("index")
        os.makedirs(os.path.join(self.out, "Sub"))
        make_audio(os.path.join(self.out, "Skyfall - Adele.mp3"), 8, "-b:a", "128k")
        make_audio(os.path.join(self.out, "Sub", "Adele - Hello.mp3"), 8, "-b:a", "64k")
        make_audio(os.path.join(self.out, "Hello - Adele.flac"), 8, "-c:a", "flac")
        with open(os.path.join(self.out, "notes.txt"), "w") as fh:
            fh.write("not music")

    def index(self, lib=None):
        lib = lib or Library(self.out)
        return FolderIndex(self.out, lib, threading.Event(), lambda text: None), lib

    def test_finds_songs_in_any_folder_and_format_best_first(self):
        idx, _ = self.index()
        self.assertEqual([f.rel for f in idx.find("Adele", "Skyfall")], ["Skyfall - Adele.mp3"])
        hello = idx.find("Adele", "Hello")
        self.assertEqual(len(hello), 2)
        self.assertEqual(hello[0].ext, ".flac", "the best copy comes first")
        self.assertEqual(idx.find("Adele", "Rolling in the Deep"), [])

    def test_second_look_reads_nothing_new(self):
        idx, lib = self.index()
        idx.find("Adele", "Hello")
        lib.save(force=True)
        self.assertEqual(len(Library(self.out).files()), 3, "what was read is remembered in the folder's state file")

    def test_deleted_files_drop_out(self):
        _, lib = self.index()
        lib.save(force=True)
        os.remove(os.path.join(self.out, "Skyfall - Adele.mp3"))
        idx, _ = self.index(Library(self.out))
        self.assertEqual(idx.find("Adele", "Skyfall"), [])

    def test_stop_while_indexing(self):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(Stopped):
            FolderIndex(self.out, Library(self.out), stop, lambda text: None)


if __name__ == "__main__":
    unittest.main()
