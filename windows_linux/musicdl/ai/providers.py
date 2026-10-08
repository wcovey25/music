"""
providers.py — Ollama (local) and OpenAI / Anthropic / Google Gemini (cloud) behind one small interface.

API keys travel only in request headers (never in a URL, so they can't end up in logs). Model lists are fetched
live from each service, so new models appear without an app update.
"""
import re

from ..core import netio
from ..core.models import EngineError, Stopped
from .base import AIError, Client, friendly

BASES = {
    "openai": "https://api.openai.com",
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com",
}
DEFAULT_OLLAMA = "http://localhost:11434"
ANTHROPIC_VERSION = "2023-06-01"
NAMES = {"ollama": "Ollama", "openai": "OpenAI", "anthropic": "Anthropic", "gemini": "Gemini"}


def normalize_ollama_url(url):
    u = (url or "").strip() or DEFAULT_OLLAMA
    if "://" not in u:
        u = "http://" + u
    return re.sub(r"/(?:api/?)?$", "", u.rstrip("/"))


def _post(provider, url, body, headers=None, timeout=(8, 120), stop=None, shown_url=""):
    try:
        return netio.post_json(url, body, headers=headers, timeout=timeout, stop=stop, retries=2, ok_404=False) or {}
    except (Stopped, AIError):
        raise
    except EngineError as e:
        raise AIError(friendly(e, NAMES[provider], shown_url)) from None


def _get(provider, url, params=None, headers=None, stop=None, shown_url=""):
    try:
        return netio.get_json(url, params=params, headers=headers, timeout=(6, 20), stop=stop, retries=1,
                              ok_404=False) or {}
    except (Stopped, AIError):
        raise
    except EngineError as e:
        raise AIError(friendly(e, NAMES[provider], shown_url)) from None


class Ollama(Client):
    provider = "ollama"

    def __init__(self, model="", key="", url=""):
        super().__init__(model, key, normalize_ollama_url(url))

    def chat(self, system, user, stop=None, timeout=(8, 180)):
        body = {"model": self.model, "stream": False, "format": "json", "options": {"temperature": 0.1},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        data = _post("ollama", f"{self.url}/api/chat", body, timeout=timeout, stop=stop, shown_url=self.url)
        return str((data.get("message") or {}).get("content") or "")

    def models(self, stop=None):
        data = _get("ollama", f"{self.url}/api/tags", stop=stop, shown_url=self.url)
        return sorted(m.get("name", "") for m in data.get("models", []) if m.get("name"))


class OpenAI(Client):
    provider = "openai"
    _SKIP = re.compile(r"audio|realtime|transcribe|tts|whisper|image|dall|embed|moderation|instruct|search|codex|"
                       r"davinci|babbage|computer-use|sora", re.I)

    def _headers(self):
        return {"Authorization": f"Bearer {self.key}"}

    def chat(self, system, user, stop=None, timeout=(8, 120)):
        body = {"model": self.model, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        data = _post("openai", f"{BASES['openai']}/v1/chat/completions", body, self._headers(), timeout, stop)
        return str(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")

    def models(self, stop=None):
        data = _get("openai", f"{BASES['openai']}/v1/models", headers=self._headers(), stop=stop)
        ids = sorted(m.get("id", "") for m in data.get("data", []) if m.get("id"))
        chat = [i for i in ids if re.match(r"^(gpt|o\d|chatgpt)", i) and not self._SKIP.search(i)]
        return chat or ids


class Anthropic(Client):
    provider = "anthropic"

    def _headers(self):
        return {"x-api-key": self.key, "anthropic-version": ANTHROPIC_VERSION}

    def chat(self, system, user, stop=None, timeout=(8, 120)):
        body = {"model": self.model, "max_tokens": 4096, "system": system,
                "messages": [{"role": "user", "content": user}]}
        data = _post("anthropic", f"{BASES['anthropic']}/v1/messages", body, self._headers(), timeout, stop)
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")

    def models(self, stop=None):
        data = _get("anthropic", f"{BASES['anthropic']}/v1/models", {"limit": 100}, self._headers(), stop)
        return [m.get("id", "") for m in data.get("data", []) if m.get("id")]


class Gemini(Client):
    provider = "gemini"
    _SKIP = re.compile(r"embed|aqa|imagen|tts|veo|image|live|vision|robotics|learnlm", re.I)

    def _headers(self):
        return {"x-goog-api-key": self.key}

    def chat(self, system, user, stop=None, timeout=(8, 120)):
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1}}
        data = _post("gemini", f"{BASES['gemini']}/v1beta/models/{self.model}:generateContent", body,
                     self._headers(), timeout, stop)
        parts = (((data.get("candidates") or [{}])[0].get("content") or {}).get("parts")) or []
        return "".join(p.get("text", "") for p in parts)

    def models(self, stop=None):
        data = _get("gemini", f"{BASES['gemini']}/v1beta/models", {"pageSize": 200}, self._headers(), stop)
        out = []
        for m in data.get("models", []):
            if "generateContent" in (m.get("supportedGenerationMethods") or []):
                name = str(m.get("name", "")).removeprefix("models/")
                if name and not self._SKIP.search(name):
                    out.append(name)
        return sorted(out)


CLIENTS = {"ollama": Ollama, "openai": OpenAI, "anthropic": Anthropic, "gemini": Gemini}

# What to pre-select the first time the model list appears: small, fast and cheap is plenty for tidying tags.
_PREFER = {
    "openai": (r"^gpt-[\w.]+-mini$", r"^gpt-.*mini", r"^gpt-", r"mini"),
    "anthropic": (r"haiku", r"sonnet", r"."),
    "gemini": (r"flash(?!.*(lite|preview|exp))", r"flash", r"."),
    "ollama": (r"llama3|qwen|mistral|gemma|phi", r"."),
}


def pick_default(provider, models):
    for pattern in _PREFER.get(provider, (r".",)):
        hits = sorted((m for m in models if re.search(pattern, m, re.I)), reverse=True)
        if hits:
            return hits[0]
    return ""


def make_client(provider, model="", key="", url=""):
    cls = CLIENTS.get(provider)
    if not cls:
        raise AIError("Choose an AI provider in Settings")
    if provider != "ollama" and not key:
        raise AIError(f"Add your {NAMES[provider]} API key in Settings")
    return cls(model=model, key=key, url=url)


def list_models(provider, key="", url="", stop=None):
    """Connect and list models — used by Settings to verify the key/server and fill the model picker."""
    return make_client(provider, "", key, url).models(stop)
