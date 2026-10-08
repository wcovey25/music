"""
views_advanced.py — Advanced mode: the tab bar (Format · Sound · Extras · Naming · Live · Activity), the rows behind the
first four tabs, and the Live tab (dashboard.py).
"""
from collections import deque

from ..audio import process
from ..config import DEFAULT_TEMPLATE, FORMATS, estimate_mb, size_kbps_of
from ..core.models import Track
from ..meta import naming
from . import fmt
from . import glass as gk
from .dashboard import Dashboard
from .rows import Group, Row
from .spectrum import HEIGHT as SPEC_H, MIN_CARD as SPEC_MIN, Spectrum

TABS = (("format", "Format"), ("sound", "Sound"), ("extras", "Extras"), ("naming", "Naming"), ("live", "Live"),
        ("activity", "Activity"))
STYLES = (("{title} - {artist}", "Title - Artist"), ("{artist} - {title}", "Artist - Title"),
          ("{artist}/{title}", "Artist / Title"), ("{artist}/{album}/{track} {title}", "Artist / Album / 01 Title"))
SAMPLE = Track("Skyfall", "Adele", album="Skyfall", year="2012", track_no=1, genre="Pop")


class AdvancedMixin:
    def _init_advanced(self):
        self.live = None
        self.spec = None
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
        w = min(bw - 56, 540)
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
        self.live = self.spec = None
        if self.tab == "live":
            self.draw_live()
        elif self.tab == "activity":
            self.act_area = area
            self.draw_act_switch(by + 16)
            self.cv.addtag_withtag("keep", "Bsw")
            self.draw_act_rows(reset=True)
        else:
            if self.tab == "format" and bh >= SPEC_MIN:             # the picture of what the format keeps sits above the rows
                self.spec = Spectrum(self, (bx + 28, by + 62, bw - 56, SPEC_H))
                self.spec.build()
                area = (area[0], p(by + 62 + SPEC_H + 8), area[2], area[3])
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
                    Row("switch", "Match the source", get=lambda: s.match_source, set=put("match_source"),
                        sub="Never write more than the source holds: no padded bitrate, fake lossless or fake hi-res"),
                    Row("info", "Estimated size", get=self.size_line),
                ]),
                Group("Encoder", [
                    Row("text", "Custom encoder flags", sub="Extra ffmpeg options, added after the codec settings",
                        get=lambda: s.encoder_flags, set=put("encoder_flags"), placeholder="None", width=250),
                ], note="Example: -compression_level 8 for smaller FLAC files. A wrong flag makes ffmpeg refuse the "
                        "file, and the song will show up under Needs attention."),
            ]
        if tab == "sound":
            return self.sound_groups(put)
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

    def sound_groups(self, put):
        """The Sound tab: finishing touches done while each song is saved (audio/process.py)."""
        s = self.s
        snap = lambda choices, value: min(choices, key=lambda c: abs(c[0] - value))[0]
        on = lambda: s.level or s.trim or s.fade != "off" or s.enhance != "off" or s.dynamics != "off"

        def preset(key):
            process.apply_profile(s, key)
            s.clamp()
            s.save()
        return [
            Group("Quick setup", [
                Row("popup", "Preset", sub="One tap sets everything below", choices=[(k, v[0]) for k, v in process.PROFILES.items()],
                    get=lambda: process.profile_of(s), set=preset, refresh=True, width=190, placeholder="Custom"),
            ]),
            Group("Volume", [
                Row("switch", "Even out the volume", sub="Every song is brought to the same loudness, so a playlist plays "
                    "at one level", get=lambda: s.level, set=put("level"), refresh=True),
                Row("segment", "Loudness", sub="How loud, measured the way streaming services do (LUFS)",
                    choices=[(v, lab) for v, lab in process.LEVELS], get=lambda: snap(process.LEVELS, s.level_target),
                    set=put("level_target"), refresh=True, min=78, show=lambda: s.level),
            ]),
            Group("Silence and fades", [
                Row("switch", "Trim silence", sub="Cut the dead air before a song starts and after it ends",
                    get=lambda: s.trim, set=put("trim"), refresh=True),
                Row("segment", "Sensitivity", sub="How quiet counts as silence",
                    choices=[(v, lab) for v, lab in process.TRIMS], get=lambda: snap(process.TRIMS, s.trim_db),
                    set=put("trim_db"), refresh=True, min=78, show=lambda: s.trim),
                Row("segment", "Fades", sub="A soft fade-in and fade-out on every song", choices=list(process.FADE_CHOICES),
                    get=lambda: s.fade, set=put("fade"), refresh=True, min=62),
            ]),
            Group("Sound", [
                Row("popup", "Enhance", sub="A gentle EQ shape", choices=list(process.ENHANCE_CHOICES),
                    get=lambda: s.enhance, set=put("enhance"), refresh=True, width=160),
                Row("segment", "Dynamics", sub="Evens out loud and quiet moments inside a song",
                    choices=list(process.DYNAMICS_CHOICES), get=lambda: s.dynamics, set=put("dynamics"), refresh=True,
                    min=62),
            ], note=lambda: ("These are applied while each song is saved, and a song is encoded afresh instead of "
                             "copied. Songs already in the folder are left as they are.") if on() else
                    "Nothing here is switched on: every song is saved exactly as it was downloaded."),
        ]

    # ---------------------------------------------------------------- live dashboard
    def draw_live(self):
        bx, by, bw, bh = self.rB
        self.live = Dashboard(self, (bx + 28, by + 68, bw - 56, bh - 68 - 16))
        self.live.build()

    def _plate(self, x, y, w, h, r):
        p = self.p
        pw, ph = p(w), p(h)
        img = self.cached(("plate", pw, ph, self.name), lambda: gk.panel_image(pw, ph, p(r), self.th, self.S))
        self.put(p(x), p(y), img, tags="Bcont")

    def tick_spec(self, now, dt):
        if self.spec:
            self.spec.tick(now, dt)

    def tick_live(self, now, dt, force=False):
        if self.live:
            self.live.tick(now, dt, force)
