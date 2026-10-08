"""Protection (core/shield.py): a made-up installed tree is sealed, damaged in the ways cleaners damage apps, and checked.
Also: the read-only lock, the start-up step in MusicDownloader.pyw, the recovery .bat, settings and song-folder record
backups, and the cleaner/scanner list. Everything happens in temp folders."""
import json
import os
import shutil
import subprocess
import sys
import unittest
import zipfile

from helpers import HOME, ROOT, fresh_dir

from musicdl import platform_
from musicdl.config import Settings
from musicdl.core import shield
from musicdl.core.library import Library


def make_tree(name):
    """A small installed copy: the launcher, setup script and a few package files (one in a subfolder)."""
    root = fresh_dir(name)
    files = {"MusicDownloader.pyw": "print('start')\n", "setup_windows.bat": "@echo off\n", "requirements.txt": "x\n",
             "musicdl/__init__.py": "", "musicdl/config.py": "A = 1\n", "musicdl/core/shield.py": "# checker\n",
             "musicdl/ui/app.py": "B = 2\n"}
    for rel, body in files.items():
        path = shield.resolve(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(body)
    os.makedirs(os.path.join(root, "musicdl", "__pycache__"))
    with open(os.path.join(root, "musicdl", "__pycache__", "config.cpython-311.pyc"), "wb") as fh:
        fh.write(b"\0")
    return root


class Tree(unittest.TestCase):
    def setUp(self):
        self.root = make_tree("shield_app")
        self.sdir = fresh_dir("shield_store")

    def tearDown(self):
        shield.lock(self.root, on=False)


class SealTests(Tree):
    def test_the_program_files_and_nothing_else(self):
        files = shield.code_files(self.root)
        self.assertIn("MusicDownloader.pyw", files)
        self.assertIn("musicdl/ui/app.py", files)
        self.assertFalse(any("__pycache__" in f or f.endswith(".pyc") for f in files))

    def test_a_first_check_seals_and_keeps_a_copy(self):
        rep = shield.check(self.root, self.sdir)
        self.assertEqual(rep.state, "ok")
        self.assertTrue(rep.sealed_now)
        self.assertTrue(os.path.isfile(os.path.join(self.sdir, shield.SNAPSHOT)))
        self.assertEqual(rep.summary(), "Everything is in place")

    def test_a_deleted_file_is_put_back(self):
        shield.check(self.root, self.sdir)
        os.remove(os.path.join(self.root, "musicdl", "ui", "app.py"))
        rep = shield.check(self.root, self.sdir)
        self.assertEqual((rep.state, rep.restored), ("restored", ["musicdl/ui/app.py"]))
        with open(os.path.join(self.root, "musicdl", "ui", "app.py")) as fh:
            self.assertEqual(fh.read(), "B = 2\n")
        self.assertEqual(rep.summary(), "Put back 1 missing file")

    def test_a_whole_folder_deleted_comes_back(self):
        shield.check(self.root, self.sdir)
        shutil.rmtree(os.path.join(self.root, "musicdl", "ui"))
        rep = shield.check(self.root, self.sdir)
        self.assertEqual(rep.state, "restored")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "musicdl", "ui", "app.py")))

    def test_a_changed_file_is_only_reported(self):
        shield.check(self.root, self.sdir)
        with open(os.path.join(self.root, "musicdl", "config.py"), "w") as fh:
            fh.write("A = 99\n")
        rep = shield.check(self.root, self.sdir)
        self.assertEqual((rep.state, rep.changed), ("attention", ["musicdl/config.py"]))
        with open(os.path.join(self.root, "musicdl", "config.py")) as fh:
            self.assertEqual(fh.read(), "A = 99\n", "it might be an edit the user wanted")

    def test_the_user_can_put_a_changed_file_back(self):
        shield.check(self.root, self.sdir)
        with open(os.path.join(self.root, "musicdl", "config.py"), "w") as fh:
            fh.write("A = 99\n")
        done, lost = shield.repair_changed(self.root, self.sdir)
        self.assertEqual((done, lost), (["musicdl/config.py"], []))
        self.assertEqual(shield.check(self.root, self.sdir).state, "ok")

    def test_or_keep_it(self):
        shield.check(self.root, self.sdir)
        with open(os.path.join(self.root, "musicdl", "config.py"), "w") as fh:
            fh.write("A = 99\n")
        shield.accept_changes(self.root, self.sdir)
        self.assertEqual(shield.check(self.root, self.sdir).state, "ok")
        os.remove(os.path.join(self.root, "musicdl", "config.py"))
        shield.check(self.root, self.sdir)
        with open(os.path.join(self.root, "musicdl", "config.py")) as fh:
            self.assertEqual(fh.read(), "A = 99\n", "the kept version is the one restored from now on")

    def test_an_update_ships_its_own_manifest_and_is_not_damage(self):
        shield.check(self.root, self.sdir)
        with open(os.path.join(self.root, "musicdl", "config.py"), "w") as fh:
            fh.write("A = 2  # new version\n")
        shield.seal(self.root)                                     # what an update does
        rep = shield.check(self.root, self.sdir)
        self.assertEqual((rep.state, rep.updated), ("ok", True))
        self.assertIn("updated", rep.summary())

    def test_a_deleted_manifest_comes_back_from_the_copy(self):
        shield.check(self.root, self.sdir)
        os.remove(os.path.join(self.root, shield.DIR, shield.MANIFEST))
        rep = shield.check(self.root, self.sdir)
        self.assertEqual((rep.state, rep.note), ("ok", "manifest restored"))

    def test_a_tampered_copy_is_never_restored(self):
        shield.check(self.root, self.sdir)
        snap = os.path.join(self.sdir, shield.SNAPSHOT)
        with zipfile.ZipFile(snap, "w") as z:
            z.writestr("musicdl/ui/app.py", "evil = True\n")
        os.remove(os.path.join(self.root, "musicdl", "ui", "app.py"))
        rep = shield.check(self.root, self.sdir)
        self.assertEqual((rep.state, rep.lost), ("attention", ["musicdl/ui/app.py"]))
        self.assertFalse(os.path.exists(os.path.join(self.root, "musicdl", "ui", "app.py")))

    def test_what_cannot_be_put_back_is_named(self):
        ff = os.path.join(self.root, ".runtime", "bin", "ffmpeg.exe")
        os.makedirs(os.path.dirname(ff))
        with open(ff, "wb") as fh:
            fh.write(b"\0" * 100)
        shield.check(self.root, self.sdir)
        os.remove(ff)
        rep = shield.check(self.root, self.sdir)
        self.assertEqual(rep.state, "attention")
        self.assertIn("FFmpeg was removed", rep.summary())

    def test_no_tree_nothing_to_protect(self):
        self.assertEqual(shield.check(None, self.sdir).state, "unsealed")


