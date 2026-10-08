"""
views_source.py — the top card. One card, four states:

    input     a field for a link / name / list, service chips, one button
    ready     what the link turned into (cover, title, count, size estimate) and a Download button
    running   big percentage, current song, progress bar, ETA, Stop
    done      summary and next steps
"""
import logging
import re
import time
import tkinter as tk
from tkinter import filedialog

from .. import ai, ingest
from ..core import disk, netio
from ..meta import artwork
from ..telemetry.stats import T
from . import fmt
from . import glass as gk
from .glass import hx
from .pulse import Pulse

log = logging.getLogger("musicdl")

CHIPS = (("spotify", "Spotify"), ("apple", "Apple Music"), ("ytmusic", "YouTube Music"), ("amazon", "Amazon Music"),
         ("youtube", "YouTube"), ("pandora", "Pandora"), ("sheet", "Spreadsheet"))
HINT = "Paste a playlist, album or song link — or type a name."
AI_HINT = "Describe the playlist you’d like, for example “mellow 90s rock for a long drive, 30 songs”."


def fetch_art(url, stop):
    """Cover for the loaded list (small JPEG bytes), or None. Never raises."""
    if not url:
        return None
    try:
        r = netio.request("GET", url, stop=stop, retries=1, timeout=(6, 15))
        if r is None or r.status_code != 200:
            return None
        try:
            return artwork.thumb_of_bytes(r.content, 256)
        finally:
            r.close()
    except Exception:
        return None


