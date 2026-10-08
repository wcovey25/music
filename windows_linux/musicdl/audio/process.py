"""
process.py — finishing touches on a song's sound, done by ffmpeg in the same pass that writes the file.

  even out the volume   every song is brought to the same loudness (EBU R128, measured in LUFS — the scale streaming
                        services use), with a limiter so the louder ones can't clip
  trim silence          dead air before the music starts and after it ends is cut, with a soft fade at the cut
  fades                 a short fade-in and a longer fade-out
  sound                 a gentle EQ preset (clarity, warmth, bass, vocal) and a dynamics setting that evens out the
                        loud and quiet moments inside a song

analyze() listens to the downloaded file once — decode only, a few hundred times faster than real time — and returns an
Fx: the exact filter chain for *this* song (how much to turn it up, where to cut). transcode.encode puts the chain in
front of the encoder, so there is no second encode and no extra generation of loss.

Nothing here may fail a song. If the file can't be analysed, or ffmpeg lacks a filter, analyze() returns None (or
filters out what it can't do) and the song is saved untouched; the engine notes it.
"""
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field

from .. import platform_
from ..core.models import EngineError
from . import transcode

log = logging.getLogger("musicdl")

# ---------------------------------------------------------------- the choices

LEVELS = ((-18, "Quiet"), (-14, "Balanced"), (-11, "Loud"))              # LUFS
TRIMS = ((-60, "Careful"), (-50, "Balanced"), (-42, "Tight"))            # dBFS: below this counts as silence
FADES = {"off": (0.0, 0.0), "short": (0.4, 2.0), "long": (1.5, 6.0)}     # (in, out) seconds
FADE_CHOICES = (("off", "Off"), ("short", "Short"), ("long", "Long"))

# name -> (label, what it does, ffmpeg filters). Gentle on purpose: a few dB, in the places that matter, then limited.
ENHANCE = {
    "clarity": ("Clarity", "Lifts the detail and presence of voices and instruments",
                ["highpass=f=28", "equalizer=f=3400:width_type=q:width=0.9:g=1.6",
                 "treble=g=1.6:f=9000:width_type=s:width=0.6"]),
    "warmth": ("Warmth", "Fuller low end and softer highs, easy on the ears",
               ["bass=g=2.4:f=120:width_type=s:width=0.6", "equalizer=f=250:width_type=q:width=0.9:g=0.8",
                "treble=g=-1.4:f=9500:width_type=s:width=0.6"]),
    "bass": ("Bass boost", "More weight and punch in the low end",
             ["bass=g=5:f=90:width_type=s:width=0.7", "equalizer=f=55:width_type=q:width=1.1:g=1.5"]),
    "vocal": ("Vocal focus", "Brings voices forward, clears the muddy middle",
              ["highpass=f=40", "equalizer=f=300:width_type=q:width=1:g=-1.2",
               "equalizer=f=2800:width_type=q:width=0.8:g=2.2"]),
}
ENHANCE_CHOICES = (("off", "Off"),) + tuple((k, v[0]) for k, v in ENHANCE.items())

DYNAMICS = {                                   # threshold is linear: 0.1 = −20 dB, 0.063 = −24 dB
    "gentle": ("Gentle", "acompressor=threshold=0.1:ratio=1.8:attack=40:release=300:makeup=1.5:knee=6"),
    "strong": ("Strong", "acompressor=threshold=0.063:ratio=3.2:attack=20:release=250:makeup=2.5:knee=4"),
}
DYNAMICS_CHOICES = (("off", "Off"),) + tuple((k, v[0]) for k, v in DYNAMICS.items())

