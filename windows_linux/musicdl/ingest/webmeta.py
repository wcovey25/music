"""webmeta.py — structured data that ordinary web pages carry: schema.org JSON-LD and Open Graph tags."""
import html as _html
import json
import re

_LD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)
_META = re.compile(r"<meta\b([^>]*)>", re.I)
_ATTR = re.compile(r'([\w:-]+)\s*=\s*"([^"]*)"')


def attrs(tag_text):
    """{name: value} of an element's attributes (HTML-unescaped)."""
    return {k.lower(): _html.unescape(v) for k, v in _ATTR.findall(tag_text)}


def jsonld(html):
    """Every schema.org object on the page (a @graph is flattened)."""
    out = []
    for m in _LD.finditer(html):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            d = stack.pop(0)
            if isinstance(d, dict):
                out.append(d)
                if isinstance(d.get("@graph"), list):
                    stack.extend(d["@graph"])
    return out


def og(html):
    """Open Graph / Twitter meta tags as {'title': …, 'image': …}."""
    out = {}
    for m in _META.finditer(html):
        a = attrs(m.group(1))
        key = (a.get("property") or a.get("name") or "").lower()
        for prefix in ("og:", "twitter:"):
            if key.startswith(prefix) and a.get("content"):
                out.setdefault(key[len(prefix):], a["content"])
    t = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if t:
        out.setdefault("title", _html.unescape(" ".join(t.group(1).split())))
    return out
