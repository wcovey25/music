"""
dashboard.py — the Live tab: where everything sits (plan), the words that go with the numbers (kpi_texts, stage_group)
and the window part that keeps it all up to date (Dashboard).

How it stays smooth
* Pictures (the speed ribbon, the core pillars, the slots, the server bars) are drawn by viz.py on the painter's
  thread; the window only swaps a finished picture in. A picture is asked for only when the numbers behind it changed
  enough to see.
* Text is real window text (so it matches the rest of the UI); an item is only touched when its words change.
* The layout is a pure function of the size (plan), and it never overlaps: from the smallest window to a very large
  one the same pieces appear, with more of them (the server bars, the pipeline) when there is room.
"""
import time

from ..core import netio
from ..telemetry.monitor import MONITOR
from ..telemetry.stats import T
from . import charts, fmt, viz
from . import glass as gk
from .glass import hx
from .painter import PAINTER

TAG = "Bcont"
GAP = 12
GROUPS = (("find", "Searching"), ("get", "Downloading"), ("make", "Encoding"), ("tag", "Tagging"))
ORDER = tuple(g for g, _ in GROUPS) + ("wait",)             # (waiting for disk space is rare: it has a colour, not a legend)
SHORT = {"Super": "S", "Performance": "P", "Efficiency": "E"}


# ---------------------------------------------------------------- layout

def _row(x, w, weights, gap):
    """(x, width) of boxes side by side that fill `w` in proportion to `weights`, with `gap` between (edges are rounded
    once, so the boxes never overlap)."""
    total = float(sum(weights))
    room = w - gap * (len(weights) - 1)
    out, acc = [], 0.0
    for i, wt in enumerate(weights):
        a = x + int(round(acc / total * room)) + i * gap
        acc += wt
        b = x + int(round(acc / total * room)) + i * gap
        out.append((a, max(1, b - a)))
    return out


def plan(w, h, kpi_h=85):
    """Rectangles (x, y, w, h) of every part of a `w` x `h` dashboard, relative to its corner. 'tier' says how much fits:

      compact  the smallest windows: the four numbers, the speed ribbon, and a right-hand column with the pipeline over
               the cores
      regular  the numbers, the ribbon beside the cores, and a row under them with the pipeline and the server bars
      wide     as regular, with a third box (API response) in the row under"""
    w, h = max(1, int(w)), max(1, int(h))
    g = GAP if w >= 700 else 8
    out = {"w": w, "h": h, "kpi": [(x, 0, tw, kpi_h) for x, tw in _row(0, w, [1, 1, 1, 1], g)],
           "pipeline": None, "servers": None, "latency": None}
    top = kpi_h + g
    rest = max(1, h - top)
    if rest < 330:
        out["tier"] = "compact"
        (cx, cw), (sx, sw) = _row(0, w, [29, 21], g)
        out["chart"] = (cx, top, cw, rest)
        ph = max(1, int(round((rest - g) * 0.44)))
        out["pipeline"] = (sx, top, sw, ph)
        out["system"] = (sx, top + ph + g, sw, max(1, rest - ph - g))
        return out
    bottom = max(112, min(190, int(round(rest * 0.30))))
    main = max(1, rest - bottom - g)
    (cx, cw), (sx, sw) = _row(0, w, [3, 2], g)
    out["chart"], out["system"] = (cx, top, cw, main), (sx, top, sw, main)
    yb = top + main + g
    if w >= 1240 and rest >= 400:
        out["tier"] = "wide"
        (px, pw), (vx, vw), (lx, lw) = _row(0, w, [3, 4, 3], g)
        out["pipeline"], out["servers"], out["latency"] = (px, yb, pw, bottom), (vx, yb, vw, bottom), (lx, yb, lw, bottom)
    else:
        out["tier"] = "regular"
        (px, pw), (vx, vw) = _row(0, w, [2, 3], g)
        out["pipeline"], out["servers"] = (px, yb, pw, bottom), (vx, yb, vw, bottom)
    return out


# ---------------------------------------------------------------- words

def stage_group(text):
    """Which part of the pipeline a song's stage ('Downloading (try 2)', 'Adding details', …) belongs to."""
    t = (text or "").lower()
    if t.startswith("download"):
        return "get"
    if t.startswith(("encod", "saving", "finishing")):
        return "make"
    if t.startswith(("adding", "finding")):
        return "tag"
    if t.startswith("waiting"):
        return "wait"
    return "find"                                           # searching, looking for close matches, checking the source …


