"""
platform_.py — everything that differs between operating systems, in one place.

This is the Windows + Linux edition. (The macOS tree ships its own platform_.py with the same public
functions, so the rest of the code never checks sys.platform.)
"""
import os
import shutil
import subprocess
import sys

IS_WINDOWS = sys.platform == "win32"
OS_NAME = "windows" if IS_WINDOWS else "linux"
APP_DIR_NAME = "MusicDownloader"

NO_WINDOW = 0x08000000 if IS_WINDOWS else 0          # CREATE_NO_WINDOW: ffmpeg/browser never flash a console
DETACHED = 0x00000008 | 0x00000200 if IS_WINDOWS else 0   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


# ---------------------------------------------------------------- folders

def config_dir():
    """Per-user settings folder. MUSICDL_HOME overrides it (portable installs, tests)."""
    home = os.environ.get("MUSICDL_HOME")
    if home:
        path = home
    elif IS_WINDOWS:
        path = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIR_NAME)
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
        path = os.path.join(base, APP_DIR_NAME.lower())
    os.makedirs(path, exist_ok=True)
    return path


def cache_dir():
    home = os.environ.get("MUSICDL_HOME")
    if home:
        path = os.path.join(home, "cache")
    elif IS_WINDOWS:
        path = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), APP_DIR_NAME, "cache")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        path = os.path.join(base, APP_DIR_NAME.lower())
    os.makedirs(path, exist_ok=True)
    return path


def log_path():
    return os.path.join(config_dir(), "musicdl.log")


def default_music_dir():
    music = os.path.join(os.path.expanduser("~"), "Music")
    return os.path.join(music if os.path.isdir(music) else os.path.expanduser("~"), "Music Downloader")


def open_path(path):
    """Show a folder or file in the system file manager."""
    try:
        if IS_WINDOWS:
            os.startfile(path)                                   # noqa: S606 (user-chosen folder)
        else:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------------------------------------------------------- machine facts

def total_ram_gb():
    try:
        if IS_WINDOWS:
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return round(st.ullTotalPhys / 2 ** 30, 1)
        else:
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        return round(int(line.split()[1]) / 2 ** 20, 1)
    except Exception:
        pass
    return None


def process_rss_mb():
    """Resident memory of this process in MB (used by the stress test and the log), or None."""
    try:
        if IS_WINDOWS:
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
            k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return pmc.WorkingSetSize / 2 ** 20
        else:
            with open("/proc/self/statm", encoding="ascii") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2 ** 20
    except Exception:
        pass
    return None


def extra_bin_dirs():
    """Folders worth searching for command-line tools when the launcher's PATH is minimal."""
    if IS_WINDOWS:                                       # where winget puts command-line tools (before the next sign-in)
        local = os.environ.get("LOCALAPPDATA")
        return [os.path.join(local, "Microsoft", "WinGet", "Links")] if local else []
    return ["/usr/local/bin", "/usr/bin", "/snap/bin", os.path.expanduser("~/.local/bin")]


def find_tool(name):
    """Full path of an executable (ffmpeg, …) or None."""
    found = shutil.which(name)
    if found:
        return found
    for d in extra_bin_dirs():
        p = os.path.join(d, name + (".exe" if IS_WINDOWS else ""))
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def ffmpeg_hint():
    return ("Install ffmpeg with:  winget install Gyan.FFmpeg" if IS_WINDOWS
            else "Install ffmpeg with your package manager, e.g.  sudo apt install ffmpeg")


def preferred_aac_encoder(available):
    """Best AAC encoder among those this ffmpeg build offers."""
    for name in ("libfdk_aac", "aac"):
        if name in available:
            return name
    return "aac"


# ---------------------------------------------------------------- appearance

def system_theme():
    """'light' or 'dark', following the operating system's setting."""
    try:
        if IS_WINDOWS:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
                return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                             capture_output=True, text=True, timeout=2).stdout.lower()
        if "dark" in out:
            return "dark"
        if "default" in out or "light" in out:
            return "light"
        out = subprocess.run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"],
                             capture_output=True, text=True, timeout=2).stdout.lower()
        return "dark" if "dark" in out else "light"
    except Exception:
        return "dark"


def font_candidates():
    """Preferred font families (first installed wins): body, semibold/display."""
    if IS_WINDOWS:
        return {"body": ("Segoe UI Variable Text", "Segoe UI"),
                "semi": ("Segoe UI Variable Text Semibold", "Segoe UI Semibold", "Segoe UI"),
                "display": ("Segoe UI Variable Display Semib", "Segoe UI Semibold", "Segoe UI")}
    return {"body": ("Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans"),
            "semi": ("Inter SemiBold", "Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans"),
            "display": ("Inter Display", "Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans")}


def ui_scale(root):
    """Logical-pixel multiplier (1.0 = 96 dpi)."""
    try:
        return max(1.0, root.winfo_fpixels("1i") / 96.0)
    except Exception:
        return 1.0


