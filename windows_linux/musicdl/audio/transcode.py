"""
transcode.py — turn a downloaded source file into the output format with ffmpeg.

Two decisions live here:
  * plan()   copy the audio untouched when the source already matches the target (re-encoding lossy audio
             only loses quality), otherwise encode;
  * encode() build the ffmpeg command for MP3 / AAC / FLAC / ALAC / WAV, honouring sample rate, bit depth
             and any custom flags, and run it without a console window, stoppable at any moment.
"""
import os
import shutil
import subprocess
import threading
import time

from .. import platform_
from ..config import FORMATS, OutFmt
from ..core.models import EngineError, Stopped

_encoders = None
_enc_lock = threading.Lock()

# codec family of a downloaded source, from its file extension / yt-dlp's acodec string
LOSSLESS_CODECS = ("flac", "wav", "alac", "aiff")


def ffmpeg_path():
    return platform_.find_tool("ffmpeg")


def encoders():
    """Names of the audio encoders this ffmpeg build offers (cached)."""
    global _encoders
    with _enc_lock:
        if _encoders is None:
            found = set()
            ff = ffmpeg_path()
            if ff:
                try:
                    out = subprocess.run([ff, "-hide_banner", "-encoders"], capture_output=True, text=True,
                                         timeout=15, creationflags=platform_.NO_WINDOW).stdout
                    for line in out.splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and parts[0].startswith("A"):
                            found.add(parts[1])
                except Exception:
                    pass
            _encoders = found
        return _encoders


def codec_family(ext, acodec=""):
    """'mp3' | 'aac' | 'opus' | 'vorbis' | 'flac' | 'wav' | 'alac' | '' for a source file."""
    a = (acodec or "").lower()
    ext = ext.lower().lstrip(".")
    for fam in ("opus", "vorbis", "flac", "alac", "mp3"):
        if a.startswith(fam):
            return fam
    if a.startswith("mp4a") or a.startswith("aac"):
        return "aac"
    return {"mp3": "mp3", "m4a": "aac", "aac": "aac", "flac": "flac", "wav": "wav", "aiff": "aiff", "aif": "aiff",
            "ogg": "vorbis", "opus": "opus", "webm": "opus", "mp4": "aac"}.get(ext, "")


def bit_depth_of(path):
    """Bits per sample of a lossless source (16 when unknown)."""
    try:
        import mutagen
        info = mutagen.File(path).info
        return int(getattr(info, "bits_per_sample", 16) or 16)
    except Exception:
        return 16


def plan(src_codec, src_kbps, fmt, src_bits=16):
    """'copy' when the source can be used as-is for `fmt`, else 'encode'."""
    if fmt.flags or fmt.sample_rate:
        return "encode"
    if src_codec != fmt.key:
        return "encode"
    if fmt.key in ("mp3", "aac"):
        return "copy" if (not src_kbps or src_kbps <= fmt.kbps * 1.05) else "encode"
    return "encode" if (fmt.bit_depth and fmt.bit_depth < src_bits) else "copy"


def codec_args(fmt, src_bits=16, src_lossless=False):
    k = fmt.key
    if k == "mp3":
        args = ["-c:a", "libmp3lame", "-b:a", f"{fmt.kbps}k"]
    elif k == "aac":
        args = ["-c:a", platform_.preferred_aac_encoder(encoders()), "-b:a", f"{fmt.kbps}k"]
    else:
        depth = fmt.bit_depth or (src_bits if src_lossless and src_bits > 16 else 16)
        if k == "flac":
            args = ["-c:a", "flac", "-compression_level", "8", "-sample_fmt", "s32" if depth > 16 else "s16"]
            if depth > 16:
                args += ["-bits_per_raw_sample", "24"]
        elif k == "alac":
            args = ["-c:a", "alac", "-sample_fmt", "s32p" if depth > 16 else "s16p"]
        else:
            args = ["-c:a", "pcm_s24le" if depth > 16 else "pcm_s16le"]
    if fmt.sample_rate:
        args += ["-ar", str(fmt.sample_rate)]
    return args + list(fmt.flags)


def encode(src, dst, fmt: OutFmt, action="encode", threads=2, stop=None, src_lossless=False):
    """Write `dst` from `src`. Raises Stopped / EngineError."""
    if action == "copy" and fmt.key == "mp3" and src.lower().endswith(".mp3"):
        shutil.copyfile(src, dst)                         # no process needed
        return
    ff = ffmpeg_path()
    if not ff:
        raise EngineError("ffmpeg not found — " + platform_.ffmpeg_hint())
    bits = bit_depth_of(src) if src_lossless else 16
    audio = ["-c:a", "copy"] if action == "copy" else codec_args(fmt, bits, src_lossless)
    cmd = [ff, "-nostdin", "-y", "-v", "error", "-i", src, "-vn", "-map_metadata", "-1", *audio,
           "-threads", str(max(1, min(8, threads))), dst]
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=platform_.NO_WINDOW)
    deadline = time.monotonic() + 1800
    while True:
        try:
            _, err = p.communicate(timeout=0.4)
            break
        except subprocess.TimeoutExpired:
            if (stop is not None and stop.is_set()) or time.monotonic() > deadline:
                p.kill()
                p.communicate()
                if stop is not None and stop.is_set():
                    raise Stopped()
                raise EngineError("ffmpeg timed out") from None
    if p.returncode != 0 or not os.path.exists(dst):
        raise EngineError((err or b"").decode("utf-8", "ignore").strip()[-240:] or "ffmpeg failed")


def extension_of(fmt):
    return FORMATS[fmt.key]["ext"]
