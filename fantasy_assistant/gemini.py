"""Small client for Google's Gemini API (REST, through `requests`).

It sends one request to generateContent and hands back Gemini's reply. The
chat (chat.py) keeps the conversation and decides what to do with the reply.

Why plain REST instead of Google's SDK: the laptop is Windows on ARM64, and
`requests` is already installed, so this adds no new dependency.

The API key comes from GEMINI_API_KEY in .env. It's sent in a header
(x-goog-api-key), never in the URL, because URLs end up in error messages.
Error messages are also scrubbed of the key, just in case, so it's never
printed.

Docs: https://ai.google.dev/gemini-api/docs/function-calling
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import requests

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Stable, on the free tier, and what Google recommends for new projects
# (checked 11/20). Change it with GEMINI_MODEL in .env.
DEFAULT_MODEL = "gemini-3.5-flash-lite"
TIMEOUT = 60          # seconds; a tool-calling reply is usually a few seconds
SERVER_RETRY_WAIT = 2  # seconds before retrying once after a Google server error
PLACEHOLDER_KEY = "paste-your-key-here"   # what .env.example ships with


# ---------- Errors (each one a plain sentence the CLI can print) ----------

class GeminiError(Exception):
    """Something went wrong talking to Gemini. `fatal` means the chat can't
    go on (the key or model is wrong), so it should stop instead of asking
    for the next question."""
    fatal = False


class MissingKey(GeminiError):
    fatal = True


class BadKey(GeminiError):
    fatal = True


class UnknownModel(GeminiError):
    fatal = True


class RateLimited(GeminiError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class Unreachable(GeminiError):
    """No answer at all: no internet, DNS, or a timeout."""


class ServerError(GeminiError):
    """Google's side failed (5xx), even after one retry."""


class BadReply(GeminiError):
    """Gemini answered, but not with something usable (blocked, empty, or a
    request it rejected)."""


# ---------- Settings ----------

@dataclass(frozen=True)
class Settings:
    key: str
    model: str

    def __repr__(self) -> str:   # so the key can't leak through a print or a traceback
        return f"Settings(key=<hidden>, model={self.model!r})"


def settings_from_env() -> Settings:
    """The key and model from .env. Raises MissingKey if there's no real key."""
    key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not key or key == PLACEHOLDER_KEY:
        raise MissingKey("Set GEMINI_API_KEY in your .env file. You can get a free key at "
                         "https://aistudio.google.com/apikey")
    model = (os.getenv("GEMINI_MODEL") or "").strip() or DEFAULT_MODEL
    return Settings(key, model)


# ---------- The request ----------

def generate(settings: Settings, contents: list[dict], system: str,
             tools: list[dict]) -> dict:
    """Send the conversation so far and return Gemini's reply content:
    {"role": "model", "parts": [...]}, exactly as Gemini sent it.

    Exactly as sent matters: newer models attach a `thoughtSignature` to
    tool calls, and the next request fails unless it comes back unchanged.

    `contents` is the conversation (user and model turns), `system` the
    rules, and `tools` the function declarations. Raises a GeminiError
    subclass with a readable message on any failure.
    """
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": contents,
        "tools": [{"functionDeclarations": tools}],
    }
    resp = _post(settings, body)
    if resp.status_code >= 500:   # Google's side: often gone a moment later
        time.sleep(SERVER_RETRY_WAIT)
        resp = _post(settings, body)
    if resp.status_code != 200:
        raise _error_for(resp, settings)
    return _reply_content(resp)


def _post(settings: Settings, body: dict) -> requests.Response:
    try:
        return requests.post(API_URL.format(model=settings.model), json=body, timeout=TIMEOUT,
                             headers={"x-goog-api-key": settings.key,
                                      "Content-Type": "application/json"})
    except requests.Timeout:
        raise Unreachable(f"Gemini didn't answer within {TIMEOUT} seconds. Try again.") from None
    except requests.RequestException as err:
        raise Unreachable("Couldn't reach Gemini (no internet connection?): "
                          + _scrub(type(err).__name__, settings)) from None


def _reply_content(resp: requests.Response) -> dict:
    try:
        data = resp.json()
    except ValueError:
        raise BadReply("Gemini sent back something that isn't JSON.") from None
    candidates = data.get("candidates") or []
    if not candidates:
        reason = (data.get("promptFeedback") or {}).get("blockReason")
        raise BadReply(f"Gemini didn't answer (blocked: {reason})." if reason
                       else "Gemini sent back an empty reply.")
    content = candidates[0].get("content") or {}
    if not content.get("parts"):
        reason = candidates[0].get("finishReason", "no reason given")
        raise BadReply(f"Gemini sent back an empty reply ({reason}). Try rewording the question.")
    content.setdefault("role", "model")
    return content


def _error_for(resp: requests.Response, settings: Settings) -> GeminiError:
    """The right GeminiError for a failed response, with Google's own message.

    Google's error body looks like {"error": {"code": 429, "message": "...",
    "status": "RESOURCE_EXHAUSTED", "details": [...]}}.
    """
    try:
        error = resp.json().get("error") or {}
    except ValueError:
        error = {}
    message = _scrub(error.get("message") or resp.reason or "no details", settings)
    details = error.get("details") or []
    reasons = {d.get("reason") for d in details if isinstance(d, dict)}
    code = resp.status_code

    if code == 429:
        wait = _retry_delay(details)
        when = f"try again in about {wait:.0f} seconds" if wait else "try again in a minute"
        return RateLimited(f"Gemini's free-tier limit is used up for now; {when}. "
                           f"(Google says: {message})", wait)
    if code in (401, 403) or "API_KEY_INVALID" in reasons or "API key not valid" in message:
        return BadKey(f"Gemini rejected the key in GEMINI_API_KEY ({code}: {message}). "
                      f"Check it in .env, or make a new one at https://aistudio.google.com/apikey")
    if code == 404:
        return UnknownModel(f"Gemini has no model named {settings.model!r} ({message}). "
                            f"Check GEMINI_MODEL in .env (default: {DEFAULT_MODEL}).")
    if code >= 500:
        return ServerError(f"Gemini's servers had a problem ({code}: {message}). Try again.")
    return BadReply(f"Gemini rejected the request ({code}: {message}).")


def _retry_delay(details: list) -> float | None:
    """Seconds to wait, from Google's RetryInfo detail ("retryDelay": "37s")."""
    for d in details:
        if isinstance(d, dict) and str(d.get("@type", "")).endswith("RetryInfo"):
            try:
                return float(str(d.get("retryDelay", "")).rstrip("s"))
            except ValueError:
                return None
    return None


def _scrub(text: str, settings: Settings) -> str:
    """Never let the key show up in a message, whatever Google sends back."""
    return text.replace(settings.key, "<hidden>") if settings.key else text
