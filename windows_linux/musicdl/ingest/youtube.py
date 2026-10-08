"""
youtube.py — YouTube and YouTube Music videos, playlists, albums and channels (also any other site yt-dlp knows).

yt-dlp lists a playlist without downloading anything ("flat" extraction), which is fast and light. Every entry
becomes a Track that points at its own video, so downloading it needs no searching.
"""
import re
from urllib.parse import parse_qs, urlsplit

from ..core.models import Collection, Track
from .base import ResolveError, host_of, squash
from .clean import clean_channel, clean_title, split_video_title

MAX_ENTRIES = 500
LINK = re.compile(r"(?:youtube\.com|youtu\.be|music\.youtube\.com)/", re.I)
_UNAVAILABLE = re.compile(r"^\[(?:private|deleted|unavailable)[^\]]*\]$", re.I)


def match(text):
    return bool(LINK.search(text))


def normalize(url):
    """A canonical URL, deciding whether a watch link that also carries a list means the video or the list."""
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    host = parts.netloc.lower().removeprefix("www.").removeprefix("m.")
    lst = (q.get("list") or [""])[0]
    music = host == "music.youtube.com"
    base = "https://music.youtube.com" if music else "https://www.youtube.com"
    if lst and not lst.startswith(("RD", "UL")):             # auto-generated mixes are endless: just take the video
        return f"{base}/playlist?list={lst}", True
    if host == "youtu.be":
        return f"https://www.youtube.com/watch?v={parts.path.strip('/')}", False
    if (q.get("v") or [""])[0]:
        return f"{base}/watch?v={q['v'][0]}", False
    return url, "/playlist" in parts.path or "/channel/" in parts.path or "/@" in parts.path


def _thumb(info):
    thumbs = [t for t in (info.get("thumbnails") or []) if t.get("url")]
    if not thumbs:
        return info.get("thumbnail") or ""
    return max(thumbs, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))["url"]


def _friendly(err):
    msg = re.sub(r"\x1b\[[0-9;]*m|^ERROR:\s*|\[\w+\]\s*", "", str(err)).strip().splitlines()[0] if str(err) else ""
    low = msg.lower()
    if "private" in low:
        return "That’s private, so it can’t be read."
    if "sign in" in low or "age" in low and "restrict" in low:
        return "YouTube wants a sign-in for that one (age-restricted or members-only)."
    if "not available" in low or "unavailable" in low or "removed" in low:
        return "That video or playlist isn’t available."
    if "unsupported url" in low:
        return "That link isn’t one I can read songs from."
    if "urlopen" in low or "timed out" in low or "network" in low or "connection" in low:
        return "Couldn’t reach the site — check the internet connection."
    return msg[:160] or "Couldn’t read that link."


def extract(url, ctx):
    """Run yt-dlp once and return its info dict (flat for playlists)."""
    try:
        from yt_dlp import YoutubeDL
        from yt_dlp.utils import DownloadError
    except ImportError:
        raise ResolveError("yt-dlp isn’t installed. Run:  pip install yt-dlp") from None
    opts = {"quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 20, "skip_download": True,
            "extract_flat": "in_playlist", "playlistend": MAX_ENTRIES, "lazy_playlist": True, "ignoreerrors": False}
    try:
        with YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
    except DownloadError as e:
        raise ResolveError(_friendly(e)) from None
    except Exception as e:                                   # yt-dlp raises many types for network trouble
        raise ResolveError(_friendly(e)) from None
    if ctx.stop.is_set():
        raise ResolveError("Cancelled")
    if not info:
        raise ResolveError("Nothing was found at that link")
    return info


def _flatten(info):
    """Entries of a playlist; a channel page nests tabs, so use the Videos tab (or the first one)."""
    entries = [e for e in (info.get("entries") or []) if e]
    if entries and all(e.get("_type") == "playlist" or (e.get("ie_key") == "YoutubeTab") for e in entries):
        tab = next((e for e in entries if str(e.get("title", "")).lower().endswith("videos")), entries[0])
        return _flatten(tab) if tab.get("entries") else []
    return entries


def _watch_url(entry, music):
    vid = entry.get("id") or ""
    if re.fullmatch(r"[\w-]{11}", vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return entry.get("url") or entry.get("webpage_url") or ""


def track_from(entry, music, album="", year="", art="", index=0):
    title_raw, channel = entry.get("title") or "", entry.get("channel") or entry.get("uploader") or ""
    if entry.get("track") or entry.get("artist"):            # YouTube Music supplies real song metadata
        title = squash(entry.get("track") or clean_title(title_raw))
        artist = squash(entry.get("artist") or clean_channel(channel))
        album = squash(entry.get("album")) or album
    elif music:
        title, artist = clean_title(title_raw), clean_channel(channel)
    else:
        artist, title = split_video_title(title_raw, channel)
    year = str(entry.get("release_year") or year or "")
    return Track(title=title, artist=artist or "Unknown artist", album=album, year=year if year.isdigit() else "",
                 duration=float(entry.get("duration") or 0), artwork=art, url=_watch_url(entry, music),
                 service="ytmusic" if music else "youtube", track_no=index)


def to_collection(info, music, source=""):
    entries = _flatten(info) if info.get("_type") in ("playlist", "multi_video") or info.get("entries") else None
    if entries is None:                                      # a single video
        t = track_from(info, music)
        t.url = info.get("webpage_url") or t.url
        t.artwork = ""
        return Collection(title=t.title, subtitle=t.artist, tracks=[t], service=t.service, kind="track",
                          artwork=_thumb(info), source=source)
    list_id = str(info.get("id") or "")
    title = squash(info.get("title"))
    is_album = list_id.startswith("OLAK5uy") or title.startswith("Album - ")
    album = re.sub(r"^Album\s*-\s*", "", title) if is_album else ""
    art = _thumb(info)
    tracks, skipped = [], 0
    for e in entries:
        if not e.get("id") and not e.get("url"):
            continue
        if _UNAVAILABLE.match(str(e.get("title") or "")) or not e.get("title"):
            skipped += 1
            continue
        tracks.append(track_from(e, music, album=album, art=art if is_album else "",
                                 index=len(tracks) + 1 if is_album else 0))
    if not tracks:
        raise ResolveError("No playable videos in that list")
    notes = []
    if skipped:
        notes.append(f"{skipped} private or removed video{'s were' if skipped != 1 else ' was'} left out.")
    if len(entries) >= MAX_ENTRIES:
        notes.append(f"Only the first {MAX_ENTRIES} videos were loaded.")
    owner = clean_channel(squash(info.get("uploader") or info.get("channel"))) or (tracks[0].artist if is_album else "")
    return Collection(title=album or title or "Playlist", subtitle=owner, tracks=tracks,
                      service="ytmusic" if music else "youtube", kind="album" if is_album else "playlist",
                      artwork=art, notes=notes, source=source)


def resolve(text, ctx):
    url = next((w for w in text.split() if "youtu" in w), text).strip()
    if not url.startswith("http"):
        url = "https://" + url
    music = host_of(url) == "music.youtube.com"
    canon, _ = normalize(url)
    ctx.status("Reading YouTube Music…" if music else "Reading YouTube…")
    return to_collection(extract(canon, ctx), music, source=text)
