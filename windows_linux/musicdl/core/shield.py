"""
shield.py — keeps the program's own files whole, and its settings safe.

Cleaners, "security" tools and half-finished updates sometimes delete a few of an app's files, or quarantine them, and the
app then fails to start with no hint why. This module is the app's answer, and it is honest about what it can and cannot do:

  seal      at build time, the installed tree gets .shield/manifest.json: every program file's size and SHA-256. A real
            update ships a new manifest with its new files; damage changes files without changing the manifest. That is
            how the two are told apart — nothing has to guess from dates or version numbers.
  snapshot  the first time the sealed tree verifies, a zip of it is kept in the user's settings folder, outside the app.
  check     every start: verify the tree against the manifest. A deleted file is put back from the snapshot at once (an
            update never deletes). A file that was *changed* is only reported — the user decides, because it might be
            an edit they wanted. What cannot be restored offline (the Python environment, ffmpeg) is named.
  lock      optionally, the program's files get Windows' read-only attribute, so a program that deletes or rewrites files
            without asking is refused (Explorer still can, after a "This file is read-only" question). It is the
            strongest thing an app without administrator rights can do, and it is not proof against an administrator.
  boot      MusicDownloader.pyw runs boot() (this file, stdlib only) before it starts the rest of the program, so a tree
            that lost files is restored from the snapshot first; if this file itself is gone, the .pyw takes it from the
            snapshot.
  data      settings.json is copied (last few distinct versions) and put back if it is deleted or unreadable; a damaged
            file is set aside as settings.json.damaged rather than thrown away.

Nothing here needs the network or touches the user's songs. This file imports nothing from the rest of the app, so it
can run as a script:   python shield.py seal|boot|check ROOT [SHIELD_DIR]
"""
import hashlib
import json
import ntpath
import os
import re
import shutil
import stat
import sys
import tempfile
import time
import zipfile

FORMAT = 1
DIR = ".shield"
MANIFEST = "manifest.json"
SNAPSHOT = "snapshot.zip"
SNAP_INFO = "snapshot.json"
KEEP_DATA = 3
SKIP_DIRS = {"__pycache__", ".venv", ".runtime", DIR, "tests", ".git", "_work"}
CODE_FILES = ("MusicDownloader.pyw", "setup_windows.bat", "requirements.txt", "run.sh")
LAUNCHER = "MusicDownloader.pyw"
RUNTIME_FILES = (".venv/bin/python", ".venv/bin/python3", ".venv/Scripts/python.exe")
RUNTIME_BINS = (".runtime/bin/ffmpeg", ".runtime/bin/ffmpeg.exe")


# ---------------------------------------------------------------- where things are

def app_root():
    """The folder that holds MusicDownloader.pyw and musicdl/ — or None when this is not an installed copy (a git
    checkout is a developer's and is never sealed or locked). MUSICDL_SHIELD_ROOT names a tree explicitly (tests)."""
    env = os.environ.get("MUSICDL_SHIELD_ROOT")
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if not os.path.isfile(os.path.join(here, LAUNCHER)):
        return None
    if any(os.path.exists(os.path.join(p, ".git")) for p in (here, os.path.dirname(here))):
        return None
    return here


def resolve(root, rel):
    return os.path.join(root, *rel.split("/"))


def shield_dir(config_dir):
    path = os.path.join(config_dir, "shield")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- the files

def code_files(root):
    """Relative (forward-slash) paths of every file the program is made of, sorted."""
    out = [n for n in CODE_FILES if os.path.isfile(os.path.join(root, n))]
    top = os.path.join(root, "musicdl")
    for base, dirs, names in os.walk(top):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for n in names:
            if n.endswith((".pyc", ".pyo", ".log", ".tmp")) or n.startswith(".") or n.lower() == "desktop.ini":
                continue
            out.append(os.path.relpath(os.path.join(base, n), root).replace(os.sep, "/"))
    return sorted(set(out))


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(root):
    """{rel: [size, sha256]} of the tree as it is now."""
    out = {}
    for rel in code_files(root):
        path = resolve(root, rel)
        try:
            out[rel] = [os.path.getsize(path), digest(path)]
        except OSError:
            pass
    return out


