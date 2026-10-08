"""Interface pieces that can be checked without a person looking: the Live dashboard's layout and pictures, the
spectrum, the tilting artwork card, the icon and its .ico, the painter thread, the launch animation's frames, the
Motion setting and the first-run sheets. The ones that need Tk are skipped when there is no display."""
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

import helpers  # noqa: F401  (a throw-away MUSICDL_HOME)
from PIL import Image

from musicdl import platform_
from musicdl.config import Settings
from musicdl.ui import card3d, dashboard, icon, spectrum, splash, viz, welcome
from musicdl.ui import glass as gk
from musicdl.ui import shell as shellmod
from musicdl.ui.painter import Painter

THEMES = [gk.THEMES[k] for k in sorted(gk.THEMES)]


def _tk():
    try:
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        return root
    except Exception:
        return None


class VizTests(unittest.TestCase):
    def test_ribbon_geometry_inside(self):
        for w, h in ((120, 60), (400, 200), (1600, 500)):
            dx, dy, x0, x1, yt, yb = viz.ribbon_geometry(w, h)
            self.assertTrue(0 <= x0 < x1 <= w)
            self.assertTrue(0 <= yt < yb <= h)
            self.assertEqual(x1 + dx, w)
            levels = viz.ribbon_levels(w, h)
            self.assertEqual((levels[0], levels[-1]), (yb, yt))

    def test_pillar_layout(self):
        self.assertEqual(viz.pillar_layout(300, 200, [])["pillars"], [])
        for sizes in ([4], [8], [2, 6], [16], [1, 1, 1]):
            for w in (160, 420, 900):
                lay = viz.pillar_layout(w, 220, sizes)
                self.assertEqual(len(lay["pillars"]), sum(sizes))
                self.assertEqual(len(lay["spans"]), len(sizes))
                for _g, _i, x, pw in lay["pillars"]:
                    self.assertGreaterEqual(x, 0)
                    self.assertLessEqual(x + pw + lay["dx"], w)
                xs = [x for _g, _i, x, _pw in lay["pillars"]]
                self.assertEqual(xs, sorted(xs))

    def test_capsule_cells_fill(self):
        for w in (40, 97, 300):
            for n in (1, 3, 8, 16):
                cells = viz.capsule_cells(w, n)
                self.assertEqual(len(cells), n)
                widths = [cw for _x, cw in cells]
                self.assertLessEqual(max(widths) - min(widths), 1)
                self.assertLessEqual(cells[-1][0] + cells[-1][1], w)
                self.assertEqual([x for x, _ in cells], sorted(x for x, _ in cells))

    def test_log_x(self):
        self.assertEqual(viz.log_x(1, 300), 0)
        self.assertAlmostEqual(viz.log_x(99999, 300), 300)
        self.assertAlmostEqual(viz.log_x(400, 300) - viz.log_x(40, 300), viz.log_x(4000, 300) - viz.log_x(400, 300))

    def test_pictures_have_the_asked_size(self):
        for th in THEMES:
            samples = [1e6 * (1 + (i % 7) / 7) for i in range(90)]
            self.assertEqual(viz.ribbon_chart(320, 140, samples, 3e6, th, 1.0).size, (320, 140))
            self.assertEqual(viz.core_pillars(200, 120, [("", [0.1, 0.5, 0.9, 0.0])], th, 1.0).size, (200, 120))
            self.assertEqual(viz.slot_capsule(180, 14, ["find", "get", None, None], th, 1.0).size, (180, 14))
            self.assertEqual(viz.range_bars(240, 72, [(90, 300), None, (400, 1800)], 24, th, 1.0).size, (240, 72))


