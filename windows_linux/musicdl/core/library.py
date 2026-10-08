"""library.py — what the output folder contains, remembered between runs in <outdir>/.musicdl.json."""
import json
import logging
import os
import threading
import time

log = logging.getLogger("musicdl")
STATE_NAME = ".musicdl.json"
BUCKETS = ("meta", "catalog", "tracks", "files")


class Library:
    """Per-folder memory: MusicBrainz lookups, catalogue lookups, a record per saved file, and a light description
    of every audio file in the folder (what dedupe.py read from it).

    A record lets the next run skip re-reading a file it already verified (size + mtime fast path) and
    remembers things like 'artwork was searched for and not found' so we do not repeat that every time.
    State written by V2 (same layout) is picked up as-is."""

    def __init__(self, outdir):
        self.path = os.path.join(outdir, STATE_NAME)
        self._lock = threading.RLock()
        self._dirty, self._saved_at = 0, 0.0
        self._gone = {}                                               # keys this run forgot: a save must not bring them back
        self.data = {"version": 3, "meta": {}, "catalog": {}, "tracks": {}, "files": {}}
        loaded = self._read()
        if loaded is None and os.path.exists(self.path):              # damaged: set aside, the last good copy instead
            try:
                from . import shield
                from .. import platform_
                if shield.heal_library(outdir, shield.shield_dir(platform_.config_dir())):
                    log.warning("the folder's record was damaged; the last good copy was put back")
                loaded = self._read()
            except Exception:
                log.debug("library recovery failed", exc_info=True)
        for k in BUCKETS:
            if isinstance((loaded or {}).get(k), dict):
                self.data[k] = loaded[k]

    def _read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            return loaded if isinstance(loaded, dict) else None
        except (OSError, ValueError):
            return None

    def track(self, name):
        with self._lock:
            return dict(self.data["tracks"].get(name) or {})

    def set_track(self, name, rec):
        with self._lock:
            self.data["tracks"][name] = rec
            self._touch()

    def forget_track(self, name):
        """A file was removed or replaced under another name: drop what we remembered about it."""
        with self._lock:
            self.data["tracks"].pop(name, None)
            self.data["files"].pop(name, None)
            self._gone.setdefault("tracks", set()).add(name)
            self._touch()

    def files(self):
        with self._lock:
            return dict(self.data["files"])

    def set_files(self, files):
        """Replace the folder listing (also drops entries for files that are gone). Saved with the next save."""
        with self._lock:
            self.data["files"] = files
            self._dirty += 1

    def get(self, bucket, key):
        with self._lock:
            return self.data[bucket].get(key)

    def put(self, bucket, key, value):
        with self._lock:
            self.data[bucket][key] = value
            self._touch()

    def forget_old(self, max_catalog=20000):
        """Keep the catalogue cache from growing without bound."""
        with self._lock:
            cat = self.data["catalog"]
            if len(cat) > max_catalog:
                for k in sorted(cat, key=lambda k: cat[k].get("t", 0))[:len(cat) - max_catalog]:
                    del cat[k]
                    self._gone.setdefault("catalog", set()).add(k)

    def _touch(self):
        self._dirty += 1
        if time.time() - self._saved_at > 5:
            self.save()

    def _merge_disk(self):
        """Another run may have saved this folder's record since we read it: keep what it added. Our records win on the
        same key, and what this run forgot stays forgotten. The folder listing ('files') is this run's alone."""
        disk = self._read() or {}
        for k in BUCKETS:
            if k == "files" or not isinstance(disk.get(k), dict):
                continue
            merged = dict(disk[k])
            merged.update(self.data[k])
            for key in self._gone.get(k, ()):
                merged.pop(key, None)
            self.data[k] = merged

    def save(self, force=False):
        with self._lock:
            if not (self._dirty or force):
                return
            try:
                self._merge_disk()
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, separators=(",", ":"))
                os.replace(tmp, self.path)
                self._dirty, self._saved_at, self._gone = 0, time.time(), {}
            except OSError as e:
                log.warning("could not save library state: %s", e)
