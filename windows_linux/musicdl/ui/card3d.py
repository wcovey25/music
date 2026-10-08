"""
card3d.py — the artwork as a card standing in the window.

It rests turned a few degrees toward the light, with a soft shadow behind it. When the pointer is over it the card turns
toward the pointer (the near edge comes forward, the far side gets a little darker), a sheen slides across the glass the
other way, and the shadow moves with it. When the pointer leaves, it eases back.

How a picture is made: the tile is lit flat first (sheen and shading painted on it), then bent into place by a perspective
transform computed from a true rotation of the card's four corners seen from a camera three card-widths away — so the
edges stay straight and the sides shrink the way they would. The tilt is kept to quarters of the way to the edge, so
there are at most 9 x 9 pictures; each takes a millisecond or two and is kept for the next time the pointer is there.
The tile is premultiplied while it is bent, so its soft edge doesn't pick up a dark fringe.
"""
import math

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from . import glass as gk

MAX_DEG = 12.0                                  # the most the card turns, at the edge of it
STEPS = 4                                       # the tilt is a multiple of 1/STEPS
REST = (-0.25, -0.25)                           # how it sits when nobody is pointing at it (a quantized tilt)
PAD = 2                                         # transparent border round the tile while it is bent (smooth edges)
EASE = 0.18                                     # seconds to turn from one tilt to another


def quantize(v):
    return round(max(-1.0, min(1.0, v)) * STEPS) / STEPS


def margin(size):
    """Room round the tile for the shadow (the picture is this much larger on every side)."""
    return max(4, int(size * 0.26))


def corners(size, tx, ty):
    """Where the tile's corners (top-left, top-right, bottom-right, bottom-left) land, relative to its centre, when it
    is turned by tx (to the right) and ty (down), each -1..1."""
    a, b = math.radians(MAX_DEG * tx), math.radians(MAX_DEG * ty)
    f = size * 3.0
    out = []
    for cx, cy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        x, y = cx * size / 2.0, cy * size / 2.0
        z = x * math.sin(a) + y * math.sin(b)                    # towards the viewer: the edge nearer the pointer
        k = f / (f - z)
        out.append((x * math.cos(a) * k, y * math.cos(b) * k))
    return out


