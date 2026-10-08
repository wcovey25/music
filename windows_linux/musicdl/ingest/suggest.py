"""
suggest.py — the source bar as a search bar.

While you type a name (a song, an artist, an album, a genre, a mood — anything that is not a link), `search_all` asks
Deezer's public search for what it might be and gives back a short, ranked list of suggestions: songs, albums, artists,
playlists and genres. Picking one turns it into the songs it stands for with `collect`:

    song      that song                          album     the whole album
    artist    the artist's top songs             playlist  the playlist (the first few hundred songs)
    genre     the genre's current chart

Deezer's own order is not trusted: a "Hello (Reggae Cover)" can come first for "adele hello". Each result is scored by
how much of what you typed it explains, whether it is exactly that, how popular it is, and whether it is a cover /
karaoke / remix you did not ask for. No account or key is needed.
"""
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from ..core import netio
from ..core.models import Collection, EngineError, Track
from ..core.text import fuzzy_overlap, norm, norm_keep_parens, toks
from . import search
from .base import ResolveError

log = logging.getLogger("musicdl")
API = search.API

MIN_CHARS = 2
SHOWN = 8                                    # rows in the list
CAPS = {"song": 4, "album": 3, "artist": 2, "playlist": 2, "genre": 1}      # most of each kind
KINDS = {"song": "Song", "album": "Album", "artist": "Artist", "playlist": "Playlist", "genre": "Genre"}
PLAYLIST_MAX = 300                           # songs taken from a playlist
# what a result is when it is somebody else's version of the song, unless you typed the word yourself
_ALTERNATE = {"cover", "karaoke", "tribute", "instrumental", "remix", "live", "lullaby", "nightcore", "8d", "slowed",
              "sped", "reverb", "parody", "medley", "version"}
# used when Deezer's genre list cannot be fetched
_GENRES = ((132, "Pop"), (116, "Rap/Hip Hop"), (152, "Rock"), (113, "Dance"), (165, "R&B"), (85, "Alternative"),
           (106, "Electro"), (466, "Folk"), (144, "Reggae"), (129, "Jazz"), (84, "Country"), (464, "Metal"),
           (98, "Classical"), (169, "Soul & Funk"), (153, "Blues"), (197, "Latin Music"))
_genres, _genres_lock = None, threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="suggest")


@dataclass
class Suggestion:
    kind: str                                # song | album | artist | playlist | genre
    title: str
    subtitle: str = ""                       # the artist, the owner …
    id: str = ""                             # Deezer's id
    art: str = ""
    count: int = 0                           # songs in it, when known
    score: float = 0.0
    data: dict = field(default_factory=dict)  # what collect() needs beyond the id

    @property
    def tag(self):
        return KINDS.get(self.kind, "")

    @property
    def label(self):
        """What goes into the search bar when this is picked."""
        return f"{self.title} — {self.subtitle}" if self.subtitle and self.kind in ("song", "album") else self.title

    @property
    def detail(self):
        bits = [self.subtitle] if self.subtitle else []
        if self.count and self.kind != "song":
            bits.append(f"{self.count:,} song{'' if self.count == 1 else 's'}")
        return " · ".join(bits)


# ---------------------------------------------------------------- asking Deezer

def _get(path, params, stop):
    """JSON from Deezer or None (a failed suggestion is only a missing suggestion)."""
    try:
        return netio.get_json(f"{API}{path}", params=params, limiter=netio.SUGGEST_LIMIT, stop=stop, search=True,
                              retries=1, timeout=(5, 8), hedge=True, ttl=120)
    except EngineError as e:
        log.info("suggestion lookup %s failed: %s", path, e)
        return None


def genres(stop=None):
    """[(id, name)] — Deezer's genre list, fetched once (the built-in list when it cannot be)."""
    global _genres
    with _genres_lock:
        if _genres is None:
            data = _get("/genre", None, stop)
            got = [(int(g["id"]), g["name"]) for g in (data or {}).get("data", []) if g.get("id") and g.get("name")]
            _genres = got or list(_GENRES)
        return _genres


def _popularity(n, low, high):
    """0..1 on a log scale: `low` is the count that is worth nothing, `high` the count that is as popular as it gets."""
    n = max(1, int(n or 0))
    return min(1.0, max(0.0, (math.log10(n) - low) / (high - low)))


def _bare(title):
    """The title without its trailing edition note: 'Kind Of Blue (Legacy Edition)' -> 'Kind Of Blue'."""
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]\s*$", "", title or "").strip() or title or ""