class DashboardTests(unittest.TestCase):
    def test_plan_tiers_and_bounds(self):
        for (w, h), tier in (((600, 380), "compact"), ((900, 560), "regular"), ((1400, 700), "wide")):
            out = dashboard.plan(w, h)
            self.assertEqual(out["tier"], tier)
            rects = list(out["kpi"]) + [out[k] for k in ("chart", "system", "pipeline", "servers", "latency") if out[k]]
            for x, y, rw, rh in rects:
                self.assertTrue(0 <= x and x + rw <= w and 0 <= y and y + rh <= h, (tier, (x, y, rw, rh)))

    def test_stage_group_and_cells(self):
        self.assertEqual(dashboard.stage_group("Downloading 42%"), "get")
        self.assertEqual(dashboard.stage_group("Encoding…"), "make")
        self.assertEqual(dashboard.stage_group("Adding details"), "tag")
        self.assertEqual(dashboard.stage_group("Searching…"), "find")
        self.assertEqual(dashboard.slot_cells(["Encoding", "Searching"], 3), ["find", "make", None, None])

    def test_host_name(self):
        self.assertEqual(dashboard.host_name("www.youtube.com"), "youtube.com")
        self.assertEqual(dashboard.host_name("rr3---sn-4g5e6nsz.googlevideo.com"), "googlevideo.com")
        self.assertEqual(dashboard.host_name("api.deezer.com"), "api.deezer.com")

    def test_kpi_texts(self):
        self.assertEqual(dashboard.kpi_texts(None, False, None, 0, 0, 0, 0, None, None, 0)[0], ("—", ""))
        from musicdl.ui.app import RunState
        r = RunState(40, tempfile.gettempdir())
        r.todo, r.planned, r.done, r.worked = 40, True, 14, 14
        r.counts = {"ok": 12, "skipped": 2}
        eta, songs, speed, api = dashboard.kpi_texts(r, True, None, 30, 2.5e6, 2e6, 3e6, 60.0, (120, 400), 1)
        self.assertEqual(eta, ("…", "estimating"))
        self.assertEqual(songs[0], "14 / 40")
        self.assertIn("12 saved", songs[1])
        self.assertIn("2 already there", songs[1])
        self.assertIn("1 failed", api[1])
        self.assertNotEqual(speed[0], "—")


class SpectrumTests(unittest.TestCase):
    def test_lossy_cut_offs(self):
        self.assertEqual(spectrum.target("mp3", 128, 0, 16), (16000.0, 96.0))
        self.assertEqual(spectrum.target("mp3", 320, 0, 16)[0], 20500.0)
        self.assertEqual(spectrum.target("aac", 256, 0, 16)[0], 21000.0)
        self.assertEqual(spectrum.target("mp3", 130, 0, 16)[0], 16000.0)          # the nearest bitrate in the table

    def test_lossless_full_band(self):
        hz, rng = spectrum.target("flac", 0, 0, 16)
        self.assertAlmostEqual(hz, 44100 / 2 * 0.995)
        self.assertEqual(rng, 96.0)
        self.assertEqual(spectrum.target("flac", 0, 96000, 24), (96000 / 2 * 0.995, 144.0))
        self.assertLess(spectrum.target("aac", 320, 32000, 16)[0], 16000)         # never past the sample rate

    def test_settings_target(self):
        s = Settings(mode="advanced", fmt="mp3", bitrate=128)
        self.assertEqual(spectrum.settings_target(s)[0], 16000.0)
        s = Settings(mode="advanced", fmt="wav", sample_rate=96000, bit_depth=24)
        self.assertEqual(spectrum.settings_target(s), (96000 / 2 * 0.995, 144.0))

    def test_scale_round_trip(self):
        for hz in (0, 100, 1000, 16000, 48000):
            self.assertAlmostEqual(spectrum.to_hz(spectrum.to_x(hz)), hz, places=6)
        self.assertEqual(spectrum.to_x(1e9), 1.0)

    def test_image_size(self):
        for th in THEMES:
            img = spectrum.image(300, 90, (spectrum.to_x(16000), 96.0), th, t=0.4)
            self.assertEqual(img.size, (300, 90))


