"""
ingest — turn whatever the user pasted into a Collection of Tracks.

    classify(text)         -> 'spotify' | 'apple' | 'ytmusic' | 'youtube' | 'amazon' | 'pandora' | 'web' |
                              'sheet' | 'search' | None      (cheap; used to light up the service chip as you type)
    resolve(text, ctx=None) -> Collection                    (does the network work; raises ResolveError)
"""
import os

from ..core.models import Collection
from . import amazon, apple, generic, pandora, search, sheet, spotify, youtube
from .base import SERVICES, Ctx, ResolveError, first_url, host_of

__all__ = ["classify", "resolve", "label", "Ctx", "ResolveError", "SERVICES"]

MAX_LINES = 400


def label(key):
    return SERVICES.get(key, key)


def _path(text):
    s = text.strip().strip('"')
    return s if s.lower().endswith((".csv", ".xlsx")) and os.path.isfile(s) else ""


def _lines(text):
    return [l.strip() for l in text.replace("\r", "").split("\n") if l.strip()]


def _is_link(line):
    return line.lower().startswith(("http://", "https://", "spotify:"))


def classify(text):
    s = (text or "").strip()
    if not s:
        return None
    if _path(s):
        return "sheet"
    lines = _lines(s)
    if len(lines) > 1 and not _is_link(lines[0]):
        return "search"
    s = lines[0]
    if spotify.match(s):
        return "spotify"
    if apple.match(s):
        return "apple"
    if pandora.match(s):
        return "pandora"
    if amazon.match(s):
        return "amazon"
    if youtube.match(s):
        return "ytmusic" if host_of(first_url(s) or "https://" + s) == "music.youtube.com" else "youtube"
    if _is_link(s) or first_url(s):
        return "web"
    return "search"


def _one(text, ctx):
    key = classify(text)
    if key == "sheet":
        return sheet.load(_path(text))
    if key == "spotify":
        return spotify.resolve(text, ctx)
    if key == "apple":
        return apple.resolve(text, ctx)
    if key == "pandora":
        return pandora.resolve(text, ctx)
    if key == "amazon":
        return amazon.resolve(text, ctx)
    if key in ("youtube", "ytmusic"):
        return youtube.resolve(text, ctx)
    if key == "web":
        return generic.resolve(text, ctx)
    return search.resolve_text(text, ctx)


def _merge(parts, title):
    seen, tracks, notes = set(), [], []
    for p in parts:
        notes += p.notes
        for t in p.tracks:
            k = (t.url or "", t.title.lower(), t.artist.lower())
            if k not in seen:
                seen.add(k)
                tracks.append(t)
    return Collection(title=title, tracks=tracks, service=parts[0].service if len({p.service for p in parts}) == 1
                      else "web", kind="playlist", subtitle=f"{len(parts)} sources", artwork=parts[0].artwork,
                      notes=notes)


def resolve(text, ctx=None):
    """Songs for whatever was pasted: one link, several links, a list of 'Artist - Song' lines, a name, or a file."""
    ctx = ctx or Ctx()
    text = (text or "").strip()
    if not text:
        raise ResolveError("Paste a playlist link, or type a song or album name")
    lines = _lines(text)[:MAX_LINES]
    if len(lines) > 1 and not _path(text):
        if all(_is_link(l) for l in lines):                   # several links at once
            parts, notes = [], []
            for i, line in enumerate(lines, 1):
                ctx.status(f"Reading link {i} of {len(lines)}…")
                try:
                    parts.append(_one(line, ctx))
                except ResolveError as e:
                    notes.append(f"Skipped {host_of(line) or line[:30]}: {e}")
                if ctx.stop.is_set():
                    raise ResolveError("Cancelled")
            if not parts:
                raise ResolveError(notes[0] if notes else "None of those links worked")
            col = _merge(parts, f"{len(parts)} links")
            col.notes += notes
            col.source = text
            return col
        tracks = []                                           # a pasted song list
        for i, line in enumerate(lines, 1):
            if ctx.stop.is_set():
                raise ResolveError("Cancelled")
            ctx.status(f"Looking up song {i} of {len(lines)}…")
            t = search.line_to_track(line, ctx)
            if t:
                tracks.append(t)
        if not tracks:
            raise ResolveError("Couldn’t make out any songs in that list")
        return Collection(title=f"{len(tracks)} songs", tracks=tracks, service="search", kind="playlist", source=text)
    col = _one(text, ctx)
    col.source = text
    return col
