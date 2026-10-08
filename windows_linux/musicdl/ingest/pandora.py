"""
pandora.py — Pandora albums, artists, songs and playlists from the public web page.

Pandora's pages embed their catalogue as JSON (`window._store`), so album/song/artist/playlist links work without an
account. Personal stations need a login and cannot be read; a station link is turned into a similar-artists queue
when the page names its seed. Pandora only serves the United States: from elsewhere it returns an
"unavailable in this country" page, which is reported plainly.
"""
import json
import logging
import re
from collections import Counter

from ..core.models import Collection, Track
from . import browser, search
from .base import ResolveError, dig, follow, page, squash
from .webmeta import jsonld, og

log = logging.getLogger("musicdl")

LINK = re.compile(r"pandora\.com/|pandora\.app\.link/", re.I)
CDN = "https://content-images.p-cdn.com/"
GEO_MARKERS = ("unavailable in this country", "not available in your country", "restricted.vm")


def match(text):
    return bool(LINK.search(text))


def art_url(rel, size=1080):
    if not rel:
        return ""
    url = rel if rel.startswith("http") else CDN + rel.lstrip("/")
    return re.sub(r"_\d+W_\d+H", f"_{size}W_{size}H", url)


def _geo_blocked(html):
    low = html.lower()
    return any(m in low for m in GEO_MARKERS) and "window._store" not in html


def store_objects(html):
    """All catalogue objects on the page, keyed by Pandora id ('TR:123', 'AL:45', 'AR:6', 'PL:…')."""
    i = html.find("window._store")
    start = html.find("{", i) if i >= 0 else -1
    if start < 0:
        return {}
    try:
        store, _ = json.JSONDecoder().raw_decode(html, start)
    except ValueError:
        return {}
    objs = {}
    groups = list(store.get("v4/catalog/annotateObjects") or []) + \
        [g.get("annotations") for g in (store.get("v4/catalog/getDetails") or []) if isinstance(g, dict)]
    for g in groups:
        if isinstance(g, dict):
            for key, val in g.items():
                if isinstance(val, dict) and re.match(r"^[A-Z]{2}:", key):
                    objs.setdefault(key, val)
    return objs


def _track(o, album_override=None, year="", art=""):
    return Track(title=squash(o.get("name")), artist=squash(o.get("artistName")),
                 album=squash(album_override or o.get("albumName")), year=year, isrc=o.get("isrc") or "",
                 duration=(o.get("durationMillis") or 0) / 1000, track_no=int(o.get("trackNumber") or 0),
                 artwork=art_url(dig(o, "icon", "artUrl", default="")) or art, service="pandora")


def _primary(objs, html, url):
    """The album / playlist / song / artist the page is about."""
    for d in jsonld(html):
        pid = str(d.get("@id", ""))
        if pid in objs:
            return pid
    m = re.search(r"/((?:AL|AR|TR|PL)[:\w]*?)(?:\?|$|#)", url)
    wanted = {"AL": "AL", "AR": "AR", "TR": "TR", "PL": "PL"}
    for key in objs:                                         # the album/playlist outranks its tracks
        if key[:2] in ("PL", "AL"):
            return key
    if m and m.group(1)[:2] in wanted:
        for key in objs:
            if key[:2] == m.group(1)[:2]:
                return key
    for kind in ("TR", "AR"):
        for key in objs:
            if key.startswith(kind):
                return key
    return None


