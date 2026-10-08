"""Entry point:  py -3 -m musicdl   (or double-click MusicDownloader.pyw)."""
import logging
import os
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import platform_
    if "--no-relaunch" in argv:
        argv.remove("--no-relaunch")
    elif platform_.relaunch_without_console(os.path.abspath(sys.argv[0]), argv):
        return 0
    logging.basicConfig(filename=platform_.log_path(), level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    # Let the window's thread get the interpreter back after 1 ms rather than 5 ms when download threads are busy
    # (measured on the macOS edition against six threads that only compute: a UI timer that was 13 ms late was 2 ms late,
    # and those threads got ~10% less done; the app's own threads mostly wait for the network and ffmpeg)
    sys.setswitchinterval(0.001)
    try:
        from .config import Settings
        from .ui.app import App
        app = App(Settings.load())
        app.mainloop()
    except Exception:
        logging.exception("fatal")
        platform_.message_box("Music Downloader", "Something went wrong starting the app.\n"
                              f"Details are in {platform_.log_path()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
