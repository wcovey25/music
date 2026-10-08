"""base.py — what every AI provider looks like, plus reading JSON out of a model's reply."""
import json
import re


class AIError(Exception):
    """Something about the AI connection or reply; the message is written for the user."""


class Client:
    """A connection to one AI service. Subclasses fill in chat() and models()."""
    provider = ""

    def __init__(self, model="", key="", url=""):
        self.model, self.key, self.url = model, key, url

    def chat(self, system, user, stop=None, timeout=(8, 120)):
        """Send one question, get the reply text. Replies are requested as JSON."""
        raise NotImplementedError

    def models(self, stop=None):
        """Names of the models the account/server can use (also proves the connection and key work)."""
        raise NotImplementedError


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)


def parse_json(text):
    """The first JSON object/array in a reply, tolerating ```json fences and chatter around it."""
    s = str(text or "").strip()
    candidates = [s] + _FENCE.findall(s)
    dec = json.JSONDecoder()
    for c in candidates:
        c = c.strip()
        try:
            return json.loads(c)
        except ValueError:
            pass
        for i, ch in enumerate(c):
            if ch in "{[":
                try:
                    return dec.raw_decode(c, i)[0]
                except ValueError:
                    continue
    raise AIError("The AI’s answer wasn’t in a form I could use")


def friendly(err, provider="the AI service", url=""):
    """Turn a network/HTTP error into something a person can act on."""
    msg = str(err)
    low = msg.lower()

    def code(n):                                # a status code, not a piece of a port number or address in the message
        return re.search(rf"(?<![\d:.]){n}(?!\d)", low) is not None

    if code(401) or code(403) or any(k in low for k in ("unauthorized", "invalid api key", "invalid x-api-key",
                                                         "permission")):
        return f"{provider} rejected the API key. Check it in Settings."
    if code(429) or "quota" in low or "rate limit" in low or "overloaded" in low:
        return f"{provider} is rate-limiting or out of quota right now. Try again in a minute."
    if code(404) or "not found" in low or "does not exist" in low:
        return f"{provider} doesn’t have that model. Pick another one in Settings."
    if any(k in low for k in ("refused", "failed to establish", "max retries", "timed out", "timeout", "connection")):
        where = f" at {url}" if url else ""
        extra = " Is it running?" if provider.lower().startswith("ollama") else " Check the internet connection."
        return f"Couldn’t reach {provider}{where}.{extra}"
    return f"{provider} returned an error ({msg[:140]})"
