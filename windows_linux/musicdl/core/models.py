"""models.py — the few data types everything else passes around."""
from dataclasses import dataclass, field

ATTENTION = ("no-file", "bad-length", "dl-fail", "error")
READY = ("ok", "skipped", "upgraded", "fixed", "kept")


@dataclass
class Track:
    """One song to fetch. Only title and artist are required; everything else just makes the result better."""
    title: str
    artist: str
    album: str = ""
    year: str = ""
    genre: str = ""
    duration: float = 0.0          # seconds, 0 = unknown
    artwork: str = ""              # image URL
    url: str = ""                  # a page that plays exactly this recording (YouTube…): no searching needed
    service: str = ""              # spotify | apple | youtube | ytmusic | amazon | pandora | search | sheet | ai | web
    track_no: int = 0
    disc_no: int = 0
    isrc: str = ""
    mbid: str = ""                 # MusicBrainz recording id
    album_artist: str = ""         # who the album is by ("Various Artists" for a compilation)
    track_total: int = 0           # tracks on the disc
    disc_total: int = 0
    date: str = ""                 # full release date (YYYY-MM-DD) when known; `year` is always the 4-digit year
    explicit: int = 0              # 1 explicit, 2 clean version
    compilation: bool = False
    record_label: str = ""         # (not `label`: that is the method below)
    extra: dict = field(default_factory=dict)

    @property
    def direct(self):
        return bool(self.url)

    def label(self):
        return f"{self.title} — {self.artist}"


@dataclass
class Collection:
    """What a link resolves to: a named list of tracks."""
    title: str
    tracks: list
    service: str = ""
    kind: str = "playlist"         # playlist | album | track | artist | station | search | sheet | ai
    subtitle: str = ""             # owner / artist
    artwork: str = ""
    source: str = ""               # the text the user pasted
    notes: list = field(default_factory=list)       # things worth telling the user (e.g. "first 100 of 150")

    @property
    def seconds(self):
        return sum(t.duration or 210.0 for t in self.tracks)


@dataclass
class Result:
    status: str                    # ok | upgraded | fixed | skipped | kept | no-file | bad-length | dl-fail | error | stopped
    track: str = ""
    artist: str = ""
    note: str = ""
    kbps: int = 0                  # bitrate of the saved file
    src_kbps: int = 0              # estimated quality of the source it came from (1411 = lossless)
    seconds: float = 0.0
    cover: bool = False
    source: str = ""
    low_quality: bool = False
    no_cover: bool = False
    thumb: bytes = None
    path: str = ""
    size: int = 0
    lyrics: bool = False

    @property
    def attention(self):
        return self.status in ATTENTION or self.low_quality or self.no_cover

    @property
    def quality_kbps(self):
        return self.src_kbps or self.kbps


class Stopped(Exception):
    """The user pressed Stop."""


class EngineError(Exception):
    pass


class DiskFull(EngineError):
    """There is no room left to write (the run pauses and waits instead of failing the song)."""
