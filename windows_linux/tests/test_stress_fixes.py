"""Regression tests for the stress-round fixes: two runs in one folder, the song folder's record, the shield's repair
and its refusal to re-seal a tampered tree, settings files with a BOM or deep nesting, the lock's counts and message,
the frame loop, and the swap. All in temp folders, no network."""
import json
import os
import time
import unittest
from unittest import mock

from helpers import fresh_dir
from test_shield import make_tree

from musicdl import platform_  # noqa: F401  (sets up the temp home before the config is used)
from musicdl.config import Settings
from musicdl.core import engine, library, shield
from musicdl.ui import shell as shellmod
from musicdl.ui.protect import ProtectMixin


class TempFolderTests(unittest.TestCase):
    def setUp(self):
        self.out = fresh_dir("fix_temp_out")

    def test_two_runs_get_their_own_temp_folder(self):
        a = engine.Job([], self.out, Settings(), emit=lambda e: None)
        b = engine.Job([], self.out, Settings(), emit=lambda e: None)
        self.assertNotEqual(a.tmp_root, b.tmp_root)
        self.assertEqual(os.path.dirname(a.tmp_root), self.out)

    def test_stale_temp_folders_are_cleared_fresh_ones_and_songs_kept(self):
        old = os.path.join(self.out, ".musicdl_tmp-old")
        fresh = os.path.join(self.out, ".musicdl_tmp-fresh")
        artist = os.path.join(self.out, "Artist")
        for d in (old, fresh, artist):
            os.makedirs(d)
        long_ago = time.time() - 7 * 3600
        os.utime(old, (long_ago, long_ago))
        engine._clear_stale_temp(self.out)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(artist))


