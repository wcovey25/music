"""
engine.py — the download job.

For every Track it makes sure there is a correct, tagged file in the output folder:

  * existing files are recognised and verified (readable? right length? good enough source? artwork? lyrics?)
    and only the ones with problems are touched — so stopping and re-running picks up where it left off;
  * a song that is already in the folder — under another name or in another format — is never downloaded again.
    The one exception is a *better version*: if the quality now chosen is clearly higher than what the folder holds,
    the job asks (through the `ask` callback) whether to replace those files. If it says yes the best available
    version is fetched, falling back step by step (lossless -> 320 -> 256 …) to the best one that is still an
    improvement, and the old file is removed only after the new one is saved; if nothing better exists, the old file
    stays and the song is remembered as "best available";
  * new songs are fetched from the exact recording a link points at, or found on archive.org / YouTube and
    accepted only if their length matches the song (that is what keeps a 75-minute podcast out of the library);
  * audio is encoded (or copied untouched) to the chosen format and tagged; artwork and lyrics are added;
  * songs run in parallel — how many at once is planned from the hardware scan and, on a long run, adjusted while it
    goes by a governor (resources.py); shared rate limiters keep every web service happy.

Nothing here knows about windows: progress is reported through an `emit(dict)` callback with events
phase / plan / begin / stage / result.
"""
import logging
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field

from .. import platform_, resources
from ..audio import process, sampler, transcode
from ..config import FORMATS, OutFmt, size_kbps_of
from ..meta import lyrics as lyrics_mod
from ..meta import naming
from ..meta.artwork import GOOD_SIDE, cover_side, prepare, sized
from ..meta.tags import Tagset, extract_cover, read_info, write as write_tags
from ..telemetry import T
from . import catalog, closematch, disk, netio, quality, sources
from .dedupe import FolderIndex
from .disk import DiskGuard
from .library import Library
from .models import DiskFull, EngineError, Result, Stopped, Track
from .text import core_title, first_artist, toks

log = logging.getLogger("musicdl")

YT_ENOUGH = 200                      # what a YouTube audio stream is good for (MP3-equivalent kbps): above this, look further
MIN_SECONDS = 45                     # shorter than this is a preview clip, not a song
MAX_UNKNOWN_SECONDS = 15 * 60        # longest we accept when we don't know how long the song should be
SCAN_UNKNOWN_SECONDS = 8 * 60        # same, for the no-network pre-scan (longer files get a second look)
RETRY_AFTER = 7 * 86400              # how long before searching again for something that wasn't found
UPGRADE_RETRY = 30 * 86400           # how long "no better version exists" is believed
RECHECK = 6.0                        # seconds before a catalogue that could not be asked (rate limit, outage) is asked again


class _Transient(Exception):
    """A picture could not be fetched *this time* (network trouble): not a reason to remember "no artwork"."""


@dataclass
class Ctx:
    """Everything the job knows about one track while it is being processed."""
    index: int
    track: Track
    title: str                       # core title (no '- Remastered 2009', no '(feat. …)')
    artist: str                      # first artist
    rel: str = ""                    # output path relative to the folder ('/'-separated)
    final: str = ""
    refs: list = field(default_factory=list)       # expected lengths in seconds
    ext_refs: list = None            # extra opinions (Deezer / iTunes), fetched lazily
    track_toks: set = field(default_factory=set)
    artist_toks: set = field(default_factory=set)
    raw: dict = field(default_factory=dict)        # cached search results per source
    cat: dict = None                 # catalogue lookup
    cat_t: float = 0.0               # when it was made (monotonic)
    clean: bool = False              # the clean edit is wanted (the setting), not the explicit original
    cover: tuple = None              # (jpeg, thumb, source) once found
    cover_done: bool = False
    src_cover: bytes = None          # the picture inside the downloaded file, a fallback when no service has one
    fmt: object = None               # the format this song is written in, when it differs from the job's (fallback)
    cands: list = None               # the ranked search result, when a scout has already made it
    scouted: bool = False
    lock: object = field(default_factory=threading.RLock)    # one thread at a time searches for / looks up this song

    def release(self):
        """Drop the heavy bits (cover bytes, search results) as soon as the track is finished."""
        self.raw, self.cover, self.cat, self.cands, self.src_cover = {}, None, None, None, None


@dataclass
class Plan:
    kind: str                        # ok | new | extras | redo | dup | have | upgrade
    info: object = None
    reasons: tuple = ()
    low_quality: bool = False
    no_cover: bool = False
    note: str = ""
    old_path: str = ""               # upgrade: the file a better version would replace
    old_rel: str = ""
    fallback: object = None          # upgrade: what happens to the song if the user keeps the existing file


