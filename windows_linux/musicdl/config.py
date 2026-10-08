"""
config.py — every preference in one dataclass, the quality presets, and API-key storage.

Easy mode only ever looks at `preset`; Advanced mode reads the individual format fields. The rest of the
program asks the Settings object (out_format(), use_art(), name_template() …) and never checks the mode.
"""
import json
import os
import shlex
from dataclasses import asdict, dataclass, field, fields

from . import platform_

SETTINGS_FILE = "settings.json"
SECRETS_FILE = "secrets.json"
DEFAULT_TEMPLATE = "{title} - {artist}"          # same names V1/V2 used, so an existing library is recognised

# ---------------------------------------------------------------- formats and presets

# key -> label, file extension, lossless?, selectable bitrates (kbps; empty = not applicable)
FORMATS = {
    "mp3":  dict(label="MP3",  ext=".mp3",  lossless=False, bitrates=(96, 128, 160, 192, 224, 256, 320)),
    "aac":  dict(label="AAC",  ext=".m4a",  lossless=False, bitrates=(96, 128, 160, 192, 224, 256, 320)),
    "flac": dict(label="FLAC", ext=".flac", lossless=True,  bitrates=()),
    "alac": dict(label="ALAC", ext=".m4a",  lossless=True,  bitrates=()),
    "wav":  dict(label="WAV",  ext=".wav",  lossless=True,  bitrates=()),
}
SAMPLE_RATES = (0, 44100, 48000, 96000)           # 0 = keep the source's
BIT_DEPTHS = (16, 24)

# Easy mode. `size_kbps` is the average data rate used for the storage estimate.
PRESETS = {
    "good":   dict(label="Good",   fmt="mp3",  kbps=192, size_kbps=192, quality=0.78,
                   tagline="Great for everyday listening", detail="MP3 · 192 kbps"),
    "better": dict(label="Better", fmt="mp3",  kbps=320, size_kbps=320, quality=0.93,
                   tagline="Near-transparent sound", detail="MP3 · 320 kbps"),
    "best":   dict(label="Best",   fmt="flac", kbps=0,   size_kbps=900, quality=1.0,
                   tagline="Lossless, true to the source", detail="FLAC · lossless"),
}
PRESET_ORDER = ("good", "better", "best")

# Optimized mode also asks what the music is for. Apple devices play AAC and ALAC natively; everything else plays MP3
# and FLAC. The same three quality steps, written in the format that suits the device (AAC 160 ≈ MP3 192, AAC 256 ≈
# MP3 320 — see quality.AAC_WEIGHT).
DEVICES = {"apple": "Apple", "other": "Windows · Android"}
DEVICE_ORDER = ("apple", "other")
APPLE_PRESETS = {
    "good":   dict(fmt="aac",  kbps=160, size_kbps=160, detail="AAC · 160 kbps"),
    "better": dict(fmt="aac",  kbps=256, size_kbps=256, detail="AAC · 256 kbps"),
    "best":   dict(fmt="alac", kbps=0,   size_kbps=950, detail="ALAC · lossless"),
}


def default_device():
    return "apple" if platform_.OS_NAME == "macos" else "other"


def preset(key, device="other"):
    """The quality step `key` as it is written for `device`: fmt, kbps, size_kbps, detail, label, tagline, quality."""
    p = dict(PRESETS[key if key in PRESETS else "good"])
    if device == "apple":
        p.update(APPLE_PRESETS[key if key in PRESETS else "good"])
    return p

PROVIDERS = {"ollama": "Ollama", "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Gemini"}
KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "gemini": "GEMINI_API_KEY"}

MIN_KBPS_CHOICES = (0, 128, 192, 256)


@dataclass
class OutFmt:
    """What to write: container/codec, bitrate, and optional resampling."""
    key: str = "mp3"
    kbps: int = 192
    sample_rate: int = 0
    bit_depth: int = 16
    flags: list = field(default_factory=list)       # extra ffmpeg output arguments (Advanced)

    @property
    def ext(self):
        return FORMATS[self.key]["ext"]

    @property
    def lossless(self):
        return FORMATS[self.key]["lossless"]

    @property
    def label(self):
        f = FORMATS[self.key]["label"]
        return f if self.lossless else f"{f} {self.kbps} kbps"

    def signature(self):
        return [self.key, self.kbps if not self.lossless else 0, self.sample_rate, self.bit_depth, list(self.flags)]


def estimate_mb(seconds, size_kbps):
    return seconds * size_kbps * 1000 / 8 / 1e6


def size_kbps_of(fmt):
    """Average data rate (kbps) for storage estimates."""
    if fmt.key in ("mp3", "aac"):
        return fmt.kbps
    bits = 24 if fmt.bit_depth == 24 else 16
    rate = fmt.sample_rate or 44100
    if fmt.key == "wav":
        return rate * 2 * bits / 1000
    return rate * 2 * bits / 1000 * (0.64 if fmt.key == "flac" else 0.60)     # typical lossless ratios


