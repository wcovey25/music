"""
closematch.py — what to offer for a song that could not be found exactly.

A big playlist always has a few songs the strict search cannot place: the title is spelled differently on YouTube, the
only upload is a live take or a slightly different cut, the artist is credited another way. Instead of just reporting
"not found", the song is searched for *around* — the way a person would — and what turns up is offered, each option
saying in plain words why it is not an exact match ("Live version", "0:42 longer", "Uploaded by another artist").

Where the options come from, cheapest first:
  1. the results the strict search already fetched (scored again with looser rules: no network at all);
  2. the title alone (the artist may be credited differently);
  3. the artist and the song's words without punctuation, plus "lyrics" (often the only upload of an obscure song).
The search stops as soon as there are enough good options.

Never offered: karaoke, tutorials, reactions, podcasts, nightcore/8D/slowed versions, mashups, hour-long loops — nothing
a person looking for the song would call close. Covers, live takes, remixes and acoustic versions are offered but
labelled; they are never picked automatically.

An option is a candidate dict (see sources.py) plus `channel`, `why` (list of short phrases), `safe` (True when the
only difference from the song is its length, so it may be taken without asking) and `close` (always True).
"""
import logging
import math
import re

from . import sources
from .models import Stopped
from .text import extra_words, fuzzy_overlap, name_in, norm, norm_keep_parens, title_fit, toks

log = logging.getLogger("musicdl")

WANT = 3                  # stop searching once this many good options are in hand
MAX_OPTIONS = 5           # what is offered at most
MAX_OTHER_ARTIST = 2      # of those, at most this many by an artist who does not match
MIN_SCORE = 5.0           # below this an option is noise
SAFE_LENGTH = 0.25        # automatic picking: at most this far from the real length (as a fraction)
DROP_LENGTH = 0.6         # further than this from every known length: another piece altogether
MIN_SECONDS = 45
MAX_UNKNOWN_SECONDS = 15 * 60

# worth showing, with a label — the word that gives them away is not held against a song that has it in its own name
LABELLED = {"live": "Live version", "cover": "Cover", "remix": "Remix", "acoustic": "Acoustic version",
            "unplugged": "Unplugged version", "instrumental": "Instrumental", "demo": "Demo", "session": "Session",
            "sessions": "Session", "concert": "Live version", "orchestral": "Orchestral version",
            "medley": "Medley", "stripped": "Stripped version", "rehearsal": "Rehearsal"}
# never offered
EXCLUDED = (sources.HARD_BAD - set(LABELLED)) | {"hour", "hours", "loop", "megamix", "compilation", "playlist",
                                                 "ringtone", "type", "beat"}

# the order the ladder asks in; the first rung is the strict search's own results (no query)
_RUNGS = ("ytsearch8:{t}", "ytsearch8:{a} {t} lyrics", "ytsearch6:{plain}")


def _wanted_words(rc):
    """Words the song itself has ('Live and Let Die' is not a live version); brackets count: 'Hello (Live)'."""
    return set(norm_keep_parens(f"{rc.track.title} {rc.track.artist}").split())


def _labels(rc, title):
    """The labels that apply to `title`, or None when it is something never offered."""
    words = _wanted_words(rc)
    have = set(re.findall(r"[a-z0-9]+", title.lower()))
    if (have & EXCLUDED) - words:
        return None
    out = []
    for w in sorted((have & set(LABELLED)) - words):
        name = LABELLED[w]
        if name not in out:
            out.append(name)
    if re.search(r"re-?record", title.lower()):
        out.append("Re-recording")
    return out


def _length(seconds, refs, tol):
    """(drop?, deviation as a fraction of the nearest known length, label for the user)."""
    if not refs:
        if seconds < MIN_SECONDS or seconds > MAX_UNKNOWN_SECONDS:
            return True, 0.0, ""
        return False, 0.0, ""
    if seconds < MIN_SECONDS and not any(r < MIN_SECONDS * 1.3 for r in refs):
        return True, 1.0, ""
    ref = min(refs, key=lambda r: abs(seconds - r))
    dev = abs(seconds - ref) / ref
    if dev > DROP_LENGTH:
        return True, dev, ""
    if dev <= tol:
        return False, dev, ""
    gap = int(round(abs(seconds - ref)))
    text = f"{gap // 60}:{gap % 60:02d}"
    return False, dev, f"{text} {'longer' if seconds > ref else 'shorter'}"


