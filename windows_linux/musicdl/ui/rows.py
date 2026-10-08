"""
rows.py — iOS-style grouped rows, driven by plain specs. The Advanced tabs and the Settings page are both just lists
of these, so there is exactly one place that knows how a switch, a segmented control, a stepper or a text field looks.

    Group("Title", [Row("switch", "Embed artwork", get=lambda: ..., set=lambda v: ...), ...])

Row kinds: switch · segment · stepper · text · password · popup · action · info
Common keys: sub (second line, str or callable), show (callable -> bool), enabled (callable -> bool),
             refresh=True (redraw the list after the value changes, for rows that other rows depend on)
"""
from . import glass as gk
from .glass import hx, mix

ROW_H, ROW_SUB_H, TITLE_H, GROUP_GAP, PAD = 50, 62, 28, 22, 20       # logical pixels


class Group:
    def __init__(self, title, rows, note=""):
        self.title, self.rows, self.note = title, rows, note


class Row(dict):
    def __init__(self, kind, label, **kw):
        super().__init__(kind=kind, label=label, **kw)


def wrap(text, font, width, limit=3):
    """Greedy word wrap (at most `limit` lines, the last one ellipsized)."""
    words, lines, cur = str(text).split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if font.measure(trial) <= width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > limit:
        lines = lines[:limit]
        lines[-1] = gk.fit(lines[-1] + " …", font, width)
    return lines


