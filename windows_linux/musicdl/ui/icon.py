"""
icon.py — the app's icon: a deep midnight squircle lit from within by a blue and violet aurora, with a lens of glass in the
shape of two beamed notes sitting in it.

Pure Pillow, drawn at several times the final size and shrunk, so every edge is clean from the 16-px taskbar size to the
256-px one. On Windows the sizes are also packed into one .ico (ico_file) for the taskbar and Alt+Tab. The colours come from the window's own glass theme, so the icon looks like it belongs to it.

Layers, bottom to top: the backdrop (a dark diagonal gradient, an aurora of blurred coloured light — cyan-blue from the
bottom-left, violet from the top-right, a touch of pink — a vignette and a hint of grain), faint ripples spreading out from
the notes, a luminous core behind them, the notes' shadows, then the glass itself. The glass is not painted: it is the picture
behind it, magnified and nudged (what a lens does), frosted a little, with the red and blue channels pulled apart by a pixel
or two (dispersion), milky at the top and deeper blue toward the bottom, a soft diagonal reflection across it, a bright rim
on the edges that face the light, light that has come through the glass along the edges that face away, and a colour fringe
just outside the shape. Over the whole icon: a faint reflection across the upper half and a fine rim light round the edge.

`margin` leaves a transparent border with a soft shadow under the shape (the macOS edition uses it for the Dock); the
window's header, the launch animation and the Windows icon use 0.
"""
import hashlib
import logging
import math
import os
import random

import PIL
from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .. import platform_
from .glass import over, scaled, soft_blur

log = logging.getLogger("musicdl")

ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)     # what Windows asks an .ico for, at 100–250 % display scaling
_CACHE = {}
_PRINT = None


# ---------------------------------------------------------------- remembered between launches

def _fingerprint():
    """Names the art this code (and this Pillow) draws: change the drawing and the remembered pictures are not used."""
    global _PRINT
    if _PRINT is None:
        try:
            with open(__file__, "rb") as fh:
                digest = hashlib.md5(fh.read() + PIL.__version__.encode()).hexdigest()[:10]
        except OSError:
            digest = "nofile"
        _PRINT = digest
    return _PRINT


def _remembered(size, margin):
    """The icon as an earlier launch drew it (a few milliseconds to read, against a fifth of a second to draw), or None."""
    try:
        path = _disk_path(size, margin)
        if os.path.exists(path):
            with Image.open(path) as im:
                im.load()
                if im.size == (size, size):
                    return im.convert("RGBA")
    except Exception:                                      # noqa: BLE001 — a damaged file is only a cache miss
        log.debug("icon cache unreadable", exc_info=True)
    return None


def _remember(img, size, margin):
    try:
        path = _disk_path(size, margin)
        folder = os.path.dirname(path)
        keep = os.path.basename(path).rsplit("-", 1)[-1]
        for old in os.listdir(folder):                     # pictures of an older drawing are of no further use
            if old.startswith("icon-") and not old.endswith(keep):
                try:
                    os.remove(os.path.join(folder, old))
                except OSError:
                    pass
        tmp = f"{path}.{os.getpid()}.tmp"
        img.save(tmp, "PNG", compress_level=1)
        os.replace(tmp, path)
    except Exception:                                      # noqa: BLE001
        log.debug("icon cache not written", exc_info=True)


def _disk_path(size, margin):
    folder = os.path.join(platform_.cache_dir(), "icons")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, f"icon-{size}-{int(round(margin * 10000))}-{_fingerprint()}.png")


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def _smooth(a, b, x):
    t = _clamp((x - a) / (b - a))
    return t * t * (3 - 2 * t)


