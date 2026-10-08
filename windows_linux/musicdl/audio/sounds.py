"""
sounds.py — plays the app's cues without ever blocking the interface.

play('click') only drops a name into a tiny queue; one background thread does the work, so a slow audio device
can't make the window stutter. Rapid clicks are thinned, clicks never cut off a longer chime that is playing, and the
volume slider is applied by playing a pre-scaled copy of the sound (made once, kept in the cache folder).
"""
import logging
import os
import queue
import sys
import threading
import time
import wave
from array import array

from .. import platform_
from . import synth

log = logging.getLogger("musicdl")
ASSETS = synth.ASSETS
LONG = ("startup", "complete")                    # these may interrupt anything; short cues never interrupt them
MIN_GAP = {"click": 0.07, "nav": 0.12}


def cache_dir_for_sounds():
    d = os.path.join(platform_.cache_dir(), "sounds")
    os.makedirs(d, exist_ok=True)
    return d


def scaled_copy(src, dst, gain):
    """Write src × gain to dst (16-bit WAV)."""
    with wave.open(src, "rb") as w:
        params, frames = w.getparams(), w.readframes(w.getnframes())
    pcm = array("h")
    pcm.frombytes(frames)
    if sys.byteorder != "little":
        pcm.byteswap()
    scaled = array("h", (int(v * gain) for v in pcm))
    if sys.byteorder != "little":
        scaled.byteswap()
    tmp = dst + ".tmp"
    with wave.open(tmp, "wb") as out:
        out.setparams(params)
        out.writeframes(scaled.tobytes())
    os.replace(tmp, dst)


class Sounds:
    def __init__(self, settings_getter):
        self._st = settings_getter
        self._q = queue.Queue(maxsize=6)
        self._last = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="sounds", daemon=True)
        self._thread.start()

    def play(self, name):
        """Request a cue; returns immediately. Does nothing when sounds are off."""
        if name not in synth.NAMES:
            return
        try:
            if not self._st().sounds:
                return
        except Exception:
            return
        now = time.monotonic()
        if now - self._last.get(name, 0) < MIN_GAP.get(name, 0):
            return
        self._last[name] = now
        try:
            self._q.put_nowait(name)
        except queue.Full:
            pass                                    # a burst of cues: the user wouldn't hear the extras anyway

    def close(self):
        self._stop.set()
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass

    # ---------------------------------------------------------------- worker
    def _run(self):
        while not self._stop.is_set():
            name = self._q.get()
            if name is None:
                break
            try:
                path = self._path(name)
                if path:
                    (platform_.play_wav if name in LONG else platform_.play_wav_if_idle)(path)
            except Exception as e:                  # no sound device, etc.: the app carries on silently
                log.debug("sound %s failed: %s", name, e)

    def _path(self, name):
        src = os.path.join(ASSETS, name + ".wav")
        if not os.path.exists(src):                 # assets were deleted: rebuild just this one
            synth.build(ASSETS, only=(name,))
        vol = max(0, min(100, int(self._st().volume)))
        if vol >= 100:
            return src
        if vol <= 0:
            return None
        dst = os.path.join(cache_dir_for_sounds(), f"{name}-{vol}.wav")
        if not os.path.exists(dst) or os.path.getmtime(dst) < os.path.getmtime(src):
            scaled_copy(src, dst, (vol / 100) ** 2)
        return dst
