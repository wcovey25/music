"""
artwork.py — turn whatever picture a service hands out into cover art every player shows properly.

What a cover must be, to show up in Apple Music, Windows Explorer / Media Player and Android players alike:
a square, sRGB, 8-bit baseline JPEG (progressive or CMYK JPEGs are what players silently fail to draw), at a size
that is sharp on the device without bloating every file. Apple's own guidance for Apple Music artwork is "square, RGB,
at least 1400 px", so .m4a files (AAC/ALAC, the Apple formats) get 1400 px; everything else gets 1000 px.
Pictures are never enlarged: a 600 px original stays 600 px.
"""
import io
import re
from dataclasses import dataclass

from PIL import Image, ImageOps

try:
    from PIL import ImageCms
except ImportError:                                           # a Pillow build without littlecms: colours are not converted
    ImageCms = None

from .tags import extract_cover, image_size

MIN_SIDE = 200               # smaller than this is a thumbnail, not a cover
GOOD_SIDE = 600              # a picture at least this big is good enough: the chain stops looking for a bigger one
APPLE_SIDE = 1400            # .m4a (AAC / ALAC): Apple's minimum for Apple Music artwork, sharp on a Retina display
STANDARD_SIDE = 1000         # MP3 / FLAC / WAV: sharp on a phone or PC screen, about 250 KB
MAX_SIDE = APPLE_SIDE
NEAR = 0.95                  # a picture within 5% of the wanted size is stretched to it (never more: no blurry enlargements)
JPEG_QUALITY = 90
THUMB_SIDE = 96

_BAR = 22                    # a line this dark (0-255) is a black bar
_SRGB = None


def cover_side(ext):
    """Edge length, in pixels, the artwork is made at for a file type ('.m4a' → Apple's size)."""
    return APPLE_SIDE if str(ext or "").lower() in (".m4a", ".mp4", ".m4b") else STANDARD_SIDE


@dataclass
class Prepared:
    jpeg: bytes
    thumb: bytes
    side: int                # edge of the finished picture
    source_side: int         # edge of what we were given (after trimming bars): how good the original was

    def __iter__(self):                                       # (jpeg, thumb): what older callers unpack
        return iter((self.jpeg, self.thumb))

    def __getitem__(self, i):
        return (self.jpeg, self.thumb)[i]


def sized(url, side):
    """Ask an image CDN for the picture at `side` px where the URL says how (Apple, Deezer, Pandora). Others are as given."""
    if not url:
        return url
    u = url.replace("{w}x{h}bb", f"{side}x{side}bb").replace("{w}x{h}", f"{side}x{side}") \
        .replace("{w}", str(side)).replace("{h}", str(side)).replace("{f}", "jpg")
    if "mzstatic.com" in u:
        u = re.sub(r"/\d+x\d+(bb|sr|cc)?(-\d+)?\.(jpg|jpeg|png|webp)$", rf"/{side}x{side}bb.jpg", u)
    elif "dzcdn.net" in u:
        u = re.sub(r"/\d+x\d+-", f"/{side}x{side}-", u, count=1)
    elif "pandora" in u or "pdora" in u:
        u = re.sub(r"_\d+W_\d+H", f"_{side}W_{side}H", u)
    return u


# ---------------------------------------------------------------- reading and normalising

def _srgb():
    global _SRGB
    if _SRGB is None and ImageCms is not None:
        _SRGB = ImageCms.createProfile("sRGB")
    return _SRGB


def _flatten(im):
    """Transparent pixels become white (a cover has no 'see-through')."""
    rgba = im.convert("RGBA")
    bg = Image.new("RGB", rgba.size, (255, 255, 255))
    bg.paste(rgba, mask=rgba.getchannel("A"))
    return bg


def _to_srgb(im):
    """8-bit RGB in the sRGB colour space (Display P3 / Adobe RGB / CMYK pictures are converted, not just relabelled)."""
    icc = im.info.get("icc_profile")
    has_alpha = im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info)
    if has_alpha:
        im = _flatten(im)
    if icc and ImageCms is not None and im.mode in ("RGB", "CMYK", "L"):
        try:
            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            return ImageCms.profileToProfile(im, src, _srgb(), outputMode="RGB")
        except Exception:
            pass
    return im.convert("RGB")


