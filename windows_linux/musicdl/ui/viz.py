"""
viz.py — the pictures of the Live dashboard: a speed ribbon that is extruded into depth, pillars for the processor
cores, a capsule of cells for the songs in flight, range bars for how fast the servers answer.

All of them are plain functions: numbers in, a transparent RGBA picture out (no Tk, no text — the labels are drawn by
the window so they match the rest of the UI). They are drawn on a worker thread (painter.py), so they may take a few
milliseconds without the window noticing; Pillow lets go of the interpreter lock for its heavy steps.

The 3D is one idea used consistently: a fixed, gentle oblique view (the depth runs up and to the right) with a light
from the upper left, so a surface that faces up is brightest, the one facing right is darkest. Nothing moves in
perspective; it is just enough depth to make the numbers feel like objects.
"""
import math

from PIL import Image, ImageChops, ImageDraw

from . import glass as gk
from .charts import smooth

VIOLET = (176, 124, 255)
SLOT_COLORS = {"find": (100, 210, 255), "get": (10, 132, 255), "make": (176, 124, 255), "tag": (48, 209, 88),
               "wait": (255, 176, 46)}
HEAT = ("Cool", "Warm", "Hot", "Very hot")


def supersample(w, h):
    """3x for small pictures, 2x for big ones (the work grows with the area)."""
    return 3 if w * h <= 200_000 else 2


def _canvas(w, h, ss, tint=(0, 0, 0)):
    return Image.new("RGBA", (w * ss, h * ss), tuple(int(v) for v in tint[:3]) + (0,))


DENSITY = 1                                                               # pixels per point of the pictures handed out


def _done(img, w, h):
    """The supersampled picture brought down to `w` x `h` points: DENSITY pixels to each (a Retina window is given more
    of what was drawn rather than having it stretched later). The picture says so in `dens`."""
    d = DENSITY
    if img.size != (w * d, h * d):
        img = img.resize((w * d, h * d), Image.BOX)
    img.dens = d
    return img


def _over(img, color, mask, a=1.0, at=(0, 0)):
    """Lay a flat colour through an 'L' mask (`a` of it) over `img`, properly: the result is right where `img` is
    see-through, which Image.paste with a mask is not."""
    mask = mask if a >= 1.0 else gk.scaled(mask, a)
    x, y = at
    if x < 0 or y < 0:                                                       # (alpha_composite takes no negative offsets)
        mask = mask.crop((max(0, -x), max(0, -y), mask.width, mask.height))
        at = (max(0, x), max(0, y))
    if mask.width > 0 and mask.height > 0:
        gk.over(img, color, mask, at)


def _poly_mask(size, pts):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    return m


def _lit(color, k):
    """`color` scaled by a brightness factor (above 1 goes toward white a little, so a lit face glows rather than clips)."""
    if k <= 1:
        return tuple(int(c * k) for c in color[:3])
    t = min(1.0, (k - 1) * 0.9)
    return tuple(int(c + (255 - c) * t) for c in color[:3])


_CACHE = {}


def _cached(key, make, limit=12):
    """Pictures that depend only on a size (the empty room of a chart, a fade) are made once. Used by the painter's
    thread only, so no lock."""
    got = _CACHE.get(key)
    if got is None:
        if len(_CACHE) >= limit:
            _CACHE.clear()
        got = _CACHE[key] = make()
    return got


def tier_colors(th, n):
    """One colour per kind of processor core, fastest first."""
    return {1: [th["accent"]], 2: [th["accent"], th["info"]]}.get(n, [VIOLET, th["accent"], th["info"]] + [th["ok"]] * 8)[:n]


def heat_word(level):
    return HEAT[max(0, min(len(HEAT) - 1, int(level)))]


# ---------------------------------------------------------------- the speed ribbon

def ribbon_geometry(w, h):
    """(dx, dy, x0, x1, y_top, y_bottom) of the ribbon's front plane in pixels: the depth runs up and to the right by
    (dx, dy), so the plane leaves room for it on the right and at the top."""
    dx = int(max(9, min(34, w * 0.036)))
    dy = int(dx * 0.55)
    return dx, dy, 2, max(4, w - dx), dy + 3, max(dy + 8, h - 3)


