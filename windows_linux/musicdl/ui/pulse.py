"""
pulse.py — the progress visualisation: a music waveform that fills with light as the songs finish.

Roughly a hundred slim bars form a waveform. Everything up to the progress edge glows in a cyan → blue → violet →
pink gradient; the rest waits as faint grey bars. The bars move with the real work: faster downloads lift the
whole waveform, a hump of energy rides the progress edge, and every finished song sends a ripple (and a few sparks)
out from it. While the disk is full the waveform turns amber and nearly stops; before the first song is known a
band of light sweeps across. Pure Pillow; one small RGBA image per frame (a few milliseconds).
"""
import math
import random

from PIL import Image, ImageDraw, ImageFilter

from . import glass as gk

SS = 3                                                    # supersampling of the cached bar sprites
STOPS = {                                                 # left -> right colours of the lit part
    "dark": ((60, 220, 255), (24, 140, 255), (138, 104, 255), (255, 110, 196)),
    "light": ((0, 176, 232), (0, 112, 235), (112, 84, 232), (226, 64, 160)),
}
AMBER = {"dark": ((255, 214, 90), (255, 176, 46), (255, 150, 40)), "light": ((235, 160, 0), (224, 132, 0), (214, 110, 0))}
STEPS = 14                                                # colour steps along the bar (sprites are cached per step)
RIPPLE_LIFE = 1.3


def _ramp(stops, n):
    """`n` colours blended through the stops."""
    out = []
    for i in range(n):
        f = i / max(1, n - 1) * (len(stops) - 1)
        k = min(len(stops) - 2, int(f))
        out.append(tuple(int(v) for v in gk.mix(stops[k], stops[k + 1], f - k)))
    return out


