"""
ai — optional helpers that use a local Ollama server or a cloud key (OpenAI, Anthropic, Gemini).

    connect(settings) -> Helper | None     (None when AI Mode is off; AIError when it is on but not set up)
    Helper.repair / .organize / .tidy_folder / .generate
    list_models(provider, key, url)        (for the Settings page)
Nothing here runs unless the user turned AI Mode on.
"""
from ..config import get_secret
from .base import AIError, parse_json
from .providers import NAMES, list_models, make_client, normalize_ollama_url, pick_default
from .tasks import GENRES, Helper

__all__ = ["connect", "list_models", "pick_default", "Helper", "AIError", "NAMES", "GENRES", "parse_json",
           "normalize_ollama_url"]


def connect(st):
    if not st.ai_enabled:
        return None
    key = "" if st.ai_provider == "ollama" else get_secret(st.ai_provider)
    client = make_client(st.ai_provider, st.ai_model, key, st.ollama_url)
    if not st.ai_model:
        raise AIError("Choose a model in Settings")
    return Helper(client)