def ribbon_levels(w, h):
    """The y (in pixels of the picture) of the 0, 1/3, 2/3 and top gridlines on the ribbon's front plane."""
    _dx, _dy, _x0, _x1, yt, yb = ribbon_geometry(w, h)
    return [yb - (yb - yt) * k / 3.0 for k in range(4)]


def _room(w, h, ss, th, S):
    """The empty chart: a floor, a back wall, a gridline per level on the wall joined to the front edge at the left."""
    dx, dy, x0, x1, yt, yb = ribbon_geometry(w, h)
    W, H = w * ss, h * ss
    X0, X1, YT, YB, DX, DY = x0 * ss, x1 * ss, yt * ss, yb * ss, dx * ss, dy * ss
    ink = (255, 255, 255) if th["dark"] else (0, 0, 0)
    img = _canvas(w, h, ss)
    _over(img, ink, _poly_mask((W, H), [(X0, YB), (X1, YB), (X1 + DX, YB - DY), (X0 + DX, YB - DY)]), 0.07)
    _over(img, ink, _poly_mask((W, H), [(X0 + DX, YT - DY), (X1 + DX, YT - DY), (X1 + DX, YB - DY), (X0 + DX, YB - DY)]),
          0.035)
    lines = Image.new("L", (W, H), 0)
    ld = ImageDraw.Draw(lines)
    lw = max(1, int(round(S * ss * 0.8)))
    for k in range(4):
        y = YB - (YB - YT) * k / 3.0
        ld.line((X0 + DX, y - DY, X1 + DX, y - DY), fill=255, width=lw)
        ld.line((X0, y, X0 + DX, y - DY), fill=255, width=lw)
    ld.line((X0, YB, X1, YB), fill=255, width=lw)                           # the front edge of the floor
    ld.line((X1, YB, X1 + DX, YB - DY), fill=255, width=lw)
    _over(img, ink, lines, 0.16 if th["dark"] else 0.20)
    return img


