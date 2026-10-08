"""
browser.py — read a JavaScript-only page by letting a browser that is already installed render it.

Edge, Chrome, Chromium and Brave can print the finished page (`--dump-dom`) without opening a window, so no
extra Python packages are needed. Used for Amazon Music (its pages are empty shells until scripts run) and as a
fallback for other single-page sites. A throw-away profile keeps it away from the user's real browser data.
"""
import logging
import shutil
import subprocess
import tempfile

from .. import platform_
from ..core.netio import BROWSER_UA
from .base import ResolveError

log = logging.getLogger("musicdl")


def available():
    return platform_.browser_candidates()[:1]


def render(url, ctx, settle_ms=12000, timeout=60):
    """HTML of `url` after its scripts ran. Raises ResolveError if no browser is installed or it fails."""
    exes = platform_.browser_candidates()
    if not exes:
        raise ResolveError("This link needs a web browser to read it, and none was found. "
                           "Install Microsoft Edge, Google Chrome or Chromium and try again.")
    profile = tempfile.mkdtemp(prefix="musicdl_browser_")
    cmd = [exes[0], "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
           "--disable-extensions", "--disable-background-networking", "--mute-audio", f"--user-data-dir={profile}",
           f"--user-agent={BROWSER_UA}", f"--virtual-time-budget={settle_ms}", "--window-size=1280,16000",
           "--dump-dom", url]
    try:
        ctx.status("Reading the page…")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             creationflags=platform_.NO_WINDOW)
        deadline = timeout
        while True:
            try:
                out, _ = p.communicate(timeout=0.4)
                break
            except subprocess.TimeoutExpired:
                deadline -= 0.4
                if ctx.stop.is_set() or deadline <= 0:
                    p.kill()
                    p.communicate()
                    raise ResolveError("Cancelled" if ctx.stop.is_set() else "The page took too long to load")
        if p.returncode not in (0, None) and not out:
            raise ResolveError("The browser couldn’t open that page")
        return out.decode("utf-8", "replace")
    except OSError as e:
        raise ResolveError(f"Couldn’t start the browser ({e})") from None
    finally:
        shutil.rmtree(profile, ignore_errors=True)
