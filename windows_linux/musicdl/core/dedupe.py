"""
dedupe.py — what is already in the output folder, so a song you have is never downloaded twice.

Songs are recognised by *who and what* they are (artist + title, from the tags, falling back to the file name), not
by exact file name or format: "Skyfall - Adele.mp3" is the same song as "Skyfall - Adele.flac" and as
"Adele - Skyfall.m4a" in an artist folder. What was read from each file is remembered in the folder's state file,
so only new or changed files are opened on later runs.
"""
import os
import re
from dataclasses import dataclass

from ..meta.tags import read_info
from . import quality
from .models import Stopped
from .text import core_title, first_artist, norm, norm_keep_parens

AUDIO_EXT = (".mp3", ".flac", ".wav", ".m4a")             # the formats the tag reader understands
_NUMBER = re.compile(r"^\s*\d{1,3}(?:\s*[-._)]\s*|\s+)")   # '01 ', '01. ', '01 - '
_DASH = re.compile(r"\s+[-–—]\s+")


def song_key(artist, title):
    """Same key for the same song however it is decorated: 'Song - Remastered 2009' and '(feat. X)' are ignored,
    '(Live)' is not."""
    a, t = norm(first_artist(artist)), norm_keep_parens(core_title(title))
    return f"{a}|{t}" if a and t else ""


def name_keys(rel):
    """Best guesses at (artist, title) from a relative path: 'Title - Artist', 'Artist - Title', '01 Title' in an
    artist folder, '01 Title' in an artist/album folder."""
    parts = rel.replace("\\", "/").split("/")
    stem = _NUMBER.sub("", os.path.splitext(parts[-1])[0])
    bits = _DASH.split(stem)
    out = []
    if len(bits) >= 2:
        out += [song_key(bits[-1], bits[0]), song_key(bits[0], bits[-1])]
    else:
        for folder in parts[-2:-4:-1]:                    # parent, then grandparent
            out.append(song_key(folder, stem))
    return [k for k in out if k]


@dataclass
class Found:
    """One audio file of the folder, with just what the quality rules need."""
    rel: str
    path: str
    ext: str
    kbps: int
    src_kbps: int
    lossless: bool
    seconds: float
    size: int = 0
    mtime: int = 0

    @property
    def quality_kbps(self):
        return quality.delivered(self)


class FolderIndex:
    """Every readable audio file under `outdir`, findable by song."""

    def __init__(self, outdir, lib, stop=None, say=None):
        self.outdir = outdir
        self.by_key = {}
        self.count = 0
        self._build(lib, stop, say)

    def _build(self, lib, stop, say):
        old, fresh, n = lib.files(), {}, 0
        for base, dirs, names in os.walk(self.outdir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if not name.lower().endswith(AUDIO_EXT):
                    continue
                path = os.path.join(base, name)
                rel = os.path.relpath(path, self.outdir).replace(os.sep, "/")
                n += 1
                if stop is not None and stop.is_set():
                    raise Stopped()
                if say and n % 200 == 0:
                    say(f"Checking your library… {n:,} files")
                try:
                    st = os.stat(path)
                except OSError:
                    continue
                rec = old.get(rel)
                if not rec or rec.get("size") != st.st_size or rec.get("mtime") != int(st.st_mtime):
                    rec = self._read(path, st)
                fresh[rel] = rec
                if not rec.get("bad"):
                    self._add(rel, path, rec)
        lib.set_files(fresh)
        self.count = n

    @staticmethod
    def _read(path, st):
        info = read_info(path)
        if info is None or info.seconds < 1:
            return {"size": st.st_size, "mtime": int(st.st_mtime), "bad": True}
        return {"size": st.st_size, "mtime": int(st.st_mtime), "title": info.title, "artist": info.artist,
                "kbps": info.kbps, "src_kbps": info.src_kbps, "lossless": bool(info.lossless),
                "seconds": round(info.seconds, 1)}

    def _add(self, rel, path, rec):
        ext = os.path.splitext(path)[1].lower()
        kbps = int(rec.get("kbps", 0))
        found = Found(rel, path, ext, kbps, int(rec.get("src_kbps", 0)),
                      quality.is_lossless_file(ext, kbps, rec.get("lossless", False)), float(rec.get("seconds", 0)),
                      int(rec.get("size", 0)), int(rec.get("mtime", 0)))
        keys = []
        if rec.get("title") and rec.get("artist"):
            keys.append(song_key(rec["artist"], rec["title"]))
        keys += name_keys(rel)
        for k in dict.fromkeys(k for k in keys if k):
            self.by_key.setdefault(k, []).append(found)

    def find(self, artist, title):
        """Files that are this song (best quality first)."""
        hits = self.by_key.get(song_key(artist, title), [])
        return sorted(hits, key=lambda f: -f.quality_kbps)