def ribbon_chart(w, h, samples, ceiling, th, S, slots=120):
    """Download speed over the last minute as a ribbon with depth. `samples` are bytes/second, oldest first, the newest
    at the right; `ceiling` is the value of the top gridline (the labels beside the picture are for 0, 1/3, 2/3 and 1
    of it, at the heights `ribbon_levels` gives)."""
    ss = supersample(w, h)
    W, H = w * ss, h * ss
    img = _cached(("room", w, h, ss, th["dark"], round(S, 2)), lambda: _room(w, h, ss, th, S)).copy()
    if not samples:
        return _done(img, w, h)
    dx, dy, x0, x1, yt, yb = ribbon_geometry(w, h)
    X0, X1, YT, YB, DX, DY = x0 * ss, x1 * ss, yt * ss, yb * ss, dx * ss, dy * ss
    acc, acc2 = th["accent"], th["accent2"]
    n = len(samples)
    step = (X1 - X0) / max(1, slots - 1)
    pts = [(X1 - (n - 1 - i) * step, YB - (YB - YT) * min(1.0, v / ceiling)) for i, v in enumerate(samples)]
    if n > 2:
        pts = smooth(pts, 3)
        pts = [(x, min(YB, max(YT, y))) for x, y in pts]
    elif n == 1:
        pts = [(pts[0][0] - step, pts[0][1])] + pts
    # the extruded top: every piece of the curve swept back into the depth, lit by how it tilts toward the light
    top = _canvas(w, h, ss)
    td = ImageDraw.Draw(top)
    last = max(1, len(pts) - 1)
    for i in range(len(pts) - 1):
        (xa, ya), (xb, yb_) = pts[i], pts[i + 1]
        t = i / last
        slope = max(-1.0, min(1.0, (yb_ - ya) / max(1e-6, xb - xa)))        # screen y grows downward: positive = falling
        col = _lit(gk.mix(acc2, acc, t), 0.92 - 0.15 * slope) + (int(70 + 150 * t),)
        td.polygon([(xa, ya), (xb, yb_), (xb + DX, yb_ - DY), (xa + DX, ya - DY)], fill=col)
    img.alpha_composite(top)
    rim = Image.new("L", (W, H), 0)
    ImageDraw.Draw(rim).line([(x + DX, y - DY) for x, y in pts], fill=255, width=max(1, int(round(S * ss))), joint="curve")
    _over(img, (255, 255, 255), ImageChops.multiply(rim, _cached(("fade-h", W, H, 40), lambda: gk.hgrad(W, H, 40, 255))),
          0.6)
    # the end of the ribbon at "now": a face the colour of the side that is turned away from the light
    xe, ye = pts[-1]
    _over(img, _lit(acc2, 0.6), _poly_mask((W, H), [(xe, ye), (xe + DX, ye - DY), (xe + DX, YB - DY), (xe, YB)]), 0.85)
    # the front: the area under the curve, fading downward and (with time) to the left
    area = _poly_mask((W, H), pts + [(xe, YB), (pts[0][0], YB)])
    fade = _cached(("fade", W, H), lambda: ImageChops.multiply(gk.vgrad(W, H, 235, 60, 1.0), gk.hgrad(W, H, 100, 255)))
    _over(img, acc, ImageChops.multiply(area, fade), 0.95)
    # the line on the front edge, brighter toward now
    line = Image.new("L", (W, H), 0)
    ImageDraw.Draw(line).line(pts, fill=255, width=max(2, int(round(2.4 * S * ss))), joint="curve")
    _over(img, _lit(acc, 1.3), ImageChops.multiply(line, _cached(("fade-h", W, H, 110), lambda: gk.hgrad(W, H, 110, 255))))
    r = 3.4 * S * ss
    pad = int(r * 6)
    halo = Image.new("L", (pad * 2, pad * 2), 0)
    ImageDraw.Draw(halo).ellipse((pad - r * 2.6, pad - r * 2.6, pad + r * 2.6, pad + r * 2.6), fill=255)
    _over(img, acc, gk.soft_blur(halo, r * 1.4), 0.65, (int(xe) - pad, int(ye) - pad))
    d = ImageDraw.Draw(img)
    d.ellipse((xe - r, ye - r, xe + r, ye + r), fill=tuple(acc) + (255,))
    d.ellipse((xe - r * 0.45, ye - r * 0.45, xe + r * 0.45, ye + r * 0.45), fill=(255, 255, 255, 240))
    return _done(img, w, h)


# ---------------------------------------------------------------- the processor cores

