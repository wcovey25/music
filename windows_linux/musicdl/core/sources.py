"""
sources.py — finding and fetching audio for a song.

Sources: archive.org (the artist, the song and the length must all fit; bitrates read from the listing) and YouTube
(scored search results; the offered formats are probed without downloading so the best audio is tried first),
plus any URL yt-dlp understands when the track already names its exact recording.

A candidate is a plain dict:
    source, id, url, ext, seconds, kbps (estimated MP3-equivalent quality; 1411 = lossless), lossless, score, title
`score` says how surely this is *the song asked for* (title, artist, length, channel, popularity); it is on one scale
for every source so the engine can weigh a YouTube hit against an archive.org file. `rc` is the engine's per-track
context (title/artist tokens, expected lengths); `fits(rc, seconds, deep)` is its length rule, passed in so this
module stays free of engine state.

How a song is searched for: one YouTube query at a time, stopping as soon as a convincing match is among the results
(most songs need just one query); archive.org only when it can matter (see the engine); its item listings are read
side by side.
"""
import copy
import logging
import math
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from .. import platform_
from ..telemetry import T
from . import disk, netio
from .models import EngineError, Stopped
from .text import (extra_words, fuzzy_overlap, name_in, norm_keep_parens, parse_seconds, title_fit, toks)

log = logging.getLogger("musicdl")

AUDIO_EXT = (".mp3", ".flac", ".wav", ".aiff", ".aif", ".ogg", ".opus", ".m4a", ".aac")
LOSSLESS_EXT = (".flac", ".wav", ".aiff", ".aif")
LOSSLESS_KBPS = 1411

# never the song you asked for (unless the song's own title says so)
HARD_BAD = {"karaoke", "instrumental", "reaction", "tribute", "slowed", "reverb", "8d", "nightcore", "sped",
            "mashup", "parody", "tutorial", "lesson", "interview", "episode", "podcast", "trailer", "review",
            "vlog", "cover", "backing", "playalong"}
# probably a different recording of it; fine only when nothing better exists
SOFT_BAD = {"live", "remix", "medley", "hour", "hours", "loop", "scene", "clip", "concert", "tour", "acoustic",
            "unplugged", "stripped", "demo", "session", "sessions", "rehearsal", "orchestral"}
# a lossy codec at a given bitrate sounds like roughly this much MP3
CODEC_WEIGHT = {"opus": 1.5, "vorbis": 1.25, "aac": 1.2, "mp4a": 1.2, "m4a": 1.2, "mp3": 1.0}

CONFIDENT = 12.0         # a match this good needs no further searching
BAND = 2.5               # candidates within this many points of the best match are "equally good matches"
YT_QUERIES = ("ytsearch10:{a} {t}", "ytsearch8:{a} {t} official audio", "ytsearch6:{t} {a} topic")
RIPPED_KBPS = 160        # what a file that was ripped from a video is worth, whatever its container claims

# archive.org files that are really a video's soundtrack saved through a converter
_RIP = re.compile(r"online[- ]?audio[- ]?converter|y2mate|ytmp3|yt1s|savefrom|youtube|youtu\.be|official (?:music )?video|"
                  r"official audio|\((?:official )?lyrics?\)|lyric video|\bmv\b|\(hd\)|\(hq\)", re.I)
_VIDEO_ID = re.compile(r"^(?=.*[0-9_-])(?=.*[A-Za-z])[\w-]{11}$")
_TRACK_NO = re.compile(r"^\d{1,3}[\s._-]+")

_PERMANENT = ("video unavailable", "private video", "this video is not available", "has been removed",
              "no longer available", "account associated", "blocked it", "not available in your country",
              "confirm your age", "age-restricted", "members-only", "premium", "removed by the uploader",
              "unsupported url", "is not a valid url", "copyright")

_tl = threading.local()
_registry, _reg_lock = [], threading.Lock()
_META = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ia-meta")      # leaf tasks only: one HTTP call each


