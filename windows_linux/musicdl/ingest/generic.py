"""
generic.py — any other link: a direct audio file, a site yt-dlp understands (SoundCloud, Bandcamp, Vimeo …),
or a page that describes music with schema.org data.
"""
import os
import re
from urllib.parse import unquote, urlsplit

from ..core.models import Collection, Track
from ..core.text import parse_seconds
from . import youtube
from .base import ResolveError, first_url, page, squash
from .clean import split_video_title
from .webmeta import jsonld

AUDIO_EXT = (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma", ".aiff")
MUSIC_TYPES = {"MusicAlbum", "MusicPlaylist", "MusicRecording", "MusicComposition"}


def iso_seconds(value):
    """'PT3M45S' -> 225.0"""
    m = re.fullmatch(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", str(value or ""))
    if not m or not any(m.groups()):
        return parse_seconds(value) or 0.0
    h, mi, s = (float(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


def _name(x):
    if isinstance(x, list):
        return ", ".join(filter(None, (_name(i) for i in x)))
    return squash(x.get("name") if isinstance(x, dict) else x)


def from_jsonld(html, url=""):
    """schema.org MusicAlbum / MusicPlaylist / MusicRecording -> Collection, or None."""
    for d in jsonld(html):
        types = d.get("@type")
        types = set(types) if isinstance(types, list) else {types}
        if not types & MUSIC_TYPES:
            continue
        title, artist = squash(d.get("name")), _name(d.get("byArtist") or d.get("author"))
        image = d.get("image") if isinstance(d.get("image"), str) else (d.get("image") or {}).get("url", "")
        if "MusicRecording" in types or "MusicComposition" in types:
            if not title:
                continue
            t = Track(title=title, artist=artist or "Unknown artist", album=_name(d.get("inAlbum")),
                      duration=iso_seconds(d.get("duration")), artwork=image or "", service="web")
            return Collection(title=title, subtitle=artist, tracks=[t], service="web", kind="track", artwork=image or "")
        items = d.get("track") or d.get("tracks") or []
        if isinstance(items, dict):
            items = items.get("itemListElement") or []
        tracks = []
        for it in items:
            it = it.get("item", it) if isinstance(it, dict) else {}
            if it.get("name"):
                tracks.append(Track(title=squash(it["name"]), artist=_name(it.get("byArtist")) or artist or "Unknown artist",
                                    album=title if "MusicAlbum" in types else "", duration=iso_seconds(it.get("duration")),
                                    service="web", track_no=len(tracks) + 1 if "MusicAlbum" in types else 0))
        if tracks:
            return Collection(title=title, subtitle=artist, tracks=tracks, service="web",
                              kind="album" if "MusicAlbum" in types else "playlist", artwork=image or "")
    return None


def direct_file(url):
    name = unquote(os.path.splitext(os.path.basename(urlsplit(url).path))[0]).replace("_", " ").strip()
    artist, title = split_video_title(name)
    t = Track(title=title or name or "Track", artist=artist if artist != "Unknown artist" else "Unknown artist",
              url=url, service="web")
    return Collection(title=t.title, subtitle=t.artist, tracks=[t], service="web", kind="track", source=url)


def resolve(text, ctx):
    url = first_url(text) or text.strip()
    if not url.startswith("http"):
        raise ResolveError("That doesn’t look like a link I can use")
    if urlsplit(url).path.lower().endswith(AUDIO_EXT):
        return direct_file(url)
    ctx.status("Reading the page…")
    first_error = None
    try:                                                      # sites with a media extractor (SoundCloud, Bandcamp …)
        col = youtube.to_collection(youtube.extract(url, ctx), False, source=text)
        col.service = "web"
        for t in col.tracks:
            t.service = "web"
        return col
    except ResolveError as e:
        first_error = e
        if ctx.stop.is_set():
            raise
    try:                                                      # pages that describe their music
        html = page(url, ctx)
    except ResolveError:
        raise first_error
    col = from_jsonld(html, url)
    if col:
        col.source = text
        return col
    raise ResolveError("No songs found at that link. Try a Spotify, Apple Music, YouTube, Amazon Music or Pandora "
                       "playlist/album link, or type the song’s name.")