class UpdateRepairTests(Tree):
    def _update(self, rel, body):
        with open(shield.resolve(self.root, rel), "w") as fh:
            fh.write(body)

    def test_a_later_start_keeps_the_updated_copy_of_a_file(self):
        shield.check(self.root, self.sdir)                                  # first run: the old build is kept
        self._update("musicdl/config.py", "A = 2\n")                       # an update changes two files and
        self._update("musicdl/ui/app.py", "B = 3\n")
        shield.seal(self.root)                                              # ships a new manifest
        os.remove(shield.resolve(self.root, "musicdl/config.py"))           # a cleaner takes one during the update
        first = shield.check(self.root, self.sdir)
        self.assertEqual(first.lost, ["musicdl/config.py"])                 # its new copy was never kept: reported
        os.remove(shield.resolve(self.root, "musicdl/ui/app.py"))           # later a cleaner takes the other updated file
        second = shield.check(self.root, self.sdir)
        self.assertEqual(second.restored, ["musicdl/ui/app.py"])            # ... and it comes back, new content
        with open(shield.resolve(self.root, "musicdl/ui/app.py")) as fh:
            self.assertEqual(fh.read(), "B = 3\n")

    def test_a_changed_file_is_never_snapshotted(self):
        shield.check(self.root, self.sdir)
        self._update("musicdl/config.py", "A = 2\n")                       # an update ships a new manifest ...
        shield.seal(self.root)
        self._update("musicdl/ui/app.py", "B = 9\n")                       # ... and then a file is edited by hand
        rep = shield.check(self.root, self.sdir)
        self.assertEqual(rep.changed, ["musicdl/ui/app.py"])
        self.assertNotEqual(shield.snapshot_info(self.sdir)["build"], shield.read_manifest(self.root)["build"])
        with zipfile.ZipFile(os.path.join(self.sdir, shield.SNAPSHOT)) as z:
            self.assertEqual(z.read("musicdl/ui/app.py"), b"B = 2\n")