def slot_cells(active, slots):
    """What each place for a song at once is doing: a list of group names, free places (None) at the end. `active` is
    the stage text of every song in flight."""
    kinds = sorted((stage_group(s) for s in active), key=ORDER.index)
    n = max(len(kinds), int(slots or 0), 4)
    return (kinds + [None] * n)[:n]


def kpi_texts(r, running, remaining_range, elapsed, speed, avg, peak, spm, lat, errors):
    """The four tiles as [(value, sub)]. `remaining_range` is T.eta_range(songs left) or None; `lat` is (median, p95) ms
    or None; the rest are plain numbers."""
    if r is None:
        return [("—", ""), ("—", ""), ("—", ""), ("—", "")]
    left = max(0, r.todo - r.worked)
    flying = len(r.active)
    if r.finished:
        eta = ("Done", f"took {fmt.clock(elapsed)}")
    elif r.stopped and not running:
        eta = ("Stopped", "")
    elif not r.planned:
        eta = ("—", "getting ready")
    elif left == 0 or (running and left <= flying):
        eta = ("Finishing up", "")
    elif remaining_range is None:
        eta = ("…", "estimating")
    else:
        exp, lo, hi = remaining_range
        eta = (fmt.duration(exp), "likely " + fmt.duration_range(lo, hi))
    counts = r.counts or {}
    saved = counts.get("ok", 0) + counts.get("upgraded", 0) + counts.get("fixed", 0)
    there = counts.get("skipped", 0) + counts.get("kept", 0)
    bad = r.attention
    bits = ([f"{saved:,} saved"] if saved else []) + ([f"{there:,} already there"] if there else []) + (
        [f"{bad:,} need a look"] if bad else [])
    songs = (f"{r.done:,} / {r.total:,}", " · ".join(bits))
    spd = (fmt.rate(speed) if running else "—", f"avg {fmt.rate(avg)} · peak {fmt.rate(peak)}" if avg > 0 else "")
    api = (f"{spm:.0f} / min" if spm is not None else "—", "")
    if lat:
        api = (api[0], f"API {fmt.ms(lat[0])} · 95% under {fmt.ms(lat[1])}" + (f" · {errors} failed" if errors else ""))
    return [eta, songs, spd, api]


def busy_text(cores, cpu):
    """'38% busy' from the load of the cores (or the overall load when the cores are not known); '' when unknown."""
    vals = [v for _n, row in cores for v in row] if cores else []
    share = (sum(vals) / len(vals)) if vals else cpu
    return "" if share is None else f"{share * 100:.0f}% busy"


def host_name(host):
    """A server's name as a person reads it: 'www.youtube.com' -> 'youtube.com'; the long generated names of media
    servers ('rr3---sn-4g5e6nsz.googlevideo.com') -> their domain."""
    labels = (host or "").split(".")
    if labels and labels[0] == "www":
        labels = labels[1:]
    if len(labels) > 3 or any("---" in x for x in labels):
        labels = labels[-2:]
    return ".".join(labels)


def shorten(text, font, maxw):
    """`text` made to fit `maxw` pixels: the last ' · ' parts go first, then the end of what is left is cut."""
    parts = text.split(" · ")
    while len(parts) > 1 and font.measure(" · ".join(parts)) > maxw:
        parts.pop()
    return gk.fit(" · ".join(parts), font, maxw)


# ---------------------------------------------------------------- the dashboard

