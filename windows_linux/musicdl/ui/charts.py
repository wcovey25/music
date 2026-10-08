"""
charts.py — the three little graphs, drawn with Pillow (graphics only; axis labels are drawn by the window so the
text matches the rest of the UI):

  speed_chart     live download speed: smooth line, soft area fill, glowing "now" dot
  sparkline       a tiny trend line for searches/min and API latency
  tradeoff_chart  Easy mode: the storage-vs-quality curve with one node per preset and a dot that glides between them
"""
import math

from PIL import Image, ImageChops, ImageDraw

from . import glass as gk

SS = 3                                                       # supersampling for smooth edges


# ---------------------------------------------------------------- curve helpers

def nice_ceiling(v, floor=64 * 1024):
    """Round a data rate up to a 1-2-5 step so the axis reads 'nicely' (and never collapses to zero)."""
    v = max(v, floor)
    unit = 1048576.0 if v >= 1048576 else 1024.0           # the axis is labelled in KB/s or MB/s
    u = v / unit
    mag = 10 ** math.floor(math.log10(u))
    for m in (1, 2, 2.5, 5, 10):
        if u <= m * mag + 1e-9:
            return m * mag * unit
    return 10 * mag * unit


def smooth(points, per=4):
    """Catmull-Rom through the points (x must increase), `per` extra samples per segment."""
    if len(points) < 3:
        return list(points)
    pts = [points[0]] + list(points) + [points[-1]]
    out = []
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        for k in range(per):
            t = k / per
            t2, t3 = t * t, t * t * t
            out.append(tuple(0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2
                                    + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3) for j in (0, 1)))
    out.append(points[-1])
    return out


def pchip(xs, ys):
    """Monotone cubic interpolation (Fritsch-Carlson): passes through every point and never overshoots."""
    n = len(xs)
    h = [xs[i + 1] - xs[i] for i in range(n - 1)]
    d = [(ys[i + 1] - ys[i]) / h[i] for i in range(n - 1)]
    m = [0.0] * n
    m[0], m[-1] = d[0], d[-1]
    for i in range(1, n - 1):
        m[i] = 0.0 if d[i - 1] * d[i] <= 0 else 2 / (1 / d[i - 1] + 1 / d[i])

    def f(x):
        x = max(xs[0], min(xs[-1], x))
        i = max(0, min(n - 2, next((k for k in range(n - 1) if x <= xs[k + 1]), n - 2)))
        t = (x - xs[i]) / h[i]
        t2, t3 = t * t, t * t * t
        return ((2 * t3 - 3 * t2 + 1) * ys[i] + (t3 - 2 * t2 + t) * h[i] * m[i]
                + (-2 * t3 + 3 * t2) * ys[i + 1] + (t3 - t2) * h[i] * m[i + 1])
    return f


_GRADS = {}


def _vgrad(*a):
    """Gradient masks never change for a given size, and the charts redraw on every hover: keep the last few."""
    return _cached(("v",) + a, gk.vgrad, a)


def _hgrad(*a):
    return _cached(("h",) + a, gk.hgrad, a)


def _cached(key, fn, a):
    got = _GRADS.get(key)
    if got is None:
        if len(_GRADS) > 16:
            _GRADS.clear()
        got = _GRADS[key] = fn(*a)
    return got


def _canvas(w, h):
    return Image.new("RGBA", (w * SS, h * SS), (0, 0, 0, 0))


def _line_mask(size, pts, width):
    m = Image.new("L", size, 0)
    d = ImageDraw.Draw(m)
    d.line(pts, fill=255, width=width, joint="curve")
    r = width / 2
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - r, y - r, x + r, y + r), fill=255)
    return m


def _glow_dot(img, x, y, r, color, th):
    """A filled dot with a soft halo, drawn into a supersampled RGBA image."""
    reach = int(r * 2.4 + r * 1.3 * 3) + 2                    # the halo and its blur only touch this square
    x0, y0 = max(0, int(x) - reach), max(0, int(y) - reach)
    x1, y1 = min(img.width, int(x) + reach), min(img.height, int(y) + reach)
    halo = Image.new("L", (x1 - x0, y1 - y0), 0)
    ImageDraw.Draw(halo).ellipse((x - x0 - r * 2.4, y - y0 - r * 2.4, x - x0 + r * 2.4, y - y0 + r * 2.4), fill=255)
    halo = gk.soft_blur(halo, r * 1.3)
    gk.over(img, color, gk.scaled(halo, 0.55), at=(x0, y0))
    d = ImageDraw.Draw(img)
    d.ellipse((x - r, y - r, x + r, y + r), fill=tuple(color) + (255,))
    d.ellipse((x - r * 0.45, y - r * 0.45, x + r * 0.45, y + r * 0.45), fill=(255, 255, 255, 235))