# ---------------------------------------------------------------- settings

@dataclass
class Settings:
    # mode
    mode: str = "easy"                 # easy | advanced
    preset: str = "good"               # good | better | best      (easy)
    device: str = field(default_factory=default_device)    # apple | other   (easy: AAC/ALAC or MP3/FLAC)
    # advanced output
    fmt: str = "mp3"
    bitrate: int = 192
    sample_rate: int = 0
    bit_depth: int = 16
    encoder_flags: str = ""
    match_source: bool = True          # never write more than the source holds (no padded bitrates, no fake lossless)
    embed_art: bool = True
    write_tags: bool = True
    fetch_lyrics: bool = False
    lrc_files: bool = False
    template: str = DEFAULT_TEMPLATE
    # audio finishing (see audio/process.py) — Advanced; Optimized mode has just the `polish` switch
    level: bool = False                # bring every song to the same loudness
    level_target: int = -14            # LUFS: -18 quiet · -14 balanced · -11 loud
    trim: bool = False                 # cut dead silence at the start and end
    trim_db: int = -50                 # below this level counts as silence
    fade: str = "off"                  # off | short | long
    enhance: str = "off"               # off | clarity | warmth | bass | vocal
    dynamics: str = "off"              # off | gentle | strong
    polish: bool = False               # Optimized mode: even out the volume and trim silence
    welcomed: bool = False             # the first-run sheets have been seen (or skipped)
    shield_lock: bool = False          # the app's own files are read-only against deletion and change (core/shield.py)
    clean_versions: bool = False       # False = explicit (the song as released, tagged explicit); True = clean edits
    # performance
    auto: bool = True
    parallel: int = 3
    retries: int = 2
    # accuracy
    tolerance: int = 15
    min_kbps: int = 128
    replace_low: bool = True
    youtube: bool = True
    verify: bool = True
    upgrade: str = "ask"               # ask | replace | keep — songs you already have, in lower quality than you now chose
    close_match: str = "ask"           # ask | auto | skip — a song with no exact match: offer close ones / take a safe one / don't look
    # interface
    appearance: str = "auto"           # auto | light | dark
    sounds: bool = True
    volume: int = 60
    splash: bool = True
    motion: str = "full"               # full | reduced — less animation (also set by Windows' "Show animations" switch)
    outdir: str = ""
    # AI mode (keys are stored separately, see get_secret)
    ai_enabled: bool = False
    ai_provider: str = "ollama"
    ai_model: str = ""
    ollama_url: str = "http://localhost:11434"
    ai_repair: bool = True
    ai_organize: bool = False
    # written by autotune
    auto_parallel: int = 0
    auto_retries: int = 0
    auto_info: dict = field(default_factory=dict)

    # ---- derived values (the rest of the app only uses these)

    @property
    def advanced(self):
        return self.mode == "advanced"

    @property
    def capped(self):
        """Never write a file bigger than its source deserves (see quality.fit). Optimized mode always does; Advanced
        does unless 'Match the source' is switched off."""
        return (not self.advanced) or bool(self.match_source)

    def preset_info(self):
        return preset(self.preset, self.device)

    def out_format(self):
        if not self.advanced:
            p = self.preset_info()
            return OutFmt(p["fmt"], kbps=p["kbps"], bit_depth=0)          # lossless keeps the source's depth
        try:
            flags = shlex.split(self.encoder_flags, posix=not platform_.IS_WINDOWS)
        except ValueError:
            flags = []
        rate = 48000 if self.fmt == "mp3" and self.sample_rate > 48000 else self.sample_rate     # MP3 tops out at 48 kHz
        return OutFmt(self.fmt, kbps=self.bitrate, sample_rate=rate, bit_depth=self.bit_depth, flags=flags)

    def quality_name(self):
        """The quality choice in words, for messages: 'Best — FLAC · lossless' or 'MP3 320 kbps'."""
        if not self.advanced:
            p = self.preset_info()
            return f"{p['label']} — {p['detail']}"
        return self.out_format().label

    def use_art(self):
        return True if not self.advanced else self.embed_art

    def use_tags(self):
        return True if not self.advanced else self.write_tags

    def use_lyrics(self):
        return False if not self.advanced else self.fetch_lyrics

    def use_lrc(self):
        return self.advanced and self.fetch_lyrics and self.lrc_files

    def audio_spec(self):
        """What to do to each song's sound (audio.process.Spec), or None to leave it exactly as downloaded."""
        from .audio import process
        if self.advanced:
            spec = process.Spec(level=bool(self.level), target=float(self.level_target), trim=bool(self.trim),
                                trim_db=int(self.trim_db), fade=self.fade, enhance=self.enhance, dynamics=self.dynamics)
        elif self.polish:
            v = process.PROFILES["playlist"][1]
            spec = process.Spec(level=True, target=float(v["level_target"]), trim=True, trim_db=int(v["trim_db"]))
        else:
            return None
        return spec if spec.active else None

    def name_template(self):
        return DEFAULT_TEMPLATE if not self.advanced or not self.template.strip() else self.template.strip()

    def effective_parallel(self):
        return self.auto_parallel if (self.auto and self.auto_parallel) else self.parallel

    def effective_retries(self):
        return self.auto_retries if (self.auto and self.auto_retries) else self.retries

    # ---- persistence

    def clamp(self):
        self.mode = self.mode if self.mode in ("easy", "advanced") else "easy"
        self.preset = self.preset if self.preset in PRESETS else "good"
        self.device = self.device if self.device in DEVICES else default_device()
        self.fmt = self.fmt if self.fmt in FORMATS else "mp3"
        self.parallel = max(1, min(16, int(self.parallel)))
        self.retries = max(0, min(5, int(self.retries)))
        self.tolerance = max(5, min(50, int(self.tolerance)))
        self.min_kbps = int(self.min_kbps) if int(self.min_kbps) in MIN_KBPS_CHOICES else 128
        self.sample_rate = int(self.sample_rate) if int(self.sample_rate) in SAMPLE_RATES else 0
        self.bit_depth = 24 if int(self.bit_depth) == 24 else 16
        self.volume = max(0, min(100, int(self.volume)))
        br = FORMATS[self.fmt]["bitrates"]
        if br and self.bitrate not in br:
            self.bitrate = min(br, key=lambda b: abs(b - int(self.bitrate)))
        if self.appearance not in ("auto", "light", "dark"):
            self.appearance = "auto"
        if self.upgrade not in ("ask", "replace", "keep"):
            self.upgrade = "ask"
        if self.close_match not in ("ask", "auto", "skip"):
            self.close_match = "ask"
        if self.motion not in ("full", "reduced"):
            self.motion = "full"
        from .audio import process
        self.level_target = max(-24, min(-8, int(self.level_target)))
        self.trim_db = max(-70, min(-30, int(self.trim_db)))
        if self.fade not in process.FADES:
            self.fade = "off"
        if self.enhance != "off" and self.enhance not in process.ENHANCE:
            self.enhance = "off"
        if self.dynamics != "off" and self.dynamics not in process.DYNAMICS:
            self.dynamics = "off"
        if self.ai_provider not in PROVIDERS:
            self.ai_provider = "ollama"
        return self

    @staticmethod
    def path():
        return os.path.join(platform_.config_dir(), SETTINGS_FILE)

    @classmethod
    def load(cls):
        s = cls()
        try:
            from .core import shield
            shield.heal_settings(platform_.config_dir())              # a deleted or damaged file: the last good copy
        except Exception:
            pass
        try:
            with open(cls.path(), encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                raise ValueError("not a settings object")
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known and type(v) is type(getattr(s, k)):
                    setattr(s, k, v)
        except OSError:
            pass
        except ValueError:                                            # damaged and no good copy: keep it aside, start afresh
            from .core import shield
            shield.set_aside(cls.path())
        if not s.outdir:
            s.outdir = platform_.default_music_dir()
        try:
            return s.clamp()
        except (TypeError, ValueError):
            fresh = cls()
            fresh.outdir = platform_.default_music_dir()
            return fresh.clamp()

    def save(self):
        try:
            tmp = self.path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(asdict(self), fh, indent=2)
            os.replace(tmp, self.path())
        except OSError:
            pass


# ---------------------------------------------------------------- API keys
# Stored in the OS keyring when the optional `keyring` package is installed; otherwise in a file that only
# this user can read (chmod 600; on Windows the per-user AppData folder). Environment variables always work too.

_KEYRING_SERVICE = "MusicDownloader"


def _secrets_path():
    return os.path.join(platform_.config_dir(), SECRETS_FILE)


def get_secret(provider):
    env = KEY_ENV.get(provider)
    if env and os.environ.get(env):
        return os.environ[env].strip()
    try:
        import keyring
        v = keyring.get_password(_KEYRING_SERVICE, provider)
        if v:
            return v
    except Exception:
        pass
    try:
        with open(_secrets_path(), encoding="utf-8") as fh:
            return str(json.load(fh).get(provider, "")).strip()
    except (OSError, ValueError):
        return ""


def set_secret(provider, value):
    value = (value or "").strip()
    try:
        import keyring
        if value:
            keyring.set_password(_KEYRING_SERVICE, provider, value)
        else:
            try:
                keyring.delete_password(_KEYRING_SERVICE, provider)
            except Exception:
                pass
        return
    except Exception:
        pass
    data = {}
    try:
        with open(_secrets_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        pass
    if value:
        data[provider] = value
    else:
        data.pop(provider, None)
    tmp = _secrets_path() + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, _secrets_path())
    try:
        os.chmod(_secrets_path(), 0o600)
    except OSError:
        pass