def _typed(q, q_toks):
    """Every word that was typed, including the ones `toks` treats as noise ('karaoke', 'version', 'the')."""
    return set(norm_keep_parens(q).split()) | q_toks


def _fit(typed, q_toks, *texts):
    """How well a result's words are what was typed, 0..1: how much of the text is explained, discounted when the result
    carries many words that were not typed ('Hello I'm Adele' for 'adele hello')."""
    have = set()
    for t in texts:
        bare = _bare(t)
        used = t if (set(norm_keep_parens(t).split()) - set(norm_keep_parens(bare).split())) & typed else bare   # a bracket note counts only if it was typed
        have |= toks(used) | (set(norm_keep_parens(used).split()) & typed)
    if not have:
        return 0.0
    coverage = fuzzy_overlap(q_toks, have)
    precision = len(q_toks & have) / len(have)
    return coverage * (0.55 + 0.45 * precision)


def _alternate_penalty(typed, title):
    return 1.5 if (set(norm_keep_parens(title).split()) & _ALTERNATE) - typed else 0.0


def _slim_album(alb):
    """The few album fields a picked song needs (the rest would only fill the index file)."""
    return {k: alb[k] for k in ("title", "cover_xl", "cover_big", "cover_medium", "release_date") if alb.get(k)}


def _song_rows(data, q, q_toks, boost=0.0):
    out, q_norm, typed = [], norm(q), _typed(q, q_toks)
    for pos, d in enumerate((data or {}).get("data", [])):
        title, artist = d.get("title", ""), (d.get("artist") or {}).get("name", "")
        if not title or d.get("readable") is False:
            continue
        alb = d.get("album") or {}
        fit = _fit(typed, q_toks, title, artist, "" if len(q_toks) > 1 else alb.get("title", ""))
        exact = q_norm in (norm(title), norm(f"{artist} {title}"), norm(f"{title} {artist}"))
        pop = _popularity(d.get("rank"), 4.0, 6.2)
        s = (2.4 * fit * (0.6 + 0.4 * pop) + (0.8 * (0.25 + 0.75 * pop) if exact else 0.0) + 1.8 * pop
             - _alternate_penalty(typed, title) - 0.04 * pos + boost)
        out.append(Suggestion("song", title, artist, str(d.get("id", "")), alb.get("cover_medium") or "", 1, s,
                              {"pop": pop, "track": {"title": title, "artist": artist, "album": _slim_album(alb),
                                                     "duration": d.get("duration"), "id": d.get("id")}}))
    return out


def _songs(q, stop, q_toks):
    query = q
    if " - " in q:
        artist, title = (s.strip() for s in q.split(" - ", 1))
        if artist and title:
            query = f'artist:"{artist}" track:"{title}"'
    data = _get("/search", {"q": query, "limit": 12}, stop)
    if query != q and not (data or {}).get("data"):
        data = _get("/search", {"q": q, "limit": 12}, stop)
    return _song_rows(data, q, q_toks)


def _albums(q, stop, q_toks):
    typed = _typed(q, q_toks)
    data = _get("/search/album", {"q": q, "limit": 8}, stop)
    out = []
    for pos, d in enumerate((data or {}).get("data", [])):
        title, artist = d.get("title", ""), (d.get("artist") or {}).get("name", "")
        if not title or not d.get("id"):
            continue
        songs = int(d.get("nb_tracks") or 0)
        s = (2.2 * _fit(typed, q_toks, title, artist) + (0.6 if norm(q) == norm(_bare(title)) else 0.0)
             + (0.35 if songs >= 8 else -0.2 if songs <= 2 else 0.0)
             - _alternate_penalty(typed, title) - 0.1 - 0.05 * pos)
        out.append(Suggestion("album", title, artist, str(d["id"]), d.get("cover_medium") or "", int(d.get("nb_tracks") or 0),
                              s, {"pop": _popularity(d.get("nb_fan"), 2.0, 5.5)}))
    return out


def _artists(q, stop, q_toks):
    typed = _typed(q, q_toks)
    data = _get("/search/artist", {"q": q, "limit": 8}, stop)
    out = []
    for pos, d in enumerate((data or {}).get("data", [])):
        name = d.get("name", "")
        if not name or not d.get("id"):
            continue
        pop = _popularity(d.get("nb_fan"), 3.0, 7.0)
        s = 2.2 * _fit(typed, q_toks, name) + (1.3 * pop if norm(q) == norm(name) else 0.0) + 1.0 * pop - 0.05 * pos
        out.append(Suggestion("artist", name, "", str(d["id"]), d.get("picture_medium") or "", 0, s, {"pop": pop}))
    return out


