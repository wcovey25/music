#!/usr/bin/env bash
# Music Downloader — Linux launcher.
#
#   ./run.sh                start the app (first run sets itself up, about a minute, needs internet)
#   ./run.sh --foreground   keep it attached to this terminal (shows errors; Ctrl+C quits)
#   ./run.sh --update       update the downloader engine (yt-dlp) and the other packages, then start
#   ./run.sh --shortcut     add "Music Downloader" to your applications menu (no terminal window)
#   ./run.sh --no-shortcut  take it out again
#
# If the file is not executable (e.g. it came out of a zip):  bash run.sh
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"
STAMP="$VENV/.requirements-stamp"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}"
DESKTOP_FILE="$APPS/applications/music-downloader.desktop"
ICON_FILE="$APPS/icons/hicolor/256x256/apps/music-downloader.png"

foreground=0 update=0 shortcut=0 unshortcut=0
for arg in "$@"; do
  case "$arg" in
    --foreground) foreground=1 ;;
    --update) update=1 ;;
    --shortcut) shortcut=1 ;;
    --no-shortcut) unshortcut=1 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# A message that is visible whether we were started from a terminal or from the applications menu.
say() {
  echo "$1" >&2
  if [ ! -t 2 ]; then
    if command -v zenity >/dev/null 2>&1; then zenity --error --title "Music Downloader" --text "$1" 2>/dev/null
    elif command -v kdialog >/dev/null 2>&1; then kdialog --error "$1" --title "Music Downloader" 2>/dev/null
    elif command -v notify-send >/dev/null 2>&1; then notify-send "Music Downloader" "$1"
    fi
  fi
}
die() { say "$1"; exit 1; }

if [ "$unshortcut" = 1 ]; then
  rm -f "$DESKTOP_FILE" "$ICON_FILE"
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS/applications" 2>/dev/null
  echo "Removed Music Downloader from the applications menu."
  exit 0
fi

# ---------------------------------------------------------------- a suitable Python (3.9 or newer, with Tk)
find_python() {
  local c
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      echo "$c"
      return 0
    fi
  done
  return 1
}

if [ ! -x "$VENV/bin/python" ]; then
  PY="$(find_python)" || die "Music Downloader needs Python 3.9 or newer, and none was found.
Install it with your package manager, e.g.:
  sudo apt install python3 python3-tk python3-venv      (Debian, Ubuntu, Mint)
  sudo dnf install python3 python3-tkinter              (Fedora)
  sudo pacman -S python tk                              (Arch)"
  "$PY" -c 'import tkinter' 2>/dev/null || die "Python was found, but it has no Tk (the toolkit the window is drawn with).
Install it with your package manager, then run this again:
  sudo apt install python3-tk          (Debian, Ubuntu, Mint)
  sudo dnf install python3-tkinter     (Fedora)
  sudo pacman -S tk                    (Arch)"
  echo "Setting up Music Downloader (first run only)…" >&2
  "$PY" -m venv "$VENV" 2>/dev/null || { rm -rf "$VENV"; die "Couldn’t create the private Python environment.
On Debian, Ubuntu and Mint this usually means:  sudo apt install python3-venv"; }
fi

# ---------------------------------------------------------------- packages (again whenever requirements.txt changes)
want="$(cksum < "$HERE/requirements.txt" | cut -d' ' -f1,2)"
if [ "$update" = 1 ] || [ ! -f "$STAMP" ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$want" ]; then
  echo "Installing packages…" >&2
  flags=(--disable-pip-version-check --quiet)
  [ "$update" = 1 ] && flags+=(--upgrade)
  "$VENV/bin/python" -m pip install "${flags[@]}" -r "$HERE/requirements.txt" ||
    die "Couldn’t install the packages Music Downloader needs. Check your internet connection and run this again."
  echo "$want" > "$STAMP"
fi

# ---------------------------------------------------------------- shortcut
if [ "$shortcut" = 1 ]; then
  mkdir -p "$(dirname "$DESKTOP_FILE")" "$(dirname "$ICON_FILE")"
  (cd "$HERE" && "$VENV/bin/python" -c "
import sys
sys.path.insert(0, '.')
from musicdl.ui import glass
glass.app_icon(256).save(sys.argv[1])" "$ICON_FILE") 2>/dev/null || ICON_FILE="audio-x-generic"
  cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Music Downloader
Comment=Turn playlists and links into music files
Exec="$HERE/run.sh"
Path=$HERE
Icon=$ICON_FILE
Terminal=false
Categories=AudioVideo;Audio;
StartupWMClass=Tk
EOF
  chmod +x "$HERE/run.sh" "$DESKTOP_FILE" 2>/dev/null
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS/applications" 2>/dev/null
  echo "Added Music Downloader to your applications menu."
  exit 0
fi

# ---------------------------------------------------------------- go
if [ -z "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
  die "There is no graphical session to open a window in. Run this from your desktop (not over plain SSH)."
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Note: ffmpeg is not installed, so songs can’t be converted yet.  sudo apt install ffmpeg" >&2
fi

cd "$HERE" || exit 1
if [ "$foreground" = 1 ]; then
  exec "$VENV/bin/python" -m musicdl --no-relaunch
fi
nohup "$VENV/bin/python" -m musicdl --no-relaunch >/dev/null 2>&1 &
disown 2>/dev/null
echo "Music Downloader is starting." >&2