# ---------------------------------------------------------------- speed graph

def speed_chart(w, h, samples, ceiling, th, S, slots=120):
    """`samples` are bytes/second, oldest first; the newest sits at the right edge. `ceiling` is the y-axis top."""
    W, H = w * SS, h * SS
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    gr, gg, gb, ga = th["hair"]
    for k in (0, 1, 2, 3):                                   # faint guide lines at 0, 1/3, 2/3, top
        y = int((H - 2 * SS) * (1 - k / 3)) + SS
        d.line((0, y, W, y), fill=(gr, gg, gb, int(255 * ga * (1.4 if k == 0 else 0.8))), width=max(1, int(S * SS * 0.8)))
    if not samples:
        return img.reduce(SS)                                    # exact SS-fold box filter: same smoothness, far cheaper than LANCZOS
    pad_top = 6 * SS
    usable = H - pad_top - 3 * SS
    n = len(samples)
    step = (W - 10 * SS) / max(1, slots - 1)
    x_last = W - 6 * SS
    pts = [(x_last - (n - 1 - i) * step, H - 3 * SS - usable * min(1.0, v / ceiling)) for i, v in enumerate(samples)]
    if len(pts) > 2:
        pts = smooth(pts, 4)
        pts = [(x, min(H - 3 * SS, max(pad_top, y))) for x, y in pts]
    elif len(pts) == 1:
        pts = [(pts[0][0] - step, pts[0][1])] + pts
    # area under the line, fading downwards
    area = Image.new("L", (W, H), 0)
    ImageDraw.Draw(area).polygon(pts + [(pts[-1][0], H), (pts[0][0], H)], fill=255)
    fade = _vgrad(W, H, 110, 0, 1.0)
    gk.over(img, th["accent"], ImageChops.multiply(area, fade))
    # the line itself, brighter toward "now"
    line = _line_mask((W, H), pts, max(2, int(2.2 * S * SS)))
    grad = Image.composite(Image.new("RGB", (W, H), th["accent"]), Image.new("RGB", (W, H), gk.shade(th["accent2"], -10)),
                           _hgrad(W, H, 0, 255))
    layer = grad.convert("RGBA")
    layer.putalpha(line)
    img.alpha_composite(layer)
    _glow_dot(img, pts[-1][0], pts[-1][1], 3.2 * S * SS, th["accent"], th)
    return img.reduce(SS)                                    # exact SS-fold box filter: same smoothness, far cheaper than LANCZOS


