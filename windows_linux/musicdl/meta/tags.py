"""
tags.py — read and write tags + artwork for MP3, FLAC, M4A (AAC/ALAC) and WAV with one small API.

Each format gets the fields its players actually read:
  M4A  (Apple Music, iPhone, iTunes)   ©nam ©ART aART ©alb ©day ©gen trkn(n,total) disk(n,total) cpil rtng stik covr …
  MP3  (Windows, Android, car stereos) ID3v2.3 with UTF-16 text: TIT2 TPE1 TPE2 TALB TYER/TDAT TCON TRCK "n/total" TPOS TCMP APIC …
  FLAC (Windows, Android, foobar)      Vorbis comments: ALBUMARTIST TRACKTOTAL DISCTOTAL COMPILATION … plus a front-cover picture block

The album artist matters most after title/artist: Apple Music, Android and Windows group an album by it, and without
one a song "by A feat. B" ends up on an album of its own.

Our own provenance (where the audio came from and how good that source was) is stored inside the file,
so a later run can recognise a file it made and judge its quality without remembering anything.
"""
import os
import re
from dataclasses import dataclass

from mutagen.flac import FLAC, Picture
from mutagen.id3 import (APIC, ID3, ID3NoHeaderError, TALB, TCMP, TCON, TDRC, TIT2, TPE1, TPE2, TPOS, TPUB, TRCK, TSRC,
                         TXXX, UFID, USLT)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.wave import WAVE

TAGGABLE = (".mp3", ".flac", ".m4a", ".wav")
MB_OWNER = "http://musicbrainz.org"
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


@dataclass
class Tagset:
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    genre: str = ""
    track_no: int = 0
    disc_no: int = 0
    mbid: str = ""
    isrc: str = ""
    lyrics: str = ""
    source: str = ""
    src_kbps: int = 0
    album_artist: str = ""         # who the album is by (the grouping key in Apple Music / Android / Windows)
    track_total: int = 0           # tracks on the disc
    disc_total: int = 0            # discs in the release
    date: str = ""                 # full release date, YYYY or YYYY-MM-DD; `year` is used when empty
    explicit: int = 0              # 1 explicit, 2 clean (an edited version of an explicit song), 0 nothing to say
    compilation: bool = False
    label: str = ""


@dataclass
class FileInfo:
    seconds: float = 0.0
    kbps: int = 0                # bitrate of the file itself
    src_kbps: int = 0            # estimated quality of the original source (0 = not recorded)
    cover: bool = False
    mbid: str = ""
    source: str = ""
    size: int = 0
    mtime: int = 0
    ext: str = ""
    lossless: bool = False
    sample_rate: int = 0
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    year: str = ""
    lyrics: bool = False
    album_artist: str = ""
    track_no: int = 0
    track_total: int = 0
    disc_no: int = 0
    disc_total: int = 0
    date: str = ""
    explicit: int = 0
    compilation: bool = False
    isrc: str = ""
    label: str = ""

    @property
    def quality_kbps(self):
        """The honest quality number: what the source was, falling back to the file's own bitrate."""
        return self.src_kbps or self.kbps


def _f(v):
    return str(v) if v not in (None, "") else ""


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _release_date(tags):
    """The date to write: the full date when we have one and it is well-formed, otherwise the 4-digit year, otherwise ''."""
    d = str(tags.date or "").strip()
    if _DATE.match(d):
        return d
    y = str(tags.year or "").strip()[:4]
    return y if y.isdigit() and len(y) == 4 else ""


def image_size(data):
    """(width, height) of an image's bytes, or (0, 0)."""
    try:
        import io

        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return 0, 0


def _pair(n, total):
    """'4/11', '4', '' — the form ID3 and Vorbis use."""
    if not n:
        return ""
    return f"{int(n)}/{int(total)}" if total and int(total) >= int(n) else str(int(n))


# ---------------------------------------------------------------- writing