class Job:
    """One run over a list of tracks. Call run(); progress arrives through emit(event_dict).

    `ask(summary) -> bool` is called (from the job's thread, before any download starts) when songs in the folder are
    clearly lower quality than the quality now chosen; True means "replace them with better versions". It may block
    until the user answers and may raise Stopped. Without it (headless runs) nothing is replaced."""

    def __init__(self, tracks, outdir, settings, emit=None, stop=None, ai=None, ask=None):
        self.tracks, self.outdir, self.st = list(tracks), outdir, settings
        self.emit = emit or (lambda ev: None)
        self.stop = stop or threading.Event()
        self.ai = ai
        self.ask = ask
        self.disk = DiskGuard(outdir, self.stop, self.emit)       # a full disk pauses the run instead of failing songs
        self._covers = netio.Memo(48)                             # prepared artwork by URL: an album's art is made once
        self.fmt = settings.out_format()
        self.capped = bool(getattr(settings, "capped", False))   # never bigger than the source deserves (every mode)
        self.no_lossless = 0                                      # songs written lossy although lossless was chosen
        self.matched = 0                                          # songs written below what was chosen: that is all the source has
        self.close_mode = getattr(settings, "close_match", "ask")    # what to do with a song that has no exact match
        self.close_taken = 0                                      # songs saved from a close match (close_match = auto)
        self.budget = resources.Budget.unlimited()                # songs at once / encodes at once (set up for real by _run_pool)
        self.governor = None                                      # watches CPU, power and the network during a long run
        self.target_q = quality.target(self.fmt)
        self.spec = settings.audio_spec() if hasattr(settings, "audio_spec") else None   # level / trim / enhance, or None
        self.template = settings.name_template()
        self.lib = None
        self._index = None
        self.counts = {}
        self.bytes_written = 0
        self.tmp_root = os.path.join(outdir, ".musicdl_tmp")
        self._name_locks = {}
        self._lock = threading.Lock()
        self.tol = settings.tolerance / 100.0
        self.crit = [settings.tolerance, settings.min_kbps if settings.replace_low else 0, bool(settings.use_art()),
                     bool(settings.verify), self.fmt.signature(), bool(settings.use_tags()), bool(settings.use_lyrics()),
                     self.capped]

    # ---------------------------------------------------------------- helpers

    def _check(self):
        if self.stop.is_set():
            raise Stopped()

    def _say(self, text):
        self.emit({"type": "phase", "text": text})

    def _stage(self, rc, text):
        self.emit({"type": "stage", "index": rc.index, "text": text})

    @contextmanager
    def _cpu(self):
        """A place among the CPU-heavy steps (see Budget.cpu); Stopped if the run is stopped while waiting for one."""
        with self.budget.cpu(self.stop) as lvl:
            if lvl is None:
                raise Stopped()
            yield lvl

    def is_low(self, kbps):
        """Below the user's quality floor? A 6% margin so a 122 kbps VBR file isn't 'low' against a 128 floor."""
        floor = self.st.min_kbps
        return bool(floor) and 0 < kbps < floor * 0.94

    # ---------------------------------------------------------------- entry point

    def run(self):
        os.makedirs(self.outdir, exist_ok=True)
        shutil.rmtree(self.tmp_root, ignore_errors=True)
        os.makedirs(self.tmp_root, exist_ok=True)
        self.lib = Library(self.outdir)
        netio.reset_health()                                      # a host that failed in an earlier run gets a fresh start
        netio.prewarm(netio.RUN_HOSTS)                            # (connections to the lookup services, opened while the plan is made)
        T.start()
        try:
            self._say("Looking up song details…")
            self._prepare()
            self._check()
            plans = self._scan()
            self._check()
            plans = self._decide_upgrades(plans)
            self._check()
            todo = [(rc, p) for rc, p in plans if p.kind in ("new", "extras", "redo", "upgrade")]
            self.emit({"type": "plan", "total": len(plans), "todo": len(todo)})
            self._say(f"Working on {len(todo)} song{'s' if len(todo) != 1 else ''}…" if todo
                      else "Everything is up to date")
            self._run_pool(todo)
        except Stopped:
            pass
        finally:
            self.lib.forget_old()
            self.lib.save(force=True)
            self._backup_library()
            shutil.rmtree(self.tmp_root, ignore_errors=True)
            T.stop()
            sources.drop_all_ydl()
            netio.close_thread_session()
        return self.counts

    def _backup_library(self):
        """Keep a copy of the folder's record (the last three different ones) next to the settings backups."""
        try:
            from . import shield
            shield.backup_library(self.outdir, shield.shield_dir(platform_.config_dir()))
        except Exception:
            log.debug("library backup failed", exc_info=True)

    def summary(self):
        """What the run did differently from what was chosen, in sentences ('' when nothing)."""
        out = []
        n = self.no_lossless
        if n:
            lossy = FORMATS["aac" if self.fmt.key == "alac" else "mp3"]["label"]
            out.append(f"{n} song{'s' if n != 1 else ''} had no lossless version to download, so "
                       f"{'they were' if n != 1 else 'it was'} saved as {lossy} instead of {self.fmt.label}.")
        m = self.matched
        if m:
            out.append(f"{m} song{'s' if m != 1 else ''} came from {'sources' if m != 1 else 'a source'} that holds "
                       f"less than {self.fmt.label}, so {'they were' if m != 1 else 'it was'} saved at the quality "
                       f"the source has instead of being padded.")
        c = self.close_taken
        if c:
            out.append(f"{c} song{'s' if c != 1 else ''} had no exact match, so {'they were' if c != 1 else 'it was'} "
                       f"saved from the closest one found (the note beside each says how it differs).")
        return " ".join(out)

    def _prepare(self):
        """Fix up track details before anything is searched: AI repair (optional), MusicBrainz lengths."""
        if self.ai is not None:
            for wanted, step in ((self.st.ai_repair, self.ai.repair), (self.st.ai_organize, self.ai.organize)):
                if not wanted:
                    continue
                try:
                    step(self.tracks, self.stop, self._say)
                except Stopped:
                    raise
                except Exception as e:                           # AI is a bonus; never block the run on it
                    log.warning("AI step skipped: %s", e)
        catalog.mb_prefetch(self.lib, [t.mbid for t in self.tracks], self.stop, self._say)

    def _run_pool(self, todo):
        """Work through `todo` with a few worker threads. How many songs are in flight at once is not the number of
        threads: it is the size of the song gate (Budget.songs), which a long run's Governor changes while it runs
        (more while the machine and the network have room, fewer on battery, on errors or when sites push back)."""
        st = self.st
        pl = resources.plan(resources.detect(), (st.auto_info or {}).get("mbps"))
        wanted = max(1, int(st.effective_parallel()))
        manual = not (st.auto and st.auto_parallel)                  # a number the user chose is a ceiling
        governed = len(todo) >= resources.MIN_GOVERNED
        cap = (wanted if manual else max(pl.ceiling, wanted)) if governed else min(wanted, max(1, len(todo)))
        start = min(cap, wanted)
        self.budget = budget = resources.Budget(pl, songs=start)
        scouts = _Scouts(self, todo, budget)
        queue, qlock, taken = iter(todo), threading.Lock(), [0]

        if governed:
            self.governor = resources.Governor(budget, cap=cap, start=start, manual=manual,
                                               link_mbps=(st.auto_info or {}).get("mbps"),
                                               backlog=lambda: len(todo) - taken[0], emit=self.emit)

        def song(rc, plan):
            scouts.advance()                                       # this song is taken: the scouts may look one further ahead
            self.emit({"type": "begin", "index": rc.index, "track": rc.track})
            res = self._work(rc, plan)
            rc.release()
            if res.status != "stopped":
                T.song_done()
                self._finish(rc, res)

        def worker():
            while not self.stop.is_set():
                place = budget.songs.acquire(self.stop)            # waits while the governor has the run held to fewer songs
                if place is None:
                    return
                try:
                    with qlock:                                    # songs are taken in order, by whoever gets a place
                        item = next(queue, None)
                        taken[0] += item is not None
                    if item is None:
                        return
                    lvl = budget.level
                    if lvl:
                        platform_.thread_priority(lvl)             # on battery this thread works at a lower priority
                    try:
                        song(*item)
                    except Exception:
                        log.exception("worker crashed")
                    finally:
                        if lvl:
                            platform_.thread_priority(0)
                finally:
                    budget.songs.release(place)

        workers = [threading.Thread(target=worker, name="song", daemon=True) for _ in range(cap)]
        scouts.start()
        if self.governor:
            self.governor.start()
        try:
            for w in workers:
                w.start()
            for w in workers:
                w.join()
        finally:
            scouts.close()
            if self.governor:
                self.governor.close()

    def _finish(self, rc, res):
        if not res.path:
            res.path = rc.final if os.path.exists(rc.final) else ""
        with self._lock:
            self.counts[res.status] = self.counts.get(res.status, 0) + 1
            self.bytes_written += res.size if res.status in ("ok", "upgraded") else 0
        self.emit({"type": "result", "index": rc.index, "total": len(self.tracks), "track": rc.track, "result": res})

    # ---------------------------------------------------------------- per-track context

    def _ctx(self, i, track):
        m = self.lib.get("meta", track.mbid) if track.mbid else None
        rc = Ctx(index=i, track=track, title=core_title(track.title), artist=first_artist(track.artist))
        rc.refs = ([m["len"] / 1000.0] if m and m.get("len") else []) + ([float(track.duration)] if track.duration else [])
        rc.track_toks, rc.artist_toks = toks(rc.title), toks(rc.artist)
        rc.clean = bool(getattr(self.st, "clean_versions", False))
        self._cached_details(track)
        self._set_path(rc)
        return rc

    def _set_path(self, rc):
        rc.rel = naming.relative_name(rc.track, self.template, (rc.fmt or self.fmt).ext)
        rc.final = naming.full_path(self.outdir, rc.rel)

    def _cached_details(self, track):
        """Album/year/track number from the catalogue cache (no network) so templates see the same values every run."""
        if "{" not in self.template or all(f not in self.template for f in ("{album", "{year", "{track", "{disc", "{genre")):
            return
        key_hit = self.lib.get("catalog", _cat_key(track))
        if key_hit and catalog.fresh(key_hit):                    # (an answer from an older layout may name the wrong album)
            self._merge_details(track, key_hit)

    @staticmethod
    def _merge_details(track, cat):
        catalog.merge_details(track, cat)

    @staticmethod
    def _wants_details(t):
        """Is anything a player groups or sorts by still unknown? (A song that is complete needs no catalogue lookup.)"""
        return not (t.album and t.year and t.track_no and t.track_total and t.genre and t.album_artist)

    def _cat(self, rc, patient=False):
        """The catalogue's answer for this song. One that could not be had (rate limit, outage) is not kept: it is asked
        again, after a short wait when `patient`."""
        with rc.lock:
            if rc.cat is None or rc.cat.get("failed"):
                if rc.cat is not None and patient:
                    wait = RECHECK - (time.monotonic() - rc.cat_t)
                    if wait > 0 and self.stop.wait(wait):
                        raise Stopped()
                if rc.cat is None or patient or time.monotonic() - rc.cat_t >= RECHECK:     # (patient: just waited it out)
                    rc.cat = catalog.lookup(self.lib, rc.track, self.stop)
                    rc.cat_t = time.monotonic()
            return rc.cat

    def _ext_refs(self, rc):
        with rc.lock:
            if rc.ext_refs is None:
                cat = self._cat(rc)
                if cat.get("failed"):
                    return []                                     # not asked: ask again next time
                rc.ext_refs = cat["durations"]
            return rc.ext_refs

    # ---------------------------------------------------------------- length rules

    def fits(self, rc, seconds, deep=False, scan=False):
        """Is `seconds` a believable length for this song?

        deep    also accept the lengths Deezer/iTunes report (a second opinion when MusicBrainz is off)
        scan    no network: used while classifying existing files, so a song with no known length is only
                trusted up to SCAN_UNKNOWN_SECONDS and anything longer is double-checked by a worker
        """
        if rc.track.extra.get("picked"):                          # the user chose this recording: its length is their call
            return seconds >= 5
        refs = list(rc.refs)
        if deep or (not refs and not scan and not rc.track.direct):
            refs += self._ext_refs(rc)
        if rc.track.direct and not refs:
            return seconds >= 5                                    # the link names this recording: trust it
        if seconds < MIN_SECONDS and not any(r < MIN_SECONDS * 1.3 for r in refs):
            return False
        if not refs:
            return seconds <= (SCAN_UNKNOWN_SECONDS if scan else MAX_UNKNOWN_SECONDS)
        return any(abs(seconds - r) <= self.tol * r for r in refs)

    # ---------------------------------------------------------------- phase 2: look at what is already in the folder

    def _scan(self):
        plans, seen, claimed = [], set(), set()
        total = len(self.tracks)
        for i, track in enumerate(self.tracks):
            self._check()
            if i % 25 == 0:
                self._say(f"Checking your library… {i}/{total}")
            rc = self._ctx(i, track)
            key = rc.rel.lower()
            plan = Plan("dup") if key in seen else self._second_look(rc, self._classify(rc))
            seen.add(key)
            if plan.kind == "upgrade":                               # two list entries can't replace the same file
                old = os.path.normcase(plan.old_path)
                plan = plan.fallback if old in claimed else plan
                claimed.add(old)
            plans.append((rc, plan))
            self._settle(rc, plan)
        return plans

    def _settle(self, rc, plan):
        """Songs that need no work are reported at once (the progress bar counts them as done)."""
        track = rc.track
        if plan.kind == "ok":
            info = plan.info
            self._finish(rc, Result("skipped", track.title, track.artist, kbps=info.kbps, src_kbps=info.src_kbps,
                                    seconds=info.seconds, cover=info.cover, low_quality=plan.low_quality,
                                    no_cover=plan.no_cover, source=info.source, note=self._flag_note(plan),
                                    lyrics=info.lyrics))
            rc.release()
        elif plan.kind == "have":
            f = plan.info
            self._finish(rc, Result("skipped", track.title, track.artist, kbps=f.kbps, src_kbps=f.src_kbps,
                                    seconds=f.seconds, note=plan.note, path=f.path))
            rc.release()
        elif plan.kind == "dup":
            self._finish(rc, Result("skipped", track.title, track.artist, note="Listed twice"))

    # ---- the same song, already in the folder?

    def _second_look(self, rc, plan):
        """Beyond 'is there a file with exactly this name?': is the song in the folder under another name or in another
        format, and would the quality chosen now be a worthwhile improvement on what is there?"""
        if plan.kind == "new" and plan.reasons == ("missing",):
            found = self._copy_elsewhere(rc)
            if found is None:
                return plan
            have = Plan("have", found, note="Already in your library · " + quality.describe(found))
            if self._worth(found, found.rel):
                return Plan("upgrade", found, ("better",), old_path=found.path, old_rel=found.rel, fallback=have)
            return have
        if plan.kind in ("ok", "extras") and plan.info is not None and self._worth(plan.info, rc.rel):
            return Plan("upgrade", plan.info, ("better",), low_quality=plan.low_quality, no_cover=plan.no_cover,
                        old_path=rc.final, old_rel=rc.rel, fallback=plan)
        return plan

    def _copy_elsewhere(self, rc):
        """The best file in the folder that is this song, other than the one we would write (None if there is none)."""
        if self._index is None:
            self._say("Checking your library…")
            self._index = FolderIndex(self.outdir, self.lib, self.stop, self._say)
        for f in self._index.find(rc.track.artist, rc.track.title):
            if os.path.normcase(f.path) != os.path.normcase(rc.final) and self.fits(rc, f.seconds, scan=True):
                return f
        return None

    def _worth(self, info, rel):
        """Is `info` clearly below the quality chosen now — and not something we already tried to improve?"""
        if self.st.upgrade == "keep" or not quality.worth_upgrading(info, self.target_q):
            return False
        rec = self.lib.track(rel)
        return not (rec.get("target_tried", 0) >= self.target_q and time.time() - rec.get("target_t", 0) < UPGRADE_RETRY)

    def _decide_upgrades(self, plans):
        """Ask once, before anything is downloaded: replace the lower-quality songs we already have?"""
        ups = [(rc, p) for rc, p in plans if p.kind == "upgrade"]
        if not ups:
            return plans
        if self.st.upgrade == "ask":
            approve = bool(self.ask(self._upgrade_summary(ups))) if self.ask else False
        else:
            approve = self.st.upgrade == "replace"
        if approve:
            return plans
        out = []
        for rc, p in plans:
            if p.kind == "upgrade":                                   # keep what is there
                p = p.fallback
                self._settle(rc, p)
            out.append((rc, p))
        return out

    def _upgrade_summary(self, ups):
        have = {}
        for _rc, p in ups:
            d = quality.describe(p.info)
            have[d] = have.get(d, 0) + 1
        return {"count": len(ups), "target": self.st.quality_name(),
                "have": [d for d, _n in sorted(have.items(), key=lambda kv: -kv[1])[:2]],
                "examples": [rc.track.title for rc, _p in ups[:3]]}

    @staticmethod
    def _flag_note(plan):
        if plan.low_quality and plan.no_cover:
            return "Low quality · no artwork"
        if plan.low_quality:
            return "Best available · low quality"
        return "No artwork found" if plan.no_cover else ""

    def _classify(self, rc):
        st = self.st
        if not os.path.exists(rc.final):
            return Plan("new", reasons=("missing",))
        rec = self.lib.track(rc.rel)
        try:
            fs = os.stat(rc.final)
        except OSError:
            return Plan("new", reasons=("missing",))
        retry_cover = rec.get("nocover") and time.time() - rec.get("cover_tried", 0) > RETRY_AFTER
        # a record from before details were tracked (det missing), or of a file that lacked some: look at the file again
        retry_details = st.verify and st.use_tags() and (
            rec.get("det") is None or (rec["det"] is False and time.time() - rec.get("details_tried", 0) > RETRY_AFTER))
        if rec.get("status") == "ok" and rec.get("crit") == self.crit and rec.get("size") == fs.st_size \
                and rec.get("mtime") == int(fs.st_mtime) and not retry_cover and not retry_details:
            ext = os.path.splitext(rc.final)[1].lower()
            info = _Info(seconds=rec.get("seconds", 0), kbps=rec.get("kbps", 0), src_kbps=rec.get("src_kbps", 0),
                         cover=rec.get("cover", False), source=rec.get("source", ""), size=fs.st_size,
                         lyrics=rec.get("lyrics", False), ext=ext,
                         lossless=quality.is_lossless_file(ext, rec.get("kbps", 0), rec.get("lossless", False)))
            return Plan("ok", info, low_quality=bool(rec.get("low")), no_cover=bool(rec.get("nocover")))
        info = read_info(rc.final)
        if info is None or info.seconds < 5:
            return Plan("redo", info, ("unreadable",))
        if not st.verify:
            return self._ok_plan(rc, info, False, False)
        reasons = []
        if not self.fits(rc, info.seconds + float(rec.get("removed", 0) or 0), scan=True):     # (trimmed silence counts)
            reasons.append("length")
        low = self.is_low(info.quality_kbps)
        if low and st.replace_low and rec.get("upgrade_tried", 0) < st.min_kbps:
            reasons.append("quality")
        no_cover = st.use_art() and not info.cover
        if no_cover and time.time() - rec.get("cover_tried", 0) > RETRY_AFTER:
            reasons.append("cover")
        if st.use_lyrics() and not info.lyrics and time.time() - rec.get("lyrics_tried", 0) > RETRY_AFTER:
            reasons.append("lyrics")
        if st.use_tags() and not self._complete(info) and time.time() - rec.get("details_tried", 0) > RETRY_AFTER:
            reasons.append("details")
        if {"length", "unreadable"} & set(reasons) or "quality" in reasons:
            return Plan("redo", info, tuple(reasons), low_quality=low, no_cover=no_cover)
        if reasons:
            return Plan("extras", info, tuple(reasons), low_quality=low, no_cover=no_cover)
        return self._ok_plan(rc, info, low, no_cover)

    def _ok_plan(self, rc, info, low, no_cover):
        self._record(rc, info, status="ok", low=low, nocover=no_cover)
        return Plan("ok", info, low_quality=low, no_cover=no_cover)

    @staticmethod
    def _complete(info):
        """Does the file say everything a player groups and sorts by? (album, album artist, track n of m, date, genre)"""
        return bool(info.album and info.album_artist and info.track_no and info.track_total
                    and (info.date or info.year) and info.genre)

    def _record(self, rc, info, status="ok", **extra):
        rec = self.lib.track(rc.rel)
        rec.update({"status": status, "size": info.size, "mtime": info.mtime, "seconds": round(info.seconds, 1),
                    "kbps": info.kbps, "src_kbps": info.src_kbps, "cover": info.cover, "source": info.source,
                    "lyrics": info.lyrics, "lossless": bool(getattr(info, "lossless", False)), "crit": self.crit,
                    "t": int(time.time())})
        if hasattr(info, "album_artist"):                          # (not from a stand-in made from this record)
            rec["det"] = self._complete(info)
        rec.update(extra)
        self.lib.set_track(rc.rel, rec)

    # ---------------------------------------------------------------- phase 3: the work for one song

    def _work(self, rc, plan):
        res = Result("error", rc.track.title, rc.track.artist)
        lock = self._name_locks.setdefault(rc.rel.lower(), threading.Lock())
        tmpdir, stalls = "", 0
        try:
            while True:
                need = self._need(rc, plan)
                try:
                    if not self.disk.enough(need):
                        self._stage(rc, "Waiting for disk space")
                    self.disk.ensure(need)                       # no room yet: the whole run waits here, nothing fails
                    tmpdir = tempfile.mkdtemp(dir=self.tmp_root)
                    with lock:
                        res = self._fix_extras(rc, plan) if plan.kind == "extras" else self._acquire(rc, plan, tmpdir)
                    break
                except Stopped:
                    raise
                except Exception as e:
                    if not disk.is_disk_full(e):
                        raise
                    shutil.rmtree(tmpdir, ignore_errors=True)    # what this song had written goes: that frees room too
                    self._stage(rc, "Waiting for disk space")
                    if self.disk.wait(need):
                        stalls = 0
                    else:                                        # 'disk full' although the drive shows room: not ours to fix
                        stalls += 1
                        if stalls >= 3:
                            raise EngineError("The disk reported an error: " + str(e)[:70]) from None
        except Stopped:
            res = Result("stopped", rc.track.title, rc.track.artist)
        except Exception as e:                                   # never let one song take the run down
            log.exception("error on %s", rc.track.label())
            res = Result("error", rc.track.title, rc.track.artist, note=str(e)[:100] or e.__class__.__name__)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
        return res

    def _need(self, rc, plan=None):
        """About how many bytes one song can occupy at its peak: the download plus the encoded copy."""
        if plan is not None and plan.kind == "extras":
            return 32 * 2 ** 20
        secs = min(1800.0, rc.refs[0] if rc.refs else float(rc.track.duration or 240.0))
        src = quality.LOSSLESS if self.fmt.lossless else 320
        return int(secs * 125 * (src + size_kbps_of(rc.fmt or self.fmt)))

    def _missing_details(self, rc, info):
        """A Tagset holding the song's details with the file's blanks filled from the catalogue, or None when there is nothing
        to add. What the file already says wins over the list's and the catalogue's (it is never contradicted)."""
        t = rc.track
        for f in ("album", "album_artist", "genre", "isrc", "year", "track_no", "track_total", "disc_no", "disc_total", "explicit"):
            if getattr(info, f):
                setattr(t, f, getattr(info, f))
        t.date = info.date if len(info.date or "") == 10 else ""         # a bare year may be completed by the catalogue
        t.compilation = t.compilation or bool(info.compilation)
        t.record_label = info.label or t.record_label
        cat = self._cat(rc)
        if cat.get("failed"):
            cat = self._cat(rc, patient=True)
        self._merge_details(t, cat)
        have = {"album": info.album, "album_artist": info.album_artist, "genre": info.genre, "isrc": info.isrc,
                "year": info.year, "date": info.date, "track_no": info.track_no, "track_total": info.track_total,
                "disc_no": info.disc_no, "disc_total": info.disc_total, "explicit": info.explicit,
                "compilation": info.compilation, "record_label": info.label}
        if all(not getattr(t, k) or getattr(t, k) == v for k, v in have.items()):
            return None
        return Tagset(album=t.album, album_artist=t.album_artist, year=str(t.year or ""), date=t.date, genre=t.genre,
                      track_no=t.track_no, track_total=t.track_total, disc_no=t.disc_no, disc_total=t.disc_total,
                      isrc=t.isrc, explicit=t.explicit, compilation=t.compilation, label=t.record_label)

    def _fix_extras(self, rc, plan):
        """Add artwork, missing details (album artist, track n of m, date…) and/or lyrics to a file that is otherwise fine.
        Nothing the file already says is changed: the details are only the blanks, filled from the catalogue."""
        t, info, reasons = rc.track, plan.info, set(plan.reasons)
        rec = self.lib.track(rc.rel)
        cover, lyr, added, ts = None, None, [], Tagset()
        if "details" in reasons:
            self._stage(rc, "Adding details")
            ts = self._missing_details(rc, info)
            rec["details_tried"] = int(time.time())
            if ts is not None:
                added.append("Details")
            ts = ts or Tagset()
        if "cover" in reasons:
            self._stage(rc, "Finding artwork")
            cover = self._cover_for(rc)
            rec["cover_tried"] = int(time.time())
            if cover:
                added.append("Artwork")
        if "lyrics" in reasons:
            self._stage(rc, "Finding lyrics")
            lyr = lyrics_mod.fetch(t.title, t.artist, t.album, info.seconds, self.stop)
            rec["lyrics_tried"] = int(time.time())
            if lyr and lyr.plain:
                ts.lyrics = lyr.plain
                added.append("Lyrics")
        if added:
            write_tags(rc.final, ts, cover[0] if cover else None, merge=True)
        if lyr and lyr.synced and self.st.use_lrc():
            self._write_lrc(rc, lyr.synced)
        self.lib.set_track(rc.rel, rec)
        info2 = read_info(rc.final) or info
        self._record(rc, info2, low=plan.low_quality, nocover=self.st.use_art() and not info2.cover,
                     **{k: rec[k] for k in ("cover_tried", "lyrics_tried", "details_tried") if k in rec})
        if not added:
            if reasons <= {"details"}:                                 # nothing known to add: the file is as good as it gets
                return Result("skipped", t.title, t.artist, kbps=info2.kbps, src_kbps=info2.src_kbps, seconds=info2.seconds,
                              cover=info2.cover, low_quality=plan.low_quality, no_cover=plan.no_cover, source=info2.source,
                              note=self._flag_note(plan), lyrics=info2.lyrics)
            note = "No artwork found" if "cover" in reasons and not cover else "No lyrics found"
            return Result("kept", t.title, t.artist, note=note, no_cover=plan.no_cover, kbps=info2.kbps,
                          src_kbps=info2.src_kbps, seconds=info2.seconds, cover=info2.cover, low_quality=plan.low_quality,
                          source=info2.source, lyrics=info2.lyrics)
        return Result("fixed", t.title, t.artist, note=" + ".join(added) + " added", kbps=info2.kbps,
                      src_kbps=info2.src_kbps, seconds=info2.seconds, cover=info2.cover, thumb=cover[1] if cover else None,
                      source=info2.source, low_quality=plan.low_quality, lyrics=info2.lyrics)

    def _acquire(self, rc, plan, tmpdir):
        st, t = self.st, rc.track
        upgrade = plan.kind == "upgrade"
        old = plan.info if plan.kind in ("redo", "upgrade") else None
        reasons = set(plan.reasons)
        if old and "length" in reasons and not (reasons - {"length"}):
            # MusicBrainz alone says the file is the wrong length; ask Deezer/iTunes before throwing it away
            self._stage(rc, "Double-checking length")
            if self.fits(rc, old.seconds, deep=True):
                low = self.is_low(old.quality_kbps)
                no_cover = bool(st.use_art()) and not old.cover
                self._record(rc, old, low=low, nocover=no_cover)
                return Result("skipped", t.title, t.artist, kbps=old.kbps, src_kbps=old.src_kbps,
                              seconds=old.seconds, cover=old.cover, low_quality=low, no_cover=no_cover,
                              source=old.source, lyrics=old.lyrics)
        need_better = bool(old) and (upgrade or not ({"length", "unreadable"} & reasons))    # only an upgrade attempt
        old_q = quality.delivered(old) if old else 0

        chosen, too_long, failed, tried = None, 0, 0, set()
        self._stage(rc, "Searching")
        for deep in (False, True):
            if deep and (chosen or not self._ext_refs(rc)):
                break
            for cand in self._pool(rc, plan, deep):               # best first: when the best fails, the next best is tried
                self._check()
                if cand["id"] in tried:
                    continue
                tried.add(cand["id"])
                fmt = self._fmt_for(cand, upgrade)
                if need_better:
                    if cand["source"] == "youtube" and not cand.get("probed") and not cand["kbps"] and not cand.get("direct"):
                        sources.probe(cand, self.stop, rc.raw.setdefault("probe", {}))       # its quality decides if it is worth it
                    if cand.get("dead") or not quality.is_better(min(sources.est_kbps(cand), quality.target(fmt)), old_q):
                        continue                                  # not worth replacing what we have
                out = self._try(rc, cand, tmpdir, fmt)
                if out == "length":
                    too_long += 1
                    continue
                if out is None:
                    failed += 1
                    continue
                path, info, q, fmt = out
                if need_better and not quality.is_better(min(q, quality.target(fmt)), old_q):
                    os.remove(path)
                    continue
                chosen = (path, info, q, cand, fmt)
                break
            if chosen:
                break
        if chosen is None:
            res = self._no_luck(rc, old, plan, need_better, too_long, failed)
            if res.status in ("no-file", "bad-length"):
                return self._look_around(rc, plan, old, tmpdir, res)
            return res
        return self._install(rc, plan, old, *chosen)

    def _look_around(self, rc, plan, old, tmpdir, res):
        """Nothing matched exactly: search around the song (see closematch). With 'auto', a close match that differs only
        in length is saved straight away; otherwise the options travel with the result for the user to choose from."""
        if self.close_mode == "skip" or rc.track.direct or not self.st.youtube:
            return res
        self._stage(rc, "Looking for close matches")
        try:
            refs = list(rc.refs) + list(self._ext_refs(rc) or [])
            options = closematch.around(rc, self.stop, refs, self.tol)
        except Stopped:
            raise
        except Exception as e:                                    # never let the extra search turn a miss into an error
            log.info("close-match search failed for %s: %s", rc.track.label(), e)
            return res
        if not options:
            return res
        if self.close_mode == "auto":
            for opt in (o for o in options if o["safe"]):
                self._check()
                fmt = self._fmt_for(opt, False)
                out = self._try(rc, opt, tmpdir, fmt, lenient=True)
                if isinstance(out, tuple):
                    saved = self._install(rc, plan, old, *out[:3], opt, out[3])
                    saved.note = " · ".join(x for x in (saved.note, "Close match: " + ", ".join(opt["why"])) if x)
                    with self._lock:
                        self.close_taken += 1
                    return saved
        n = len(options)
        res.close = options
        res.note = f"{res.note} · {n} close option{'' if n == 1 else 's'}"
        return res

    def _fmt_for(self, cand, upgrade):
        """The format to write a candidate in, judged from what the search says about it (_try judges again from the
        downloaded file itself).

        Optimized mode (`capped`): the format chosen, but never more than the source can fill — a lossy source is not
        turned into a bigger file, and a lossless choice falls back to MP3/AAC at the bitrate the source has.
        Advanced mode writes what was asked for, except that upgrading towards lossless when the best version that can
        be found is lossy would only make a bigger copy of the same sound, so the 'next best' (MP3 at the nearest
        bitrate) is used instead."""
        if self.capped:
            return quality.fit(self.fmt, bool(cand.get("lossless")), 0 if cand.get("lossless") else cand.get("kbps", 0))
        if upgrade and self.fmt.lossless and not cand.get("lossless"):
            tier = quality.lossy_tier(sources.est_kbps(cand), FORMATS["mp3"]["bitrates"])
            return OutFmt("mp3", kbps=tier, bit_depth=16)
        return self.fmt

    def _no_luck(self, rc, old, plan, need_better, too_long, failed):
        t = rc.track
        if plan.kind == "upgrade":                                # nothing better than what the user already has
            rel = plan.old_rel or rc.rel
            if not failed:                                        # (a failed download says nothing about availability)
                rec = self.lib.track(rel)
                rec["target_tried"], rec["target_t"] = self.target_q, int(time.time())
                self.lib.set_track(rel, rec)
            return Result("kept", t.title, t.artist, kbps=old.kbps, src_kbps=old.src_kbps, seconds=old.seconds,
                          cover=getattr(old, "cover", False), source=getattr(old, "source", ""), path=plan.old_path,
                          lyrics=getattr(old, "lyrics", False), low_quality=self.is_low(quality.delivered(old)),
                          note="Couldn’t download a better version right now" if failed
                          else "Already the best version available")
        if need_better and old:                                   # an upgrade attempt found nothing better
            rec = self.lib.track(rc.rel)
            rec["upgrade_tried"] = self.st.min_kbps
            self.lib.set_track(rc.rel, rec)
            self._record(rc, old, low=True, nocover=plan.no_cover, upgrade_tried=self.st.min_kbps)
            return Result("kept", t.title, t.artist, note="Best available · low quality", kbps=old.kbps,
                          src_kbps=old.src_kbps, seconds=old.seconds, cover=old.cover, low_quality=True,
                          no_cover=plan.no_cover, source=old.source, lyrics=old.lyrics)
        if too_long and not failed:
            return Result("bad-length", t.title, t.artist, note=f"{too_long} found, none the right length")
        if failed:
            return Result("dl-fail", t.title, t.artist, note="Download failed")
        return Result("no-file", t.title, t.artist, note="No matching source found")

    def _install(self, rc, plan, old, path, info, q, cand, fmt):
        st, t = self.st, rc.track
        if fmt is not self.fmt:                                    # the 'next best' format: the file name follows it
            rc.fmt = fmt
            self._set_path(rc)
        self._stage(rc, "Adding details")
        if st.use_tags() and self._wants_details(t):
            cat = self._cat(rc)
            if cat.get("failed"):                                  # rate-limited or offline a moment ago: once more
                cat = self._cat(rc, patient=True)
            self._merge_details(t, cat)
            self._set_path(rc)                                     # the template may use album/year/track
        self._stage(rc, "Adding artwork")
        cover = self._cover_for(rc, cand)
        lyr = None
        if st.use_lyrics():
            self._stage(rc, "Finding lyrics")
            lyr = lyrics_mod.fetch(t.title, t.artist, t.album, info.seconds, self.stop)
        if st.use_tags():
            ts = Tagset(title=t.title, artist=t.artist, album=t.album, year=str(t.year or ""), genre=t.genre,
                        track_no=t.track_no, disc_no=t.disc_no, mbid=t.mbid, isrc=t.isrc,
                        lyrics=lyr.plain if lyr else "", source=cand["source"], src_kbps=q,
                        album_artist=t.album_artist, track_total=t.track_total, disc_total=t.disc_total, date=t.date,
                        explicit=self._explicit(rc, cand), compilation=t.compilation, label=t.record_label)
            write_tags(path, ts, cover[0] if cover else None)
        elif cover:
            write_tags(path, Tagset(), cover[0], merge=True)
        try:
            os.makedirs(os.path.dirname(rc.final), exist_ok=True)
            os.replace(path, rc.final)
        except PermissionError:
            return Result("error", t.title, t.artist, note="File is in use — close your player and re-run")
        if lyr and lyr.synced and st.use_lrc():
            self._write_lrc(rc, lyr.synced)
        info2 = read_info(rc.final) or info
        low = self.is_low(q)
        fx = rc.raw.pop("fx", None)
        self._record(rc, info2, low=low, nocover=cover is None and bool(st.use_art()), details_tried=int(time.time()),
                     removed=round(fx.removed, 1) if fx is not None else 0,
                     **({"cover_tried": int(time.time())} if cover is None else {}))
        replaced = self._remove_replaced(rc, plan) if plan.kind == "upgrade" else ""
        if plan.kind == "upgrade":
            note = f"Replaced {quality.describe(old)}" + (
                " · no lossless version found" if self.fmt.lossless and not fmt.lossless else "")
            note += f" · {replaced}" if replaced else ""
        else:
            note = "Low quality source" if low else ("No artwork found" if cover is None else "")
            if self.fmt.lossless and not fmt.lossless:                   # the source could not fill a lossless file
                with self._lock:
                    self.no_lossless += 1
                note = f"{note} · " if note else ""
                note += f"No lossless source · {fmt.label}"
        matched = rc.raw.pop("match_note", "")
        if matched and not (self.fmt.lossless and not fmt.lossless):
            with self._lock:
                self.matched += 1
            note = f"{note} · {matched}" if note else matched
        finished = rc.raw.pop("fx_note", "")
        if finished:
            note = f"{note} · {finished}" if note else finished
        version = rc.raw.pop("version_note", "")
        if version:
            note = f"{note} · {version}" if note else version
        return Result("upgraded" if old else "ok", t.title, t.artist, note=note,
                      kbps=info2.kbps, src_kbps=q, seconds=info2.seconds, cover=cover is not None,
                      thumb=cover[1] if cover else None, source=cand["source"], low_quality=low,
                      no_cover=cover is None and bool(st.use_art()), size=info2.size, lyrics=bool(lyr and lyr.plain))

    def _explicit(self, rc, cand):
        """The explicit tag for this file: 1 explicit, 2 clean, 0 nothing to say. It follows what the file most likely
        is — the upload's own title first, then what the catalogue says about the song — never the setting alone, so an
        explicit file is never labelled clean (or the other way round)."""
        said = sources.version_of(rc, cand.get("title", ""))
        if said == "clean":
            return 2
        if said == "explicit":
            return 1
        known = int(rc.track.explicit or 0)
        if known == 1 and rc.clean:
            rc.raw["version_note"] = "No clean version found"
        return known

    def _remove_replaced(self, rc, plan):
        """The better version is saved and verified; take the old copy away (unless the new file *is* the old path).
        Returns a short note when the old copy could not be removed."""
        old = plan.old_path
        if not old or os.path.normcase(os.path.abspath(old)) == os.path.normcase(os.path.abspath(rc.final)):
            return ""
        try:
            os.remove(old)
        except FileNotFoundError:
            pass
        except OSError as e:
            log.info("could not remove replaced file %s: %s", old, e)
            return "old copy still there"
        self.lib.forget_track(plan.old_rel)
        return ""

    def _write_lrc(self, rc, synced):
        try:
            with open(os.path.splitext(rc.final)[0] + ".lrc", "w", encoding="utf-8") as fh:
                fh.write(synced)
        except OSError as e:
            log.info("could not write .lrc: %s", e)

    # ---------------------------------------------------------------- candidates: every acceptable source, best first

    def _pool(self, rc, plan, deep):
        """What to try for this song, in order. A link that names its recording comes first; the search behind it is only
        made if that fails. A scout may already have done the search."""
        t = rc.track
        if t.direct and not deep:
            yield sources.direct_candidate(t)
        cands = None
        if not deep:
            with rc.lock:
                if not rc.scouted and not t.direct:
                    self._scout_search(rc, plan)
                cands, rc.cands = rc.cands, None
        if cands is None:
            cands = self._candidates(rc, deep)
        yield from cands[:6]

    def _scout_search(self, rc, plan):
        """Catalogue details and the search for a song, ahead of the worker that will download it (a worker that
        gets there first does it itself). Failures are left for the worker, which reports them properly."""
        with rc.lock:
            if rc.scouted:
                return
            rc.scouted = True
            try:
                self._cat(rc)
                rc.cands = self._candidates(rc, False)
            except Stopped:
                rc.scouted = False
                raise
            except Exception as e:
                log.info("search ahead failed for %s: %s", rc.track.label(), e)

    def _scout(self, rc, plan):
        """Called by a scout thread. Songs that need no download, or whose recording a link names, are skipped."""
        if plan.kind == "extras" or rc.track.direct or (plan.kind == "redo" and set(plan.reasons) == {"length"}):
            return
        self._scout_search(rc, plan)

    def _candidates(self, rc, deep=False):
        """Sources whose title, artist and length fit this song. Best match first; among equally good matches the
        better audio first (a 320 kbps file is no help for a 192 kbps target, so quality counts only up to what the
        output can hold).

        YouTube is searched first and usually decides it. archive.org — slower, and worth it only for sound better than
        a video's — is added when the target asks for more than a video holds (or YouTube is off, or nothing
        convincing turned up); then both are searched at the same time."""
        with rc.lock:
            use_yt = bool(self.st.youtube)
            want_arch = not use_yt or self.target_q > YT_ENOUGH
            bg = None
            if use_yt and want_arch and "archive" not in rc.raw:
                bg = threading.Thread(target=self._archive_ahead, args=(rc, deep), name="archive", daemon=True)
                bg.start()
            yt = sources.youtube_candidates(rc, self.stop, self.fits, deep, self.tol) if use_yt else []
            if bg is not None:
                bg.join()
            found = list(yt)
            if want_arch or not any(sources.convincing(rc, c["score"], c["seconds"], deep) for c in yt):
                found = list(sources.archive_candidates(rc, self.stop, self.fits, deep)) + found
            pool, seen = [], set()
            for c in found:
                if c["id"] not in seen:
                    seen.add(c["id"])
                    pool.append(c)
            probes = rc.raw.setdefault("probe", {})
            tubes = [c for c in pool if c["source"] == "youtube"]
            if tubes and not any(c.get("probed") for c in tubes):      # one look at what the site offers; a dead link is skipped
                for c in tubes[:3]:
                    sources.probe(c, self.stop, probes)
                    if not c.get("dead"):
                        break
            pool = [c for c in pool if not c.get("dead")]
            known = [c["kbps"] for c in pool if c["source"] == "youtube" and c.get("probed") and c["kbps"]]
            for c in pool:                                             # the same site offers the same audio for every video
                if known and c["source"] == "youtube" and not c.get("probed"):
                    c["kbps"] = max(known)
            return self._rank(pool)

    def _archive_ahead(self, rc, deep):
        """Run the archive.org search beside the YouTube one. It needs nothing from `rc` but what it fills in itself
        (the length rule is not applied yet), so it cannot wait for a thread that is waiting for it."""
        try:
            sources.archive_candidates(rc, self.stop, lambda *_: True, deep)
        except Stopped:
            pass
        except Exception as e:
            log.info("archive search failed for %s: %s", rc.track.label(), e)

    def _rank(self, pool):
        """Matches first (those within BAND of the best are equals), then audio quality up to what the target can use:
        sound beyond the output format's own quality is of no use, so 320 kbps does not beat 200 for a 192 kbps file."""
        if not pool:
            return pool
        cap = self.target_q
        top = max(c["score"] for c in pool)

        def key(c):
            near = c["score"] >= top - sources.BAND
            return (0 if near else 1, -(min(sources.est_kbps(c), cap) // 16) if near else 0, -c["score"])
        return sorted(pool, key=key)

    # ---------------------------------------------------------------- fetching, encoding and verifying one candidate

    def _try(self, rc, cand, tmpdir, fmt=None, lenient=False):
        """Download + encode + verify one candidate. Returns (output path, FileInfo, quality, format written) | 'length' |
        None. In Optimized mode the source is sampled first and the format follows what it really is (quality.fit).
        `lenient`: a close match is being tried, so the length rule is only that the file is as long as it was listed."""
        fmt = fmt or self.fmt
        retries = max(0, int(self.st.effective_retries()))
        got = None
        for attempt in range(retries + 1):
            self._check()
            self._stage(rc, "Downloading" + (f" (try {attempt + 1})" if attempt else ""))
            try:
                got = sources.fetch(cand, tmpdir, self.stop)
                break
            except Stopped:
                raise
            except Exception as e:
                if disk.is_disk_full(e):
                    raise DiskFull(str(e)) from None
                log.info("download failed (%s): %s", cand.get("title"), e)
                if sources.is_permanent(e):                      # gone, private, forbidden: asking again cannot help
                    cand["dead"] = True
                    break
                T.error()                                        # a flaky failure: the governor counts these
                if self.stop.wait(1.5 * (attempt + 1)):
                    raise Stopped() from None
        if not got:
            return None
        src, est_kbps, hint = got
        rc.src_cover = None                                              # a picture inside the download: a late fallback
        if self.st.use_art():
            inside = extract_cover(src)
            rc.src_cover = inside if inside and len(inside) >= 4000 else None
        if cand["source"] != "archive.org":
            cand["kbps"] = est_kbps
        family = transcode.codec_family(os.path.splitext(src)[1], hint)
        lossless_src = cand["lossless"] or family in transcode.LOSSLESS_CODECS
        honest_lossless = lossless_src and not cand.get("ripped")      # a WAV made from a video's sound is not lossless
        rate, bits = transcode.probe_pcm(src, family)                  # what the file says it holds
        if lossless_src and not bits:
            bits = transcode.bit_depth_of(src)
        file_kbps = est_kbps                                           # what the container carries, whatever it holds
        rc.raw["match_note"] = ""
        if self.capped:
            smp = None
            if honest_lossless or (not lossless_src and est_kbps >= sampler.LISTEN_ABOVE):
                self._stage(rc, "Checking source quality")
                with self._cpu():
                    smp = sampler.sample(src, self.stop)               # a FLAC made from a 128 kbps MP3 is a big MP3
            if smp is not None and not smp.lossless:
                if honest_lossless:
                    log.info("%s: lossless file with a %d Hz edge — really ~%d kbps", cand.get("title"), smp.cutoff, smp.q)
                    honest_lossless, cand["lossless"], cand["kbps"], est_kbps = False, False, smp.q, smp.q
                elif smp.q * (1.4 if family == "mp3" else 1.8) < est_kbps:     # a "320 kbps" file with a 128 kbps edge
                    log.info("%s: claims ~%d kbps but has a %d Hz edge — really ~%d kbps", cand.get("title"), est_kbps,
                             smp.cutoff, smp.q)
                    cand["kbps"], est_kbps = smp.q, smp.q
            held_rate = held_bits = 0
            if honest_lossless and (rate >= 88200 or bits > 16):       # hi-res claims are listened to a second time
                with self._cpu():
                    held_rate, held_bits = sampler.pcm_truth(src, rate, bits, self.stop)
            fitted = quality.fit(self.fmt, honest_lossless, sources.LOSSLESS_KBPS if honest_lossless else cand["kbps"])
            fmt = quality.fit_pcm(fitted, rate, bits if honest_lossless else 0, held_rate, held_bits)
            rc.raw["match_note"] = quality.match_note(fitted, fmt, self.fmt, rate, bits, held_rate, held_bits)
        raw_kbps = int(file_kbps / sources.CODEC_WEIGHT.get(family, 1.0)) if file_kbps and not lossless_src else 0
        action = transcode.plan(family, raw_kbps, fmt, (bits or 16) if lossless_src else 16)
        fx, af = self._finishing(rc, src), ""
        if fx is not None and fx.active:
            action = "encode"                                          # a straight copy can't be made louder or trimmed
            af = fx.chain(dither=transcode.writes_16bit(fmt, bits or 16, lossless_src))
        self._stage(rc, "Encoding" if action == "encode" else "Saving")
        dst = os.path.join(tmpdir, f"out{rc.index}_{abs(hash(cand['id'])) % 10**6}{fmt.ext}")
        try:
            with self._cpu():                                          # only so many encodes at once, the extra ones gently
                transcode.encode(src, dst, fmt, action, self.budget.threads(), self.stop, lossless_src, af)
        except Stopped:
            raise
        except Exception as e:
            if disk.is_disk_full(e):
                raise DiskFull(str(e)) from None
            log.info("encode failed (%s): %s", cand.get("title"), e)
            return None
        finally:
            try:
                os.remove(src)
            except OSError:
                pass
        info = read_info(dst)
        if info is None or info.seconds < 5:
            return None
        full = info.seconds + (fx.removed if fx is not None else 0.0)    # trimmed silence still counts toward the song's length
        listed = float(cand.get("seconds") or 0)
        near_listing = abs(full - listed) <= max(5.0, 0.1 * listed)
        if not ((lenient and near_listing) or self.fits(rc, full) or self.fits(rc, full, deep=True)):
            os.remove(dst)
            return "length"
        q = sources.LOSSLESS_KBPS if honest_lossless else (cand["kbps"] or info.kbps)
        return dst, info, int(q), fmt

    def _finishing(self, rc, src):
        """Listen to the downloaded file and work out its volume / trim / EQ chain (an Fx, or None).
        Whatever happens here, the song is still saved: an analysis that fails just means it is saved untouched."""
        rc.raw["fx"] = None
        rc.raw["fx_note"] = ""
        if not self.spec:
            return None
        self._stage(rc, "Finishing the sound")
        try:
            with self._cpu():
                fx = process.analyze(src, self.spec, self.stop)
        except Stopped:
            raise
        except Exception as e:
            log.info("audio finishing skipped (%s): %s", rc.track.title, e)
            fx = None
        if fx is None:
            rc.raw["fx_note"] = "Sound options skipped"
            return None
        rc.raw["fx"] = fx
        rc.raw["fx_note"] = fx.note()
        return fx

    # ---------------------------------------------------------------- artwork
    # service art -> the catalogue's picture of the fitting release -> Cover Art Archive -> the picture inside the download
    # -> the video's thumbnail (black bars trimmed). The first picture that is big enough wins; a small one is kept only
    # if nothing better turns up. Pictures are made for the player: 1400 px for .m4a (Apple Music), 1000 px otherwise.

    def _cover_for(self, rc, cand=None):
        if not self.st.use_art():
            return None
        if rc.cover_done:
            return rc.cover
        rc.cover_done = True
        side = cover_side((rc.fmt or self.fmt).ext)
        best, tried = None, []
        for attempt in (0, 1):
            transient = False
            for source, url, video in self._cover_urls(rc, cand, side):
                self._check()
                key = (url, side, video)
                try:
                    # every song of an album points at the same picture: it is fetched and prepared once, whoever asks first
                    got = self._covers.get(key, lambda: self._make_cover(source, url, video, side, rc), self.stop) \
                        if url else self._make_cover(source, url, video, side, rc)
                except _Transient as e:
                    self._covers.forget(key)                          # network trouble now says nothing about the picture
                    transient = True
                    tried.append(f"{source}: {e}")
                    continue
                if got is None:
                    tried.append(source)
                    continue
                if best is None or got.source_side > best[0].source_side:
                    best = (got, source)
                if got.source_side >= GOOD_SIDE:
                    break
            cat_down = bool((rc.cat or {}).get("failed"))
            if best or attempt or not (transient or cat_down):
                break
            if self.stop.wait(RECHECK):                               # rate-limited or offline a moment ago: once more
                raise Stopped()
        if best:
            rc.cover = (best[0].jpeg, best[0].thumb, best[1])
        else:
            log.info("no artwork for %s — tried %s", rc.track.label(), ", ".join(tried) or "nothing (no source offered a picture)")
        return rc.cover

    def _make_cover(self, source, url, video, side, rc=None):
        """One picture, made ready: a Prepared, None when this picture is unusable, _Transient when asking failed."""
        if not url:                                                   # the picture inside the downloaded file
            return prepare(rc.src_cover, square=False, side=side) if rc is not None and rc.src_cover else None
        try:
            r = netio.request("GET", url, limiter=netio.CAA_LIMIT if source == "caa" else netio.WEB_LIMIT,
                              timeout=(8, 30), retries=2, stop=self.stop)
        except EngineError as e:
            raise _Transient(str(e)[:80]) from None
        if r is None:
            return None
        try:
            return prepare(r.content, square=video, side=side) if r.status_code == 200 else None
        finally:
            r.close()

    def _cover_urls(self, rc, cand, side):
        t = rc.track
        if t.artwork:
            yield "service", sized(t.artwork, side), False
        for url in catalog.covers_for(t, self._cat(rc))[:3]:
            yield "catalog", sized(url, side), False
        if t.mbid:
            for url in catalog.cover_art_archive_urls(self.lib, t.mbid):
                yield "caa", url, False
        if rc.src_cover:
            yield "download", "", False
        vid = (cand or {}).get("vid") if cand and cand.get("source") == "youtube" else ""
        if vid:                       # a 'Topic' video is the square album cover in the middle of a black frame; trimmed
            yield "youtube", f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg", True
            yield "youtube", f"https://i.ytimg.com/vi/{vid}/sddefault.jpg", True
            yield "youtube", f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg", True
        elif t.extra.get("thumb"):
            yield "thumb", t.extra["thumb"], True


class _Scouts:
    """Looks a few songs ahead of the workers. Searching is mostly waiting for the network; downloading and encoding
    are bandwidth and CPU. While the workers do the second, a couple of threads already do the first for the songs that
    come next, so the two overlap. A song is only scouted once (a worker that gets there first does it itself and the
    scout moves on), and the scouts never run more than twice the songs-at-once ahead (the governor changes that
    number during a long run)."""

    def __init__(self, job, todo, budget):
        self.job, self.todo, self.budget = job, todo, budget
        self.next = self.taken = 0
        self.cond = threading.Condition()
        self.closing = threading.Event()
        self.threads = [threading.Thread(target=self._loop, name="scout", daemon=True)
                        for _ in range(min(3, max(1, budget.songs.limit)))]

    def start(self):
        for t in self.threads:
            t.start()

    def advance(self):
        """A worker has taken a song."""
        with self.cond:
            self.taken += 1
            self.cond.notify_all()

    def close(self):
        self.closing.set()
        with self.cond:
            self.cond.notify_all()
        for t in self.threads:
            t.join(timeout=5)

    def _over(self):
        return self.closing.is_set() or self.job.stop.is_set()

    def _loop(self):
        job = self.job
        try:
            while not self._over():
                with self.cond:
                    while not self._over() and (self.next - self.taken >= max(2, self.budget.songs.limit * 2)
                                                or not self.budget.scouts_on.is_set()):
                        self.cond.wait(0.3)
                    if self._over() or self.next >= len(self.todo):
                        return
                    rc, plan = self.todo[self.next]
                    self.next += 1
                try:
                    job._scout(rc, plan)
                except Stopped:
                    return
                except Exception:
                    log.exception("scout crashed")
        finally:
            sources.drop_thread_ydl()
            netio.close_thread_session()


@dataclass
class _Info:
    """Minimal stand-in for FileInfo when a file is trusted from the library record (no disk read)."""
    seconds: float = 0.0
    kbps: int = 0
    src_kbps: int = 0
    cover: bool = False
    source: str = ""
    size: int = 0
    lyrics: bool = False
    mtime: int = 0
    ext: str = ""
    lossless: bool = False

    @property
    def quality_kbps(self):
        return self.src_kbps or self.kbps


def _cat_key(track):
    from .text import norm
    return norm(first_artist(track.artist)) + "|" + norm(core_title(track.title))


def run_headless(tracks, outdir, settings, stop=None, printer=print, ai=None):
    """Run a Job with plain-text progress (used by --headless)."""
    state = {"done": 0, "total": len(tracks)}

    def emit(ev):
        t = ev["type"]
        if t == "phase":
            printer(ev["text"])
        elif t == "paused":
            printer(f"Disk full — paused ({ev['free'] // 2 ** 20} MB free). It continues by itself when there is room.")
        elif t == "resumed":
            printer("Disk space is back — continuing.")
        elif t == "pace" and ev["state"] != "normal":
            printer(ev["text"])
        elif t == "result":
            r = ev["result"]
            state["done"] += 1
            if r.status != "skipped" or r.attention:
                tag = {"ok": "saved", "upgraded": "replaced", "fixed": "fixed", "kept": "kept"}.get(r.status, r.status)
                extra = f" ({'lossless' if r.quality_kbps >= 1000 else str(r.quality_kbps) + ' kbps'})" if r.quality_kbps else ""
                printer(f"[{state['done']}/{state['total']}] {tag}: {r.track} - {r.artist}{extra}"
                        + (f" — {r.note}" if r.note else ""))
                for o in r.close[:3]:
                    printer(f"      close: {o['title']} · {o.get('channel', '')} · {', '.join(o['why'])} · {o['url']}")
    return Job(tracks, outdir, settings, emit=emit, stop=stop, ai=ai).run()
