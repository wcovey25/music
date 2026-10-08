"""
glass.py — the drawing toolkit behind the "liquid glass" look. Pure Pillow, no numpy.

Themes, squircle masks, frosted-glass cards, pill buttons, switches, segmented controls, artwork tiles and a few
icons. Nothing here knows about the app; app.py composes these into the window.

The glass edge "refraction" of V2 needed numpy. Here the rim is faked with two cheap Pillow steps: the padded
backdrop is squeezed into the card (so its edge shows what lies just outside) and blended in only near the edge.
"""
import io
import math

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter

# ---------------------------------------------------------------- theme

THEMES = {
    "dark": dict(
        dark=True, base=(15, 16, 23),
        # (x, y, radius) as fractions of the window, colour, opacity
        orbs=[(0.10, 0.12, 0.46, (36, 84, 196), 0.55),
              (0.94, 0.10, 0.38, (104, 70, 196), 0.38),
              (0.78, 0.98, 0.44, (26, 112, 164), 0.30)],
        tint=(13, 14, 22, 0.46), sat=1.15, lift=1.08,
        sheen=0.07, glow=0.10, rim=0.42, rim_dark=0.0, shadow=0.50,
        well=(26, 28, 40), well_edge=(255, 255, 255, 0.09),
        track=(255, 255, 255, 0.10), hair=(255, 255, 255, 0.08),
        btn=0.10, btn_hover=0.17, btn_press=0.06,
        seg=(255, 255, 255, 0.08), seg_sel=(255, 255, 255, 0.20), tile=(255, 255, 255, 0.07), group=(255, 255, 255, 0.055),
        switch_off=(150, 152, 170, 0.34), knob=(255, 255, 255),
        fg=(245, 245, 248), fg2=(184, 186, 200), fg3=(136, 139, 154),
        link=(120, 178, 255), stop=(255, 115, 105),
        accent=(10, 132, 255), accent2=(70, 108, 250),
        ok=(48, 209, 88), warn=(255, 176, 46), bad=(255, 105, 97), info=(100, 210, 255),
    ),
    "light": dict(
        dark=False, base=(241, 242, 247),
        orbs=[(0.08, 0.10, 0.36, (176, 208, 255), 0.80),
              (0.94, 0.08, 0.30, (222, 208, 255), 0.65),
              (0.86, 0.94, 0.34, (255, 218, 232), 0.55),
              (0.06, 0.96, 0.28, (196, 238, 232), 0.50)],
        tint=(255, 255, 255, 0.46), sat=1.10, lift=1.02,
        sheen=0.30, glow=0.40, rim=0.85, rim_dark=0.10, shadow=0.12,
        well=(255, 255, 255), well_edge=(0, 0, 0, 0.07),
        track=(0, 0, 0, 0.07), hair=(0, 0, 0, 0.08),
        btn=0.55, btn_hover=0.85, btn_press=0.30,
        seg=(0, 0, 0, 0.06), seg_sel=(255, 255, 255, 0.96), tile=(0, 0, 0, 0.05), group=(255, 255, 255, 0.62),
        switch_off=(120, 120, 128, 0.26), knob=(255, 255, 255),
        fg=(29, 29, 31), fg2=(96, 96, 106), fg3=(140, 140, 150),
        link=(0, 112, 235), stop=(224, 52, 42),
        accent=(0, 122, 255), accent2=(56, 116, 246),
        ok=(40, 190, 80), warn=(224, 132, 0), bad=(235, 60, 50), info=(40, 160, 225),
    ),
}


def hx(c):
    return "#%02x%02x%02x" % tuple(int(v) for v in c[:3])