def prepare_process(app_id="MusicDownloader.V3"):
    """Crisp text on high-DPI screens and our own taskbar identity (Windows)."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def style_window(root, dark, bar_rgb, text_rgb):
    """Colour the native title bar to match the backdrop (Windows 11); no-op elsewhere."""
    if not IS_WINDOWS:
        return
    import ctypes
    try:
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
    except Exception:
        return

    def attr(a, v):
        try:
            c = ctypes.c_int(v)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, a, ctypes.byref(c), ctypes.sizeof(c))
        except Exception:
            pass
    ref = lambda c: c[0] | (c[1] << 8) | (c[2] << 16)
    attr(20, 1 if dark else 0)          # immersive dark mode
    attr(35, ref(bar_rgb))              # caption colour
    attr(34, ref(bar_rgb))              # border colour
    attr(36, ref(text_rgb))             # caption text


def attention(root, on=True, message=""):
    """Ask for the user's attention without stealing focus: the taskbar button flashes until the window is brought to
    the front (Windows). On Linux a desktop notification does the job. `on=False` stops the flashing."""
    try:
        if IS_WINDOWS:
            import ctypes
            from ctypes import wintypes

            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT), ("hwnd", ctypes.c_void_p), ("dwFlags", wintypes.DWORD),
                            ("uCount", wintypes.UINT), ("dwTimeout", wintypes.DWORD)]
            user32 = ctypes.windll.user32
            user32.GetParent.restype = ctypes.c_void_p
            user32.GetParent.argtypes = [ctypes.c_void_p]
            user32.FlashWindowEx.argtypes = [ctypes.POINTER(FLASHWINFO)]
            hwnd = user32.GetParent(root.winfo_id())
            flags = 0x2 | 0xC if on else 0x0               # FLASHW_TRAY | FLASHW_TIMERNOFG   /   FLASHW_STOP
            user32.FlashWindowEx(ctypes.byref(FLASHWINFO(ctypes.sizeof(FLASHWINFO), hwnd, flags, 0, 0)))
        elif on and message and shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "Music Downloader", "-u", "critical", "Music Downloader", message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def wheel_steps(event):
    """Mouse-wheel event -> notches (positive = scroll up)."""
    if getattr(event, "num", 0) == 4:
        return 1
    if getattr(event, "num", 0) == 5:
        return -1
    return event.delta / 120.0


def bind_wheel(widget, callback):
    """Bind the wheel on every OS to callback(event)."""
    if IS_WINDOWS:
        widget.bind_all("<MouseWheel>", callback)
    else:
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            widget.bind_all(seq, callback)


MOD_KEY = "Control"


# ---------------------------------------------------------------- headless browser (for JavaScript-only pages)

def browser_candidates():
    """Chromium-family executables that can render a page without a window."""
    if IS_WINDOWS:
        roots = [os.environ.get(v) for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        rel = (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
               r"BraveSoftware\Brave-Browser\Application\brave.exe", r"Chromium\Application\chrome.exe")
        return [os.path.join(r, p) for p in rel for r in roots if r and os.path.exists(os.path.join(r, p))]
    names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge",
             "microsoft-edge-stable", "brave-browser")
    return [p for p in (shutil.which(n) for n in names) if p]


# ---------------------------------------------------------------- audio cues

def play_file_command(path):
    """Linux: first available command-line player for a WAV file."""
    for exe, args in (("paplay", []), ("pw-play", []), ("aplay", ["-q"]), ("play", ["-q"]),
                      ("ffplay", ["-nodisp", "-autoexit", "-loglevel", "quiet"])):
        found = shutil.which(exe)
        if found:
            return [found] + args + [path]
    return None


_playing = None                      # Linux: the player process of the cue sounding now (sounds.py calls from one thread)


def _busy():
    return _playing is not None and _playing.poll() is None


def play_wav(path):
    """Start playing a WAV file without blocking; replaces a cue that is still sounding."""
    global _playing
    if IS_WINDOWS:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        return None
    cmd = play_file_command(path)
    if not cmd:
        return None
    if _busy():
        try:
            _playing.terminate()
        except OSError:
            pass
    _playing = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return None


def play_wav_if_idle(path):
    """Like play_wav but never cuts off a sound that is still playing."""
    if IS_WINDOWS:
        import winsound
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT
                               | winsound.SND_NOSTOP)
        except RuntimeError:
            pass
        return None
    if _busy():
        return None
    return play_wav(path)


# ---------------------------------------------------------------- launching

def relaunch_without_console(script, argv):
    """Started from python.exe (so a terminal is open)? Start the same thing under pythonw.exe."""
    if not IS_WINDOWS or os.path.basename(sys.executable).lower() != "python.exe":
        return False
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        return False
    try:
        subprocess.Popen([pyw, script, "--no-relaunch"] + list(argv), creationflags=DETACHED, close_fds=True,
                         cwd=os.path.dirname(script))
        return True
    except OSError:
        return False


def message_box(title, text):
    """Works with no console and before (or without) Tk."""
    try:
        if IS_WINDOWS:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
            return
        for exe, args in (("zenity", ["--error", "--title", title, "--text", text]),
                          ("kdialog", ["--error", text, "--title", title])):
            if shutil.which(exe):
                subprocess.run([exe] + args, timeout=60)
                return
    except Exception:
        pass
    print(f"{title}: {text}", file=sys.stderr)