class CardTests(unittest.TestCase):
    def test_flat_corners_are_the_square(self):
        self.assertEqual(card3d.corners(100, 0, 0), [(-50, -50), (50, -50), (50, 50), (-50, 50)])

    def test_coefficients_identity(self):
        sq = [(0, 0), (100, 0), (100, 100), (0, 100)]
        c = card3d.coefficients(sq, sq)
        for got, want in zip(c, (1, 0, 0, 0, 1, 0, 0, 0)):
            self.assertAlmostEqual(got, want, places=9)

    def test_tilt_moves_the_near_edge_out(self):
        tl, tr, br, bl = card3d.corners(100, 1.0, 0.0)
        self.assertGreater(abs(tr[1]), abs(tl[1]))                               # the right edge is nearer: taller

    def test_quantize(self):
        self.assertEqual(card3d.quantize(0.3), 0.25)
        self.assertEqual(card3d.quantize(5), 1.0)
        self.assertEqual(card3d.quantize(-0.9), -1.0)

    def test_card_pictures(self):
        tile = Image.new("RGBA", (80, 80), (200, 40, 40, 255))
        card = card3d.Card(tile, THEMES[0], 1.0)
        full = 80 + 2 * card3d.margin(80)
        self.assertEqual(card.image(0.4, -0.2).size, (full, full))
        self.assertIs(card.image(0.4, -0.2), card.image(0.45, -0.24))            # one picture per quantized tilt

    def test_tilt_follows_the_pointer(self):
        root = _tk()
        if root is None:
            self.skipTest("no display")
        try:
            class App:
                th, S, D = THEMES[0], 1.0, 1
                motion = False

                def __init__(self):
                    self.cv = __import__("tkinter").Canvas(root)
                    self.regions = []
                    self.anims = {}

                def stop_anim(self, k):
                    self.anims.pop(k, None)

                def put(self, x, y, ph, anchor, tags):
                    return self.cv.create_image(x, y, image=ph, anchor=anchor, tags=tags)

                def tkphoto(self, img, scale=None):
                    from PIL import ImageTk
                    return ImageTk.PhotoImage(img, master=root)

                def region(self, box, **kw):
                    self.regions.append((box, kw))

                def reduced(self):
                    return self.motion

                def animate(self, key, dur, fn, **kw):
                    fn(1.0)

            a = App()
            t = card3d.Tilt(a, Image.new("RGBA", (60, 60), (0, 0, 255, 255)), 10, 10)
            self.assertEqual(t.box, (10, 10, 70, 70))
            box, kw = a.regions[0]
            ev = mock.Mock(x=70, y=40)
            kw["track"](ev)
            self.assertEqual(t.shown, (1.0, 0.0))
            kw["hover"](False)
            self.assertEqual(t.shown, card3d.REST)
            a.motion = True
            kw["track"](ev)
            self.assertEqual(t.shown, card3d.REST)                                   # Reduced motion: it stays put
        finally:
            root.destroy()


class IconTests(unittest.TestCase):
    def test_sizes(self):
        for n in (16, 48, 256):
            img = icon.app_icon(n)
            self.assertEqual((img.size, img.mode), ((n, n), "RGBA"))
        self.assertEqual(gk.app_icon(32).size, (32, 32))

    def test_corners_are_clear(self):
        img = icon.app_icon(64)
        self.assertEqual(img.getpixel((0, 0))[3], 0)
        self.assertEqual(img.getpixel((32, 32))[3], 255)

    def test_ico_holds_every_size(self):
        path = icon.ico_file()
        self.assertTrue(path and path.startswith(platform_.cache_dir()))
        with Image.open(path) as ico:
            self.assertEqual(set(ico.info["sizes"]), {(n, n) for n in icon.ICO_SIZES})
        self.assertEqual(icon.ico_file(), path)                                  # written once per drawing

    def test_old_ico_removed(self):
        folder = os.path.join(platform_.cache_dir(), "icons")
        os.makedirs(folder, exist_ok=True)
        stale = os.path.join(folder, "app-0000.ico")
        with open(stale, "wb") as fh:
            fh.write(b"x")
        with mock.patch.object(icon, "_fingerprint", return_value="fresh-test"):
            path = icon.ico_file()
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(path.endswith("app-fresh-test.ico"))
        os.remove(path)


class PainterTests(unittest.TestCase):
    def _wait(self, p):
        t = time.time() + 5
        while not p.idle() and time.time() < t:
            time.sleep(0.01)

    def test_newest_only_and_token(self):
        p = Painter()
        gate = threading.Event()
        p.submit("a", 1, lambda: gate.wait(2) and "first")
        time.sleep(0.05)
        p.submit("b", 1, lambda: "old")
        p.submit("b", 2, lambda: "new")                                          # replaces the one not drawn yet
        gate.set()
        self._wait(p)
        self.assertEqual(p.take("b", 2), "new")
        self.assertIsNone(p.take("b", 2))                                        # once
        self.assertGreaterEqual(p.skipped, 1)
        self.assertIsNone(p.take("a", 99))                                       # another token's picture

    def test_error_does_not_kill_it(self):
        p = Painter()
        p.submit("x", 1, lambda: 1 / 0)
        self._wait(p)
        p.submit("y", 1, lambda: "ok")
        self._wait(p)
        self.assertEqual(p.take("y", 1), "ok")


