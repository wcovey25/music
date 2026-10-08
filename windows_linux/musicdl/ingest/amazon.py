"""
amazon.py — Amazon Music albums and playlists.

Amazon's pages are empty until their scripts run, so the page is rendered by an installed browser
(see browser.py) and the finished DOM is read. The rendered page lists titles and artists but not song lengths;
those are filled in from public catalogues later. Needs Microsoft Edge, Google Chrome or Chromium.
"""
import re

from ..core.models import Collection, Track
from . import browser
from .base import ResolveError, follow, host_of, squash
from .webmeta import attrs

LINK = re.compile(r"(?:music\.amazon\.[a-z.]+|amazon\.[a-z.]+/music|amzn\.to|a\.co)/", re.I)
_HEADER = re.compile(r"<music-detail-header\b([^>]*)>", re.I)
_ROW = re.compile(r"<music-text-row\b([^>]*)>", re.I)


def match(text):
    return bool(LINK.search(text))


def parse(dom):
    """Rendered page HTML -> Collection (None when it holds no song rows)."""
    heads = [attrs(m.group(1)) for m in _HEADER.finditer(dom)]      # the page holds an empty placeholder header too
    head = next((h for h in heads if h.get("headline") or h.get("primary-text")), {})
    kind_word = (head.get("label") or "").lower()
    title = squash(head.get("headline") or head.get("primary-text"))
    owner = squash(head.get("primary-text")) if head.get("headline") else ""
    facts = head.get("tertiary-text", "")
    ym = re.search(r"\b((?:19|20)\d\d)\b", facts)
    declared = re.search(r"(\d+)\s+SONGS?", facts, re.I)
    is_album = "album" in kind_word
    year = ym.group(1) if (ym and is_album) else ""

    tracks, seen = [], set()
    for rm in _ROW.finditer(dom):
        a = attrs(rm.group(1))
        href = a.get("primary-href", "")
        name = squash(a.get("primary-text"))
        if not name or not re.match(r"^/(?:tracks|albums)/", href) or href in seen:
            continue
        seen.add(href)
        artist = squash(a.get("secondary-text-2") or a.get("secondary-text-1") or a.get("secondary-text")) or owner
        if re.fullmatch(r"[\d:]+", artist):                  # a duration, not an artist
            artist = owner
        tracks.append(Track(title=name, artist=artist, album=title if is_album else "", year=year,
                            artwork=head.get("image-src", "") if is_album else "", service="amazon",
                            track_no=len(tracks) + 1 if is_album else 0,
                            extra={"asin": href.rsplit("/", 1)[-1]}))
    if not tracks:
        return None
    notes = []
    if declared and len(tracks) < int(declared.group(1)):
        notes.append(f"Amazon showed {len(tracks)} of {declared.group(1)} songs on that page.")
    return Collection(title=title or "Amazon Music", subtitle=owner, tracks=tracks, service="amazon",
                      kind="album" if is_album else ("artist" if "artist" in kind_word else "playlist"),
                      artwork=head.get("image-src", ""), notes=notes)


def resolve(text, ctx):
    url = next((w for w in text.split() if re.search(r"amazon|amzn|a\.co", w)), text).strip()
    if not url.startswith("http"):
        url = "https://" + url
    if re.search(r"amzn\.to|a\.co/", url):
        url = follow(url, ctx)
    if "music" not in host_of(url) and "/music" not in url:
        raise ResolveError("That’s an Amazon shopping link, not an Amazon Music one.")
    dom = browser.render(url, ctx)
    col = parse(dom)
    if col is None:
        raise ResolveError("Couldn’t find songs on that Amazon Music page. It may need a sign-in, or may be a "
                           "podcast or station. Try the album or playlist link.")
    col.source = text
    return col
