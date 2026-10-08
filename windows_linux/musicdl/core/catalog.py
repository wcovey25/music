"""
catalog.py — second opinions about a song from public catalogues.

MusicBrainz (batched by recording id) gives the expected length and release ids for spreadsheet rows.
iTunes (Apple's own catalogue, so album names, genres, explicit flags and artwork agree with Apple Music) and, when it has
nothing, Deezer give the album, album artist, date, track and disc numbers, genre, length and artwork for anything, keyed
by artist + title. Which of a song's many releases is used is decided in release.py. Everything is cached in the Library so
a re-run costs no requests.

A lookup answer:
  durations, covers            lengths and picture URLs of the recording (best first; covers are sized by the caller)
  releases                     up to KEEP candidate releases, best first, each: album, album_artist, date, year, track_no,
                               track_total, disc_no, disc_total, genre, explicit, compilation, label, cover
  album, year, … (top level)   the best release's fields, for callers that want one answer
  genre, isrc, explicit        facts about the song rather than one release
  v, t, miss, failed           bookkeeping: version (older cached answers are looked up again), time, "nothing found",
                               "could not ask" (never cached: asked again next time)
"""
import logging
import time

from . import release
from .models import EngineError
from .netio import CAA_LIMIT, DEEZER_LIMIT, ITUNES_LIMIT, MB_LIMIT, get_json
from .text import core_title, first_artist, norm, overlap, sim, toks

log = logging.getLogger("musicdl")
META_TTL = 30 * 86400
MISS_TTL = 7 * 86400
RELEASE_V = 2                      # bump when what an answer holds (or means) changes: older cached answers are asked again
KEEP = 4                           # releases kept per song
ITUNES_URL = "https://itunes.apple.com/search"
DEEZER_URL = "https://api.deezer.com"
_UNKNOWN = {"", "unknown", "unknown artist", "various artists", "various", "n/a"}


def mb_prefetch(lib, mbids, stop, say=None):
    """Fill lib 'meta' for MusicBrainz recording ids: expected length + the releases it appears on."""
    need, seen, now = [], set(), time.time()
    for rid in mbids:
        rid = (rid or "").strip()
        if not rid or rid in seen:
            continue
        seen.add(rid)
        m = lib.get("meta", rid)
        if not m or now - m.get("t", 0) > (META_TTL if not m.get("miss") else MISS_TTL):
            need.append(rid)
    for i in range(0, len(need), 40):
        if stop.is_set():
            return
        chunk = need[i:i + 40]
        if say:
            say(f"Looking up song details… {min(i + 40, len(need))}/{len(need)}")
        try:
            data = get_json("https://musicbrainz.org/ws/2/recording",
                            params={"query": "rid:(" + " OR ".join(chunk) + ")", "fmt": "json", "limit": 100},
                            limiter=MB_LIMIT, retries=4, stop=stop, search=True)
        except EngineError as e:
            log.warning("MusicBrainz batch failed: %s", e)
            continue
        found = {r["id"]: r for r in (data or {}).get("recordings", [])}
        for rid in chunk:
            r = found.get(rid)
            if not r:
                lib.put("meta", rid, {"len": None, "rel": [], "t": time.time(), "miss": True})
                continue
            rels = sorted(r.get("releases") or [], key=lambda x: (x.get("status") != "Official", x.get("date") or "9999"))
            lib.put("meta", rid, {
                "len": r.get("length"), "t": time.time(),
                "rel": [{"id": x["id"], "rg": (x.get("release-group") or {}).get("id", ""),
                         "d": x.get("date", "")} for x in rels[:10]]})


def cover_art_archive_urls(lib, mbid):
    """Cover Art Archive URLs for a MusicBrainz recording (release group first, then releases)."""
    rels = ((lib.get("meta", mbid) or {}).get("rel", [])) if mbid else []
    for rg in list(dict.fromkeys(r["rg"] for r in rels if r.get("rg")))[:3]:
        yield f"https://coverartarchive.org/release-group/{rg}/front-1200"
        yield f"https://coverartarchive.org/release-group/{rg}/front-500"
    for r in rels[:4]:
        yield f"https://coverartarchive.org/release/{r['id']}/front-500"


