"""fmt.py — small human-readable formatters for the interface."""


def plural(n, one, many=None):
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def duration(seconds):
    """'3 min', '2 h 5 min' — coarse on purpose (it's an estimate)."""
    s = int(round(seconds))
    if s < 60:
        return f"{max(s, 1)} sec" if s else "0 sec"
    m = round(s / 60)
    if m < 60:
        return f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min" if m else f"{h} h"


def duration_range(lo, hi):
    """A span of time in words that suit its size: '40–60 sec', '2–4 min', '1.4–1.9 h' ('about 3 min' when the two ends
    round to the same thing, 'under 2 min' when the soon end is nearly nothing, likewise for seconds)."""
    lo, hi = max(0.0, float(lo)), max(float(lo), float(hi))
    if hi < 90:
        a, b = int(round(lo / 5.0)) * 5, int(round(hi / 5.0)) * 5
        b = max(b, 5)
        if a == b:
            return f"about {b} sec"
        return f"under {b} sec" if a == 0 else f"{a}–{b} sec"
    if hi < 90 * 60:
        a, b = int(round(lo / 60.0)), int(round(hi / 60.0))
        if a == b:
            return f"about {b} min"
        return f"under {b} min" if a == 0 else f"{a}–{b} min"
    a, b = lo / 3600.0, hi / 3600.0
    if round(a, 1) == round(b, 1):
        return f"about {b:.1f} h"
    return f"{a:.1f}–{b:.1f} h"


def left(seconds):
    """ETA text: 'About 4 min left', 'Less than a minute left'."""
    if seconds is None:
        return "Estimating time…"
    if seconds < 45:
        return "Less than a minute left"
    return f"About {duration(seconds)} left"


def clock(seconds):
    s = int(max(0, seconds))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def size(mb):
    """File-size estimate: '85 MB', '1.4 GB'."""
    if mb < 1:
        return "< 1 MB"
    if mb < 1000:
        return f"{mb:,.0f} MB" if mb >= 10 else f"{mb:.1f} MB"
    gb = mb / 1000.0
    return f"{gb:.1f} GB" if gb < 100 else f"{gb:,.0f} GB"


def rate(bps):
    """Download speed from bytes/second."""
    if bps < 1024:
        return f"{bps:.0f} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.0f} KB/s" if bps >= 10240 else f"{bps / 1024:.1f} KB/s"
    mb = bps / 1048576
    return f"{mb:.1f} MB/s" if mb < 100 else f"{mb:.0f} MB/s"


def axis_rate(bps):
    """Axis label for a speed ceiling: '500 KB/s', '2 MB/s'."""
    if bps < 1024 * 1024:
        return f"{bps / 1024:.0f} KB/s"
    mb = bps / 1048576
    return f"{mb:.0f} MB/s" if abs(mb - round(mb)) < 0.05 else f"{mb:.1f} MB/s"


def ms(v):
    if v is None:
        return "—"
    return f"{v:.0f} ms" if v < 1000 else f"{v / 1000:.1f} s"


def quality_text(kbps):
    if not kbps:
        return ""
    return "Lossless" if kbps >= 1000 else f"{kbps} kbps"