def _dark_run(im, vertical):
    """How many lines at the start / end are black bars: (before, after)."""
    w, h = im.size
    n = h if vertical else w

    def dark(i):
        box = (0, i, w, i + 1) if vertical else (i, 0, i + 1, h)
        return im.crop(box).getextrema()[1] <= _BAR
    before = 0
    while before < n and dark(before):
        before += 1
    if before == n:
        return 0, 0
    after = 0
    while after < n - before and dark(n - 1 - after):
        after += 1
    return before, after


def trim_bars(im):
    """Video frames come with black bars: a 16:9 thumbnail holds 4:3 art with bars above and below, and 'Topic' videos
    show the square album cover in the middle of a black 16:9 frame. Cut them away, but only when the picture clearly
    has them on both sides and most of it remains."""
    w, h = im.size
    k = max(1, max(w, h) // 360)                              # look at a small copy: bars are hundreds of pixels wide
    g = im.convert("L").reduce(k) if k > 1 else im.convert("L")
    left, right, top, bottom = 0, 0, 0, 0
    tb, ta = _dark_run(g, True)
    lb, la = _dark_run(g, False)
    gw, gh = g.size
    if tb and ta and abs(tb - ta) <= 0.15 * gh and (tb + ta) >= 0.04 * gh:
        top, bottom = tb * k, ta * k
    if lb and la and abs(lb - la) <= 0.15 * gw and (lb + la) >= 0.04 * gw:
        left, right = lb * k, la * k
    if not (left or right or top or bottom):
        return im
    box = (left, top, w - right, h - bottom)
    if (box[2] - box[0]) < 0.45 * w or (box[3] - box[1]) < 0.45 * h:
        return im                                             # nearly everything is black: not bars, a dark picture
    return im.crop(box)


def _center_square(im):
    s = min(im.size)
    x, y = (im.width - s) // 2, (im.height - s) // 2
    return im.crop((x, y, x + s, y + s))


def _is_plain_jpeg(data, im, side):
    """Already what we would make (baseline sRGB JPEG, square, not over-sized, nothing odd attached): keep the bytes, so a
    good picture is not compressed a second time."""
    return (im.format == "JPEG" and im.mode == "RGB" and im.width == im.height and im.width <= side
            and not im.info.get("progressive") and not im.info.get("progression") and not im.info.get("icc_profile")
            and "exif" not in im.info and data[:3] == b"\xff\xd8\xff")


def prepare(data, square=False, side=STANDARD_SIDE):
    """Check image bytes and make the cover. `square=True` means the picture is a video frame / thumbnail (black bars are
    trimmed first). Returns Prepared, or None when it is unusable (broken, or smaller than MIN_SIDE)."""
    if not data or len(data) < 4000:
        return None
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            im = ImageOps.exif_transpose(opened)
            plain = _is_plain_jpeg(data, opened, side) and not square
            im = _to_srgb(im.copy() if im is opened else im)
        if square:
            im = trim_bars(im)
        if im.width != im.height:
            im = _center_square(im)
            plain = False
        source_side = im.width
        if source_side < MIN_SIDE:
            return None
        if source_side > side or source_side >= side * NEAR:       # (a 1398 px picture is made the 1400 the spec asks for)
            if source_side != side:
                im = im.resize((side, side), Image.LANCZOS)
                plain = False
        if plain:
            jpeg = data
        else:
            out = io.BytesIO()
            im.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=False)
            jpeg = out.getvalue()
        th = im.resize((THUMB_SIDE, THUMB_SIDE), Image.LANCZOS)
        tout = io.BytesIO()
        th.save(tout, "JPEG", quality=85)
        return Prepared(jpeg, tout.getvalue(), im.width, source_side)
    except Exception:
        return None


def prepare_cover(data, square=False, side=STANDARD_SIDE):
    """prepare() as a (jpeg_bytes, 96px_thumb_jpeg) pair, or None."""
    got = prepare(data, square, side)
    return (got.jpeg, got.thumb) if got else None


def thumb_of_bytes(data, size=96):
    """Small square JPEG from any image bytes (None if unreadable)."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = _center_square(_to_srgb(ImageOps.exif_transpose(im))).resize((size, size), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return None


def thumb_of_file(path):
    """Thumbnail of a file's embedded artwork (for the results list)."""
    data = extract_cover(path)
    return thumb_of_bytes(data) if data else None