# ---------------------------------------------------------------- one answer from the cache or the catalogues

def fresh(entry):
    """Is this cached answer in the current layout? (Older ones may name the wrong release: they are asked again.)"""
    return bool(entry) and entry.get("v") == RELEASE_V


def _blank(src="", miss=False, failed=False):
    return {"v": RELEASE_V, "t": 0 if failed else time.time(), "src": src, "miss": miss, "failed": failed,
            "durations": [], "covers": [], "releases": [], "album": "", "album_artist": "", "year": "", "date": "",
            "track_no": 0, "track_total": 0, "disc_no": 0, "disc_total": 0, "isrc": "", "genre": "", "explicit": 0,
            "compilation": False, "label": ""}


def lookup(lib, track, stop, want_genre=False):
    """The catalogue's answer for a track (see the module doc). Cached; never raises; a lookup that could not be made
    comes back with failed=True and is not cached."""
    artist, title = first_artist(track.artist), core_title(track.title)
    key = norm(artist) + "|" + norm(title)
    got = lib.get("catalog", key)
    if fresh(got) and time.time() - got.get("t", 0) < (MISS_TTL if got.get("miss") else META_TTL) \
            and (got.get("genre") or not want_genre or got.get("nogenre") or got.get("miss")):
        return got
    if norm(artist) in _UNKNOWN or not norm(title):
        return _blank(miss=True)
    need_t, need_a = toks(title), toks(artist)

    def match(t, a):
        t_ok = sim(title, t) >= 0.85 or (need_t and need_t <= toks(t) and len(toks(t)) <= len(need_t) + 1)
        return t_ok and overlap(need_a, toks(a)) >= 0.5

    want = {"title": track.title, "album": track.album, "seconds": float(track.duration or 0)}
    rec, asked, failed = None, 0, 0
    for name, provider in (("iTunes", _itunes), ("Deezer", _deezer)):
        asked += 1
        try:
            found = provider(artist, title, want, match, stop)
        except EngineError as e:
            failed += 1
            log.info("%s lookup failed for %s: %s", name, track.label(), e)
            continue
        if found and (rec is None or (found["releases"] and not rec["releases"])):
            rec = found
        if rec and rec["releases"]:
            break
    if rec is None or not (rec["releases"] or rec["durations"]):
        if failed == asked:
            return _blank(failed=True)                          # nobody could be asked: say so, and do not remember it
        rec = rec or _blank(miss=True)
    rec["miss"] = not (rec["releases"] or rec["durations"])
    rec["nogenre"] = not rec["genre"]
    lib.put("catalog", key, rec)
    return rec


# ---------------------------------------------------------------- iTunes

def _int(v):
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _itunes_hit(d):
    return {"src": "itunes", "title": d.get("trackName") or "", "artist": d.get("artistName") or "",
            "album": d.get("collectionName") or "", "album_artist": d.get("collectionArtistName") or d.get("artistName") or "",
            "date": str(d.get("releaseDate") or "")[:10], "track_no": _int(d.get("trackNumber")),
            "track_total": _int(d.get("trackCount")), "disc_no": _int(d.get("discNumber")),
            "disc_total": _int(d.get("discCount")), "genre": d.get("primaryGenreName") or "",
            "explicit": {"explicit": 1, "cleaned": 2}.get(str(d.get("trackExplicitness") or ""), 0),
            "seconds": (d.get("trackTimeMillis") or 0) / 1000.0, "cover": d.get("artworkUrl100") or ""}


