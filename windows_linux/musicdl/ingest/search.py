"""
search.py — songs from plain text, via Deezer's public search (no account, no key).

  'Artist - Song'      one track
  'Abbey Road Beatles' the best-matching album and all its songs
Also the helpers other modules share: turning (artist, title) pairs from an AI into verified tracks, and
building a 'radio' queue (an artist's top songs plus similar artists) for station links.
"""
import logging
import re

from ..core import netio
from ..core.models import Collection, EngineError, Track
from ..core.text import core_title, first_artist, norm, overlap, sim, toks
from .base import ResolveError

log = logging.getLogger("musicdl")
API = "https://api.deezer.com"


def _track(d, album=None, service="search"):
    alb = album or d.get("album") or {}
    return Track(title=d.get("title", ""), artist=(d.get("artist") or {}).get("name", ""), album=alb.get("title", ""),
                 duration=float(d.get("duration") or 0), artwork=alb.get("cover_xl") or alb.get("cover_big") or "",
                 service=service, track_no=int(d.get("track_position") or 0), isrc=d.get("isrc") or "",
                 year=(alb.get("release_date") or "")[:4])


def search_tracks(query, ctx, limit=10):
    try:
        data = netio.get_json(f"{API}/search", params={"q": query, "limit": limit}, limiter=netio.DEEZER_LIMIT,
                              stop=ctx.stop, search=True)
    except EngineError as e:
        raise ResolveError(f"Search is unavailable right now ({e})") from None
    return [d for d in (data or {}).get("data", []) if d.get("title")]


def album_collection(album_id, ctx, service="search"):
    try:
        d = netio.get_json(f"{API}/album/{album_id}", limiter=netio.DEEZER_LIMIT, stop=ctx.stop)
    except EngineError as e:
        raise ResolveError(f"Couldn’t load the album ({e})") from None
    if not d:
        raise ResolveError("That album wasn’t found")
    genre = (((d.get("genres") or {}).get("data") or [{}])[0]).get("name", "")
    tracks = []
    for i, t in enumerate(((d.get("tracks") or {}).get("data") or []), 1):
        tr = Track(title=t.get("title", ""), artist=(t.get("artist") or {}).get("name", "") or (d.get("artist") or {}).get("name", ""),
                   album=d.get("title", ""), year=(d.get("release_date") or "")[:4], genre=genre,
                   duration=float(t.get("duration") or 0), artwork=d.get("cover_xl") or "", service=service, track_no=i)
        tracks.append(tr)
    return Collection(title=d.get("title", "Album"), tracks=tracks, service=service, kind="album",
                      subtitle=(d.get("artist") or {}).get("name", ""), artwork=d.get("cover_xl") or "")


def resolve_text(text, ctx):
    """Free text -> a single track ('Artist - Song') or the best-matching album."""
    q = " ".join(text.split())
    if not q:
        raise ResolveError("Type a song, an album, or paste a link")
    ctx.status("Searching…")
    if " - " in q:
        artist, title = (s.strip() for s in q.split(" - ", 1))
        hits = search_tracks(f'artist:"{artist}" track:"{title}"', ctx) or search_tracks(q, ctx)
        if hits:
            t = _track(hits[0])
            return Collection(title=t.title, subtitle=t.artist, tracks=[t], service="search", kind="track",
                              artwork=t.artwork, source=text)
    try:
        data = netio.get_json(f"{API}/search/album", params={"q": q, "limit": 5}, limiter=netio.DEEZER_LIMIT,
                              stop=ctx.stop, search=True)
    except EngineError as e:
        raise ResolveError(f"Search is unavailable right now ({e})") from None
    albums = (data or {}).get("data") or []
    if albums:
        best = max(albums, key=lambda a: sim(q, f"{a.get('artist', {}).get('name', '')} {a.get('title', '')}")
                   + 0.05 * overlap(toks(q), toks(a.get("title", "")) | toks(a.get("artist", {}).get("name", ""))))
        col = album_collection(best["id"], ctx)
        col.source = text
        if col.tracks:
            return col
    hits = search_tracks(q, ctx)
    if hits:
        t = _track(hits[0])
        return Collection(title=t.title, subtitle=t.artist, tracks=[t], service="search", kind="track",
                          artwork=t.artwork, source=text)
    raise ResolveError(f"Nothing found for “{q}”")