def _solve(a, b):
    """Solve a·x = b (Gauss with partial pivoting)."""
    n = len(b)
    m = [row[:] + [v] for row, v in zip(a, b)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(m[r][i]))
        m[i], m[piv] = m[piv], m[i]
        if abs(m[i][i]) < 1e-12:
            raise ValueError("degenerate corners")
        for r in range(i + 1, n):
            f = m[r][i] / m[i][i]
            for c in range(i, n + 1):
                m[r][c] -= f * m[i][c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (m[i][n] - sum(m[i][c] * x[c] for c in range(i + 1, n))) / m[i][i]
    return x


def coefficients(dst, src):
    """The eight numbers Pillow's PERSPECTIVE transform wants: they send each point of `dst` (the output) to the matching
    point of `src` (the input)."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -x * u, -y * u])
        rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -x * v, -y * v])
        rhs.append(v)
    return _solve(rows, rhs)


def lit(tile, tx, ty):
    """The flat tile with the light on it for this tilt: a sheen that slides against the turn, and the far side darker."""
    s = tile.width
    alpha = tile.getchannel("A")
    out = tile.copy()
    t = s * (1.0 - 0.9 * (0.75 * tx + 0.5 * ty))                  # where the sheen crosses (along the diagonal x + y)
    band = Image.new("L", (s, s), 0)
    d = ImageDraw.Draw(band)
    w = s * 0.17
    d.polygon([(t - w, 0), (t + w, 0), (t + w - s, s), (t - w - s, s)], fill=255)
    band = gk.scaled(gk.soft_blur(band, s * 0.09), 0.20)
    streak = Image.new("L", (s, s), 0)
    t2, w2 = t + s * 0.30, max(1.0, s * 0.012)
    ImageDraw.Draw(streak).polygon([(t2 - w2, 0), (t2 + w2, 0), (t2 + w2 - s, s), (t2 - w2 - s, s)], fill=255)
    streak = gk.scaled(streak.filter(ImageFilter.GaussianBlur(0.7)), 0.16)
    gk.over(out, (255, 255, 255), ImageChops.multiply(ImageChops.add(band, streak), alpha))
    if tx or ty:
        ramp = Image.linear_gradient("L").resize((s, s), Image.BILINEAR)            # 0 at the top, 255 at the bottom
        across = ramp.rotate(90)                                                    # 0 at the left, 255 at the right
        shade = Image.new("L", (s, s), 0)
        for amount, mask in ((tx, across), (ty, ramp)):
            if amount:                                                              # the side that is turned away
                far = ImageChops.invert(mask) if amount > 0 else mask
                shade = ImageChops.add(shade, gk.scaled(far, 0.18 * abs(amount)))
        gk.over(out, (0, 0, 0), ImageChops.multiply(shade, alpha))
    return out


def bend(flat, tx, ty, full):
    """The flat tile turned by (tx, ty), in the middle of a transparent picture `full` pixels square."""
    s = flat.width
    pts = [(full / 2.0 + x, full / 2.0 + y) for x, y in corners(s, tx, ty)]
    padded = Image.new("RGBA", (s + 2 * PAD, s + 2 * PAD), (0, 0, 0, 0))
    padded.paste(flat, (PAD, PAD))
    src = [(PAD, PAD), (PAD + s, PAD), (PAD + s, PAD + s), (PAD, PAD + s)]
    return padded.convert("RGBa").transform((full, full), Image.PERSPECTIVE, coefficients(pts, src),
                                            Image.BICUBIC).convert("RGBA")


class Card:
    """The pictures of one tile at every tilt (made when first asked for)."""

    def __init__(self, tile, th, S):
        self.tile, self.th, self.S = tile.convert("RGBA"), th, S
        self.size = self.tile.width
        self.margin = margin(self.size)
        self._made = {}

    @staticmethod
    def key(tx, ty):
        return quantize(tx), quantize(ty)

    def image(self, tx=0.0, ty=0.0):
        key = self.key(tx, ty)
        got = self._made.get(key)
        if got is None:
            got = self._made[key] = self._draw(*key)
        return got

    def _draw(self, tx, ty):
        s, m = self.size, self.margin
        full = s + 2 * m
        card = bend(lit(self.tile, tx, ty), tx, ty, full)
        pts = [(full / 2.0 + x, full / 2.0 + y) for x, y in corners(s, tx, ty)]
        out = Image.new("RGBA", (full, full), (0, 0, 0, 0))
        dark = self.th["dark"]
        for reach, blur, strength in ((0.075, 0.075, 0.46 if dark else 0.24), (0.02, 0.022, 0.30 if dark else 0.16)):
            k = reach / 0.075                                                   # (the light is at the upper left, so it falls
            dx, dy = s * (0.5 * reach + 0.03 * tx * k), s * (reach + 0.03 * ty * k)      # lower right, further on the raised side)
            sh = Image.new("L", (full, full), 0)
            ImageDraw.Draw(sh).polygon([(x + dx, y + dy) for x, y in pts], fill=255)
            gk.over(out, (0, 0, 0), gk.scaled(gk.soft_blur(sh, max(1.0, s * blur)), strength))
        out.alpha_composite(card)
        return out


class Tilt:
    """A card on the canvas: it follows the pointer while that is over the card and eases back to rest when it leaves.
    With reduced motion it stays at rest."""

    def __init__(self, app, tile, x, y, group="A"):
        self.app = app
        self.card = Card(tile, app.th, app.S)
        self.photos = {}
        self.cur = self.goal = REST
        self.shown = self.card.key(*REST)
        d = app.D                                                           # (the card's pictures have d pixels to the point)
        s, m = self.card.size / d, self.card.margin / d
        self.box = (x, y, x + s, y + s)
        app.stop_anim("tilt")
        self.item = app.put(x - m, y - m, self._photo(self.shown), "nw", group)
        app.region(self.box, group=group, hover=self.hover, track=self.track, sound=None)

    def _photo(self, key):
        ph = self.photos.get(key)
        if ph is None:
            ph = self.photos[key] = self.app.tkphoto(self.card.image(*key), self.app.D)
        return ph

    def _show(self, tilt):
        self.cur = tilt
        key = self.card.key(*tilt)
        if key != self.shown:
            self.shown = key
            self.app.cv.itemconfigure(self.item, image=self._photo(key))

    def aim(self, goal):
        goal = self.card.key(*goal)
        if goal == self.goal:
            return
        self.goal = goal
        a = self.cur
        self.app.animate("tilt", EASE, lambda f: self._show((a[0] + (goal[0] - a[0]) * f, a[1] + (goal[1] - a[1]) * f)))

    def hover(self, on):
        if not on:
            self.aim(REST)

    def track(self, e):
        if self.app.reduced():
            return
        x0, y0, x1, y1 = self.box
        self.aim(((e.x - (x0 + x1) / 2.0) / ((x1 - x0) / 2.0), (e.y - (y0 + y1) / 2.0) / ((y1 - y0) / 2.0)))
