"""gemini.py against fake HTTP responses (no network): what's sent, what
comes back, and every way a request can fail, with the key never shown."""

import pytest
import requests

from fantasy_assistant import gemini

KEY = "AIza-test-key-not-real-123"
SETTINGS = gemini.Settings(KEY, "gemini-3.5-flash-lite")


class FakeResponse:
    def __init__(self, status_code=200, body=None, reason=""):
        self.status_code, self._body, self.reason = status_code, body, reason

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


class FakePost:
    """Stands in for requests.post: hands back the scripted responses in
    order (or raises them) and records each request."""

    def __init__(self, *responses):
        self.responses, self.requests = list(responses), []

    def __call__(self, url, json=None, timeout=None, headers=None):
        self.requests.append({"url": url, "json": json, "headers": headers})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def post(monkeypatch):
    def install(*responses):
        fake = FakePost(*responses)
        monkeypatch.setattr(gemini.requests, "post", fake)
        return fake
    monkeypatch.setattr(gemini.time, "sleep", lambda s: None)
    return install


def google_error(code, status, message, details=()):
    return FakeResponse(code, {"error": {"code": code, "status": status, "message": message,
                                         "details": list(details)}})


def ask():
    return gemini.generate(SETTINGS, [{"role": "user", "parts": [{"text": "hi"}]}],
                           "rules", [{"name": "get_lineup", "description": "lineup"}])


# ---------- Settings ----------

def test_settings_need_a_real_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(gemini.MissingKey, match="GEMINI_API_KEY"):
        gemini.settings_from_env()
    monkeypatch.setenv("GEMINI_API_KEY", "paste-your-key-here")   # .env.example's placeholder
    with pytest.raises(gemini.MissingKey):
        gemini.settings_from_env()


def test_model_defaults_to_flash_lite_and_gemini_model_overrides_it(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    assert gemini.settings_from_env().model == "gemini-3.5-flash-lite"
    monkeypatch.setenv("GEMINI_MODEL", "gemini-4-hydrogen")
    assert gemini.settings_from_env().model == "gemini-4-hydrogen"


def test_settings_never_show_the_key_when_printed():
    assert KEY not in repr(SETTINGS) and KEY not in str(SETTINGS)


# ---------- A good request ----------

def test_key_goes_in_a_header_not_the_url_and_the_reply_comes_back_unchanged(post):
    part = {"functionCall": {"name": "get_lineup", "args": {}, "id": "c1"},
            "thoughtSignature": "sig-A"}
    fake = post(FakeResponse(200, {"candidates": [{"content": {"role": "model",
                                                               "parts": [part]}}]}))
    content = ask()
    assert content == {"role": "model", "parts": [part]}   # signature kept as sent
    [req] = fake.requests
    assert req["url"] == ("https://generativelanguage.googleapis.com/v1beta/models/"
                          "gemini-3.5-flash-lite:generateContent")
    assert KEY not in req["url"] and req["headers"]["x-goog-api-key"] == KEY
    assert req["json"]["systemInstruction"] == {"parts": [{"text": "rules"}]}
    assert req["json"]["tools"] == [{"functionDeclarations": [
        {"name": "get_lineup", "description": "lineup"}]}]


def test_a_server_error_is_retried_once(post):
    fake = post(google_error(503, "UNAVAILABLE", "The model is overloaded."),
                FakeResponse(200, {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}))
    assert ask() == {"role": "model", "parts": [{"text": "ok"}]}
    assert len(fake.requests) == 2


# ---------- Failures ----------

def test_invalid_key_ends_the_chat_with_a_clear_message(post):
    post(google_error(400, "INVALID_ARGUMENT", "API key not valid. Please pass a valid API key.",
                      [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                        "reason": "API_KEY_INVALID"}]))
    with pytest.raises(gemini.BadKey, match="rejected the key in GEMINI_API_KEY") as err:
        ask()
    assert err.value.fatal


def test_forbidden_key_is_a_bad_key_too(post):
    post(google_error(403, "PERMISSION_DENIED", "Your API key was reported as leaked."))
    with pytest.raises(gemini.BadKey):
        ask()


def test_rate_limit_says_how_long_to_wait_and_the_chat_goes_on(post):
    post(google_error(429, "RESOURCE_EXHAUSTED", "You exceeded your current quota.",
                      [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                        "retryDelay": "37s"}]))
    with pytest.raises(gemini.RateLimited, match="try again in about 37 seconds") as err:
        ask()
    assert err.value.retry_after == 37.0 and not err.value.fatal


def test_unknown_model_points_at_gemini_model_in_env(post):
    post(google_error(404, "NOT_FOUND", "models/gemini-9 is not found for API version v1beta"))
    with pytest.raises(gemini.UnknownModel, match="GEMINI_MODEL") as err:
        ask()
    assert err.value.fatal


def test_server_error_twice_gives_up(post):
    post(google_error(500, "INTERNAL", "Internal error."), google_error(500, "INTERNAL", "Again."))
    with pytest.raises(gemini.ServerError, match="Try again"):
        ask()


@pytest.mark.parametrize("problem", [requests.ConnectionError("Max retries exceeded"),
                                     requests.Timeout("read timed out")])
def test_no_connection_is_a_clear_message_not_a_traceback(post, problem):
    post(problem)
    with pytest.raises(gemini.Unreachable, match="Gemini") as err:
        ask()
    assert not err.value.fatal


def test_blocked_or_empty_replies_are_bad_replies(post):
    post(FakeResponse(200, {"promptFeedback": {"blockReason": "SAFETY"}}))
    with pytest.raises(gemini.BadReply, match="blocked: SAFETY"):
        ask()
    post(FakeResponse(200, {"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL",
                                            "content": {}}]}))
    with pytest.raises(gemini.BadReply, match="MALFORMED_FUNCTION_CALL"):
        ask()
    post(FakeResponse(200, None))
    with pytest.raises(gemini.BadReply, match="isn't JSON"):
        ask()


@pytest.mark.parametrize("response", [
    google_error(400, "INVALID_ARGUMENT", f"API key not valid: {KEY}"),
    google_error(403, "PERMISSION_DENIED", f"Key {KEY} is blocked."),
    google_error(429, "RESOURCE_EXHAUSTED", f"Quota for {KEY} exceeded."),
    google_error(404, "NOT_FOUND", f"Not found ({KEY})."),
    google_error(400, "INVALID_ARGUMENT", f"Bad request with {KEY}."),
    requests.ConnectionError(f"https://example.com/?key={KEY}"),
])
def test_the_key_never_shows_up_in_an_error_message(post, response):
    # Even if Google (or an exception) echoes the key back, it's hidden.
    post(response, response)
    with pytest.raises(gemini.GeminiError) as err:
        ask()
    assert KEY not in str(err.value)