class SourceMixin:
    def _init_source(self, initial):
        self.source_text = initial or ""
        self.multi = ""
        self.multi_label = ""
        self.ai_prompt = False
        self.status, self.status_tone = "", "fg3"
        self.service_key = None
        self.resolve_cancelled = False
        self.a_items = {}                       # handles used by the live updates
        self.ra = {}
        self.shown_fraction = 0.0
        self._ra_last = {}
        self.pulse, self._pulse_key, self._bursts_seen = None, None, 0

    # ---------------------------------------------------------------- entry bookkeeping
    def close_entries(self):
        self.src_remember()
        super().close_entries()

    def src_remember(self):
        rec = self.entries.get("src")
        if rec:
            try:
                v = rec["var"].get()
            except tk.TclError:
                return
            if self.multi and v == self.multi_label:
                return
            self.source_text = v

    def src_text_restore(self):
        pass

    def redraw_a(self):
        self.src_remember()
        self.cv.delete("A")
        self.clear_regions("A")
        self.close_entry("src")
        self.draw_card_a()
        self.tag_ui()

    # ---------------------------------------------------------------- drawing
    def draw_card_a(self):
        self.cv.delete("A")
        self.clear_regions("A")
        self.a_items, self.ra, self._ra_last = {}, {}, {}
        {"idle": self._a_input, "resolving": self._a_input, "ready": self._a_ready, "running": self._a_running,
         "stopping": self._a_running, "done": self._a_done}[self.stage]()

    def _ax(self):
        x, y, w, h = self.rA
        return x, y, w, h

    # ---- input
    def _a_input(self):
        cv, p, th = self.cv, self.p, self.th
        ax, ay, aw, ah = self._ax()
        resolving = self.stage == "resolving"
        btn_w = 136
        fx, fy, fw, fh = ax + 28, ay + 28, aw - 56 - btn_w - 14, 52
        box = (p(fx), p(fy), p(fx + fw), p(fy + fh))
        g = int(round(8 * self.S))
        w, h = box[2] - box[0], box[3] - box[1]
        imgs = {f: self.cached(("field", w, h, f, self.name), lambda f=f: gk.field_image(w, h, f, th, self.S))
                for f in (False, True)}
        item = cv.create_image(box[0] - g, box[1] - g, anchor="nw", image=imgs[False], tags="A")
        placeholder = AI_HINT if self.ai_prompt else HINT
        placeholder = gk.fit(placeholder, self.f_body, w - p(48))
        text = self.multi_label if self.multi else self.source_text
        rec = self.entry("src", box, text=text, placeholder=placeholder, font=self.f_body, on_change=self._src_changed,
                         on_commit=lambda v: self.submit_source(), on_focus=lambda on: cv.itemconfigure(
                             item, image=imgs[on]), group="A", pad=22)
        e = rec["widget"]
        e.bind("<<Paste>>", self._on_paste)
        if resolving:
            e.config(state="disabled")
        elif not self.source_text and not self.multi:
            e.focus_set()
        self.a_items["field"] = item
        # button
        bx = ax + aw - 28 - btn_w
        if resolving:
            self.add_button("find", bx, fy + 4, btn_w, 44, "Cancel", "glass", self.cancel_resolve, group="A")
        else:
            self.add_button("find", bx, fy + 4, btn_w, 44, "Create" if self.ai_prompt else "Find songs", "primary",
                            self.submit_source, group="A")
        self._a_chips(ax + 28, ay + 104, aw - 56)
        # status line
        self.a_items["status"] = cv.create_text(p(ax + 30), p(ay + 150), text=gk.fit(self.status, self.f_small, p(aw - 60)),
                                                anchor="w", font=self.f_small, fill=self.tone(self.status_tone), tags="A")

    def _a_chips(self, x, y, avail):
        cv, p, th = self.cv, self.p, self.th
        chips = list(CHIPS)
        ai_on = self.s.ai_enabled
        pad, gap, h = 18, 6, 26
        spark_w = 18
        widths = {k: self.f_tiny.measure(lab) / self.S + pad for k, lab in chips}
        ai_w = self.f_tiny.measure("AI playlist") / self.S + pad + spark_w
        def total():
            return sum(widths[k] for k, _ in chips) + gap * max(0, len(chips) - 1) + ((ai_w + gap) if ai_on else 0)
        for drop in ("sheet", "pandora", "youtube"):
            if total() > avail:
                chips = [c for c in chips if c[0] != drop]
        cx = x
        self.a_items["chips"] = {}
        for key, lab in chips:
            w = widths[key]
            on = self.service_key == key
            self._chip(key, lab, cx, y, w, h, on, cb=(self.pick_sheet if key == "sheet" else None))
            cx += w + gap
        if ai_on:
            self._chip("ai", "AI playlist", cx, y, ai_w, h, self.ai_prompt, cb=self.toggle_ai_prompt, spark=True)

    def _chip(self, key, label, x, y, w, h, on, cb=None, spark=False):
        cv, p, th = self.cv, self.p, self.th
        pw, ph = p(w), p(h)
        img = lambda on_, hov: self.cached(("chip", pw, ph, on_, hov, self.name),
                                           lambda: gk.chip_image(pw, ph, on_, hov, th, self.S))
        item = cv.create_image(p(x), p(y), anchor="nw", image=img(on, False), tags="A")
        col = hx(th["accent"] if on else th["fg2"])
        tx = p(x + w / 2)
        if spark:
            ic = self.cached(("spark", p(12), on, self.name),
                             lambda: gk.sparkle_icon(p(12), th["accent"] if on else th["fg2"]))
            cv.create_image(p(x + 12), p(y + h / 2), image=ic, tags="A")
            tx = p(x + w / 2 + 6)
        cv.create_text(tx, p(y + h / 2), text=label, font=self.f_tiny, fill=col, tags="A")
        self.a_items["chips"][key] = (item, pw, ph)
        if cb:
            self.region((p(x), p(y), p(x + w), p(y + h)), cb=cb, group="A",
                        hover=lambda h_, item=item, on=on: cv.itemconfigure(item, image=img(on, h_)),
                        sound="nav" if key == "ai" else "click")

    def _src_changed(self, value):
        if self.multi and value != self.multi_label:
            self.multi = ""
        text = self.multi or value
        key = ingest.classify(text) if text.strip() else None
        if key == "web":
            key = None
        if key != self.service_key and not self.ai_prompt:
            self.service_key = key
            for k, (item, pw, ph) in self.a_items.get("chips", {}).items():
                if k == "ai":
                    continue
                on = k == key
                self.cv.itemconfigure(item, image=self.cached(
                    ("chip", pw, ph, on, False, self.name), lambda: gk.chip_image(pw, ph, on, False, self.th, self.S)))
        if self.status and self.stage == "idle":
            self.set_status("")

    def _on_paste(self, event):
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        lines = [l.strip() for l in text.replace("\r", "").split("\n") if l.strip()]
        e = event.widget
        if len(lines) > 1:
            self.multi = "\n".join(lines)
            self.multi_label = f"{len(lines)} lines pasted"
            e.delete(0, "end")
            e.insert(0, self.multi_label)
            e.selection_range(0, "end")
        elif lines:
            try:
                e.delete("sel.first", "sel.last")
            except tk.TclError:
                pass
            e.insert("insert", lines[0])
        return "break"

    # ---- ready
    def _a_ready(self):
        cv, p, th = self.cv, self.p, self.th
        ax, ay, aw, ah = self._ax()
        col = self.col
        ts = 96
        tile = self.cached(("tile", id(col), bool(self.col_art), p(ts), self.name),
                           lambda: gk.tile_image(p(ts), th, self.S, self.col_art))
        self.a_items["tile"] = cv.create_image(p(ax + 28), p(ay + 24), anchor="nw", image=tile, tags="A")
        tx = ax + 28 + ts + 20
        btn_w = 150
        room = p(aw - (tx - ax) - btn_w - 28 - 24)
        cv.create_text(p(tx), p(ay + 30), text=gk.fit(col.title, self.f_h, room), anchor="nw", font=self.f_h,
                       fill=hx(th["fg"]), tags="A")
        bits = [b for b in (col.subtitle, ingest.label(col.service), col.kind.capitalize()
                            if col.kind not in ("playlist", "search") else "") if b]
        cv.create_text(p(tx), p(ay + 58), text=gk.fit(" · ".join(dict.fromkeys(bits)), self.f_body, room), anchor="nw",
                       font=self.f_body, fill=hx(th["fg2"]), tags="A")
        self.a_items["stats"] = cv.create_text(p(tx), p(ay + 84), text=self._stats_text(), anchor="nw", font=self.f_body,
                                               fill=hx(th["fg"]), tags="A")
        if col.notes:
            ic = self.cached(("warnic", p(14), self.name), lambda: gk.status_icon("warn", p(14), th["warn"]))
            cv.create_image(p(tx), p(ay + 128), anchor="w", image=ic, tags="A")
            cv.create_text(p(tx + 22), p(ay + 128), text=gk.fit(col.notes[0], self.f_small, room - p(22)), anchor="w",
                           font=self.f_small, fill=hx(th["warn"]), tags="A")
        bx = ax + aw - 28 - btn_w
        self.add_button("download", bx, ay + 30, btn_w, 44, "Download", "primary", self.start_job, group="A")
        self.add_button("change", bx, ay + 86, btn_w, 34, "Change", "glass", self.clear_collection, group="A",
                        font=self.f_chip)

    def _stats_text(self):
        col = self.col
        n = len(col.tracks)
        parts = [fmt.plural(n, "song"), fmt.duration(col.seconds)]
        mb = self.estimate_mb()
        if mb:
            parts.append(f"{'about' if self.s.advanced else 'up to about'} {fmt.size(mb)}")
        return " · ".join(parts)

    def refresh_estimate(self):
        item = self.a_items.get("stats")
        if item and self.col and self.stage == "ready":
            self.cv.itemconfigure(item, text=self._stats_text())

    def clear_collection(self):
        self.col, self.col_art = None, None
        self.set_status("")
        self.set_stage("idle")

    # ---- running
    def _a_running(self):
        cv, p, th = self.cv, self.p, self.th
        ax, ay, aw, ah = self._ax()
        stopping = self.stage == "stopping"
        r = self.run
        ra = self.ra
        ra["pct"] = cv.create_text(p(ax + 28), p(ay + 22), text="0%", anchor="nw", font=self.f_big,
                                   fill=hx(th["fg"]), tags="A")
        sx = ax + 28 + 140
        stop_w = 112
        room = p(aw - 140 - 28 - stop_w - 28 - 20)
        ra["room"] = room
        ra["song"] = cv.create_text(p(sx), p(ay + 34), text="", anchor="nw", font=self.f_semi, fill=hx(th["fg"]), tags="A")
        ra["stage"] = cv.create_text(p(sx), p(ay + 60), text="", anchor="nw", font=self.f_small, fill=hx(th["fg2"]),
                                     tags="A")
        bx = ax + aw - 28 - stop_w
        self.add_button("stop", bx, ay + 30, stop_w, 40, "Stopping…" if stopping else "Stop", "glass", self.stop_job,
                        group="A", color=th["stop"], font=self.f_semi)
        if stopping:
            self.set_button("stop", enabled=False)
        ra["vx"], ra["vy"], ra["vw"], ra["vh"] = p(ax + 28), p(ay + 82), p(aw - 56), p(54)
        ra["viz"] = cv.create_image(ra["vx"], ra["vy"], anchor="nw", image="", tags="A")
        ra["count"] = cv.create_text(p(ax + 30), p(ay + 148), text="", anchor="w", font=self.f_small, fill=hx(th["fg2"]),
                                     tags="A")
        ra["left"] = cv.create_text(p(ax + aw - 30), p(ay + 148), text="", anchor="e", font=self.f_small,
                                    fill=hx(th["fg2"]), tags="A")
        self._ra_last = {}
        self.update_run_card(0.0)

    def tick_card_a(self, now, dt):
        if self.stage in ("running", "stopping") and self.ra:
            self.update_run_card(dt, now)

    def _set(self, key, text):
        """itemconfigure only when the text changed (this runs every frame)."""
        if self._ra_last.get(key) != text:
            self._ra_last[key] = text
            self.cv.itemconfigure(self.ra[key], text=text)

    def update_run_card(self, dt, now=None):
        r, ra, p = self.run, self.ra, self.p
        if not r or "viz" not in ra:
            return
        now = now or time.monotonic()
        target = r.fraction
        self.shown_fraction += (target - self.shown_fraction) * min(1.0, max(dt, 0.001) * 6)
        if abs(target - self.shown_fraction) < 0.0015:
            self.shown_fraction = target
        pct = int(self.shown_fraction * 100 + 0.5) if r.planned else 0
        self._set("pct", f"{pct}%" if r.planned else "")
        # current song
        stopping = self.stage == "stopping"
        paused = bool(r.paused) and not stopping
        if stopping:
            song, stage = "Finishing up…", "Stopping after the songs in progress"
        elif paused:
            song = "Disk full — paused"
            stage = f"Free up some space and it carries on by itself · {self._free_text()}"
        elif r.active:
            first = next(iter(r.active.values()))
            song = gk.fit(first["track"].label(), self.f_semi, ra["room"])
            more = len(r.active) - 1
            stage = first["stage"] + (f"  ·  +{more} more" if more else "") + (f"  ·  {r.pace}" if r.pace else "")
        else:
            song, stage = r.phase, ""
        self._set("song", song)
        self._set("stage", gk.fit(stage, self.f_small, ra["room"]))
        tone = "warn" if paused else "fg"
        if self._ra_last.get("tone") != tone:                     # amber while paused
            self._ra_last["tone"] = tone
            self.cv.itemconfigure(ra["song"], fill=hx(self.th[tone]))
            self.cv.itemconfigure(ra["pct"], fill=hx(self.th[tone]))
        self._draw_pulse(r, dt, "paused" if paused else ("stopping" if stopping else "run"))
        self._set("count", f"{r.done:,} of {r.total:,} songs" if r.planned else "")
        if not r.planned:
            left = ""
        elif r.todo == 0:
            left = "Everything is already up to date"
        else:
            left = fmt.left(T.eta(max(0, r.todo - r.worked)))
        self._set("left", left)

    def _free_text(self):
        """'1.2 GB free' for the music folder's drive, looked up at most once a second."""
        now = time.monotonic()
        last = getattr(self, "_free_at", (0.0, None))
        if now - last[0] > 1.0:
            last = self._free_at = (now, disk.free_bytes(self.run.outdir))
        return f"{fmt.size(last[1] / 1048576)} free" if last[1] is not None else "no room left"

    def _draw_pulse(self, r, dt, mode):
        """One frame of the waveform progress. How lively it is follows the real download speed."""
        ra = self.ra
        key = (ra["vw"], ra["vh"], self.name)
        if getattr(self, "pulse", None) is None or self._pulse_key != key:
            self.pulse, self._pulse_key = Pulse(ra["vw"], ra["vh"], self.th, self.S, seed=r.seed), key
            self._bursts_seen = r.bursts
        frac = min(1.0, max(self.shown_fraction, 0.0)) if (r.planned or r.done) else None
        if r.bursts != self._bursts_seen:                          # a song just finished
            self._bursts_seen = r.bursts
            if frac is not None:
                self.pulse.burst(frac)
        speed, peak = T.speed_now(), max(T.peak, 256 * 1024.0)
        energy = 0.22 + 0.62 * min(1.0, (speed / peak) ** 0.6) + (0.10 if r.active else 0.0)
        img = self.pulse.frame(dt, frac, min(1.0, energy), mode)
        self.cv.itemconfigure(ra["viz"], image=self.photo("viz", img))

    # ---- done
    def _a_done(self):
        cv, p, th = self.cv, self.p, self.th
        ax, ay, aw, ah = self._ax()
        r = self.run
        counts = r.counts if r else {}
        saved = counts.get("ok", 0) + counts.get("upgraded", 0) + counts.get("fixed", 0)
        there = counts.get("skipped", 0) + counts.get("kept", 0)
        bad = r.attention if r else 0
        stopped = bool(r and r.stopped)
        if stopped:
            title, kind = "Stopped", "warn"
        elif bad and not saved and not there:
            title, kind = "Nothing could be saved", "error"
        elif bad:
            title, kind = "Done — a few need a look", "warn"
        else:
            title, kind = "All done", "check"
        color = {"check": th["ok"], "warn": th["warn"], "error": th["bad"]}[kind]
        size = 52
        ic = self.cached(("done", kind, p(size), self.name), lambda: gk.status_icon(kind, p(size), color))
        cv.create_image(p(ax + 28), p(ay + 28), anchor="nw", image=ic, tags="A")
        tx = ax + 28 + size + 20
        btn_w = 150
        room = p(aw - (tx - ax) - btn_w - 28 - 24)
        cv.create_text(p(tx), p(ay + 28), text=title, anchor="nw", font=self.f_h, fill=hx(th["fg"]), tags="A")
        parts = []
        if saved:
            parts.append(f"{fmt.plural(saved, 'song')} saved")
        if there:
            parts.append(f"{there:,} already in your library")
        if bad:
            parts.append(f"{bad:,} need attention")
        if not parts:
            parts.append("No songs were saved")
        cv.create_text(p(tx), p(ay + 58), text=gk.fit(" · ".join(parts), self.f_body, room), anchor="nw",
                       font=self.f_body, fill=hx(th["fg2"]), tags="A")
        elapsed = (time.monotonic() - r.t0) if r else 0
        extra = r.note if (r and r.note) else (f"Took {fmt.duration(elapsed)}" if elapsed >= 20 else "")
        if extra:
            cv.create_text(p(tx), p(ay + 82), text=gk.fit(extra, self.f_small, room), anchor="nw", font=self.f_small,
                           fill=hx(th["fg3"]), tags="A")
        bx = ax + aw - 28 - btn_w
        self.add_button("open", bx, ay + 30, btn_w, 44, "Open folder", "primary", self.open_folder, group="A")
        self.add_button("again", bx, ay + 86, btn_w, 34, "Start over", "glass", self.start_over, group="A",
                        font=self.f_chip)
        if stopped or bad:
            self.add_button("retry", ax + 28, ay + 118, 130, 32, "Try again", "glass", self.retry_job, group="A",
                            font=self.f_chip)

    def start_over(self):
        self.col, self.col_art, self.run = None, None, None
        self.set_status("")
        self.set_stage("idle")

    def retry_job(self):
        self.stage = "ready"
        self.start_job()

    # ---------------------------------------------------------------- resolving a link
    def set_status(self, text, tone="fg3"):
        self.status, self.status_tone = text, tone
        item = self.a_items.get("status") if self.page == "main" else None
        if item and self.stage in ("idle", "resolving"):
            self.cv.itemconfigure(item, text=gk.fit(text, self.f_small, self.p(self.rA[2] - 60)), fill=self.tone(tone))

    def submit_source(self):
        if self.stage not in ("idle",):
            return
        self.src_remember()
        text = (self.multi or self.source_text).strip()
        if not text:
            self.set_status("Paste a link or type a song or album name first.", "fg2")
            return
        self.source_text = text if not self.multi else self.source_text
        if self.ai_prompt:
            self.start_generate(text)
        else:
            self.start_resolve(text)

    def pick_sheet(self):
        if self.stage != "idle":
            return
        path = filedialog.askopenfilename(parent=self.root, title="Choose a song list",
                                          filetypes=[("Song lists", "*.csv *.xlsx"), ("All files", "*.*")])
        if path:
            self.multi = ""
            self.ai_prompt = False
            self.close_entry("src")                  # it still holds whatever was typed; that must not replace the file
            self.source_text = path
            self.submit_source()

    def toggle_ai_prompt(self):
        self.ai_prompt = not self.ai_prompt
        self.source_text = "" if self.ai_prompt else self.source_text
        self.multi = ""
        self.service_key = None
        self.set_status("")
        self.redraw_a()

    def _when_free(self, fn, tries=60):
        if not self.runner.busy():
            fn()
        elif tries:
            self.root.after(120, lambda: self._when_free(fn, tries - 1))

    def start_resolve(self, text):
        self.resolve_cancelled = False
        self.status, self.status_tone = "Reading…", "fg2"
        self.set_stage("resolving", animate=False)

        def work(stop, post):
            ctx = ingest.Ctx(stop, lambda t: post("status", t))
            col = ingest.resolve(text, ctx)
            if stop.is_set():
                return
            post("resolved", (col, fetch_art(col.artwork, stop)))
        self._when_free(lambda: self.runner.start("resolve", work))

    def start_generate(self, prompt):
        self.resolve_cancelled = False
        try:
            helper = ai.connect(self.s)
        except Exception as e:
            self.set_status(str(e), "bad")
            return
        count = 25
        m = re.search(r"\b(\d{1,3})\s*(?:songs|tracks|tunes)\b", prompt, re.I)
        if m:
            count = max(5, min(100, int(m.group(1))))
        self.status, self.status_tone = "Asking the AI…", "fg2"
        self.set_stage("resolving", animate=False)

        def work(stop, post):
            ctx = ingest.Ctx(stop, lambda t: post("status", t))
            col = helper.generate(prompt, count, ctx)
            if stop.is_set():
                return
            post("resolved", (col, None))
        self._when_free(lambda: self.runner.start("generate", work))

    def cancel_resolve(self):
        self.resolve_cancelled = True
        self.runner.cancel()
        self.status = ""
        self.set_stage("idle", animate=False)

    def on_resolved(self, col, art):
        if self.resolve_cancelled or self.stage != "resolving":
            return
        if not col.tracks:
            self.resolve_failed("No songs were found there.")
            return
        self.col, self.col_art = col, art
        self.status = ""
        self.ai_prompt = False if col.service == "ai" else self.ai_prompt
        self.set_stage("ready")

    def on_art(self, data):
        self.col_art = data

    def resolve_failed(self, message):
        if self.resolve_cancelled:
            return
        self.status, self.status_tone = message, "bad"
        self.set_stage("idle", animate=False)
