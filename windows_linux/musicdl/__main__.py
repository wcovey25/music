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