def mix(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def shade(c, d):
    return tuple(max(0, min(255, int(v + d))) for v in c[:3])


def ease(t):
    """Smooth start and stop (0..1 -> 0..1)."""
    t = max(0.0, min(1.0, t))
    return t * t * (3 - 2 * t)


def ease_out(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


# ---------------------------------------------------------------- imaging

_MASKS = {}


def clear_caches():
    _MASKS.clear()


def shape_mask(w, h, r, ss=3, n=3.0):
    """Anti-aliased 'L' mask of a rounded rect with continuous (squircle-ish) corners."""
    key = (w, h, r, ss, n)
    if key in _MASKS:
        return _MASKS[key]
    if len(_MASKS) > 300:
        _MASKS.clear()
    W, H = w * ss, h * ss
    R = max(1.0, min(r * ss, W / 2, H / 2))
    e = 2.0 / n
    steps = 24
    pts = []
    for cx, cy, sx, sy, fwd in ((W - R, R, 1, -1, True), (R, R, -1, -1, False),
                                (R, H - R, -1, 1, True), (W - R, H - R, 1, 1, False)):
        for i in range(steps + 1):
            t = (i / steps) * math.pi / 2
            if not fwd:
                t = math.pi / 2 - t
            pts.append((cx + sx * R * max(0.0, math.cos(t)) ** e, cy + sy * R * max(0.0, math.sin(t)) ** e))
    m = Image.new("L", (W, H), 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    m = m.resize((w, h), Image.LANCZOS)
    _MASKS[key] = m
    return m


def ring_mask(w, h, r, t):
    """'L' mask of a t-px outline hugging the inside of the shape."""
    inner = Image.new("L", (w, h), 0)
    if w > 2 * t and h > 2 * t:
        inner.paste(shape_mask(w - 2 * t, h - 2 * t, max(1, r - t)), (t, t))
    return ImageChops.subtract(shape_mask(w, h, r), inner)


def vgrad(w, h, top, bottom, stop=1.0):
    n = 64
    g = Image.new("L", (1, n))
    g.putdata([int(top + (bottom - top) * min(1.0, i / (n - 1) / stop)) for i in range(n)])
    return g.resize((w, h), Image.BILINEAR)


def hgrad(w, h, left, right):
    n = 64
    g = Image.new("L", (n, 1))
    g.putdata([int(left + (right - left) * i / (n - 1)) for i in range(n)])
    return g.resize((w, h), Image.BILINEAR)


def corners(w, h, tl, tr, bl, br):
    g = Image.new("L", (2, 2))
    g.putdata([tl, tr, bl, br])
    return g.resize((w, h), Image.BILINEAR)


def soft_blur(img, radius):
    """Gaussian blur at reduced resolution: the backdrops are smooth, so it looks the same and is far faster."""
    k = max(1, int(radius // 6))
    if k == 1:
        return img.filter(ImageFilter.GaussianBlur(radius))
    w, h = img.size
    small = img.resize((max(1, w // k), max(1, h // k)), Image.BILINEAR)
    small = small.filter(ImageFilter.GaussianBlur(radius / k))
    return small.resize((w, h), Image.BICUBIC)


def over(canvas, color, mask, at=(0, 0)):
    """Alpha-composite a flat colour, shaped by an 'L' mask, onto an RGBA canvas."""
    layer = Image.new("RGBA", mask.size, tuple(int(v) for v in color[:3]) + (0,))
    layer.putalpha(mask)
    canvas.alpha_composite(layer, at)


def scaled(mask, a):
    return mask.point(lambda v: int(v * a))


def make_background(W, H, th, S):
    """Soft colour field: base + blurred orbs, a flat top edge (matches the title bar) and a hint of grain."""
    sw, sh = max(8, W // 4), max(8, H // 4)
    img = Image.new("RGB", (sw, sh), th["base"])
    d = ImageDraw.Draw(img, "RGBA")
    for cx, cy, rr, col, a in th["orbs"]:
        r = rr * max(sw, sh)
        d.ellipse((cx * sw - r, cy * sh - r, cx * sw + r, cy * sh + r), fill=tuple(col) + (int(a * 255),))
    img = img.filter(ImageFilter.GaussianBlur(sw * 0.045)).resize((W, H), Image.BICUBIC)
    fade = int(150 * S)
    vm = Image.new("L", (1, H), 0)
    vm.putdata([max(0, 255 - int(255 * max(0, y - 40 * S) / max(1, fade - 40 * S))) if y < fade else 0 for y in range(H)])
    img.paste(th["base"], (0, 0), vm.resize((W, H)))
    noise = Image.effect_noise((W, H), 2)
    return ImageChops.add(img, Image.merge("RGB", (noise,) * 3), 1.0, -128)


def refract(back, w, h, r, pad, bevel, strength):
    """Glass rim without numpy: the padded backdrop squeezed into the card shows what lies just outside the edge;
    it is blended in only within `bevel` px of the edge (quadratically), so the middle stays undistorted."""
    flat = back.crop((pad, pad, pad + w, pad + h))
    squeezed = back.resize((w, h), Image.BILINEAR)
    b = max(2, int(bevel))
    core = Image.new("L", (w, h), 0)
    if w > 2 * b + 2 and h > 2 * b + 2:
        core.paste(shape_mask(w - 2 * b, h - 2 * b, max(1, r - b)), (b, b))
    weight = ImageChops.invert(soft_blur(core, max(2.0, bevel * 0.55)))
    lut = [int(255 * strength * (v / 255.0) ** 2) for v in range(256)]
    return Image.composite(squeezed, flat, weight.point(lut))


def glass(scene, box, r, th, S, shadow=True):
    """Composite one sheet of liquid glass onto the scene at `box`."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    pad = int(24 * S)
    mask = shape_mask(w, h, r)

    if shadow:                                                # kept outside the glass so the backdrop inside stays clean
        sp = int(36 * S)
        sh = Image.new("L", (w + 2 * sp, h + 2 * sp), 0)
        sh.paste(mask, (sp, sp + int(8 * S)))
        sh = scaled(soft_blur(sh, 14 * S), th["shadow"])
        hole = Image.new("L", sh.size, 0)
        hole.paste(mask, (sp, sp))
        scene.paste((0, 0, 0), (x0 - sp, y0 - sp), ImageChops.subtract(sh, hole))

    back = soft_blur(scene.crop((x0 - pad, y0 - pad, x1 + pad, y1 + pad)), 12 * S)
    back = refract(back, w, h, r, pad, 20 * S, 0.8)
    back = ImageEnhance.Color(back).enhance(th["sat"])
    back = ImageEnhance.Brightness(back).enhance(th["lift"])
    tr, tg, tb, ta = th["tint"]
    back = Image.blend(back, Image.new("RGB", (w, h), (tr, tg, tb)), ta)

    back.paste((255, 255, 255), (0, 0), ImageChops.multiply(vgrad(w, h, int(255 * th["sheen"]), 0, 0.6), mask))
    e = max(2, int(3 * S))
    core = Image.new("L", (w, h), 0)
    core.paste(shape_mask(w - 2 * e, h - 2 * e, r - e), (e, e))
    edge = ImageChops.subtract(mask, soft_blur(core, 9 * S))
    back.paste((255, 255, 255), (0, 0), scaled(edge, th["glow"]))

    rim = ring_mask(w, h, r, max(1, int(round(1.4 * S))))
    lit = corners(w, h, 255, 80, 80, 200)
    back.paste((255, 255, 255), (0, 0), scaled(ImageChops.multiply(rim, lit), th["rim"]))
    if th["rim_dark"]:
        back.paste((0, 0, 0), (0, 0), scaled(ImageChops.multiply(rim, ImageChops.invert(lit)), th["rim_dark"]))
    scene.paste(back, (x0, y0), mask)


def well(scene, box, r, th, S, fill=None, alpha=1.0, edge=True):
    """An inset field: flat fill plus a hairline edge."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    m = shape_mask(w, h, r)
    scene.paste(fill or th["well"], (x0, y0), scaled(m, alpha) if alpha < 1 else m)
    if edge:
        er, eg, eb, ea = th["well_edge"]
        scene.paste((er, eg, eb), (x0, y0), scaled(ring_mask(w, h, r, max(1, int(S))), ea))


def hair(scene, x0, x1, y, th, S):
    r, g, b, a = th["hair"]
    t = max(1, int(round(S)))
    scene.paste((r, g, b), (x0, y), Image.new("L", (x1 - x0, t), int(255 * a)))


# ---------------------------------------------------------------- controls

def button_image(w, h, kind, state, th, S):
    """RGBA bitmap of a pill button with room around it for its glow/shadow."""
    g = int(round(14 * S))
    img = Image.new("RGBA", (w + 2 * g, h + 2 * g), (0, 0, 0, 0))
    mask = shape_mask(w, h, h // 2)
    pad_mask = Image.new("L", img.size, 0)
    pad_mask.paste(mask, (g, g))
    below = Image.new("L", img.size, 0)
    below.paste(mask, (g, g + int(3 * S)))
    t = max(1, int(round(1.3 * S)))
    rim = ring_mask(w, h, h // 2, t)
    lit = corners(w, h, 255, 90, 90, 200)

    if kind == "primary":
        ac, ac2 = th["accent"], th["accent2"]
        d = {"hover": 22, "press": -26}.get(state, 0)
        top, bot = shade(ac, 40 + d), shade(mix(ac, ac2, 0.45), -4 + d)
        if state != "disabled":
            glow = scaled(soft_blur(below, 9 * S), 0.16 if state == "press" else 0.30)
            over(img, ac, ImageChops.subtract(glow, pad_mask))
        fill = Image.composite(Image.new("RGB", (w, h), bot), Image.new("RGB", (w, h), top), vgrad(w, h, 0, 255))
        fl = fill.convert("RGBA")
        fl.putalpha(mask)
        img.alpha_composite(fl, (g, g))
        over(img, (255, 255, 255), ImageChops.multiply(vgrad(w, h, 64, 0, 0.55), mask), (g, g))
        over(img, (255, 255, 255), scaled(ImageChops.multiply(rim, lit), 0.45), (g, g))
        if state == "disabled":
            img.putalpha(scaled(img.getchannel("A"), 0.62))
    else:
        a0 = th["btn"]
        a = {"hover": th["btn_hover"], "press": th["btn_press"], "disabled": a0 * 0.6}.get(state, a0)
        sh = scaled(soft_blur(below, 4 * S), 0.30 if th["dark"] else 0.15)
        over(img, (0, 0, 0), ImageChops.subtract(sh, pad_mask))
        over(img, (255, 255, 255), scaled(mask, a), (g, g))
        over(img, (255, 255, 255), ImageChops.multiply(vgrad(w, h, 36, 0, 0.5), mask), (g, g))
        over(img, (255, 255, 255), scaled(ImageChops.multiply(rim, lit), th["rim"] * 0.8), (g, g))
        if th["rim_dark"]:
            over(img, (0, 0, 0), scaled(ImageChops.multiply(rim, ImageChops.invert(lit)), th["rim_dark"]), (g, g))
    return img


def ring_image(w, h, r, th, S):
    """Accent focus ring (outline + soft halo) for an input field."""
    g = int(round(8 * S))
    img = Image.new("RGBA", (w + 2 * g, h + 2 * g), (0, 0, 0, 0))
    shape = Image.new("L", img.size, 0)
    shape.paste(shape_mask(w, h, r), (g, g))
    halo = ImageChops.subtract(scaled(soft_blur(shape, 4 * S), 0.55), shape)
    over(img, th["accent"], halo)
    outline = Image.new("L", img.size, 0)
    outline.paste(ring_mask(w, h, r, max(2, int(round(2 * S)))), (g, g))
    over(img, th["accent"], scaled(outline, 0.95))
    return img


def bar_image(w, h, th, S, phase=None):
    """Progress fill: gradient pill with gloss, glow and an optional moving shimmer."""
    g = int(round(9 * S))
    img = Image.new("RGBA", (w + 2 * g, h + 2 * g), (0, 0, 0, 0))
    mask = shape_mask(w, h, h // 2)
    full = Image.new("L", img.size, 0)
    full.paste(mask, (g, g))
    over(img, th["accent"], ImageChops.subtract(scaled(soft_blur(full, 5 * S), 0.55), full))
    ac, ac2 = th["accent"], th["accent2"]
    fill = Image.composite(Image.new("RGB", (w, h), ac2), Image.new("RGB", (w, h), shade(ac, 18)), hgrad(w, h, 0, 255))
    fl = fill.convert("RGBA")
    fl.putalpha(mask)
    img.alpha_composite(fl, (g, g))
    over(img, (255, 255, 255), ImageChops.multiply(vgrad(w, h, 120, 0, 0.6), mask), (g, g))
    if phase is not None:
        bw = max(int(70 * S), w // 4)
        band = Image.new("L", (33, 1))
        band.putdata([int(255 * (1 - abs(i - 16) / 16)) for i in range(33)])
        band = band.resize((bw, h), Image.BILINEAR)
        lane = Image.new("L", (w, h), 0)
        lane.paste(band, (int(phase * (w + bw)) - bw, 0))
        over(img, (255, 255, 255), scaled(ImageChops.multiply(lane, mask), 0.35), (g, g))
    return img


def pill(w, h, fill_rgba, th, S, outline=None):
    """RGBA capsule filled with an (r, g, b, a) colour, optionally with a hairline outline."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    over(img, fill_rgba[:3], scaled(shape_mask(w, h, h // 2), fill_rgba[3]))
    if outline:
        over(img, outline[:3], scaled(ring_mask(w, h, h // 2, max(1, int(S))), outline[3]))
    return img


def switch_image(w, h, on, th, S, enabled=True):
    """iOS-style toggle: coloured track, white knob with a soft shadow. `on` may be 0..1 for the glide animation."""
    g = int(round(4 * S))
    t = float(on)
    img = Image.new("RGBA", (w + 2 * g, h + 2 * g), (0, 0, 0, 0))
    off, onc = th["switch_off"], tuple(th["ok"]) + (1.0,)
    track = pill(w, h, tuple(off[i] + (onc[i] - off[i]) * t for i in range(4)), th, S)
    img.alpha_composite(track, (g, g))
    d = int(h - 4 * S)
    lo, hi = int(2 * S), w - d - int(2 * S)
    kx = g + int(lo + (hi - lo) * t)
    ky = g + (h - d) // 2
    full = Image.new("L", img.size, 0)
    full.paste(shape_mask(d, d, d // 2), (kx, ky + int(S)))
    over(img, (0, 0, 0), scaled(soft_blur(full, 2.5 * S), 0.34))
    knob = Image.new("RGBA", (d, d), tuple(th["knob"]) + (0,))
    knob.putalpha(shape_mask(d, d, d // 2))
    img.alpha_composite(knob, (kx, ky))
    if not enabled:
        img.putalpha(scaled(img.getchannel("A"), 0.5))
    return img


def segmented_image(w, h, n, sel, th, S, enabled=True):
    """Segmented control: soft capsule with a lifted selected segment. `sel` may be fractional (mid-animation).
    Labels are drawn by the caller."""
    g = int(round(5 * S))
    img = Image.new("RGBA", (w + 2 * g, h + 2 * g), (0, 0, 0, 0))
    img.alpha_composite(pill(w, h, th["seg"], th, S), (g, g))
    inset = max(2, int(round(2 * S)))
    sw = (w - 2 * inset) / n
    x0, x1 = int(inset + sw * sel), int(inset + sw * (sel + 1))
    pw, ph = x1 - x0, h - 2 * inset
    full = Image.new("L", img.size, 0)
    full.paste(shape_mask(pw, ph, ph // 2), (g + x0, g + inset + int(S)))
    over(img, (0, 0, 0), scaled(soft_blur(full, 2.5 * S), 0.22 if th["dark"] else 0.16))
    over(img, (255, 255, 255), scaled(shape_mask(pw, ph, ph // 2), th["seg_sel"][3]), (g + x0, g + inset))
    if th["dark"]:
        over(img, (255, 255, 255), scaled(ring_mask(pw, ph, ph // 2, max(1, int(S))), 0.10), (g + x0, g + inset))
    if not enabled:
        img.putalpha(scaled(img.getchannel("A"), 0.5))
    return img


def chip_image(w, h, on, hover, th, S):
    """Small service chip: quiet when idle, accent-tinted when it matches what the user pasted."""
    ac = th["accent"]
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    if on:
        over(img, ac, scaled(shape_mask(w, h, h // 2), 0.16))
        over(img, ac, scaled(ring_mask(w, h, h // 2, max(1, int(round(1.2 * S)))), 0.70))
    else:
        base = th["seg"]
        a = base[3] * (1.7 if hover else 1.0)
        over(img, base[:3], scaled(shape_mask(w, h, h // 2), min(1.0, a)))
    return img


def field_image(w, h, focus, th, S):
    """Solid text-input capsule (the Entry on top uses the same fill so they merge)."""
    g = int(round(8 * S))
    img = Image.new("RGBA", (w + 2 * g, h + 2 * g), (0, 0, 0, 0))
    if focus:
        img.alpha_composite(ring_image(w, h, h // 2, th, S), (0, 0))
    base = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    over(base, th["well"], shape_mask(w, h, h // 2))
    if not focus:
        er, eg, eb, ea = th["well_edge"]
        over(base, (er, eg, eb), scaled(ring_mask(w, h, h // 2, max(1, int(S))), ea))
    img.alpha_composite(base, (g, g))
    return img


def option_image(w, h, sel, hover, th, S, r=None):
    """A selectable tile (quality presets): faint glass at rest, accent-tinted with a ring when chosen."""
    r = r if r is not None else max(10, h // 5)
    g = int(round(6 * S))
    img = Image.new("RGBA", (w + 2 * g, h + 2 * g), (0, 0, 0, 0))
    m = shape_mask(w, h, r)
    if sel:
        full = Image.new("L", img.size, 0)
        full.paste(m, (g, g))
        over(img, th["accent"], ImageChops.subtract(scaled(soft_blur(full, 5 * S), 0.30), full))
        over(img, th["accent"], scaled(m, 0.15), (g, g))
        over(img, th["accent"], scaled(ring_mask(w, h, r, max(2, int(round(1.8 * S)))), 0.85), (g, g))
    else:
        base = th["seg"]
        over(img, base[:3], scaled(m, min(1.0, base[3] * (1.9 if hover else 1.0))), (g, g))
        er, eg, eb, ea = th["well_edge"]
        over(img, (er, eg, eb), scaled(ring_mask(w, h, r, max(1, int(S))), ea), (g, g))
    return img


def panel_image(w, h, r, th, S):
    """Inset group for settings rows: a faint rounded plate with a hairline edge."""
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    m = shape_mask(w, h, r)
    over(img, th["group"][:3], scaled(m, th["group"][3]))
    er, eg, eb, ea = th["well_edge"]
    over(img, (er, eg, eb), scaled(ring_mask(w, h, r, max(1, int(S))), ea))
    return img


def popup_image(w, h, r, th, S):
    """A floating menu sheet: opaque enough to read over anything, with a soft shadow."""
    g = int(round(22 * S))
    img = Image.new("RGBA", (w + 2 * g, h + 2 * g), (0, 0, 0, 0))
    m = shape_mask(w, h, r)
    full = Image.new("L", img.size, 0)
    full.paste(m, (g, g + int(6 * S)))
    over(img, (0, 0, 0), scaled(soft_blur(full, 10 * S), 0.45 if th["dark"] else 0.22))
    fill = (38, 40, 54) if th["dark"] else (252, 252, 254)
    over(img, fill, m, (g, g))
    er, eg, eb, ea = th["well_edge"]
    over(img, (er, eg, eb), scaled(ring_mask(w, h, r, max(1, int(S))), min(1.0, ea * 1.6)), (g, g))
    return img


def tile_image(size, th, S, data=None, radius=None):
    """Square artwork tile with squircle corners: the cover if we have one, else a quiet note on a soft fill."""
    r = radius if radius is not None else max(4, size // 5)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    art = None
    if data:
        try:
            art = Image.open(io.BytesIO(data)).convert("RGB")
            s = min(art.size)
            art = art.crop(((art.width - s) // 2, (art.height - s) // 2,
                            (art.width - s) // 2 + s, (art.height - s) // 2 + s)).resize((size, size), Image.LANCZOS)
        except Exception:
            art = None
    mask = shape_mask(size, size, r)
    if art is not None:
        art = art.convert("RGBA")
        art.putalpha(mask)
        img.alpha_composite(art)
        over(img, (0, 0, 0) if not th["dark"] else (255, 255, 255), scaled(ring_mask(size, size, r, max(1, int(S))), 0.10))
    else:
        over(img, th["tile"][:3], scaled(mask, th["tile"][3] * 1.6))
        note = note_glyph(int(size * 0.5), th["fg3"])
        img.alpha_composite(note, ((size - note.width) // 2, (size - note.height) // 2))
    return img


def thumb_image(w, h, color):
    """Scrollbar thumb."""
    m = shape_mask(w, h, w // 2)
    img = Image.new("RGBA", (w, h), tuple(color[:3]) + (0,))
    img.putalpha(scaled(m, 0.55))
    return img


# ---------------------------------------------------------------- icons

def _draw(size, fn, ss=4):
    img = Image.new("RGBA", (size * ss, size * ss), (0, 0, 0, 0))
    fn(ImageDraw.Draw(img), size * ss)
    return img.resize((size, size), Image.LANCZOS)


def _stroke(d, pts, wd, fill):
    d.line(pts, fill=fill, width=int(wd), joint="curve")
    for x, y in (pts[0], pts[-1]):
        d.ellipse((x - wd / 2, y - wd / 2, x + wd / 2, y + wd / 2), fill=fill)


def status_icon(kind, size, color):
    white = (255, 255, 255, 255)

    def fn(d, s):
        d.ellipse((0, 0, s - 1, s - 1), fill=tuple(color) + (255,))
        wd = s * 0.11
        if kind == "check":
            _stroke(d, [(s * .28, s * .52), (s * .44, s * .67), (s * .73, s * .35)], wd, white)
        elif kind == "skip":
            _stroke(d, [(s * .33, s * .32), (s * .49, s * .5), (s * .33, s * .68)], wd, white)
            _stroke(d, [(s * .52, s * .32), (s * .68, s * .5), (s * .52, s * .68)], wd, white)
        elif kind == "warn":
            _stroke(d, [(s * .5, s * .27), (s * .5, s * .54)], wd, white)
            d.ellipse((s * .5 - wd * .6, s * .69 - wd * .6, s * .5 + wd * .6, s * .69 + wd * .6), fill=white)
        else:
            _stroke(d, [(s * .33, s * .33), (s * .67, s * .67)], wd, white)
            _stroke(d, [(s * .67, s * .33), (s * .33, s * .67)], wd, white)
    return _draw(size, fn)


def note_glyph(size, color):
    """Quiet beamed-note glyph (empty states, artwork placeholders)."""
    def fn(d, s):
        c = tuple(color) + (255,)
        d.ellipse((s * .16, s * .60, s * .42, s * .80), fill=c)
        d.ellipse((s * .52, s * .52, s * .78, s * .72), fill=c)
        d.rectangle((s * .38, s * .22, s * .43, s * .70), fill=c)
        d.rectangle((s * .74, s * .14, s * .79, s * .62), fill=c)
        d.polygon([(s * .38, s * .22), (s * .79, s * .14), (s * .79, s * .26), (s * .38, s * .34)], fill=c)
    return _draw(size, fn)


def gear_icon(size, color):
    col = tuple(color) + (255,)

    def fn(d, s):
        c = s / 2
        for i in range(8):
            a = i * math.pi / 4
            ca, sa = math.cos(a), math.sin(a)
            hw, r0, r1 = s * 0.085, s * 0.30, s * 0.46
            px, py = -sa, ca
            d.polygon([(c + ca * r0 + px * hw, c + sa * r0 + py * hw), (c + ca * r1 + px * hw * .85, c + sa * r1 + py * hw * .85),
                       (c + ca * r1 - px * hw * .85, c + sa * r1 - py * hw * .85), (c + ca * r0 - px * hw, c + sa * r0 - py * hw)],
                      fill=col)
        d.ellipse((c - s * .33, c - s * .33, c + s * .33, c + s * .33), fill=col)
        d.ellipse((c - s * .14, c - s * .14, c + s * .14, c + s * .14), fill=(0, 0, 0, 0))
    return _draw(size, fn)


def chevron_icon(size, color, direction="left"):
    col = tuple(color) + (255,)

    def fn(d, s):
        pts = [(s * .62, s * .22), (s * .36, s * .5), (s * .62, s * .78)]
        if direction == "right":
            pts = [(s - x, y) for x, y in pts]
        elif direction in ("down", "up"):
            pts = [(s * .22, s * .38), (s * .5, s * .64), (s * .78, s * .38)]
            if direction == "up":
                pts = [(x, s - y) for x, y in pts]
        _stroke(d, pts, s * 0.11, col)
    return _draw(size, fn)


def sparkle_icon(size, color):
    """Four-point sparkle: the AI marker."""
    col = tuple(color) + (255,)

    def fn(d, s):
        c = s / 2
        pts = []
        for i in range(8):
            a = -math.pi / 2 + i * math.pi / 4
            r = s * (0.46 if i % 2 == 0 else 0.13)
            pts.append((c + r * math.cos(a), c + r * math.sin(a)))
        d.polygon(pts, fill=col)
        d.ellipse((s * .70, s * .12, s * .86, s * .28), fill=col)
    return _draw(size, fn)


def x_icon(size, color):
    col = tuple(color) + (255,)

    def fn(d, s):
        _stroke(d, [(s * .30, s * .30), (s * .70, s * .70)], s * .10, col)
        _stroke(d, [(s * .70, s * .30), (s * .30, s * .70)], s * .10, col)
    return _draw(size, fn)


def app_icon(size):
    """Gradient squircle with glossy highlight and a pair of beamed notes."""
    ss = 4
    s = size * ss
    mask = shape_mask(s, s, int(s * 0.26), ss=1, n=4.0)
    grad = Image.composite(Image.new("RGB", (s, s), (112, 86, 235)), Image.new("RGB", (s, s), (24, 140, 255)),
                           corners(s, s, 0, 120, 140, 255))
    img = grad.convert("RGBA")
    img.putalpha(mask)
    over(img, (255, 255, 255), ImageChops.multiply(vgrad(s, s, 110, 0, 0.55), mask))
    notes = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(notes)
    white = (255, 255, 255, 255)
    d.ellipse((s * .245, s * .625, s * .475, s * .795), fill=white)
    d.ellipse((s * .565, s * .565, s * .795, s * .735), fill=white)
    d.rectangle((s * .435, s * .30, s * .475, s * .71), fill=white)
    d.rectangle((s * .755, s * .24, s * .795, s * .65), fill=white)
    d.polygon([(s * .435, s * .30), (s * .795, s * .24), (s * .795, s * .335), (s * .435, s * .395)], fill=white)
    over(img, (20, 20, 90), scaled(soft_blur(notes.getchannel("A"), 6 * ss), 0.35))
    img.alpha_composite(notes)
    over(img, (255, 255, 255), scaled(ImageChops.multiply(ring_mask(s, s, int(s * .26), max(2, ss)), corners(s, s, 255, 60, 60, 160)), 0.7))
    return img.resize((size, size), Image.LANCZOS)


def glow_ring(size, color, alpha=1.0, width=0.04):
    """A soft luminous ring (the launch animation's halo)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ring = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(ring)
    w = max(2, int(size * width))
    d.ellipse((size * .16, size * .16, size * .84, size * .84), outline=255, width=w)
    ring = soft_blur(ring, max(2.0, size * 0.03))
    over(img, color, scaled(ring, alpha))
    return img


def fit(text, font, maxw):
    """Ellipsize `text` to at most `maxw` px in `font`."""
    if maxw <= 0:
        return ""
    if font.measure(text) <= maxw:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid].rstrip() + "…") <= maxw:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo].rstrip() + "…"
