"""
views_activity.py — the list of songs as they finish (and the ones working right now), with a "Needs attention" tab.

Rows are drawn straight from RunState (bounded deques), newest first; only the rows in view become canvas items,
so a 5,000-song run costs the same to display as a 20-song one.
"""
from . import glass as gk
from .glass import hx

ROW_H = 58
STATUS = {                                  # status -> (label, icon, colour key)
    "ok": ("Saved", "check", "ok"), "upgraded": ("Upgraded", "check", "ok"), "fixed": ("Fixed", "check", "ok"),
    "kept": ("Kept", "check", "ok"), "skipped": ("Already there", "skip", "fg3"),
    "no-file": ("Not found", "error", "bad"), "bad-length": ("Wrong length", "warn", "warn"),
    "dl-fail": ("Download failed", "error", "bad"), "error": ("Error", "error", "bad"),
}


class ActivityMixin:
    def _init_activity(self):
        self.act_tab = 0
        self.activity_dirty = False
        self._act_last = 0.0
        self._act_count = 0
        self.act_seg = None
        self.act_area = (0, 0, 0, 0)

    def reset_activity(self):
        self.act_tab = 0
        self._act_count = 0
        self.scrollers.pop("act", None)
        self.activity_dirty = True

    # ---------------------------------------------------------------- the switch (All | Needs attention)
    def _act_labels(self):
        n = self.run.attention if self.run else 0
        return ["All", f"Attention ({n})" if n else "Attention"]

    def draw_act_switch(self, y):
        bx, by, bw, bh = self.rB
        p = self.p
        w, h = 220, 34
        x = bx + bw - 28 - w
        self.act_seg = self.segmented("actseg", (p(x), p(y), p(x + w), p(y + h)), self._act_labels(), self.act_tab,
                                      self._act_pick, "Bsw")

    def _act_pick(self, i):
        self.act_tab = i
        self.draw_act_rows(reset=True)

    def draw_activity_view(self, title=True):
        cv, p, th = self.cv, self.p, self.th
        bx, by, bw, bh = self.rB
        if title:
            cv.create_text(p(bx + 28), p(by + 24), text="Activity", anchor="nw", font=self.f_h, fill=hx(th["fg"]),
                           tags="B")
            self.draw_act_switch(by + 18)
        self.act_area = (p(bx + 16), p(by + 66), p(bx + bw - 16), p(by + bh - 14))
        cv.addtag_withtag("keep", "B")
        cv.addtag_withtag("keep", "Bsw")
        self.draw_act_rows(reset=True)

    # ---------------------------------------------------------------- rows
    def _act_entries(self):
        r = self.run
        if r is None:
            return []
        if self.act_tab == 1:
            return [("row", row) for row in reversed(r.attn)]
        out = [("active", a) for a in r.active.values()]
        out += [("row", row) for row in reversed(r.rows)]
        return out

    def draw_act_rows(self, reset=False):
        cv, p, th = self.cv, self.p, self.th
        x0, y0, x1, y1 = self.act_area
        cv.delete("rows")
        self.clear_regions("rows")
        entries = self._act_entries()
        rh = p(ROW_H)
        total = len(entries) * rh
        if reset:
            self.scrollers.pop("act", None)
            self._act_count = len(entries)
        sc = self.scrollers.get("act")
        if sc and sc["off"] > 0 and len(entries) > self._act_count:        # keep what you are reading where it is
            sc["off"] += (len(entries) - self._act_count) * rh
        self._act_count = len(entries)
        off = self.scroller("act", self.act_area, total, self.draw_act_rows, step=rh)
        if not entries:
            self._act_empty(x0, y0, x1, y1)
            self.cv.tag_raise("keep")
            return
        first = off // rh                                   # only rows that start within one row of the area (the cover
        last = min(len(entries), (off + (y1 - y0)) // rh + 1)   # strips hide exactly one row's height at each end)
        for i in range(first, last):
            self._act_row(entries[i], x0, y0 + i * rh - off, x1 - x0, rh)
        self.cover(x0 - p(4), y0, (x1 - x0) + p(8), y1 - y0, rh, "rows")
        self.draw_scrollbar("act", "rows")
        self.cv.tag_raise("keep")
        self.cv.tag_raise("popup")

    def _act_empty(self, x0, y0, x1, y1):
        cv, p, th = self.cv, self.p, self.th
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        glyph = self.cached(("noteg", p(40), self.name), lambda: gk.note_glyph(p(40), th["fg3"]))
        cv.create_image(cx, cy - p(18), image=glyph, tags="rows")
        if self.act_tab == 1:
            text = "Nothing needs attention."
        elif self.run is None:
            text = "Songs appear here as they download."
        else:
            text = "Songs appear here as they finish."
        cv.create_text(cx, cy + p(24), text=text, font=self.f_body, fill=hx(th["fg3"]), tags="rows")

    def _act_row(self, entry, x, y, w, rh):
        cv, p, th = self.cv, self.p, self.th
        kind, row = entry
        pad = p(12)
        ts = p(40)
        if kind == "active":
            tr = row["track"]
            title, sub, label, icon, color = tr.title, row["stage"], "Working", None, "accent"
            thumb = None
        else:
            title = row["title"]
            sub = row["artist"] + (f" · {row['note']}" if row["note"] else "")
            label, icon, color = STATUS.get(row["status"], ("", "check", "ok"))
            if row["status"] in ("ok", "upgraded", "fixed", "kept") and row["kbps"]:
                label = "Lossless" if row["kbps"] >= 1000 else f"{row['kbps']} kbps"
            if row["attention"] and row["status"] in ("ok", "upgraded", "fixed", "kept", "skipped"):
                icon, color = "warn", "warn"                                   # saved, but flagged (low quality, no art)
            thumb = row["thumb"]
        key = ("rowtile", id(row), ts, self.name) if thumb else ("rowtile", None, ts, self.name)
        tile = self.cached(key, lambda: gk.tile_image(ts, th, self.S, thumb, radius=p(8)))
        cy = y + rh / 2
        cv.create_image(x + pad, cy, anchor="w", image=tile, tags="rows")
        tx = x + pad + ts + p(14)
        right = x + w - pad
        lab_w = self.f_small.measure(label) if label else 0
        icon_w = p(20) if icon else 0
        room = right - tx - lab_w - icon_w - p(30)
        cv.create_text(tx, cy - p(10), text=gk.fit(title, self.f_semi, room), anchor="w", font=self.f_semi,
                       fill=hx(th["fg"]), tags="rows")
        cv.create_text(tx, cy + p(11), text=gk.fit(sub, self.f_small, room), anchor="w", font=self.f_small,
                       fill=hx(th["fg3"]), tags="rows")
        if icon:
            ic = self.cached(("sic", icon, color, p(16), self.name), lambda: gk.status_icon(icon, p(16), th[color]))
            cv.create_image(right, cy, anchor="e", image=ic, tags="rows")
        cv.create_text(right - icon_w, cy, text=label, anchor="e", font=self.f_small,
                       fill=hx(th[color] if kind == "active" else th["fg2"]), tags="rows")

    # ---------------------------------------------------------------- live refresh
    def tick_activity(self, now, dt):
        if not self.activity_dirty or now - self._act_last < 0.25 or self.popup_state:
            return
        self._act_last = now
        self.activity_dirty = False
        if self.act_seg:
            labels = self._act_labels()
            try:
                self.cv.itemconfigure(self.act_seg["texts"][1], text=labels[1])
            except Exception:
                pass
        self.draw_act_rows()
