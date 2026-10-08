"""
release.py — which release of a song to tag it with (no network).

A catalogue lists one song on many releases: the studio album, a live album, greatest-hits compilations, deluxe
reissues, karaoke versions. Tagging a studio recording with "Live in Texas" or "The Best of …" is wrong, and so is giving
a 1983 song the year of its 2026 digital reissue. Every candidate ("hit") is judged on

  * the recording: is it a live / remixed / acoustic / karaoke / sped-up version, and is its length the song's length?
    A candidate that is a different recording than the one asked for is rejected;
  * the release: a studio album beats a single or an EP, which beats a compilation, a box set, a soundtrack;
  * where the catalogue itself lists it: it returns the most relevant (usually the canonical) release first.

Hits are grouped into albums (a deluxe edition and the standard edition are one album); the best album wins, and the
standard edition stands in for it, since a deluxe edition has other track numbers and totals.

A hit is a dict: src, title, artist, album, album_artist, date ('YYYY-MM-DD' or 'YYYY'), track_no, track_total, disc_no,
disc_total, genre, explicit (0/1/2), seconds, cover (image URL), plus whatever the provider needs to know about it.
"""
import re
from dataclasses import dataclass, field

from .text import norm, norm_keep_parens

REJECT = 6.0                     # a recording penalty at or above this: it is a different recording, never used
GENERIC_GENRES = {"", "music", "unknown", "other"}
LISTING = 0.25                   # penalty per place down the catalogue's own list ...
LISTING_MAX = 3.0                # ... up to this much

_BRACKET = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_DASH_TAIL = re.compile(r"\s[-–—]\s+(.+)$")
_RERECORD = r"taylor.?s version|\bre-?recorded\b|\bfrom the vault\b"       # a new recording of an old album: another release