def _playlists(q, stop, q_toks):
    typed = _typed(q, q_toks)
    data = _get("/search/playlist", {"q": q, "limit": 8}, stop)
    out = []
    for pos, d in enumerate((data or {}).get("data", [])):
        title = d.get("title", "")
        if not title or not d.get("id") or d.get("public") is False or int(d.get("nb_tracks") or 0) < 5:
            continue
        s = (2.0 * _fit(typed, q_toks, title) + 0.4 * _popularity(d.get("nb_tracks"), 0.7, 2.3) - 0.3 - 0.05 * pos
             - _alternate_penalty(typed, title))
        out.append(Suggestion("playlist", title, (d.get("user") or {}).get("name", ""), str(d["id"]),
                              d.get("picture_medium") or "", int(d.get("nb_tracks") or 0), s,
                              {"pop": _popularity(d.get("nb_tracks"), 0.7, 3.0) * 0.5}))
    return out


def _genre_matches(q, stop, q_toks):
    """The genre the text names ('rock', 'hip hop', 'jazz'), if it does: matched on whole words or the start of one."""
    out = []
    q_norm = norm(q)
    for gid, name in genres(stop):
        gn = norm(name)
        exact = q_norm == gn
        if gid and (exact or (len(q_norm) >= 3 and (gn.startswith(q_norm) or f" {q_norm}" in f" {gn}"))):
            out.append(Suggestion("genre", name, "Top songs right now", str(gid), "", 0, 3.6 if exact else 1.4))
    return out


def _splits(q):
    """The ways 'adele hello' / 'taylor swift love story' could be an artist followed by a song: (artist, song) pairs."""
    words = q.split()
    n = len(words)
    cuts = [c for c in (1, n - 1, 2) if 0 < c < n]      # first word | the rest, the rest | last word, two | the rest
    out = []
    for c in dict.fromkeys(cuts):
        pair = (" ".join(words[:c]), " ".join(words[c:]))
        if pair not in out:
            out.append(pair)
    return out[:3]


def _artist_then_song(q, stop, q_toks):
    """Deezer's own order puts 'Hello I'm Adele' first for 'adele hello'. When the text could be an artist and a song,
    ask for exactly that (one call per way of splitting it, side by side); only results by that artist count."""
    if " - " in q or len(q.split()) < 2:
        return []
    jobs = [(a, _POOL.submit(_get, "/search", {"q": f'artist:"{a}" track:"{t}"', "limit": 6}, stop)) for a, t in _splits(q)]
    out = []
    for a, job in jobs:
        data = job.result() or {}
        keep = [d for d in data.get("data", []) if fuzzy_overlap(toks(a), toks((d.get("artist") or {}).get("name", ""))) >= 1.0]
        out += _song_rows({"data": keep}, q, q_toks, boost=0.5)
    return out


# ---------------------------------------------------------------- the list on this computer (answers at once)

INDEX_MAX = 1500                             # most things remembered
INDEX_FILE = "suggest_index.json"
CHART_REFRESH = 3 * 86400                    # how often the popular songs / artists are fetched again


def _key(sg):
    return (sg.kind, norm(sg.title), norm(sg.subtitle))


