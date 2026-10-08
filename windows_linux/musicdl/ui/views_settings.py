"""
views_settings.py — the Settings page: grouped rows for performance, accuracy, appearance and sound, the output
folder, AI Mode (Ollama or a cloud key) and an About block. Everything is a row spec; rows.py draws them.
"""
import logging
import os
from tkinter import filedialog

from .. import __version__, ai, autotune, platform_
from ..config import MIN_KBPS_CHOICES, PROVIDERS, get_secret, set_secret
from .rows import Group, Row

log = logging.getLogger("musicdl")


def _has_keyring():
    try:
        import keyring  # noqa: F401
        return True
    except Exception:
        return False


class SettingsMixin:
    def _init_settings(self):
        self.ai_state = dict(models=[], status="", ok=False, busy=False, provider="")
        self.tidy_status = ""

    # ---------------------------------------------------------------- page
    def draw_settings(self):
        p = self.p
        x, y, w, h = self.rS
        self.rows.groups = self.settings_groups()
        self.rows.draw((p(x + 16), p(y + 16), p(x + w - 16), p(y + h - 16)), reset=True)

    def settings_groups(self):
        s = self.s

        def put(attr, then=None):
            def set_(v):
                setattr(s, attr, v)
                s.clamp()
                s.save()
                if then:
                    then()
            return set_

        def appearance(v):
            s.appearance = v
            s.save()
            self.root.after(40, self.retheme)

        def volume(v):
            s.volume = v
            s.save()
            self.cue("click")

        prov = lambda: s.ai_provider
        ollama = lambda: s.ai_enabled and s.ai_provider == "ollama"
        cloud = lambda: s.ai_enabled and s.ai_provider != "ollama"
        key_home = "your system keychain" if _has_keyring() else "a private file in your profile folder"

        return [
            Group("Performance", [
                Row("switch", "Automatic", sub=lambda: (autotune.describe(s.auto_info) if s.auto and s.auto_info
                                                        else "Match speed to this computer and connection"),
                    get=lambda: s.auto,
                    set=put("auto"), refresh=True),
                Row("stepper", "Songs at once", lo=1, hi=16, get=lambda: s.parallel, set=put("parallel"),
                    enabled=lambda: not s.auto,
                    sub=lambda: f"Automatic is using {s.effective_parallel()}" if s.auto else ""),
                Row("stepper", "Retries", lo=0, hi=5, get=lambda: s.retries, set=put("retries"),
                    enabled=lambda: not s.auto,
                    sub=lambda: f"Automatic is using {s.effective_retries()}" if s.auto else ""),
            ]),
            Group("Accuracy", [
                Row("stepper", "Length tolerance", lo=5, hi=50, step=5, fmt=lambda v: f"±{v}%", get=lambda: s.tolerance,
                    set=put("tolerance"), sub="How far a file may differ from the song’s real length"),
                Row("popup", "Minimum quality", choices=[(k, "Off" if not k else f"{k} kbps") for k in MIN_KBPS_CHOICES],
                    get=lambda: s.min_kbps, set=put("min_kbps"), width=130),
                Row("switch", "Replace low-quality files", sub="Look for a better copy when a file is below the minimum",
                    get=lambda: s.replace_low, set=put("replace_low")),
                Row("switch", "Search YouTube", sub="Used when other sources don’t have the song", get=lambda: s.youtube,
                    set=put("youtube")),
                Row("switch", "Check existing files", sub="Re-check length and quality of songs you already have",
                    get=lambda: s.verify, set=put("verify")),
                Row("segment", "Better versions", choices=[("ask", "Ask"), ("replace", "Replace"), ("keep", "Keep")],
                    get=lambda: s.upgrade, set=put("upgrade"), min=70,
                    sub="When a song you have is lower quality than you chose"),
            ]),
            Group("Experience", [
                Row("segment", "Appearance", choices=[("auto", "Auto"), ("light", "Light"), ("dark", "Dark")],
                    get=lambda: s.appearance, set=appearance, min=64),
                Row("switch", "Sounds", sub="Soft cues for starting, clicks and finishing", get=lambda: s.sounds,
                    set=put("sounds"), refresh=True),
                Row("stepper", "Volume", lo=0, hi=100, step=10, fmt=lambda v: f"{v}%", get=lambda: s.volume,
                    set=volume, show=lambda: s.sounds),
                Row("switch", "Launch animation", get=lambda: s.splash, set=put("splash")),
            ]),
            Group("Music folder", [
                Row("action", "Save songs to", sub=lambda: s.outdir, button="Choose…", cb=self.choose_folder),
            ]),
            Group("AI Mode", [
                Row("switch", "AI Mode", sub="Repair song details and build playlists with a model you provide",
                    get=lambda: s.ai_enabled, set=self.set_ai_enabled, refresh=True),
                Row("segment", "Provider", choices=[(k, v) for k, v in PROVIDERS.items()], get=prov,
                    set=self.set_provider, show=lambda: s.ai_enabled, refresh=True, min=70),
                Row("text", "Server address", get=lambda: s.ollama_url, set=self.set_ollama_url, show=ollama,
                    width=240, placeholder="http://localhost:11434"),
                Row("password", "API key", sub=f"Kept in {key_home}", get=lambda: get_secret(s.ai_provider),
                    set=self.set_api_key, show=cloud, width=200, placeholder="Paste key"),
                Row("action", "Connection", sub=lambda: self.ai_state["status"] or "Not tested yet",
                    button="Connect", cb=self.connect_ai, show=lambda: s.ai_enabled),
                Row("popup", "Model", choices=lambda: [(m, m) for m in self.ai_state["models"]], get=lambda: s.ai_model,
                    set=put("ai_model"), show=lambda: s.ai_enabled, width=230,
                    placeholder="Connect to choose"),
                Row("switch", "Repair song details", sub="Fix missing or messy titles, artists and albums",
                    get=lambda: s.ai_repair, set=put("ai_repair"), show=lambda: s.ai_enabled),
                Row("switch", "Add genres", sub="Genre tag for each new song", get=lambda: s.ai_organize,
                    set=put("ai_organize"), show=lambda: s.ai_enabled),
                Row("action", "Tidy my music folder", sub=lambda: self.tidy_status or "Add genres to songs you already have",
                    button="Add genres", cb=self.tidy_genres, show=lambda: s.ai_enabled),
            ], note="Only song titles, artists and albums are sent to the model — never audio files or file paths. "
                    "A local Ollama server keeps everything on your computer."),
            Group("About", [
                Row("info", "Version", get=lambda: __version__),
                Row("info", "ffmpeg", get=lambda: "Ready" if platform_.find_tool("ffmpeg") else "Not found",
                    color=None),
                Row("action", "Log file", sub="For troubleshooting", button="Show", cb=self.show_log),
            ]),
        ]

    # ---------------------------------------------------------------- actions
    def choose_folder(self):
        path = filedialog.askdirectory(parent=self.root, initialdir=self.s.outdir or None, title="Save songs to")
        if path:
            self.s.outdir = os.path.normpath(path)
            self.s.save()
            self.rows.refresh()

    def show_log(self):
        platform_.open_path(os.path.dirname(platform_.log_path()))

    # ---------------------------------------------------------------- AI
    def ai_provider_now(self):
        return self.s.ai_provider

    def set_ai_enabled(self, on):
        self.s.ai_enabled = bool(on)
        self.s.save()
        if on and not self.ai_state["ok"]:
            self.root.after(80, self.connect_ai)

    def set_provider(self, value):
        self.s.ai_provider = value
        self.s.ai_model = ""
        self.s.save()
        self.ai_state.update(models=[], status="", ok=False)
        self.root.after(80, self.connect_ai)

    def set_ollama_url(self, value):
        self.s.ollama_url = ai.normalize_ollama_url(value)
        self.s.save()
        self.ai_state.update(models=[], status="", ok=False)
        self.root.after(80, self.connect_ai)

    def set_api_key(self, value):
        try:
            set_secret(self.s.ai_provider, value)
        except Exception as e:
            self.ai_state["status"] = f"Couldn’t save the key: {e}"
            return
        self.ai_state.update(models=[], status="", ok=False)
        self.root.after(80, self.connect_ai)

    def connect_ai(self):
        st, s = self.ai_state, self.s
        if not s.ai_enabled or st["busy"]:
            return
        provider = s.ai_provider
        key = "" if provider == "ollama" else get_secret(provider)
        if provider != "ollama" and not key:
            st.update(status=f"Add your {PROVIDERS[provider]} API key to connect", ok=False)
            self._settings_refresh()
            return
        if self.runner.busy():
            st["status"] = "Busy right now — try again in a moment"
            self._settings_refresh()
            return
        st.update(busy=True, status="Connecting…")
        url = s.ollama_url
        self._settings_refresh()

        def work(stop, post):
            try:
                post("models", (provider, ai.list_models(provider, key, url, stop), None))
            except Exception as e:
                post("models", (provider, None, str(e) or "Couldn’t connect"))
        self.runner.start("models", work)

    def on_models(self, provider, models, error):
        st, s = self.ai_state, self.s
        st["busy"] = False
        if provider != s.ai_provider:
            return
        if error or not models:
            st.update(models=[], ok=False, status=error or "Connected, but no models were found")
        else:
            st.update(models=list(models), ok=True,
                      status=f"Connected · {len(models)} model{'s' if len(models) != 1 else ''}")
            if not s.ai_model or s.ai_model not in models:
                s.ai_model = ai.pick_default(provider, models) or models[0]
                s.save()
        self._settings_refresh()

    def tidy_genres(self):
        if self.runner.busy():
            self.tidy_status = "Busy right now — try again in a moment"
            self._settings_refresh()
            return
        try:
            helper = ai.connect(self.s)
        except Exception as e:
            self.tidy_status = str(e)
            self._settings_refresh()
            return
        folder = self.s.outdir
        self.tidy_status = "Looking through your music…"
        self._settings_refresh()

        def work(stop, post):
            n = helper.tidy_folder(folder, stop, say=lambda t: post("tidied", t))
            post("tidied", f"Added genres to {n:,} song{'s' if n != 1 else ''}")
        self.runner.start("tidy", work)

    def on_tidied(self, text):
        self.tidy_status = text
        self._settings_refresh()

    def _settings_refresh(self):
        if self.page == "settings" and not self.transitioning and not self.entries:
            self.rows.refresh()
