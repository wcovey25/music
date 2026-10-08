"""base.py — shared pieces of every link resolver."""
import json
import re
import threading
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ..core import netio
from ..core.models import EngineError

SERVICES = {
    "spotify": "Spotify", "apple": "Apple Music", "ytmusic": "YouTube Music", "youtube": "YouTube",
    "amazon": "Amazon Music", "pandora": "Pandora", "web": "Web link", "sheet": "Spreadsheet",
    "search": "Search", "ai": "AI playlist",
}


class ResolveError(Exception):
    """A link could not be turned into songs; the message is written for the user."""


@dataclass
class Ctx:
    stop: threading.Event = field(default_factory=threading.Event)
    say: object = None                                       # optional status callback(str)

    def status(self, text):
        if self.say:
            self.say(text)


def host_of(url):
    return urlsplit(url).netloc.lower().removeprefix("www.")


def page(url, ctx, **kw):
    """Fetch a web page as text; network trouble becomes a friendly ResolveError."""
    try:
        text = netio.get_text(url, browser=True, stop=ctx.stop, **kw)
    except EngineError as e:
        raise ResolveError(f"Couldn’t reach {host_of(url)} ({e})") from None
    if text is None:
        raise ResolveError(f"{host_of(url)} says this page doesn’t exist")
    return text


def follow(url, ctx):
    """Resolve a short link (spotify.link, pandora.app.link, apple.co …) to its destination."""
    try:
        r = netio.http().get(url, headers={"User-Agent": netio.BROWSER_UA}, timeout=(8, 15), allow_redirects=True,
                             stream=True)
        final = r.url
        r.close()
        return final
    except Exception:
        return url


def script_json(html, script_id):
    """The JSON inside <script id="…"> (Spotify's __NEXT_DATA__, Apple's serialized-server-data), or None."""
    m = re.search(r'<script[^>]*\bid="%s"[^>]*>(.*?)</script>' % re.escape(script_id), html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def dig(obj, *path, default=None):
    """dig(d, 'a', 0, 'b') — like d['a'][0]['b'] but None-safe."""
    for key in path:
        try:
            obj = obj[key]
        except (KeyError, IndexError, TypeError):
            return default
    return default if obj is None else obj


def squash(s):
    """Collapse the odd whitespace (nbsp, newlines) streaming sites put in names."""
    return " ".join(str(s or "").split())


def first_url(text):
    m = re.search(r"https?://[^\s<>\"']+", text)
    return m.group(0).rstrip(".,);]") if m else ""