class LocalIndex:
    """Everything the search has shown so far (and today's popular songs, artists, albums and playlists), kept in memory and
    in a file. Typing is matched against it on the spot, so the list is there before the first answer from the network."""

    def __init__(self, path=None):
        self.path = path
        self.items = {}                      # key -> (Suggestion, popularity 0..1, last seen)
        self.words = {}                      # key -> (title words, all words)
        self.charts_at = 0.0
        self.lock = threading.Lock()
        self.dirty = False

    # -- remembering
    def add(self, sugs):
        now = time.time()
        with self.lock:
            for sg in sugs:
                if sg.kind == "genre" or not sg.title:
                    continue
                k = _key(sg)
                old = self.items.get(k)
                pop = max(old[1] if old else 0.0, min(1.0, max(0.0, sg.data.get("pop", 0.0))))
                self.items[k] = (sg, pop, now)
                self.words[k] = (tuple(norm(sg.title).split()), tuple(norm(f"{sg.title} {sg.subtitle}").split()))
            if len(self.items) > INDEX_MAX:
                keep = sorted(self.items, key=lambda k: -(self.items[k][1] + (self.items[k][2] - now) / 8e6))[:INDEX_MAX]
                self.items = {k: self.items[k] for k in keep}
                self.words = {k: self.words[k] for k in keep}
            self.dirty = True

    # -- asking
    def match(self, q):
        """Suggestions whose words begin with the words typed (the last one may be half-typed), best first."""
        words = norm(q).split()
        if not words:
            return []
        out = []
        with self.lock:
            for k, (sg, pop, _seen) in self.items.items():
                title_w, all_w = self.words[k]
                if not all(any(w.startswith(t) for w in all_w) for t in words):
                    continue
                starts = " ".join(title_w).startswith(" ".join(words))
                whole = " ".join(title_w) == " ".join(words)
                score = 1.2 + 1.6 * pop + (1.4 if starts else 0.0) + (1.0 if whole else 0.0)
                out.append(Suggestion(sg.kind, sg.title, sg.subtitle, sg.id, sg.art, sg.count, score, sg.data))
        return _choose(out)

    # -- the file
    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.charts_at = float(data.get("charts_at") or 0)
            self.add(Suggestion(**row) for row in data.get("items", []))
            self.dirty = False
        except (OSError, ValueError, TypeError):
            pass

    def save(self):
        if not self.path or not self.dirty:
            return
        with self.lock:
            rows = [dict(kind=sg.kind, title=sg.title, subtitle=sg.subtitle, id=sg.id, art=sg.art, count=sg.count,
                         score=0.0, data=sg.data) for sg, _pop, _seen in self.items.values()]
            self.dirty = False
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"charts_at": self.charts_at, "items": rows}, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except OSError:
            pass


INDEX = LocalIndex()


def open_index(folder):
    """Point the index at its file in `folder` and read what an earlier run learned."""
    INDEX.path = os.path.join(folder, INDEX_FILE)
    INDEX.load()


def warm(stop=None):
    """Fetch what is popular right now (songs, artists, albums, playlists) into the index. Runs in the background a few
    seconds after start-up, and again after a few days."""
    if time.time() - INDEX.charts_at < CHART_REFRESH:
        return
    stop = stop or threading.Event()
    jobs = [_POOL.submit(_get, f"/chart/0/{part}", {"limit": n}, stop)
            for part, n in (("tracks", 60), ("artists", 60), ("albums", 40), ("playlists", 20))]
    got = []
    for part, job in zip(("tracks", "artists", "albums", "playlists"), jobs):
        data = job.result() or {}
        rows = data.get("data", [])
        n = len(rows) or 1
        for pos, d in enumerate(rows):
            heat = 1.0 - 0.6 * pos / n                       # the chart's own order: the top of it counts most
            if part == "tracks":
                for row in _song_rows({"data": [d]}, "", set()):
                    row.data["pop"] = heat
                    got.append(row)
            elif part == "artists" and d.get("name") and d.get("id"):
                got.append(Suggestion("artist", d["name"], "", str(d["id"]), d.get("picture_medium") or "", 0, 0.0,
                                      {"pop": heat}))
            elif part == "albums" and d.get("title") and d.get("id"):
                got.append(Suggestion("album", d["title"], (d.get("artist") or {}).get("name", ""), str(d["id"]),
                                      d.get("cover_medium") or "", 0, 0.0, {"pop": heat}))
            elif part == "playlists" and d.get("title") and d.get("id"):
                got.append(Suggestion("playlist", d["title"], (d.get("user") or {}).get("name", ""), str(d["id"]),
                                      d.get("picture_medium") or "", int(d.get("nb_tracks") or 0), 0.0, {"pop": heat}))
    if got:
        INDEX.add(got)
        INDEX.charts_at = time.time()
        INDEX.save()


def instant(query):
    """What is already known about `query`, at once (no network): the list to show before the real answer arrives."""
    q = " ".join(str(query or "").split())
    return INDEX.match(q) if len(q) >= MIN_CHARS else []


# ---------------------------------------------------------------- the list

def _choose(found):
    """The list shown: the best of each kind first (so a mood still shows its playlists), then the best of the rest,
    within the per-kind limits, best first."""
    ranked = sorted((s for s in found if s.score >= 0.6), key=lambda s: -s.score)
    chosen, taken = [], {}

    def same(a, b):
        return a.kind == b.kind and norm(a.title) == norm(b.title) and norm(a.subtitle) == norm(b.subtitle)

    for pass_one in (True, False):
        for sg in ranked:
            if sg in chosen or taken.get(sg.kind, 0) >= (1 if pass_one else CAPS[sg.kind]) or len(chosen) >= SHOWN:
                continue
            if any(same(o, sg) for o in chosen):                   # the same thing listed twice
                continue
            chosen.append(sg)
            taken[sg.kind] = taken.get(sg.kind, 0) + 1
    return sorted(chosen, key=lambda s: -s.score)


