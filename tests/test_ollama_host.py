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
    assert s.chat("some-model", "write the plan", "plan") == "Hello."
    assert seen["url"] == "https://example.supabase.co/functions/v1/gateway/api/chat"
    assert seen["headers"]["authorization"] == "Bearer sgw_" + "a" * 48
    assert seen["headers"]["x-sdlc-repository"] == "acme/app"
    assert seen["headers"]["x-sdlc-stage"] == "plan"
    assert seen["body"] == {"model": "some-model", "stream": True,
                            "messages": [{"role": "user", "content": "write the plan"}]}


def test_provider_key_goes_only_to_a_gateway(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "sgw_" + "a" * 48)
    monkeypatch.setenv("LLM_API_KEY", "sk-customer-own")
    monkeypatch.delenv("OLLAMA_THINK", raising=False)
    for host in ("", "https://ollama.com", "https://ollama.com/", "https://OLLAMA.com", "https://www.ollama.com"):
        if host:
            monkeypatch.setenv("OLLAMA_HOST", host)
        else:
            monkeypatch.delenv("OLLAMA_HOST", raising=False)
        seen = capture(monkeypatch)
        s.chat("some-model", "hi", "spec")
        assert "x-sdlc-provider-key" not in seen["headers"], host
        assert seen["headers"]["authorization"] == "Bearer sgw_" + "a" * 48
    monkeypatch.setenv("OLLAMA_HOST", "https://example.supabase.co/functions/v1/gateway")
    seen = capture(monkeypatch)
    s.chat("openai/some-model", "hi", "spec")
    assert seen["headers"]["x-sdlc-provider-key"] == "sk-customer-own"
    assert seen["headers"]["authorization"] == "Bearer sgw_" + "a" * 48
    assert seen["body"]["model"] == "openai/some-model"


def test_no_provider_key_header_without_the_secret(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    monkeypatch.setenv("OLLAMA_HOST", "https://example.supabase.co/functions/v1/gateway")
    monkeypatch.setenv("LLM_API_KEY", "  ")
    assert "X-SDLC-Provider-Key" not in s.ollama_headers("plan")


def test_workflow_passes_the_optional_secret_to_every_model_call():
    from conftest import ROOT
    workflow = (ROOT / ".github" / "workflows" / "sdlc.yml").read_text()
    action = (ROOT / "actions" / "sdlc-stage" / "action.yml").read_text()
    assert workflow.count("ollama-api-key: ${{ secrets.OLLAMA_API_KEY }}") == 4
    assert workflow.count("llm-api-key: ${{ secrets.LLM_API_KEY }}") == 4
    assert "LLM_API_KEY:\n" in workflow and "required: false" in workflow
    assert "LLM_API_KEY: ${{ inputs.llm-api-key }}" in action