class SongFolderRecordTests(unittest.TestCase):
    def setUp(self):
        self.out = fresh_dir("fix_record_out")
        self.path = os.path.join(self.out, library.STATE_NAME)

    def disk(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_two_runs_keep_each_others_records(self):
        a = library.Library(self.out)
        b = library.Library(self.out)
        a.set_track("A", {"status": "ok"})
        a.save(force=True)
        b.set_track("B", {"status": "ok"})
        b.save(force=True)
        self.assertEqual(set(self.disk()["tracks"]), {"A", "B"})

    def test_a_forgotten_track_stays_forgotten(self):
        a = library.Library(self.out)
        a.set_track("X", {"status": "ok"})
        a.save(force=True)
        b = library.Library(self.out)                      # has X in memory from the start
        b.forget_track("X")
        b.save(force=True)
        self.assertNotIn("X", self.disk()["tracks"])

    def test_the_folder_listing_is_this_runs_alone(self):
        a = library.Library(self.out)
        a.set_files({"gone.mp3": [1, 2]})
        a.save(force=True)
        b = library.Library(self.out)
        b.set_files({})
        b.save(force=True)
        self.assertEqual(self.disk()["files"], {})


class ShieldRepairTests(unittest.TestCase):
    def setUp(self):
        self.root = make_tree("fix_shield_app")
        self.sdir = fresh_dir("fix_shield_store")
        shield.check(self.root, self.sdir)                 # sealed, snapshot taken

    def tearDown(self):
        shield.lock(self.root, on=False)

    def at(self, rel):
        return shield.resolve(self.root, rel)

    def test_a_deleted_file_comes_back_while_another_file_is_changed(self):
        os.remove(os.path.join(self.sdir, shield.SNAP_INFO))
        os.remove(self.at("musicdl/ui/app.py"))
        with open(self.at("musicdl/config.py"), "w") as fh:          # changed: the snapshot is not refreshed now
            fh.write("A = 9\n")
        rep = shield.check(self.root, self.sdir)
        self.assertEqual(rep.restored, ["musicdl/ui/app.py"])
        self.assertEqual(rep.changed, ["musicdl/config.py"])
        self.assertTrue(os.path.isfile(self.at("musicdl/ui/app.py")))

    def test_boot_restores_without_snapshot_info(self):
        os.remove(os.path.join(self.sdir, shield.SNAP_INFO))
        os.remove(self.at("musicdl/ui/app.py"))
        self.assertEqual(shield.boot(self.root, self.sdir), ["musicdl/ui/app.py"])

    def test_a_tampered_tree_is_not_re_sealed_when_its_file_list_is_gone(self):
        os.remove(os.path.join(self.root, shield.DIR, shield.MANIFEST))
        os.remove(os.path.join(self.sdir, shield.SNAP_INFO))
        with open(self.at("musicdl/ui/app.py"), "w") as fh:
            fh.write("print('tampered')\n")
        zip_path = os.path.join(self.sdir, shield.SNAPSHOT)
        with open(zip_path, "rb") as fh:
            before = fh.read()
        rep = shield.check(self.root, self.sdir)
        self.assertEqual(rep.state, "attention")
        self.assertFalse(os.path.exists(os.path.join(self.root, shield.DIR, shield.MANIFEST)))
        with open(zip_path, "rb") as fh:
            self.assertEqual(fh.read(), before)


class SettingsFileTests(unittest.TestCase):
    def setUp(self):
        self.dir = fresh_dir("fix_settings")
        self.path = os.path.join(self.dir, "settings.json")
        patch = mock.patch.object(Settings, "path", staticmethod(lambda: self.path))
        patch.start()
        self.addCleanup(patch.stop)

    def write(self, raw):
        with open(self.path, "wb") as fh:
            fh.write(raw)

    def test_a_byte_order_mark_is_not_damage(self):
        self.write(b"\xef\xbb\xbf" + json.dumps({"volume": 11}).encode("utf-8"))
        self.assertEqual(Settings.load().volume, 11)
        self.assertTrue(os.path.exists(self.path))

    def test_deep_nesting_is_set_aside_not_a_crash(self):
        self.write(b"[" * 100000 + b"]" * 100000)
        self.assertIsInstance(Settings.load(), Settings)
        self.assertFalse(os.path.exists(self.path))

    def test_set_aside_keeps_earlier_damaged_copies(self):
        for body in (b"{bad", b"{worse"):
            self.write(body)
            self.assertTrue(shield.set_aside(self.path))
        kept = [n for n in os.listdir(self.dir) if n.startswith("settings.json.damaged")]
        self.assertEqual(len(kept), 2)

    def test_a_failed_save_leaves_no_temp_file(self):
        s = Settings()
        s.auto_info = {"x": {1, 2}}                        # not JSON
        with self.assertRaises(TypeError):
            s.save()
        self.assertFalse(os.path.exists(self.path + ".tmp"))


class LockCountTests(unittest.TestCase):
    def test_a_failed_unlock_is_not_counted(self):
        root = make_tree("fix_lock_app")
        shield.lock(root, True)
        locked = shield.lock_state(root)[0]
        self.assertGreater(locked, 0)
        with mock.patch.object(shield.os, "chmod", side_effect=OSError("denied")):
            self.assertEqual(shield.lock(root, False), 0)
        self.assertEqual(shield.lock_state(root)[0], locked)
        shield.lock(root, False)

    def test_the_message_says_what_stayed_locked(self):
        root = make_tree("fix_lock_msg")
        shield.lock(root, True)

        class Page(ProtectMixin):
            def __init__(self):
                self.s = mock.Mock(shield_lock=True)
                self.shield_msg = ""

            def shield_root(self):
                return root

            def _settings_refresh(self):
                pass
        page = Page()
        with mock.patch.object(shield, "_flags_off", return_value=False):
            page.set_shield_lock(False)
        self.assertIn("could not be unlocked", page.shield_msg)
        shield.lock(root, False)


class FrameFixTests(unittest.TestCase):
    def shell(self):
        sh = shellmod.Shell.__new__(shellmod.Shell)
        sh.alive, sh.anims, sh.busy, sh.W = True, {}, False, 100
        sh.on_tick = lambda now, dt: None
        sh._loop_job = None
        sh._last = time.monotonic()
        sh.root = mock.Mock()
        sh.root.winfo_viewable.return_value = True
        return sh

    def test_a_replacement_started_in_the_same_frame_survives(self):
        sh = self.shell()
        started = []

        def other_update(v):                                  # another animation replaces "k" while "k" is finishing
            if not started:
                started.append(True)
                sh.animate("k", 10.0, lambda v: None)
        sh.anims["a"] = dict(t0=time.monotonic(), dur=10.0, update=other_update, ease=lambda f: f, done=None)
        sh.anims["k"] = dict(t0=time.monotonic() - 20, dur=0.5, update=lambda v: None, ease=lambda f: f, done=None)
        sh._loop()
        self.assertIn("k", sh.anims)
        self.assertEqual(sh.anims["k"]["dur"], 10.0)
        self.assertTrue(sh.root.after.called)

    def test_a_finish_callback_that_starts_an_animation_keeps_it(self):
        sh = self.shell()

        def first_done():
            sh.animate("k", 10.0, lambda v: None)
        sh.anims["k"] = dict(t0=time.monotonic() - 20, dur=0.5, update=lambda v: None, ease=lambda f: f, done=first_done)
        sh._loop()
        self.assertEqual(sh.anims["k"]["dur"], 10.0)

    def test_animations_use_the_monotonic_clock(self):
        sh = self.shell()
        with mock.patch("time.time", return_value=0.0):      # a wall clock set far back changes nothing
            sh.animate("k", 1.0, lambda v: None)
        self.assertAlmostEqual(sh.anims["k"]["t0"], time.monotonic(), delta=1.0)

    def test_an_unanimated_swap_cancels_a_pending_fade_out(self):
        sh = self.shell()
        calls = []
        sh.anims[("dismiss", "t")] = dict(t0=0, dur=1, update=lambda v: None, ease=lambda f: f,
                                          done=lambda: calls.append("old page"))
        sh.swap("t", lambda: calls.append("new page"), animate=False)
        self.assertNotIn(("dismiss", "t"), sh.anims)
        self.assertEqual(calls, ["new page"])


class WrapCacheTests(unittest.TestCase):
    class Font:
        def __init__(self, name):
            self.name, self.calls = name, 0

        def __str__(self):
            return self.name

        def measure(self, text):
            self.calls += 1
            return len(text) * 7

    def test_wrapping_is_remembered_per_font_and_width(self):
        from musicdl.ui import rows
        text = "one two three four five six seven"
        f = self.Font("f1")
        first = rows.wrap(text, f, 60)
        measured = f.calls
        second = rows.wrap(text, f, 60)
        self.assertEqual(first, second)
        self.assertEqual(f.calls, measured)                 # the second time, nothing is measured
        second.append("changed")                            # a copy: the cache is not changed by callers
        self.assertEqual(rows.wrap(text, f, 60), first)
        g = self.Font("f2")                                 # another font object is measured for itself
        rows.wrap(text, g, 60)
        self.assertGreater(g.calls, 0)


if __name__ == "__main__":
    unittest.main()