def line_to_track(line, ctx):
    """One line of a pasted song list: 'Artist - Song' is taken as written, anything else is looked up."""
    line = " ".join(re.sub(r"^\s*(?:\d+[.)]\s*|[-•*]\s*)", "", line).split())
    if " - " in line:
        artist, title = (s.strip() for s in line.split(" - ", 1))
        if artist and title:
            return Track(title=title, artist=artist, service="search")
    m = re.match(r"^(.+?)\s+by\s+(.+)$", line, re.I)
    if m:
        return Track(title=m.group(1).strip(), artist=m.group(2).strip(), service="search")
    hits = search_tracks(line, ctx, limit=1) if line else []
    return _track(hits[0]) if hits else None


def verify_pairs(pairs, ctx, service="ai"):
    """[(artist, title)] -> Tracks confirmed to exist (with length, album and artwork); unknown songs are dropped."""
    out, seen = [], set()
    for artist, title in pairs:
        if ctx.stop.is_set():
            break
        key = (norm(artist), norm(core_title(title)))
        if key in seen or not key[1]:
            continue
        seen.add(key)
        try:
            hits = search_tracks(f'artist:"{first_artist(artist)}" track:"{core_title(title)}"', ctx, limit=5)
        except ResolveError:
            hits = []
        need_t, need_a = toks(core_title(title)), toks(first_artist(artist))
        for d in hits:
            if (sim(core_title(title), core_title(d.get("title", ""))) >= 0.8
                    and overlap(need_a, toks((d.get("artist") or {}).get("name", ""))) >= 0.5):
                t = _track(d, service=service)
                out.append(t)
                break
        else:
            log.info("dropped unverified suggestion: %s - %s", artist, title)
    return out


def artist_radio(name, ctx, count=40, service="pandora"):
    """A radio-style queue: the artist's top songs, then top songs of similar artists, interleaved."""
    try:
        found = netio.get_json(f"{API}/search/artist", params={"q": name, "limit": 3}, limiter=netio.DEEZER_LIMIT,
                               stop=ctx.stop, search=True)
        artists = [a for a in (found or {}).get("data", []) if sim(a.get("name", ""), name) >= 0.7]
        if not artists:
            return []
        seed = artists[0]
        top = (netio.get_json(f"{API}/artist/{seed['id']}/top", params={"limit": max(10, count // 2)},
                              limiter=netio.DEEZER_LIMIT, stop=ctx.stop) or {}).get("data", [])
        related = (netio.get_json(f"{API}/artist/{seed['id']}/related", params={"limit": 8},
                                  limiter=netio.DEEZER_LIMIT, stop=ctx.stop) or {}).get("data", [])
        lists = [[_track(d, service=service) for d in top]]
        for a in related[:6]:
            tops = (netio.get_json(f"{API}/artist/{a['id']}/top", params={"limit": 5}, limiter=netio.DEEZER_LIMIT,
                                   stop=ctx.stop) or {}).get("data", [])
            lists.append([_track(d, service=service) for d in tops])
    except EngineError as e:
        raise ResolveError(f"Couldn’t build the station ({e})") from None
    out, seen = [], set()
    # seed artist first, then take turns so the queue has variety
    for rank in range(max(len(x) for x in lists)):
        for lst in lists:
            if rank < len(lst):
                t = lst[rank]
                k = (norm(t.artist), norm(core_title(t.title)))
                if k not in seen:
                    seen.add(k)
                    out.append(t)
    return out[:count]
