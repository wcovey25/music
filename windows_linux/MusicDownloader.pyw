import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _boot():
    """Put back program files that were deleted, from the copy the app kept (musicdl/core/shield.py), before any of
    them is imported. If the checker itself is gone, it comes out of that copy first. Never stops the app starting."""
    try:
        if any(os.path.exists(os.path.join(p, ".git")) for p in (HERE, os.path.dirname(HERE))):
            return                                                      # a developer's checkout is never touched
        home = os.environ.get("MUSICDL_HOME") or os.path.join(
            os.environ.get("APPDATA") or os.path.expanduser("~"), "MusicDownloader")
        sdir = os.path.join(home, "shield")
        checker = os.path.join(HERE, "musicdl", "core", "shield.py")
        if not os.path.isfile(checker):
            import zipfile
            with zipfile.ZipFile(os.path.join(sdir, "snapshot.zip")) as z:
                data = z.read("musicdl/core/shield.py")
            os.makedirs(os.path.dirname(checker), exist_ok=True)
            with open(checker, "wb") as fh:
                fh.write(data)
        import importlib.util
        spec = importlib.util.spec_from_file_location("_musicdl_shield_boot", checker)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.boot(HERE, sdir)
    except Exception:
        pass


_boot()

from musicdl.__main__ import main  # noqa: E402

sys.exit(main(["--no-relaunch"]))