# One tap to set it all up. (level, target, trim, trim_db, fade, enhance, dynamics)
PROFILES = {
    "off": ("Off", dict(level=False, trim=False, fade="off", enhance="off", dynamics="off")),
    "playlist": ("Playlist ready", dict(level=True, level_target=-14, trim=True, trim_db=-50, fade="off",
                                        enhance="off", dynamics="off")),
    "car": ("Car & speakers", dict(level=True, level_target=-11, trim=True, trim_db=-50, fade="off",
                                   enhance="clarity", dynamics="gentle")),
    "night": ("Late night", dict(level=True, level_target=-18, trim=True, trim_db=-50, fade="short",
                                 enhance="warmth", dynamics="strong")),
}
PROFILE_FIELDS = ("level", "level_target", "trim", "trim_db", "fade", "enhance", "dynamics")

# ---------------------------------------------------------------- how far it may go

PEAK_CEILING = 0.891          # the limiter's ceiling: −1 dBFS
SILENT_LUFS = -69.5           # at or below this the integrated loudness means "digital silence", not "very quiet"
MAX_BOOST_DB, MAX_CUT_DB = 14.0, 20.0
MIN_GAIN_DB = 0.3             # closer than this to the target counts as already there
LEAD_KEEP, TAIL_KEEP = 0.10, 0.30      # seconds of the silence kept: a breath before the music, the ending's last ring
MIN_CUT_LEAD, MIN_CUT_TAIL = 0.30, 0.50   # not worth touching a song for less than this
CUT_FADE_IN, CUT_FADE_OUT = 0.006, 0.25   # at a trimmed start / end, so a cut never clicks
MAX_TRIM_FRACTION = 0.5       # never cut away more than half of a song
MIN_KEEP_SECONDS = 5.0


@dataclass(frozen=True)
class Spec:
    """What the user asked for (see Settings.audio_spec)."""
    level: bool = False
    target: float = -14.0
    trim: bool = False
    trim_db: int = -50
    fade: str = "off"
    enhance: str = "off"
    dynamics: str = "off"

    @property
    def active(self):
        return bool(self.level or self.trim or self.fade != "off" or self.enhance != "off" or self.dynamics != "off")

    @property
    def shapes_sound(self):
        return self.enhance != "off" or self.dynamics != "off"


@dataclass
class Fx:
    """The filter chain worked out for one song."""
    filters: list = field(default_factory=list)
    seconds: float = 0.0               # length of the source
    removed: float = 0.0               # seconds trimmed off
    gain_db: float = 0.0               # volume change applied
    loudness: float = None             # LUFS measured before the change (None when not measured)
    notes: list = field(default_factory=list)

    @property
    def active(self):
        return bool(self.filters)

    def chain(self, dither=False):
        """The -af argument. `dither`: the file will be written as 16-bit PCM, so round it properly."""
        parts = list(self.filters)
        if dither and parts:
            parts.append("aresample=osf=s16:dither_method=triangular_hp")
        return ",".join(parts)

    def note(self):
        return " · ".join(self.notes)


# ---------------------------------------------------------------- profiles

def profile_of(settings):
    """The key of the profile the current options match exactly, or '' (custom)."""
    for key, (_label, vals) in PROFILES.items():
        base = dict(level=False, level_target=-14, trim=False, trim_db=-50, fade="off", enhance="off", dynamics="off")
        base.update(vals)
        if not base["level"]:
            base["level_target"] = getattr(settings, "level_target")        # the target means nothing while it is off
        if not base["trim"]:
            base["trim_db"] = getattr(settings, "trim_db")
        if all(getattr(settings, f) == base[f] for f in PROFILE_FIELDS):
            return key
    return ""


def apply_profile(settings, key):
    if key in PROFILES:
        for f, v in PROFILES[key][1].items():
            setattr(settings, f, v)


# ---------------------------------------------------------------- ffmpeg support

_FILTERS = {"silencedetect", "ebur128", "volume", "alimiter", "afade", "atrim", "asetpts", "equalizer", "bass", "treble",
            "highpass", "acompressor", "aresample"}
_have = None
_have_lock = threading.Lock()


