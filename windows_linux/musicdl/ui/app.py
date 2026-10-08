"""
app.py — the window itself: header, the two glass cards, page changes, and the event pump that connects the
interface to the engine (resolving links, running a Job, AI tasks).

The screens live in the view modules and are mixed in here; this file owns state, layout and the message loop.
"""
import dataclasses
import logging
import os
import threading
import time
from collections import deque
from tkinter import messagebox

from .. import APP_NAME, ai, autotune, platform_
from ..core import engine, netio
from ..ingest import suggest
from ..telemetry.stats import T
from . import glass as gk
from .glass import hx
from .protect import ProtectMixin
from .rows import Rows
from .runner import Runner, wait_until
from .shell import Shell
from .splash import SplashMixin
from .views_activity import ActivityMixin
from .views_advanced import AdvancedMixin
from .views_quality import QualityMixin
from .views_settings import SettingsMixin
from .views_source import SourceMixin
from .welcome import WelcomeMixin

log = logging.getLogger("musicdl")
ROWS_MAX, ATTN_MAX, THUMBS_KEPT = 1500, 500, 300
M = 32                                              # outer margin (logical px)
HEAD_H = 80
CARD_A_H = 168
CARD_B = ("B", "rows", "Bcont", "Bsw")                # canvas tags / region groups that belong to the lower card
EVERYTHING = ("H", "A") + CARD_B


class RunState:
    """Everything the window shows about the current download. Bounded: a 5,000-song run never grows without limit."""

    def __init__(self, total, outdir):
        self.total, self.outdir = total, outdir
        self.todo, self.planned = 0, False
        self.done = self.worked = 0
        self.counts = {}
        self.phase = "Getting ready…"
        self.active = {}
        self.rows = deque(maxlen=ROWS_MAX)
        self.attn = deque(maxlen=ATTN_MAX)
        self.t0 = time.monotonic()
        self.stopped = False
        self.finished = False
        self.note = ""
        self.paused = None                  # set while the disk is full: {'free': bytes, 'need': bytes}
        self.pace = ""                      # set while the governor holds the run back ('Easing off while on battery — 2 at once')
        self.pace_songs = 0                 # ... as numbers: the songs allowed at once, and whether that is the normal pace
        self.pace_state = "normal"
        self.bursts = 0                     # songs finished with something new (the waveform ripples for each)
        self.seed = int(time.time() * 1000) & 0xFFFF            # gives every run its own waveform
        self.settings = None                # the settings the run started with (a chosen close match is fetched with them)

    @property
    def fraction(self):
        return min(1.0, self.done / self.total) if self.total else 0.0

    @property
    def attention(self):
        return sum(1 for r in self.attn)


