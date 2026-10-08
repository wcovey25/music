"""
splash.py — the launch animation, "Aurora", cut to the launch sound (audio/synth.py TIMELINE).

    0.00  light gathers: an aurora swells behind the window and sparks spiral in
    0.50  the thump: the glass icon turns in out of the dark in 3D and settles with a little spring
    0.62  four bells: a ripple with a coloured fringe leaves the icon for each, and the icon rings
    1.46  the sweep: a highlight crosses the glass while the name appears
    1.78  the chord: the glow blooms
    2.40  the icon flies to its place in the header while the window it was hiding opens

Everything that moves lives in one image of the middle of the window, so each frame costs a small repaint rather than a
full-window one. `Stage` is pure Pillow, with no Tk in it. The mixin below drives it, hides the finished screen behind it, and hands over.
With Motion: Reduced (or Windows' animation effects off) only the icon and the name fade in.
"""
import logging
import math
import random
import threading
import time

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from ..audio.synth import TIMELINE
from . import glass as gk
from .glass import hx, mix, over, scaled, soft_blur

log = logging.getLogger("musicdl")
clock = time.time                    # (a test can stand in for it to step through the animation)
LEAD = 0.022                         # a frame is made this far ahead of the moment it is shown
THUMP, BELLS, SWEEP, CHORD = TIMELINE["thump"], TIMELINE["bells"], TIMELINE["sweep"], TIMELINE["chord"]
LEAVE = CHORD + 0.62                 # the icon sets off for the header and the window opens
FLY = 0.72                           # how long that takes
CALM = dict(leave=0.95, fly=0.40)    # the same hand-over with Reduced motion

PALETTE = ((58, 150, 255), (140, 92, 255), (255, 104, 190), (70, 224, 214))


def clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else hi if x > hi else x


def smooth(a, b, x):
    t = clamp((x - a) / (b - a))
    return t * t * (3 - 2 * t)


def lerp(a, b, t):
    return a + (b - a) * t


def out_cubic(t):
    return 1 - (1 - clamp(t)) ** 3


def spring(t):
    """0 -> 1 with one small overshoot (t in seconds)."""
    return 0.0 if t <= 0 else 1 - math.exp(-7.5 * t) * math.cos(11.0 * t)