def write(path, tags, cover=None, merge=False):
    """Write `tags` (+ JPEG `cover`) into `path`. merge=True keeps every existing tag that `tags` leaves empty."""
    ext = os.path.splitext(path)[1].lower()
    {".mp3": _write_mp3, ".flac": _write_flac, ".m4a": _write_mp4, ".wav": _write_wav}.get(ext, lambda *a: None)(
        path, tags, cover, merge)


def _id3_fill(t, tags, cover, merge):
    if not merge:
        t.clear()

    def put(frame_id, frame):
        t.delall(frame_id)
        t.add(frame)
    if tags.title:
        put("TIT2", TIT2(encoding=1, text=tags.title))
    if tags.artist:
        put("TPE1", TPE1(encoding=1, text=tags.artist))
    if tags.album_artist:
        put("TPE2", TPE2(encoding=1, text=tags.album_artist))
    if tags.album:
        put("TALB", TALB(encoding=1, text=tags.album))
    date = _release_date(tags)
    if date:
        put("TDRC", TDRC(encoding=1, text=date))                  # saved as TYER (+ TDAT) in ID3v2.3
    if tags.genre:
        put("TCON", TCON(encoding=1, text=tags.genre))
    if tags.track_no:
        put("TRCK", TRCK(encoding=1, text=_pair(tags.track_no, tags.track_total)))
    if tags.disc_no:
        put("TPOS", TPOS(encoding=1, text=_pair(tags.disc_no, tags.disc_total)))
    if tags.isrc:
        put("TSRC", TSRC(encoding=1, text=tags.isrc))
    if tags.label:
        put("TPUB", TPUB(encoding=1, text=tags.label))
    if tags.compilation:
        put("TCMP", TCMP(encoding=1, text="1"))
    if tags.mbid:
        t.delall("UFID:" + MB_OWNER)
        t.add(UFID(owner=MB_OWNER, data=tags.mbid.encode("ascii", "ignore")))
    if tags.lyrics:
        t.delall("USLT")
        t.add(USLT(encoding=1, lang="eng", desc="", text=tags.lyrics))
    if tags.source or tags.src_kbps:
        for desc, val in (("MUSICDL_SOURCE", tags.source or "unknown"), ("MUSICDL_SRC_KBPS", str(int(tags.src_kbps or 0)))):
            t.delall("TXXX:" + desc)
            t.add(TXXX(encoding=1, desc=desc, text=val))
    if cover:
        t.delall("APIC")
        t.add(APIC(encoding=0, mime="image/jpeg", type=3, desc="Front cover", data=cover))
    t.update_to_v23()                                             # TDRC → TYER + TDAT: a v2.3 tag has no TDRC (Explorer shows no year)


def _write_mp3(path, tags, cover, merge):
    try:
        t = ID3(path) if merge else ID3()
    except ID3NoHeaderError:
        t = ID3()
    _id3_fill(t, tags, cover, merge)
    t.save(path, v2_version=3)


def _write_wav(path, tags, cover, merge):
    w = WAVE(path)
    if w.tags is None:
        w.add_tags()
    _id3_fill(w.tags, tags, cover, merge)
    w.save(v2_version=3)


def _write_flac(path, tags, cover, merge):
    f = FLAC(path)
    if not merge:
        f.clear()

    def put(key, value):
        if value:
            f[key] = [str(value)]
    put("title", tags.title)
    put("artist", tags.artist)
    put("albumartist", tags.album_artist)
    put("album", tags.album)
    put("date", _release_date(tags))
    put("genre", tags.genre)
    put("tracknumber", tags.track_no or "")
    if tags.track_no and tags.track_total:
        put("tracktotal", tags.track_total)
        put("totaltracks", tags.track_total)
    put("discnumber", tags.disc_no or "")
    if tags.disc_no and tags.disc_total:
        put("disctotal", tags.disc_total)
        put("totaldiscs", tags.disc_total)
    put("isrc", tags.isrc)
    put("label", tags.label)
    put("compilation", "1" if tags.compilation else "")
    put("musicbrainz_trackid", tags.mbid)
    put("lyrics", tags.lyrics)
    if tags.source or tags.src_kbps:
        f["musicdl_source"] = [tags.source or "unknown"]
        f["musicdl_src_kbps"] = [str(int(tags.src_kbps or 0))]
    if cover:
        f.clear_pictures()
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = 3, "image/jpeg", "Front cover", cover
        pic.width, pic.height = image_size(cover)                  # players that read the picture block want the size
        pic.depth = 24
        f.add_picture(pic)
    f.save()