class App(SourceMixin, QualityMixin, AdvancedMixin, ActivityMixin, SettingsMixin, WelcomeMixin, ProtectMixin, SplashMixin,
          Shell):
    TABS = (("format", "Format"), ("sound", "Sound"), ("extras", "Extras"), ("naming", "Naming"), ("live", "Live"),
            ("activity", "Activity"))

    def __init__(self, settings, initial=""):
        Shell.__init__(self, settings)
        self.M = M
        self.page = "main"
        self.stage = "idle"                 # idle | resolving | ready | running | stopping | done
        self.col = None
        self.col_art = None                 # artwork bytes of the loaded collection
        self.tab = "format"
        self.runner = Runner()
        self.scenes = {}
        self.splashing = False
        self.transitioning = False
        self.run = None
        self.rows = Rows(self, "rows")
        self.rec_total = 0
        self._init_source(initial)                          # settings are remembered; the last link or list is not
        self._init_quality()
        self._init_advanced()
        self._init_activity()
        self._init_settings()
        self._init_protect()
        self._init_welcome()
        self.root.after(40, self.begin)

    # ---------------------------------------------------------------- start-up
    def begin(self):
        self.root.update_idletasks()
        if not self.W:
            self.root.after(30, self.begin)
            return
        self.compute_layout()
        if self.s.splash:
            self.run_splash(self.finish_start)                  # (finish_start is its fallback: open without the show)
        else:
            self.cue("startup")
            self.finish_start()

    def finish_start(self):
        self.splashing = False
        self.paint(first=True)
        self.root.after(2500, self.maybe_tune)
        self.start_followups()

    def start_followups(self):
        """Warming up, the first-run sheets and the file check, once the window is up (from finish_start, or the launch
        animation's hand-over, which doesn't go through finish_start)."""
        if getattr(self, "_followups", False):
            return
        self._followups = True
        self.root.after(900, lambda: netio.prewarm(netio.SEARCH_HOSTS))     # the first search finds a connection waiting
        self.root.after(1500, lambda: threading.Thread(target=suggest.warm, name="suggest-warm", daemon=True).start())
        self.root.after(self.WELCOME_DELAY, self.maybe_welcome)               # (first run only)
        self.root.after(self.SHIELD_DELAY, self.start_shield)

    def maybe_tune(self):
        """Measure this computer and connection in the background (Automatic mode, at most weekly)."""
        if not autotune.due(self.s):
            return

        def work():
            try:
                self.runner.post("tuned", autotune.optimize())
            except Exception:
                log.debug("autotune failed", exc_info=True)
        threading.Thread(target=work, name="autotune", daemon=True).start()

    def on_tuned(self, result):
        self.s.auto_parallel, self.s.auto_retries = result["parallel"], result["retries"]
        self.s.auto_info = result["info"]
        self.s.save()
        self._settings_refresh()

    # ---------------------------------------------------------------- layout
    def compute_layout(self):
        S = self.S
        self.w, self.h = self.W / S, self.H / S
        w, h = self.w, self.h
        self.rA = (M, HEAD_H, w - 2 * M, CARD_A_H)
        self.rB = (M, HEAD_H + CARD_A_H + 14, w - 2 * M, max(300, h - M - (HEAD_H + CARD_A_H + 14)))
        self.rS = (M, HEAD_H, w - 2 * M, max(300, h - M - HEAD_H))

    def card_px(self, rect):
        x0, y0, x1, y1 = self.box(rect)
        return x0, y0, x1, y1

    def render_scene(self, page=None):
        page = page or self.page
        key = (page, self.W, self.H, self.name)
        scene = self.scenes.get(key)
        if scene is None:
            if len(self.scenes) > 3:
                self.scenes.clear()
            scene = gk.make_background(self.W, self.H, self.th, self.S)
            for rect in ((self.rA, self.rB) if page == "main" else (self.rS,)):
                gk.glass(scene, self.card_px(rect), self.p(26), self.th, self.S)
            self.scenes[key] = scene
        return scene

    # ---------------------------------------------------------------- painting
    def paint(self, first=False, staged=False):
        """Draw the page. `staged` builds it hidden, behind the launch animation, which opens it when it is done."""
        if self.splashing and not staged:
            return
        self.close_entries()
        self.close_popup()
        self.cv.delete("all")
        self.clear_regions(*EVERYTHING, "popup", "chrome")
        self.scrollers.clear()
        self.scene = self.render_scene()
        self.put(0, 0, self.scene_photo(self.scene), tags="scene")
        self.apply_window_theme()
        self.draw_page()
        self.redraw_sheet()
        if staged:
            self.tag_ui()
            self.cv.itemconfigure("ui", state="hidden")
        elif first:
            self.tag_ui()
            self.reveal("ui", dy=12, dur=0.45)
        self.root.after(350, self.prerender)

    def prerender(self):
        """Render the other page's backdrop while idle, so opening Settings doesn't stall."""
        if self.alive and not self.anims and not self.splashing:
            try:
                self.render_scene("settings" if self.page == "main" else "main")
            except Exception:
                log.debug("prerender failed", exc_info=True)

    def draw_page(self):
        self.draw_header()
        if self.page == "main":
            self.draw_card_a()
            self.draw_card_b()
        else:
            self.draw_settings()
        self.cv.tag_raise("popup")

    def tag_ui(self):
        for it in self.cv.find_all():
            tags = self.cv.gettags(it)
            if "scene" not in tags and "popup" not in tags and "sheet" not in tags:
                self.cv.addtag_withtag("ui", it)

    # ---------------------------------------------------------------- header
    def draw_header(self):
        cv, p, th = self.cv, self.p, self.th
        cv.delete("H")
        self.clear_regions("H")
        fg = hx(th["fg"])
        cy = 38
        if self.page == "main":
            icon = self.cached(("hicon", p(36)), lambda: gk.app_icon(p(36)))
            cv.create_image(p(M), p(cy), anchor="w", image=icon, tags=("H", "hicon"))
            cv.create_text(p(M + 48), p(cy), text=APP_NAME, anchor="w", font=self.f_title, fill=fg, tags="H")
            gx = self.w - M - 36
            gear = self.cached(("gear", p(18), self.name), lambda: gk.gear_icon(p(18), th["fg2"]))
            self.add_button("gear", gx, cy - 18, 36, 36, "", "glass", lambda: self.goto("settings"), icon=gear,
                            group="H", sound="nav")
            sw, sh = 200, 34
            sx = gx - 14 - sw
            self.segmented("mode", self.box((sx, cy - sh / 2, sw, sh))[:2] + (self.p(sx + sw), self.p(cy + sh / 2)),
                           ["Optimized", "Advanced"], 1 if self.s.advanced else 0, self.set_mode, "H")
        else:
            back = self.cached(("back", p(18), self.name), lambda: gk.chevron_icon(p(18), th["fg2"], "left"))
            self.add_button("back", M, cy - 18, 36, 36, "", "glass", lambda: self.goto("main"), icon=back, group="H",
                            sound="nav")
            cv.create_text(p(M + 52), p(cy), text="Settings", anchor="w", font=self.f_title, fill=fg, tags="H")
        cv.addtag_withtag("keep", "H")

    def set_mode(self, i):
        mode = "advanced" if i == 1 else "easy"
        if mode == self.s.mode:
            return
        self.s.mode = mode
        self.s.save()
        self.close_entries()
        self.swap(("A",) + CARD_B, self._redraw_cards)

    def _redraw_cards(self):
        self.close_entries()
        self.cv.delete("A", *CARD_B)
        self.clear_regions("A", *CARD_B)
        self.draw_card_a()
        self.draw_card_b()
        self.tag_ui()

    # ---------------------------------------------------------------- pages
    def goto(self, page):
        if page == self.page or self.transitioning or self.splashing:
            return
        self.transitioning = True
        self.close_popup()
        self.close_entries()
        self.tag_ui()

        def after_out():
            self.page = page
            self.cv.delete("ui", "entry", *EVERYTHING)
            self.clear_regions(*EVERYTHING)
            self.scrollers.clear()
            self.crossfade_scene(self.render_scene(page))
            self.apply_window_theme()
            self.draw_page()
            self.tag_ui()
            self.transitioning = False
            self.reveal("ui", dy=10, dur=0.32)
        self.dismiss("ui", dur=0.14, done=after_out)

    def on_escape(self):
        if self.sheet_state:
            Shell.on_escape(self)
        elif self.popup_state:
            self.close_popup()
        elif self.page == "settings":
            self.goto("main")

    def retheme(self):
        """Appearance changed: rebuild everything in the new colours."""
        old = self.scene
        self.set_theme()
        self.scenes.clear()
        self.close_entries()
        self.cv.delete("all")
        self.clear_regions(*EVERYTHING, "popup")
        self.scene = self.render_scene()
        self.cv.create_image(0, 0, anchor="nw", image=self.photo("scene", old), tags="scene")
        self.apply_window_theme()
        self.draw_page()
        self.redraw_sheet()
        self.animate("retheme", 0.30, lambda f: self.cv.itemconfigure("scene", image=self.photo("scene", gk_blend(old, self.scene, f))))

    # ---------------------------------------------------------------- relayout
    def _relayout(self):
        self._relayout_job = None
        if self.splashing:
            return
        self.cache.clear()
        self.compute_layout()
        self.paint()
        self.after_relayout()

    def after_relayout(self):
        self.src_text_restore()

    # ---------------------------------------------------------------- messages from workers
    def on_tick(self, now, dt):
        msgs = self.runner.drain(300)
        for kind, payload in msgs:
            try:
                self.handle(kind, payload)
            except Exception:
                log.exception("handling %s failed", kind)
        self.busy = self.stage in ("resolving", "running", "stopping") or self.runner.busy()
        if self.sheet_state:
            self.cv.tag_raise("sheet")                          # screens redrawing underneath never cover the question
        if self.page == "main" and not self.splashing:
            self.tick_card_a(now, dt)
            self.tick_card_b(now, dt)

    def handle(self, kind, payload):
        if kind == "status":
            self.set_status(payload)
        elif kind == "resolved":
            self.on_resolved(*payload)
        elif kind == "art":
            self.on_art(payload)
        elif kind == "suggest":
            self.on_suggest(*payload)
        elif kind == "ev":
            self.on_event(payload)
        elif kind == "ask":
            self.ask_upgrade(*payload)
        elif kind == "counts":
            self.run.counts = dict(payload) if self.run else {}
        elif kind == "note":
            if self.run:
                self.run.note = payload
        elif kind == "models":
            self.on_models(*payload)
        elif kind == "tidied":
            self.on_tidied(payload)
        elif kind == "tuned":
            self.on_tuned(payload)
        elif kind == "shield":
            self.on_shield(payload)
        elif kind == "error":
            self.on_error(*payload)
        elif kind == "done":
            self.on_worker_done(payload)

    def on_error(self, name, message):
        log.warning("%s error: %s", name, message)
        if name in ("resolve", "generate"):
            self.resolve_failed(message)
        elif name == "job":
            if self.run:
                self.run.note = f"Something went wrong: {message}"
        elif name == "pick":
            self.pick_failed(message)
        elif name == "tidy":
            self.on_tidied(f"Couldn’t finish: {message}")
        elif name == "models":
            self.on_models(self.ai_provider_now(), None, message)

    def on_worker_done(self, name):
        if name == "job":
            self.finish_job()
        if name in ("job", "pick"):
            self._flush_picks()

    # ---------------------------------------------------------------- running a job
    def start_job(self):
        if self.runner.busy() or not self.col or not self.col.tracks:
            return
        st = dataclasses.replace(self.s)                  # the run keeps the settings it started with
        os.makedirs(st.outdir, exist_ok=True)
        tracks = list(self.col.tracks)
        self.run = RunState(len(tracks), st.outdir)
        self.run.settings = st
        self.picks.clear()
        self._pick = None
        self.pulse = None
        self.shown_fraction = 0.0
        self.reset_activity()
        self.reset_live()
        T.reset()
        prev_tab = self.tab
        if self.s.advanced:
            self.tab = "live"
        self.set_stage("running", rebuild_b=prev_tab != self.tab)

        def work(stop, post):
            helper = None
            if st.ai_enabled and (st.ai_repair or st.ai_organize):
                try:
                    helper = ai.connect(st)
                except Exception as e:
                    post("note", f"AI skipped: {e}")
            def ask(summary):
                """Put the question on screen and wait for the answer (True = replace with better versions)."""
                box = {"event": threading.Event(), "answer": False}
                post("ask", (summary, box))
                while not box["event"].wait(0.25):
                    if stop.is_set():
                        raise engine.Stopped()
                return box["answer"]

            job = engine.Job(tracks, st.outdir, st, emit=lambda ev: post("ev", ev), stop=stop, ai=helper, ask=ask)
            post("counts", job.run())
            if job.summary():
                post("note", job.summary())
        self.runner.start("job", work)

    def ask_upgrade(self, summary, box):
        """Some songs in the folder are lower quality than the quality chosen now: replace them, or keep them?"""
        if self.stage != "running" or self.sheet_state:
            box["event"].set()                                  # stopped meanwhile (or a second question): keep
            return
        n = summary["count"]
        one = n == 1
        lines = [f"{n} song{'' if one else 's'} in this folder {'is' if one else 'are'} lower quality than you "
                 f"chose ({summary['target']})."]
        if summary["have"]:
            lines[0] += f" {'It is' if one else 'They are'} now {' and '.join(summary['have'])}."
        eg = summary["examples"]
        if eg:
            more = n - len(eg)
            lines.append("Includes " + ", ".join(eg) + (f" and {more} more." if more > 0 else "."))
        lines.append("Old files are only removed after the better version has been saved and checked. If none can be "
                     "found, the old file stays. Change this any time in Settings › Better versions.")

        def answer(key):
            box["answer"] = key == "replace"
            box["event"].set()
        self.cue("nav")
        self.open_sheet("Replace with higher quality?", lines,
                        [("keep", "Keep Existing", "glass"), ("replace", "Replace", "primary")], answer, default="keep")

    def stop_job(self):
        if self.stage != "running":
            return
        self.run.stopped = True
        self.close_sheet()
        platform_.attention(self.root, False)
        self.runner.cancel()
        self.set_stage("stopping", animate=False)

    def on_event(self, ev):
        r = self.run
        if r is None:
            return
        if self._pick is not None:
            self.on_pick_event(ev)
            return
        kind = ev["type"]
        if kind == "phase":
            r.phase = ev["text"]
        elif kind == "plan":
            r.todo, r.planned = ev["todo"], True
        elif kind == "begin":
            r.active[ev["index"]] = dict(track=ev["track"], stage="Starting…", t0=time.monotonic())
        elif kind == "pace":
            r.pace = ev["text"] if ev.get("easing") else ""
            r.pace_songs, r.pace_state = ev.get("songs") or 0, ev.get("state") or "normal"
        elif kind == "stage":
            a = r.active.get(ev["index"])
            if a:
                a["stage"] = ev["text"]
        elif kind == "result":
            self.add_result(ev)
        elif kind == "paused":
            r.paused = ev
            self.cue("nav")
            platform_.attention(self.root, True, "Disk full — paused. Free up some space and it will carry on.")
        elif kind == "resumed":
            r.paused = None
            platform_.attention(self.root, False)
        self.activity_dirty = True

    def add_result(self, ev):
        r, res = self.run, ev["result"]
        r.active.pop(ev["index"], None)
        r.done += 1
        if res.status != "skipped":
            r.worked += 1
            r.bursts += 1
        row = dict(title=res.track or ev["track"].title, artist=res.artist or ev["track"].artist, status=res.status,
                   note=res.note, kbps=res.kbps or res.quality_kbps, attention=res.attention, thumb=res.thumb, path=res.path,
                   service=res.source, art=ev["track"].artwork, close=res.close, track=ev["track"])
        if res.status != "skipped" or res.attention:
            if len(r.rows) >= THUMBS_KEPT:                  # old thumbnails go; the text rows stay
                r.rows[-THUMBS_KEPT]["thumb"] = None
            r.rows.append(row)
        if res.attention:
            r.attn.append(row)
        res.thumb = None

    def finish_job(self):
        r = self.run
        if r is None or r.finished:
            return
        r.finished = True
        self.close_sheet()
        r.paused = None
        platform_.attention(self.root, False)
        r.active.clear()
        self.shown_fraction = r.fraction if not r.stopped else self.shown_fraction
        self.set_stage("done")
        if not r.stopped:
            self.cue("complete")
            self.offer_close_matches()
        self.activity_dirty = True

    def open_folder(self):
        folder = self.run.outdir if self.run else self.s.outdir
        platform_.open_path(folder)

    # ---------------------------------------------------------------- stage changes
    def set_stage(self, stage, animate=True, rebuild_b=False):
        old, self.stage = self.stage, stage
        if self.page != "main":
            return
        self.src_remember()
        both = rebuild_b or self.card_b_view_for(old) != self.card_b_view()
        tags = ("A",) + CARD_B if both else ("A",)
        if animate and not self.transitioning:
            self.swap(tags, lambda: self._redraw_after_stage(tags))
        else:
            self._redraw_after_stage(tags)

    def _redraw_after_stage(self, tags):
        self.close_entries()
        self.cv.delete(*tags)
        self.clear_regions(*tags)
        self.draw_card_a()
        if "B" in tags:
            self.draw_card_b()
        self.tag_ui()

    def card_b_view_for(self, stage):
        if stage in ("running", "stopping", "done") and not self.s.advanced:
            return "activity"
        return self.tab if self.s.advanced else "quality"

    def card_b_view(self):
        return self.card_b_view_for(self.stage)

    def draw_card_b(self):
        self.cv.delete(*CARD_B)
        self.clear_regions(*CARD_B)
        self.scrollers.pop("act", None)
        view = self.card_b_view()
        if view == "quality":
            self.draw_quality()
        else:
            self.draw_advanced_or_activity(view)
        self.cv.tag_raise("popup")

    def tick_card_b(self, now, dt):
        view = self.card_b_view()
        if view == "live":
            self.tick_live(now, dt)
        elif view == "format":
            self.tick_spec(now, dt)
        elif view == "activity":
            self.tick_activity(now, dt)

    # ---------------------------------------------------------------- telemetry shared helpers
    def estimate_mb(self):
        """Storage estimate for the loaded list in the current format (None without a list)."""
        if not self.col:
            return None
        from ..config import estimate_mb, size_kbps_of
        return estimate_mb(self.col.seconds, size_kbps_of(self.s.out_format()) if self.s.advanced
                           else self.preset_kbps())

    def preset_kbps(self):
        return self.s.preset_info()["size_kbps"]

    # ---------------------------------------------------------------- shutdown
    def close(self):
        if self.runner.busy() and self.stage in ("running", "stopping"):
            if not messagebox.askyesno(APP_NAME, "A download is still running.\nStop it and quit?", parent=self.root):
                return
        self.src_remember()
        self.s.save()
        suggest.INDEX.save()
        self.runner.cancel()
        if self.runner.busy():
            self.root.title("Closing…")
            wait_until(lambda: (self.root.update() or True) and not self.runner.busy(), 6.0, 0.03)
        self.destroy()


def gk_blend(a, b, f):
    from PIL import Image
    return Image.blend(a, b, f) if a.size == b.size else b
