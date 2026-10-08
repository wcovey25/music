"""
sampler.py — a short listen to a downloaded source, to find out what it is really worth.

A file can claim more than it holds: a "FLAC" or "WAV" made from a 128 kbps MP3 or a video's soundtrack is a big file
with lossy sound in it. Lossy encoders throw away everything above a cut-off frequency (about 11 kHz at 64 kbps, 17 kHz
at 128, 19.5 kHz at 256), and the decoded audio keeps that hard edge forever. So the sampler decodes a few seconds from
three places in the song (the loudest frames are used), measures how much sound there is in each 250 Hz slice between
9 and 21.5 kHz, and looks for that edge: a sudden fall of 30 dB or more after which there is *digital silence* (nothing
above the 16-bit rounding noise). Natural recordings roll off gradually, and analog sources (tape, FM radio) keep a hiss
above their cut-off, so neither counts. The cut-off is turned into the MP3 bitrate that would have produced it.

This is an estimate, not a proof: measured on synthetic encodes, it reliably recognises a 64–192 kbps MP3/AAC-style
origin. A 224 kbps one is mapped but sits too close to the top of the band to be seen in practice, and a 256–320 kbps
one (or a full-band Opus/Vorbis rip) cannot be told from lossless, because those reach 20 kHz too. A recording that was
band-limited on purpose in the digital domain can look lossy. When in doubt — silence, a short file, ffmpeg missing, an
error — the file is believed.

The same listening checks hi-res files: a "96 kHz / 24-bit" file made from CD audio has nothing above about 24 kHz and
nothing in its lowest 8 bits (pcm_truth).

Pure Python (a small FFT), no numpy, and only run on sources whose claim is worth checking (lossless, or lossy above
128 kbps).
"""
import math
import subprocess
import time
from dataclasses import dataclass

from .. import platform_, resources
from ..core.models import Stopped

RATE = 44100
N = 1024                                  # FFT size: 43 Hz per bin
BAND_HZ = 250
FIRST_HZ, LAST_HZ = 9000, 21500           # the slices that are measured
EDGE_MIN, EDGE_MAX = 10500, 19500         # edges that count. Higher ones (256/320 kbps origins) are left alone on purpose:
                                          # a CD's own anti-alias filter ends near 20 kHz, and wrongly turning a real
                                          # lossless file into MP3 is the worse mistake
DROP_DB = 30.0                            # how far everything above the edge sits below the sound just under it
FLOOR_DB = -64.0                          # ... and that "nothing" must be digital silence: the 16-bit rounding noise of
                                          # a decode sits at -75 on this scale (dither -70), analog hiss at -55 or more
SECONDS = 14                              # a short file is read from its start for this long
SEGMENTS = (0.2, 0.5, 0.8)                # a longer one is read in three places, SEGMENT seconds each
SEGMENT = 6
FRAMES = 40                               # loudest frames that are analysed
LISTEN_ABOVE = 144                        # a lossy source claiming more than this (MP3-equivalent kbps) is listened to too
# cut-off (Hz, upper bound) -> MP3-equivalent kbps of the encode that makes such an edge (LAME's low-pass: 64 kbps
# 11 kHz, 80 13.5, 96 15.1, 112 15.6, 128 17, 160 17.5, 192 18.6, 224 19.4)
CUTOFF_Q = ((11500, 64), (14000, 80), (15500, 96), (16250, 112), (17250, 128), (18000, 160), (19000, 192), (19500, 224))


@dataclass
class Sample:
    lossless: bool            # the lossless claim holds (or could not be disproved)
    q: int                    # MP3-equivalent kbps when it does not
    cutoff: int = 0           # the edge found, Hz (0 = none)
    checked: bool = False     # the spectrum was really measured


TRUSTED = Sample(True, 0)


def q_from_cutoff(hz):
    for limit, q in CUTOFF_Q:
        if hz <= limit:
            return q
    return 0


# ---------------------------------------------------------------- the maths

