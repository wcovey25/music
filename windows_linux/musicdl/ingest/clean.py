"""clean.py — turn a video title + channel into a song title + artist."""
import re

_JUNK = re.compile(
    r"\s*[\(\[\{]\s*(?:official|lyrics?|lyric video|music video|video|audio|visuali[sz]er|hd|hq|4k|8k|mv|"
    r"full (?:song|album|video)|explicit|clean|with lyrics|hd video|live performance video|performance video|"
    r"official (?:music )?video|official audio|official lyric video|video oficial|audio oficial)[^\)\]\}]*[\)\]\}]",
    re.I)
_PIPE_TAIL = re.compile(r"\s*[|｜]\s*(?:official|lyrics?|music video|audio|visuali[sz]er|hd|4k)[^|]*$", re.I)
_CHANNEL_NOISE = re.compile(r"\s*(?:-\s*topic|vevo|official(?: artist channel)?|music|records|channel)\s*$", re.I)
_SEP = re.compile(r"\s+[-–—―]\s+|\s*[:：]\s+(?=[A-Z])")


def clean_title(title):
    """Drop '(Official Video)', '[HD]', '| Lyrics' … but keep meaningful brackets like '(Remastered 2009)'."""
    t = str(title or "")
    for _ in range(3):
        t = _JUNK.sub("", t)
    t = _PIPE_TAIL.sub("", t)
    return re.sub(r"\s+", " ", t).strip(" -–—|")


def clean_channel(channel):
    c = str(channel or "")
    for _ in range(2):
        c = _CHANNEL_NOISE.sub("", c)
    c = re.sub(r"^official\s+(?=\S)", "", c.strip(), flags=re.I)         # "Official Arctic Monkeys"
    return c.strip()


def split_video_title(title, channel=""):
    """('Rick Astley - Never Gonna Give You Up (Official Video)', 'Rick Astley') -> ('Rick Astley', 'Never Gonna Give You Up')."""
    ch_raw = str(channel or "")
    t = clean_title(title)
    artist_from_channel = clean_channel(ch_raw)
    if ch_raw.lower().endswith("- topic"):                  # auto-generated channel: the title is just the song
        return artist_from_channel, t
    parts = _SEP.split(t, maxsplit=1)
    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
        left, right = parts[0].strip(), parts[1].strip()
        if not (re.search(r"\d{1,2}:\d{2}", left) or len(left) > 60):
            return left, right
    return artist_from_channel or "Unknown artist", t
