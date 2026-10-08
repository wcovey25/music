"""
views_activity.py — the list of songs as they finish (and the ones working right now), with a "Needs attention" tab.

Rows are drawn straight from RunState (bounded deques), newest first; only the rows in view become canvas items,
so a 5,000-song run costs the same to display as a 20-song one.
"""
import dataclasses
import logging

from ..core import engine
from ..core.models import Result
from . import glass as gk
from .glass import hx

log = logging.getLogger("musicdl")
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
        self.picks = []                         # (row, option) the user chose, waiting for the worker to be free
        self._pick = None                       # (row, option) being fetched now
        self._flush_timer = None

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
        self.put(cx, cy - p(18), glyph, "center", "rows")
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
            if row.get("picking"):
                label, icon, color = "Getting your pick", None, "accent"
            thumb = row["thumb"]
        key = ("rowtile", id(row), ts, self.name) if thumb else ("rowtile", None, ts, self.name)
        tile = self.cached(key, lambda: gk.tile_image(ts, th, self.S, thumb, radius=p(8)))
        cy = y + rh / 2
        self.put(x + pad, cy, tile, "w", "rows")
        tx = x + pad + ts + p(14)
        right = x + w - pad
        lab_w = self.f_small.measure(label) if label else 0
        icon_w = p(20) if icon else 0
        pill_w = (self.f_tiny.measure("Choose…") + p(26)) if kind == "row" and row.get("close") and not row.get("picking") else 0
        room = right - tx - lab_w - icon_w - pill_w - p(30) - (p(10) if pill_w else 0)
        cv.create_text(tx, cy - p(10), text=gk.fit(title, self.f_semi, room), anchor="w", font=self.f_semi,
                       fill=hx(th["fg"]), tags="rows")
        cv.create_text(tx, cy + p(11), text=gk.fit(sub, self.f_small, room), anchor="w", font=self.f_small,
                       fill=hx(th["fg3"]), tags="rows")
        if icon:
            ic = self.cached(("sic", icon, color, p(16), self.name), lambda: gk.status_icon(icon, p(16), th[color]))
            self.put(right, cy, ic, "e", "rows")
        cv.create_text(right - icon_w, cy, text=label, anchor="e", font=self.f_small,
                       fill=hx(th[color] if kind in ("active",) or row.get("picking") else th["fg2"]), tags="rows")
        if pill_w:
            self._choose_pill(row, right - icon_w - lab_w - p(10) - pill_w, cy, pill_w)

    def _choose_pill(self, row, x, cy, w):
        """'Choose…' beside a song that was not found exactly but has close matches: opens the list of them."""
        cv, p, th = self.cv, self.p, self.th
        h = p(26)
        y = cy - h / 2
        img = lambda hov: self.cached(("chip", w, h, True, hov, self.name), lambda: gk.chip_image(w, h, True, hov, th, self.S))
        item = self.put(x, y, img(False), tags="rows")
        cv.create_text(x + w / 2, cy, text="Choose…", font=self.f_tiny, fill=hx(th["accent"]), tags="rows")
        box = (x, y, x + w, y + h)
        top, bottom = self.act_area[1], self.act_area[3]
        self.region(box, cb=lambda: self.open_choices(row, box), group="rows", sound="nav",
                    hover=lambda on: cv.itemconfigure(item, image=img(on)),
                    enabled=lambda: top <= cy <= bottom)               # not through the strips that hide the list's edges

    # ---------------------------------------------------------------- close matches: choosing one
    def open_choices(self, row, anchor):
        opts = row.get("close") or []
        if not opts or row.get("picking"):
            return

        def line(o):
            bits = [o.get("channel") or "", ", ".join(o.get("why") or [])]
            return " · ".join(b for b in bits if b)

        def clock(sec):
            sec = int(round(sec or 0))
            return f"{sec // 60}:{sec % 60:02d}" if sec else ""
        items = [(i, dict(title=o.get("title") or "Untitled", detail=line(o), tag=clock(o.get("seconds"))))
                 for i, o in enumerate(opts)]
        w = min(self.p(600), self.W - self.p(24))
        anchor = (max(self.p(12), anchor[2] - w), anchor[1], anchor[2], anchor[3])     # right edge under the pill: stays on the card
        self.open_popup(anchor, items, None, lambda i: self._choose(row, opts[i]), width=w, row_h=54,
                        max_rows=len(items))

    def _choose(self, row, opt):
        if self.run is None or row.get("picking") or any(r is row for r, _ in self.picks):
            return
        self.picks.append((row, opt))
        row["picking"] = True
        row["note"] = "Waiting for the others to finish" if self.runner.busy() else row.get("note", "")
        self.activity_dirty = True
        self._flush_picks()

    def _flush_picks(self):
        """Fetch the next chosen recording when the worker is free (they wait out a running download)."""
        r = self.run
        if r is None or self._pick is not None or not self.picks or self.stage != "done":
            return
        if self.runner.busy():                                 # (a worker says it is done a moment before its thread ends)
            if not self._flush_timer:
                self._flush_timer = self.root.after(80, self._flush_later)
            return
        row, opt = self.picks.pop(0)
        self._pick = (row, opt)
        row["note"] = "Starting…"
        tr = row["track"]
        track = dataclasses.replace(tr, url=opt["url"], extra={**tr.extra, "picked": True})
        st = r.settings or self.s
        self.activity_dirty = True

        def work(stop, post):
            engine.Job([track], r.outdir, st, emit=lambda ev: post("ev", ev), stop=stop).run()
        if not self.runner.start("pick", work):
            self._pick = None
            row.pop("picking", None)

    def _flush_later(self):
        self._flush_timer = None
        self._flush_picks()

    def on_pick_event(self, ev):
        """Events of the one-song job that fetches a chosen recording (never counted in the run's own progress)."""
        row, opt = self._pick
        if ev["type"] == "stage":
            row["note"] = ev["text"]
        elif ev["type"] == "result":
            self._pick_done(ev["result"])
        self.activity_dirty = True

    def pick_failed(self, message):
        """The one-song job broke before it could report (a crash, not a missing recording)."""
        if self._pick is None:
            return
        self._pick_done(Result("error", note=message))

    def _pick_done(self, res):
        row, opt = self._pick
        self._pick = None
        r = self.run
        row.pop("picking", None)
        if res.status in ("ok", "upgraded", "fixed", "kept"):
            was = row["status"]
            row.update(status=res.status, note=res.note or "Saved the one you chose", kbps=res.kbps or res.quality_kbps,
                       attention=res.attention, thumb=res.thumb, path=res.path, close=[], service=res.source,
                       title=res.track or row["title"], artist=res.artist or row["artist"])
            res.thumb = None
            if r:
                r.counts[was] = max(0, r.counts.get(was, 0) - 1)
                r.counts[res.status] = r.counts.get(res.status, 0) + 1
                items = [x for x in r.attn if x is not row]
                if res.attention:
                    items.append(row)
                r.attn.clear()
                r.attn.extend(items)
            self.cue("complete")
        else:
            row["close"] = [o for o in row.get("close", []) if o is not opt]
            row["note"] = "That one could not be downloaded" + (" — try another" if row["close"] else "")
        if self.page == "main" and self.stage == "done" and not self.popup_state:
            self.redraw_a()                                    # the summary above the list counts differently now
        self._flush_picks()

    def offer_close_matches(self):
        """At the end of a run: songs that had no exact match but do have close ones are worth one question."""
        r = self.run
        if r is None or r.stopped or self.sheet_state:
            return
        n = sum(1 for row in r.rows if row.get("close"))
        if not n:
            return
        one = n == 1
        lines = [f"{n} song{'' if one else 's'} could not be found exactly, but {'it has' if one else 'they have'} close "
                 f"matches."]
        lines.append("Open the list to choose the version you want. Each option says how it differs from the song — "
                     "for example a live version, or a cut that is a little longer.")
        lines.append("Change this any time in Settings › Close matches.")

        def answer(key):
            if key == "review":
                self.act_tab = 1
                if self.s.advanced:
                    self.tab = "activity"
                if self.page == "main":
                    self.draw_card_b()
                    self.tag_ui()
        self.cue("nav")
        self.open_sheet("Some songs have close matches", lines, [("later", "Later", "glass"), ("review", "Review", "primary")],
                        answer, default="later")

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