def judge(rc, e, refs, tol):
    """Score one search result `e` as an option for the song; None when it is not worth offering."""
    try:
        dur = float(e.get("duration") or 0)
    except (TypeError, ValueError):
        return None
    if not dur or e.get("live_status") in ("is_live", "is_upcoming"):
        return None
    title = e.get("title", "")
    chan = e.get("channel") or e.get("uploader") or ""
    labels = _labels(rc, title)
    if labels is None:
        return None
    drop, dev, length_label = _length(dur, refs, tol)
    if drop:
        return None
    ov_t = fuzzy_overlap(rc.track_toks, toks(title))
    ov_a = sources.artist_match(rc, title, chan)
    other_artist = ov_a < 0.5
    if other_artist and ov_t < 1.0:                    # another artist's song must at least have every word of the title
        return None
    if ov_t < 0.5:
        return None
    fit = title_fit(rc.title, rc.artist, title)
    low = chan.lower()
    s = 4 * ov_a + 3 * ov_t + 2.5 * fit
    s += 2.0 * max(0.0, 1.0 - dev / DROP_LENGTH) if refs else 1.0
    s += 2.0 * low.endswith("- topic") + 1.0 * ("vevo" in low)
    s += 1.0 * (name_in(rc.artist, chan) or fuzzy_overlap(rc.artist_toks, toks(chan)) >= 1.0)
    s += 0.6 * bool(e.get("channel_is_verified"))
    s += min(1.0, math.log10(max(1, int(e.get("view_count") or 0))) / 9.0)
    s -= 1.2 * len(labels) + (2.0 if other_artist else 0.0)
    s -= min(2.0, 0.4 * len(extra_words(rc.track_toks | rc.artist_toks, title) - toks(chan)))     # the uploader's name is no noise
    if s < MIN_SCORE:
        return None
    why = list(labels)
    if length_label:
        why.append(length_label)
    if other_artist:
        why.append(f"Uploaded by {chan}" if chan else "Different artist")
    elif ov_t < 0.8:
        why.append("Title differs")
    safe = (not labels and not other_artist and ov_t >= 0.9 and ov_a >= 0.8 and bool(refs) and dev <= SAFE_LENGTH)
    return {"source": "youtube", "id": "yt:" + e["id"], "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
            "vid": e["id"], "seconds": dur, "kbps": 0, "lossless": False, "score": s, "title": title, "channel": chan,
            "why": why or ["Close match"], "safe": safe, "close": True, "views": int(e.get("view_count") or 0)}


def _plain(rc):
    return " ".join(x for x in (norm(rc.title), norm(rc.artist)) if x)


def _search(rc, st, query, stop):
    """Run one query into `st`. False when searching is not working (the ladder gives up)."""
    if stop.is_set():
        raise Stopped()
    try:
        info = sources._ydl("search").extract_info(query, download=False)
    except Stopped:
        raise
    except Exception as e:
        st["failed"] += 1
        log.info("close-match search failed for %s: %s", rc.track.label(), e)
        return st["failed"] < 2
    for e in (info or {}).get("entries", []):
        if e and e.get("id") and e.get("duration") and e.get("availability") not in ("private", "needs_auth",
                                                                                      "premium_only", "subscriber_only"):
            st["entries"].setdefault(e["id"], e)
    return True


def _pick(options):
    """The best few, with at most MAX_OTHER_ARTIST by another artist. Same video is never listed twice."""
    out, others, seen = [], 0, set()
    for o in sorted(options, key=lambda o: -o["score"]):
        if o["id"] in seen:
            continue
        other = any(w.startswith("Uploaded by") or w == "Different artist" for w in o["why"])
        if other and others >= MAX_OTHER_ARTIST:
            continue
        seen.add(o["id"])
        others += other
        out.append(o)
        if len(out) == MAX_OPTIONS:
            break
    return out


def around(rc, stop, refs, tol, youtube=True):
    """Options for a song the strict search could not place, best first (empty when there is nothing close).

    `refs` are every length the song is known to have (seconds). Results the strict search already fetched are reused;
    the rest of the ladder is asked one query at a time and stops once WANT good options are in hand. The queries and
    their answers are kept on `rc.raw`, so asking again costs nothing."""
    if not youtube:
        return []
    st = rc.raw.setdefault("close", {"entries": {}, "asked": 0, "failed": 0})
    strict = (rc.raw.get("yt") or {}).get("entries", {})

    def options():
        pool = dict(strict)
        pool.update(st["entries"])
        return _pick([o for o in (judge(rc, e, refs, tol) for e in pool.values()) if o])

    got = options()
    queries = []
    for rung in _RUNGS:
        q = rung.format(a=rc.artist, t=rc.title, plain=_plain(rc))
        if q not in queries:
            queries.append(q)
    while len([o for o in got if o["score"] >= MIN_SCORE + 3]) < WANT and st["asked"] < len(queries) and st["failed"] < 2:
        q = queries[st["asked"]]
        st["asked"] += 1
        if not _search(rc, st, q, stop):
            break
        got = options()
    return got
