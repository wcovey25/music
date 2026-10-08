"""
spotify.py — Spotify playlists, albums, tracks and artists, read from the public embed player page.

No account or API key: open.spotify.com/embed/<kind>/<id> contains the list as JSON. Spotify only puts the
first 100 songs of a playlist in that page, so longer playlists are trimmed and the user is told.
"""
import re

from ..core.models import Collection, Track
from .base import ResolveError, dig, page, script_json, squash

LINK = re.compile(r"(?:open\.spotify\.com/(?:intl-[\w-]+/)?(?:embed/)?(?:user/[^/]+/)?|spotify:)"
                  r"(playlist|album|track|artist)[/:]([A-Za-z0-9]{10,})")
PLAYLIST_CAP = 100


def match(text):
    m = LINK.search(text)
    return (m.group(1), m.group(2)) if m else None


def _best_image(images):
    imgs = [i for i in (images or []) if i.get("url")]
    return max(imgs, key=lambda i: i.get("maxWidth") or i.get("width") or 0)["url"] if imgs else ""


def _year(release):
    iso = release.get("isoString") if isinstance(release, dict) else release
    return str(iso or "")[:4] if str(iso or "")[:4].isdigit() else ""


def _entity(html):
    data = script_json(html, "__NEXT_DATA__")
    ent = dig(data, "props", "pageProps", "state", "data", "entity")
    if not isinstance(ent, dict):
        raise ResolveError("Spotify didn’t share this page. It may be private or removed.")
    return ent


def parse(html, kind):
    """Embed page HTML -> Collection."""
    ent = _entity(html)
    name = squash(ent.get("title") or ent.get("name"))
    subtitle = squash(ent.get("subtitle"))
    art = _best_image(dig(ent, "visualIdentity", "image", default=[])) \
        or dig(ent, "coverArt", "sources", 0, "url", default="")
    year = _year(ent.get("releaseDate"))
    items = [t for t in (ent.get("trackList") or []) if str(t.get("uri", "")).startswith("spotify:track:")]

    if ent.get("type") == "track" or (kind == "track" and not items):
        artists = ", ".join(a.get("name", "") for a in (ent.get("artists") or []) if a.get("name")) or subtitle
        t = Track(title=name, artist=artists, year=year, artwork=art, service="spotify",
                  duration=(ent.get("duration") or 0) / 1000)
        return Collection(title=name, subtitle=artists, tracks=[t], service="spotify", kind="track", artwork=art)

    is_album = ent.get("type") == "album"
    tracks = []
    for i, it in enumerate(items, 1):
        title = squash(it.get("title"))
        if not title:
            continue
        tracks.append(Track(
            title=title, artist=squash(it.get("subtitle")) or (subtitle if is_album else ""),
            album=name if is_album else "", year=year if is_album else "", artwork=art if is_album else "",
            duration=(it.get("duration") or 0) / 1000, service="spotify", track_no=i if is_album else 0))
    if not tracks:
        raise ResolveError("No songs found on that Spotify page")
    notes = []
    if len(tracks) >= PLAYLIST_CAP and ent.get("type") == "playlist":
        notes.append(f"Spotify only shares the first {PLAYLIST_CAP} songs of a playlist, so that’s what was loaded. "
                     "For the rest, copy the later songs into a second playlist and paste that too.")
    return Collection(title=name, tracks=tracks, service="spotify", kind=ent.get("type") or kind, subtitle=subtitle,
                      artwork=art, notes=notes)


def resolve(text, ctx):
    found = match(text)
    if not found:
        raise ResolveError("That doesn’t look like a Spotify playlist, album or song link")
    kind, sid = found
    ctx.status("Reading Spotify…")
    html = page(f"https://open.spotify.com/embed/{kind}/{sid}", ctx)
    col = parse(html, kind)
    col.source = text
    return col