def _write_mp4(path, tags, cover, merge):
    m = MP4(path)
    if not merge:
        m.clear()

    def put(key, value):
        if value:
            m[key] = [value]
    put("\xa9nam", tags.title)
    put("\xa9ART", tags.artist)
    put("aART", tags.album_artist)
    put("\xa9alb", tags.album)
    put("\xa9day", _release_date(tags))
    put("\xa9gen", tags.genre)
    put("\xa9lyr", tags.lyrics)
    if tags.track_no:
        m["trkn"] = [(int(tags.track_no), int(tags.track_total or 0))]
    if tags.disc_no:
        m["disk"] = [(int(tags.disc_no), int(tags.disc_total or 0))]
    if tags.compilation:
        m["cpil"] = True
    if tags.explicit in (1, 2):
        m["rtng"] = [int(tags.explicit)]                           # Apple Music shows the "E" badge for 1
    if (tags.title or tags.artist) and (not merge or "stik" not in m):
        m["stik"] = [1]                                            # media kind: Music (not a podcast or an audiobook)
    free = lambda v: [MP4FreeForm(str(v).encode("utf-8"))]
    if tags.source or tags.src_kbps:
        m["----:com.musicdl:SOURCE"] = free(tags.source or "unknown")
        m["----:com.musicdl:SRC_KBPS"] = free(int(tags.src_kbps or 0))
    if tags.mbid:
        m["----:com.apple.iTunes:MusicBrainz Track Id"] = free(tags.mbid)
    if tags.isrc:
        m["----:com.apple.iTunes:ISRC"] = free(tags.isrc)
    if tags.label:
        m["----:com.apple.iTunes:LABEL"] = free(tags.label)
    if cover:
        m["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    m.save()


# ---------------------------------------------------------------- reading

def read_info(path):
    """Length, bitrate, artwork, provenance and basic tags of an audio file. None if it isn't a usable file."""
    ext = os.path.splitext(path)[1].lower()
    try:
        st = os.stat(path)
        info = FileInfo(size=st.st_size, mtime=int(st.st_mtime), ext=ext)
        if ext == ".mp3":
            a = MP3(path)
            _basic(info, a)
            _read_id3(info, a.tags)
        elif ext == ".wav":
            a = WAVE(path)
            _basic(info, a)
            info.lossless = True
            _read_id3(info, a.tags)
        elif ext == ".flac":
            a = FLAC(path)
            _basic(info, a)
            info.lossless = True
            info.cover = bool(a.pictures)
            get = lambda k: _f((a.get(k) or [""])[0])
            info.title, info.artist, info.album, info.genre = get("title"), get("artist"), get("album"), get("genre")
            info.date = get("date")
            info.year = info.date[:4]
            info.mbid, info.source = get("musicbrainz_trackid"), get("musicdl_source")
            info.src_kbps = _int(get("musicdl_src_kbps"))
            info.lyrics = bool(get("lyrics"))
            info.album_artist, info.isrc, info.label = get("albumartist"), get("isrc"), get("label")
            info.track_no, info.track_total = _int(get("tracknumber").split("/")[0]), _int(get("tracktotal") or get("totaltracks"))
            info.disc_no, info.disc_total = _int(get("discnumber").split("/")[0]), _int(get("disctotal") or get("totaldiscs"))
            info.compilation = get("compilation") == "1"
        elif ext == ".m4a":
            a = MP4(path)
            _basic(info, a)
            info.lossless = str(getattr(a.info, "codec", "")).lower().startswith("alac")
            t = a.tags or {}
            info.cover = bool(t.get("covr"))
            first = lambda k: _f((t.get(k) or [""])[0])
            info.title, info.artist, info.album, info.genre = first("\xa9nam"), first("\xa9ART"), first("\xa9alb"), first("\xa9gen")
            info.album_artist = first("aART")
            info.date = first("\xa9day")
            info.year = info.date[:4]
            info.lyrics = bool(first("\xa9lyr"))
            free = lambda k: bytes((t.get(k) or [b""])[0]).decode("utf-8", "ignore")
            info.source = free("----:com.musicdl:SOURCE")
            info.src_kbps = _int(free("----:com.musicdl:SRC_KBPS"))
            info.mbid = free("----:com.apple.iTunes:MusicBrainz Track Id")
            info.isrc, info.label = free("----:com.apple.iTunes:ISRC"), free("----:com.apple.iTunes:LABEL")
            trkn, disk = (t.get("trkn") or [(0, 0)])[0], (t.get("disk") or [(0, 0)])[0]
            info.track_no, info.track_total = int(trkn[0] or 0), int(trkn[1] or 0)
            info.disc_no, info.disc_total = int(disk[0] or 0), int(disk[1] or 0)
            info.compilation = bool(t.get("cpil"))
            info.explicit = int((t.get("rtng") or [0])[0] or 0)
        else:
            return None
        if info.lossless and not info.src_kbps and not info.source:
            info.src_kbps = 0
        return info
    except Exception:
        return None


def _basic(info, a):
    info.seconds = float(a.info.length or 0)
    info.kbps = int(round((getattr(a.info, "bitrate", 0) or 0) / 1000))
    info.sample_rate = int(getattr(a.info, "sample_rate", 0) or 0)


def _read_id3(info, tags):
    if not tags:
        return
    info.cover = any(k.startswith("APIC") for k in tags.keys())
    info.lyrics = any(k.startswith("USLT") for k in tags.keys())
    u = tags.get("UFID:" + MB_OWNER)
    if u is not None:
        info.mbid = u.data.decode("ascii", "ignore")
    text = lambda k: _f(tags[k].text[0]) if k in tags and getattr(tags[k], "text", None) else ""
    info.title, info.artist, info.album, info.genre = text("TIT2"), text("TPE1"), text("TALB"), text("TCON")
    info.album_artist, info.isrc, info.label = text("TPE2"), text("TSRC"), text("TPUB")
    info.date = text("TDRC")
    info.year = info.date[:4]
    trck, tpos = text("TRCK"), text("TPOS")
    info.track_no, info.track_total = _int(trck.split("/")[0]), _int(trck.split("/")[1]) if "/" in trck else 0
    info.disc_no, info.disc_total = _int(tpos.split("/")[0]), _int(tpos.split("/")[1]) if "/" in tpos else 0
    info.compilation = text("TCMP") == "1"
    for fr in tags.getall("TXXX"):
        try:
            if fr.desc == "MUSICDL_SRC_KBPS":
                info.src_kbps = int(float(fr.text[0]))
            elif fr.desc == "MUSICDL_SOURCE":
                info.source = str(fr.text[0])
        except (ValueError, IndexError):
            pass


def extract_cover(path):
    """Raw embedded artwork bytes of a file, or None."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".mp3", ".wav"):
            tags = (MP3(path) if ext == ".mp3" else WAVE(path)).tags or {}
            for k, fr in tags.items():
                if k.startswith("APIC"):
                    return fr.data
        elif ext == ".flac":
            pics = FLAC(path).pictures
            return pics[0].data if pics else None
        elif ext == ".m4a":
            covr = (MP4(path).tags or {}).get("covr")
            return bytes(covr[0]) if covr else None
    except Exception:
        pass
    return None