# what a song title says about the *recording*, looked for inside its brackets and after a dash: "(Live at …)", "- Radio Edit"
_TITLE_FLAGS = (
    ("live", re.compile(r"\blive\b|\bunplugged\b|\bin concert\b|\bon stage\b"), 8.0),
    ("acoustic", re.compile(r"\bacoustic\b|\bstripped\b|\bpiano version\b|\bsolo version\b"), 4.0),
    ("remix", re.compile(r"\bremix(es|ed)?\b|\brmx\b|\bmash-?up\b|\bbootleg\b|\bvip\b|\bflip\b|\bdub\b|\bclub mix\b|"
                         r"\bextended (mix|version)\b"), 6.0),
    ("imitation", re.compile(r"\binstrumental\b|\bkaraoke\b|\bbacking track\b|\bin the style of\b|\bmade famous\b|"
                             r"\boriginally performed\b|\btribute\b|\bcover\b|\bperformed by\b|\bsing-?along\b|"
                             r"\blullaby\b|\bstring quartet\b|\bpiano cover\b"), 12.0),
    ("speed", re.compile(r"\bsped\b|\bslowed\b|\breverb\b|\bnightcore\b|\b8d\b|\blo-?fi\b|\bchopped\b|\bspeed up\b"), 12.0),
    ("demo", re.compile(r"\bdemo\b|\bouttake\b|\balternat(e|ive)\b|\brough mix\b|\bunreleased\b|\bwork(ing)? (tape|mix)\b|"
                        r"\btake \d+\b|\bsessions?\b|\bearly version\b"), 4.0),
    ("edit", re.compile(r"\b(radio|single|album|clean|short|extended|original) (edit|version|mix)\b|\bedit\b|\bmono\b"), 0.8),
    ("rerecord", re.compile(_RERECORD), 3.0),
)
# what an album's name says about the *release*
_ALBUM_FLAGS = (
    ("live", re.compile(r"\blive (at|in|from|on|around|with|aid|and)\b|[\(\[:\-\s]live[\)\]]?!?$|\bunplugged\b|"
                        r"\bin concert\b|\blive\b.*\b(tour|concert|session|sessions|show|recording)\b|"
                        r"\b(19|20)\d\d\b.*\blive\b|\blive\b.*\b(19|20)\d\d\b"), 6.0, "rec"),
    ("remix", re.compile(r"\bremix(es|ed)?\b|\bmash-?ups?\b|\bdance mixes\b"), 6.0, "rec"),
    ("imitation", re.compile(r"\bkaraoke\b|\btribute\b|\bmade famous\b|\boriginally performed\b|\bin the style of\b|"
                             r"\bbacking tracks?\b|\blullaby\b|\bstring quartet\b|\bsound-?alike\b|\bsped up\b|\bslowed\b|"
                             r"\bsing-?along\b|\bcover versions?\b|\bhits? of the\b"), 12.0, "rec"),
    ("compilation", re.compile(r"\bgreatest hits\b|\bbest of\b|\bthe best\b|\bhits\b|\bcollection\b|\banthology\b|\bessentials?\b|"
                               r"\bdefinitive\b|\bultimate\b|\bgold\b|\bplatinum\b|\bclassics\b|\bnumber (ones?|1'?s)\b|#1|"
                               r"\bsingles\b|\bthe story\b|\blegends?\b|\bicon\b|\bplaylist\b|\bnow that'?s\b|\bthrowback\b|"
                               r"\bcomplete\b|\bbox\b|\bdecades?\b|\bsongbook\b|\bretrospective\b|\bselected\b|\bthe very\b|"
                               r"\bfavou?rites\b|\byears\b|\boriginals\b|\bcentury\b|\bvintage\b|"
                               r"\b(19|20)\d\d\s*[-–]\s*((19|20)\d\d|\d\d)\b"), 3.0, "rel"),
    ("soundtrack", re.compile(r"\bsoundtrack\b|\boriginal motion picture\b|\bmusic from\b|\boriginal score\b|\bcast recording\b|\bost\b"),
     1.0, "rel"),
    ("edition", re.compile(r"\bdeluxe\b|\banniversary\b|\bexpanded\b|\bremaster(ed)?\b|\bspecial edition\b|\bcollector'?s?\b|"
                           r"\blegacy\b|\breissue\b|\bbonus\b|\bedition\b|\bsuper deluxe\b"), 0.3, "rel"),
    ("rerecord", re.compile(_RERECORD), 3.0, "rec"),
    ("ep", re.compile(r"[-–]\s*ep$|\bep\)?$"), 1.0, "rel"),
    ("single", re.compile(r"[-–]\s*single$|\(single\)$"), 0.5, "rel"),
)
_EDITION_WORDS = re.compile(r"\b(deluxe|super|anniversary|expanded|remaster(ed)?|special|collector'?s?|legacy|reissue|bonus|"
                            r"edition|version|explicit|clean|stereo|mono|\d+(st|nd|rd|th))\b")


def _segments(title):
    """The bracketed parts and the text after a dash: where a title says what kind of recording it is."""
    t = str(title or "")
    segs = [m.group(1) for m in _BRACKET.finditer(t)]
    m = _DASH_TAIL.search(t)
    if m:
        segs.append(m.group(1))
    return " ".join(segs).lower()


def title_flags(title):
    """{flag: penalty} for a song title: 'Numb (Live)' → {'live': 8.0}."""
    seg = _segments(title)
    return {name: pen for name, rx, pen in _TITLE_FLAGS if seg and rx.search(seg)}


def album_flags(album, album_artist=""):
    """{flag: (penalty, 'rec'|'rel')} for an album name; 'rec' flags mean the recording differs, 'rel' only the release."""
    a = str(album or "").lower().strip()
    out = {name: (pen, kind) for name, rx, pen, kind in _ALBUM_FLAGS if a and rx.search(a)}
    if str(album_artist or "").strip().lower() == "various artists":
        out["various"] = (4.0, "rel")
    return out


def family(album):
    """An album's identity across its editions: 'Meteora (Deluxe Edition)' and 'Meteora' are the same album."""
    mark = " rerecorded" if re.search(_RERECORD, str(album or "").lower()) else ""      # "1989" ≠ "1989 (Taylor's Version)"
    s = _BRACKET.sub(" ", str(album or "")).lower()
    s = re.sub(r"\s[-–—]\s+(single|ep)$", "", s)
    s = _EDITION_WORDS.sub(" ", norm_keep_parens(s))
    return (" ".join(norm(s).split()) or norm(album)) + mark