def sparkline(w, h, values, th, S, color=None, floor=None, ceiling=None):
    """A bare trend line (newest at the right). Returns RGBA."""
    W, H = w * SS, h * SS
    img = _canvas(w, h)
    color = color or th["accent"]
    vals = list(values)[-60:]
    if len(vals) < 2:
        d = ImageDraw.Draw(img)
        gr, gg, gb, ga = th["hair"]
        d.line((0, H // 2, W, H // 2), fill=(gr, gg, gb, int(255 * ga * 1.6)), width=max(1, int(S * SS)))
        return img.reduce(SS)                                    # exact SS-fold box filter: same smoothness, far cheaper than LANCZOS
    lo = min(vals) if floor is None else floor
    hi = max(vals) if ceiling is None else ceiling
    if hi - lo < 1e-9:
        hi = lo + 1.0
    pad = 4 * SS
    xs = [pad + (W - 2 * pad) * i / (len(vals) - 1) for i in range(len(vals))]
    ys = [H - pad - (H - 2 * pad) * (v - lo) / (hi - lo) for v in vals]
    pts = smooth(list(zip(xs, ys)), 3) if len(vals) > 2 else list(zip(xs, ys))
    pts = [(x, min(H - pad, max(pad, y))) for x, y in pts]
    area = Image.new("L", (W, H), 0)
    ImageDraw.Draw(area).polygon(pts + [(pts[-1][0], H), (pts[0][0], H)], fill=255)
    gk.over(img, color, ImageChops.multiply(area, _vgrad(W, H, 70, 0, 1.0)))
    line = _line_mask((W, H), pts, max(2, int(1.8 * S * SS)))
    layer = Image.new("RGBA", (W, H), tuple(color[:3]) + (0,))
    layer.putalpha(line)
    img.alpha_composite(layer)
    _glow_dot(img, pts[-1][0], pts[-1][1], 2.6 * S * SS, color, th)
    return img.reduce(SS)                                    # exact SS-fold box filter: same smoothness, far cheaper than LANCZOS


# ---------------------------------------------------------------- storage vs quality

class Tradeoff:
    """Geometry of the Easy-mode indicator, shared by the drawing and the click/hover tests.

    x is average data rate (kbps, proportional to file size); y is perceived quality (0-1). The curve is monotone, so
    it visibly flattens: going from Better to Best costs far more space than it gains in quality."""

    def __init__(self, nodes, kbps_max=1000):
        self.nodes = nodes                                    # [(key, kbps, quality)]
        self.kbps_max = kbps_max
        xs = [0.0] + [n[1] for n in nodes]
        ys = [0.0] + [n[2] for n in nodes]
        self.f = pchip(xs, ys)

    def point(self, kbps, w, h, pad):
        x = pad + (w - 2 * pad) * (kbps / self.kbps_max)
        y = h - pad - (h - 2 * pad) * self.f(kbps)
        return x, y


def tradeoff_chart(w, h, tr, pos_kbps, selected, hover, th, S):
    """Graphics for the curve; `pos_kbps` is where the gliding dot is (it eases between preset values)."""
    pad = int(26 * S)
    W, H = w * SS, h * SS
    img = _canvas(w, h)
    d = ImageDraw.Draw(img)
    gr, gg, gb, ga = th["hair"]
    for k in (0, 1, 2):
        y = int((H - 2 * pad * SS) * k / 2) + pad * SS
        d.line((pad * SS, y, W - pad * SS, y), fill=(gr, gg, gb, int(255 * ga)), width=max(1, int(S * SS * 0.8)))
    steps = 90
    curve = [tuple(v * SS for v in tr.point(tr.kbps_max * i / steps, w, h, pad)) for i in range(steps + 1)]
    dot = tuple(v * SS for v in tr.point(pos_kbps, w, h, pad))
    # area up to the dot, fading downward
    done = [p for p in curve if p[0] <= dot[0]] + [dot]
    area = Image.new("L", (W, H), 0)
    ImageDraw.Draw(area).polygon(done + [(dot[0], H - pad * SS), (done[0][0], H - pad * SS)], fill=255)
    gk.over(img, th["accent"], ImageChops.multiply(area, _vgrad(W, H, 130, 0, 1.0)))
    # the whole curve faint, the part up to the dot in accent
    full = _line_mask((W, H), curve, max(2, int(2.0 * S * SS)))
    layer = Image.new("RGBA", (W, H), tuple(th["fg3"][:3]) + (0,))
    layer.putalpha(gk.scaled(full, 0.55))
    img.alpha_composite(layer)
    part = _line_mask((W, H), done if len(done) > 1 else done * 2, max(2, int(2.6 * S * SS)))
    grad = Image.composite(Image.new("RGB", (W, H), th["accent"]), Image.new("RGB", (W, H), gk.shade(th["accent2"], -8)),
                           _hgrad(W, H, 0, 255))
    lay = grad.convert("RGBA")
    lay.putalpha(part)
    img.alpha_composite(lay)
    # one node per preset
    for i, (key, kbps, _q) in enumerate(tr.nodes):
        x, y = (v * SS for v in tr.point(kbps, w, h, pad))
        on, hov = key == selected, key == hover
        r = (5.2 if (on or hov) else 4.2) * S * SS
        fill = tuple(th["accent"]) + (255,) if kbps <= pos_kbps + 1e-6 else tuple(th["fg3"]) + (255,)
        d.ellipse((x - r, y - r, x + r, y + r), fill=fill)
        d.ellipse((x - r * .45, y - r * .45, x + r * .45, y + r * .45), fill=(255, 255, 255, 230))
    _glow_dot(img, dot[0], dot[1], 6.4 * S * SS, th["accent"], th)
    return img.reduce(SS)                                    # exact SS-fold box filter: same smoothness, far cheaper than LANCZOS