class LockTests(Tree):
    def test_lock_and_unlock(self):
        shield.check(self.root, self.sdir)
        n = shield.lock(self.root)
        locked, total = shield.lock_state(self.root)
        self.assertEqual((n, locked), (total, total))
        self.assertTrue(shield.is_locked(os.path.join(self.root, "musicdl", "config.py")))
        shield.lock(self.root, on=False)
        self.assertEqual(shield.lock_state(self.root)[0], 0)

    def test_a_locked_tree_can_still_be_repaired_and_resealed(self):
        shield.check(self.root, self.sdir)
        shield.lock(self.root)
        os.remove(os.path.join(self.root, "musicdl", "ui", "app.py"))
        self.assertEqual(shield.check(self.root, self.sdir).state, "restored")
        shield.accept_changes(self.root, self.sdir)                      # writes the manifest through its lock
        self.assertEqual(shield.check(self.root, self.sdir).state, "ok")


class BootTests(Tree):
    def test_boot_puts_back_what_is_gone_and_touches_nothing_else(self):
        shield.check(self.root, self.sdir)
        os.remove(os.path.join(self.root, "musicdl", "config.py"))
        with open(os.path.join(self.root, "musicdl", "ui", "app.py"), "w") as fh:
            fh.write("edited\n")
        self.assertEqual(shield.boot(self.root, self.sdir), ["musicdl/config.py"])
        with open(os.path.join(self.root, "musicdl", "ui", "app.py")) as fh:
            self.assertEqual(fh.read(), "edited\n")

    def test_boot_without_a_copy_does_nothing(self):
        self.assertEqual(shield.boot(self.root, self.sdir), [])

    def test_the_launcher_restores_a_deleted_checker_before_anything_runs(self):
        """MusicDownloader.pyw's own start-up step, run for real on a copy of the launcher."""
        root = make_tree("shield_boot_app")
        with open(os.path.join(ROOT, "MusicDownloader.pyw")) as fh:
            launcher = fh.read()
        boot_only = launcher.split("_boot()\n\nfrom musicdl")[0] + "_boot()\n"
        with open(os.path.join(root, "MusicDownloader.pyw"), "w") as fh:
            fh.write(boot_only)
        shutil.copyfile(shield.__file__, os.path.join(root, "musicdl", "core", "shield.py"))
        home = fresh_dir("shield_boot_home")
        sdir = os.path.join(home, "shield")
        shield.check(root, sdir)
        os.remove(os.path.join(root, "musicdl", "core", "shield.py"))
        os.remove(os.path.join(root, "musicdl", "config.py"))
        env = dict(os.environ, MUSICDL_HOME=home)
        subprocess.run([sys.executable, os.path.join(root, "MusicDownloader.pyw")], check=True, env=env, timeout=60)
        self.assertTrue(os.path.isfile(os.path.join(root, "musicdl", "core", "shield.py")))
        self.assertTrue(os.path.isfile(os.path.join(root, "musicdl", "config.py")), "taken from the copy at start-up")

    def test_a_developers_checkout_is_never_touched(self):
        os.makedirs(os.path.join(self.root, ".git"))
        saved = os.environ.pop("MUSICDL_SHIELD_ROOT", None)
        try:
            self.assertIsNone(shield.app_root(), "this test run is itself a git checkout")
            os.environ["MUSICDL_SHIELD_ROOT"] = self.root
            self.assertEqual(shield.app_root(), os.path.abspath(self.root))
        finally:
            os.environ.pop("MUSICDL_SHIELD_ROOT", None)
            if saved is not None:
                os.environ["MUSICDL_SHIELD_ROOT"] = saved


