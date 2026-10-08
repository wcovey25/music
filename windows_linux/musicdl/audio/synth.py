"""
synth.py — builds the app's four sounds from scratch, so there are no audio files to license or download.

  startup   a warm low swell under three soft bell notes (D major), ~3.5 s
  click     a tiny, dry tick, ~50 ms
  nav       a gentle upward "blip", ~0.5 s with its reverb tail
  complete  a rising C-major bell arpeggio that settles into a chord, ~3.9 s

Bells are sine partials with an inharmonic overtone and a fast-decaying brightness; a small Schroeder reverb
gives them air. Everything is stereo 44.1 kHz 16-bit, peak-limited well below clipping.
Run  python -m musicdl.audio.synth  to rewrite the files in audio/assets/.
"""
import math
import os
import random
import sys
import wave
from array import array

SR = 44100
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
NAMES = ("startup", "click", "nav", "complete")


# ---------------------------------------------------------------- voices

def _ramp(i, n_attack):
    return i / n_attack if i < n_attack else 1.0


def bell(freq, dur, amp=1.0, decay=0.8, bright=1.0, attack=0.004):
    """A struck-bell voice: fundamental + octave + an inharmonic partial, each dying at its own speed."""
    n = int(dur * SR)
    na = max(1, int(attack * SR))
    out = [0.0] * n
    for ratio, level, speed in ((1.0, 1.0, 1.0), (2.0, 0.32 * bright, 1.8), (2.76, 0.14 * bright, 2.8),
                                (5.4, 0.04 * bright, 4.5)):
        w = 2 * math.pi * freq * ratio / SR
        k = math.exp(-speed / (decay * SR))                  # per-sample decay factor
        e = level * amp
        for i in range(n):
            out[i] += e * math.sin(w * i) * _ramp(i, na)
            e *= k
    return out


def pad(freq, dur, amp=0.3, rise=0.7, fall=1.2):
    """A slow sine swell (fundamental + octave) used as warmth under the first notes."""
    n = int(dur * SR)
    out = [0.0] * n
    w = 2 * math.pi * freq / SR
    for i in range(n):
        t = i / SR
        env = min(1.0, t / rise) * min(1.0, max(0.0, (dur - t) / fall))
        env = env * env * (3 - 2 * env)                      # smoothstep
        out[i] = amp * env * (math.sin(w * i) + 0.25 * math.sin(2 * w * i))
    return out


def glide(f0, f1, dur, amp=1.0, tau=0.06):
    """A sine that slides from f0 to f1 while fading: the 'blip'."""
    n = int(dur * SR)
    out, phase = [0.0] * n, 0.0
    for i in range(n):
        t = i / n
        f = f0 + (f1 - f0) * (1 - (1 - t) ** 2)
        phase += 2 * math.pi * f / SR
        env = math.exp(-i / (tau * SR)) * min(1.0, i / (0.006 * SR))
        out[i] = amp * env * (math.sin(phase) + 0.18 * math.sin(2 * phase))
    return out


def tick(dur=0.05, amp=1.0):
    """A soft dry 'tap': two short decaying sines and a hint of noise."""
    n = int(dur * SR)
    rnd = random.Random(7)
    out = [0.0] * n
    for i in range(n):
        t = i / SR
        env_hi, env_lo = math.exp(-t / 0.005), math.exp(-t / 0.012)
        out[i] = amp * (0.7 * env_hi * math.sin(2 * math.pi * 2300 * t) + 0.5 * env_lo * math.sin(2 * math.pi * 900 * t)
                        + 0.05 * env_hi * (rnd.random() * 2 - 1)) * min(1.0, i / (0.0012 * SR))
    return out


# ---------------------------------------------------------------- mixing + space

def place(track, voice, start, pan=0.0):
    """Add a mono voice into a stereo pair of lists at `start` seconds with equal-power panning."""
    left, right = track
    a = (pan + 1) * math.pi / 4
    gl, gr = math.cos(a), math.sin(a)
    s = int(start * SR)
    need = s + len(voice)
    for ch in (left, right):
        if len(ch) < need:
            ch.extend([0.0] * (need - len(ch)))
    for i, v in enumerate(voice):
        left[s + i] += v * gl
        right[s + i] += v * gr


def _comb(x, delay, fb, damp):
    buf, out, lp, j = [0.0] * delay, [0.0] * len(x), 0.0, 0
    for i, v in enumerate(x):
        y = buf[j]
        lp = y * (1 - damp) + lp * damp
        buf[j] = v + lp * fb
        out[i] = y
        j = (j + 1) % delay
    return out


def _allpass(x, delay, g):
    buf, out, j = [0.0] * delay, [0.0] * len(x), 0
    for i, v in enumerate(x):
        b = buf[j]
        out[i] = -g * v + b
        buf[j] = v + g * b
        j = (j + 1) % delay
    return out