def yt_quality(fmt, info=None):
    """Rough 'MP3-equivalent kbps' of a YouTube audio format: Opus/AAC sound better than MP3 at the same bitrate."""
    info = info or {}
    abr = fmt.get("abr") or info.get("abr") or 0
    codec = str(fmt.get("acodec") or info.get("acodec") or "").lower()
    weight = next((w for k, w in CODEC_WEIGHT.items() if codec.startswith(k)), 1.0)
    return int(round(abr * weight)) if abr else 0


def est_kbps(c):
    """Quality we expect from a candidate before downloading it (lossless counts as the best there is)."""
    return LOSSLESS_KBPS if c["lossless"] else (c["kbps"] or 128)


def is_permanent(e):
    """Is this failure one that asking again cannot fix (the video is gone, private, blocked…)?"""
    if isinstance(e, netio.HttpStatus):
        return e.permanent
    s = str(e).lower()
    return any(p in s for p in _PERMANENT)


def penalty(rc, title):
    """None = clearly a different thing (karaoke, 8D, podcast…); otherwise how much to mark it down
    (live takes, remixes and re-recordings are acceptable only when nothing better turns up)."""
    words = set(norm_keep_parens(f"{rc.track.title} {rc.track.artist}").split())     # not the album: it may be a live one
    have = set(norm_keep_parens(title).split())
    if (have & HARD_BAD) - words:
        return None
    pen = 3.5 * len((have & SOFT_BAD) - words)
    if re.search(r"re-?record", title.lower()) and "rerecord" not in "".join(words):
        pen += 3.5
    return pen


def closeness(seconds, refs):
    """1.0 when `seconds` is exactly an expected length, falling off over a few seconds (0 when none is known)."""
    return max((math.exp(-((seconds - r) / max(4.0, 0.03 * r)) ** 2) for r in refs), default=0.0)


def artist_match(rc, heading, who=""):
    """0..1: do the heading or the uploader/creator name the artist? Spelling slips and 'ArianaGrandeVevo' count."""
    ov = fuzzy_overlap(rc.artist_toks, toks(heading) | toks(who))
    if ov < 1.0 and (name_in(rc.artist, who) or name_in(rc.artist, heading)):
        ov = 1.0
    return ov


# ---------------------------------------------------------------- yt-dlp plumbing

def _ydl(kind):
    """One YoutubeDL per thread and purpose (they are not thread-safe, and building one is slow)."""
    cache = getattr(_tl, "y", None)
    if cache is None:
        cache = _tl.y = {}
    if kind not in cache:
        from yt_dlp import YoutubeDL
        base = {"quiet": True, "no_warnings": True, "noprogress": True, "socket_timeout": 20, "skip_download": True}
        if kind == "search":
            base["extract_flat"] = True
        else:
            base.update({"format": "bestaudio/best", "noplaylist": True})
        ff = platform_.find_tool("ffmpeg")
        if ff:
            base["ffmpeg_location"] = os.path.dirname(ff)
        cache[kind] = YoutubeDL(base)
        with _reg_lock:
            _registry.append(cache[kind])
    return cache[kind]


def drop_thread_ydl():
    cache = getattr(_tl, "y", None)
    if cache:
        for y in cache.values():
            try:
                y.close()
            except Exception:
                pass
        _tl.y = {}


def drop_all_ydl():
    """Close every YoutubeDL any thread made (called when a run is over and its workers have finished)."""
    drop_thread_ydl()
    with _reg_lock:
        ys, _registry[:] = list(_registry), []
    for y in ys:
        try:
            y.close()
        except Exception:
            pass


# ---------------------------------------------------------------- archive.org

def _archive_docs(rc, stop):
    """Items that could be this song: first those filed under the artist, then (if there are none) any with the title."""
    title = rc.title.replace('"', " ")
    artist = rc.artist.replace('"', " ")
    queries = [f'title:("{title}") AND creator:("{artist}") AND mediatype:audio',
               f'title:("{title}") AND mediatype:audio']
    for q in queries:
        try:
            T.search()
            data = netio.get_json("https://archive.org/advancedsearch.php",
                                  params={"q": q, "fl[]": ["identifier", "title", "creator"],
                                          "sort[]": "downloads desc", "rows": "24", "output": "json"},
                                  limiter=netio.ARCHIVE_LIMIT, stop=stop, ttl=300)
        except EngineError as e:
            log.info("archive search failed for %s: %s", rc.track.label(), e)
            return []
        docs = ((data or {}).get("response") or {}).get("docs", [])
        if docs:
            return docs
    return []