def available_filters():
    """Names of the audio filters this ffmpeg has (cached)."""
    global _have
    with _have_lock:
        if _have is None:
            found = set()
            ff = transcode.ffmpeg_path()
            if ff:
                try:
                    out = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=15,
                                         creationflags=platform_.NO_WINDOW).stdout
                    for line in out.splitlines():
                        parts = line.split()
                        if len(parts) >= 2 and len(parts[0]) == 3 and parts[1] in _FILTERS:
                            found.add(parts[1])
                except Exception:
                    pass
            _have = found
        return _have


def missing_filters():
    """Filters the audio options need that this ffmpeg lacks (empty on any normal build)."""
    return sorted(_FILTERS - available_filters()) if transcode.ffmpeg_path() else []


# ---------------------------------------------------------------- listening to the file

@dataclass
class Probe:
    seconds: float = 0.0
    silences: list = field(default_factory=list)      # [(start, end or None)] — None: runs to the end
    loudness: float = None                            # integrated, LUFS
    peak: float = None                                # sample peak, dBFS (the limiter, not this, keeps the song from clipping)


def _shaping(spec):
    out = []
    if spec.enhance in ENHANCE:
        out += ENHANCE[spec.enhance][2]
    if spec.dynamics in DYNAMICS:
        out.append(DYNAMICS[spec.dynamics][1])
    return out


