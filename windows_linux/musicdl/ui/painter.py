"""
painter.py — draws the dashboard's pictures on a worker thread so the window never waits for them.

The window asks for a picture with submit(key, token, function, *args); it asks again whenever the numbers change, and
only the newest request for a key is ever drawn (if the window asks three times while one is being drawn, the first
result is used and the two in between are skipped). take(key, token) hands the finished picture over once. The token
says which layout the request was for (a different window size, a different theme): a picture made for another one is
dropped.

Only Pillow work belongs in the function: Tk may be touched from the window's thread alone, so turning the picture into
something Tk can show (ImageTk.PhotoImage) is done by whoever calls take().
"""
import logging
import threading

from .. import platform_

log = logging.getLogger("musicdl")


class Painter:
    def __init__(self):
        self._cv = threading.Condition()
        self._want = {}                  # key -> (token, fn, args): the newest request, not drawn yet
        self._done = {}                  # key -> (token, picture)
        self._busy = None                # key being drawn
        self._thread = None
        self._complained = set()
        self.drawn = self.skipped = 0

    def submit(self, key, token, fn, *args):
        with self._cv:
            if key in self._want:
                self.skipped += 1
            self._want[key] = (token, fn, args)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name="painter", daemon=True)
                self._thread.start()
            self._cv.notify()

    def take(self, key, token):
        """The newest finished picture for `key` that was made for `token`, once (None when there is none)."""
        with self._cv:
            got = self._done.pop(key, None)
        if got is not None and got[0] == token:
            return got[1]
        return None

    def pending(self, key):
        with self._cv:
            return key in self._want or self._busy == key

    def idle(self):
        with self._cv:
            return not self._want and self._busy is None

    def forget(self):
        """Drop everything not drawn yet and everything drawn but not collected (the window is going away)."""
        with self._cv:
            self._want.clear()
            self._done.clear()

    def _loop(self):
        platform_.thread_priority(0)
        while True:
            with self._cv:
                while not self._want:
                    self._cv.wait()
                key = next(iter(self._want))
                token, fn, args = self._want.pop(key)
                self._busy = key
            try:
                pic = fn(*args)
            except Exception:                                  # noqa: BLE001 — a picture that fails is one that stays as it was
                pic = None
                if key not in self._complained:
                    self._complained.add(key)
                    log.exception("could not draw %s", key)
            with self._cv:
                self._busy = None
                if pic is not None:
                    self._done[key] = (token, pic)
                    self.drawn += 1


PAINTER = Painter()
