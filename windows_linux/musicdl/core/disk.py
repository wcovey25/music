"""
disk.py — a full disk is not a failed song.

When the music folder's drive runs out of room the run pauses, says so, and carries on by itself as soon as space
comes back (the user deletes something, empties the bin, plugs a bigger drive in …). Nothing is marked failed and
nothing is lost: the half-written files of the songs in flight are thrown away (which frees room too) and each of
those songs simply starts again once there is space.

  is_disk_full(exc)   does this error (or text) mean 'no space'?  (errno 28, Windows 39/112, ffmpeg and yt-dlp messages)
  free_bytes(path)    free space on the drive holding `path`
  DiskGuard           shared by every worker: `ensure(need)` before writing, `wait(need)` after a 'disk full' error
"""
import errno
import logging
import os
import shutil
import threading

from .models import DiskFull, Stopped

log = logging.getLogger("musicdl")

RESERVE = 150 * 2 ** 20                 # always leave this much free: the OS and other programs need room too
POLL = 1.5                              # seconds between looks at the free space while paused
_WIN_FULL = (39, 112)                   # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
_WORDS = ("no space left", "not enough space", "not enough room", "disk full", "disk is full", "no space on",
          "errno 28", "winerror 112", "winerror 39", "quota exceeded", "out of disk", "write failed: no space")


def is_disk_full(exc):
    """True when an exception (or a message from ffmpeg / yt-dlp) means the drive is full."""
    if isinstance(exc, str):
        return any(w in exc.lower() for w in _WORDS)
    for _ in range(4):                                      # the error itself, then what it was raised from
        if exc is None:
            break
        if isinstance(exc, DiskFull):
            return True
        if isinstance(exc, OSError) and (exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", -1))
                                         or getattr(exc, "winerror", None) in _WIN_FULL):
            return True
        if any(w in str(exc).lower() for w in _WORDS):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def free_bytes(path):
    """Free bytes on the drive that holds `path` (the nearest folder that exists). None when it cannot be told."""
    p = os.path.abspath(path)
    for _ in range(8):
        if os.path.isdir(p):
            try:
                return shutil.disk_usage(p).free
            except OSError:
                return None
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


class DiskGuard:
    """Pauses the whole run while the disk is full. One per job, shared by all worker threads."""

    def __init__(self, path, stop, emit=None, reserve=RESERVE, poll=POLL, free=None):
        self.path, self.stop, self.emit = path, stop, emit or (lambda ev: None)
        self.reserve, self.poll = reserve, poll
        self._free = free or (lambda: free_bytes(self.path))
        self._lock = threading.Lock()
        self._waiting = 0
        self.pauses = 0                              # how many times the run had to wait (for the summary / tests)

    @property
    def paused(self):
        return self._waiting > 0

    def free(self):
        return self._free()

    def enough(self, need=0):
        f = self.free()
        return f is None or f >= need + self.reserve         # can't tell -> don't block the run

    def ensure(self, need=0):
        """Call before writing about `need` bytes: returns at once when there is room, otherwise waits for it."""
        if self.stop.is_set():
            raise Stopped()
        return self.wait(need) if not self.enough(need) else False

    def wait(self, need=0):
        """Block until `need` bytes (plus the reserve) are free or the run is stopped. True if it really had to wait."""
        waited = False
        try:
            while True:
                if self.stop.is_set():
                    raise Stopped()
                if self.enough(need):
                    return waited
                if not waited:
                    waited = True
                    with self._lock:
                        self._waiting += 1
                        if self._waiting == 1:
                            self.pauses += 1
                            log.warning("disk full: pausing (%s free, need %s)", self.free(), need + self.reserve)
                            self.emit({"type": "paused", "reason": "disk", "free": self.free() or 0,
                                       "need": need + self.reserve, "path": self.path})
                self.stop.wait(self.poll)
        finally:
            if waited:
                with self._lock:
                    self._waiting -= 1
                    if self._waiting == 0:
                        log.info("disk space is back: resuming")
                        self.emit({"type": "resumed", "reason": "disk", "free": self.free() or 0})