class Pulse:
    def __init__(self, w, h, th, S, seed=1):
        self.w, self.h, self.th, self.S = int(w), int(h), th, S
        self.bw = max(2, int(round(3.4 * S)))
        gap = max(2, int(round(3.2 * S)))
        pitch = self.bw + gap
        self.pitch = pitch
        self.n = max(8, (self.w + gap) // pitch)
        self.x0 = (self.w - (self.n * pitch - gap)) // 2
        self.maxhalf = max(3, int(self.h / 2 - 2 * S))
        rng = random.Random(seed)
        p1, p2, p3 = (rng.random() * 6.28 for _ in range(3))
        self.base, self.sp, self.ph = [], [], []
        for i in range(self.n):
            slow = 0.55 + 0.45 * math.sin(i * 0.052 + p1) * math.sin(i * 0.013 + p2)         # verses and choruses
            beat = 0.5 + 0.5 * math.sin(i * 0.9 + p3)                                       # the beat inside them
            self.base.append(min(1.0, max(0.14, 0.18 + 0.5 * slow + 0.25 * beat * slow + 0.22 * rng.random())))
            self.sp.append(1.6 + rng.random() * 3.2)
            self.ph.append(rng.random() * 6.28)
        mode = "dark" if th["dark"] else "light"
        self.lit = _ramp(STOPS[mode], STEPS)
        self.warm = _ramp(AMBER[mode], STEPS)
        self.dim = tuple(th["fg3"][:3])
        self.dim_a = 105 if th["dark"] else 120
        self.t = 0.0
        self.energy = 0.25
        self.ripples = []                                       # [age, x where it started]
        self.sparks = []                                        # [x, y, vx, vy, age]
        self._sprites = {}
        self._rng = random.Random(seed + 7)
        self._glow = None

    # ---------------------------------------------------------------- events
    def edge(self, fraction):
        """x of the progress edge for a 0..1 fraction."""
        return self.x0 + max(0.0, min(1.0, fraction)) * (self.n * self.pitch)

    def burst(self, fraction):
        """A song finished: a ripple and a few sparks leave the progress edge."""
        x = self.edge(fraction)
        if len(self.ripples) < 4:
            self.ripples.append([0.0, x])
        S = self.S
        for _ in range(5):
            self.sparks.append([x, self.h / 2 + self._rng.uniform(-8, 8) * S, self._rng.uniform(-70, 70) * S,
                                self._rng.uniform(-80, -15) * S, 0.0])
        del self.sparks[:-24]

    # ---------------------------------------------------------------- pieces
    def _sprite(self, half, rgb, alpha):
        key = (half, rgb, alpha)
        sp = self._sprites.get(key)
        if sp is None:
            if len(self._sprites) > 1800:
                self._sprites.clear()
            bw, hh = self.bw * SS, half * 2 * SS
            big = Image.new("RGBA", (bw, hh), (0, 0, 0, 0))
            ImageDraw.Draw(big).rounded_rectangle((0, 0, bw - 1, hh - 1), radius=bw // 2, fill=rgb + (alpha,))
            sp = self._sprites[key] = big.resize((self.bw, half * 2), Image.LANCZOS)
        return sp

    def _glow_sprite(self):
        if self._glow is None:
            gw, gh = int(60 * self.S), self.h
            m = Image.new("L", (gw, gh), 0)
            ImageDraw.Draw(m).ellipse((gw * 0.34, 2, gw * 0.66, gh - 2), fill=255)
            self._glow = gk.soft_blur(m, 9 * self.S)
        return self._glow

    # ---------------------------------------------------------------- one frame
    def frame(self, dt, fraction, energy, mode="run"):
        """RGBA image of the waveform. fraction: 0..1 progress, or None while the plan is still being made.
        energy: 0..1 how busy the work is. mode: run | paused | stopping."""
        paused, stopping = mode == "paused", mode == "stopping"
        dt = min(0.1, max(0.0, dt))
        speed = 0.22 if paused else (0.5 if stopping else 1.0)
        self.t += dt * speed
        goal = 0.12 if paused else (0.10 if stopping else energy)
        self.energy += (goal - self.energy) * (1 - math.exp(-dt * 4.0))
        for r in self.ripples:
            r[0] += dt
        self.ripples = [r for r in self.ripples if r[0] < RIPPLE_LIFE]
        for s in self.sparks:
            s[0] += s[2] * dt
            s[1] += s[3] * dt
            s[3] += 40 * self.S * dt
            s[4] += dt
        self.sparks = [s for s in self.sparks if s[4] < 0.9]

        W, H, S = self.w, self.h, self.S
        mid = H // 2
        px = None if fraction is None else self.edge(fraction)
        sweep = None if fraction is not None else (self.t * 0.42 % 1.4 - 0.2) * W
        palette = self.warm if paused else self.lit
        lit_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        dim_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        sig = 30 * S
        e = self.energy
        for i in range(self.n):
            cx = self.x0 + i * self.pitch
            c = cx + self.bw / 2
            level = self.base[i] * (0.30 + 0.70 * e) * (0.76 + 0.24 * math.sin(self.t * self.sp[i] + self.ph[i]))
            glow = 0.0
            if px is not None:
                d = c - px
                glow = math.exp(-(d / sig) ** 2)
                level += 0.5 * (0.3 + 0.7 * e) * glow
                for age, x_from in self.ripples:
                    front, fade = age * 300 * S, 1 - age / RIPPLE_LIFE
                    level += 0.62 * fade * math.exp(-((abs(c - x_from) - front) / (24 * S)) ** 2)
            if sweep is not None:
                glow = math.exp(-((c - sweep) / (0.10 * W)) ** 2)
                level += 0.35 * glow
            level = min(1.0, max(0.07, level))
            half = max(1, int(level * self.maxhalf))
            on = (px is not None and c <= px) or glow > 0.35 and sweep is not None
            if on:
                step = min(STEPS - 1, int(c / W * STEPS))
                rgb = palette[step]
                if glow > 0.25:                                           # brighter right at the edge
                    rgb = tuple(int(v + (255 - v) * 0.45 * glow) for v in rgb)
                lit_layer.alpha_composite(self._sprite(half, rgb, 255), (cx, mid - half))
            else:
                half = max(1, int(half * 0.8))
                dim_layer.alpha_composite(self._sprite(half, self.dim, self.dim_a), (cx, mid - half))

        out = dim_layer
        alpha = lit_layer.getchannel("A")
        if alpha.getbbox():                                                # a soft bloom around everything lit
            small = alpha.resize((max(1, W // 4), max(1, H // 4)), Image.BILINEAR).filter(ImageFilter.GaussianBlur(2.2))
            bloom = small.resize((W, H), Image.BICUBIC).point(lambda v: int(v * 0.55))
            gk.over(out, palette[STEPS // 3], bloom)
            out.alpha_composite(lit_layer)
        if px is not None and 0.0 < (fraction or 0.0) < 1.0:             # the playhead
            g = self._glow_sprite().point(lambda v: int(v * 0.8))
            gx = int(px - g.width / 2)
            if gx < 0 or gx + g.width > W:                                # keep the halo inside the picture
                full = Image.new("L", (W, H), 0)
                full.paste(g, (gx, 0))
                g, gx = full, 0
            gk.over(out, palette[min(STEPS - 1, int(px / W * STEPS))], g, (gx, 0))
            ph_w = max(2, int(2 * S))
            head = (255, 255, 255) if self.th["dark"] else tuple(int(v * 0.8) for v in palette[min(STEPS - 1, int(px / W * STEPS))])
            bar = Image.new("RGBA", (ph_w, H - int(4 * S)), head + (235,))
            out.alpha_composite(bar, (max(0, min(W - ph_w, int(px - ph_w / 2))), int(2 * S)))
        if self.sparks:
            layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            d = ImageDraw.Draw(layer)
            for x, y, _vx, _vy, age in self.sparks:
                a = int(255 * (1 - age / 0.9))
                r = max(1.0, 1.7 * S * (1 - age / 1.4))
                d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, a))
            out.alpha_composite(layer)
        return out
