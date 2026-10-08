"""
shell.py — the window machinery under the app: one Tk canvas, hit regions, pill buttons, text fields, a popup
menu, scrolling, animations and content transitions. It knows nothing about music; app.py builds the screens.

Everything visible is either part of a pre-rendered glass "scene" image or a canvas item on top of it, so
changing a value only moves a few items and the glass is re-rendered only when the layout changes.
"""
import logging
import time
import tkinter as tk
from tkinter import font as tkfont

from PIL import Image, ImageTk

from .. import platform_
from ..audio.sounds import Sounds
from . import glass as gk
from . import icon
from .glass import hx, mix

log = logging.getLogger("musicdl")
TITLE = "Music Downloader"
FADE_TYPES = ("text", "line", "rectangle", "oval", "polygon", "arc")


class Shell:
    def __init__(self, settings, title=TITLE, size=(980, 800), minimum=(820, 700)):
        platform_.prepare_process()
        self.s = settings
        self.root = root = tk.Tk()
        root.title(title)
        self.S = platform_.ui_scale(root)
        self.D = 1                                   # pixels per point of the pictures (the macOS edition uses 2 on Retina)
        self.name = self._theme_name()
        self.th = gk.THEMES[self.name]
        sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
        w, h = int(min(size[0] * self.S, sw * 0.92)), int(min(size[1] * self.S, sh * 0.92))
        root.geometry(f"{w}x{h}+{(sw - w) // 2}+{max(0, (sh - h) // 3)}")
        root.minsize(min(w, int(minimum[0] * self.S)), min(h, int(minimum[1] * self.S)))
        self._make_fonts()
        self._icon = ImageTk.PhotoImage(icon.app_icon(256))
        self._set_icon()
        self.cv = tk.Canvas(root, highlightthickness=0, bd=0, bg=hx(self.th["base"]))
        self.cv.pack(fill="both", expand=True)

        self.W = self.H = 0
        self.scene = None
        self.alive = True
        self.photos, self.cache = {}, {}
        self.regions = []                        # dicts: box, cb, group, hover, press, move, release, sound, cursor
        self.buttons = {}
        self.anims = {}
        self.entries = {}
        self.scrollers = {}
        self.popup_state = None
        self.sheet_state = None
        self._hover = self._pressed = None
        self._relayout_job = None
        self._last = time.time()
        self.busy = False                        # something is moving on its own (progress shimmer etc.)
        self.splashing = False                   # the launch animation is playing: the screen behind it takes no input
        self.sounds = Sounds(lambda: self.s)

        cv = self.cv
        cv.bind("<Configure>", self._on_configure)
        cv.bind("<ButtonPress-1>", self._on_press)
        cv.bind("<B1-Motion>", self._on_drag)
        cv.bind("<ButtonRelease-1>", self._on_release)
        cv.bind("<Motion>", self._on_motion)
        cv.bind("<Leave>", lambda e: self._set_hover(None))
        platform_.bind_wheel(root, self._on_wheel)
        root.bind("<Escape>", lambda e: self.on_escape())
        for key in ("<Escape>", "<space>", "<Return>"):                       # these skip the launch animation
            root.bind(key, lambda e: self.skip_splash() if self.splashing else None, add="+")
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._loop_job = root.after(33, self._loop)

    # ---------------------------------------------------------------- hooks for the app
    def compute_layout(self): ...
    def render_scene(self): return gk.make_background(self.W, self.H, self.th, self.S)
    def paint(self): ...
    def on_tick(self, now, dt): ...
    def on_sheet(self): ...                   # a question just opened or closed
    def skip_splash(self): ...                # a click or key while the launch animation plays
    def on_escape(self):
        if self.sheet_state:
            self._sheet_pick(self.sheet_state["default"])
        else:
            self.close_popup()
    def close(self): self.destroy()

    # ---------------------------------------------------------------- setup helpers
    def _theme_name(self):
        return self.s.appearance if self.s.appearance in ("light", "dark") else platform_.system_theme()

    def _make_fonts(self):
        p, root = self.p, self.root
        fams = set(tkfont.families(root))
        cands = platform_.font_candidates()
        pick = lambda names: next((n for n in names if n in fams), "TkDefaultFont")
        body, semi, disp = pick(cands["body"]), pick(cands["semi"]), pick(cands["display"])
        mk = lambda fam, px: tkfont.Font(root=root, family=fam, size=-p(px))
        self.f_title, self.f_big, self.f_stat = mk(semi, 19), mk(disp, 44), mk(disp, 26)
        self.f_h, self.f_semi, self.f_btn = mk(semi, 17), mk(semi, 14), mk(semi, 14)
        self.f_body, self.f_small, self.f_chip = mk(body, 14), mk(body, 13), mk(semi, 13)
        self.f_tiny = mk(semi, 11.5)
        self.f_mono = mk(pick(("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono")), 13)

    def p(self, v):
        return int(round(v * self.S))

    def tone(self, name):
        th = self.th
        return hx(th[name] if name in th else th["fg2"])

    def _set_icon(self):
        """The taskbar and Alt+Tab icon. On Windows a multi-size .ico (16–256 px, each drawn for its size) is crisper
        than one big picture shrunk by the system; elsewhere, or if the .ico can't be written, the 256-px picture."""
        path = icon.ico_file() if platform_.IS_WINDOWS else None
        if path:
            try:
                self.root.iconbitmap(default=path)
                return
            except tk.TclError:
                log.debug("iconbitmap failed", exc_info=True)
        try:
            self.root.iconphoto(True, self._icon)
        except tk.TclError:
            pass

    def tkphoto(self, img, scale=None):
        """A Tk photo of `img` (the macOS edition resamples denser pictures here; Windows draws one pixel per point)."""
        return ImageTk.PhotoImage(img)

    def photo(self, key, img, scale=None):
        ph = self.photos[key] = self.tkphoto(img, scale)
        return ph

    def cached(self, key, make, scale=None):
        ph = self.cache.get(key)
        if ph is None:
            if len(self.cache) > 700:
                self.cache.clear()
            ph = self.cache[key] = self.tkphoto(make(), scale)
        return ph

    _HOME = {"nw": (0, 0), "n": (.5, 0), "ne": (1, 0), "w": (0, .5), "center": (.5, .5), "e": (1, .5), "sw": (0, 1),
             "s": (.5, 1), "se": (1, 1)}

    def corner(self, x, y, ph, anchor="nw"):
        """Top left of `ph` placed with `anchor` at (x, y)."""
        fx, fy = self._HOME[anchor]
        return x - fx * ph.width(), y - fy * ph.height()

    def put(self, x, y, ph, anchor="nw", tags=()):
        """create_image for a photo made by photo/cached (the macOS edition places denser pictures here; Windows draws
        one pixel per point, so this is create_image as it is)."""
        return self.cv.create_image(x, y, anchor=anchor, image=ph, tags=tags)

    def box(self, r):
        p = self.p
        x, y, w, h = r
        return (p(x), p(y), p(x + w), p(y + h))

    def bg_at(self, x, y):
        """Colour of the glass scene under a canvas point (used to fade text into the background)."""
        try:
            return self.scene.getpixel((max(0, min(self.W - 1, int(x))), max(0, min(self.H - 1, int(y)))))[:3]
        except Exception:
            return self.th["base"]

    # ---------------------------------------------------------------- sound
    def cue(self, name):
        self.sounds.play(name)

    # ---------------------------------------------------------------- hit regions
    def region(self, box, cb=None, group="view", hover=None, press=None, move=None, release=None, sound="click",
               cursor=True, enabled=lambda: True, key=None, track=None):
        """`hover(on)` runs when the pointer enters or leaves; `track(event)` on every move while it is inside."""
        r = dict(box=tuple(box), cb=cb, group=group, hover=hover, press=press, move=move, release=release,
                 sound=sound, cursor=cursor, enabled=enabled, key=key, track=track)
        self.regions.append(r)
        return r

    def clear_regions(self, *groups):
        self.regions = [r for r in self.regions if r["group"] not in groups]
        if self._hover and self._hover["group"] in groups:
            self._hover = None
        if self._pressed and self._pressed["group"] in groups:
            self._pressed = None

    def _find(self, x, y):
        if self.splashing:
            return None
        sheet = [r for r in self.regions if r["group"] == "sheet"]
        pop = sheet or [r for r in self.regions if r["group"] == "popup"]
        pool = pop if pop and (sheet or not (self.popup_state or {}).get("quiet")) else self.regions
        for r in reversed(pool):
            x0, y0, x1, y1 = r["box"]
            if x0 <= x <= x1 and y0 <= y <= y1 and r["enabled"]():
                return r
        return None

    def _set_hover(self, r):
        if r is self._hover:
            return
        old, self._hover = self._hover, r
        if old and old["hover"]:
            old["hover"](False)
        if r and r["hover"]:
            r["hover"](True)
        self.cv.config(cursor="hand2" if r and r["cursor"] else "")

    def _on_motion(self, e):
        r = self._find(e.x, e.y)
        self._set_hover(r)
        if r and r["track"]:
            r["track"](e)

    def _on_press(self, e):
        if self.splashing:
            self.skip_splash()
            return
        if self.popup_state and not any(r["group"] == "popup" and r["box"][0] <= e.x <= r["box"][2]
                                        and r["box"][1] <= e.y <= r["box"][3] for r in self.regions):
            quiet = self.popup_state.get("quiet")
            self.close_popup()
            if not quiet:                                  # a menu swallows the click that closes it; a suggestion list does not
                return
        self.cv.focus_set()
        r = self._find(e.x, e.y)
        self._pressed = r
        if r and r["press"]:
            r["press"](e)

    def _on_drag(self, e):
        r = self._pressed
        if r and r["move"]:
            r["move"](e)

    def _on_release(self, e):
        r, self._pressed = self._pressed, None
        if not r:
            return
        if r["release"]:
            r["release"](e)
        if r["cb"] and self._find(e.x, e.y) is r:
            if r["sound"]:
                self.cue(r["sound"])
            r["cb"]()

    # ---------------------------------------------------------------- buttons
    def add_button(self, key, x, y, w, h, label, kind, cmd, color=None, icon=None, group="chrome", font=None,
                   sound="click", px=False, clip=None):
        """A pill button. x/y/w/h are logical pixels (canvas pixels with px=True). `group` is both the canvas tag and the
        region group, so a whole screen of buttons can be dropped with one delete + clear_regions. `clip` = (y0, y1)
        limits the clickable part (for buttons inside scrolling lists)."""
        cv = self.cv
        p = (lambda v: int(round(v))) if px else self.p
        x, y, w, h = p(x), p(y), p(w), p(h)
        b = dict(key=key, w=w, h=h, kind=kind, label=label, color=color, icon=icon, enabled=True,
                 hover=False, press=False, cmd=cmd, group=group, font=font)
        g = int(round(14 * self.S))
        b["img"] = cv.create_image(x - g, y - g, anchor="nw", image=self._btn_photo(b), tags=(group, "b_" + key))
        if icon is not None:
            ph = icon if isinstance(icon, ImageTk.PhotoImage) else self.photo("ico_" + key, icon)
            b["ico"] = cv.create_image(x + w // 2, y + h // 2, image=ph, tags=(group, "b_" + key))
        else:
            b["txt"] = cv.create_text(x + w // 2, y + h // 2, text=label, font=font or self.f_btn,
                                      fill=self._btn_color(b), tags=(group, "b_" + key))

        def hover(on):
            b["hover"] = on
            if not on:
                b["press"] = False
            self.refresh_button(key)

        def press(e):
            b["press"] = True
            self.refresh_button(key)

        def release(e):
            b["press"] = False
            self.refresh_button(key)

        y0, y1 = (y, y + h) if clip is None else (max(y, clip[0]), min(y + h, clip[1]))
        if y1 > y0:
            self.region((x, y0, x + w, y1), cb=lambda: b["enabled"] and cmd(), group=group, hover=hover,
                        press=press, release=release, sound=sound, enabled=lambda: b["enabled"], key="btn_" + key)
        self.buttons[key] = b
        return b

    def _btn_state(self, b):
        if not b["enabled"]:
            return "disabled"
        return "press" if b["press"] else "hover" if b["hover"] else "normal"

    def _btn_photo(self, b):
        th, S = self.th, self.S
        w, h, st = b["w"], b["h"], self._btn_state(b)
        return self.cached(("btn", b["kind"], w, h, st, self.name), lambda: gk.button_image(w, h, b["kind"], st, th, S))

    def _btn_color(self, b):
        th, st = self.th, self._btn_state(b)
        if b["kind"] == "primary":
            return "#ffffff" if st != "disabled" else hx(mix((255, 255, 255), th["accent"], 0.2))
        if st == "disabled":
            return hx(th["fg3"])
        return hx(b["color"] or th["fg"])

    def refresh_button(self, key):
        b = self.buttons.get(key)
        if not b or not self.cv.find_withtag("b_" + key):
            return
        self.cv.itemconfigure(b["img"], image=self._btn_photo(b))
        if "txt" in b:
            self.cv.itemconfigure(b["txt"], text=b["label"], fill=self._btn_color(b))

    def set_button(self, key, **kw):
        b = self.buttons.get(key)
        if b:
            b.update(kw)
            self.refresh_button(key)

    def segmented(self, key, box, labels, sel, on_pick, group, font=None, sound="nav", enabled=True):
        """Segmented control in canvas px `box`; the selection capsule glides to the tapped segment."""
        cv, th, S = self.cv, self.th, self.S
        x0, y0, x1, y1 = box
        w, h, n = x1 - x0, y1 - y0, len(labels)
        g = int(round(5 * S))
        img = lambda t: self.cached(("seg", w, h, n, round(t * 8), self.name),
                                    lambda: gk.segmented_image(w, h, n, round(t * 8) / 8, th, S))
        item = cv.create_image(x0 - g, y0 - g, anchor="nw", image=img(sel), tags=(group,))
        seg_w = w / n
        texts = [cv.create_text(x0 + seg_w * (i + 0.5), (y0 + y1) / 2, text=lab, font=font or self.f_chip,
                                fill=hx(th["fg"] if i == sel else th["fg2"]), tags=(group,)) for i, lab in enumerate(labels)]
        state = {"sel": sel}

        def pick(i):
            old = state["sel"]
            if i == old:
                return
            state["sel"] = i
            for k, t in enumerate(texts):
                cv.itemconfigure(t, fill=hx(th["fg"] if k == i else th["fg2"]))
            self.animate(("seg", key), 0.22, lambda f: cv.itemconfigure(item, image=img(old + (i - old) * f)))
            on_pick(i)
        if enabled:
            for i in range(n):
                self.region((x0 + seg_w * i, y0, x0 + seg_w * (i + 1), y1), cb=lambda i=i: pick(i), group=group,
                            sound=sound)
        return dict(item=item, texts=texts)

    # ---------------------------------------------------------------- animation
    _rm_at, _rm = -99.0, False

    def reduced(self):
        """Less animation: the Motion setting, or Windows' own animation switch (asked about at most once a minute)."""
        if self.s.motion == "reduced":
            return True
        now = time.time()
        if now - self._rm_at > 60.0:
            self._rm_at, self._rm = now, platform_.reduce_motion()
        return self._rm

    def animate(self, key, dur, update, ease=gk.ease, done=None, delay=0.0):
        """Run update(eased 0..1) every frame for `dur` seconds. A new animation with the same key replaces the old."""
        self.anims[key] = dict(t0=time.time() + delay, dur=max(0.001, dur), update=update, ease=ease, done=done)

    def stop_anim(self, key):
        self.anims.pop(key, None)

    def _loop(self):
        if not self.alive:
            return
        now = time.time()
        dt, self._last = min(0.1, now - self._last), now
        finished = []
        for key, a in list(self.anims.items()):
            f = (now - a["t0"]) / a["dur"]
            if f < 0:
                continue
            try:
                a["update"](a["ease"](min(1.0, f)))
            except tk.TclError:
                finished.append(key)
                continue
            if f >= 1.0:
                finished.append(key)
        for key in finished:
            a = self.anims.pop(key, None)
            if a and a["done"]:
                a["done"]()
        try:
            self.on_tick(now, dt)
        except tk.TclError:
            pass
        except Exception:
            log.exception("tick failed")
        try:
            hidden = not self.root.winfo_viewable()                    # minimised: nobody sees the frames
        except tk.TclError:
            hidden = False
        self._loop_job = self.root.after(250 if hidden else 16 if self.anims else (33 if self.busy else 90), self._loop)

    # ---------------------------------------------------------------- content transitions
    def _ids(self, tag):
        """Canvas item ids under a tag, or under any of several (a tuple); each once, in stacking order."""
        if isinstance(tag, str):
            return list(self.cv.find_withtag(tag))
        seen = set()
        for t in tag:
            seen.update(self.cv.find_withtag(t))
        return [i for i in self.cv.find_all() if i in seen]

    def _fade_items(self, tag):
        """Snapshot every canvas item under `tag` so it can be faded/slid and restored."""
        out = []
        for it in self._ids(tag):
            kind = self.cv.type(it)
            x, y = (self.cv.coords(it) or (0, 0))[:2]
            bg = self.bg_at(x, y)
            if kind in ("text", "line", "rectangle", "oval", "polygon", "arc"):
                opt = "fill" if kind in ("text", "line", "polygon") else "outline"
                if kind in ("rectangle", "oval") and self.cv.itemcget(it, "fill"):
                    opt = "fill"
                final = self.cv.itemcget(it, opt)
                out.append((it, kind, opt, final, bg))
            else:
                out.append((it, kind, None, None, bg))
        return out

    @staticmethod
    def _rgb(color, widget):
        try:
            r, g, b = widget.winfo_rgb(color)
            return (r >> 8, g >> 8, b >> 8)
        except tk.TclError:
            return None

    def reveal(self, tag, dy=10, dur=0.30, delay=0.0, key=None):
        """Fade + slide the items under `tag` into place (text dissolves in; images/buttons appear mid-way)."""
        items = self._fade_items(tag)
        if not items:
            return
        if self.reduced():                                                              # no travelling, a short fade
            dy, dur, delay = 0, min(dur, 0.18), min(delay, 0.06)
        shift = self.p(dy)
        parts = []
        for it, kind, opt, final, bg in items:
            rgb = self._rgb(final, self.cv) if opt and final else None
            parts.append((it, kind, opt, rgb, bg, 0.0))
            if opt and rgb:
                self.cv.itemconfigure(it, state="normal", **{opt: hx(bg)})
            elif not opt:
                self.cv.itemconfigure(it, state="hidden")
            self.cv.move(it, 0, shift)
        state = {"moved": 0.0}

        def update(f):
            step = (shift * f) - state["moved"]
            state["moved"] = shift * f
            for it, kind, opt, rgb, bg, _ in parts:
                try:
                    self.cv.move(it, 0, -step)
                    if opt and rgb:
                        self.cv.itemconfigure(it, **{opt: hx(mix(bg, rgb, f))})
                    elif not opt:
                        self.cv.itemconfigure(it, state="normal" if f > 0.45 else "hidden")
                except tk.TclError:
                    pass

        def done():
            for it, kind, opt, rgb, bg, _ in parts:
                try:
                    if opt and rgb:
                        self.cv.itemconfigure(it, **{opt: hx(rgb)})
                    elif not opt:
                        self.cv.itemconfigure(it, state="normal")
                except tk.TclError:
                    pass
        self.animate(key or ("reveal", tag), dur, update, gk.ease_out, done, delay)

    def dismiss(self, tag, dur=0.13, done=None, dy=-6):
        """Fade the items under `tag` out (then call done, which usually draws the replacement)."""
        items = self._fade_items(tag)
        if self.reduced():
            dy, dur = 0, min(dur, 0.07)
        shift = self.p(dy)
        parts = [(it, opt, self._rgb(final, self.cv) if opt and final else None, bg) for it, kind, opt, final, bg in items]
        if not parts:
            if done:
                done()
            return
        state = {"moved": 0.0}

        def update(f):
            step = shift * f - state["moved"]
            state["moved"] = shift * f
            for it, opt, rgb, bg in parts:
                try:
                    self.cv.move(it, 0, step)
                    if opt and rgb:
                        self.cv.itemconfigure(it, **{opt: hx(mix(rgb, bg, f))})
                    elif not opt and f > 0.5:
                        self.cv.itemconfigure(it, state="hidden")
                except tk.TclError:
                    pass
        self.animate(("dismiss", tag), dur, update, gk.ease, done)

    def swap(self, tag, redraw, animate=True):
        """Replace what is under `tag`: fade it out, run redraw() (which creates the new items), fade those in."""
        if not animate or not self.W:
            redraw()
            return

        def after_out():
            redraw()
            self.reveal(tag)
        self.dismiss(tag, done=after_out)

    def crossfade_scene(self, new_scene, dur=0.2, done=None):
        """Dissolve the glass backdrop into a new one (page changes); items must be hidden by the caller."""
        old = self.scene
        self.scene = new_scene
        item = self.cv.find_withtag("scene")
        if not item or old is None or old.size != new_scene.size or self.reduced():
            self._set_scene_image(new_scene)
            if done:
                done()
            return

        def update(f):
            self._set_scene_image(Image.blend(old, new_scene, f))

        def fin():
            self._set_scene_image(new_scene)
            if done:
                done()
        self.animate("scene-fade", dur, update, gk.ease, fin)

    def scene_photo(self, img):
        return self.photo("scene", img)

    def _set_scene_image(self, img):
        self.cv.itemconfigure("scene", image=self.scene_photo(img))

    # ---------------------------------------------------------------- scrolling
    def scroller(self, key, area, total, redraw, step=48):
        """Register a scrollable area (canvas px). Returns the clamped offset. `redraw()` repaints its items."""
        sc = self.scrollers.get(key) or dict(off=0)
        sc.update(area=area, total=total, redraw=redraw, step=step)
        sc["off"] = max(0, min(sc["off"], max(0, total - (area[3] - area[1]))))
        self.scrollers[key] = sc
        return sc["off"]

    def drop_scroller(self, key):
        self.scrollers.pop(key, None)

    def scroll_to(self, key, off):
        sc = self.scrollers.get(key)
        if sc:
            sc["off"] = max(0, min(off, max(0, sc["total"] - (sc["area"][3] - sc["area"][1]))))
            sc["redraw"]()

    def _on_wheel(self, e):
        if not self.W or self.sheet_state or self.popup_state and self.popup_state.get("scroll") is None:
            return
        cx = self.cv.winfo_pointerx() - self.cv.winfo_rootx()
        cy = self.cv.winfo_pointery() - self.cv.winfo_rooty()
        notches = platform_.wheel_steps(e)
        for key, sc in list(self.scrollers.items()):
            x0, y0, x1, y1 = sc["area"]
            if x0 <= cx <= x1 + self.p(16) and y0 <= cy <= y1 and sc["total"] > (y1 - y0):
                self.close_entry("row")
                sc["off"] = max(0, min(sc["off"] - int(notches * sc["step"]), sc["total"] - (y1 - y0)))
                sc["redraw"]()
                return

    def draw_scrollbar(self, key, tag):
        """A slim thumb at the right of a registered scroller (draggable)."""
        sc = self.scrollers.get(key)
        if not sc:
            return
        x0, y0, x1, y1 = sc["area"]
        h, total = y1 - y0, sc["total"]
        if total <= h:
            return
        p = self.p
        th_h = max(p(28), int(h * h / total))
        ty = y0 + (h - th_h) * (sc["off"] / (total - h))
        thumb = self.cached(("thumb", th_h, self.name), lambda: gk.thumb_image(p(5), th_h, self.th["fg3"]))
        self.cv.create_image(x1 + p(10), ty, anchor="ne", image=thumb, tags=tag)
        ratio = (total - h) / max(1, h - th_h)
        start = {}

        def press(e):
            start["y"], start["off"] = e.y, sc["off"]

        def move(e):
            self.scroll_to(key, start["off"] + (e.y - start["y"]) * ratio)
        self.region((x1 - p(4), ty, x1 + p(14), ty + th_h), group="view", press=press, move=move, sound=None)

    def cover(self, x, y, w, h, band, tag):
        """Rows scroll under a card's edges: patch those strips back in from the glass scene."""
        cv = self.cv
        up = y if band is None else min(band, y)
        if up > 0:
            cv.create_image(x, y - up, anchor="nw", image=self.photo(tag + "_top", self.scene.crop((x, y - up, x + w, y))),
                            tags=tag)
        bot = self.H - (y + h) if band is None else min(band, self.H - (y + h))
        if bot > 0:
            cv.create_image(x, y + h, anchor="nw",
                            image=self.photo(tag + "_bot", self.scene.crop((x, y + h, x + w, y + h + bot))), tags=tag)

    # ---------------------------------------------------------------- text fields
    def entry(self, key, box, text="", placeholder="", show=None, font=None, on_change=None, on_commit=None,
              on_focus=None, group="entry", fill=None, pad=16):
        """A real Tk Entry (native editing, clipboard, IME) sitting inside a rendered capsule, coloured to match."""
        self.close_entry(key)
        x0, y0, x1, y1 = box
        th = self.th
        bgc = hx(fill or th["well"])
        var = tk.StringVar(value=text)
        e = tk.Entry(self.root, textvariable=var, bd=0, highlightthickness=0, relief="flat", font=font or self.f_body,
                     bg=bgc, fg=hx(th["fg"]), insertbackground=hx(th["fg"]), selectbackground=hx(th["accent"]),
                     selectforeground="#ffffff", disabledbackground=bgc, show=show or "")
        h = y1 - y0
        ph = self.cv.create_text(x0 + self.p(pad), (y0 + y1) / 2, text=placeholder, anchor="w", font=font or self.f_body,
                                 fill=hx(th["fg3"]), tags=(group,))
        win = self.cv.create_window(x0 + self.p(pad), y0 + self.p(2), window=e, anchor="nw",
                                    width=max(10, x1 - x0 - 2 * self.p(pad)), height=max(10, h - self.p(4)), tags=(group,))
        rec = dict(widget=e, var=var, win=win, ph=ph, on_commit=on_commit)
        self.entries[key] = rec

        def sync(*_):
            self.cv.itemconfigure(ph, state="hidden" if var.get() else "normal")
            if on_change:
                on_change(var.get())
        var.trace_add("write", sync)
        sync()
        e.bind("<Return>", lambda ev: on_commit(var.get()) if on_commit else None)
        e.bind("<FocusIn>", lambda ev: on_focus(True) if on_focus else None)
        e.bind("<FocusOut>", lambda ev: on_focus(False) if on_focus else None)
        e.bind("<Escape>", lambda ev: self.cv.focus_set())
        # a canvas click on the placeholder should focus the field
        self.cv.tag_bind(ph, "<Button-1>", lambda ev: e.focus_set())
        return rec

    def close_entry(self, key):
        rec = self.entries.pop(key, None)
        if rec:
            try:
                for seq in ("<FocusIn>", "<FocusOut>", "<Return>"):
                    rec["widget"].unbind(seq)
                rec["widget"].destroy()
            except tk.TclError:
                pass
            self.cv.delete(rec["win"], rec["ph"])

    def close_entries(self):
        for k in list(self.entries):
            self.close_entry(k)

    def entry_value(self, key, default=""):
        rec = self.entries.get(key)
        return rec["var"].get() if rec else default

    # ---------------------------------------------------------------- popup menu
    def open_popup(self, anchor, items, current, on_pick, width=None, row_h=36, max_rows=7, quiet=False):
        """Floating menu under (or over) the `anchor` box (canvas px). items = [(value, label)]. A label can also be a
        dict(title, detail, tag) for a two-line row with a kind tag at its right.

        `quiet` is for a list that comes and goes while you type (suggestions): no sound, the rest of the window
        stays usable, a click elsewhere closes it and still counts, and Up/Down/Return work through popup_move and
        popup_accept."""
        self.close_popup()
        p, th, cv = self.p, self.th, self.cv
        rows = len(items)
        shown = min(rows, max_rows)
        w = width or max(anchor[2] - anchor[0], p(200))
        h = shown * p(row_h) + p(12)
        x = min(anchor[0], self.W - w - p(12))
        y = anchor[3] + p(6)
        if y + h > self.H - p(8):
            y = max(p(8), anchor[1] - h - p(6))
        g = int(round(22 * self.S))
        img = self.cached(("popup", w, h, self.name), lambda: gk.popup_image(w, h, p(14), th, self.S))
        st = dict(items=items, current=current, on_pick=on_pick, box=(x, y, x + w, y + h), row_h=p(row_h), off=0,
                  shown=shown, hover=None, scroll=True if rows > shown else None, w=w, quiet=quiet, sel=None)
        self.popup_state = st
        cv.create_image(x - g, y - g, anchor="nw", image=img, tags="popup")
        self._popup_rows()
        if not quiet:
            self.cue("nav")

    def _popup_rows(self):
        st = self.popup_state
        if not st:
            return
        cv, th, p = self.cv, self.th, self.p
        cv.delete("popup_rows")
        self.clear_regions("popup")
        x0, y0, x1, y1 = st["box"]
        fill = (38, 40, 54) if th["dark"] else (252, 252, 254)
        hl = hx(mix(fill, th["fg"], 0.10))
        total = len(st["items"])
        st["off"] = max(0, min(st["off"], total - st["shown"]))
        if st["quiet"]:
            self.region((x0, y0, x1, y1), group="popup", sound=None)       # the padding is part of the list
        for i in range(st["shown"]):
            idx = st["off"] + i
            value, label = st["items"][idx]
            ry = y0 + p(6) + i * st["row_h"]
            rich = isinstance(label, dict)
            sel = (idx == st["sel"]) if rich else value == st["current"]
            rect = cv.create_rectangle(x0 + p(6), ry, x1 - p(6), ry + st["row_h"], fill=hl if sel else "", outline="",
                                       tags=("popup", "popup_rows"))
            if rich:
                self._rich_row(label, x0, x1, ry, st, sel)
            else:
                cv.create_text(x0 + p(18), ry + st["row_h"] / 2, text=gk.fit(str(label), self.f_body, st["w"] - p(60)),
                               anchor="w", font=self.f_body, fill=hx(th["accent"] if sel else th["fg"]),
                               tags=("popup", "popup_rows"))
                if sel:
                    cv.create_text(x1 - p(18), ry + st["row_h"] / 2, text="✓", anchor="e", font=self.f_semi,
                                   fill=hx(th["accent"]), tags=("popup", "popup_rows"))

            def hover(on, rect=rect, sel=sel):
                cv.itemconfigure(rect, fill=hl if (on or sel) else "")

            self.region((x0 + p(6), ry, x1 - p(6), ry + st["row_h"]), cb=lambda v=value: self._popup_pick(v),
                        group="popup", hover=hover, sound="click")
        cv.tag_raise("popup")

    def _rich_row(self, label, x0, x1, ry, st, lit):
        """Title over a quieter line of detail, the kind of thing at the right ('Song', 'Album' …)."""
        cv, th, p = self.cv, self.th, self.p
        tag = label.get("tag", "")
        tag_w = (self.f_tiny.measure(tag) + p(14)) if tag else 0
        room = st["w"] - p(36) - tag_w
        mid = ry + st["row_h"] / 2
        two = bool(label.get("detail"))
        cv.create_text(x0 + p(18), mid - (p(9) if two else 0), text=gk.fit(label.get("title", ""), self.f_body, room),
                       anchor="w", font=self.f_body, fill=hx(th["fg"]), tags=("popup", "popup_rows"))
        if two:
            cv.create_text(x0 + p(18), mid + p(10), text=gk.fit(label["detail"], self.f_small, room), anchor="w",
                           font=self.f_small, fill=hx(th["fg3"]), tags=("popup", "popup_rows"))
        if tag:
            cv.create_text(x1 - p(18), mid, text=tag, anchor="e", font=self.f_tiny, fill=hx(th["accent"] if lit else th["fg3"]),
                           tags=("popup", "popup_rows"))

    def popup_move(self, step):
        """Up/Down in an open list: moves the highlight (wrapping), scrolling it into view. False when no list is open."""
        st = self.popup_state
        if not st or not st["items"]:
            return False
        n = len(st["items"])
        st["sel"] = (0 if step > 0 else n - 1) if st["sel"] is None else (st["sel"] + step) % n
        if st["sel"] < st["off"]:
            st["off"] = st["sel"]
        elif st["sel"] >= st["off"] + st["shown"]:
            st["off"] = st["sel"] - st["shown"] + 1
        self._popup_rows()
        return True

    def popup_accept(self):
        """Return in an open list: picks the highlighted row. False when there is none (the key is then not ours)."""
        st = self.popup_state
        if not st or st["sel"] is None:
            return False
        self._popup_pick(st["items"][st["sel"]][0])
        return True

    def _popup_pick(self, value):
        st = self.popup_state
        self.close_popup()
        if st:
            st["on_pick"](value)

    def close_popup(self):
        if self.popup_state:
            self.popup_state = None
            self.cv.delete("popup", "popup_rows")
            self.clear_regions("popup")

    # ---------------------------------------------------------------- question sheet
    def open_sheet(self, title, paragraphs, buttons, on_pick, default=None, follow=False):
        """A modal question over a dimmed window. buttons = [(key, label, kind)], laid out left to right; Esc picks
        `default` (a key). Nothing underneath can be clicked until one is chosen. `follow=True` is for a sheet that
        answers another one: it appears in place, without the dimming and sliding in again."""
        self.close_popup()
        self.close_entries()
        self.sheet_state = dict(title=title, paras=list(paragraphs), buttons=list(buttons), on_pick=on_pick,
                                default=default or buttons[-1][0], fresh=not follow)
        self.on_sheet()
        self.redraw_sheet()

    def close_sheet(self):
        if self.sheet_state:
            self.sheet_state = None
            self.on_sheet()
            self.stop_anim("sheet-dim")
            self.cv.delete("sheet", "sheet_panel")
            self.clear_regions("sheet")

    def _sheet_pick(self, key):
        st = self.sheet_state
        self.close_sheet()
        if st:
            st["on_pick"](key)

    def _dim_photo(self, level):
        W, H, dark = self.W, self.H, self.th["dark"]
        return self.cached(("dim", W, H, level, self.name),
                           lambda: Image.new("RGBA", (W, H), (0, 0, 0, int((120 if dark else 80) * level / 4))))

    def redraw_sheet(self):
        """(Re)draw the open sheet at the current size/theme; harmless when none is open."""
        cv, p, th = self.cv, self.p, self.th
        self.stop_anim("sheet-dim")
        self.stop_anim("sheet-in")
        cv.delete("sheet", "sheet_panel")
        self.clear_regions("sheet")
        st = self.sheet_state
        if not st or not self.W:
            return
        from .rows import wrap
        pw = min(p(460), self.W - p(40))
        inner = pw - p(56)
        paras = [wrap(t, self.f_body, inner, limit=6) for t in st["paras"]]
        title = wrap(st["title"], self.f_h, inner, limit=2)
        lh_t, lh_b = self.f_h.metrics("linespace"), self.f_body.metrics("linespace")
        gap = p(8)
        text_h = len(title) * lh_t + p(10) + sum(len(q) * lh_b for q in paras) + gap * max(0, len(paras) - 1)
        bh = p(44)
        ph = p(26) + text_h + p(24) + bh + p(24)
        x0, y0 = (self.W - pw) // 2, max(p(16), (self.H - ph) // 2 - p(12))
        dim = cv.create_image(0, 0, anchor="nw", image=self._dim_photo(2 if st["fresh"] else 4), tags="sheet")
        g = int(round(22 * self.S))
        img = self.cached(("sheet", pw, ph, self.name), lambda: gk.popup_image(pw, ph, p(22), th, self.S))
        cv.create_image(x0 - g, y0 - g, anchor="nw", image=img, tags=("sheet", "sheet_panel"))
        y = y0 + p(26)
        for line in title:
            cv.create_text(x0 + p(28), y, text=line, anchor="nw", font=self.f_h, fill=hx(th["fg"]),
                           tags=("sheet", "sheet_panel"))
            y += lh_t
        y += p(10)
        for q in paras:
            for line in q:
                cv.create_text(x0 + p(28), y, text=line, anchor="nw", font=self.f_body, fill=hx(th["fg2"]),
                               tags=("sheet", "sheet_panel"))
                y += lh_b
            y += gap
        n = len(st["buttons"])
        bw = (inner - p(12) * (n - 1)) // n
        by = y0 + ph - p(24) - bh
        self.region((0, 0, self.W, self.H), group="sheet", sound=None, cursor=False)         # swallows stray clicks
        for i, (key, label, kind) in enumerate(st["buttons"]):
            self.add_button("sheet_" + key, x0 + p(28) + i * (bw + p(12)), by, bw, bh, label, kind,
                            lambda k=key: self._sheet_pick(k), group="sheet", px=True, sound="click")
            cv.addtag_withtag("sheet_panel", "b_sheet_" + key)
        cv.tag_raise("sheet")
        if st["fresh"]:
            st["fresh"] = False
            self.reveal("sheet_panel", dy=10, dur=0.22, key="sheet-in")
            self.animate("sheet-dim", 0.22, lambda f: cv.itemconfigure(dim, image=self._dim_photo(2 + 2 * round(f))))

    # ---------------------------------------------------------------- layout events
    def _on_configure(self, e):
        if (e.width, e.height) == (self.W, self.H) or e.width < 50:
            return
        self.W, self.H = e.width, e.height
        if self._relayout_job:
            self.root.after_cancel(self._relayout_job)
        self._relayout_job = self.root.after(70, self._relayout)

    def _relayout(self):
        self._relayout_job = None
        self.cache.clear()
        self.compute_layout()
        self.paint()

    # ---------------------------------------------------------------- theme + chrome
    def apply_window_theme(self):
        """Title bar takes the backdrop's colour (and light/dark) so it melts into the window."""
        if not self.scene:
            return
        top = self.scene.crop((0, 0, self.W, 2)).resize((1, 1), Image.BOX).getpixel((0, 0))
        platform_.style_window(self.root, self.th["dark"], top[:3], self.th["fg"])

    def set_theme(self):
        self.name = self._theme_name()
        self.th = gk.THEMES[self.name]
        self.cv.config(bg=hx(self.th["base"]))
        self.cache.clear()
        gk.clear_caches()

    # ---------------------------------------------------------------- shutdown
    def destroy(self):
        self.alive = False
        self.sounds.close()
        for job in (self._loop_job, self._relayout_job):
            if job:
                try:
                    self.root.after_cancel(job)
                except Exception:
                    pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def mainloop(self):
        self.root.mainloop()
