"""
views_quality.py — Optimized (Easy) mode: three quality tiles, an Apple / Windows·Android choice, and the
storage-vs-quality curve.

Choosing a preset glides the dot along the curve and counts the size estimate up or down with it, so the trade-off
(Best costs a lot more space than Better for a small step up in sound) is something you see rather than read.
The curve itself always uses the same nominal sizes: the device choice only changes how each step is written
(AAC/ALAC for Apple, MP3/FLAC for everything else), so the tiles' wording and the estimate follow it, not the geometry.
"""
from ..config import DEVICE_ORDER, DEVICES, PRESET_ORDER, PRESETS, estimate_mb, preset
from . import charts, fmt
from . import glass as gk
from .glass import hx

CAPTIONS = {"good": "Great for everyday listening", "better": "Near-transparent sound",
            "best": "Lossless when the source is — never a bigger file than the source deserves"}


class QualityMixin:
    def _init_quality(self):
        self.tradeoff = charts.Tradeoff([(k, PRESETS[k]["size_kbps"], PRESETS[k]["quality"]) for k in PRESET_ORDER])
        self.qpos = float(PRESETS[self.s.preset]["size_kbps"])
        self.qest = self._real_kbps(self.s.preset)
        self.qhover = None
        self.qv = {}

    def _real_kbps(self, key):
        """Average data rate of a step in the format the current device gets (for the size estimate)."""
        return float(preset(key, self.s.device)["size_kbps"])

    # ---------------------------------------------------------------- drawing
    def draw_quality(self):
        cv, p, th = self.cv, self.p, self.th
        bx, by, bw, bh = self.rB
        qv = self.qv = {}
        if "qpos" not in self.anims:
            self.qpos = float(PRESETS[self.s.preset]["size_kbps"])
            self.qest = self._real_kbps(self.s.preset)
        cv.create_text(p(bx + 28), p(by + 22), text="Quality", anchor="nw", font=self.f_h, fill=hx(th["fg"]), tags="B")
        cv.create_text(p(bx + 28), p(by + 46), text="Cover art and song details are added automatically.", anchor="nw",
                       font=self.f_small, fill=hx(th["fg3"]), tags="B")
        # ---- device: what the music will be played on decides AAC/ALAC or MP3/FLAC
        dw = min(bw - 56, 290)
        self.segmented("device", (p(bx + bw - 28 - dw), p(by + 18), p(bx + bw - 28), p(by + 18 + 30)),
                       [DEVICES[k] for k in DEVICE_ORDER], DEVICE_ORDER.index(self.s.device), self.pick_device, "B",
                       font=self.f_chip)
        # ---- tiles
        gap = 14
        tw = (bw - 56 - 2 * gap) / 3
        ty, tile_h = by + 76, 84
        qv["tiles"], qv["details"] = {}, {}
        for i, key in enumerate(PRESET_ORDER):
            self._tile(key, bx + 28 + i * (tw + gap), ty, tw, tile_h)
        # ---- chart
        cy = ty + tile_h + 14
        ch = max(110, bh - (cy - by) - 74)
        qv["chart_box"] = (p(bx + 28), p(cy), p(bw - 56), p(ch))
        qv["chart"] = cv.create_image(p(bx + 28), p(cy), anchor="nw", image=self._chart_photo(self.qpos), tags="B")
        # axis hints and node labels under the chart
        ly = cy + ch + 4
        cv.create_text(p(bx + 28 + 4), p(cy + 6), text="Quality", anchor="nw", font=self.f_tiny, fill=hx(th["fg3"]), tags="B")
        qv["labels"] = {}
        cw_px, chh_px = p(bw - 56), p(ch)
        pad = int(26 * self.S)
        for key in PRESET_ORDER:
            kbps = PRESETS[key]["size_kbps"]
            nx, _ny = self.tradeoff.point(kbps, cw_px, chh_px, pad)
            x = p(bx + 28) + nx
            qv["labels"][key] = cv.create_text(x, p(ly + 9), text=PRESETS[key]["label"], font=self.f_chip,
                                               fill=self._label_color(key), tags="B")
            self.region((x - p(34), p(cy), x + p(34), p(ly + 20)), cb=lambda k=key: self.pick_preset(k), group="B",
                        hover=lambda on, k=key: self._qhover(k if on else None))
        cv.create_text(p(bx + 28), p(ly + 9), text="Smaller files", anchor="w", font=self.f_tiny, fill=hx(th["fg3"]),
                       tags="B")
        cv.create_text(p(bx + bw - 28), p(ly + 9), text="Larger files", anchor="e", font=self.f_tiny, fill=hx(th["fg3"]),
                       tags="B")
        # ---- caption
        sy = ly + 34
        est_room = 200
        qv["room"] = p(bw - 56 - est_room)
        qv["tag"] = cv.create_text(p(bx + 28), p(sy), text=gk.fit(CAPTIONS[self.s.preset], self.f_body, qv["room"]),
                                   anchor="w", font=self.f_body, fill=hx(th["fg"]), tags="B")
        qv["est"] = cv.create_text(p(bx + bw - 28), p(sy), text=self._estimate_text(self.qest), anchor="e",
                                   font=self.f_semi, fill=hx(th["accent"]), tags="B")

    def _tile(self, key, x, y, w, h):
        cv, p, th = self.cv, self.p, self.th
        pw, ph = p(w), p(h)
        g = int(round(6 * self.S))
        sel = self.s.preset == key
        img = lambda s_, hov: self.cached(("opt", pw, ph, s_, hov, self.name),
                                          lambda: gk.option_image(pw, ph, s_, hov, th, self.S))
        item = cv.create_image(p(x) - g, p(y) - g, anchor="nw", image=img(sel, False), tags="B")
        pr = preset(key, self.s.device)
        room = pw - p(36)
        self.qv["room_tile"] = room
        label = cv.create_text(p(x + 18), p(y + 14), text=pr["label"], anchor="nw", font=self.f_semi,
                               fill=hx(th["accent"] if sel else th["fg"]), tags="B")
        self.qv["details"][key] = cv.create_text(p(x + 18), p(y + 38), text=gk.fit(pr["detail"], self.f_body, room),
                                                 anchor="nw", font=self.f_body, fill=hx(th["fg2"]), tags="B")
        cv.create_text(p(x + 18), p(y + 60), text=gk.fit(pr["tagline"], self.f_tiny, room), anchor="nw", font=self.f_tiny,
                       fill=hx(th["fg3"]), tags="B")
        self.qv["tiles"][key] = (item, label, img)
        self.region((p(x), p(y), p(x + w), p(y + h)), cb=lambda: self.pick_preset(key), group="B",
                    hover=lambda on: self._qhover(key if on else None))

    def _label_color(self, key):
        th = self.th
        if key == self.s.preset:
            return hx(th["accent"])
        return hx(th["fg"] if key == self.qhover else th["fg2"])

    # ---------------------------------------------------------------- behaviour
    def _chart_photo(self, kbps):
        x, y, w, h = self.qv["chart_box"] if self.qv.get("chart_box") else (0, 0, 1, 1)
        img = charts.tradeoff_chart(w, h, self.tradeoff, kbps, self.s.preset, self.qhover, self.th, self.S)
        return self.photo("qchart", img)

    def _estimate_text(self, kbps):
        """'up to about …': in Optimized mode a song is never written bigger than its source deserves."""
        if self.col:
            return f"up to about {fmt.size(estimate_mb(self.col.seconds, kbps))} for {fmt.plural(len(self.col.tracks), 'song')}"
        return f"up to about {fmt.size(estimate_mb(210, kbps))} per song"

    def _qhover(self, key):
        if key == self.qhover or "tiles" not in self.qv:
            return
        old, self.qhover = self.qhover, key
        for k in (old, key):
            if k in self.qv["tiles"]:
                item, _label, img = self.qv["tiles"][k]
                self.cv.itemconfigure(item, image=img(k == self.s.preset, k == self.qhover))
            if k in self.qv["labels"]:
                self.cv.itemconfigure(self.qv["labels"][k], fill=self._label_color(k))
        if "qpos" not in self.anims:
            self.cv.itemconfigure(self.qv["chart"], image=self._chart_photo(self.qpos))

    def pick_preset(self, key):
        if key == self.s.preset or "tiles" not in self.qv:
            return
        old_key, self.s.preset = self.s.preset, key
        self.s.save()
        for k in (old_key, key):
            item, label, img = self.qv["tiles"][k]
            on = k == key
            self.cv.itemconfigure(item, image=img(on, k == self.qhover))
            self.cv.itemconfigure(label, fill=hx(self.th["accent"] if on else self.th["fg"]))
            self.cv.itemconfigure(self.qv["labels"][k], fill=self._label_color(k))
        self.cv.itemconfigure(self.qv["tag"], text=gk.fit(CAPTIONS[key], self.f_body, self.qv["room"]))
        start, end = self.qpos, float(PRESETS[key]["size_kbps"])
        est_start, est_end = self.qest, self._real_kbps(key)

        def frame(f):
            self.qpos = start + (end - start) * f
            self.qest = est_start + (est_end - est_start) * f
            self.cv.itemconfigure(self.qv["chart"], image=self._chart_photo(self.qpos))
            self.cv.itemconfigure(self.qv["est"], text=self._estimate_text(self.qest))
        self.animate("qpos", 0.55, frame, gk.ease, done=self.refresh_estimate)
        self.refresh_estimate()

    def pick_device(self, i):
        """Apple (AAC / ALAC) or Windows · Android (MP3 / FLAC): the tiles are rewritten in place, nothing moves."""
        key = DEVICE_ORDER[i]
        if key == self.s.device or "tiles" not in self.qv:
            return
        self.s.device = key
        self.s.save()
        for k in PRESET_ORDER:
            self.cv.itemconfigure(self.qv["details"][k],
                                  text=gk.fit(preset(k, key)["detail"], self.f_body, self.qv["room_tile"]))
        self.qest = self._real_kbps(self.s.preset)
        self.cv.itemconfigure(self.qv["est"], text=self._estimate_text(self.qest))
        self.refresh_estimate()