class Dashboard:
    def __init__(self, app, area):
        self.app = app
        self.area = area                                     # (x, y, w, h) on the canvas, design pixels
        self.items, self.shown, self.sigs = {}, {}, {}
        self.rects = {}
        self.token = object()
        self._last = -1.0
        self.sync = False

    # ---- small helpers over the canvas

    def _t(self, key, x, y, font, color, anchor="nw", text=""):
        a = self.app
        self.items[key] = a.cv.create_text(a.p(x), a.p(y), text=text, anchor=anchor, font=font, fill=hx(color), tags=TAG)
        self.shown[key] = (text, tuple(color))

    def _set(self, key, text, color=None):
        item = self.items.get(key)
        if item is None:
            return
        cur = self.shown[key]
        new = (text, cur[1] if color is None else tuple(color))
        if new != cur:
            self.shown[key] = new
            self.app.cv.itemconfigure(item, text=text, fill=hx(new[1]))

    def _picture(self, key, x, y):
        a = self.app
        self.items["img-" + key] = a.cv.create_image(a.p(x), a.p(y), anchor="nw", image="", tags=TAG)

    def _show(self, key, img):
        a = self.app
        a.cv.itemconfigure(self.items["img-" + key], image=a.photo("live-" + key, img, getattr(img, "dens", 1)))

    def _ask(self, key, sig, fn, *args):
        """Have the painter draw `key` unless it already has (or is drawing) a picture for this signature."""
        if self.sigs.get(key) == sig:
            return
        self.sigs[key] = sig
        PAINTER.submit(key, self.token, fn, *args)

    def _plate(self, rect, r=14):
        x, y, w, h = rect
        self.app._plate(self.area[0] + x, self.area[1] + y, w, h, r)

    def _abs(self, rect):
        return (self.area[0] + rect[0], self.area[1] + rect[1], rect[2], rect[3])

    def _caption(self, key, rect, text):
        x, y, w, _h = rect
        th = self.app.th
        self._t(key, x + 16, y + 12, self.app.f_tiny, th["fg3"], text=text)
        self._t(key + "-r", x + w - 16, y + 12, self.app.f_tiny, th["fg2"], anchor="ne")

    def _right(self, key, rect, text, color=None):
        """The small text at the top right of a box, kept clear of the caption on its left."""
        a = self.app
        room = a.p(rect[2] - 46) - a.f_tiny.measure(self.shown[key][0])
        self._set(key + "-r", gk.fit(text, a.f_tiny, room) if text else "", color)

    # ---- building

    def build(self):
        a = self.app
        th = a.th
        PAINTER.forget()
        viz.DENSITY = a.D
        MONITOR.warm()
        ls = lambda f: f.metrics("linespace")                                  # noqa: E731
        kpi_h = 10 + ls(a.f_tiny) + 1 + ls(a.f_stat) + 1 + ls(a.f_tiny) + 10
        _x, _y, w, h = self.area
        pl = self.plan = plan(w, h, kpi_h)
        self.rects = {k: self._abs(v) for k, v in pl.items() if isinstance(v, tuple)}
        self.rects["kpi"] = [self._abs(v) for v in pl["kpi"]]
        # four numbers
        for i, (rect, cap) in enumerate(zip(self.rects["kpi"], ("TIME LEFT", "SONGS", "SPEED", "SEARCHES / MIN"))):
            x, y, tw, _th = rect
            a._plate(x, y, tw, kpi_h, 14)
            self._t(f"kc{i}", x + 16, y + 10, a.f_tiny, th["fg3"], text=cap)
            self._t(f"kv{i}", x + 16, y + 10 + ls(a.f_tiny) + 1, a.f_stat, th["fg"], text="—")
            self._t(f"ks{i}", x + 16, y + kpi_h - 10, a.f_tiny, th["fg2"], anchor="sw")
        self._build_chart(a)
        self._build_system(a)
        self._build_pipeline(a)
        self._build_servers(a)
        self._build_latency(a)
        self.sigs = {}
        self._last = -1.0
        self.tick(time.time(), 0.0, force=True)

    def _inner(self, rect, top=34, side=14, bottom=12):
        x, y, w, h = rect
        return (x + side, y + top, max(1, w - 2 * side), max(1, h - top - bottom))

    def _build_chart(self, a):
        rect = self.rects["chart"]
        a._plate(*rect, 16)
        self._caption("chart", rect, "NETWORK SPEED")
        gx, gy, gw, gh = self._inner(rect, top=36, side=14, bottom=10)
        self.gutter = 54
        self.cbox = (gx + self.gutter, gy, max(8, gw - self.gutter), gh)
        self._picture("chart", self.cbox[0], self.cbox[1])
        self.axis_keys = []
        levels = viz.ribbon_levels(a.p(self.cbox[2]), a.p(self.cbox[3]))
        for k in range(1, 4):
            key = f"ax{k}"
            self.axis_keys.append(key)
            self._t(key, gx + self.gutter - 8, gy, a.f_tiny, a.th["fg3"], anchor="e")
            a.cv.coords(self.items[key], a.p(gx + self.gutter - 8), a.p(gy) + levels[k])
        self._t("chart-empty", self.cbox[0] + self.cbox[2] / 2, self.cbox[1] + self.cbox[3] / 2, a.f_small, a.th["fg3"],
                anchor="center")

    def _build_system(self, a):
        rect = self.rects["system"]
        a._plate(*rect, 16)
        self._caption("sys", rect, "PROCESSOR")
        gx, gy, gw, gh = self._inner(rect, top=36, side=14, bottom=10)
        self.foot = gh >= 150
        lab = a.f_tiny.metrics("linespace")
        foot = (lab + 4) if self.foot else 0
        self.sbox = (gx, gy, gw, max(24, gh - lab - 4 - foot))
        self.sys_lab_y = self.sbox[1] + self.sbox[3] + 3
        self._picture("sys", self.sbox[0], self.sbox[1])
        self.sys_labels = []
        for i in range(3):
            key = f"sl{i}"
            self._t(key, gx, self.sys_lab_y, a.f_tiny, a.th["fg3"], anchor="n")
            self.sys_labels.append(key)
        self._t("sfoot", gx, gy + gh, a.f_tiny, a.th["fg3"], anchor="sw")
        self.sys_geo = None

    def _build_pipeline(self, a):
        rect = self.rects.get("pipeline")
        if not rect:
            return
        a._plate(*rect, 16)
        self._caption("pipe", rect, "PIPELINE")
        gx, gy, gw, gh = self._inner(rect, top=36, side=16, bottom=10)
        self.cap_h = max(12, min(20, int(a.f_tiny.metrics("linespace") * 1.15)))
        self.cap_box = (gx, gy, gw, self.cap_h)
        self._picture("pipe", gx, gy)
        lh = a.f_tiny.metrics("linespace") + 4
        cols = 4 if gw >= 430 else 2
        rows = -(-len(GROUPS) // cols)
        ly = gy + self.cap_h + 10
        self.legend = []
        for i, (g, name) in enumerate(GROUPS):
            cx = gx + (i % cols) * (gw / cols)
            cy = ly + (i // cols) * lh
            self._t(f"ld{i}", cx, cy, a.f_tiny, viz.SLOT_COLORS[g], text="●")
            self._t(f"lt{i}", cx + 14, cy, a.f_tiny, a.th["fg2"], text=name)
            self.legend.append((cx, gw / cols - 18))
        yline = ly + rows * lh + 2
        self.pipe_status = yline + lh <= gy + gh + 6
        self._t("pstat", gx, yline, a.f_tiny, a.th["fg3"])

    def _build_servers(self, a):
        rect = self.rects.get("servers")
        if not rect:
            return
        a._plate(*rect, 16)
        self._caption("srv", rect, "SERVERS")
        gx, gy, gw, gh = self._inner(rect, top=36, side=16, bottom=10)
        self.row_h = 24
        self.srv_rows = max(1, min(6, gh // self.row_h))
        name_w = max(70, min(150, int(gw * 0.30)))
        val_w = 58
        self.srv_geo = (gx, gy, name_w, val_w, gw)
        self.bars_box = (gx + name_w + 8, gy, max(20, gw - name_w - val_w - 16), self.srv_rows * self.row_h)
        self._picture("srv", self.bars_box[0], self.bars_box[1])
        for i in range(self.srv_rows):
            cy = gy + (i + 0.5) * self.row_h
            self._t(f"sn{i}", gx, cy, a.f_small, a.th["fg2"], anchor="w")
            self._t(f"sv{i}", gx + gw, cy, a.f_small, a.th["fg"], anchor="e")
        self._t("srv-empty", gx + gw / 2, gy + gh / 2, a.f_small, a.th["fg3"], anchor="center")

    def _build_latency(self, a):
        rect = self.rects.get("latency")
        if not rect:
            return
        a._plate(*rect, 16)
        self._caption("lat", rect, "API RESPONSE")
        gx, gy, gw, gh = self._inner(rect, top=36, side=16, bottom=10)
        self._t("lat-v", gx, gy, a.f_h, a.th["fg"], text="—")
        self._t("lat-s", gx, gy + a.f_h.metrics("linespace") + 2, a.f_tiny, a.th["fg2"])
        self._t("lat-s2", gx, gy + a.f_h.metrics("linespace") + 4 + a.f_tiny.metrics("linespace"), a.f_tiny, a.th["fg3"])
        sx = gx + max(110, int(gw * 0.42))
        self.lbox = (sx, gy, max(20, gx + gw - sx), gh)
        self._picture("lat", self.lbox[0], self.lbox[1])

    # ---- every frame / every quarter second

    def tick(self, now, dt, force=False):
        a = self.app
        self._collect()
        if not force and now - self._last < 0.25:
            return
        self._last = now
        r = a.run
        running = a.stage in ("running", "stopping")
        th, S, p = a.th, a.S, a.p
        # ---- the four numbers
        remaining = max(0, r.todo - r.worked) if r else 0
        rng = T.eta_range(remaining) if (r and r.planned and running and remaining) else None
        if running and now - a._spm_at >= 1.0:
            a._spm_at = now
            a.live_spm.append(T.searches_per_min())
        spm = a.live_spm[-1] if (a.live_spm and r) else None
        texts = kpi_texts(r, running, rng, T.elapsed(), T.speed_now(), T.avg_speed(), T.peak, spm,
                          T.latency_percentiles() if r else None, T.errors if r else 0)
        for i, (value, sub) in enumerate(texts):
            self._set(f"kv{i}", value)
            self._set(f"ks{i}", shorten(sub, a.f_tiny, p(self.rects["kpi"][i][2] - 32)) if sub else "")
        # ---- the speed ribbon
        samples = T.speed_history() if r else []
        top = max(samples) * 1.15 if samples else 0
        want = charts.nice_ceiling(top)
        a.live_ceil = want if want > a.live_ceil else max(want, a.live_ceil * 0.96)
        ceil = charts.nice_ceiling(a.live_ceil)
        cx, cy, cw, ch = self.cbox
        size = (p(cw), p(ch))
        self._ask("chart", (size, len(samples), samples[-1] if samples else 0, ceil, a.name), viz.ribbon_chart, size[0],
                  size[1], samples, ceil, th, S)
        for k, key in enumerate(self.axis_keys, start=1):
            self._set(key, fmt.axis_rate(ceil * k / 3))
        self._set("chart-empty", "" if samples else "Live numbers appear while songs are downloading")
        self._right("chart", self.rects["chart"], (f"now {fmt.rate(T.speed_now())} · peak {fmt.rate(T.peak)}"
                                                    if samples else ""))
        # ---- the processor
        self._system(running)
        # ---- the pipeline
        self._pipeline(r, running)
        # ---- the servers + API response
        self._servers(r)
        self._latency(r)

    def _collect(self):
        """Put every finished picture on the canvas (cheap: runs each frame, so a picture shows as soon as it exists)."""
        for key in ("chart", "sys", "pipe", "srv", "lat"):
            if "img-" + key not in self.items:
                continue
            img = PAINTER.take(key, self.token)
            if img is not None:
                self._show(key, img)

    def _system(self, running):
        a = self.app
        p = a.p
        mon = MONITOR.snapshot()
        live = bool(running and (mon["cores"] or mon["cpu_now"] is not None))
        if live:
            groups = mon["groups"] or ([("Cores", mon["cores"])] if mon["cores"] else [("CPU", [mon["cpu_now"]])])
        else:                                                    # at rest: the cores as they will be, all quiet
            groups = [(name, [0.0] * n) for name, n in MONITOR.shape()]
        w, h = p(self.sbox[2]), p(self.sbox[3])
        shown = [(n, [round(v * 40) / 40.0 for v in row]) for n, row in groups]
        self._ask("sys", ((w, h), tuple((n, tuple(row)) for n, row in shown), a.name), viz.core_pillars, w, h, shown,
                  a.th, a.S)
        shape = tuple((n, len(row)) for n, row in shown)
        if shape != self.sys_geo:                                # the labels under the groups move only when the shape does
            self.sys_geo = shape
            lay = viz.pillar_layout(w, h, [k for _n, k in shape])
            for i, key in enumerate(self.sys_labels):
                if i < len(lay["spans"]):
                    x0, x1 = lay["spans"][i]
                    name = shape[i][0]
                    room = max(1, x1 - x0 + p(10))
                    text = gk.fit(name, a.f_tiny, room)
                    if a.f_tiny.measure(name) > room * 1.15:
                        text = SHORT.get(name) or name[:1]
                    a.cv.coords(self.items[key], p(self.sbox[0]) + (x0 + x1) / 2, p(self.sys_lab_y))
                    self._set(key, text)
                else:
                    self._set(key, "")
        if live:
            heat = mon["heat"]
            busy = busy_text(mon["groups"] or ([("", mon["cores"])] if mon["cores"] else None), mon["cpu_now"])
            tone = a.th["ok"] if heat < 1 else a.th["warn"] if heat < 2 else a.th["bad"]
            self._right("sys", self.rects["system"], " · ".join(x for x in (viz.heat_word(heat), busy) if x), tone)
        else:
            self._right("sys", self.rects["system"], "idle", a.th["fg2"])
        if self.foot:
            bits = []
            if live and mon["rss"]:
                bits.append(f"app memory {mon['rss']:.0f} MB")
            if live and mon["battery"]:
                bits.append("on battery" + (" · battery saver" if mon["low_power"] else ""))
            self._set("sfoot", shorten(" · ".join(bits), a.f_tiny, p(self.sbox[2])) if bits else "")

    def _pipeline(self, r, running):
        if "pipe-r" not in self.shown:
            return
        a = self.app
        p = a.p
        active = [v["stage"] for v in r.active.values()] if (r and running) else []
        slots = (getattr(r, "pace_songs", 0) or 0) if r else 0
        cells = slot_cells(active, slots) if (r and running) else slot_cells([], 0)
        w, h = p(self.cap_box[2]), p(self.cap_box[3])
        self._ask("pipe", ((w, h), tuple(cells), a.name), viz.slot_capsule, w, h, cells, a.th, a.S)
        counts = dict.fromkeys(ORDER, 0)
        for s in active:
            counts[stage_group(s)] += 1
        for i, (g, name) in enumerate(GROUPS):
            n = counts[g]
            self._set(f"lt{i}", f"{n} {name.lower()}" if n else name, a.th["fg"] if n else a.th["fg3"])
        eased = bool(r) and getattr(r, "pace_state", "normal") != "normal" and running
        note = f"{len(active)} of {slots} at once" if (running and slots) else ""
        self._right("pipe", self.rects["pipeline"], note, a.th["warn"] if eased else a.th["fg2"])
        if self.pipe_status:
            bits = []
            if r and running:
                queued = max(0, r.todo - r.worked - len(r.active))
                if queued:
                    bits.append(f"{queued:,} waiting their turn")
                waiting = sum(1 for v in r.active.values() if stage_group(v["stage"]) == "wait")
                if waiting:
                    bits.insert(0, f"{waiting} waiting for disk space")
            self._set("pstat", shorten(" · ".join(bits), a.f_tiny, p(self.cap_box[2])) if bits else "")

    def _servers(self, r):
        if "srv-empty" not in self.items:
            return
        a = self.app
        p = a.p
        hosts = netio.report(self.srv_rows, 300) if r else []
        w, h = p(self.bars_box[2]), p(self.bars_box[3])
        rows = [(hh["p50"], hh["p95"]) for hh in hosts]
        sig = ((w, h), tuple((hh["host"], None if hh["p50"] is None else round(hh["p50"] / 5),
                              None if hh["p95"] is None else round(hh["p95"] / 5)) for hh in hosts), a.name)
        self._ask("srv", sig, viz.range_bars, w, h, rows, p(self.row_h), a.th, a.S)
        gx, gy, name_w, val_w, gw = self.srv_geo
        for i in range(self.srv_rows):
            if i < len(hosts):
                hh = hosts[i]
                self._set(f"sn{i}", gk.fit(host_name(hh["host"]), a.f_small, p(name_w)))
                typ = hh["p50"]
                self._set(f"sv{i}", fmt.ms(typ) if typ is not None else "—",
                          viz.server_color(hh["p95"] or typ or 0, a.th) if typ is not None else a.th["fg3"])
            else:
                self._set(f"sn{i}", "")
                self._set(f"sv{i}", "")
        self._set("srv-empty", "" if hosts else "No servers contacted yet")
        self._right("srv", self.rects["servers"], "typical → slow end" if hosts else "")

    def _latency(self, r):
        if "lat-v" not in self.items:
            return
        a = self.app
        p = a.p
        vals = T.latency_history(60) if r else []
        pct = T.latency_percentiles() if r else None
        self._set("lat-v", fmt.ms(T.latency_ms()) if vals else "—")
        self._set("lat-s", f"typical {fmt.ms(pct[0])} · 95% under {fmt.ms(pct[1])}" if pct else "")
        spm = list(a.live_spm)
        self._set("lat-s2", f"{spm[-1]:.0f} searches / min" if spm and r else "")
        w, h = p(self.lbox[2]), p(self.lbox[3])
        self._ask("lat", ((w, h), len(vals), vals[-1] if vals else 0, a.name), charts.sparkline, w, h, vals, a.th, a.S,
                  a.th["info"], 0)