def search_all(query, stop=None, partial=None):
    """Ranked suggestions for what was typed (best first, at most SHOWN). Asks Deezer five things side by side. With
    `partial` (a callable) the list so far is handed to it each time one of them answers, so the first rows appear after
    the quickest answer rather than the slowest. What this computer already knows is part of every list."""
    stop = stop or threading.Event()
    q = " ".join(str(query or "").split())
    if len(q) < MIN_CHARS:
        return []
    q_toks = toks(q) or {norm(q)}
    found = list(instant(q)) + _genre_matches(q, stop, q_toks)
    jobs = [_POOL.submit(fn, q, stop, q_toks) for fn in (_songs, _albums, _artists, _playlists, _artist_then_song)]
    for j in as_completed(jobs):
        try:
            rows = j.result()
        except Exception as e:                                   # never let one kind of result spoil the rest
            log.info("suggestion search failed: %s", e)
            continue
        found += rows
        INDEX.add(rows)
        if partial and not stop.is_set():
            partial(_choose(found))
    return _choose(found)


# ---------------------------------------------------------------- turning a pick into songs

def _tracks(rows, service="search"):
    return [search._track(d, service=service) for d in rows if d.get("title") and d.get("readable") is not False]


def _list(path, ctx, want, params=None):
    """Up to `want` rows of a Deezer list (paged); also the total it holds."""
    rows, total, index = [], 0, 0
    while len(rows) < want:
        if ctx.stop.is_set():
            raise ResolveError("Cancelled")
        try:
            data = netio.get_json(f"{API}{path}", params={**(params or {}), "limit": min(100, want - len(rows)),
                                                          "index": index}, limiter=netio.DEEZER_LIMIT, stop=ctx.stop,
                                                          hedge=True, ttl=120)
        except EngineError as e:
            raise ResolveError(f"Couldn’t load that ({e})") from None
        page = (data or {}).get("data") or []
        total = int((data or {}).get("total") or total or len(page))
        rows += page
        index += len(page)
        if not page or index >= total:
            break
    return rows, total


def collect(sg, ctx):
    """The songs a suggestion stands for, as a Collection."""
    ctx.status(f"Loading {sg.title}…")
    if sg.kind == "song":
        d = sg.data.get("track") or {}
        t = search._track({"title": d.get("title"), "duration": d.get("duration"), "artist": {"name": sg.subtitle},
                           "album": d.get("album") or {}}, service="search")
        return Collection(title=t.title, subtitle=t.artist, tracks=[t], service="search", kind="track", artwork=t.artwork,
                          source=sg.label)
    if sg.kind == "album":
        col = search.album_collection(sg.id, ctx)
        col.source = sg.label
        return col
    if sg.kind == "artist":
        rows, _ = _list(f"/artist/{sg.id}/top", ctx, 50)
        tracks = _tracks(rows)
        if not tracks:
            raise ResolveError(f"No songs found for {sg.title}")
        return Collection(title=sg.title, subtitle="Top songs", tracks=tracks, service="search", kind="artist",
                          artwork=tracks[0].artwork, source=sg.label)
    if sg.kind == "playlist":
        rows, total = _list(f"/playlist/{sg.id}/tracks", ctx, PLAYLIST_MAX)
        tracks = _tracks(rows)
        if not tracks:
            raise ResolveError("That playlist has no songs we can use")
        notes = [f"First {len(tracks):,} of {total:,} songs"] if total > len(tracks) else []
        return Collection(title=sg.title, subtitle=sg.subtitle, tracks=tracks, service="search", kind="playlist",
                          artwork=tracks[0].artwork, notes=notes, source=sg.label)
    if sg.kind == "genre":
        rows, _ = _list(f"/chart/{sg.id}/tracks", ctx, 100)
        tracks = _tracks(rows)
        if not tracks:
            raise ResolveError(f"No chart found for {sg.title}")
        return Collection(title=f"Top {sg.title}", subtitle="Chart", tracks=tracks, service="search", kind="playlist",
                          artwork=tracks[0].artwork, source=sg.label)
    raise ResolveError("Don’t know how to open that")
