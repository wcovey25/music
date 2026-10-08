"""
welcome.py — the first run.

The first time the app opens, a few short sheets say what it needs and what protects it:

  1. where songs go — the folder, a chance to pick another, and a check that the app can write there (Windows'
     Controlled folder access can block a program from the Music folder; better to find out now than mid-download);
  2. protection — lock the app's own files (read-only) and, if a cleaner or scanner is installed, say how to keep it
     away from them.

Every step can be skipped and everything is also in Settings. Nothing here changes a system setting. The file check at
every start, and the Protection group in Settings, are in protect.py; this mixin only adds the walk-through, and holds
back the file check's own sheet until the walk-through is over so two questions never stack.

(The macOS edition has a third step, permission to control Apple Music, and clears macOS' download flag; neither exists
on Windows.)
"""
import logging
import os
import tempfile
from tkinter import filedialog

from ..core import shield

log = logging.getLogger("musicdl")


def short(path, limit=44):
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    return path if len(path) <= limit else "…" + path[-(limit - 1):]


def can_write(folder):
    """'ok' | 'denied' | 'missing' for the folder songs go to (or, if it doesn't exist yet, the nearest folder above it
    that does: that is where it will be made). A real file is written and removed — listing a folder is not enough on
    Windows, where Controlled folder access lets a program read a protected folder but not write to it."""
    while folder and not os.path.isdir(folder) and os.path.dirname(folder) != folder:
        folder = os.path.dirname(folder)
    if not folder or not os.path.isdir(folder):
        return "missing"
    try:
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".musicdl-check-", delete=True):
            pass
        return "ok"
    except PermissionError:
        return "denied"
    except OSError:
        return "denied" if shield.folder_access(folder) == "denied" else "missing"


class WelcomeMixin:
    WELCOME_DELAY = 1400                                    # ms after the window is shown

    welcome_step = None

    def _init_welcome(self):
        self.welcome_step = None

    def maybe_welcome(self):
        if self.s.welcomed or self.sheet_state or self.splashing:
            return
        self.welcome_step = "hello"
        self.open_sheet("Welcome to Music Downloader",
                        ["A minute of setup, so nothing interrupts you later: a folder for your songs and the app’s own "
                         "files.", "Everything here can be changed afterwards in Settings."],
                        [("skip", "Not now", "glass"), ("go", "Continue", "primary")], self._welcome_pick, default="skip")

    def finish_welcome(self):
        self.welcome_step = None
        self.s.welcomed = True
        self.s.save()
        rep = self.shield_report                            # the start-up check waited for the walk-through
        if rep is not None and rep.state == "attention" and not self.shield_noted and not self.sheet_state:
            self.shield_noted = True
            self.shield_sheet(rep)

    def _welcome_pick(self, key):
        step, skip = self.welcome_step, key == "skip"
        if step == "hello":
            return self.finish_welcome() if skip else self.welcome_folder()
        if step == "folder":
            if key == "change":
                return self.welcome_choose()
            return self.welcome_protect() if skip else self.welcome_folder_answer()
        if step == "protect":
            return self.finish_welcome() if skip else self.welcome_protect_answer()
        self.finish_welcome()

    # ---- 1. the folder
    def welcome_folder(self, note=""):
        self.welcome_step = "folder"
        paras = [f"Songs are saved to {short(self.s.outdir)}."]
        if note:
            paras.insert(0, note)
        paras.append("Keep it, or choose another folder — an external drive works too.")
        self.open_sheet("Where your songs go", paras,
                        [("change", "Choose…", "glass"), ("go", "Continue", "primary")], self._welcome_pick,
                        default="go", follow=True)

    def welcome_choose(self):
        path = filedialog.askdirectory(parent=self.root, initialdir=self.s.outdir or None, title="Save songs to")
        if path:
            self.s.outdir = os.path.normpath(path)
            self.s.save()
        self.welcome_folder()

    def welcome_folder_answer(self):
        state = can_write(self.s.outdir)
        if state == "denied":
            self.welcome_step = "folder-denied"
            self.open_sheet("Windows blocked that folder",
                            [f"Music Downloader can’t save to {short(self.s.outdir)}.",
                             "Windows Security’s Controlled folder access may be protecting it: allow the app in Ransomware "
                             "protection › Allow an app, or choose another folder."],
                            [("later", "Not now", "glass"), ("change", "Choose…", "primary")],
                            self._welcome_denied, default="later", follow=True)
            return
        self.welcome_protect()

    def _welcome_denied(self, key):
        if key == "change":
            self.welcome_step = "folder"
            return self.welcome_choose()
        self.welcome_protect()

    # ---- 2. protection
    def welcome_protect(self):
        if self.shield_root() is None:
            return self.finish_welcome()
        self.welcome_step = "protect"
        paras = ["Music Downloader keeps a sealed copy of its own files and checks them every time it opens. If a cleaner "
                 "or an update deletes one, it puts it back — and it can make the files read-only so ordinary programs "
                 "can’t remove them."]
        cleaners = self.cleaner_note()
        if cleaners:
            paras.append(cleaners)
        self.open_sheet("Protect the app", paras,
                        [("skip", "Not now", "glass"), ("go", "Protect", "primary")], self._welcome_pick, default="skip",
                        follow=True)

    def welcome_protect_answer(self):
        self.set_shield_lock(True)
        self.start_shield()
        self.welcome_step = "done"
        self.open_sheet("You’re set", ["The app’s files are locked.",
                                       "Settings › Protection shows the status, lets you check or unlock, and lists the "
                                       "folders to add to a cleaner’s or scanner’s exclusions."],
                        [("ok", "Done", "primary")], lambda k: self.finish_welcome(), default="ok", follow=True)
