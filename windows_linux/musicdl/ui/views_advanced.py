"""
views_advanced.py — Advanced mode: the tab bar (Format · Extras · Naming · Live · Activity), the rows behind the first
three tabs, and the real-time telemetry dashboard (ETA, progress, live speed graph, searches/min, API latency).
"""
import time
from collections import deque

from ..config import DEFAULT_TEMPLATE, FORMATS, estimate_mb, size_kbps_of
from ..core.models import Track
from ..meta import naming
from ..telemetry.stats import T
from . import charts, fmt
from . import glass as gk
from .glass import hx
from .rows import Group, Row

TABS = (("format", "Format"), ("extras", "Extras"), ("naming", "Naming"), ("live", "Live"), ("activity", "Activity"))
STYLES = (("{title} - {artist}", "Title - Artist"), ("{artist} - {title}", "Artist - Title"),
          ("{artist}/{title}", "Artist / Title"), ("{artist}/{album}/{track} {title}", "Artist / Album / 01 Title"))
SAMPLE = Track("Skyfall", "Adele", album="Skyfall", year="2012", track_no=1, genre="Pop")


class AdvancedMixin:
    def _init_advanced(self):
        self.live = {}
        self._live_last = 0.0
        self.live_spm = deque(maxlen=60)
        self.live_ceil = 256 * 1024.0
        self._spm_at = 0.0

    def reset_live(self):
        self.live_spm.clear()
        self.live_ceil = 256 * 1024.0
        self._spm_at = 0.0

    # ---------------------------------------------------------------- tab bar + dispatch
    def draw_advanced_or_activity(self, view):
        if not self.s.advanced:                                    # Optimized mode while a run is active
            self.draw_activity_view(title=True)
            return
        cv, p = self.cv, self.p
        bx, by, bw, bh = self.rB
        w = min(bw - 56, 470)
        keys = [k for k, _ in TABS]
        self.segmented("tabs", (p(bx + 28), p(by + 16), p(bx + 28 + w), p(by + 16 + 34)), [lab for _k, lab in TABS],
                       keys.index(self.tab), self.pick_tab, "B")
        cv.addtag_withtag("keep", "B")
        self.draw_tab_content()

    def pick_tab(self, i):
        key = TABS[i][0]
        if key == self.tab:
            return
        self.tab = key
        self.close_entry("row")
        self.close_popup()
        self.swap(("rows", "Bcont", "Bsw"), self._redraw_tab_content)

    def _redraw_tab_content(self):
        self.cv.delete("rows", "Bcont", "Bsw")
        self.clear_regions("rows", "Bcont", "Bsw")
        self.scrollers.pop("act", None)
        self.draw_tab_content()
        self.tag_ui()

    def draw_tab_content(self):
        bx, by, bw, bh = self.rB
        p = self.p
        area = (p(bx + 16), p(by + 66), p(bx + bw - 16), p(by + bh - 14))
        self.live = {}
        if self.tab == "live":
            self.draw_live()
        elif self.tab == "activity":
            self.act_area = area
            self.draw_act_switch(by + 16)
            self.cv.addtag_withtag("keep", "Bsw")
            self.draw_act_rows(reset=True)
        else:
            self.rows.groups = self.tab_groups(self.tab)
            self.rows.draw(area, reset=True)
        self.cv.tag_raise("keep")
        self.cv.tag_raise("popup")

    # ---------------------------------------------------------------- rows
    def size_line(self):
        kbps = size_kbps_of(self.s.out_format())
        if self.col:
            return f"about {fmt.size(estimate_mb(self.col.seconds, kbps))} for {fmt.plural(len(self.col.tracks), 'song')}"
        return f"about {fmt.size(estimate_mb(210, kbps))} per song"

    def _naming_sample(self):
        return self.col.tracks[0] if self.col and self.col.tracks else SAMPLE

    def tab_groups(self, tab):
        s = self.s

        def put(attr, value=None, then=None):
            def set_(v):
                setattr(s, attr, v if value is None else value)
                s.clamp()
                s.save()
                if then:
                    then()
            return set_

        if tab == "format":
            lossless = lambda: FORMATS[s.fmt]["lossless"]
            rates = lambda: [(0, "Source"), (44100, "44.1 kHz"), (48000, "48 kHz")] + (
                [] if s.fmt == "mp3" else [(96000, "96 kHz")])

            def set_fmt(v):
                s.fmt = v
                if v == "mp3" and s.sample_rate > 48000:
                    s.sample_rate = 0
                s.clamp()
                s.save()
                self.refresh_estimate()
            return [
                Group("Output", [
                    Row("segment", "Format", choices=[(k, f["label"]) for k, f in FORMATS.items()], get=lambda: s.fmt,
                        set=set_fmt, refresh=True, min=60),
                    Row("popup", "Bitrate", choices=lambda: [(b, f"{b} kbps") for b in FORMATS[s.fmt]["bitrates"]],
                        get=lambda: s.bitrate, set=put("bitrate", then=self.refresh_estimate), width=130,
                        show=lambda: not lossless()),
                    Row("segment", "Sample rate", choices=rates, get=lambda: s.sample_rate,
                        set=put("sample_rate", then=self.refresh_estimate), refresh=True,
                        sub="Resample every song, or keep what the source has", min=62),
                    Row("segment", "Bit depth", choices=[(16, "16-bit"), (24, "24-bit")], get=lambda: s.bit_depth,
                        set=put("bit_depth", then=self.refresh_estimate), show=lossless, refresh=True),
                    Row("info", "Estimated size", get=self.size_line),
                ]),
                Group("Encoder", [
                    Row("text", "Custom encoder flags", sub="Extra ffmpeg options, added after the codec settings",
                        get=lambda: s.encoder_flags, set=put("encoder_flags"), placeholder="None", width=250),
                ], note="Example: -compression_level 8 for smaller FLAC files. A wrong flag makes ffmpeg refuse the "
                        "file, and the song will show up under Needs attention."),
            ]
        if tab == "extras":
            return [Group("Added to every song", [
                Row("switch", "Embed artwork", sub="Cover art inside each file", get=lambda: s.embed_art,
                    set=put("embed_art")),
                Row("switch", "Write metadata tags", sub="Title, artist, album, year, track number, genre",
                    get=lambda: s.write_tags, set=put("write_tags")),
            ]), Group("Lyrics", [
                Row("switch", "Fetch lyrics", sub="Looked up online and stored in the file", get=lambda: s.fetch_lyrics,
                    set=put("fetch_lyrics"), refresh=True),
                Row("switch", "Save .lrc files", sub="Time-synced lyrics next to each song, when available",
                    get=lambda: s.lrc_files, set=put("lrc_files"), show=lambda: s.fetch_lyrics),
            ])]
        # naming
        style = lambda: next((t for t, _l in STYLES if t == s.template), "")
        return [Group("File names", [
            Row("popup", "Quick styles", choices=[(t, l) for t, l in STYLES], get=style, set=put("template"),
                refresh=True, width=230, placeholder="Custom"),
            Row("text", "Template", sub="Use / to put songs in folders", get=lambda: s.template,
                set=lambda v: (setattr(s, "template", v or DEFAULT_TEMPLATE), s.save()), width=280),
            Row("info", "Preview", get=lambda: naming.relative_name(self._naming_sample(), s.template.strip()
                                                                    or DEFAULT_TEMPLATE, s.out_format().ext)),
        ], note="Fields: {title} {artist} {album} {year} {track} {disc} {genre}. Songs already saved under a "
                "different name are treated as new ones.")]

    # ---------------------------------------------------------------- live dashboard
    def draw_live(self):
        cv, p, th = self.cv, self.p, self.th
        bx, by, bw, bh = self.rB
        x, y, w = bx + 28, by + 68, bw - 56
        avail = bh - 68 - 16
        gap = 12
        lv = self.live = {}
        tile_w, tile_h = (w - 2 * gap) / 3, 70
        for i, (key, cap) in enumerate((("eta", "TIME LEFT"), ("songs", "SONGS"), ("speed", "SPEED"))):
            tx = x + i * (tile_w + gap)
            self._plate(tx, y, tile_w, tile_h, 14)
            cv.create_text(p(tx + 16), p(y + 12), text=cap, anchor="nw", font=self.f_tiny, fill=hx(th["fg3"]), tags="Bcont")
            lv[key] = cv.create_text(p(tx + 16), p(y + 31), text="—", anchor="nw", font=self.f_stat, fill=hx(th["fg"]),
                                     tags="Bcont")
        yb = y + tile_h + 14
        tw, bar_h = p(w), p(8)
        track = self.cached(("track", tw, bar_h, self.name), lambda: gk.pill(tw, bar_h, th["track"], th, self.S))
        cv.create_image(p(x), p(yb), anchor="nw", image=track, tags="Bcont")
        lv["bar"] = cv.create_image(p(x), p(yb), anchor="nw", image="", tags="Bcont")
        lv["bar_geo"] = (p(x), p(yb), tw, bar_h)
        lv["bar_w"] = None
        spark_h = 78
        yg = yb + 8 + 16
        gh = max(112, avail - (yg - (by + 68)) - 12 - spark_h)
        self._plate(x, yg, w, gh, 16)
        cv.create_text(p(x + 16), p(yg + 13), text="NETWORK SPEED", anchor="nw", font=self.f_tiny, fill=hx(th["fg3"]),
                       tags="Bcont")
        lv["gnow"] = cv.create_text(p(x + w - 16), p(yg + 13), text="", anchor="ne", font=self.f_tiny, fill=hx(th["fg2"]),
                                    tags="Bcont")
        cbox = (p(x + 16), p(yg + 34), p(w - 32), p(gh - 34 - 10))
        lv["cbox"] = cbox
        lv["chart"] = cv.create_image(cbox[0], cbox[1], anchor="nw", image="", tags="Bcont")
        lv["axis"] = [cv.create_text(cbox[0] + p(4), cbox[1], text="", anchor="sw", font=self.f_tiny, fill=hx(th["fg3"]),
                                     tags="Bcont") for _ in range(3)]
        lv["empty"] = cv.create_text(cbox[0] + cbox[2] / 2, cbox[1] + cbox[3] / 2, text="", font=self.f_small,
                                     fill=hx(th["fg3"]), tags="Bcont")
        ys = yg + gh + 12
        half = (w - gap) / 2
        for i, (key, cap) in enumerate((("spm", "SEARCHES / MIN"), ("lat", "API RESPONSE"))):
            tx = x + i * (half + gap)
            self._plate(tx, ys, half, spark_h, 14)
            cv.create_text(p(tx + 16), p(ys + 12), text=cap, anchor="nw", font=self.f_tiny, fill=hx(th["fg3"]), tags="Bcont")
            lv[key] = cv.create_text(p(tx + 16), p(ys + 34), text="—", anchor="nw", font=self.f_h, fill=hx(th["fg"]),
                                     tags="Bcont")
            sbox = (p(tx + half * 0.46), p(ys + 14), p(half * 0.54 - 16), p(spark_h - 28))
            lv[key + "_box"] = sbox
            lv[key + "_img"] = cv.create_image(sbox[0], sbox[1], anchor="nw", image="", tags="Bcont")
        self._live_last = 0.0
        self.tick_live(time.time(), 0.0, force=True)

    def _plate(self, x, y, w, h, r):
        p = self.p
        pw, ph = p(w), p(h)
        img = self.cached(("plate", pw, ph, self.name), lambda: gk.panel_image(pw, ph, p(r), self.th, self.S))
        self.cv.create_image(p(x), p(y), anchor="nw", image=img, tags="Bcont")

    def tick_live(self, now, dt, force=False):
        lv = self.live
        if not lv or "chart" not in lv:
            return
        r = self.run
        running = self.stage in ("running", "stopping")
        self._live_bar(r)
        if not force and now - self._live_last < 0.25:
            return
        self._live_last = now
        cv, th, S = self.cv, self.th, self.S
        # stat tiles
        if r:
            eta = T.eta(max(0, r.todo - r.worked)) if (r.planned and running) else None
            cv.itemconfigure(lv["eta"], text=("Done" if r.finished else "—" if not r.planned else
                                              "0 sec" if r.todo == 0 else fmt.duration(eta) if eta is not None else "…"))
            cv.itemconfigure(lv["songs"], text=f"{r.done:,} / {r.total:,}")
        else:
            cv.itemconfigure(lv["eta"], text="—")
            cv.itemconfigure(lv["songs"], text="—")
        cv.itemconfigure(lv["speed"], text=fmt.rate(T.speed_now()) if running else "—")
        # speed graph
        samples = T.speed_history() if r else []
        top = max(samples) * 1.15 if samples else 0
        want = charts.nice_ceiling(top)
        self.live_ceil = want if want > self.live_ceil else max(want, self.live_ceil * 0.96)
        ceil = charts.nice_ceiling(self.live_ceil)
        x, y, w, h = lv["cbox"]
        img = charts.speed_chart(w, h, samples, ceil, th, S)
        cv.itemconfigure(lv["chart"], image=self.photo("livechart", img))
        for k, item in enumerate(lv["axis"], start=1):
            yy = y + (h - 2) * (1 - k / 3) + 1
            cv.coords(item, x + self.p(4), yy + self.p(14) if k == 3 else yy - 1)
            cv.itemconfigure(item, text="" if (k == 2 and h < self.p(120)) else fmt.axis_rate(ceil * k / 3))
        cv.itemconfigure(lv["gnow"], text=f"now {fmt.rate(T.speed_now())} · peak {fmt.rate(T.peak)}" if samples else "")
        cv.itemconfigure(lv["empty"], text="" if samples else "Live numbers appear while songs are downloading")
        # searches per minute / latency
        if running and now - self._spm_at >= 1.0:
            self._spm_at = now
            self.live_spm.append(T.searches_per_min())
        spm = list(self.live_spm)
        lat = T.latency_history(60) if r else []
        cv.itemconfigure(lv["spm"], text=f"{spm[-1]:.0f} / min" if spm else "—")
        cv.itemconfigure(lv["lat"], text=fmt.ms(T.latency_ms()) if lat else "—")
        for key, vals, color in (("spm", spm, th["accent"]), ("lat", lat, th["info"])):
            sx, sy, sw, sh = lv[key + "_box"]
            img = charts.sparkline(sw, sh, vals, th, S, color=color, floor=0)
            cv.itemconfigure(lv[key + "_img"], image=self.photo("live_" + key, img))

    def _live_bar(self, r):
        lv = self.live
        x, y, tw, bh = lv["bar_geo"]
        frac = self.shown_fraction if r else 0.0
        w = max(bh, int(tw * frac)) if frac > 0.004 else 0
        q = (w // 6) * 6 if w < tw else tw
        if q == lv["bar_w"]:
            return
        lv["bar_w"] = q
        g = int(round(9 * self.S))
        if not q:
            self.cv.itemconfigure(lv["bar"], image="")
            return
        img = self.cached(("bar", max(q, bh), bh, self.name), lambda: gk.bar_image(max(q, bh), bh, self.th, self.S))
        self.cv.itemconfigure(lv["bar"], image=img)
        self.cv.coords(lv["bar"], x - g, y - g)