def is_edition(hit):
    return "edition" in album_flags(hit.get("album"))


def valid_date(d):
    """'YYYY-MM-DD' / 'YYYY' (a plausible one), else ''. A date of January 1st is only trusted as a year."""
    d = str(d or "")[:10]
    if not re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", d) or not ("1900" <= d[:4] <= "2100"):
        return ""
    if d.endswith("-01-01"):
        return d[:4]
    return d


def _date_key(d):
    """Sort key: a year alone counts as the end of that year (we only know it was sometime then)."""
    d = valid_date(d)
    return (d + "-12-31") if len(d) == 4 else (d or "9999")


def judge(hit, want):
    """(recording_penalty, release_penalty) of a hit for what was asked. `want`: title, album, seconds."""
    want_title = title_flags(want.get("title"))
    want_album = album_flags(want.get("album"))
    rec, rel = 0.0, 0.0
    mine_flags = title_flags(hit.get("title"))
    for name, pen in mine_flags.items():
        if name not in want_title and name not in want_album:
            rec += pen
    for name, (pen, kind) in album_flags(hit.get("album"), hit.get("album_artist")).items():
        if name in want_title or name in want_album:
            continue
        if kind == "rec":
            rec += pen
        else:
            rel += pen
    for name in ("live", "remix", "acoustic"):                # asked for a live version: a studio one is the wrong recording
        if name in want_title and name not in mine_flags and name not in album_flags(hit.get("album")):
            rec += 4.0
    secs, mine = float(hit.get("seconds") or 0), float(want.get("seconds") or 0)
    if secs and mine:
        d = abs(secs - mine) / mine
        rec += 6.0 if d > 0.25 else (2.5 if d > 0.10 else 0.0)
    tracks, discs = int(hit.get("track_total") or 0), int(hit.get("disc_total") or 0)
    if tracks >= 32:                                          # a long track list is a compilation or a box set
        rel += 3.0
    elif tracks >= 24:
        rel += 1.5
    if discs >= 2:
        rel += 1.0
    want_fam = family(want.get("album"))
    if want_fam and family(hit.get("album")) == want_fam:
        rel -= 6.0                                            # the service already said which album this is
    return rec, min(rel, 7.0)


@dataclass
class Choice:
    """One album the song is on: the hit that stands for it, how it scored, and every hit of the same album."""
    hit: dict
    score: float
    members: list = field(default_factory=list)


def rank(hits, want):
    """The albums this song is on, best first: [Choice]. Only hits that are the right recording count."""
    groups = {}
    for i, h in enumerate(hits):
        rec, rel = judge(h, want)
        if rec >= REJECT:
            continue
        score = rec + rel + min(LISTING_MAX, LISTING * i)
        groups.setdefault(family(h.get("album")), []).append((score, i, h))
    out = []
    for members in groups.values():
        best = min(m[0] for m in members)
        rep = min(members, key=lambda m: (is_edition(m[2]), m[0], m[1]))[2]       # the standard edition if there is one
        out.append(Choice(rep, best, [m[2] for m in sorted(members, key=lambda m: m[1])]))
    out.sort(key=lambda c: (c.score, _date_key(c.hit.get("date"))))
    return out


def original_date(choice):
    """The release date of an album. Editions are dated by when *they* came out (and sometimes carelessly), so the
    standard edition's full date is preferred, then another edition's, then a bare year; the earliest of those."""
    def full(h):
        d = valid_date(h.get("date"))
        return d if len(d) == 10 else ""
    plain = [full(h) for h in choice.members if not is_edition(h)]
    editions = [full(h) for h in choice.members if is_edition(h)]
    years = [valid_date(h.get("date")) for h in choice.members]
    for pool in (plain, editions, years):
        pool = [d for d in pool if d]
        if pool:
            return min(pool)
    return ""


def generic_genre(g):
    return str(g or "").strip().lower() in GENERIC_GENRES