class Rows:
    def __init__(self, app, key="rows"):
        self.app, self.key = app, key
        self.groups = []
        self.area = (0, 0, 0, 0)
        self._plates = {}

    # ------------------------------------------------------------ layout
    def _visible(self, g):
        return [r for r in g.rows if r.get("show", lambda: True)()]

    def _measure(self, width):
        a = self.app
        p = a.p
        out, y = [], 0
        for g in self.groups:
            rows = self._visible(g)
            if not rows:
                continue
            item = dict(group=g, y=y, rows=[], title_y=y)
            if g.title:
                y += p(TITLE_H)
            item["plate_y"] = y
            for r in rows:
                sub = r.get("sub")
                sub = sub() if callable(sub) else sub
                h = p(ROW_SUB_H if sub else ROW_H)
                item["rows"].append((r, y, h, sub))
                y += h
            item["plate_h"] = y - item["plate_y"]
            if g.note:
                lines = wrap(g.note, a.f_tiny, width - p(2 * PAD))
                item["note"] = lines
                y += p(8) + len(lines) * p(16)
            y += p(GROUP_GAP)
            out.append(item)
        return out, y

    def total_height(self, width):
        return self._measure(width)[1]

    # ------------------------------------------------------------ drawing
    def draw(self, area, reset=False):
        """Paint the rows into `area` (canvas px). Keeps the scroll offset unless `reset`."""
        a = self.app
        cv, p, th = a.cv, a.p, a.th
        self.area = area
        x0, y0, x1, y1 = area
        width = x1 - x0
        a.close_entry("row")                                   # (the source field in the top card stays)
        cv.delete(self.key)
        a.clear_regions(self.key)
        layout, total = self._measure(width)
        if reset:
            a.scrollers.pop(self.key, None)
        off = a.scroller(self.key, area, total, lambda: self.draw(self.area), step=p(46))
        top, bot = y0 - p(80), y1 + p(80)
        for item in layout:
            g = item["group"]
            base = y0 - off
            if g.title:
                ty = base + item["title_y"]
                if top < ty < bot:
                    cv.create_text(x0 + p(PAD), ty + p(TITLE_H) - p(9), text=g.title.upper(), anchor="sw",
                                   font=a.f_tiny, fill=hx(th["fg3"]), tags=self.key)
            py = base + item["plate_y"]
            ph = item["plate_h"]
            if py + ph < top or py > bot:
                continue
            v0, v1 = max(py, y0), min(py + ph, y1)                  # draw only the part of the plate inside the area
            if v1 > v0:
                key = (width, ph, a.name)
                if key not in self._plates:
                    if len(self._plates) > 12:
                        self._plates.clear()
                    self._plates[key] = gk.panel_image(width, ph, p(16), th, a.S)
                crop = self._plates[key].crop((0, v0 - py, width, v1 - py))
                cv.create_image(x0, v0, anchor="nw", image=a.photo(("plate", id(g)), crop), tags=self.key)
            for n, (row, ry, rh, sub) in enumerate(item["rows"]):
                yy = base + ry
                if yy + rh < y0 - p(2) or yy > y1 + p(2):
                    continue
                if n:
                    gk_line = hx(mix(a.bg_at(x0 + width / 2, min(max(yy, y0), y1 - 1)), th["fg"], 0.10 if th["dark"] else 0.08))
                    cv.create_line(x0 + p(PAD), yy, x0 + width - p(PAD), yy, fill=gk_line, tags=self.key)
                self._row(row, x0, yy, width, rh, sub, area)
            if item.get("note"):
                ny = base + item["plate_y"] + ph + p(8)
                for i, line in enumerate(item["note"]):
                    cv.create_text(x0 + p(PAD), ny + i * p(16), text=line, anchor="nw", font=a.f_tiny,
                                   fill=hx(th["fg3"]), tags=self.key)
        a.cover(x0 - p(4), y0, width + p(8), y1 - y0, p(ROW_SUB_H + 4), self.key)
        a.draw_scrollbar(self.key, self.key)
        a.cv.tag_raise("keep")
        a.cv.tag_raise("popup")

    # ------------------------------------------------------------ rows
    def _row(self, r, x0, y, w, h, sub, area):
        a = self.app
        cv, p, th = a.cv, a.p, a.th
        kind = r["kind"]
        enabled = r.get("enabled", lambda: True)()
        cy = y + h / 2
        xr = x0 + w - p(PAD)
        fg = th["fg"] if enabled else th["fg3"]
        label_w = w - p(2 * PAD)
        right_w = self._control_width(r, kind)
        label = gk.fit(r["label"], a.f_body, label_w - right_w - p(16))
        if sub:
            cv.create_text(x0 + p(PAD), cy - p(9), text=label, anchor="w", font=a.f_body, fill=hx(fg), tags=self.key)
            cv.create_text(x0 + p(PAD), cy + p(11), text=gk.fit(sub, a.f_small, label_w - right_w - p(16)), anchor="w",
                           font=a.f_small, fill=hx(th["fg3"]), tags=self.key)
        else:
            cv.create_text(x0 + p(PAD), cy, text=label, anchor="w", font=a.f_body, fill=hx(fg), tags=self.key)
        getattr(self, "_" + kind)(r, xr, cy, enabled, area, right_w)

    def _control_width(self, r, kind):
        a, p = self.app, self.app.p
        if kind == "switch":
            return p(51)
        if kind == "segment":
            return self._seg_geometry(r)[0]
        if kind == "stepper":
            return p(30) * 2 + p(70)
        if kind in ("text", "password"):
            return p(r.get("width", 230))
        if kind == "popup":
            return p(r.get("width", 190))
        if kind == "action":
            return max(p(96), a.f_btn.measure(r.get("button", r["label"])) + p(34))
        if kind == "info":
            v = r["get"]() if callable(r.get("get")) else r.get("value", "")
            return min(a.f_body.measure(str(v)) + p(4), p(300))
        return 0

    # ---- switch
    def _switch(self, r, xr, cy, enabled, area, _w):
        a, p, th = self.app, self.app.p, self.app.th
        w, h = p(51), p(31)
        g = int(round(4 * a.S))
        on = bool(r["get"]())
        img = lambda t: a.cached(("sw", w, h, round(t * 5), enabled, a.name),
                                 lambda: gk.switch_image(w, h, round(t * 5) / 5, th, a.S, enabled))
        item = a.cv.create_image(xr - w - g, cy - h / 2 - g, anchor="nw", image=img(1.0 if on else 0.0), tags=self.key)

        def toggle():
            new = not bool(r["get"]())
            r["set"](new)
            a.animate(("sw", id(r)), 0.20, lambda f: a.cv.itemconfigure(
                item, image=img(f if new else 1 - f)), done=(self.refresh if r.get("refresh") else None))
        self._hit((xr - w, cy - h / 2, xr, cy + h / 2), toggle if enabled else None, area)

    # ---- segmented control
    def _seg_geometry(self, r):
        a, p = self.app, self.app.p
        choices = r["choices"]() if callable(r["choices"]) else r["choices"]
        seg_w = max(a.f_chip.measure(str(lab)) for _v, lab in choices) + p(26)
        seg_w = max(seg_w, p(r.get("min", 54)))
        return seg_w * len(choices), seg_w, choices

    def _segment(self, r, xr, cy, enabled, area, _w):
        a, p, th = self.app, self.app.p, self.app.th
        total, seg_w, choices = self._seg_geometry(r)
        h = p(30)
        g = int(round(5 * a.S))
        x0 = xr - total
        cur = r["get"]()
        idx = next((i for i, (v, _l) in enumerate(choices) if v == cur), 0)
        img = lambda t: a.cached(("seg", total, h, len(choices), round(t * 8), enabled, a.name),
                                 lambda: gk.segmented_image(total, h, len(choices), round(t * 8) / 8, th, a.S, enabled))
        item = a.cv.create_image(x0 - g, cy - h / 2 - g, anchor="nw", image=img(idx), tags=self.key)
        labels = []
        for i, (v, lab) in enumerate(choices):
            labels.append(a.cv.create_text(x0 + seg_w * i + seg_w / 2, cy, text=lab, font=a.f_chip,
                                           fill=hx(th["fg"] if i == idx and enabled else th["fg2"] if enabled else th["fg3"]),
                                           tags=self.key))

            def pick(i=i, v=v):
                old = next((k for k, (vv, _l) in enumerate(choices) if vv == r["get"]()), 0)
                if v == r["get"]():
                    return
                r["set"](v)
                for k, it in enumerate(labels):
                    a.cv.itemconfigure(it, fill=hx(th["fg"] if k == i else th["fg2"]))
                a.animate(("seg", id(r)), 0.22, lambda f: a.cv.itemconfigure(item, image=img(old + (i - old) * f)),
                          done=(self.refresh if r.get("refresh") else None))
            self._hit((x0 + seg_w * i, cy - h / 2, x0 + seg_w * (i + 1), cy + h / 2), pick if enabled else None, area,
                      sound="nav")

    # ---- stepper
    def _stepper(self, r, xr, cy, enabled, area, _w):
        a, p, th = self.app, self.app.p, self.app.th
        bw, bh, vw = p(30), p(28), p(70)
        val = r["get"]()
        lo, hi, step = r.get("lo", 0), r.get("hi", 100), r.get("step", 1)
        fmt = r.get("fmt", str)
        a.cv.create_text(xr - bw - vw / 2, cy, text=fmt(val), font=a.f_semi, fill=hx(th["fg"] if enabled else th["fg3"]),
                         tags=self.key)
        n = id(r) % 100000

        def bump(d):
            def go():
                nv = max(lo, min(hi, r["get"]() + d * step))
                if nv != r["get"]():
                    r["set"](nv)
                    self.refresh()
            return go
        y0 = cy - bh / 2
        for key, x, label, d, ok in (("m", xr - 2 * bw - vw, "−", -1, val > lo), ("p", xr - bw, "+", 1, val < hi)):
            b = a.add_button(f"row{n}{key}", x, y0, bw, bh, label, "glass", bump(d), group=self.key, px=True,
                             font=a.f_semi, clip=(area[1], area[3]))
            if not (enabled and ok):
                a.set_button(f"row{n}{key}", enabled=False)

    # ---- text / password
    def _text(self, r, xr, cy, enabled, area, w):
        a, p, th = self.app, self.app.p, self.app.th
        raw = r["get"]()
        secret = r["kind"] == "password"
        shown = ("•" * 10 if raw else "") if secret else raw
        placeholder = r.get("placeholder", "Not set")
        txt = shown or placeholder
        a.cv.create_text(xr, cy, text=gk.fit(txt, a.f_body, w), anchor="e", font=a.f_body,
                         fill=hx(th["fg2"] if shown else th["fg3"]), tags=self.key)
        box = (xr - w, cy - p(15), xr, cy + p(15))

        def edit():
            a.close_popup()
            cap = a.cached(("fld", w, box[3] - box[1], a.name), lambda: gk.field_image(w, box[3] - box[1], True, th, a.S))
            g = int(round(8 * a.S))
            a.cv.create_image(box[0] - g, box[1] - g, anchor="nw", image=cap, tags=(self.key, "entry"))
            state = {"done": False}

            def commit(value):
                if state["done"]:
                    return
                state["done"] = True
                if not (secret and not value.strip()):          # an empty key field means "leave it alone"
                    r["set"](value.strip())
                a.cv.focus_set()
                self.refresh()
            holder = []
            rec = a.entry("row", box, text="" if secret else raw, placeholder=placeholder, show="•" if secret else None,
                          font=a.f_body, on_commit=commit,
                          on_focus=lambda on: None if on else commit(holder[0]["var"].get()), group="entry", pad=12)
            holder.append(rec)
            rec["widget"].focus_set()
            rec["widget"].select_range(0, "end")
        self._hit((xr - w, cy - p(18), xr, cy + p(18)), edit if enabled else None, area)

    _password = _text

    # ---- popup
    def _popup(self, r, xr, cy, enabled, area, w):
        a, p, th = self.app, self.app.p, self.app.th
        choices = r["choices"]() if callable(r["choices"]) else r["choices"]
        cur = r["get"]()
        label = next((lab for v, lab in choices if v == cur), cur or r.get("placeholder", "Choose…"))
        chev = a.cached(("chev", p(14), a.name), lambda: gk.chevron_icon(p(14), th["fg3"], "down"))
        a.cv.create_image(xr - p(7), cy, image=chev, tags=self.key)
        a.cv.create_text(xr - p(20), cy, text=gk.fit(str(label), a.f_body, w - p(24)), anchor="e", font=a.f_body,
                         fill=hx(th["accent"] if enabled else th["fg3"]), tags=self.key)
        box = (xr - w, cy - p(16), xr, cy + p(16))

        def open_():
            if choices:
                a.open_popup(box, choices, cur, r["set"] if not r.get("refresh") else
                             (lambda v: (r["set"](v), self.refresh())), width=max(w, p(220)))
        self._hit(box, open_ if enabled and choices else None, area, sound="nav")

    # ---- action button
    def _action(self, r, xr, cy, enabled, area, w):
        a, p = self.app, self.app.p
        n = id(r) % 100000
        h = p(30)
        a.add_button(f"act{n}", xr - w, cy - h / 2, w, h, r.get("button", r["label"]), "glass", r["cb"], group=self.key,
                     px=True, font=a.f_chip, color=r.get("color"), clip=(area[1], area[3]))
        if not enabled:
            a.set_button(f"act{n}", enabled=False)

    # ---- info
    def _info(self, r, xr, cy, enabled, area, w):
        a, th = self.app, self.app.th
        v = r["get"]() if callable(r.get("get")) else r.get("value", "")
        a.cv.create_text(xr, cy, text=gk.fit(str(v), a.f_body, w + self.app.p(4)), anchor="e", font=a.f_body,
                         fill=hx(r.get("color") and th[r["color"]] or th["fg2"]), tags=self.key)

    # ------------------------------------------------------------ plumbing
    def _hit(self, box, cb, area, sound="click"):
        x0, y0, x1, y1 = box
        y0, y1 = max(y0, area[1]), min(y1, area[3])
        if cb and y1 > y0:
            self.app.region((x0, y0, x1, y1), cb=cb, group=self.key, sound=sound)

    def refresh(self):
        self.draw(self.area)
