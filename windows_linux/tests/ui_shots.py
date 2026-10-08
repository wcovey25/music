"""Screenshots of the window for a person (or a model) to look at — not a unit test.

    xvfb-run -a -s "-screen 0 1280x900x24" python ui_shots.py OUT_DIR [light|dark]

Starts the real App in a throw-away MUSICDL_HOME, with an installed copy of the program in which one file was changed
(so the Protection sheet appears) and fake cleaners installed, then clicks and scrolls through the main page, the
Sound tab, the search suggestions, Activity with a close-match row and its chooser, and Settings. Each step is saved as
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
    json.dump({"splash": False, "mode": "advanced", "outdir": os.path.join(home, "Music"), "volume": 0,
               "appearance": THEME, "clean_versions": True}, fh)

from musicdl.config import Settings  # noqa: E402
from musicdl.core.models import Track  # noqa: E402
from musicdl.ingest import suggest  # noqa: E402
from musicdl.ui import app as appmod  # noqa: E402

suggest.warm = lambda: None
a = appmod.App(Settings.load())
a.root.geometry(f"{W}x{H}+0+0")
errors = []
t = [1500]


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


def finish():
    with open(os.path.join(OUT, "errors.txt"), "w") as fh:
        fh.write("\n".join(errors) or "none")
    a.alive = False
    a.root.destroy()


at(lambda: None, 3500)                                  # the start-up check (3.2 s) finds the changed file
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
at(lambda: (a.close_popup(), click(1047, 40)), 1500)    # the gear
at(lambda: shot("08_settings"))
at(lambda: scroll(560))
at(lambda: shot("09_settings_songs"))
at(lambda: scroll(10_000))
at(lambda: shot("10_settings_protection"))
at(finish)
a.root.report_callback_exception = lambda *e: errors.append("".join(traceback.format_exception(*e)))
a.root.mainloop()
print(f"{len(errors)} error(s); pictures in {OUT}")
shutil.rmtree(home, ignore_errors=True)
