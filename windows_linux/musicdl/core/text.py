"""text.py — matching, naming and formatting helpers (no I/O)."""
import difflib
import re
import unicodedata

STOPWORDS = {"the", "a", "an", "and", "of", "feat", "ft", "featuring", "vs", "with", "from", "in", "on"}
NUMBER_WORDS = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
                "nine": "9", "ten": "10"}
# words a video title adds around the real name of a song; they say nothing about which song it is
NOISE = {"official", "audio", "video", "lyrics", "lyric", "hd", "hq", "music", "remastered", "remaster", "version",
         "topic", "vevo", "explicit", "clean", "single", "album", "full", "song", "new", "4k", "visualizer", "mv", "m",
         "v", "original", "stereo", "mono", "radio", "edit", "with", "subtitulado", "sub", "english", "letra"}
_APOSTROPHES = str.maketrans("", "", "'’‘ʼ`´")            # straight, curly, modifier letter, grave, acute
_BRACKETED = re.compile(r"\([^)]*\)|\[[^\]]*\]")


def _ascii(s):
    """Lowercase ASCII text with apostrophes removed, so "Don't", "Don’t" and "Dont" are one and the same."""
    s = str(s or "").translate(_APOSTROPHES)
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower().translate(_APOSTROPHES)


def norm(s):
    """Lowercase ASCII words with bracketed text dropped: 'Hello (Live)' -> 'hello'."""
    s = _BRACKETED.sub(" ", _ascii(s)).replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def norm_keep_parens(s):
    """Like norm() but keeps bracketed text — '(Live at the BBC)' is part of what the song is."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", _ascii(s).replace("&", " and ")).split())


def _seq(s):
    """The words of a title in order, bracketed text dropped. Latin words are folded to plain ASCII ('Beyoncé' ->
    'beyonce'); words in other alphabets are kept as they are (norm() would erase them)."""
    s = _BRACKETED.sub(" ", str(s or "").translate(_APOSTROPHES).replace("&", " and ").replace("_", " ")).lower()
    out = []
    for w in re.findall(r"\w+", s):
        folded = unicodedata.normalize("NFKD", w).encode("ascii", "ignore").decode()
        out.append(folded or w)
    return out


def toks(s):
    """Meaningful words of a title/artist ('four' and '4' are the same word); words in other alphabets count too."""
    return {NUMBER_WORDS.get(w, w) for w in _seq(s) if w not in STOPWORDS}


_VERSION_TAIL = re.compile(
    r"\s+[-–—]\s+(?=[^-–—]*(?:remaster|version|edit\b|mix\b|mono|stereo|deluxe|bonus|acoustic|live|\bfrom\b|"
    r"(?:19|20)\d\d))[^-–—]*$", re.I)
_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat|ft|featuring|with)\b\.?[^\)\]]*[\)\]]", re.I)


def core_title(title):
    """The song's name without release-specific decoration: 'Come Together - Remastered 2009' -> 'Come Together',
    'Song (feat. X)' -> 'Song'. Used for searching and matching, never for tags."""
    t = _FEAT.sub("", str(title or ""))
    for _ in range(2):
        t = _VERSION_TAIL.sub("", t)
    return t.strip() or str(title or "").strip()


def first_artist(artist):
    """'Drake, Rihanna' -> 'Drake' (the name catalogues list the song under; 'Hall & Oates' stays whole)."""
    return re.split(r",|;|\bfeat\.?\b|\bft\.?\b", str(artist or ""), maxsplit=1, flags=re.I)[0].strip() \
        or str(artist or "")


def overlap(need, have):
    return len(need & have) / len(need) if need else 1.0


FUZZY_FLOOR = 0.78


def fuzzy_overlap(need, have):
    """overlap(), but a word that is *almost* there still earns part of a point: typos ('hallelujha'), spelling variants
    ('colour'/'color'), 'lovin'/'loving'. Words under four letters must match exactly."""
    if not need:
        return 1.0
    got = 0.0
    for w in need:
        if w in have:
            got += 1.0
        elif len(w) >= 4:
            best = max((difflib.SequenceMatcher(None, w, h).ratio() for h in have
                        if h[:1] == w[:1] and abs(len(h) - len(w)) <= 3), default=0.0)
            if best >= FUZZY_FLOOR:
                got += 0.5 + 0.5 * (best - FUZZY_FLOOR) / (1.0 - FUZZY_FLOOR)
    return got / len(need)


def squash(s):
    """Letters and digits only: 'Ariana Grande' and 'ArianaGrandeVEVO' both contain 'arianagrande'."""
    return norm(s).replace(" ", "")


def name_in(name, text):
    """Is `name` (an artist) written somewhere in `text`, spaces or no spaces? ('Jay-Z' in 'JAYZ Official')"""
    a = squash(name)
    return bool(a) and len(a) >= 4 and a in squash(text)


def title_fit(title, artist, heading):
    """How exactly a video/file heading says this song and nothing else, 0..1.
    'Queen - Bohemian Rhapsody (Official Video)' is a perfect fit for 'Bohemian Rhapsody' by Queen; 'Bohemian Rhapsody
    Piano Cover by Someone' is not. The artist's name and filler words ('official', 'audio', 'hd'…) are ignored."""
    own = _seq(title)
    if not own:
        return 0.0
    skip = set(_seq(artist)) - set(own)
    words = [w for w in _seq(heading) if w not in skip and w not in NOISE]
    return difflib.SequenceMatcher(None, " ".join(own), " ".join(words)).ratio()


def extra_words(need, heading):
    """Meaningful words of `heading` that are neither the song's nor the artist's nor filler."""
    return {w for w in toks(heading) if w not in need and w not in NOISE}


def sim(a, b):
    return difflib.SequenceMatcher(None, norm(a), norm(b)).ratio()


def parse_seconds(v):
    """'3:41' / '221.5' / 221 -> seconds, or None."""
    if v in (None, ""):
        return None
    try:
        sec = 0.0
        for part in str(v).strip().split(":"):
            sec = sec * 60 + float(part)
        return sec
    except ValueError:
        return None


def duration_text(seconds):
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def long_duration(seconds):
    """'3 h 12 min' / '47 min'."""
    m = int(round(seconds / 60))
    if m < 60:
        return f"{max(1, m)} min"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if m else f"{h} h"


def eta_text(seconds):
    if seconds is None:
        return ""
    if seconds < 60:
        return "Less than a minute left"
    m = int(round(seconds / 60))
    if m < 60:
        return f"About {m} min left"
    h, m = divmod(m, 60)
    return f"About {h} h {m:02d} min left"


def size_text(nbytes):
    if nbytes >= 1e9:
        return f"{nbytes / 1e9:.1f} GB"
    if nbytes >= 1e6:
        return f"{nbytes / 1e6:.0f} MB" if nbytes >= 1e8 else f"{nbytes / 1e6:.1f} MB"
    return f"{nbytes / 1e3:.0f} KB"


def speed_text(bps):
    """Bytes/second -> '850 KB/s' or '3.2 MB/s'."""
    if bps >= 1e6:
        return f"{bps / 1e6:.1f} MB/s"
    return f"{bps / 1e3:.0f} KB/s"


# ---------------------------------------------------------------- file names

_ILLEGAL = {":": " -", "?": "", "*": "", '"': "'", "<": "", ">": "", "|": "-", "/": "-", "\\": "-"}
_FIELD = re.compile(r"\{(\w+)(?::(\d*)(d?))?\}")
FIELDS = ("title", "artist", "album", "year", "track", "disc", "genre")
MAX_SEGMENT = 150


def clean_name(s, limit=MAX_SEGMENT):
    """One file or folder name with only the characters Windows disallows changed (same rules as V1/V2)."""
    s = re.sub(r'[<>:"/\\|?*]', lambda m: _ILLEGAL[m.group()], str(s))
    s = re.sub(r"[\x00-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(". ")
    return s[:limit].rstrip(". ")


def render_template(template, values):
    """Fill '{artist}/{album}/{track:02d} {title}' from `values`; returns a relative path using '/' separators.

    Unknown fields become empty, a segment that ends up empty becomes 'Unknown', and '..' can never escape
    the output folder. Without a path separator the result is a plain file name."""
    def fill(m):
        name, width, d = m.group(1), m.group(2), m.group(3)
        v = values.get(name, "")
        if width is not None and str(v).isdigit() and (d or width):
            return str(int(v)).zfill(int(width or 0)) if int(v) else ""
        return str(v or "")
    out = []
    for seg in re.split(r"[\\/]", template):
        name = clean_name(_FIELD.sub(fill, seg))
        if name in ("", ".", ".."):
            name = "Unknown" if seg.strip() else ""
        if name:
            out.append(name)
    return "/".join(out) or "Unknown"
