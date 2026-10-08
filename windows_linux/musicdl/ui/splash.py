"""splash.py — the launch animation: the icon eases in with a soft halo, the name fades up, then the window opens."""
from PIL import Image

from . import glass as gk
from .glass import hx, mix


class SplashMixin:
    def run_splash(self, after):
        cv, p, th = self.cv, self.p, self.th
        self.splashing = True
        scene = gk.make_background(self.W, self.H, th, self.S)
        self.scene = scene
        cv.delete("all")
        cv.create_image(0, 0, anchor="nw", image=self.photo("scene", scene), tags="scene")
        size = p(120)
        base = gk.app_icon(size)
        halo = gk.glow_ring(int(size * 1.9), th["accent"], 0.9)
        cx, cy = self.W // 2, int(self.H * 0.44)
        item = cv.create_image(cx, cy, image="", tags="splash")
        title = cv.create_text(cx, cy + size // 2 + p(34), text="Music Downloader", font=self.f_title,
                               fill=hx(self.bg_at(cx, cy)), tags="splash")
        bg = self.bg_at(cx, cy + size)
        self.cue("startup")

        def frame(f):
            k = gk.ease_out(min(1.0, f / 0.55))
            s = 0.86 + 0.14 * k
            w = max(8, int(size * s))
            canvas = Image.new("RGBA", halo.size, (0, 0, 0, 0))
            ring = halo.resize((int(halo.width * (0.8 + 0.5 * f)),) * 2, Image.BILINEAR)
            ring.putalpha(ring.getchannel("A").point(lambda v: int(v * (1 - f) * min(1.0, f * 4))))
            canvas.alpha_composite(ring, ((canvas.width - ring.width) // 2, (canvas.height - ring.height) // 2))
            icon = base.resize((w, w), Image.LANCZOS)
            icon.putalpha(icon.getchannel("A").point(lambda v: int(v * k)))
            canvas.alpha_composite(icon, ((canvas.width - w) // 2, (canvas.height - w) // 2))
            cv.itemconfigure(item, image=self.photo("splash", canvas))
            t = gk.ease(max(0.0, (f - 0.35) / 0.4))
            cv.itemconfigure(title, fill=hx(mix(bg, th["fg"], t)))

        def done():
            cv.delete("splash")
            after()
        self.animate("splash", 1.7, frame, lambda t: t, done=done)