class SplashTests(unittest.TestCase):
    def _stage(self, calm):
        th = THEMES[0]
        scene = Image.new("RGB", (400, 360), (230, 230, 240))
        return splash.Stage((320, 280), (40, 40), scene, th, icon.app_icon(96), calm=calm)

    def test_frames(self):
        for calm in (False, True):
            st = self._stage(calm)
            for t in (0.0, 0.4, 1.2, 2.5):
                img = st.render(t)
                self.assertEqual(img.size, (320, 280))
            self.assertEqual(st.title_amount(0.0), 0.0)
            self.assertEqual(st.title_amount(10.0), 1.0)

    def test_calm_hands_over_sooner(self):
        self.assertLess(splash.CALM["leave"] + splash.CALM["fly"], splash.LEAVE + splash.FLY)

    def test_matches_the_sound(self):
        from musicdl.audio import synth
        self.assertTrue(synth.TIMELINE)


class MotionTests(unittest.TestCase):
    def test_config_clamp_and_round_trip(self):
        s = Settings(motion="sideways")
        s.clamp()
        self.assertEqual(s.motion, "full")
        s = Settings(motion="reduced", welcomed=True)
        s.save()
        back = Settings.load()
        self.assertEqual((back.motion, back.welcomed), ("reduced", True))
        os.remove(Settings.path())
        fresh = Settings.load()
        self.assertEqual((fresh.motion, fresh.welcomed), ("full", False))

    def test_reduced(self):
        sh = shellmod.Shell.__new__(shellmod.Shell)
        sh.s = Settings()
        sh._rm_at, sh._rm = 0.0, False
        with mock.patch.object(platform_, "reduce_motion", return_value=False) as rm:
            self.assertFalse(sh.reduced())
            sh.reduced()
            self.assertEqual(rm.call_count, 1)                                   # asked once a minute at most
        sh.s.motion = "reduced"
        self.assertTrue(sh.reduced())
        sh.s.motion = "full"
        sh._rm_at = 0.0
        with mock.patch.object(platform_, "reduce_motion", return_value=True):
            self.assertTrue(sh.reduced())                                        # Windows' own switch


class FakeWelcome(welcome.WelcomeMixin):
    def __init__(self, root_dir):
        self.s = Settings(outdir=os.path.join(root_dir, "Music"))
        self.sheet_state = None
        self.splashing = False
        self.shield_report = None
        self.shield_noted = False
        self.sheets, self.locked, self.started, self.shown = [], [], 0, []
        self._root_dir = root_dir
        self._init_welcome()

    def open_sheet(self, title, paras, buttons, pick, default=None, follow=False):
        self.sheets.append((title, [k for k, *_ in buttons]))
        self._pick = pick

    def press(self, key):
        self._pick(key)

    def shield_root(self):
        return self._root_dir

    def cleaner_note(self):
        return "CCleaner is installed."

    def set_shield_lock(self, on):
        self.locked.append(on)

    def start_shield(self):
        self.started += 1

    def shield_sheet(self, rep):
        self.shown.append(rep)


class WelcomeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="welcome_")

    def test_whole_walk_through(self):
        w = FakeWelcome(self.dir)
        w.maybe_welcome()
        w.press("go")
        self.assertEqual(w.sheets[-1][0], "Where your songs go")
        w.press("go")
        self.assertEqual(w.sheets[-1][0], "Protect the app")
        w.press("go")
        self.assertEqual((w.locked, w.started), ([True], 1))
        self.assertEqual(w.sheets[-1][0], "You’re set")
        w.press("ok")
        self.assertTrue(w.s.welcomed)
        n = len(w.sheets)
        w.maybe_welcome()
        self.assertEqual(len(w.sheets), n)                                        # never again

    def test_not_now_ends_it(self):
        w = FakeWelcome(self.dir)
        w.maybe_welcome()
        w.press("skip")
        self.assertTrue(w.s.welcomed)
        self.assertEqual((len(w.sheets), w.locked), (1, []))

    def test_skip_protection(self):
        w = FakeWelcome(self.dir)
        w.maybe_welcome()
        w.press("go")
        w.press("go")
        w.press("skip")
        self.assertTrue(w.s.welcomed)
        self.assertEqual(w.locked, [])

    def test_waits_for_splash_and_other_sheets(self):
        w = FakeWelcome(self.dir)
        w.splashing = True
        w.maybe_welcome()
        w.splashing, w.sheet_state = False, "open"
        w.maybe_welcome()
        self.assertEqual(w.sheets, [])

    def test_held_shield_sheet_shown_after(self):
        w = FakeWelcome(self.dir)
        w.shield_report = mock.Mock()
        w.shield_report.state = "attention"
        w.maybe_welcome()
        w.press("skip")
        self.assertEqual(w.shown, [w.shield_report])
        self.assertTrue(w.shield_noted)

    def test_blocked_folder(self):
        w = FakeWelcome(self.dir)
        w.maybe_welcome()
        w.press("go")
        with mock.patch.object(welcome, "can_write", return_value="denied"):
            w.press("go")
        self.assertEqual(w.sheets[-1][0], "Windows blocked that folder")
        w.press("later")
        self.assertEqual(w.sheets[-1][0], "Protect the app")

    def test_choose_folder(self):
        w = FakeWelcome(self.dir)
        w.root = None
        w.maybe_welcome()
        w.press("go")
        new = os.path.join(self.dir, "Elsewhere")
        with mock.patch.object(welcome.filedialog, "askdirectory", return_value=new):
            w.press("change")
        self.assertEqual(w.s.outdir, os.path.normpath(new))
        self.assertEqual(w.sheets[-1][0], "Where your songs go")

    def test_can_write(self):
        self.assertEqual(welcome.can_write(self.dir), "ok")
        self.assertEqual(welcome.can_write(os.path.join(self.dir, "not", "yet")), "ok")    # made later, under this one
        self.assertEqual(os.listdir(self.dir), [])                                         # the probe is gone
        with mock.patch.object(welcome.tempfile, "NamedTemporaryFile", side_effect=PermissionError):
            self.assertEqual(welcome.can_write(self.dir), "denied")

    def test_short(self):
        home = os.path.expanduser("~")
        self.assertEqual(welcome.short(os.path.join(home, "Music")), os.path.join("~", "Music"))
        self.assertEqual(len(welcome.short("/x" * 60)), 44)


class ShieldGateTests(unittest.TestCase):
    def test_sheet_waits_for_welcome(self):
        from musicdl.ui.protect import ProtectMixin

        class P(ProtectMixin):
            def __init__(self, welcomed):
                self.s = Settings(welcomed=welcomed)
                self.shield_noted, self.sheet_state, self.splashing = False, None, False
                self.shown = []

            def shield_sheet(self, rep):
                self.shown.append(rep)

            def _settings_refresh(self):
                pass

        rep = mock.Mock(state="attention", restored=[], changed=["a"], lost=[], runtime=[])
        for welcomed, want in ((False, []), (True, [rep])):
            p = P(welcomed)
            p.on_shield(rep)
            self.assertEqual(p.shown, want)


class FrameLoopTests(unittest.TestCase):
    def _shell(self, root):
        from musicdl.ui import shell as shellmod
        sh = shellmod.Shell.__new__(shellmod.Shell)
        sh.root, sh.alive, sh.anims, sh._last, sh.busy = root, True, {}, 0.0, False
        sh.on_tick = lambda now, dt: None
        sh._loop_job = None
        return sh

    def test_a_failing_animation_step_does_not_stop_the_loop(self):
        root = _tk()
        if root is None:
            self.skipTest("no display")
        try:
            sh = self._shell(root)
            ran = []
            sh.anims["bad"] = dict(t0=time.monotonic(), dur=10.0, update=lambda v: 1 / 0, ease=lambda f: f, done=None)
            sh.anims["good"] = dict(t0=time.monotonic(), dur=10.0, update=lambda v: ran.append(v), ease=lambda f: f, done=None)
            sh._loop()
            self.assertNotIn("bad", sh.anims)                                 # dropped, not kept failing every frame
            self.assertIn("good", sh.anims)
            self.assertIsNotNone(sh._loop_job)                                # the next frame is still scheduled
            self.assertTrue(ran)
        finally:
            root.destroy()

    def test_a_failing_finish_callback_does_not_stop_the_loop(self):
        root = _tk()
        if root is None:
            self.skipTest("no display")
        try:
            sh = self._shell(root)

            def boom():
                raise RuntimeError("finish")
            sh.anims["done"] = dict(t0=time.monotonic() - 1, dur=0.001, update=lambda v: None, ease=lambda f: f, done=boom)
            sh._loop()
            self.assertIsNotNone(sh._loop_job)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
