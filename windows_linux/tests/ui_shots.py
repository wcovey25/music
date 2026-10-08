"""Screenshots of the window for a person (or a model) to look at — not a unit test.

    xvfb-run -a -s "-screen 0 1280x900x24" python ui_shots.py OUT_DIR [light|dark]

Starts the real App in a throw-away MUSICDL_HOME as on a first run, with an installed copy of the program in which one
file was changed (so the Protection sheet appears) and fake cleaners installed. It photographs the launch animation, the
first-run sheets, the Protection sheet they hold back, then clicks and scrolls through the main page (with the spectrum),
the Sound tab, the search suggestions, Activity with a close-match row and its chooser, the Live dashboard during a
made-up run, the artwork card tilted by the pointer, and Settings. Each step is saved as
OUT_DIR/NN_name.png (ImageMagick's `import` takes the picture); exceptions raised in the window go to OUT_DIR/errors.txt.
Needs a display (xvfb on Linux); never reaches the network.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import traceback

OUT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "shots")
THEME = sys.argv[2] if len(sys.argv) > 2 else "light"
W, H = 1100, 860
SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

os.makedirs(OUT, exist_ok=True)
home = tempfile.mkdtemp(prefix="ui_shots_")
app_dir = os.path.join(home, "app")
shutil.copytree(os.path.join(SRC, "musicdl"), os.path.join(app_dir, "musicdl"), ignore=shutil.ignore_patterns("__pycache__"))
for n in ("MusicDownloader.pyw", "setup_windows.bat", "requirements.txt"):
    if os.path.exists(os.path.join(SRC, n)):
        shutil.copy(os.path.join(SRC, n), app_dir)
pf = os.path.join(home, "Program Files")
for n in ("CCleaner", "Windows Defender", "Malwarebytes"):
    os.makedirs(os.path.join(pf, n))
os.environ.update(MUSICDL_HOME=home, MUSICDL_NO_PREWARM="1", MUSICDL_SHIELD_ROOT=app_dir, MUSICDL_APP_DIRS=pf)
sys.path.insert(0, SRC)

from musicdl.core import shield  # noqa: E402

shield.check(app_dir, os.path.join(home, "shield"))
with open(os.path.join(app_dir, "musicdl", "ui", "rows.py"), "a") as fh:
    fh.write("\n# changed by a cleaner\n")
with open(os.path.join(home, "settings.json"), "w") as fh:
    json.dump({"splash": True, "mode": "advanced", "outdir": os.path.join(home, "Music"), "volume": 0,
               "appearance": THEME, "clean_versions": True}, fh)

from musicdl.config import Settings  # noqa: E402
from musicdl.core import netio  # noqa: E402
from musicdl.core.models import Collection, Track  # noqa: E402
from musicdl.telemetry.stats import T  # noqa: E402
from musicdl.ingest import suggest  # noqa: E402
from musicdl.ui import app as appmod  # noqa: E402

suggest.warm = lambda: None
a = appmod.App(Settings.load())
a.root.geometry(f"{W}x{H}+0+0")
a.root.update()                                         # settle the size before the launch animation stages
errors = []
t = [2000]


def shot(name):
    a.root.update()
    subprocess.run(["import", "-window", "root", "-crop", f"{W}x{H}+0+0", os.path.join(OUT, name + ".png")])


def click(x, y):
    a.cv.event_generate("<Motion>", x=x, y=y)
    a.cv.event_generate("<ButtonPress-1>", x=x, y=y)
    a.cv.event_generate("<ButtonRelease-1>", x=x, y=y)


def scroll(off):
    for k in list(a.scrollers):
        a.scroll_to(k, off)


def at(fn, gap=900):
    def run():
        try:
            fn()
        except Exception:
            errors.append(traceback.format_exc())
    a.root.after(t[0], run)
    t[0] += gap


def suggestions():
    items = [suggest.Suggestion("song", "Hello", "Adele", "1"), suggest.Suggestion("album", "25", "Adele", "5", count=11),
             suggest.Suggestion("artist", "Adele", "", "3"),
             suggest.Suggestion("playlist", "Hello Hits", "Deezer", "9", count=80),
             suggest.Suggestion("genre", "Pop", "", "132")]
    a._suggest_show("adele hel", items)


def activity():
    a.close_popup()
    run = appmod.RunState(3, os.path.join(home, "Music"))
    a.run = run

    def opt(title, secs, ch, why, safe):
        return {"title": title, "seconds": secs, "channel": ch, "why": why, "safe": safe, "source": "youtube", "id": title,
                "url": "http://127.0.0.1:1/x"}
    rows = [dict(title="Harbour Lights", artist="Mara Quill", status="no-file", note="Not found exactly · 3 close options",
                 kbps=0, attention=True, thumb=None, path="", service="", art="", track=Track("Harbour Lights", "Mara Quill"),
                 close=[opt("Mara Quill - Harbour Lights (Extended Mix)", 330, "Mara Quill - Topic", ["1:30 longer"], True),
                        opt("Mara Quill - Harbour Lights (Live at the Roundhouse)", 250, "Mara Quill", ["Live version"], False),
                        opt("Harbour Lights", 241, "Dana Fell", ["Uploaded by Dana Fell"], False)]),
            dict(title="Hello", artist="Adele", status="ok", note="Trimmed 2.1 s of silence · Volume +3.2 dB", kbps=256,
                 attention=False, thumb=None, path="", service="youtube", art="", track=None, close=[]),
            dict(title="Clean Song", artist="Someone", status="ok", note="No clean version found", kbps=192,
                 attention=True, thumb=None, path="", service="youtube", art="", track=None, close=[])]
    for r in rows:
        run.rows.append(r)
        if r["attention"]:
            run.attn.append(r)
    click(577, 308)                                     # the Activity tab


def chooser():
    click(913, 492)                                     # Choose… on the Harbour Lights row


def sheet(key):
    if a.sheet_state:
        a._sheet_pick(key)


def live():
    run = a.run
    run.total, run.todo, run.planned, run.done, run.worked = 40, 40, True, 14, 14
    run.pace_songs = 4
    for i, st in enumerate(("Searching…", "Downloading 42%", "Encoding…", "Downloading 77%")):
        run.active[i] = dict(track=Track(f"Song {i}", "Artist"), stage=st, t0=0)
    T.reset()
    t0 = T._clock()
    for i in range(80):
        T.add_bytes(int(900_000 * (1.2 + 0.8 * ((i * 7) % 11) / 11)))
        T.sample(t0 + 0.5 * (i + 1))
        T.search()
        T.latency("api.deezer.com", 80 + (i * 13) % 90)
    for host, base in (("api.deezer.com", 90), ("www.youtube.com", 160), ("itunes.apple.com", 240), ("lrclib.net", 420)):
        for k in range(12):
            netio.STATS.get(host).observe(base + (k * 17) % 120)
    a.stage = "running"
    click(483, 308)                                     # the Live tab


def ready():
    a.stage = "idle"
    a.run = None
    a.col = Collection("Night Drive", [Track(f"Song {i}", "Mara Quill") for i in range(12)], "spotify", "playlist",
                       "Mara Quill")
    a.col_art = None
    a.set_stage("ready")


def tilt():
    x0, y0, x1, y1 = a.a_items["tile"].box
    a.cv.event_generate("<Motion>", x=int(x1 - 6), y=int(y0 + 6))


def finish():
    with open(os.path.join(OUT, "errors.txt"), "w") as fh:
        fh.write("\n".join(errors) or "none")
    a.alive = False
    a.root.destroy()


at(lambda: shot("00a_splash"), 700)                     # the launch animation: the icon forms, the bells, the name
at(lambda: shot("00b_splash"), 1100)
at(lambda: None, 1800)                                  # the window opens; the first-run sheets follow
at(lambda: shot("00c_welcome"))
at(lambda: sheet("go"), 500)
at(lambda: shot("00d_welcome_folder"))
at(lambda: sheet("go"), 500)
at(lambda: shot("00e_welcome_protect"))
at(lambda: sheet("go"), 1500)
at(lambda: shot("00f_welcome_done"))
at(lambda: sheet("ok"), 1200)                           # the start-up check found the changed file and waited
at(lambda: shot("01_protection_sheet"))
at(lambda: a.close_sheet() if a.sheet_state else None)
at(lambda: shot("02_main_format"))
at(lambda: click(203, 308), 1000)                       # the Sound tab
at(lambda: shot("03_sound_tab"))
at(lambda: scroll(10_000))
at(lambda: shot("04_sound_tab_end"))
at(suggestions, 1000)
at(lambda: shot("05_suggestions"))
at(activity, 1200)
at(lambda: shot("06_activity"))
at(chooser)
at(lambda: shot("07_close_match_chooser"))
at(lambda: a.close_popup())
at(live, 1500)
at(lambda: shot("07b_live"))
at(ready, 1200)
at(tilt, 800)
at(lambda: shot("07c_artwork_tilt"))
at(lambda: click(1047, 40), 1500)                       # the gear
at(lambda: shot("08_settings"))
at(lambda: scroll(560))
at(lambda: shot("09_settings_songs"))
at(lambda: scroll(900))
at(lambda: shot("09b_settings_experience"))
at(lambda: scroll(10_000))
at(lambda: shot("10_settings_protection"))
at(finish)
a.root.report_callback_exception = lambda *e: errors.append("".join(traceback.format_exception(*e)))
a.root.mainloop()
print(f"{len(errors)} error(s); pictures in {OUT}")
shutil.rmtree(home, ignore_errors=True)