def _hms(text):
    h, m, s = text.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def parse_probe(text):
    """Read what ffmpeg printed while it listened: length, silent stretches, loudness."""
    p = Probe()
    times = re.findall(r"time=(\d+:\d+:\d+(?:\.\d+)?)", text)
    if times:
        p.seconds = _hms(times[-1])
    if not p.seconds:
        m = re.search(r"Duration:\s*(\d+:\d+:\d+(?:\.\d+)?)", text)
        if m:
            p.seconds = _hms(m.group(1))
    open_start = None
    for m in re.finditer(r"silence_(start|end):\s*(-?[\d.]+)", text):
        t = max(0.0, float(m.group(2)))
        if m.group(1) == "start":
            open_start = t
        elif open_start is not None:
            p.silences.append((open_start, t))
            open_start = None
        else:
            p.silences.append((0.0, t))
    if open_start is not None:
        p.silences.append((open_start, None))
    tail = text[text.rfind("Summary:"):] if "Summary:" in text else ""
    m = re.search(r"\bI:\s*(-?[\d.]+|-inf)\s*LUFS", tail)
    if m and m.group(1) != "-inf" and float(m.group(1)) > SILENT_LUFS:         # -70 is the gate's floor: nothing was heard
        p.loudness = float(m.group(1))
    m = re.search(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", tail)
    if m and m.group(1) != "-inf":
        p.peak = float(m.group(1))
    return p


def listen(src, spec, stop=None):
    """One decode-only pass over `src`. Returns a Probe, or None if ffmpeg could not do it."""
    ff = transcode.ffmpeg_path()
    if not ff:
        return None
    have = available_filters()
    chain = []
    if spec.trim and "silencedetect" in have:
        chain.append(f"silencedetect=noise={int(spec.trim_db)}dB:d=0.2")
    if spec.level and "ebur128" in have:
        chain += [f for f in _shaping(spec) if f.split("=")[0] in have]          # measured as it will sound
        chain.append("ebur128=peak=sample:framelog=quiet")             # (true-peak metering costs five times as much)
    cmd = [ff, "-nostdin", "-hide_banner", "-v", "info", "-i", src, "-vn"]
    if chain:
        cmd += ["-af", ",".join(chain)]
    cmd += ["-f", "null", "-"]
    try:
        code, err = transcode.run(cmd, stop, timeout=900)
    except EngineError:
        return None
    if code != 0:
        log.info("audio analysis failed: %s", err[-200:])
        return None
    return parse_probe(err)


# ---------------------------------------------------------------- deciding

def trim_bounds(probe, spec):
    """(start, end) seconds to keep, or None when there is nothing worth cutting (or cutting would be unsafe)."""
    dur = probe.seconds
    if not dur or not probe.silences:
        return None
    lead = tail = 0.0
    tail_start = dur
    for s, e in probe.silences:
        if s <= 0.05 and e is not None:
            lead = e
        if e is None or e >= dur - 0.05:
            tail_start = s
            tail = dur - s
    start = max(0.0, lead - LEAD_KEEP) if lead - LEAD_KEEP >= MIN_CUT_LEAD else 0.0
    end = min(dur, tail_start + TAIL_KEEP) if tail - TAIL_KEEP >= MIN_CUT_TAIL else dur
    if start == 0.0 and end == dur:
        return None
    kept = end - start
    if kept < MIN_KEEP_SECONDS or (dur - kept) > MAX_TRIM_FRACTION * dur:
        return None                                      # all silence, or a very quiet song: leave it alone
    return start, end


def plan(probe, spec):
    """The Fx for a song, from what listening found."""
    fx = Fx(seconds=probe.seconds, loudness=probe.loudness)
    have = available_filters()
    start, end = 0.0, probe.seconds
    cut_start = cut_end = False
    if spec.trim and "atrim" in have and "asetpts" in have:
        bounds = trim_bounds(probe, spec)
        if bounds:
            start, end = bounds
            cut_start, cut_end = start > 0.0, end < probe.seconds
            fx.filters.append(f"atrim=start={start:.3f}:end={end:.3f}")
            fx.filters.append("asetpts=PTS-STARTPTS")
            fx.removed = probe.seconds - (end - start)
            fx.notes.append(f"Trimmed {fx.removed:.1f} s of silence")
    for f in _shaping(spec):
        if f.split("=")[0] in have:
            fx.filters.append(f)
    if spec.shapes_sound:
        names = [ENHANCE[spec.enhance][0]] if spec.enhance in ENHANCE else []
        if spec.dynamics in DYNAMICS:
            names.append(DYNAMICS[spec.dynamics][0].lower() + " dynamics")
        fx.notes.append(" + ".join(names))
    gain = 0.0
    if spec.level and probe.loudness is not None and "volume" in have:
        gain = max(-MAX_CUT_DB, min(MAX_BOOST_DB, spec.target - probe.loudness))
        if abs(gain) >= MIN_GAIN_DB:
            fx.filters.append(f"volume={gain:.2f}dB")
            fx.gain_db = gain
            fx.notes.append(f"Volume {gain:+.1f} dB".replace("-", "−"))
        else:
            gain = 0.0                                              # already where it should be
    if (gain > 0 or spec.shapes_sound) and "alimiter" in have:
        fx.filters.append(f"alimiter=limit={PEAK_CEILING}:attack=3:release=50:level=0")
    length = end - start
    fade_in, fade_out = FADES.get(spec.fade, (0.0, 0.0))
    if cut_start:
        fade_in = max(fade_in, CUT_FADE_IN)
    if cut_end:
        fade_out = max(fade_out, CUT_FADE_OUT)
    fade_in, fade_out = min(fade_in, length * 0.3), min(fade_out, length * 0.4)
    if "afade" in have and length > 0:
        if fade_in > 0:
            fx.filters.append(f"afade=t=in:st=0:d={fade_in:.3f}")
        if fade_out > 0:
            fx.filters.append(f"afade=t=out:st={length - fade_out:.3f}:d={fade_out:.3f}")
        if spec.fade != "off":
            fx.notes.append("fades")
    return fx


def analyze(src, spec, stop=None):
    """Listen to `src` and work out its Fx. None when the file can't be examined; an Fx with nothing to do is still
    returned (check .active) so the caller can tell 'already perfect' from 'couldn't look'."""
    if spec is None or not spec.active:
        return None
    probe = listen(src, spec, stop)
    if probe is None or not probe.seconds:
        return None
    return plan(probe, spec)
