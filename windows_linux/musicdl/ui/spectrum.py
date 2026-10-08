"""
spectrum.py — Advanced mode's picture: what the chosen format keeps of the sound.

A row of bars is the spectrum of a piece of music, low notes on the left, high on the right (the scale is a square root
of the frequency, so the bass has room and the top octave is not squeezed into a corner). The bars that the format keeps
are lit and shimmer; the ones above its cut-off are left as faint outlines. A glowing node marks the cut-off and glides
along the top of the bars when the format, bitrate or sample rate changes. How deep the bars are lit says what the bit
depth gives: the part below the noise floor is dim, and 24 bits pushes that floor off the bottom.

It is the cousin of Optimized mode's quality curve, not a copy: that one shows what a preset costs, this one shows what
the settings keep.

  target(fmt, kbps, rate, depth)   (cut-off Hz, dynamic range dB) the settings give — pure, and what the tests check
  image(w, h, view, th, d, t)      the picture at `d` pixels to the point (RGBA, marked `dens`)
  Spectrum                         the part of the window that keeps it up to date (text, gliding, idle shimmer)

The cut-offs of the lossy formats are typical figures for LAME (MP3) and ffmpeg's AAC encoder, not measurements of a
particular file: the readout says "about".
"""
import math
import time

from PIL import Image, ImageChops, ImageDraw

from . import glass as gk
from .painter import PAINTER

TOP_HZ = 48000.0                                        # the right edge: half of the fastest sample rate we write
PEAK = {"mp3": {96: 15000, 128: 16000, 160: 17500, 192: 18600, 224: 19500, 256: 20000, 320: 20500},
        "aac": {96: 15000, 128: 17000, 160: 18500, 192: 19500, 224: 20500, 256: 21000, 320: 22000}}
LOSSY_RANGE = 96.0                                      # a lossy file is decoded to 16 bits
SPAN_DB = 144.0                                         # the bars' full height: 24 bits
FPS = 20.0


def target(fmt_key, kbps, rate, depth):
    """(cut-off in Hz, dynamic range in dB) of the settings. A sample rate of 0 means the source's, taken as 44.1 kHz."""
    nyq = (rate or 44100) / 2.0 * 0.995
    if fmt_key in PEAK:
        table = PEAK[fmt_key]
        near = min(table, key=lambda b: abs(b - (kbps or 192)))
        return float(min(table[near], nyq)), LOSSY_RANGE
    return float(min(TOP_HZ, nyq)), (SPAN_DB if depth == 24 else LOSSY_RANGE)


def settings_target(s):
    out = s.out_format()
    return target(out.key, out.kbps, out.sample_rate, out.bit_depth)


def to_x(hz):
    """0..1 along the bars for a frequency."""
    return math.sqrt(max(0.0, min(TOP_HZ, hz)) / TOP_HZ)


def to_hz(x):
    return TOP_HZ * max(0.0, min(1.0, x)) ** 2


PEAKS = ((60, 0.55, 9.0), (180, 0.45, 5.0), (500, 0.5, 9.0), (1800, 0.4, 7.0), (3600, 0.35, 9.0), (9000, 0.45, 6.0))


def level_db(hz):
    """How loud music is at a frequency, in dB under its loudest part: bass-heavy, easing off by about 6 dB an octave,
    with a few humps where voices and instruments sit."""
    hz = max(8.0, hz)
    base = -4.0 - (4.0 * math.log2(70.0 / hz) if hz < 70 else 6.2 * math.log2(hz / 70.0))
    bumps = sum(gain * math.exp(-((math.log2(hz / centre)) / width) ** 2) for centre, width, gain in PEAKS)
    return base + bumps


def grain(i):
    """A fixed bit of unevenness per bar, so the row reads as a spectrum rather than a fence (dB, -2..2)."""
    return 2.0 * math.sin(i * 12.9898) * math.cos(i * 4.1414 + 1.0)


def bar_count(w):
    return max(28, min(96, int(w / 8.5)))


