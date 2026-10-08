"""
apple.py — Apple Music albums, playlists, songs and artists.

The public web page embeds its data as JSON ('serialized-server-data'), so no account or key is needed.
Song links (…?i=123) pick that song out of its album page, with the iTunes lookup as backup.
"""
import re
from urllib.parse import parse_qs, urlsplit

from ..core import netio
from ..core.models import Collection, EngineError, Track
from .base import ResolveError, dig, follow, page, script_json, squash

LINK = re.compile(r"(?:music|classical|embed\.music)\.apple\.com/([a-z]{2})/(album|playlist|song|artist|station)/", re.I)
SHORT = re.compile(r"(?:apple\.co|itunes\.apple\.com)/", re.I)


def match(text):
    return bool(LINK.search(text) or SHORT.search(text))


def art_url(template, size=1200):
    """Apple artwork URLs are templates: '…/{w}x{h}bb.{f}'."""
    if not template:
        return ""
    return template.replace("{w}", str(size)).replace("{h}", str(size)).replace("{f}", "jpg")


def _sections(html):
    data = script_json(html, "serialized-server-data")
    secs = dig(data, "data", 0, "data", "sections")
    if not isinstance(secs, list):
        raise ResolveError("Apple Music didn’t share this page. It may be private, a station, or unavailable in your region.")
    return secs


def _artists(item):
    name = squash(item.get("artistName"))
    if name:
        return name
    return ", ".join(squash(l.get("title")) for l in (item.get("subtitleLinks") or []) if l.get("title"))


def _song_id(item):
    return str(dig(item, "contentDescriptor", "identifiers", "storeAdamID", default=""))


def parse(html, only_song=None):
    """Page HTML -> Collection. With only_song (an adam id) the result is just that song."""
    secs = _sections(html)
    head = next((s["items"][0] for s in secs if "detail-header" in str(s.get("id")) and s.get("items")), {})
    lst = next((s for s in secs if str(s.get("id", "")).startswith("track-list")), None)
    if not lst or not lst.get("items"):
        raise ResolveError("No songs found on that Apple Music page")

    title = squash(head.get("title"))
    owner = squash(dig(head, "subtitleLinks", 0, "title"))
    art = art_url(dig(head, "artwork", "dictionary", "url", default=""))
    meta = squash(head.get("quaternaryTitle"))               # "Rock · 1969"
    ym = re.search(r"\b(19|20)\d\d\b", meta)
    year = ym.group(0) if ym else ""
    genre = next((p.strip() for p in meta.split("·") if p.strip() and not re.fullmatch(r"\s*\d{4}\s*", p)), "")
    is_album = str(head.get("id", "")).startswith("album-")

    tracks = []
    for it in lst["items"]:
        if not it.get("title"):
            continue
        if only_song and _song_id(it) != only_song:
            continue
        own_art = art_url(dig(it, "artwork", "dictionary", "url", default=""))
        alb = squash(dig(it, "tertiaryLinks", 0, "title")) if not is_album else title
        tracks.append(Track(
            title=squash(it["title"]), artist=_artists(it) or owner, album=alb, year=year if is_album else "",
            genre=genre if is_album else "", artwork=own_art or (art if is_album else ""),
            duration=(it.get("duration") or 0) / 1000, service="apple", track_no=int(it.get("trackNumber") or 0),
            disc_no=int(it.get("discNumber") or 0)))
    if not tracks:
        raise ResolveError("That song wasn’t on the page")
    kind = "track" if only_song else ("album" if is_album else "playlist")
    return Collection(title=tracks[0].title if only_song else title, tracks=tracks, service="apple", kind=kind,
                      subtitle=tracks[0].artist if only_song else owner, artwork=art)


def _itunes(ids, country, songs_of=False):
    """iTunes lookup: the record for an id, or (songs_of=True) that artist's songs."""
    params = {"id": ids, "country": country}
    if songs_of:
        params.update(entity="song", limit=25)
    try:
        data = netio.get_json("https://itunes.apple.com/lookup", params=params, limiter=netio.ITUNES_LIMIT)
    except EngineError as e:
        raise ResolveError(f"Couldn’t reach Apple ({e})") from None
    return (data or {}).get("results", [])


def _from_itunes(rec):
    art = (rec.get("artworkUrl100") or "").replace("100x100bb", "1200x1200bb")
    return Track(title=rec.get("trackName", ""), artist=rec.get("artistName", ""), album=rec.get("collectionName", ""),
                 year=str(rec.get("releaseDate", ""))[:4], genre=rec.get("primaryGenreName", ""),
                 duration=(rec.get("trackTimeMillis") or 0) / 1000, artwork=art, service="apple",
                 track_no=int(rec.get("trackNumber") or 0), disc_no=int(rec.get("discNumber") or 0))


def resolve(text, ctx):
    url = next((w for w in text.split() if "apple" in w or "itunes" in w), text).strip()
    if not url.startswith("http"):
        url = "https://" + url
    if SHORT.search(url) and not LINK.search(url):
        url = follow(url, ctx)
    m = LINK.search(url)
    if not m:
        raise ResolveError("That doesn’t look like an Apple Music link")
    country, kind = m.group(1).lower(), m.group(2).lower()
    if kind == "station":
        raise ResolveError("Apple Music stations play from your account and can’t be read from a link. "
                           "Paste an album or playlist link instead.")
    song = (parse_qs(urlsplit(url).query).get("i") or [""])[0]
    ctx.status("Reading Apple Music…")

    if kind == "song":
        sid = re.search(r"/(\d+)(?:\?|$)", url.split("#")[0])
        recs = [r for r in _itunes(sid.group(1), country) if r.get("wrapperType") == "track"] if sid else []
        if not recs:
            raise ResolveError("Couldn’t find that song on Apple Music")
        t = _from_itunes(recs[0])
        return Collection(title=t.title, subtitle=t.artist, tracks=[t], service="apple", kind="track",
                          artwork=t.artwork, source=text)

    if kind == "artist":
        aid = re.search(r"/(\d+)(?:\?|$)", url.split("#")[0])
        recs = [r for r in _itunes(aid.group(1), country, songs_of=True) if r.get("wrapperType") == "track"] if aid else []
        if not recs:
            raise ResolveError("Couldn’t find that artist’s songs on Apple Music")
        tracks = [_from_itunes(r) for r in recs]
        return Collection(title=tracks[0].artist, subtitle="Top songs", tracks=tracks, service="apple", kind="artist",
                          artwork=tracks[0].artwork, source=text)

    base = url.split("?")[0]
    col = None
    try:
        col = parse(page(base, ctx), only_song=song or None)
    except ResolveError:
        if not song:
            raise
    if col is None:                                          # song not in the page: ask iTunes directly
        recs = [r for r in _itunes(song, country) if r.get("wrapperType") == "track"]
        if not recs:
            raise ResolveError("Couldn’t find that song on Apple Music")
        t = _from_itunes(recs[0])
        col = Collection(title=t.title, subtitle=t.artist, tracks=[t], service="apple", kind="track", artwork=t.artwork)
    col.source = text
    return col