def _archive_search(rc, stop, fits, deep):
    docs = []
    for d in _archive_docs(rc, stop):
        creator = d.get("creator") or ""
        creator = " ".join(creator) if isinstance(creator, list) else creator
        d["creator"] = creator
        if artist_match(rc, f"{d.get('title', '')} {d.get('identifier', '')}", creator) >= 0.5:
            docs.append(d)
        if len(docs) >= 8:
            break
    if not docs:
        return []

    def listing(d):
        try:
            return netio.get_json(f"https://archive.org/metadata/{quote(d['identifier'])}",
                                  limiter=netio.ARCHIVE_LIMIT, stop=stop, retries=2, ttl=600)
        except EngineError:
            return None

    metas = list(_META.map(listing, docs))                      # the listings are fetched side by side
    out = []
    for d, meta in zip(docs, metas):
        out += _archive_files(rc, d, (meta or {}).get("files", []))
    return out


def _archive_files(rc, d, files):
    """Candidates from one item's file listing."""
    audio = []
    for f in files:
        name = f.get("name", "")
        ext = os.path.splitext(name)[1].lower()
        seconds = parse_seconds(f.get("length"))
        if ext in AUDIO_EXT and seconds:
            audio.append((f, name, ext, seconds))
    single = len({_TRACK_NO.sub("", os.path.splitext(os.path.basename(n))[0]).lower() for _f, n, _e, _s in audio}) <= 1
    item_title = d.get("title", "")
    out = []
    for f, name, ext, seconds in audio:
        stem = _TRACK_NO.sub("", os.path.splitext(os.path.basename(name))[0])
        ftitle = f.get("title") or stem
        label = f"{ftitle} {stem} {item_title}"
        file_ov = fuzzy_overlap(rc.track_toks, toks(f"{ftitle} {stem}"))
        item_ov = fuzzy_overlap(rc.track_toks, toks(item_title)) if single else 0.0
        ov_t = max(file_ov, item_ov)
        if ov_t < 0.85:                                  # '001_Kick_01.wav' in an item called 'Bohemian Rhapsody' is a drum stem
            continue
        pen = penalty(rc, label)
        if pen is None:
            continue
        ov_a = artist_match(rc, f"{item_title} {ftitle} {d.get('identifier', '')}", d.get("creator", ""))
        fit = max(title_fit(rc.title, rc.artist, ftitle), title_fit(rc.title, rc.artist, item_title) if single else 0.0)
        size = int(f.get("size") or 0)
        lossless = ext in LOSSLESS_EXT
        try:
            kbps = float(f.get("bitrate"))
        except (TypeError, ValueError):
            kbps = size * 8 / seconds / 1000 if size else 0
        ripped = bool(_RIP.search(label) or _VIDEO_ID.match(stem))
        if ripped:                                       # a WAV made from a video's soundtrack is not lossless
            lossless, kbps = False, min(kbps or RIPPED_KBPS, RIPPED_KBPS)
        score = (4 * ov_a + 3 * ov_t + 2.5 * fit + 2.5 * closeness(seconds, rc.refs)
                 + (0.5 if f.get("source") == "original" else 0) - pen
                 - min(2.0, 0.4 * len(extra_words(rc.track_toks | rc.artist_toks, ftitle))))
        out.append({"source": "archive.org", "id": f"ia:{d['identifier']}/{name}",
                    "url": f"https://archive.org/download/{quote(d['identifier'])}/{quote(name)}",
                    "ext": ext, "seconds": seconds, "kbps": LOSSLESS_KBPS if lossless else int(kbps),
                    "lossless": lossless, "ripped": ripped, "score": score, "title": f.get("title") or name})
    return out


def archive_candidates(rc, stop, fits, deep):
    """archive.org files that match the artist, title and length (best few by score)."""
    if "archive" not in rc.raw:
        rc.raw["archive"] = _archive_search(rc, stop, fits, deep)
    ok = [c for c in rc.raw["archive"] if fits(rc, c["seconds"], deep)]
    return sorted(ok, key=lambda c: -c["score"])[:4]


