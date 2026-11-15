"""ollama-host: the stages can call an SDLC gateway in place of ollama.com, and
every call says which repository and stage made it."""
import io
import json

import sdlc_stage as s


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture(monkeypatch):
    seen = {}

    def urlopen(req, timeout=None):
        seen["url"], seen["headers"] = req.full_url, {k.lower(): v for k, v in req.header_items()}
        seen["body"] = json.loads(req.data)
        lines = [{"message": {"content": "Hello"}}, {"message": {"content": "."}, "done": True}]
        return FakeResponse("".join(json.dumps(line) + "\n" for line in lines).encode())

    monkeypatch.setattr(s.urllib.request, "urlopen", urlopen)
    return seen


def test_chat_defaults_to_ollama_com(monkeypatch):
    for name in ("OLLAMA_HOST", "GITHUB_REPOSITORY", "OLLAMA_THINK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama-key")
    seen = capture(monkeypatch)
    assert s.chat("m", "hi") == "Hello."
    assert seen["url"] == "https://ollama.com/api/chat"
    assert seen["headers"]["authorization"] == "Bearer ollama-key"
    assert "x-sdlc-repository" not in seen["headers"] and "x-sdlc-stage" not in seen["headers"]


def test_chat_goes_through_a_gateway_and_names_the_repository_and_stage(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "https://example.supabase.co/functions/v1/gateway/")
    monkeypatch.setenv("OLLAMA_API_KEY", "sgw_" + "a" * 48)
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.delenv("OLLAMA_THINK", raising=False)
    seen = capture(monkeypatch)
    assert s.chat("deepseek-v4.1-flash:cloud", "write the plan", "plan") == "Hello."
    assert seen["url"] == "https://example.supabase.co/functions/v1/gateway/api/chat"
    assert seen["headers"]["authorization"] == "Bearer sgw_" + "a" * 48
    assert seen["headers"]["x-sdlc-repository"] == "acme/app"
    assert seen["headers"]["x-sdlc-stage"] == "plan"
    assert seen["body"] == {"model": "deepseek-v4.1-flash:cloud", "stream": True,
                            "messages": [{"role": "user", "content": "write the plan"}]}