def pillar_layout(w, h, sizes):
    """Where the core pillars stand. `sizes` is the number of cores in each group (fastest kind first).

    Returns {'pillars': [(group, index, x, width)], 'spans': [(x0, x1) per group], 'dx', 'dy', 'floor', 'top'} with x/width
    of the front face in pixels; the depth of each pillar runs up and to the right by (dx, dy). Everything stays inside
    (0, 0, w, h)."""
    sizes = [max(0, int(s)) for s in sizes if s]
    n = sum(sizes)
    out = {"pillars": [], "spans": [], "dx": 0, "dy": 0, "floor": max(1, h - 2), "top": 2}
    if not n:
        return out
    g = len(sizes)
    # a pillar is 1 unit wide, the gap between two is 0.55, the gap between two groups 1.1; the depth is 0.45 of a pillar
    units = n + 0.55 * (n - g) + 1.1 * (g - 1) + 0.45
    u = max(2.0, min(40.0, (w - 4) / units))
    pw = max(2, int(u))
    dx = max(1, int(round(pw * 0.45)))
    dy = max(1, int(round(dx * 0.6)))
    x = 2.0
    for gi, size in enumerate(sizes):
        gx = x
        for ci in range(size):
            out["pillars"].append((gi, ci, int(round(x)), pw))
            x += u * 1.55 if ci < size - 1 else u
        out["spans"].append((int(round(gx)), int(round(x)) + dx))
        x += u * 1.1
    out.update(dx=dx, dy=dy, floor=max(dy + 4, h - 2 - dy // 2), top=dy + 2)
    return out


def core_pillars(w, h, groups, th, S):
    """Glass pillars, one per core, filled to how busy the core is. `groups` is [(name, [load 0..1, ...])], fastest
    kind first (a single group for a computer whose cores are all alike)."""
    ss = 3 if w * h <= 600_000 else 2
    lay = pillar_layout(w, h, [len(loads) for _n, loads in groups])
    img = _canvas(w, h, ss)
    if not lay["pillars"]:
        return _done(img, w, h)
    dx, dy, floor, top = lay["dx"] * ss, lay["dy"] * ss, lay["floor"] * ss, lay["top"] * ss
    ink = (255, 255, 255) if th["dark"] else (0, 0, 0)
    cols = tier_colors(th, len(groups))
    for (a, b), col in zip(lay["spans"], cols):                            # a slab under each group
        a, b = a * ss - 3 * ss, b * ss + 3 * ss
        m = _poly_mask(img.size, [(a, floor), (b - dx, floor), (b, floor - dy), (a + dx, floor - dy)])
        _over(img, col, m, 0.12 if th["dark"] else 0.16)
    glass = _canvas(w, h, ss)
    d = ImageDraw.Draw(glass)
    height = floor - top
    loads = [min(1.0, max(0.0, v)) for _n, row in groups for v in row]
    ghost = 0 if th["dark"] else -8                                         # (dark ink is faint already; black on light is not)
    for (_gi, _ci, x, pw) in lay["pillars"]:                               # the empty glass: the whole pillar, very faint
        x, pw = x * ss, pw * ss
        d.polygon([(x + pw, top), (x + pw + dx, top - dy), (x + pw + dx, floor - dy), (x + pw, floor)], fill=ink + (12 + ghost,))
        d.polygon([(x, top), (x + pw, top), (x + pw, floor), (x, floor)], fill=ink + (22 + ghost,))
        d.polygon([(x, top), (x + dx, top - dy), (x + pw + dx, top - dy), (x + pw, top)], fill=ink + (38 + ghost,))
    for (gi, _ci, x, pw), load in zip(lay["pillars"], loads):
        x, pw = x * ss, pw * ss
        col = cols[gi]
        fh = max(2 * ss, int(height * load))
        yt = floor - fh
        k = 0.78 + 0.32 * load                                              # a busier core is a brighter one
        d.polygon([(x + pw, yt), (x + pw + dx, yt - dy), (x + pw + dx, floor - dy), (x + pw, floor)],
                  fill=_lit(col, k * 0.62) + (240,))                       # the side, away from the light
        d.polygon([(x, yt), (x + pw, yt), (x + pw, floor), (x, floor)], fill=_lit(col, k) + (240,))
        d.polygon([(x, yt), (x + dx, yt - dy), (x + pw + dx, yt - dy), (x + pw, yt)], fill=_lit(col, k * 1.38) + (250,))
        if fh > 5 * ss:                                                     # a thin highlight down the lit edge
            d.line((x + ss, yt + 2 * ss, x + ss, floor - 2 * ss), fill=(255, 255, 255, 70), width=max(1, ss))
    img.alpha_composite(glass)
    return _done(img, w, h)


# ---------------------------------------------------------------- the songs in flight

def capsule_cells(w, n, gap=3):
    """(x, width) of each of `n` cells that fill a capsule `w` px wide (the widths differ by at most one pixel)."""
    n = max(1, n)
    gap = min(gap, max(0, (w - n) // max(1, n - 1))) if n > 1 else 0
    cell = (w - gap * (n - 1)) / n
    return [(int(round(i * (cell + gap))), max(1, int(round((i + 1) * cell + i * gap)) - int(round(i * (cell + gap)))))
            for i in range(n)]


def slot_capsule(w, h, cells, th, S):
    """One glossy cell per place a song can be worked on at once: the colour of what that song is doing, or an empty
    glass for a free place. `cells` is a list of colour names (SLOT_COLORS) or None."""
    ss = 3
    img = _canvas(w, h, ss)
    if w < 4 or h < 4:
        return _done(img, w, h)
    cells = list(cells)[:48] or [None]
    for (x, cw), kind in zip(capsule_cells(w, len(cells), max(2, int(3 * S))), cells):
        r = min(cw, h) * 0.5
        big = gk.shape_mask(cw * ss, h * ss, int(r * ss)) if cw > 2 else Image.new("L", (cw * ss, h * ss), 255)
        if kind is None:
            _over(img, (255, 255, 255) if th["dark"] else (0, 0, 0), big, 0.08, (x * ss, 0))
            continue
        col = SLOT_COLORS.get(kind, th["accent"])
        _over(img, _lit(col, 0.86), big, 0.95, (x * ss, 0))
        gloss = ImageChops.multiply(big, gk.vgrad(cw * ss, h * ss, 120, 0, 0.55))      # light from above
        _over(img, (255, 255, 255), gloss, 0.55, (x * ss, 0))
        shade = ImageChops.multiply(big, gk.vgrad(cw * ss, h * ss, 0, 90, 1.0))
        _over(img, (0, 0, 0), shade, 0.35, (x * ss, 0))
    return _done(img, w, h)


# ---------------------------------------------------------------- how fast the servers answer

LOG_LO, LOG_HI = 20.0, 4000.0                                               # ms: the ends of the scale
TICKS = (50, 100, 250, 500, 1000, 2000)


def log_x(ms, w):
    """Where `ms` falls on a logarithmic scale that is `w` wide (a server answering in 40 ms and one in 400 ms are as far
    apart as 400 and 4000: what matters is by how many times)."""
    ms = min(LOG_HI, max(LOG_LO, ms))
    return (math.log(ms) - math.log(LOG_LO)) / (math.log(LOG_HI) - math.log(LOG_LO)) * w


def server_color(ms, th):
    return th["ok"] if ms < 300 else th["warn"] if ms < 900 else th["bad"]


def range_bars(w, h, rows, row_h, th, S):
    """One row per server: a bar from how fast it usually answers (the dot) to how slow it sometimes is, on a log scale.
    `rows` is [(typical_ms, slow_ms)], None for a server that has not been measured yet."""
    ss = 3
    img = _canvas(w, h, ss)
    if w < 8 or not rows:
        return _done(img, w, h)
    ink = (255, 255, 255) if th["dark"] else (0, 0, 0)
    W, H = w * ss, h * ss
    ticks = Image.new("L", (W, H), 0)
    td = ImageDraw.Draw(ticks)
    for t in TICKS:
        x = int(log_x(t, W))
        td.line((x, 0, x, H), fill=255, width=max(1, ss // 2))
    _over(img, ink, ticks, 0.08)
    d = ImageDraw.Draw(img)
    bar = max(4, int(row_h * 0.28)) * ss
    for i, row in enumerate(rows):
        cy = int((i + 0.5) * row_h * ss)
        d.rounded_rectangle((0, cy - bar // 2, W - 1, cy + bar // 2), radius=bar // 2, fill=tuple(ink) + (14,))
        if not row or row[0] is None:
            continue
        typ = row[0]
        slow = max(typ, row[1] if row[1] is not None else typ)
        xa, xb = int(log_x(typ, W)), int(log_x(slow, W))
        col = server_color(slow, th)
        d.rounded_rectangle((max(0, xa - bar // 2), cy - bar // 2, min(W - 1, max(xb, xa + bar)), cy + bar // 2),
                            radius=bar // 2, fill=_lit(col, 0.9) + (225,))
        r = int(bar * 0.95)
        pad = r * 3
        halo = Image.new("L", (pad * 2, pad * 2), 0)
        ImageDraw.Draw(halo).ellipse((pad - r * 1.8, pad - r * 1.8, pad + r * 1.8, pad + r * 1.8), fill=255)
        _over(img, col, gk.soft_blur(halo, r * 0.9), 0.55, (xa - pad, cy - pad))
        d.ellipse((xa - r, cy - r, xa + r, cy + r), fill=_lit(col, 1.1) + (255,))
        d.ellipse((xa - r * 0.42, cy - r * 0.42, xa + r * 0.42, cy + r * 0.42), fill=(255, 255, 255, 235))
    return _done(img, w, h)
