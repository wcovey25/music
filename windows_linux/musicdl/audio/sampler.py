"""
sampler.py — a short listen to a downloaded source, to find out what it is really worth.

A file can claim more than it holds: a "FLAC" or "WAV" made from a 128 kbps MP3 or a video's soundtrack is a big file
with lossy sound in it. Lossy encoders throw away everything above a cut-off frequency (about 16 kHz at 128 kbps,
19.5 kHz at 256), and the decoded audio keeps that hard edge forever. So the sampler decodes a few seconds from the
loudest part of the song, measures how much sound there is in each 250 Hz slice between 14 and 21.5 kHz, and looks
for that edge: a sudden fall of 30 dB or more after which nothing comes back. Natural recordings roll off gradually,
so a gentle slope never counts. The cut-off is turned into the MP3 bitrate that would have produced it.

This is an estimate, not a proof: it recognises a 128–224 kbps MP3/AAC-style origin, but cannot tell a 256–320 kbps
one (or a full-band Opus/Vorbis rip) from lossless, because those reach 20 kHz too, and a recording that was
band-limited on purpose can look lossy. When in doubt — silence, a short file, ffmpeg missing, an error — the file is
believed.

Pure Python (a small FFT), no numpy, and only ever run on files that claim to be lossless.
"""
import math
import subprocess
import time
from dataclasses import dataclass

from .. import platform_
from ..core.models import Stopped

RATE = 44100
N = 1024                                  # FFT size: 43 Hz per bin
BAND_HZ = 250
FIRST_HZ, LAST_HZ = 14000, 21500          # the slices that are measured
EDGE_MIN, EDGE_MAX = 15000, 19500         # edges that count. Higher ones (256/320 kbps origins) are left alone on purpose:
                                          # a CD's own anti-alias filter ends near 20 kHz, and wrongly turning a real
                                          # lossless file into MP3 is the worse mistake
DROP_DB = 30.0                            # how far everything above the edge sits below the sound just under it
SECONDS = 14                              # excerpt length
FRAMES = 40                               # loudest frames of the excerpt that are analysed
# cut-off (Hz, upper bound) -> MP3-equivalent kbps of the encode that makes such an edge
CUTOFF_Q = ((17250, 128), (18000, 160), (19000, 192), (19500, 224))


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
    """The lowest edge (Hz) after which the sound stays 30 dB under what is just below it; 0 when there is none."""
    for j in range(max(2, (EDGE_MIN - FIRST_HZ) // BAND_HZ - 1), (EDGE_MAX - FIRST_HZ) // BAND_HZ):
        below = bands[j - 1]
        if below < ref - 85:                                     # nothing real to compare with
            continue
        if below - max(bands[j + 1:]) >= DROP_DB:
            return FIRST_HZ + (j + 1) * BAND_HZ
    return 0


def analyse(samples):
    """samples: 44.1 kHz mono floats in -1..1 -> the cut-off in Hz (0 = none / cannot tell)."""
    n = len(samples) // N
    if n < 8:
        return 0
    energy = []
    stride = max(1, n // (FRAMES * 3))
    for i in range(0, n, stride):
        seg = samples[i * N:(i + 1) * N]
        energy.append((sum(x * x for x in seg), i))
    energy.sort(reverse=True)
    chosen = sorted(i for _e, i in energy[:FRAMES])
    if not energy or energy[0][0] / N < 1e-6:                    # silence (below about -60 dBFS rms)
        return 0
    power = spectrum([samples[i * N:(i + 1) * N] for i in chosen])
    bands, ref = band_levels(power)
    return find_edge(bands, ref)


# ---------------------------------------------------------------- reading the file

def _decode(path, start, seconds, stop):
    ff = platform_.find_tool("ffmpeg")
    if not ff:
        return None
    cmd = [ff, "-nostdin", "-v", "error", "-ss", f"{max(0.0, start):.2f}", "-t", str(seconds), "-i", path, "-vn",
           "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=platform_.NO_WINDOW)
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
    if p.returncode != 0 or len(out) < 2 * RATE:
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


def sample(path, stop=None):
    """Is this file that claims to be lossless really? Never raises (except Stopped): when it cannot tell, it believes."""
    if stop is not None and stop.is_set():
        raise Stopped()
    try:
        length = _length(path)
        start = length * 0.35 if length > SECONDS + 4 else 0.0
        samples = _decode(path, start, SECONDS, stop)
        if not samples:
            return TRUSTED
        edge = analyse(samples)
        q = q_from_cutoff(edge) if edge else 0
        return Sample(False, q, edge, True) if q else Sample(True, 0, 0, True)
    except Stopped:
        raise
    except Exception:
        return TRUSTED