def _itunes(artist, title, want, match, stop):
    data = get_json(ITUNES_URL, params={"term": f"{artist} {title}", "entity": "song", "limit": 40},
                    limiter=ITUNES_LIMIT, retries=2, stop=stop, search=True, throttle=(403,))
    hits = [h for h in map(_itunes_hit, (data or {}).get("results") or []) if h["title"] and match(h["title"], h["artist"])]
    return _build(hits, want, "itunes")


# ---------------------------------------------------------------- Deezer (when iTunes does not know the song)

def _deezer(artist, title, want, match, stop):
    data = get_json(DEEZER_URL + "/search", params={"q": f"{artist} {title}", "limit": 25},
                    limiter=DEEZER_LIMIT, retries=2, stop=stop, search=True)
    hits = []
    for d in (data or {}).get("data") or []:
        alb = d.get("album") or {}
        name = f"{d.get('title_short') or d.get('title') or ''} {d.get('title_version') or ''}".strip()
        h = {"src": "deezer", "title": name, "artist": (d.get("artist") or {}).get("name", ""), "album": alb.get("title", ""),
             "album_artist": "", "date": "", "track_no": 0, "track_total": 0, "disc_no": 0, "disc_total": 0, "genre": "",
             "explicit": 1 if d.get("explicit_lyrics") else 0, "seconds": float(d.get("duration") or 0),
             "cover": alb.get("cover_xl") or alb.get("cover_big") or "", "track_id": d.get("id"), "album_id": alb.get("id")}
        if name and match(name, h["artist"]):
            hits.append(h)
    rec = _build(hits, want, "deezer")
    if not rec or not rec["releases"]:
        return rec
    best = rec["releases"][0]
    try:                                                          # the details a search result leaves out
        full = get_json(f"{DEEZER_URL}/track/{best['_track_id']}", limiter=DEEZER_LIMIT, retries=2, stop=stop)
        alb = get_json(f"{DEEZER_URL}/album/{best['_album_id']}", limiter=DEEZER_LIMIT, retries=2, stop=stop) \
            if best.get("_album_id") else None
    except EngineError as e:
        log.info("Deezer details failed for %s: %s", title, e)
        full = alb = None
    if full:
        best["track_no"] = _int(full.get("track_position"))
        best["disc_no"] = _int(full.get("disk_number"))
        rec["isrc"] = full.get("isrc") or ""
        best["explicit"] = 1 if full.get("explicit_lyrics") else best["explicit"]
    if alb:
        # the *album's* date is the real one: a track's own 'release_date' is when Deezer re-released it
        best["date"] = release.valid_date(alb.get("release_date")) or best["date"]
        best["year"] = best["date"][:4]
        best["album_artist"] = (alb.get("artist") or {}).get("name", "") or best["album_artist"]
        best["track_total"] = _int(alb.get("nb_tracks"))
        best["label"] = alb.get("label") or ""
        best["compilation"] = best["album_artist"].lower() == "various artists" or alb.get("record_type") == "compile"
        genres = [g.get("name", "") for g in ((alb.get("genres") or {}).get("data") or [])]
        best["genre"] = next((g for g in genres if not release.generic_genre(g)), "")
    rec["album"], rec["album_artist"], rec["date"], rec["year"] = best["album"], best["album_artist"], best["date"], best["year"]
    rec["track_no"], rec["track_total"], rec["disc_no"] = best["track_no"], best["track_total"], best["disc_no"]
    rec["genre"], rec["explicit"], rec["label"], rec["compilation"] = best["genre"], best["explicit"], best["label"], best["compilation"]
    for r in rec["releases"]:
        for k in ("_track_id", "_album_id"):
            r.pop(k, None)
    return rec


# ---------------------------------------------------------------- from matching hits to one answer

def _brief(choice):
    hit = choice.hit
    date = release.original_date(choice)
    out = {"album": hit["album"], "album_artist": hit.get("album_artist") or "", "date": date, "year": date[:4],
           "track_no": hit["track_no"], "track_total": hit["track_total"], "disc_no": hit["disc_no"],
           "disc_total": hit["disc_total"], "genre": "" if release.generic_genre(hit["genre"]) else hit["genre"],
           "explicit": hit["explicit"], "compilation": (hit.get("album_artist") or "").lower() == "various artists",
           "label": "", "cover": hit["cover"]}
    if hit.get("src") == "deezer":
        out["_track_id"], out["_album_id"] = hit.get("track_id"), hit.get("album_id")
    return out


