"""
protect.py — keeping the app whole: the file check at every start, and Settings › Protection.

start_shield() checks the installed files in the background a few seconds after the window opens (core/shield.py),
backs up settings.json, and tells the person only when something needs a decision. The rest is what the Protection
group in Settings calls: lock the program files (Windows' read-only attribute), check now, copy the folders a cleaner or
scanner should leave alone, and show the recovery script.

Nothing here changes a system setting; the lock only concerns the app's own files. (The macOS edition does this in
welcome.py, together with its first-run sheets; on Windows the first-run sheets are still to come — see PORT_NOTES.md.)
"""
import logging
import os
import threading

from .. import platform_
from ..core import shield

log = logging.getLogger("musicdl")


class ProtectMixin:
    SHIELD_DELAY = 3200                                 # ms after the window is shown

    def _init_protect(self):
        self.shield_report = None
        self.shield_msg = ""                            # the last thing the Protection group said (copied, checked …)
        self.shield_checked = False
        self.shield_noted = False                       # the "needs attention" sheet is shown once per start
        self._cleaners = None

    # ---------------------------------------------------------------- where things are
    def shield_root(self):
        return shield.app_root()

    def shield_dir(self):
        return shield.shield_dir(platform_.config_dir())

    # ---------------------------------------------------------------- the check at start
    def start_shield(self):
        """Verify the installed files and back up the settings, off the interface thread."""
        root, cfg = self.shield_root(), platform_.config_dir()

        def work():
            try:
                sdir = shield.shield_dir(cfg)
                shield.backup_data(cfg, sdir)
                if root is None:
                    return self.runner.post("shield", shield.check(None, sdir))
                rep = shield.check(root, sdir)
                if self.s.shield_lock:
                    shield.lock(root, True)                         # (files an update or a restore wrote are locked too)
                shield.write_lifeline(sdir, root)
                self.runner.post("shield", rep)
            except Exception:
                log.exception("shield check failed")
        threading.Thread(target=work, name="shield", daemon=True).start()

    def on_shield(self, rep):
        self.shield_report, self.shield_checked = rep, True
        if rep.restored:
            log.info("shield: put back %s", ", ".join(rep.restored))
        if rep.state == "attention":
            log.warning("shield: %s (changed %s, lost %s, %s)", rep.summary(), rep.changed, rep.lost, rep.runtime)
            if not self.shield_noted and not self.sheet_state and not self.splashing:
                self.shield_noted = True
                self.shield_sheet(rep)
        self._settings_refresh()

    def shield_status_text(self):
        rep = self.shield_report
        if not self.shield_checked:
            return "Checking…"
        text = rep.summary()
        if rep.state in ("ok", "restored") and self.s.shield_lock and self.shield_root():
            locked, total = shield.lock_state(self.shield_root())
            if total and locked == total:
                text += " · locked"
        return text

    def shield_sheet(self, rep):
        lines = []
        if rep.changed:
            n = len(rep.changed)
            lines.append(f"{n} of the app’s files {'was' if n == 1 else 'were'} changed since it was installed — by an "
                         "update, a cleaner or something else. Restore them to what was installed?")
        if rep.lost:
            lines.append(f"{len(rep.lost)} missing file{'s' if len(rep.lost) != 1 else ''} could not be put back from the "
                         "saved copy.")
        for sentence in rep.runtime:
            lines.append(sentence[0].upper() + sentence[1:] + ". Run setup_windows.bat again to put it back (it needs "
                         "internet).")
        if rep.changed:
            self.open_sheet("Some of the app’s files changed", lines,
                            [("later", "Not now", "glass"), ("restore", "Restore", "primary")], self._shield_pick,
                            default="later")
        else:
            lines.append("The recovery script puts the app back from the copy it saved.")
            self.open_sheet("The app needs repair", lines,
                            [("ok", "Not now", "glass"), ("script", "Show script", "primary")], self._shield_pick,
                            default="ok")

    def _shield_pick(self, key):
        if key == "restore":
            self.shield_restore()
        elif key == "script":
            self.show_recovery()

    def shield_restore(self):
        root = self.shield_root()
        if not root:
            return
        done, lost = shield.repair_changed(root, self.shield_dir())
        if self.s.shield_lock:
            shield.lock(root, True)
        self.shield_msg = f"Restored {len(done)} file{'s' if len(done) != 1 else ''}" + (f" · {len(lost)} could not be" if lost else "")
        self.start_shield()

    # ---------------------------------------------------------------- Settings › Protection
    def shield_check_now(self):
        self.shield_checked = False
        self.shield_msg = ""
        self.start_shield()
        self._settings_refresh()

    def set_shield_lock(self, on):
        root = self.shield_root()
        self.s.shield_lock = bool(on)
        self.s.save()
        if root:
            n = shield.lock(root, bool(on))
            self.shield_msg = (f"Locked {n} files" if on else f"Unlocked {n} files") if n else ""
        self._settings_refresh()

    def show_recovery(self):
        root = self.shield_root()
        if root and not os.path.exists(os.path.join(self.shield_dir(), shield.LIFELINE_NAME)):
            shield.write_lifeline(self.shield_dir(), root)
        platform_.open_path(self.shield_dir())                    # (the folder: opening the script itself would run it)

    def copy_protect_list(self):
        folders = shield.protect_list(self.shield_root(), platform_.config_dir(), self.s.outdir)
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(folders))
        self.shield_msg = f"Copied {len(folders)} folder{'s' if len(folders) != 1 else ''}"
        self._settings_refresh()

    def open_exclusions(self):
        """Windows Security at Virus & threat protection settings, where Exclusions are (it asks for administrator)."""
        if not platform_.open_virus_settings():
            self.shield_msg = "Couldn’t open Windows Security"
            self._settings_refresh()

    def cleaners(self):
        if self._cleaners is None:
            self._cleaners = platform_.cleaner_apps()
        return self._cleaners