def _lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _field(fn, n=96):
    """A smooth RGB picture from fn(u, v) -> (r, g, b), computed on an n×n grid (it is stretched later)."""
    img = Image.new("RGB", (n, n))
    img.putdata([tuple(int(_clamp(c, 0, 255)) for c in fn((i % n + .5) / n, (i // n + .5) / n)) for i in range(n * n)])
    return img


def squircle(size, n=5.0):
    """'L' mask of the macOS icon shape: a superellipse (|x|ⁿ + |y|ⁿ = 1), n≈5, drawn supersampled."""
    big = size * 2
    pts = []
    for i in range(720):
        t = i / 720 * 2 * math.pi
        c, s = math.cos(t), math.sin(t)
        pts.append((big / 2 + big / 2 * math.copysign(abs(c) ** (2 / n), c) * 0.999,
                    big / 2 + big / 2 * math.copysign(abs(s) ** (2 / n), s) * 0.999))
    m = Image.new("L", (big, big), 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    return m.resize((size, size), Image.LANCZOS)


# ---------------------------------------------------------------- the backdrop

_RADIAL = {}


def _radial(a, cx, cy, r, power=1.6):
    """'L' radial falloff, `a` px square: 255 at (cx, cy), 0 at distance r (both fractions of the width)."""
    key = (round(cx, 3), round(cy, 3), round(r, 3), power)
    small = _RADIAL.get(key)
    if small is None:
        n = 96
        small = Image.new("L", (n, n))
        small.putdata([int(255 * (1 - _smooth(0.0, 1.0, math.hypot((i % n + .5) / n - cx, (i // n + .5) / n - cy) / r)) ** power)
                       for i in range(n * n)])
        _RADIAL[key] = small
    return small.resize((a, a), Image.BICUBIC)


# where the coloured light comes from: (x, y, reach, colour, strength) — fractions of the icon's width
AURORA = ((0.08, 1.00, 0.80, (24, 120, 255), 1.00),       # cyan-blue, bottom-left
          (1.00, 0.02, 0.74, (168, 60, 255), 0.95),       # violet, top-right
          (0.82, 1.04, 0.42, (255, 80, 170), 0.60),       # a touch of pink, bottom-right
          (0.46, 0.50, 0.62, (60, 96, 255), 0.80),        # the middle, where the notes sit
          (0.10, 0.02, 0.36, (40, 150, 255), 0.45))       # a cool glint, top-left


def _backdrop(a):
    img = _field(lambda u, v: _lerp((24, 22, 86), (7, 6, 34), _smooth(0.0, 1.0, _clamp(0.25 * u + 0.75 * v)))
                 ).resize((a, a), Image.BICUBIC).convert("RGBA")
    for cx, cy, r, col, k in AURORA:
        over(img, col, scaled(_radial(a, cx, cy, r), k))
    vignette = _radial(a, 0.5, 0.5, 0.78, 0.55).point(lambda v: 255 - v)                  # 0 in the middle, 255 at the corners
    over(img, (4, 4, 28), scaled(vignette, 0.55))
    grain = random.Random(7).randbytes(a * a)                                              # a hint of grain against banding;
    noise = Image.frombytes("L", (a, a), grain).point(lambda v: 126 + v // 64)             # the same every time
    return Image.merge("RGBA", [ImageChops.add(c, noise, 1.0, -128) for c in img.split()[:3]] + [img.getchannel("A")])


# ---------------------------------------------------------------- the notes

def _notes_mask(s, thick):
    """'L' mask of two beamed eighth notes, tilted heads and a slanted beam, centred in an s×s square."""
    m = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(m)
    sw = 0.040 * thick * s                                   # stem width
    beam_t = 0.092 * thick * s
    heads = ((0.352, 0.690), (0.672, 0.624))
    rx, ry, tilt = 0.104 * s, 0.076 * s, 24
    stems = []
    for hx, hy in heads:
        layer = Image.new("L", (s, s), 0)
        ImageDraw.Draw(layer).ellipse((hx * s - rx, hy * s - ry, hx * s + rx, hy * s + ry), fill=255)
        layer = layer.rotate(tilt, resample=Image.BICUBIC, center=(hx * s, hy * s))
        m = ImageChops.lighter(m, layer)
        stems.append(hx * s + rx * math.cos(math.radians(tilt)) * 0.90)
    d = ImageDraw.Draw(m)
    top = (0.292 * s, 0.232 * s)                             # beam: left end, right end (higher), before thickness
    for (hx, hy), x in zip(heads, stems):
        y_top = top[0] + (top[1] - top[0]) * (x - stems[0]) / (stems[1] - stems[0])
        d.rounded_rectangle((x - sw / 2, y_top, x + sw / 2, hy * s), radius=sw / 2, fill=255)
    x0, x1 = stems[0] - sw / 2, stems[1] + sw / 2
    y0 = top[0] + (top[1] - top[0]) * (x0 - stems[0]) / (stems[1] - stems[0])
    y1 = top[0] + (top[1] - top[0]) * (x1 - stems[0]) / (stems[1] - stems[0])
    d.polygon([(x0, y0), (x1, y1), (x1, y1 + beam_t), (x0, y0 + beam_t)], fill=255)
    m = m.filter(ImageFilter.GaussianBlur(s * 0.003)).point(lambda v: int(_clamp((v - 100) * 3.0, 0, 255)))     # rounds the joins
    return _centred(m)


def _centred(mask):
    """`mask` moved (by a fraction of a pixel if need be) so the middle of the box round the notes is the middle of the
    square: the two heads and the beam are not the same width either side of anything, so the drawing coordinates alone
    leave them off to one side."""
    s = mask.width
    x0, y0, x1, y1 = mask.point(lambda v: 255 if v > 127 else 0).getbbox()
    dx, dy = s / 2.0 - (x0 + x1) / 2.0, s / 2.0 - (y0 + y1) / 2.0
    if abs(dx) < 0.01 and abs(dy) < 0.01:
        return mask
    return mask.transform(mask.size, Image.AFFINE, (1, 0, -dx, 0, 1, -dy), Image.BICUBIC)


def _shift(mask, dx, dy):
    out = Image.new("L", mask.size, 0)
    out.paste(mask, (int(dx), int(dy)))
    return out


# ---------------------------------------------------------------- the icon

def _art(a, thick):
    """The finished icon artwork, `a` px square, full-bleed, with a transparent outside."""
    shape = squircle(a)
    img = _backdrop(a)
    ramp = Image.linear_gradient("L").resize((a, a), Image.BILINEAR)                      # 0 at the top, 255 at the bottom

    ripples = Image.new("L", (a, a), 0)                                                    # sound spreading out from the notes
    d = ImageDraw.Draw(ripples)
    for k, rr in enumerate((0.30, 0.40, 0.51, 0.63)):
        d.ellipse((a * (0.5 - rr), a * (0.5 - rr), a * (0.5 + rr), a * (0.5 + rr)),
                  outline=int(255 * (0.9 - 0.18 * k)), width=max(1, int(a * 0.004)))
    over(img, (170, 210, 255), scaled(ImageChops.multiply(soft_blur(ripples, a * 0.0016), _radial(a, 0.5, 0.5, 0.7, 0.8)), 0.20))

    notes = _notes_mask(a, thick)
    over(img, (110, 170, 255), scaled(soft_blur(notes, a * 0.16), 0.70))                   # light for the glass to pick up
    over(img, (190, 225, 255), scaled(soft_blur(notes, a * 0.06), 0.40))
    behind = img.copy()
    over(img, (6, 8, 56), scaled(soft_blur(_shift(notes, 0, a * 0.034), a * 0.04), 0.62))   # shadows
    over(img, (4, 6, 40), scaled(soft_blur(_shift(notes, 0, a * 0.010), a * 0.008), 0.40))

    # the lens: what is behind, magnified and nudged, frosted, red and blue pulled apart, milky on top, bluer below
    z, dx, dy = 1.20, a * 0.012, -a * 0.016
    lens = behind.crop(tuple(int(v) for v in (a * (1 - 1 / z) / 2 + dx, a * (1 - 1 / z) / 2 + dy,
                                              a * (1 + 1 / z) / 2 + dx, a * (1 + 1 / z) / 2 + dy))).resize((a, a), Image.BICUBIC)
    lens = Image.blend(lens, soft_blur(lens, a * 0.018), 0.55)
    r, g, b, _ = lens.split()
    e = int(max(1, a * 0.004))
    lens = Image.merge("RGBA", (ImageChops.offset(r, e, 0), g, ImageChops.offset(b, -e, 0), Image.new("L", (a, a), 255)))
    over(lens, (86, 96, 255), ramp.point(lambda v: int(v * 0.36)))
    over(lens, (236, 244, 255), ramp.point(lambda v: int(176 - v * 0.52)))
    lens.putalpha(scaled(notes, 0.96))
    img.alpha_composite(lens)

    sheen = Image.new("L", (a, a), 0)                                                      # a reflection across the glass
    ImageDraw.Draw(sheen).polygon([(a * 0.10, a * 0.80), (a * 0.30, a * 0.80), (a * 0.80, a * 0.16), (a * 0.60, a * 0.16)], fill=255)
    over(img, (255, 255, 255), scaled(ImageChops.multiply(soft_blur(sheen, a * 0.035), notes), 0.30))

    em = max(1.0, a * 0.0058)                                                              # the edges
    lit = ImageChops.subtract(notes, _shift(notes, em, em * 1.2))                          # facing the light
    glint = ImageChops.subtract(notes, _shift(notes, em * 5, em * 6))
    low = ImageChops.subtract(notes, _shift(notes, -em * 1.4, -em * 1.8))                  # facing away
    low_in = ImageChops.subtract(notes, _shift(notes, -em * 6, -em * 7))
    rim = ImageChops.subtract(notes, notes.filter(ImageFilter.MinFilter(max(3, int(em * 1.4) // 2 * 2 + 1))))
    over(img, (120, 210, 255), scaled(ImageChops.multiply(soft_blur(low_in, em * 3), notes), 0.55))     # light that came through
    over(img, (255, 255, 255), scaled(ImageChops.multiply(soft_blur(glint, em * 2.4), notes), 0.45))
    over(img, (255, 255, 255), scaled(ImageChops.multiply(soft_blur(rim, em * 0.4), notes), 0.55))
    over(img, (190, 230, 255), scaled(ImageChops.multiply(soft_blur(low, em * 0.6), notes), 0.70))
    over(img, (255, 255, 255), scaled(ImageChops.multiply(soft_blur(lit, em * 0.35), notes), 1.0))
    over(img, (60, 200, 255), scaled(soft_blur(ImageChops.subtract(_shift(notes, em * 1.3, em * 1.3), notes), em * 0.8), 0.45))
    over(img, (255, 90, 200), scaled(soft_blur(ImageChops.subtract(_shift(notes, -em * 1.3, -em * 1.3), notes), em * 0.8), 0.40))

    gloss = Image.new("L", (a, a), 0)                                                      # over the whole icon: upper half
    ImageDraw.Draw(gloss).ellipse((-0.34 * a, -0.82 * a, 1.34 * a, 0.56 * a), fill=255)
    fall = ramp.transpose(Image.FLIP_TOP_BOTTOM).point(lambda v: int(_smooth(0.0, 1.0, max(0.0, (v / 255 - 0.40) / 0.60)) * 255))
    over(img, (255, 255, 255), scaled(ImageChops.multiply(ImageChops.multiply(soft_blur(gloss, a * 0.025), shape), fall), 0.20))

    w = max(2, int(a * 0.010))                                                             # rim light round the shape
    inner = Image.new("L", (a, a), 0)
    inner.paste(squircle(a - 2 * w), (w, w))
    diag = ramp.transpose(Image.FLIP_TOP_BOTTOM)
    light = ImageChops.add(diag.point(lambda v: int(v * 0.9)), Image.new("L", (a, a), 30))
    over(img, (255, 255, 255), scaled(ImageChops.multiply(ImageChops.subtract(shape, inner), light), 0.75))
    inner2 = Image.new("L", (a, a), 0)
    inner2.paste(squircle(a - 4 * w), (2 * w, 2 * w))
    over(img, (10, 12, 60), scaled(soft_blur(ImageChops.subtract(inner, inner2), w * 0.6), 0.30))
    img.putalpha(shape)
    return img


def app_icon(size, margin=0.0):
    """The icon at `size` px. `margin` leaves a transparent border (with a soft shadow)."""
    key = (size, round(margin, 4))
    if key in _CACHE:
        return _CACHE[key].copy()
    out = _remembered(size, margin)
    if out is not None:
        _CACHE[key] = out
        return out.copy()
    ss = 4 if size <= 96 else 3 if size <= 300 else 2
    s = size * ss
    a = int(round(s * (1 - 2 * margin)))
    art = _art(a, thick=1.0 if size >= 64 else 1.0 + 0.45 * (64 - size) / 64)
    canvas = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    off = (s - a) // 2
    if margin > 0:
        shadow = Image.new("L", (s, s), 0)
        shadow.paste(art.getchannel("A"), (off, off + int(s * 0.012)))
        over(canvas, (0, 0, 0), scaled(soft_blur(shadow, s * 0.018), 0.34))
    canvas.alpha_composite(art, (off, off))
    out = canvas.resize((size, size), Image.LANCZOS)
    if len(_CACHE) > 12:
        _CACHE.clear()
    _CACHE[key] = out
    _remember(out, size, margin)
    return out.copy()


def ico_file():
    """A multi-size .ico of the icon in the cache folder (written once per drawing), or None if it can't be written. Each
    size is drawn for itself, so the small ones keep their thicker strokes instead of being shrunk from the big one."""
    try:
        folder = os.path.join(platform_.cache_dir(), "icons")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"app-{_fingerprint()}.ico")
        if not os.path.exists(path):
            for old in os.listdir(folder):
                if old.startswith("app-") and old.endswith(".ico"):
                    try:
                        os.remove(os.path.join(folder, old))
                    except OSError:
                        pass
            pics = [app_icon(n) for n in ICO_SIZES]
            tmp = f"{path}.{os.getpid()}.tmp"
            pics[-1].save(tmp, "ICO", sizes=[(n, n) for n in ICO_SIZES], append_images=pics[:-1])
            os.replace(tmp, path)
        return path
    except Exception:                                      # noqa: BLE001 — the 256-px picture is used instead
        log.debug("icon .ico not written", exc_info=True)
        return None