def shimmer(i, t):
    """A small, smooth, never-repeating-looking wobble for bar `i` (-1..1)."""
    return (0.6 * math.sin(t * 1.9 + i * 0.61) + 0.4 * math.sin(t * 3.1 - i * 1.27 + 1.3))


def _smooth(v):
    v = max(0.0, min(1.0, v))
    return v * v * (3 - 2 * v)


_GRAD = {}


def _gradient(size, top, bottom):
    key = (size, tuple(top), tuple(bottom))
    got = _GRAD.get(key)
    if got is None:
        if len(_GRAD) > 8:
            _GRAD.clear()
        w, h = size
        got = _GRAD[key] = Image.composite(Image.new("RGB", size, tuple(top[:3])), Image.new("RGB", size, tuple(bottom[:3])),
                                           gk.vgrad(w, h, 255, 0, 1.0))
    return got


_STILL = {}                                                             # what a picture keeps from frame to frame
_NODE = {}


def _still(w, h, th, d, floor):
    """The parts of the picture that don't move with time: the faint outline of every bar, the gradient the lit bars are
    cut from, the dim zone under the noise floor and its dotted line. Made once (at a higher resolution, then shrunk) and
    reused for every frame; a frame only draws the lit bars, which shimmer."""
    W, H = max(1, int(w * d)), max(1, int(h * d))
    key = (W, H, d, round(floor, 1), tuple(th["fg3"][:3]), tuple(th["fg2"][:3]), tuple(th["accent"][:3]), tuple(th["accent2"][:3]))
    got = _STILL.get(key)
    if got is not None:
        return got
    ss = 3 if d == 1 else 2
    SW, SH = W * ss, H * ss
    top = int(7 * d) * ss
    base = SH
    span = base - top
    n = bar_count(w)
    pitch = SW / n
    bw = pitch * 0.58
    r = bw / 2.0
    ghost = Image.new("L", (SW, SH), 0)
    gd = ImageDraw.Draw(ghost)
    bars = []
    for i in range(n):
        x = (i + 0.5) / n
        env = max(0.0, -(level_db(to_hz(x)) + grain(i)) / SPAN_DB)         # depth under the top, 0..1
        x0 = i * pitch + (pitch - bw) / 2.0
        bars.append((x, env, x0 / ss))                                   # (its left edge in final pixels)
        gd.rounded_rectangle((x0, top + span * env, x0 + bw, base + r), r, fill=255)
    ghost = ghost.resize((W, H), Image.BOX).point(lambda v: int(v * 0.16))
    out = Image.new("RGBA", (W, H), tuple(th["fg3"][:3]) + (0,))
    out.putalpha(ghost)
    yf = (top + span * (floor / SPAN_DB)) / ss                          # (in final pixels from here on)
    has_floor = yf < H - 1
    zone = dots = None
    if has_floor:
        zone = Image.new("L", (W, H), 0)
        ImageDraw.Draw(zone).rectangle((0, yf, W, H), fill=255)
        dots = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dd = ImageDraw.Draw(dots)
        gr, gg, gb = th["fg2"][:3]
        dash, lw = 5 * d, max(1, int(round(d * 0.9)))
        x = 0.0
        while x < W:
            dd.rectangle((x, yf - lw / 2.0, min(W, x + dash), yf + lw / 2.0), fill=(gr, gg, gb, 150))
            x += dash * 2
    colour = _gradient((W, H), th["accent2"], th["accent"]).convert("RGBA")
    if len(_STILL) > 6:
        _STILL.clear()
    got = _STILL[key] = dict(W=W, H=H, ss=ss, n=n, top=top / ss, span=span / ss, pitch=pitch / ss, bw=bw / ss, bars=bars, out=out,
                             yf=yf, zone=zone, dots=dots, colour=colour)
    return got