def _solve(a, b):
    """Gauss-Jordan for the small systems the perspective maths needs (no numpy)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        d = m[c][c] or 1e-12
        m[c] = [v / d for v in m[c]]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c]
                m[r] = [v - f * w for v, w in zip(m[r], m[c])]
    return [m[i][n] for i in range(n)]


def homography(src, dst):
    """Coefficients for Image.transform(PERSPECTIVE): output corners `dst` take their pixels from `src`."""
    rows, rhs = [], []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rhs.append(u)
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
        rhs.append(v)
    return _solve(rows, rhs)


def pose(sprite, out, scale, yaw, pitch, focal=3.4):
    """The sprite (an 'RGBa' square) turned `yaw` about its vertical axis and `pitch` about its horizontal one, seen in
    perspective, centred in an `out`-px square."""
    n = sprite.width
    half = n / 2
    dst = []
    for ux, uy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        x, y = ux * half, uy * half
        x1, z1 = x * math.cos(yaw), x * math.sin(yaw)
        y2, z2 = y * math.cos(pitch) - z1 * math.sin(pitch), y * math.sin(pitch) + z1 * math.cos(pitch)
        s = focal * half / (focal * half + z2)
        dst.append((out / 2 + x1 * s * scale, out / 2 + y2 * s * scale))
    coef = homography([(0, 0), (n, 0), (n, n), (0, n)], dst)
    return sprite.transform((out, out), Image.PERSPECTIVE, coef, Image.BICUBIC).convert("RGBA")


def _glow(size, power=2.0):
    """'L' radial falloff, 255 in the middle and 0 at the edge."""
    g = Image.new("L", (size, size))
    c = (size - 1) / 2
    g.putdata([int(255 * max(0.0, 1 - math.hypot(i - c, j - c) / c) ** power) for j in range(size) for i in range(size)])
    return g


def _curtain(w, h, low, high, seed):
    """A band of northern light, seamless across its width, bright at the foot and thinning upward."""
    rnd = random.Random(seed)
    waves = [(k, rnd.random() * math.tau, a) for k, a in ((1, 0.10), (2, 0.06), (3, 0.035))]
    rise = int(h * 0.74)
    strips = []
    for level in (0.25, 0.5, 0.75, 1.0):
        strip = Image.new("RGBA", (1, rise))
        px = []
        for y in range(rise):
            u = y / (rise - 1)
            a = smooth(0.0, 0.62, u) ** 1.5 * (1 - smooth(0.66, 1.0, u))
            r, g, b = mix(high, low, u)
            px.append((int(r), int(g), int(b), int(255 * a * level)))
        strip.putdata(px)
        strips.append(strip)
    tex = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    for x in range(w):
        ph = x / w * math.tau
        top = h * 0.13 + sum(a * h * math.sin(k * ph + p0) for k, p0, a in waves)
        fold = 0.5 + 0.5 * math.sin(5 * ph + waves[0][1]) * math.sin(2 * ph + waves[1][1])
        tex.paste(strips[min(3, int(fold * 4))], (x, int(clamp(top, 0, h - rise))))
    return tex.filter(ImageFilter.GaussianBlur(1.1))


def _border(w, h, mx, my):
    """'L' mask that is 0 along the edges and 255 inside, easing over `mx` of the width and `my` of the height."""
    def ramp(n, m):
        m = max(1.0, n * m)
        return bytes(int(255 * smooth(0.0, m, min(i + 0.5, n - i - 0.5))) for i in range(n))
    cols = Image.frombytes("L", (w, 1), ramp(w, mx)).resize((w, h))
    rows = Image.frombytes("L", (1, h), ramp(h, my)).resize((w, h))
    return ImageChops.multiply(cols, rows)


class Stage:
    """The picture of the middle of the window at time `t` (seconds into the animation)."""

    def __init__(self, size, box, scene, th, icon, calm=False, d=1):
        """`size`, `box`, `scene` and `icon` are in pixels, which are `d` to the point (the effects that need to look
        crisp are drawn at full resolution when that is more than one)."""
        self.RW, self.RH = size
        self.x0, self.y0 = box
        self.d = d
        self.th, self.dark, self.calm = th, th["dark"], calm
        self.bg = scene.crop((self.x0, self.y0, self.x0 + self.RW, self.y0 + self.RH)).convert("RGB")
        self.icon = icon
        self.N = icon.width
        self.out = int(self.N * 1.5)
        self.cx, self.cy = self.RW // 2, int(self.RH * 0.43)
        self.sprite = icon.convert("RGBa")
        self.keep = {}
        if calm:
            return
        N, out = self.N, self.out
        self.lw, self.lh = max(8, self.RW // 4), max(8, self.RH // 4)                  # soft layers: a quarter of the pixels
        self.pl = 1 if d > 1 else 2                                                      # ring planes: full size, or half
        self.u = 2.0 * d / self.pl                                                       # (a point's size in a ring plane, vs. the 1x one)
        self.blur = 1.3 if d == 1 else 0.55 * d + 0.3
        self.hw, self.hh = self.RW // self.pl, self.RH // self.pl
        self.curtains = [dict(tex=_curtain(self.lw * 2, self.lh, (70, 150, 255), (150, 90, 255), 5), speed=6.0, k=1.0),
                         dict(tex=_curtain(self.lw * 2, self.lh, (255, 110, 190), (120, 90, 255), 9), speed=-4.0, k=0.62),
                         dict(tex=_curtain(self.lw * 2, self.lh, (80, 230, 215), (70, 140, 255), 14), speed=9.0, k=0.45)]
        feather = Image.new("L", (self.lw, self.lh), 0)
        ImageDraw.Draw(feather).ellipse((self.lw * 0.03, self.lh * 0.08, self.lw * 0.97, self.lh * 1.02), fill=255)
        self.feather = ImageChops.multiply(soft_blur(feather, self.lw * 0.16), _border(self.lw, self.lh, 0.17, 0.22))
        core = _glow(96, 1.7)
        bloom = Image.new("RGBA", (96, 96), (0, 0, 0, 0))
        over(bloom, (150, 200, 255), core)
        self.bloom = bloom
        sh = Image.new("L", (out, out), 0)
        ImageDraw.Draw(sh).rounded_rectangle((out / 2 - N * 0.46, out / 2 - N * 0.40, out / 2 + N * 0.46, out / 2 + N * 0.52),
                                             radius=N * 0.2, fill=255)
        self.shadow = soft_blur(sh, N * 0.12)
        window = Image.new("L", (self.hw, self.hh), 0)
        m = max(6, int(min(self.hw, self.hh) * 0.16))
        window.paste(255, (m, m, self.hw - m, self.hh - m))
        self.window = soft_blur(window, m * 0.55)
        band = Image.new("L", (out * 3, out), 0)
        w = out * 0.20
        ImageDraw.Draw(band).polygon([(out * 1.2, out), (out * 1.2 + w, out), (out * 1.2 + w + out * 0.45, 0),
                                      (out * 1.2 + out * 0.45, 0)], fill=255)
        self.band = soft_blur(band, out * 0.045)
        rnd = random.Random(3)
        self.sparks = [(rnd.random() * math.tau, rnd.uniform(0.62, 1.0), rnd.uniform(0.0, 0.18), rnd.choice((1, 1, 2)),
                        rnd.choice(PALETTE)) for _ in range(46)]

    # -------------------------------------------------------------- the icon
    def icon_pose(self, t, pointer=(0.0, 0.0)):
        """(alpha, scale, yaw, pitch) of the icon at t."""
        pop = t - THUMP
        if pop <= 0:
            return 0.0, 0.5, -1.3, 0.2
        k = spring(pop)
        settle = smooth(0.7, 1.4, pop)
        yaw = lerp(-1.3, 0.0, spring(pop * 0.92)) + settle * (0.05 * math.sin(t * 1.6) + 0.20 * pointer[0])
        pitch = lerp(0.22, 0.0, spring(pop * 0.92)) + settle * (0.03 * math.sin(t * 1.25 + 1.0) - 0.14 * pointer[1])
        return smooth(0.0, 0.14, pop), lerp(0.5, 1.0, k), yaw, pitch

    def rings(self, t):
        """Ripples as (start, strength, fringe): the thump's wide one, the four bells', the chord's."""
        return [(THUMP, 1.0, 5.0)] + [(b, 0.82 - 0.06 * i, 4.0) for i, b in enumerate(BELLS)] + [(CHORD, 0.9, 6.0)]

    # -------------------------------------------------------------- one frame
    def render(self, t, bg=None, pointer=(0.0, 0.0), show_icon=True, fade=1.0):
        """RGB image of the region. `bg` replaces the still backdrop (while the window opens it dissolves into the
        screen behind); `fade` takes the aurora away; `show_icon` False once the icon has set off on its own."""
        img = (self.bg if bg is None else bg).copy()
        if self.calm:
            return self._calm(img, t, show_icon)
        RW, RH, cx, cy, N = self.RW, self.RH, self.cx, self.cy, self.N

        # aurora, a bloom behind the icon and the sparks, on a quarter-size layer that is stretched up (it is soft anyway)
        env = (0.25 + 0.75 * smooth(0.0, 1.3, t)) * (0.65 + 0.35 * smooth(THUMP, THUMP + 0.5, t)) * fade
        layer = Image.new("RGBA", (self.lw, self.lh), (0, 0, 0, 0))
        if env > 0.01:
            for c in self.curtains:
                tex = ImageChops.offset(c["tex"], int(t * c["speed"] * (1.0 + 1.4 * smooth(THUMP - 0.1, THUMP + 0.3, t))), 0)
                part = tex.crop((0, 0, self.lw, self.lh))
                part.putalpha(part.getchannel("A").point(lambda v, k=c["k"] * env * (0.85 if self.dark else 0.5): int(v * k)))
                layer.alpha_composite(part)
        glow = (smooth(0.0, THUMP, t) * 0.45 + 0.55 * math.exp(-max(0.0, t - THUMP) * 3.2) * smooth(THUMP - 0.04, THUMP + 0.02, t)
                + 0.5 * math.exp(-max(0.0, t - CHORD) * 2.0) * smooth(CHORD - 0.02, CHORD + 0.05, t)
                + 0.18 * smooth(CHORD, CHORD + 0.8, t)) * fade
        if glow > 0.01:
            r = int((self.N * (0.9 + 0.7 * smooth(THUMP - 0.1, THUMP + 0.5, t) + 0.8 * smooth(CHORD, CHORD + 0.7, t))) / 4)
            sprite = self.bloom.resize((max(2, 2 * r), max(2, 2 * r)), Image.BILINEAR)
            sprite.putalpha(sprite.getchannel("A").point(lambda v, k=clamp(glow) * (0.9 if self.dark else 0.55): int(v * k)))
            layer.alpha_composite(sprite, (cx // 4 - r, cy // 4 - r))
        layer.putalpha(ImageChops.multiply(layer.getchannel("A"), self.feather))        # nothing may reach the region's edge
        big = layer.resize((RW, RH), Image.BICUBIC)
        img.paste(big.convert("RGB"), (0, 0), big.getchannel("A"))

        # ripples and sparks, on a half-size layer (each is a thin ring, with the colours split a little at its edge)
        hw, hh = self.hw, self.hh
        planes = [Image.new("L", (hw, hh), 0) for _ in range(3)]
        draws = [ImageDraw.Draw(p) for p in planes]
        pl, u = self.pl, self.u
        c2x, c2y = cx / pl, cy / pl
        reach = min(hw, hh) * 0.98
        any_light = False
        for start, strength, fringe in self.rings(t):
            age = t - start
            if age <= 0 or age > 1.6:
                continue
            k = age / 1.6
            r0 = N / (2.0 * pl) * 0.9
            r = r0 + (reach - r0) * out_cubic(k)
            amp = int(255 * 0.8 * strength * (1 - k) ** 1.6 * smooth(0.0, 0.05, age) * fade)
            if amp < 4:
                continue
            width = max(2, round(lerp(4.2, 1.6, k) * u))
            for i, off in enumerate((-fringe * 0.5, 0.0, fringe * 0.5)):
                rr = r + off * u * (0.4 + k)
                draws[i].ellipse((c2x - rr, c2y - rr * 0.97, c2x + rr, c2y + rr * 0.97), outline=amp, width=width)
            any_light = True
        if t < THUMP + 0.12:                                                            # the sparks spiral in and vanish into the icon
            gather = smooth(0.04, THUMP, t)
            for ang, dist, delay, size, col in self.sparks:
                fl = clamp((t - delay) / (THUMP - delay))
                if fl <= 0:
                    continue
                rad = reach * dist * (1 - out_cubic(fl * 0.96)) + 6 * u
                th_ = ang + fl * 2.6
                x, y = c2x + math.cos(th_) * rad, c2y + math.sin(th_) * rad * 0.9
                a = smooth(0.0, 0.18, fl) * (1 - smooth(0.9, 1.0, fl)) * (0.4 + 0.6 * gather)
                if a < 0.03:
                    continue
                col = col if self.dark else (sum(col) // 3,) * 3
                size *= u
                for i in range(3):
                    draws[i].ellipse((x - size, y - size, x + size, y + size), fill=int(col[i] * a))
                    if size > u:
                        draws[i].ellipse((x - size * 2, y - size * 2, x + size * 2, y + size * 2), outline=int(col[i] * a * 0.4),
                                         width=max(1, round(u)))
            any_light = True
        if any_light:
            ring = Image.merge("RGB", [ImageChops.multiply(p, self.window).point(lambda v, k=k: int(v * k))
                                       for p, k in zip(planes, (0.78, 0.92, 1.0))])
            ring = ring.filter(ImageFilter.GaussianBlur(self.blur))
            if pl > 1:
                ring = ring.resize((RW, RH), Image.BICUBIC)
            if self.dark:
                img = ImageChops.screen(img, ring)
            else:
                img = ImageChops.multiply(img, ImageChops.invert(ring.point(lambda v: int(v * 0.55))))

        if show_icon:
            self._icon(img, t, pointer)
        return img

    def _icon(self, img, t, pointer):
        alpha, scale, yaw, pitch = self.icon_pose(t, pointer)
        if alpha <= 0.003:
            return
        out, N, cx, cy = self.out, self.N, self.cx, self.cy
        ring = sum(math.exp(-(t - b) * 9.0) for b in BELLS if t >= b)                   # the icon rings with each bell
        scale *= 1 + 0.02 * min(1.0, ring)
        posed = pose(self.sprite, out, scale, yaw, pitch)
        a = posed.getchannel("A")
        pos = (cx - out // 2, cy - out // 2)
        # the shadow, shrinking with the icon
        sh = int(out * scale)
        shadow = self.shadow.resize((sh, sh), Image.BILINEAR)
        img.paste((4, 6, 24) if self.dark else (30, 40, 90), (cx - sh // 2, cy - sh // 2 + int(N * 0.16)),
                  shadow.point(lambda v, k=0.5 * alpha: int(v * k)))
        rgb = posed.convert("RGB")
        light = ring * 0.26
        if light > 0.01:
            rgb.paste((255, 255, 255), (0, 0), a.point(lambda v, k=min(1.0, light): int(v * k)))
        sweep = (t - SWEEP) / 0.62
        if 0.0 <= sweep <= 1.0:                                                         # a highlight crossing the glass
            x = int(lerp(-out * 0.35, out * 1.25, out_cubic(sweep)))
            band = self.band.crop((out + out // 2 - x - out // 2 + out // 2, 0, out + out // 2 - x + out + out // 2, out)).crop((0, 0, out, out))
            spec = ImageChops.multiply(band, a)
            rgb.paste((255, 255, 255), (0, 0), spec.point(lambda v, k=0.62 * math.sin(math.pi * sweep) ** 0.7: int(v * k)))
        if alpha < 0.999:
            a = a.point(lambda v, k=alpha: int(v * k))
        img.paste(rgb, pos, a)

    def _calm(self, img, t, show_icon):
        k = smooth(0.0, 0.55, t)
        if show_icon and k > 0:
            sprite = self.icon.copy()
            a = sprite.getchannel("A").point(lambda v: int(v * k))
            sprite.putalpha(a)
            img.paste(sprite.convert("RGB"), (self.cx - self.N // 2, self.cy - self.N // 2), a)
        return img

    # -------------------------------------------------------------- the name
    def title_amount(self, t):
        return smooth(SWEEP - 0.05, SWEEP + 0.55, t) if not self.calm else smooth(0.15, 0.7, t)


class Ahead:
    """Makes the next frames on worker threads while Tk is still drawing the last one. Tk's drawing runs without the
    interpreter lock and Pillow releases it for the heavy parts, so the drawing and two frames in the making all overlap:
    one frame can take longer than a tick on a slow computer, but two at a time keep up with the screen. Frames
    are numbered, and one that finishes after a newer one has been made is dropped, so they never show out of order."""

    WORKERS = 2

    def __init__(self, make):
        self.make, self.plan, self.out, self.stopped = make, None, None, False
        self.asked = self.shown = 0                           # the newest plan's number, the newest finished frame's
        self.cond = threading.Condition()
        for i in range(self.WORKERS):
            threading.Thread(target=self._run, name=f"launch-frames-{i}", daemon=True).start()

    def ask(self, *plan):
        with self.cond:
            self.asked += 1
            self.plan = (self.asked, plan)
            self.cond.notify()

    def take(self):
        with self.cond:
            out, self.out = self.out, None
            return out

    def stop(self):
        with self.cond:
            self.stopped = True
            self.cond.notify_all()

    def _run(self):
        while True:
            with self.cond:
                while self.plan is None and not self.stopped:
                    self.cond.wait()
                if self.stopped:
                    return
                (n, plan), self.plan = self.plan, None
            try:
                out = self.make(*plan)
            except Exception:
                log.debug("launch frame failed", exc_info=True)
                out = None
            with self.cond:
                if out is not None and n > self.shown:
                    self.shown, self.out = n, out


class Launch:
    """One run of the animation: shows the stage, opens the window hidden behind it at the right moment, and flies the icon
    into the header while the window comes up."""

    def __init__(self, app, after):
        self.app, self.after = app, after
        cv, p, th = app.cv, app.p, app.th
        self.calm = app.reduced()
        app.splashing = True
        self.native = False                                                             # (the macOS edition's system glass)
        D = self.D = app.D
        scene = self.scene0 = gk.make_background(app.W, app.H, th, app.S)                # (D pixels to the point, as is all of this)
        app.scene = scene
        cv.delete("all")
        cv.create_image(0, 0, anchor="nw", image=app.scene_photo(scene), tags="scene")
        cv.update_idletasks()                                                           # the backdrop shows while the rest is made
        app.paint(staged=True)                                                          # the finished window, built hidden
        self.page_scene = app.scene
        cv.itemconfigure("scene", image=app.scene_photo(scene))
        app.scene = self.page_scene
        self.staged = (app.W, app.H)

        self.size = size = p(160)
        icon = self.icon = gk.app_icon(size)
        RW, RH = min(p(660), app.W), min(p(560), app.H)
        self.box = box = ((app.W - RW) // 2, max(0, min(app.H - RH, int(app.H * 0.44) - int(RH * 0.43))))
        bx, by = box[0] * D, box[1] * D
        self.stage = stage = Stage((RW * D, RH * D), (bx, by), scene, th, icon, self.calm, D)
        self.page_bg = self.page_scene.crop((bx, by, bx + RW * D, by + RH * D)).convert("RGB")
        self.photo = app.tkphoto(stage.render(0.0), D)
        self.item = app.put(box[0], box[1], self.photo, "nw", "splash")
        self.cx, self.cy = box[0] + stage.cx / D, box[1] + stage.cy / D
        self.title_y = self.cy + size // 2 + p(58)
        self.title = cv.create_text(self.cx, self.title_y, text="Music Downloader", font=app.f_stat, tags="splash",
                                    state="hidden", fill=hx(app.bg_at(self.cx, self.title_y)))
        self.fly = cv.create_image(self.cx, self.cy, anchor="nw", image="", state="hidden", tags="splash")
        self.header = (p(app.M) + p(18), p(38))
        self.leave, self.flight = (CALM["leave"], CALM["fly"]) if self.calm else (LEAVE, FLY)
        self.t0 = None
        self.opened = self.skipped = self.dead = False
        self.region = True
        self.last = None
        self.blend = 0.0
        self.ahead = Ahead(lambda t, blend, look, show, fade: self.stage.render(t, self.blended(blend), look, show, fade))
        self.look = [0.0, 0.0]                                                          # the pointer, smoothed
        self.last_bg = app.bg_at(self.cx, self.title_y)
        app.cue("startup")
        app.animate("splash", self.leave + self.flight + 6.0, self.tick, lambda f: f, done=self.finish)

    # -------------------------------------------------------------- per frame
    def pointer(self):
        app = self.app
        try:
            x = (app.cv.winfo_pointerx() - app.cv.winfo_rootx()) / max(1, app.W) - 0.5
            y = (app.cv.winfo_pointery() - app.cv.winfo_rooty()) / max(1, app.H) - 0.5
        except Exception:
            return 0.0, 0.0
        for i, v in enumerate((x * 2, y * 2)):
            self.look[i] += (clamp(v, -1.0, 1.0) - self.look[i]) * 0.12
        return self.look[0], self.look[1]

    def tick(self, _f):
        if self.dead:
            return
        app = self.app
        if self.t0 is None:
            self.t0 = clock()
        now = clock() - self.t0
        if (app.W, app.H) != self.staged:                                               # resized meanwhile: build it afresh
            return self.abort()
        if self.skipped and now < self.leave:
            self.t0 -= self.leave - now
            now = self.leave
        if not self.opened and now >= self.leave:
            self.open()
        h = clamp((now - self.leave) / self.flight) if self.opened else 0.0
        self.draw(now, h)
        if h >= 1.0:
            self.finish()

    def blended(self, blend):
        """The still backdrop of the region while the window opens: it dissolves into the screen behind it."""
        if blend <= 0:
            return None
        return self.page_bg if blend >= 1 else Image.blend(self.stage.bg, self.page_bg, blend)

    def phase(self, now):
        """(h, blend, away): how far the hand-over has got, how far the backdrop has dissolved, whether the stage is done."""
        h = clamp((now - self.leave) / self.flight) if self.opened or now >= self.leave else 0.0
        blend = (1.0 if h > 0 else 0.0) if self.calm else smooth(0.12, 0.72, h)
        if self.native:
            blend = 0.0
        away = h >= 0.5 or (self.calm and h > 0) or (self.opened and self.native)       # the stage has done its part
        return h, blend, away

    def draw(self, now, h):
        app, cv, stage = self.app, self.app.cv, self.stage
        _, blend, away = self.phase(now)
        dissolve = self.opened and not self.native
        if dissolve and blend != self.blend:
            self.blend = blend
            cv.itemconfigure("scene", image=app.scene_photo(Image.blend(self.scene0, self.page_scene, blend)))
        if away:
            if self.region:
                self.region = False
                self.ahead.stop()
                cv.delete(self.item)
        else:
            img = self.ahead.take()
            if img is None and self.last is None:
                img = stage.render(now, None, (0.0, 0.0))                               # the very first frame
            if img is not None:
                self.photo.paste(img)
                self.last = img
            _, nblend, naway = self.phase(now + LEAD)
            if not naway:
                self.ahead.ask(now + LEAD, nblend if self.opened else 0.0, self.pointer(), h <= 0.0,
                               1 - smooth(0.0, 0.5, h))
        ta = stage.title_amount(now) * (1 - smooth(0.0, 0.3, h))
        if ta > 0.004:
            if self.last is not None and self.region:
                self.last_bg = self.last.getpixel((stage.cx, min(stage.RH - 1, int((self.title_y - self.box[1]) * self.D))))
            cv.itemconfigure(self.title, state="normal", fill=hx(mix(self.last_bg, app.th["fg"], ta)))
            cv.coords(self.title, self.cx, self.title_y - app.p(8) * (1 - ta) - app.p(10) * h)
        elif self.last_title > 0.004:
            cv.itemconfigure(self.title, state="hidden")
        self.last_title = ta
        if h > 0:
            k = gk.ease(h)
            w = max(4, int(lerp(self.size, app.p(36), k)))
            x = lerp(self.cx, self.header[0], k)
            y = lerp(self.cy, self.header[1], k) - math.sin(k * math.pi) * app.p(26)
            wp = w * self.D
            img_i = self.icon.resize((wp, wp), Image.LANCZOS)
            if w < app.p(48):                                                           # settle into the header's own bitmap
                img_i = Image.blend(img_i, gk.app_icon(app.p(36)).resize((wp, wp), Image.LANCZOS), smooth(0.7, 1.0, h))
            ph = app.photo("fly", img_i, self.D)
            cv.itemconfigure(self.fly, image=ph, state="normal")
            cv.coords(self.fly, *app.corner(x, y, ph, "center"))
            cv.tag_raise(self.fly)

    last_title = 0.0

    # -------------------------------------------------------------- hand-over
    def open(self):
        """The animation has hidden the finished window long enough: bring it up behind the leaving icon."""
        app, cv = self.app, self.app.cv
        self.opened = True
        app.splashing = False
        cv.dtag("hicon", "ui")
        cv.itemconfigure("hicon", state="hidden")
        cv.tag_raise("ui", self.item)                                                   # the window comes up through the aurora, not under a box
        app.reveal("ui", dy=12, dur=0.3 if self.calm else 0.5, delay=0.0 if self.calm else 0.18)
        app.root.after(2500, app.maybe_tune)
        app.start_followups()

    def abort(self):
        self.dead = True
        self.ahead.stop()
        self.app.stop_anim("splash")
        self.app.cv.delete("splash")
        self.app._splash = None
        self.app.splashing = False
        self.after()

    def finish(self):
        if self.dead:
            return
        self.dead = True
        self.ahead.stop()
        app, cv = self.app, self.app.cv
        app.stop_anim("splash")
        cv.delete("splash")
        app.scene = self.page_scene
        if not self.native:
            cv.itemconfigure("scene", image=app.scene_photo(self.page_scene))
        cv.itemconfigure("hicon", state="normal")
        app._splash = None
        if not self.opened:                                                             # (defensive: never leave it hidden)
            app.splashing = False
            cv.itemconfigure("ui", state="normal")


class SplashMixin:
    """Plays the animation over the finished window (which is built first, hidden) and hands over to it."""

    _splash = None

    def run_splash(self, after):
        self._splash = Launch(self, after)

    def skip_splash(self):
        if self._splash and not self._splash.opened:
            self._splash.skipped = True
