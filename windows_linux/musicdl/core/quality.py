"""
quality.py — one scale for "how good is this file", so the engine can tell a better version from a merely different one.

Everything is expressed as "MP3-equivalent kbps" (the same scale sources.py uses to rank downloads; lossless = 1411).
A file's *delivered* quality is the lower of what its container can hold and what its source was: a 192 kbps MP3 made
from a lossless original delivers 192; a FLAC made from a 128 kbps video delivers 128.
"""
from ..config import FORMATS, OutFmt

LOSSLESS = 1411                  # same number sources.py gives lossless candidates
AAC_WEIGHT = 1.2                 # AAC at a given bitrate sounds like roughly this much MP3
MIN_GAIN = 1.3                   # a replacement has to be this much better before it is worth asking about
LOSSLESS_EXT = (".flac", ".wav", ".aiff", ".aif")


def _ext(info):
    return str(getattr(info, "ext", "") or "").lower()


def is_lossless_file(ext, kbps=0, flagged=False):
    """FLAC/WAV always are; an .m4a only when it is ALAC (flagged, or — for old records — far too big for AAC)."""
    ext = ext.lower()
    return bool(flagged) or ext in LOSSLESS_EXT or (ext == ".m4a" and kbps > 450)


def container(info):
    """What the file itself could deliver, ignoring where the audio came from."""
    if getattr(info, "lossless", False):
        return LOSSLESS
    k = int(getattr(info, "kbps", 0) or 0)
    return int(k * AAC_WEIGHT) if _ext(info) in (".m4a", ".aac") else k


def delivered(info):
    """The honest quality of a file: container or source, whichever is lower."""
    c = container(info)
    s = int(getattr(info, "src_kbps", 0) or 0)
    return min(c, s) if s and c else (c or s)


def target(fmt):
    """What a chosen output format can deliver at best."""
    if fmt.lossless:
        return LOSSLESS
    return int(fmt.kbps * AAC_WEIGHT) if fmt.key == "aac" else int(fmt.kbps)


def potential(info, tgt):
    """How good this song could get under target `tgt`. A known source caps it: a file whose best source was 128 kbps
    cannot be improved by asking for 320."""
    s = int(getattr(info, "src_kbps", 0) or 0)
    return min(tgt, s) if s else tgt


def worth_upgrading(info, tgt):
    d = delivered(info)
    return 0 < d < LOSSLESS and potential(info, tgt) >= d * MIN_GAIN


def is_better(new_q, old_q):
    """Is a freshly made file enough of an improvement over the one it would replace?"""
    return new_q >= old_q + 16 and new_q >= old_q * 1.1


def label(q):
    return "lossless" if q >= LOSSLESS else f"{int(q)} kbps"


def describe(info):
    """'MP3 192 kbps', 'FLAC lossless' — what a file delivers, in words."""
    return f"{_ext(info).lstrip('.').upper() or 'File'} {label(delivered(info))}"


def lossy_tier(q, bitrates):
    """The smallest selectable bitrate that keeps quality `q` (capped at the highest one)."""
    ok = [b for b in sorted(bitrates) if b >= q]
    return ok[0] if ok else max(bitrates)


# ---------------------------------------------------------------- the source cap (Optimized mode)

LOSSY_CEILING = {"mp3": 320, "aac": 256}     # the most a lossy stand-in for a lossless choice is ever written at
UNKNOWN_Q = 192                               # a lossy source of unknown bitrate: assume this much (rounds to a tier)


def fit(fmt, lossless, q):
    """The format a song is really written in: what was chosen, but never more than its source can fill.

    * lossless source  -> exactly the format chosen (FLAC/ALAC stay FLAC/ALAC; MP3/AAC stay as chosen)
    * lossy source     -> a chosen lossy format keeps its bitrate only if the source has that much to give, otherwise
                          the smallest bitrate that holds everything the source has; a chosen *lossless* format becomes
                          MP3 (for FLAC) or AAC (for ALAC) at that bitrate, because a lossless copy of lossy sound is
                          just a much bigger file.
    `q` is the source quality in MP3-equivalent kbps (0 = unknown). Returns `fmt` itself when nothing changes."""
    if lossless:
        return fmt
    base = {"flac": "mp3", "wav": "mp3", "alac": "aac"}.get(fmt.key, fmt.key)
    if not q and not fmt.lossless:
        return fmt                                            # a lossy choice with nothing known about the source
    bitrates = FORMATS[base]["bitrates"]
    weight = AAC_WEIGHT if base == "aac" else 1.0
    top = fmt.kbps if not fmt.lossless else LOSSY_CEILING[base]
    kbps = min(top, lossy_tier((q or UNKNOWN_Q) / weight, bitrates))
    if base == fmt.key and kbps == fmt.kbps:
        return fmt
    return OutFmt(base, kbps=kbps, bit_depth=16)