# ---------------------------------------------------------------- YouTube

def _yt_more(rc, st, stop):
    """Run the next search query. False when there is none left (or searching keeps failing)."""
    if st["asked"] >= len(YT_QUERIES) or st["failed"] >= 2:
        return False
    if stop.is_set():
        raise Stopped()
    q = YT_QUERIES[st["asked"]].format(a=rc.artist, t=rc.title)
    st["asked"] += 1
    try:
        T.search()
        info = _ydl("search").extract_info(q, download=False)
    except Stopped:
        raise
    except Exception as e:
        st["failed"] += 1
        log.info("youtube search failed for %s: %s", rc.track.label(), e)
        return True
    for e in (info or {}).get("entries", []):
        if e and e.get("id") and e.get("duration") and e.get("availability") not in ("private", "needs_auth",
                                                                                      "premium_only", "subscriber_only"):
            st["entries"].setdefault(e["id"], e)
    return True


def _yt_score(e, rc, fits, deep, tol):
    """How surely video `e` is the song asked for (None = it is not)."""
    dur = float(e["duration"])
    if e.get("live_status") in ("is_live", "is_upcoming") or not fits(rc, dur, deep):
        return None
    title = e.get("title", "")
    chan = e.get("channel") or e.get("uploader") or ""
    ov_t = fuzzy_overlap(rc.track_toks, toks(title))
    ov_a = artist_match(rc, title, chan)
    if ov_t < 0.8 or ov_a < 0.5:
        return None
    pen = penalty(rc, title)
    if pen is None:
        return None
    s = 4 * ov_a + 3 * ov_t + 2.5 * title_fit(rc.title, rc.artist, title)
    refs = rc.refs + (rc.ext_refs or []) if deep else rc.refs
    s += 2.5 * closeness(dur, refs)
    low, lower = chan.lower(), title.lower()
    s += 2.0 * low.endswith("- topic") + 1.0 * ("vevo" in low)                      # the label's own audio uploads
    s += 1.0 * (name_in(rc.artist, chan) or fuzzy_overlap(rc.artist_toks, toks(chan)) >= 1.0)    # the artist's channel
    s += 0.6 * bool(e.get("channel_is_verified"))
    s += 0.5 * ("official" in lower) + 0.5 * ("audio" in lower)
    s -= 0.4 * ("lyric" in lower) + 0.4 * ("video" in lower and "audio" not in lower)    # videos have intros and skits
    s += min(1.0, math.log10(max(1, int(e.get("view_count") or 0))) / 9.0)         # a billion views is hard to fake
    s -= pen
    s -= min(2.0, 0.4 * len(extra_words(rc.track_toks | rc.artist_toks, title)))
    return s


def _yt_ranked(rc, st, fits, deep, tol):
    scored = []
    for e in st["entries"].values():
        s = _yt_score(e, rc, fits, deep, tol)
        if s is not None:
            scored.append((s, e))
    scored.sort(key=lambda t: -t[0])
    return scored


def convincing(rc, score, seconds, deep=False):
    """Is a match this good surely the song — by name *and* by length (a 16-second difference is another cut)?"""
    if score < CONFIDENT:
        return False
    refs = rc.refs + (rc.ext_refs or []) if deep else rc.refs
    return not refs or closeness(float(seconds), refs) >= 0.4


def _convincing(rc, ranked, deep):
    return bool(ranked) and convincing(rc, ranked[0][0], ranked[0][1]["duration"], deep)


def youtube_candidates(rc, stop, fits, deep, tol):
    """Videos that are this song, best match first. Queries are asked one at a time; the search ends as soon as one of
    the results is a convincing match, so most songs cost a single search."""
    st = rc.raw.setdefault("yt", {"entries": {}, "asked": 0, "failed": 0})
    probes = rc.raw.setdefault("probe", {})
    ranked = _yt_ranked(rc, st, fits, deep, tol)
    while not _convincing(rc, ranked, deep) and _yt_more(rc, st, stop):
        ranked = _yt_ranked(rc, st, fits, deep, tol)
    out = []
    for s, e in ranked:
        known = probes.get("yt:" + e["id"]) or {}
        if known.get("dead"):
            continue
        out.append({"source": "youtube", "id": "yt:" + e["id"],
                    "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                    "vid": e["id"], "seconds": float(e["duration"]), "kbps": known.get("kbps", 0), "lossless": False,
                    "score": s, "title": e.get("title", ""), "probed": bool(known), "_info": known.get("info")})
        if len(out) == 4:
            break
    return out


