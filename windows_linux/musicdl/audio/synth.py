"""
synth.py — builds the app's four sounds from scratch, so there are no audio files to license or download.

  startup   "Aurora", ~4.4 s, written to the launch animation (see TIMELINE): a warm pad and a rising breath of air, a soft
            thump as the icon forms, four glass bells climbing a D-major-9 chord on the ripples, a swish as the light
            sweeps across the icon, then a wide chord that rings out behind the window
  click     a tiny, dry tick, ~50 ms
  nav       a gentle upward "blip", ~0.5 s with its reverb tail
  complete  a rising C-major bell arpeggio that settles into a chord, ~3.9 s

Bells are sine partials with an inharmonic overtone and a fast-decaying brightness (the launch bells are FM "glass");
a small Schroeder reverb gives the short cues air and a feedback-delay-network reverb the launch sound. Everything is
stereo 44.1 kHz 16-bit with TPDF dither, peak-limited well below clipping.
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


def finish(left, right, peak, fade=0.06, cutoff=None, passes=1):
    """Peak-normalise, fade the end to silence, remove any DC offset, and pack as 16-bit stereo with TPDF dither
    (±1 LSB of seeded noise: a quiet tail fades smoothly instead of crackling into steps). `passes` low-pass stages."""
    chans = []
    for ch in (left, right):
        for _ in range(passes if cutoff else 0):
            ch = lowpass(ch, cutoff)
        mean = sum(ch) / max(1, len(ch))
        chans.append([v - mean for v in ch])
    n = max(len(c) for c in chans)
    top = max((abs(v) for c in chans for v in c), default=1.0) or 1.0
    g = peak / top
    nf = int(fade * SR)
    rnd = random.Random(11)
    pcm = array("h")
    for i in range(n):
        f = min(1.0, (n - i) / nf) if i > n - nf else 1.0
        for c in chans:
            v = (c[i] if i < len(c) else 0.0) * g * f
            q = v * 32767 + (rnd.random() - rnd.random())
            pcm.append(int(max(-32767, min(32767, round(q)))))
    return pcm


# ---------------------------------------------------------------- the launch sound

# When things happen in the launch animation (seconds from the start; ui/splash.py reads these, so the picture and the
# sound can't drift apart).
TIMELINE = {"thump": 0.50, "bells": (0.62, 0.80, 0.98, 1.16), "sweep": 1.46, "chord": 1.78}
STARTUP_SECONDS = 4.4


def glass_bell(freq, dur, amp=1.0, decay=1.5, ratio=2.76, index=2.0, idx_decay=0.10, attack=0.002):
    """An FM 'glass' bell: a bright inharmonic strike that settles within ~0.3 s into a pure tone with a soft octave, the
    way a struck wine glass does. Sidebands stay under 18 kHz for every note used, so nothing aliases."""
    n = int(dur * SR)
    na = max(1, int(attack * SR))
    wc, wm = 2 * math.pi * freq / SR, 2 * math.pi * freq * ratio / SR
    out = [0.0] * n
    env, e2, e3, idx = amp, 0.22 * amp, 0.05 * amp, index
    k1, k2, k3 = math.exp(-1 / (decay * SR)), math.exp(-1 / (decay * 0.55 * SR)), math.exp(-1 / (decay * 0.30 * SR))
    ki = math.exp(-1 / (idx_decay * SR))
    for i in range(n):
        s = math.sin(wc * i + idx * math.sin(wm * i)) * env + math.sin(2 * wc * i) * e2 + math.sin(3.01 * wc * i) * e3
        out[i] = s * (i / na if i < na else 1.0)
        env *= k1
        e2 *= k2
        e3 *= k3
        idx *= ki
    return out


def aurora_pad(dur, amp=0.30, rise=1.3, fall=1.6, side=0):
    """The warm bed: D–A–E–F♯ over a D, two slightly detuned copies per note (one for each ear), a slow swell, rounded
    off above ~2 kHz. `side` picks which of the detuned pair this ear gets."""
    n = int(dur * SR)
    out = [0.0] * n
    de = (0.9965, 1.0035)[side]
    for freq, level in ((73.42, 1.0), (146.83, 0.9), (220.00, 0.55), (329.63, 0.38), (369.99, 0.30)):
        w = 2 * math.pi * freq * de / SR
        ph = 1.1 * side
        for i in range(n):
            out[i] += level * (math.sin(w * i + ph) + (0.20 * math.sin(2 * w * i) if freq < 150 else 0.0))
    for i in range(n):
        t = i / SR
        env = min(1.0, t / rise) * min(1.0, max(0.0, (dur - t) / fall))
        out[i] *= amp * env * env * (3 - 2 * env) / 2.4
    return lowpass(lowpass(out, 2200), 2200)


def thump(dur=0.5, amp=0.9, f0=118.0, f1=52.0):
    """A soft low 'boom' that sinks in pitch — weight for the moment the icon forms."""
    n = int(dur * SR)
    out, phase = [0.0] * n, 0.0
    for i in range(n):
        t = i / SR
        f = f1 + (f0 - f1) * math.exp(-t / 0.07)
        phase += 2 * math.pi * f / SR
        out[i] = amp * math.exp(-t / 0.15) * math.sin(phase) * min(1.0, i / (0.004 * SR))
    return out


def air(dur, f0, f1, amp, seed, q=1.8, curve=2.0, tail=0.0):
    """Filtered noise whose centre frequency glides from f0 to f1 (a breath, a swish). The level swells as t**curve and, if
    `tail` is set, falls away over the last `tail` seconds."""
    n = int(dur * SR)
    rnd = random.Random(seed)
    low = band = 0.0
    out = [0.0] * n
    for i in range(n):
        u = i / n
        fc = min(6500.0, f0 * (f1 / f0) ** u)
        f = 2 * math.sin(math.pi * fc / SR)
        low += f * band
        high = (rnd.random() * 2 - 1) - low - band / q
        band += f * high
        env = u ** curve
        if tail and i > n - tail * SR:
            env *= (n - i) / (tail * SR)
        out[i] = amp * env * band
    return out


def _delay_net(x, wet=1.0, rt60=2.3, predelay=0.020):
    """A feedback delay network (six delay lines mixed by a Householder matrix, a low-pass in each loop) at half the sample
    rate — the tail is dark anyway — for a dense, smooth room instead of the ring of a few combs. Returns (left, right)."""
    half = [0.5 * (x[i] + x[i + 1]) for i in range(0, len(x) - 1, 2)]
    sr = SR // 2
    n = len(half) + int(rt60 * 0.9 * sr)
    half += [0.0] * (n - len(half))
    delays = [int(ms * sr / 1000) for ms in (29.7, 37.1, 41.1, 47.3, 53.9, 61.3)]
    gains = [10 ** (-3 * d / (rt60 * sr)) for d in delays]
    bufs = [[0.0] * d for d in delays]
    pos = [0] * 6
    damp = [0.0] * 6
    pre = int(predelay * sr)
    lo, ro = [0.0] * n, [0.0] * n
    sl, sr_ = (1, -1, 1, -1, 1, -1), (1, 1, -1, -1, 1, 1)
    for i in range(n):
        vals = [bufs[k][pos[k]] for k in range(6)]
        for k in range(6):
            damp[k] += 0.42 * (vals[k] - damp[k])                  # darker with every trip round the loop
        total = sum(damp) / 3.0                                   # Householder mix: x - (2/N)·sum(x), N = 6
        xin = half[i - pre] if i >= pre else 0.0
        for k in range(6):
            bufs[k][pos[k]] = gains[k] * (damp[k] - total) + (xin if k % 2 == 0 else -xin) * 0.5
            pos[k] = (pos[k] + 1) % delays[k]
        lo[i] = sum(v * s for v, s in zip(vals, sl)) * 0.45
        ro[i] = sum(v * s for v, s in zip(vals, sr_)) * 0.45
    def up(h):                                                    # back to 44.1 kHz (linear: the tail is under 8 kHz)
        out = [0.0] * (2 * len(h))
        for i, v in enumerate(h):
            nxt = h[i + 1] if i + 1 < len(h) else 0.0
            out[2 * i], out[2 * i + 1] = v, 0.5 * (v + nxt)
        return out
    return up(lo), up(ro)


def make_startup():
    T = TIMELINE
    n = int(STARTUP_SECONDS * SR)
    dry = ([0.0] * n, [0.0] * n)
    send = [0.0] * n                                              # what goes to the room
    pad_dur = 3.7
    for side, ch in enumerate(dry):
        for i, v in enumerate(aurora_pad(pad_dur, side=side)):
            ch[i] += v
    for i, v in enumerate(air(1.30, 450, 6200, 0.16, 5, curve=2.2, tail=0.12)):
        dry[0][i] += v * 0.8
    for i, v in enumerate(air(1.30, 520, 6000, 0.16, 6, curve=2.2, tail=0.12)):
        dry[1][i] += v * 0.8
    voices = [(thump(0.55, 0.85), T["thump"], 0.0, 0.0)]
    for (freq, amp, pan), start in zip(((587.33, 0.78, -0.30), (739.99, 0.62, -0.05), (880.00, 0.58, 0.20), (1318.51, 0.40, 0.42)),
                                       T["bells"]):
        voices.append((glass_bell(freq, 2.6, amp, decay=1.5, index=2.0 if freq < 1000 else 1.4), start, pan, 0.55))
    voices.append((air(0.42, 1800, 6300, 0.20, 9, q=2.4, curve=1.2, tail=0.18), T["sweep"], 0.0, 0.25))
    for k, (freq, amp, pan) in enumerate(((2349.32, 0.07, -0.4), (2959.96, 0.06, 0.4), (3520.00, 0.05, 0.1))):     # glints
        voices.append((glass_bell(freq, 0.9, amp, decay=0.35, index=0.8), T["sweep"] + 0.02 + 0.07 * k, pan, 0.7))
    for freq, amp, pan in ((293.66, 0.50, -0.45), (440.00, 0.42, 0.40), (659.25, 0.36, -0.20), (739.99, 0.30, 0.25),
                           (1174.66, 0.22, 0.05)):                # the wide chord the whole thing settles into
        voices.append((glass_bell(freq, 2.6, amp, decay=1.8, index=0.9, attack=0.03), T["chord"], pan, 0.8))
    for voice, start, pan, wet in voices:
        place(dry, voice, start, pan)
        s0 = int(start * SR)
        for i, v in enumerate(voice):
            if s0 + i < n:
                send[s0 + i] += v * wet
    rl, rr = _delay_net(send)
    left = [(dry[0][i] if i < n else 0.0) + 0.55 * rl[i] for i in range(len(rl))]
    right = [(dry[1][i] if i < n else 0.0) + 0.55 * rr[i] for i in range(len(rr))]
    keep = int((STARTUP_SECONDS + 0.2) * SR)
    return finish(left[:keep], right[:keep], 0.42, fade=0.7,
                  cutoff=15000, passes=2)


# ---------------------------------------------------------------- the four sounds
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