def parse(html, url=""):
    """Page HTML -> Collection (album, playlist, artist top songs, or one song)."""
    objs = store_objects(html)
    pid = _primary(objs, html, url)
    if not pid:
        return None
    main = objs[pid]
    kind = pid[:2]
    tracks_all = {k: v for k, v in objs.items() if k.startswith("TR:")}

    if kind == "TR":
        o = tracks_all.get(pid) or main
        t = _track(o)
        return Collection(title=t.title, subtitle=t.artist, tracks=[t], service="pandora", kind="track",
                          artwork=t.artwork)

    if kind == "AL":
        art = art_url(dig(main, "icon", "artUrl", default=""))
        year = str(main.get("originalReleaseDate") or main.get("releaseDate") or "")[:4]
        ids = [i if isinstance(i, str) else dig(i, "pandoraId", default="") for i in (main.get("tracks") or [])]
        rows = [tracks_all[i] for i in ids if i in tracks_all] or list(tracks_all.values())
        rows.sort(key=lambda o: int(o.get("trackNumber") or 999))
        tracks = [_track(o, album_override=main.get("name"), year=year, art=art) for o in rows if o.get("name")]
        if not tracks:
            return None
        return Collection(title=squash(main.get("name")), subtitle=squash(main.get("artistName")), tracks=tracks,
                          service="pandora", kind="album", artwork=art)

    if kind == "PL":
        ids = [i if isinstance(i, str) else dig(i, "pandoraId", default="") for i in (main.get("tracks") or [])]
        rows = [tracks_all[i] for i in ids if i in tracks_all] or list(tracks_all.values())
        tracks = [_track(o) for o in rows if o.get("name")]
        for t in tracks:
            t.track_no = 0
        if not tracks:
            return None
        return Collection(title=squash(main.get("name")), subtitle=squash(main.get("ownerName") or "Pandora"),
                          tracks=tracks, service="pandora", kind="playlist",
                          artwork=art_url(dig(main, "icon", "artUrl", default="")))

    if kind == "AR":                                         # the artist page lists top songs
        rows = list(tracks_all.values())
        tracks = [_track(o) for o in rows if o.get("name")]
        if not tracks:
            return None
        for t in tracks:
            t.track_no = 0
        return Collection(title=squash(main.get("name")), subtitle="Top songs", tracks=tracks, service="pandora",
                          kind="artist", artwork=art_url(dig(main, "icon", "artUrl", default="")))
    return None


def _station_seed(objs):
    names = Counter(squash(o.get("artistName") or o.get("name")) for k, o in objs.items()
                    if k.startswith(("TR:", "AR:")) and (o.get("artistName") or o.get("name")))
    return names.most_common(1)[0][0] if names else ""


def resolve(text, ctx):
    url = next((w for w in text.split() if "pandora" in w), text).strip()
    if not url.startswith("http"):
        url = "https://" + url
    if "pandora.app.link" in url:
        url = follow(url, ctx)
    ctx.status("Reading Pandora…")
    html = page(url, ctx)
    if _geo_blocked(html):
        raise ResolveError("Pandora only works inside the United States, and it’s refusing this connection. "
                           "Use a US connection, or paste a Spotify, Apple Music or YouTube link for the same music.")
    col = parse(html, url)
    if col is None and "window._store" not in html:          # a page that builds itself with scripts
        try:
            col = parse(browser.render(url, ctx), url)
        except ResolveError as e:
            log.info("pandora browser fallback: %s", e)
    is_station = "/station/" in url
    if is_station:
        seed = _station_seed(store_objects(html)) or (col.subtitle if col and col.kind in ("album", "track") else "")
        if seed:
            tracks = search.artist_radio(seed, ctx, service="pandora")
            if tracks:
                return Collection(title=f"{seed} Radio", subtitle="Similar artists", tracks=tracks, service="pandora",
                                  kind="station", source=text,
                                  notes=["Pandora stations are personal, so this queue is built from the station’s "
                                         f"starting artist ({seed}) and similar artists."])
        raise ResolveError("Pandora stations belong to a listener’s account and can’t be read from a link. "
                           "Paste an artist, album, song or playlist link instead.")
    if col is None:
        if "/podcast" in url:
            raise ResolveError("Pandora podcasts aren’t music, so there’s nothing to download there.")
        og_meta = og(html)
        raise ResolveError("Couldn’t find any songs on that Pandora page" +
                           (f" (“{og_meta['title']}”)." if og_meta.get("title") else "."))
    col.source = text
    return col
