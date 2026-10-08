"""lyrics.py — plain and time-synced lyrics from LRCLIB (free, no account)."""
import logging
import re
from dataclasses import dataclass

from ..core.models import EngineError
from ..core.netio import LYRICS_LIMIT, get_json
from ..core.text import sim

log = logging.getLogger("musicdl")
API = "https://lrclib.net/api"


@dataclass
class Lyrics:
    plain: str = ""
    synced: str = ""          # LRC text: "[00:12.30] words"

    def __bool__(self):
        return bool(self.plain or self.synced)


def _plain_of(synced):
    return "\n".join(re.sub(r"^\[[^\]]*\]\s*", "", line) for line in synced.splitlines()).strip()


def _from(d):
    if not d or d.get("instrumental"):
        return Lyrics()
    plain, synced = (d.get("plainLyrics") or "").strip(), (d.get("syncedLyrics") or "").strip()
    return Lyrics(plain or (_plain_of(synced) if synced else ""), synced)


def fetch(title, artist, album="", seconds=0, stop=None):
    """Best lyrics for a song, or an empty Lyrics. Never raises for 'not found'."""
    try:
        params = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        if seconds:
            params["duration"] = int(round(seconds))
        got = _from(get_json(f"{API}/get", params=params, limiter=LYRICS_LIMIT, retries=2, stop=stop))
        if got:
            return got
        hits = get_json(f"{API}/search", params={"track_name": title, "artist_name": artist},
                        limiter=LYRICS_LIMIT, retries=2, stop=stop) or []
        for h in hits[:8]:
            if seconds and h.get("duration") and abs(float(h["duration"]) - seconds) > 6:
                continue
            if sim(h.get("trackName", ""), title) < 0.8:
                continue
            got = _from(h)
            if got:
                return got
    except EngineError as e:
        log.info("lyrics lookup failed for %s: %s", title, e)
    return Lyrics()