def _node(th, d):
    """The glowing dot at the cut-off (it doesn't change shape, only place), as a small picture."""
    key = (d, tuple(th["accent"][:3]))
    got = _NODE.get(key)
    if got is None:
        ss = 3 if d == 1 else 2
        rad = 4.6 * d * ss
        box = int(rad * 9)
        glow = Image.new("L", (box, box), 0)
        ImageDraw.Draw(glow).ellipse((box / 2 - rad * 2.4, box / 2 - rad * 2.4, box / 2 + rad * 2.4, box / 2 + rad * 2.4), fill=255)
        glow = gk.scaled(gk.soft_blur(glow, rad * 1.3), 0.6)
        patch = Image.new("RGBA", (box, box), (0, 0, 0, 0))
        gk.over(patch, th["accent"], glow)
        pd = ImageDraw.Draw(patch)
        c = box / 2.0
        pd.ellipse((c - rad, c - rad, c + rad, c + rad), fill=tuple(th["accent"][:3]) + (255,))
        pd.ellipse((c - rad * 0.45, c - rad * 0.45, c + rad * 0.45, c + rad * 0.45), fill=(255, 255, 255, 235))
        got = patch.resize((max(1, box // ss), max(1, box // ss)), Image.BOX)
        if len(_NODE) > 6:
            _NODE.clear()
        _NODE[key] = got
    return got


def image(w, h, view, th, d=1, t=0.0, animate=True):
    """The picture for a `w` x `h` point box. `view` = (x, floor_db): the cut-off's place along the bars (0..1, see
    to_x) and the dynamic range; both may be between two settings while the node glides."""
    cx, floor = view
    st = _still(w, h, th, d, floor)
    W, H, ss, n = st["W"], st["H"], st["ss"], st["n"]
    top, span, pitch, bw = st["top"], st["span"], st["pitch"], st["bw"]
    r = bw / 2.0
    base = float(H)
    soft = 1.8 / n * 2.2                                               # how many bar-widths the cut-off fades over
    lit = Image.new("L", (W * ss, H * ss), 0)                          # the lit bars, drawn big for smooth ends, then shrunk
    ld = ImageDraw.Draw(lit)
    for i, (x, env, x0) in enumerate(st["bars"]):
        keep = _smooth((cx - x) / soft + 0.5)
        if keep > 0.01:
            yy = top + span * (env - (5.5 * shimmer(i, t) / SPAN_DB if animate else 0.0)) if env > 0.03 else top + span * env
            ld.rounded_rectangle((x0 * ss, yy * ss, (x0 + bw) * ss, (base + r) * ss), r * ss, fill=int(255 * keep))
    lit = lit.resize((W, H), Image.BOX)
    if st["zone"] is not None:                                         # below the noise floor the bars are dim
        lit = Image.composite(lit.point(lambda v: int(v * 0.34)), lit, st["zone"])
    layer = st["colour"].copy()
    layer.putalpha(lit)
    out = st["out"].copy()
    out.alpha_composite(layer)
    if st["dots"] is not None:                                          # the floor: a dotted line
        out.alpha_composite(st["dots"])
    # the node and its stem
    nx = cx * W
    fi = cx * n - 0.5                                                   # (the node rides the bars, wobble and all)
    nenv = max(0.0, -(level_db(to_hz(cx)) + grain(fi)) / SPAN_DB) - (5.5 * shimmer(fi, t) / SPAN_DB if animate else 0.0)
    ny = top + span * nenv
    sw, sh = max(1, int(round(d * 0.9))), max(1, int(base - ny))
    gk.over(out, th["accent"], gk.vgrad(sw, sh, 210, 0, 1.0), (int(nx - sw / 2.0), int(ny)))
    patch = _node(th, d)
    _paste(out, patch, (int(nx - patch.width / 2), int(ny - patch.height / 2)))
    out.dens = d
    return out


def _paste(canvas, patch, at):
    """alpha_composite that lets the patch hang over the edges."""
    x, y = at
    if x < 0 or y < 0:
        patch = patch.crop((max(0, -x), max(0, -y), patch.width, patch.height))
        x, y = max(0, x), max(0, y)
    if x >= canvas.width or y >= canvas.height:
        return
    patch = patch.crop((0, 0, canvas.width - x, canvas.height - y))
    if patch.width > 0 and patch.height > 0:
        canvas.alpha_composite(patch, (x, y))


# ---------------------------------------------------------------- the window part
HEIGHT = 124                                                            # points, the whole strip
MIN_CARD = 470                                                          # a shorter card goes without it
TICKS = ((100, "100"), (1000, "1k"), (5000, "5k"), (10000, "10k"), (20000, "20k"))


class Spectrum:
    """The strip above the Format rows. `tick` runs every frame the tab is showing."""

    def __init__(self, app, rect):
        self.app = app
        self.rect = rect                                                 # (x, y, w, h) in points, canvas coordinates
        x, y, w, h = rect
        self.cw, self.ch = int(w - 32), int(h - 54)
        self.cx, self.cy = x + 16, y + 30
        self.token = ("spec", app.name, self.cw, self.ch, app.D)
        hz, db = settings_target(app.s)
        self.goal = (to_x(hz), db)
        self.cur = self.goal
        self.shown = None
        self.last = 0.0
        self.t0 = time.time()
        self.item = None
        self.text = {}

    def build(self):
        a = self.app
        x, y, w, h = self.rect
        before = set(a.cv.find_withtag("Bcont"))
        a._plate(x, y, w, h, 14)
        th = a.th
        p = a.p
        self.item = a.put(p(self.cx), p(self.cy), a.photo("spec", image(self.cw, self.ch, self.cur, th, a.D, 0.0), a.D),
                          tags="Bcont")
        self.text["cap"] = a.cv.create_text(p(x + 16), p(y + 14), text="WHAT THIS FORMAT KEEPS", anchor="w",
                                            font=a.f_tiny, fill=gk.hx(th["fg3"]), tags="Bcont")
        self.text["read"] = a.cv.create_text(p(x + w - 16), p(y + 14), text="", anchor="e", font=a.f_tiny,
                                             fill=gk.hx(th["fg2"]), tags="Bcont")
        ay = y + h - 12
        for hz, label in TICKS:
            tx = self.cx + self.cw * to_x(hz)
            a.cv.create_text(p(tx), p(ay), text=label, font=a.f_tiny, fill=gk.hx(th["fg3"]), tags="Bcont")
        a.cv.create_text(p(self.cx + self.cw), p(ay), text="Hz", anchor="e", font=a.f_tiny, fill=gk.hx(th["fg3"]),
                         tags="Bcont")
        self._readout()
        for it in set(a.cv.find_withtag("Bcont")) - before:
            a.cv.addtag_withtag("spec", it)
        self.shown = self.cur
        self.last = time.time()

    def _readout(self):
        hz = to_hz(self.goal[0])
        rng = self.goal[1]
        words = f"Up to about {hz / 1000:.1f} kHz".replace(".0 kHz", " kHz") + f"  ·  {int(round(rng))} dB range"
        a = self.app
        room = a.p(self.rect[2] - 32) - a.f_tiny.measure("WHAT THIS FORMAT KEEPS") - a.p(24)
        a.cv.itemconfigure(self.text["read"], text=gk.fit(words, a.f_tiny, max(a.p(60), room)))

    def tick(self, now, dt, force=False):
        a = self.app
        if self.item is None:
            return
        hz, db = settings_target(a.s)
        goal = (to_x(hz), db)
        if goal != self.goal:
            self.goal = goal
            self._readout()
        reduced = a.reduced()
        if reduced:
            self.cur = self.goal
        else:
            k = 1.0 - math.exp(-min(0.1, dt) * 9.0)
            gx, gf = self.goal
            cx, cf = self.cur
            cx += (gx - cx) * k
            cf += (gf - cf) * k
            if abs(gx - cx) < 0.0004:
                cx = gx
            if abs(gf - cf) < 0.05:
                cf = gf
            self.cur = (cx, cf)
        got = PAINTER.take("spec", self.token)
        if got is not None:
            a.cv.itemconfigure(self.item, image=a.photo("spec", got, getattr(got, "dens", a.D)))
        moving = self.cur != self.shown
        if (moving or force or (not reduced and now - self.last >= 1.0 / FPS)) and not PAINTER.pending("spec"):
            self.last, self.shown = now, self.cur
            PAINTER.submit("spec", self.token, image, self.cw, self.ch, self.cur, a.th, a.D, now - self.t0, not reduced)