def _tables(n):
    rev, bits = [0] * n, n.bit_length() - 1
    for i in range(n):
        rev[i] = int(format(i, f"0{bits}b")[::-1], 2)
    cos = [math.cos(2 * math.pi * k / n) for k in range(n // 2)]
    sin = [-math.sin(2 * math.pi * k / n) for k in range(n // 2)]
    return rev, cos, sin, [0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]


_REV, _COS, _SIN, _WIN = _tables(N)


def _fft(re, im):
    """In-place radix-2 FFT of `re`/`im` (length N)."""
    n = N
    for i in range(n):
        j = _REV[i]
        if i < j:
            re[i], re[j] = re[j], re[i]
            im[i], im[j] = im[j], im[i]
    size = 2
    while size <= n:
        half, step = size >> 1, n // size
        for start in range(0, n, size):
            k = 0
            for a in range(start, start + half):
                b = a + half
                wr, wi = _COS[k], _SIN[k]
                tr = re[b] * wr - im[b] * wi
                ti = re[b] * wi + im[b] * wr
                re[b], im[b] = re[a] - tr, im[a] - ti
                re[a] += tr
                im[a] += ti
                k += step
        size <<= 1


def spectrum(frames):
    """Average power per bin (0 .. N/2) of equal-length frames of samples in -1..1. Two real frames share one FFT."""
    acc = [0.0] * (N // 2 + 1)
    count = 0
    for i in range(0, len(frames) - 1, 2):
        re = [x * w for x, w in zip(frames[i], _WIN)]
        im = [x * w for x, w in zip(frames[i + 1], _WIN)]
        _fft(re, im)
        for k in range(N // 2 + 1):
            kk = (N - k) % N
            ar, ai = (re[k] + re[kk]) * 0.5, (im[k] - im[kk]) * 0.5            # first frame's bin
            br, bi = (im[k] + im[kk]) * 0.5, (re[kk] - re[k]) * 0.5            # second frame's bin
            acc[k] += ar * ar + ai * ai + br * br + bi * bi
        count += 2
    return [a / count for a in acc] if count else acc


def band_levels(power):
    """dB level of each BAND_HZ slice from FIRST_HZ to LAST_HZ, plus the 1–4 kHz level as a reference."""
    bw = RATE / N
    def level(lo, hi):
        a, b = int(lo / bw), max(int(lo / bw) + 1, int(hi / bw))
        return 10 * math.log10(sum(power[a:b]) / (b - a) + 1e-30)
    bands = [level(f, f + BAND_HZ) for f in range(FIRST_HZ, LAST_HZ, BAND_HZ)]
    return bands, level(1000, 4000)


def find_edge(bands, ref):
    """The lowest edge (Hz) after which the sound stays 30 dB under what is just below it and (further up) is digital
    silence; 0 when there is none."""
    for j in range(max(2, (EDGE_MIN - FIRST_HZ) // BAND_HZ - 1), (EDGE_MAX - FIRST_HZ) // BAND_HZ):
        below = bands[j - 1]
        if below < ref - 85:                                     # nothing real to compare with
            continue
        above = bands[j + 1:]
        if below - max(above) < DROP_DB:
            continue
        if max(above[2:] or above) > FLOOR_DB:                   # a hiss or a gentle slope, not an encoder's low-pass
            continue
        return FIRST_HZ + (j + 1) * BAND_HZ
    return 0


def loud_frames(samples):
    """The FRAMES loudest frames of `samples` (each N long), or [] when it is too short or silent."""
    n = len(samples) // N
    if n < 8:
        return []
    energy = []
    stride = max(1, n // (FRAMES * 3))
    for i in range(0, n, stride):
        seg = samples[i * N:(i + 1) * N]
        energy.append((sum(x * x for x in seg), i))
    energy.sort(reverse=True)
    if not energy or energy[0][0] / N < 1e-6:                    # silence (below about -60 dBFS rms)
        return []
    return [samples[i * N:(i + 1) * N] for i in sorted(i for _e, i in energy[:FRAMES])]


def analyse(samples):
    """samples: 44.1 kHz mono floats in -1..1 -> the cut-off in Hz (0 = none / cannot tell)."""
    frames = loud_frames(samples)
    if not frames:
        return 0
    bands, ref = band_levels(spectrum(frames))
    return find_edge(bands, ref)


# ---------------------------------------------------------------- reading the file

def _pcm(path, start, seconds, stop, *out_args):
    """Raw output of ffmpeg decoding `seconds` of `path` from `start` (None when it cannot)."""
    ff = platform_.find_tool("ffmpeg")
    if not ff:
        return None
    cmd = [ff, "-nostdin", "-v", "error", "-ss", f"{max(0.0, start):.2f}", "-t", str(seconds), "-i", path, "-vn",
           *out_args, "-"]
    cmd, nice = resources.launch(cmd)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=platform_.NO_WINDOW | nice)
    deadline = time.monotonic() + 60
    while True:
        try:
            out, _ = p.communicate(timeout=0.4)
            break
        except subprocess.TimeoutExpired:
            if (stop is not None and stop.is_set()) or time.monotonic() > deadline:
                p.kill()
                p.communicate()
                if stop is not None and stop.is_set():
                    raise Stopped()
                return None
    return out if p.returncode == 0 else None


def _decode(path, start, seconds, stop, rate=RATE):
    """Mono samples in -1..1 at `rate` (None when the file cannot be read or is under a second long)."""
    out = _pcm(path, start, seconds, stop, "-ac", "1", "-ar", str(rate), "-f", "s16le")
    if not out or len(out) < 2 * rate:
        return None
    import array
    pcm = array.array("h")
    pcm.frombytes(out[:len(out) // 2 * 2])
    return [v / 32768.0 for v in pcm]


def _length(path):
    try:
        import mutagen
        return float(mutagen.File(path).info.length)
    except Exception:
        return 0.0


def _excerpts(path, length, stop):
    """Samples from three places in the song (one place for a short song), each a whole number of frames."""
    if length > SECONDS + 4:
        spots = [(min(length * f, length - SEGMENT - 1), SEGMENT) for f in SEGMENTS]
    else:
        spots = [(0.0, SECONDS)]
    out = []
    for start, secs in spots:
        got = _decode(path, start, secs, stop)
        if got:
            out.extend(got[:len(got) // N * N])
    return out


def sample(path, stop=None):
    """Is this file what it claims to be? Never raises (except Stopped): when it cannot tell, it believes."""
    if stop is not None and stop.is_set():
        raise Stopped()
    try:
        samples = _excerpts(path, _length(path), stop)
        if not samples:
            return TRUSTED
        edge = analyse(samples)
        q = q_from_cutoff(edge) if edge else 0
        return Sample(False, q, edge, True) if q else Sample(True, 0, 0, True)
    except Stopped:
        raise
    except Exception:
        return TRUSTED


# ---------------------------------------------------------------- hi-res files

def pcm_truth(path, rate, bits, stop=None):
    """(sample rate, bit depth) that a file claiming `rate` Hz / `bits` bits really holds. A 96 kHz file made from CD
    audio has no sound above 22 kHz; a 24-bit one made from 16-bit audio has nothing in its lowest 8 bits. Never lowers
    anything it cannot prove."""
    if stop is not None and stop.is_set():
        raise Stopped()
    held_rate, held_bits = rate, bits
    try:
        length = _length(path)
        start = length * 0.4 if length > 20 else 0.0
        if bits > 16:
            raw = _pcm(path, start, 4, stop, "-f", "s32le")
            if raw and len(raw) >= 4 * 44100:
                import array
                import functools
                import operator
                arr = array.array("i")
                arr.frombytes(raw[:len(raw) // 4 * 4])
                acc = functools.reduce(operator.or_, arr, 0)
                if acc:
                    used = 32 - ((acc & -acc).bit_length() - 1)              # significant bits in the 32-bit container
                    held_bits = min(bits, 16 if used <= 16 else 24 if used <= 24 else 32)
        if rate >= 88200:
            held_rate = min(rate, _held_rate(path, start, rate, stop))
    except Stopped:
        raise
    except Exception:
        pass
    return held_rate, held_bits


def _held_rate(path, start, rate, stop):
    """48000 or 44100 when a high-rate file has digital silence above what that rate can carry; else `rate`. A
    resampler's skirt reaches several kHz past the old Nyquist, so silence is only asked for above 30 kHz (a real
    recording has the noise of its own converters up there; only a resampled one is silent)."""
    samples = _decode(path, start, 8, stop, rate)
    frames = loud_frames(samples) if samples else []
    if not frames:
        return rate
    power = spectrum(frames)
    bw = rate / N

    def peak_above(hz):
        return 10 * math.log10(max(power[int(hz / bw) + 1:]) + 1e-30)
    if peak_above(30000) > FLOOR_DB:
        return rate
    return 44100 if peak_above(24500) <= FLOOR_DB else 48000