_COMBS = (((29.7, 37.1, 41.1, 43.7)), ((30.5, 38.3, 42.3, 45.1)))        # ms, slightly different per ear


def reverb(ch, side, wet=0.3, size=0.82, tail=0.9):
    """Schroeder reverb for one channel. `size` is the comb feedback (longer tail as it nears 1)."""
    x = ch + [0.0] * int(tail * SR)
    acc = [0.0] * len(x)
    for ms in _COMBS[side]:
        c = _comb(x, int(ms * SR / 1000), size, 0.35)
        for i, v in enumerate(c):
            acc[i] += v * 0.25
    for ms, g in ((5.0, 0.7), (1.7, 0.7)):
        acc = _allpass(acc, int(ms * SR / 1000), g)
    return [x[i] * (1 - wet * 0.4) + acc[i] * wet * 1.6 for i in range(len(x))]


def lowpass(ch, cutoff):
    a = 1 - math.exp(-2 * math.pi * cutoff / SR)
    y, out = 0.0, []
    for v in ch:
        y += a * (v - y)
        out.append(y)
    return out


def finish(left, right, peak, fade=0.06, cutoff=None):
    """Peak-normalise, fade the end to silence, remove any DC offset, and pack as 16-bit stereo."""
    chans = []
    for ch in (left, right):
        if cutoff:
            ch = lowpass(ch, cutoff)
        mean = sum(ch) / max(1, len(ch))
        chans.append([v - mean for v in ch])
    n = max(len(c) for c in chans)
    top = max((abs(v) for c in chans for v in c), default=1.0) or 1.0
    g = peak / top
    nf = int(fade * SR)
    pcm = array("h")
    for i in range(n):
        f = min(1.0, (n - i) / nf) if i > n - nf else 1.0
        for c in chans:
            v = (c[i] if i < len(c) else 0.0) * g * f
            pcm.append(int(max(-1.0, min(1.0, v)) * 32767))
    return pcm


# ---------------------------------------------------------------- the four sounds

def make_startup():
    t = ([], [])
    place(t, pad(146.83, 2.6, 0.30), 0.0, 0.0)                              # D3 warmth
    place(t, bell(587.33, 2.4, 0.80, decay=0.95), 0.00, -0.10)              # D5
    place(t, bell(880.00, 2.0, 0.55, decay=0.85), 0.22, 0.32)               # A5
    place(t, bell(1479.98, 1.7, 0.32, decay=0.70, bright=0.7), 0.46, 0.45)  # F#6
    left, right = reverb(t[0], 0, 0.38, 0.84), reverb(t[1], 1, 0.38, 0.84)
    return finish(left, right, 0.50, fade=0.25, cutoff=6500)


def make_click():
    t = ([], [])
    place(t, tick(), 0.0, 0.0)
    return finish(t[0], t[1], 0.26, fade=0.008, cutoff=9000)


def make_nav():
    t = ([], [])
    place(t, glide(520, 780, 0.24, 0.9, tau=0.05), 0.0, -0.1)
    place(t, bell(1560, 0.2, 0.12, decay=0.09, bright=0.2), 0.02, 0.2)
    left, right = reverb(t[0], 0, 0.16, 0.7, 0.3), reverb(t[1], 1, 0.16, 0.7, 0.3)
    return finish(left, right, 0.32, fade=0.05, cutoff=7000)


def make_complete():
    t = ([], [])
    for freq, start, pan, amp in ((783.99, 0.00, -0.22, 0.65), (1046.50, 0.15, -0.02, 0.60),
                                  (1318.51, 0.30, 0.22, 0.55), (1567.98, 0.45, 0.42, 0.50)):
        place(t, bell(freq, 2.2, amp, decay=0.9), start, pan)
    for freq, pan in ((523.25, -0.05), (659.25, 0.15), (783.99, 0.3)):       # the chord everything settles into
        place(t, bell(freq, 2.4, 0.38, decay=1.1, bright=0.6, attack=0.05), 0.55, pan)
    left, right = reverb(t[0], 0, 0.42, 0.85), reverb(t[1], 1, 0.42, 0.85)
    return finish(left, right, 0.52, fade=0.3, cutoff=6500)


MAKERS = {"startup": make_startup, "click": make_click, "nav": make_nav, "complete": make_complete}


def save(path, pcm):
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes() if sys.byteorder == "little" else _swap(pcm))


def _swap(pcm):
    p = array("h", pcm)
    p.byteswap()
    return p.tobytes()


def build(outdir=ASSETS, only=None):
    os.makedirs(outdir, exist_ok=True)
    paths = {}
    for name in only or NAMES:
        paths[name] = os.path.join(outdir, name + ".wav")
        save(paths[name], MAKERS[name]())
    return paths


if __name__ == "__main__":
    for n, p in build(sys.argv[1] if len(sys.argv) > 1 else ASSETS).items():
        print(f"{n:9} {os.path.getsize(p) / 1024:6.0f} KB  {p}")