def _build(hits, want, src):
    """One answer from the hits that are this song: the best releases, the lengths, the pictures."""
    if not hits:
        return None
    ranked = release.rank(hits, want)
    rec = _blank(src)
    seen, secs = set(), []
    for h in [m for c in ranked for m in c.members] + hits:      # the right recordings' lengths first, then the others'
        s = round(h["seconds"])
        if s and s not in seen:
            seen.add(s)
            secs.append(float(s))
    rec["durations"] = secs[:6]
    if not ranked:                                               # only live / remixed / other recordings exist
        return rec
    releases = [_brief(c) for c in ranked[:KEEP]]
    rec["releases"] = releases
    best = releases[0]
    for k in ("album", "album_artist", "date", "year", "track_no", "track_total", "disc_no", "disc_total", "explicit",
              "compilation", "label"):
        rec[k] = best[k]
    rec["genre"] = best["genre"] or next((r["genre"] for r in releases if r["genre"]), "")
    rec["covers"] = list(dict.fromkeys(r["cover"] for r in releases if r["cover"]))[:4]
    return rec


# ---------------------------------------------------------------- using an answer

def pick_release(track, cat):
    """The release in an answer that fits what the track already says about itself. A track that names an album is only
    given details of that album (never a track number from another one); None when the answer has no such release."""
    rels = cat.get("releases")
    if not rels:
        return cat if (cat.get("album") or cat.get("year") or cat.get("track_no")) else None   # (a hand-made / legacy answer)
    if track.album:
        fam = release.family(track.album)
        return next((r for r in rels if release.family(r.get("album")) == fam), None)
    return rels[0]


def merge_details(track, cat):
    """Fill what the track does not know from the answer, without ever contradicting what it does know."""
    rel = pick_release(track, cat)
    if rel:
        track.album = track.album or rel.get("album", "")
        track.album_artist = track.album_artist or rel.get("album_artist", "")
        year = str(rel.get("year") or str(rel.get("date") or "")[:4] or "")
        if rel.get("date") and not track.date and (not track.year or track.year == year):
            track.date = rel["date"]
        track.year = track.year or year
        same_track = not track.track_no or track.track_no == _int(rel.get("track_no"))
        track.track_no = track.track_no or _int(rel.get("track_no"))
        track.disc_no = track.disc_no or _int(rel.get("disc_no"))
        if same_track:
            track.track_total = track.track_total or _int(rel.get("track_total"))
            track.disc_total = track.disc_total or _int(rel.get("disc_total"))
        track.compilation = track.compilation or bool(rel.get("compilation"))
        track.record_label = track.record_label or rel.get("label", "")
    track.genre = track.genre or cat.get("genre", "")                  # facts about the song, not about one release
    track.isrc = track.isrc or cat.get("isrc", "")
    track.explicit = track.explicit or _int(cat.get("explicit"))
    if not track.album_artist and track.album:                          # an album the service named: its artist, at least
        track.album_artist = first_artist(track.artist)


def covers_for(track, cat):
    """Picture URLs for a track, the fitting release's first."""
    rels = cat.get("releases")
    if not rels:
        return list(cat.get("covers") or [])
    rel = pick_release(track, cat)
    if track.album and rel is None:
        return []                                                       # the album the service named is not in the answer
    return list(dict.fromkeys(([rel["cover"]] if rel and rel.get("cover") else []) + [r["cover"] for r in rels if r.get("cover")]))


__all__ = ["mb_prefetch", "cover_art_archive_urls", "lookup", "fresh", "merge_details", "covers_for", "pick_release",
           "CAA_LIMIT", "RELEASE_V"]