def direct_candidate(track):
    """The recording a link already points at (YouTube video, SoundCloud track …)."""
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([\w-]{11})", track.url)
    return {"source": "youtube" if m else "web", "id": "direct:" + track.url, "url": track.url,
            "vid": m.group(1) if m else "", "seconds": float(track.duration or 0), "kbps": 0, "lossless": False,
            "score": 99.0, "title": track.title, "direct": True}


def probe(cand, stop, cache=None):
    """Ask the site which audio it offers (nothing is downloaded) so the best source is tried first. What it found is
    kept on the candidate — the download starts from it instead of asking the site all over again — and in `cache`
    (the song's own dict) so a rebuilt candidate list does not ask twice. A video that is gone is marked dead."""
    if stop.is_set():
        raise Stopped()
    cand["probed"] = True
    res = {"kbps": 0}
    try:
        T.search()
        info = _ydl("probe").extract_info(cand["url"], download=False)
        res = {"kbps": yt_quality(info or {}, info), "info": info}
    except Stopped:
        raise
    except Exception as e:
        log.info("probe failed for %s: %s", cand.get("title"), e)
        res["dead"] = is_permanent(e)
    cand["kbps"], cand["_info"], cand["dead"] = res["kbps"], res.get("info"), res.get("dead", False)
    if cache is not None:
        cache[cand["id"]] = res
    return res


# ---------------------------------------------------------------- downloading

def fetch(cand, tmpdir, stop):
    """Download one candidate. Returns (path, estimated_kbps, codec_family_hint)."""
    if cand["source"] == "archive.org":
        dest = os.path.join(tmpdir, "src" + cand["ext"])
        netio.download(cand["url"], dest, stop=stop, min_bytes=100_000)
        return dest, cand["kbps"], cand["ext"].lstrip(".")
    return _yt_download(cand, tmpdir, stop)


def _yt_download(cand, tmpdir, stop):
    from yt_dlp import YoutubeDL
    last = {}

    def hook(d):
        if stop.is_set():
            raise Stopped()
        if d.get("status") == "downloading":
            fid, done = d.get("filename"), d.get("downloaded_bytes") or 0
            T.add_bytes(max(0, done - last.get(fid, 0)))
            last[fid] = done
    opts = {"format": "bestaudio/best", "noplaylist": True, "quiet": True, "no_warnings": True, "noprogress": True,
            "outtmpl": os.path.join(tmpdir, "yt.%(ext)s"), "progress_hooks": [hook], "socket_timeout": 30,
            "retries": 3, "fragment_retries": 3, "windowsfilenames": True, "concurrent_fragment_downloads": 4}
    ff = platform_.find_tool("ffmpeg")
    if ff:
        opts["ffmpeg_location"] = os.path.dirname(ff)
    with YoutubeDL(opts) as y:
        info = None
        pre = cand.get("_info")
        if pre:                                          # the probe already did the slow part (reading the page)
            try:
                info = y.process_ie_result(copy.deepcopy(pre), download=True)
            except Stopped:
                raise
            except Exception as e:
                if disk.is_disk_full(e) or is_permanent(e):
                    raise
                log.info("download from the probe's answer failed (%s); asking again", e)
                cand["_info"] = None
        if info is None:
            info = y.extract_info(cand["url"], download=True)
    files = [os.path.join(tmpdir, f) for f in os.listdir(tmpdir) if f.startswith("yt.") and not f.endswith(".part")]
    if not files:
        raise EngineError("nothing downloaded")
    req = ((info or {}).get("requested_downloads") or [{}])[0]
    return files[0], yt_quality(req, info), str(req.get("acodec") or (info or {}).get("acodec") or "")