def build_id(files):
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(f"{rel}\0{files[rel][0]}\0{files[rel][1]}\n".encode())
    return h.hexdigest()[:16]


def runtime_state(root):
    """Facts about what cannot be put back offline: {'python': bool, 'ffmpeg': size or 0}."""
    python = any(os.path.isfile(os.path.join(root, *r.split("/"))) for r in RUNTIME_FILES)
    ff = 0
    for r in RUNTIME_BINS:
        try:
            ff = max(ff, os.path.getsize(os.path.join(root, *r.split("/"))))
        except OSError:
            pass
    return {"python": python, "ffmpeg": ff}


# ---------------------------------------------------------------- locking

def _flags_off(path):
    """Make `path` writable and deletable again (clears Windows' read-only attribute)."""
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    except OSError:
        pass


def _flags_on(path):
    """Set the read-only attribute (on Windows, os.chmod without S_IWRITE is exactly that)."""
    try:
        os.chmod(path, os.stat(path).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        return True
    except OSError:
        return False


def locked_paths(root):
    paths = [resolve(root, r) for r in code_files(root)]
    paths.append(os.path.join(root, DIR, MANIFEST))
    return [p for p in paths if p and os.path.isfile(p)]


def is_locked(path):
    try:
        return not os.stat(path).st_mode & stat.S_IWUSR
    except OSError:
        return False


def lock(root, on=True):
    """Lock (or unlock) the program's files. Returns how many files changed."""
    n = 0
    for path in locked_paths(root):
        if on and not is_locked(path):
            n += _flags_on(path)
        elif not on and is_locked(path):
            _flags_off(path)
            n += 1
    return n


def lock_state(root):
    """(locked, total) over the program's files."""
    paths = locked_paths(root)
    return sum(1 for p in paths if is_locked(p)), len(paths)


# ---------------------------------------------------------------- manifest + snapshot

def _write_json(path, data):
    if os.path.exists(path):
        _flags_off(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def seal(root, now=None):
    """Write the manifest for the tree as it is. Run when an app is built or updated. Returns the manifest."""
    files = fingerprint(root)
    run = runtime_state(root)
    manifest = {"format": FORMAT, "build": build_id(files), "sealed": int(now or time.time()), "files": files,
                "ffmpeg": run["ffmpeg"], "python": run["python"]}
    _write_json(os.path.join(root, DIR, MANIFEST), manifest)
    return manifest


def read_manifest(root):
    m = _read_json(os.path.join(root, DIR, MANIFEST))
    if m and m.get("format") == FORMAT and isinstance(m.get("files"), dict):
        return m
    return None


def verify(root, manifest):
    """(missing, changed): relative paths the manifest lists that are gone, or present with other content."""
    missing, changed = [], []
    for rel, (size, sha) in sorted(manifest["files"].items()):
        path = resolve(root, rel)
        if not path:
            continue                                               # (not inside an .app any more: not checked)
        if not os.path.isfile(path):
            missing.append(rel)
            continue
        try:
            if os.path.getsize(path) != size or digest(path) != sha:
                changed.append(rel)
        except OSError:
            missing.append(rel)
    return missing, changed


def snapshot_info(sdir):
    return _read_json(os.path.join(sdir, SNAP_INFO))


def take_snapshot(root, sdir, manifest):
    """Keep a zip of the verified tree (and a copy of its manifest) in `sdir`."""
    os.makedirs(sdir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=sdir, suffix=".tmp")
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in sorted(manifest["files"]):
                path = resolve(root, rel)
                if path and os.path.isfile(path):
                    z.write(path, rel)
        os.replace(tmp, os.path.join(sdir, SNAPSHOT))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    _write_json(os.path.join(sdir, SNAP_INFO), {"build": manifest["build"], "taken": int(time.time()),
                                                "manifest": manifest})


def restore(root, sdir, manifest, rels):
    """Put `rels` back from the snapshot, but only those whose snapshot content is what the manifest expects.
    Returns (restored, lost)."""
    restored, lost = [], []
    zpath = os.path.join(sdir, SNAPSHOT)
    try:
        z = zipfile.ZipFile(zpath)
    except (OSError, zipfile.BadZipFile):
        return [], list(rels)
    with z:
        names = set(z.namelist())
        for rel in rels:
            want = manifest["files"].get(rel)
            path = resolve(root, rel)
            if not want or not path or rel not in names:
                lost.append(rel)
                continue
            try:
                data = z.read(rel)
                if hashlib.sha256(data).hexdigest() != want[1]:
                    lost.append(rel)
                    continue
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if os.path.exists(path):
                    _flags_off(path)
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, path)
                restored.append(rel)
            except OSError:
                lost.append(rel)
    return restored, lost


class Report:
    """What a check found. `state`: ok | restored | attention | unsealed (not an installed app)."""

    def __init__(self):
        self.state = "ok"
        self.missing, self.changed, self.restored, self.lost = [], [], [], []
        self.runtime = []                     # sentences about what cannot be restored offline
        self.updated = False                  # the tree was replaced by a newer build and re-sealed
        self.sealed_now = False               # no manifest existed (a first run): it was made from the tree as found
        self.note = ""

    def problems(self):
        return len(self.changed) + len(self.lost) + len(self.runtime)

    def summary(self):
        if self.state == "unsealed":
            return "Not installed as an app — nothing to protect"
        bits = []
        if self.restored:
            bits.append(f"put back {len(self.restored)} missing file{'s' if len(self.restored) != 1 else ''}")
        if self.changed:
            bits.append(f"{len(self.changed)} file{'s' if len(self.changed) != 1 else ''} changed")
        if self.lost:
            bits.append(f"{len(self.lost)} could not be restored")
        bits += self.runtime
        if not bits:
            return "Everything is in place" + (" · updated" if self.updated else "")
        return bits[0][0].upper() + bits[0][1:] + "".join(" · " + b for b in bits[1:])


def check(root, sdir, repair_missing=True):
    """Verify the installed tree and put back whatever was deleted. See the module doc for the rules."""
    rep = Report()
    if not root or not os.path.isdir(root):
        rep.state = "unsealed"
        return rep
    manifest = read_manifest(root)
    snap = snapshot_info(sdir)
    if manifest is None:
        saved = (snap or {}).get("manifest")
        if saved and saved.get("format") == FORMAT and isinstance(saved.get("files"), dict):
            _write_json(os.path.join(root, DIR, MANIFEST), saved)          # a cleaner took the manifest: take it back
            manifest = saved
            rep.note = "manifest restored"
        else:
            manifest = seal(root)                                           # first run on an install made without one
            rep.sealed_now = True
    missing, changed = verify(root, manifest)
    if snap is None or snap.get("build") != manifest["build"]:
        if not missing and not changed:
            take_snapshot(root, sdir, manifest)                             # a first run, or a real update: keep this one
            rep.updated = snap is not None
            snap = snapshot_info(sdir)
    if missing and repair_missing and snap:
        done, lost = restore(root, sdir, manifest, missing)
        rep.restored += done
        rep.lost += lost
        missing, changed = verify(root, manifest)
    rep.missing, rep.changed = missing, changed
    rep.lost = sorted(set(rep.lost) | set(m for m in missing if m not in rep.restored))
    run = runtime_state(root)
    if manifest.get("python") and not run["python"]:
        rep.runtime.append("the app’s Python environment is missing")
    if manifest.get("ffmpeg") and run["ffmpeg"] != manifest["ffmpeg"]:
        rep.runtime.append("FFmpeg was removed" if not run["ffmpeg"] else "FFmpeg was changed")
    if rep.lost or rep.changed or rep.runtime:
        rep.state = "attention"
    elif rep.restored:
        rep.state = "restored"
    return rep


def repair_changed(root, sdir, rels=None):
    """Put the changed files back to what the manifest says (the user asked for it). Returns (restored, lost)."""
    manifest = read_manifest(root)
    if not manifest:
        return [], list(rels or [])
    if rels is None:
        rels = verify(root, manifest)[1]
    return restore(root, sdir, manifest, rels)


def accept_changes(root, sdir):
    """The user says the changes are intended: seal the tree as it is now and keep a new snapshot."""
    manifest = seal(root)
    take_snapshot(root, sdir, manifest)
    return manifest


# ---------------------------------------------------------------- boot (before any of the program's code runs)

def boot(root, sdir):
    """Put back deleted files from the snapshot, cheaply (existence only). Never overwrites anything."""
    info = snapshot_info(sdir)
    manifest = (read_manifest(root) if root else None) or (info or {}).get("manifest")
    if not info or not manifest or not isinstance(manifest.get("files"), dict):
        return []
    gone = [rel for rel in manifest["files"] if (resolve(root, rel) and not os.path.isfile(resolve(root, rel)))]
    if not gone:
        return []
    restored, _lost = restore(root, sdir, manifest, gone)
    if restored and read_manifest(root) is None:
        _write_json(os.path.join(root, DIR, MANIFEST), manifest)
    return restored


# ---------------------------------------------------------------- the user's settings

def _next_stamp(existing):
    """Now, but always after the newest copy (several saves in one second must not sort below the ones they follow)."""
    newest = max((int(re.search(r"(\d+)\.json$", n).group(1)) for n in existing), default=0)
    return max(int(time.time()), newest + 1)


def backup_data(config_dir, sdir):
    """Keep the last few different, readable copies of settings.json. Returns True when a new copy was made."""
    src = os.path.join(config_dir, "settings.json")
    if _read_json(src) is None:
        return False
    folder = os.path.join(sdir, "data")
    os.makedirs(folder, exist_ok=True)
    existing = sorted((n for n in os.listdir(folder) if re.fullmatch(r"settings-\d+\.json", n)), reverse=True)
    if existing:
        try:
            if digest(os.path.join(folder, existing[0])) == digest(src):
                return False
        except OSError:
            pass
    stamp = _next_stamp(existing)
    shutil.copyfile(src, os.path.join(folder, f"settings-{stamp}.json"))
    for old in sorted(existing + [f"settings-{stamp}.json"], reverse=True)[KEEP_DATA:]:
        try:
            os.unlink(os.path.join(folder, old))
        except OSError:
            pass
    return True


def heal_settings(config_dir, sdir=None):
    """If settings.json is gone or unreadable, put back the newest readable copy. Returns True if it did."""
    src = os.path.join(config_dir, "settings.json")
    if _read_json(src) is not None:
        return False
    folder = os.path.join(sdir or os.path.join(config_dir, "shield"), "data")
    try:
        names = sorted((n for n in os.listdir(folder) if re.fullmatch(r"settings-\d+\.json", n)), reverse=True)
    except OSError:
        return False
    for n in names:
        if _read_json(os.path.join(folder, n)) is not None:
            set_aside(src)
            shutil.copyfile(os.path.join(folder, n), src)
            return True
    return False


def set_aside(path):
    """Keep a damaged file next to where it was, as <name>.damaged (an older one is replaced). True if it moved."""
    if not os.path.exists(path):
        return False
    try:
        _flags_off(path)
        os.replace(path, path + ".damaged")
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- the song folder's own record (<outdir>/.musicdl.json)

LIBRARY_FILE = ".musicdl.json"


def _library_tag(outdir):
    return hashlib.sha256(os.path.normcase(os.path.abspath(outdir)).encode("utf-8")).hexdigest()[:12]


def _library_copies(folder, tag):
    try:
        return sorted((n for n in os.listdir(folder) if re.fullmatch(rf"library-{tag}-\d+\.json", n)), reverse=True)
    except OSError:
        return []


def backup_library(outdir, sdir):
    """Keep the last few different, readable copies of a song folder's record. True when a new copy was made."""
    src = os.path.join(outdir, LIBRARY_FILE)
    if _read_json(src) is None:
        return False
    folder = os.path.join(sdir, "data")
    os.makedirs(folder, exist_ok=True)
    tag = _library_tag(outdir)
    existing = _library_copies(folder, tag)
    if existing:
        try:
            if digest(os.path.join(folder, existing[0])) == digest(src):
                return False
        except OSError:
            pass
    stamp = _next_stamp(existing)
    shutil.copyfile(src, os.path.join(folder, f"library-{tag}-{stamp}.json"))
    for old in sorted(existing + [f"library-{tag}-{stamp}.json"], reverse=True)[KEEP_DATA:]:
        try:
            os.unlink(os.path.join(folder, old))
        except OSError:
            pass
    return True


def heal_library(outdir, sdir):
    """A song folder's record that is there but unreadable is set aside (.damaged) and the newest readable copy put in
    its place. A record that is simply gone is left gone (deleting it is how a full re-check is asked for). True when a
    copy was put back."""
    src = os.path.join(outdir, LIBRARY_FILE)
    if not os.path.exists(src) or _read_json(src) is not None:
        return False
    set_aside(src)
    folder = os.path.join(sdir, "data")
    for n in _library_copies(folder, _library_tag(outdir)):
        if _read_json(os.path.join(folder, n)) is not None:
            try:
                shutil.copyfile(os.path.join(folder, n), src)
                return True
            except OSError:
                return False
    return False


# ---------------------------------------------------------------- folders the user may need to protect by hand

def protect_list(root, config_dir, outdir=""):
    """The folders to add to a cleaner's or scanner's exclusion list (Windows Security › Virus & threat protection ›
    Exclusions, CCleaner › Options › Exclude, …), one per line."""
    out = []
    for p in (root, config_dir, outdir):
        if p and p not in out:
            out.append(p)
    return out


def folder_access(path):
    """'ok' | 'denied' (e.g. Windows' Controlled folder access says no) | 'missing'."""
    if not path:
        return "missing"
    try:
        os.listdir(path)
        return "ok"
    except PermissionError:
        return "denied"
    except OSError:
        return "missing"


# ---------------------------------------------------------------- the way back if the whole app is deleted

LIFELINE_NAME = "Restore Music Downloader.bat"


def _bat(text):
    """A path for a batch file line inside double quotes (% doubled; a double quote cannot occur in a Windows path)."""
    return str(text).replace("%", "%%")


def _ps(text):
    """A path for a PowerShell single-quoted string."""
    return _bat(str(text).replace("'", "''"))


def lifeline_text(sdir, app_dir):
    snap = ntpath.join(sdir, SNAPSHOT)
    return f"""@echo off
rem Music Downloader - put the program back.
rem Run this if Music Downloader was deleted or stopped opening. It unpacks the copy of the program that the app kept
rem into the folder it was installed in. If Python's packages were removed too, run setup_windows.bat there afterwards
rem (that part needs internet).
setlocal
set "SNAP={_bat(snap)}"
set "DEST={_bat(app_dir)}"
if not exist "%SNAP%" (
  echo No saved copy was found at %SNAP%
  pause
  exit /b 1
)
if exist "%DEST%" attrib -R "%DEST%\\*" /S /D >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -LiteralPath '{_ps(snap)}' -DestinationPath '{_ps(app_dir)}' -Force"
if errorlevel 1 (
  echo Couldn't unpack the saved copy.
  pause
  exit /b 1
)
echo Done. Double-click MusicDownloader.pyw in %DEST% to start the app.
pause
"""


def write_lifeline(sdir, app_dir):
    """Write the recovery script next to the snapshot. Returns its path."""
    path = os.path.join(sdir, LIFELINE_NAME)
    if os.path.exists(path):
        _flags_off(path)
    with open(path, "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(lifeline_text(sdir, app_dir))
    return path


# ---------------------------------------------------------------- as a script

def _default_sdir():
    home = os.environ.get("MUSICDL_HOME")
    if not home:
        home = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "MusicDownloader") \
            if sys.platform == "win32" else os.path.join(os.path.expanduser("~"), ".config", "musicdownloader")
    return os.path.join(home, "shield")


def main(argv):
    if len(argv) < 2 or argv[0] not in ("seal", "boot", "check"):
        print(__doc__.strip().splitlines()[-1])
        return 2
    cmd, root = argv[0], os.path.abspath(argv[1])
    sdir = argv[2] if len(argv) > 2 else _default_sdir()
    if cmd == "seal":
        m = seal(root)
        print(f"sealed {len(m['files'])} files, build {m['build']}")
    elif cmd == "boot":
        done = boot(root, sdir)
        if done:
            print(f"put back {len(done)} missing file(s)")
    else:
        rep = check(root, sdir)
        print(rep.summary())
        return 0 if rep.state in ("ok", "restored") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