class LifelineTests(unittest.TestCase):
    def test_paths_are_quoted_for_cmd_and_powershell(self):
        body = shield.lifeline_text(r"C:\Users\Ann O'Neil\AppData\Roaming\MusicDownloader\shield",
                                    r"C:\Apps\100% Music")
        self.assertIn(r'set "DEST=C:\Apps\100%% Music"', body)
        self.assertIn(r"'C:\Users\Ann O''Neil\AppData\Roaming\MusicDownloader\shield\snapshot.zip'", body)
        self.assertIn("Expand-Archive", body)
        self.assertIn("attrib -R", body, "a locked install is unlocked before it is written over")

    def test_written_with_windows_line_endings(self):
        sdir = fresh_dir("shield_life")
        path = shield.write_lifeline(sdir, r"C:\Apps\Music")
        self.assertEqual(os.path.basename(path), "Restore Music Downloader.bat")
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertIn(b"\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))
        shield._flags_on(path)
        shield.write_lifeline(sdir, r"C:\Apps\Music")                    # rewriting a read-only one works


class SettingsBackupTests(unittest.TestCase):
    def setUp(self):
        self.cfg = fresh_dir("shield_cfg")
        self.sdir = os.path.join(self.cfg, "shield")

    def write(self, data):
        with open(os.path.join(self.cfg, "settings.json"), "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))

    def test_only_different_copies_and_only_three(self):
        for v in range(5):
            self.write({"v": v})
            self.assertTrue(shield.backup_data(self.cfg, self.sdir))
            self.assertFalse(shield.backup_data(self.cfg, self.sdir), "the same file is not copied twice")
        self.assertEqual(len(os.listdir(os.path.join(self.sdir, "data"))), shield.KEEP_DATA)

    def test_a_damaged_file_is_set_aside_and_the_last_good_one_used(self):
        self.write({"v": 1})
        shield.backup_data(self.cfg, self.sdir)
        self.write("{ not json")
        self.assertTrue(shield.heal_settings(self.cfg, self.sdir))
        with open(os.path.join(self.cfg, "settings.json")) as fh:
            self.assertEqual(json.load(fh), {"v": 1})
        with open(os.path.join(self.cfg, "settings.json.damaged")) as fh:
            self.assertEqual(fh.read(), "{ not json")

    def test_a_deleted_file_comes_back(self):
        self.write({"v": 2})
        shield.backup_data(self.cfg, self.sdir)
        os.remove(os.path.join(self.cfg, "settings.json"))
        self.assertTrue(shield.heal_settings(self.cfg, self.sdir))

    def test_a_readable_file_is_left_alone(self):
        self.write({"v": 3})
        self.assertFalse(shield.heal_settings(self.cfg, self.sdir))


class SettingsLoadTests(unittest.TestCase):
    """Settings.load() itself: the real config folder is the test HOME."""

    def setUp(self):
        self.path = Settings.path()
        for p in (self.path, self.path + ".damaged"):
            if os.path.exists(p):
                os.remove(p)
        shutil.rmtree(os.path.join(HOME, "shield", "data"), ignore_errors=True)

    def tearDown(self):
        self.setUp()

    def test_damaged_settings_with_a_backup_load_the_backup(self):
        st = Settings()
        st.bitrate, st.fmt = 192, "mp3"
        st.save()
        shield.backup_data(platform_.config_dir(), shield.shield_dir(platform_.config_dir()))
        with open(self.path, "w") as fh:
            fh.write("[1, 2")
        self.assertEqual(Settings.load().bitrate, 192)

    def test_damaged_settings_without_a_backup_start_afresh_and_keep_the_file(self):
        with open(self.path, "w") as fh:
            fh.write("[1, 2, 3]")
        st = Settings.load()
        self.assertEqual(st.bitrate, Settings().bitrate)
        self.assertTrue(os.path.exists(self.path + ".damaged"))


class LibraryBackupTests(unittest.TestCase):
    def setUp(self):
        self.out = fresh_dir("shield_songs")
        self.sdir = fresh_dir("shield_lib_store")
        self.rec = os.path.join(self.out, shield.LIBRARY_FILE)

    def write(self, data):
        with open(self.rec, "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))

    def test_copies_are_per_folder_and_bounded(self):
        other = fresh_dir("shield_songs2")
        for v in range(5):
            self.write({"tracks": {"a.mp3": {"v": v}}})
            shield.backup_library(self.out, self.sdir)
        with open(os.path.join(other, shield.LIBRARY_FILE), "w") as fh:
            json.dump({"tracks": {}}, fh)
        shield.backup_library(other, self.sdir)
        mine = shield._library_copies(os.path.join(self.sdir, "data"), shield._library_tag(self.out))
        theirs = shield._library_copies(os.path.join(self.sdir, "data"), shield._library_tag(other))
        self.assertEqual((len(mine), len(theirs)), (shield.KEEP_DATA, 1))

    def test_a_damaged_record_is_replaced_by_the_last_good_one(self):
        self.write({"version": 3, "tracks": {"a.mp3": {"kbps": 320}}})
        shield.backup_library(self.out, self.sdir)
        self.write('{"version": 3, "tracks": {"a.mp3"')
        self.assertTrue(shield.heal_library(self.out, self.sdir))
        self.assertTrue(os.path.exists(self.rec + ".damaged"))

    def test_a_deleted_record_stays_deleted(self):
        self.write({"tracks": {}})
        shield.backup_library(self.out, self.sdir)
        os.remove(self.rec)
        self.assertFalse(shield.heal_library(self.out, self.sdir), "deleting it is how a full re-check is asked for")
        self.assertFalse(os.path.exists(self.rec))

    def test_the_library_heals_itself_when_opened(self):
        self.write({"version": 3, "tracks": {"a.mp3": {"kbps": 320}}})
        shield.backup_library(self.out, shield.shield_dir(platform_.config_dir()))
        self.write("garbage")
        self.assertEqual(Library(self.out).track("a.mp3"), {"kbps": 320})


class CleanerListTests(unittest.TestCase):
    def setUp(self):
        self.saved = os.environ.get("MUSICDL_APP_DIRS")

    def tearDown(self):
        if self.saved is None:
            os.environ.pop("MUSICDL_APP_DIRS", None)
        else:
            os.environ["MUSICDL_APP_DIRS"] = self.saved

    def test_installed_cleaners_and_scanners_are_named(self):
        a, b = fresh_dir("pf"), fresh_dir("pf86")
        for d, name in ((a, "CCleaner"), (a, "Windows Defender"), (b, "Malwarebytes"), (b, "Mozilla Firefox")):
            os.makedirs(os.path.join(d, name))
        os.environ["MUSICDL_APP_DIRS"] = os.pathsep.join([a, b, os.path.join(a, "missing")])
        self.assertEqual(platform_.cleaner_apps(), ["Microsoft Defender", "CCleaner", "Malwarebytes"])

    def test_none_installed(self):
        os.environ["MUSICDL_APP_DIRS"] = fresh_dir("pf_empty")
        self.assertEqual(platform_.cleaner_apps(), [])

    def test_the_folders_to_exclude(self):
        self.assertEqual(shield.protect_list("C:/app", "C:/cfg", ""), ["C:/app", "C:/cfg"])
        self.assertEqual(shield.protect_list("C:/app", "C:/cfg", "D:/Music"), ["C:/app", "C:/cfg", "D:/Music"])
        self.assertEqual(shield.folder_access(fresh_dir("acc")), "ok")
        self.assertEqual(shield.folder_access(os.path.join(HOME, "nope")), "missing")


if __name__ == "__main__":
    unittest.main()
