"""Every pipeline run reports its repository's name to the usage endpoint
(LICENSING.md, "Usage tracking"), and a ping that fails never fails the run."""
import json
import re
import urllib.error

import sdlc_stage as s
from conftest import ROOT

URL = re.search(r"^SDLC_PING_URL=(.+)$", (ROOT / "actions" / "defaults.env").read_text(), flags=re.MULTILINE).group(1)


class Ok:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture(monkeypatch, error=None):
    seen = []

    def urlopen(req, timeout=None):
        seen.append({"url": req.full_url, "body": json.loads(req.data), "timeout": timeout,
                     "headers": {k.lower(): v for k, v in req.header_items()}})
        if error:
            raise error
        return Ok()

    monkeypatch.setattr(s.urllib.request, "urlopen", urlopen)
    return seen


def test_ping_sends_only_the_repository_name_to_the_default_endpoint(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setenv("OLLAMA_API_KEY", "secret-key")
    monkeypatch.delenv("SDLC_PING_URL", raising=False)
    seen = capture(monkeypatch)
    s.usage_ping()
    assert seen == [{"url": URL, "body": {"repository": "acme/app"}, "timeout": 10,
                     "headers": {"content-type": "application/json"}}]
    assert URL.startswith("https://") and URL.endswith("/functions/v1/ping")
    assert "acme/app" in capsys.readouterr().out


def test_ping_is_skipped_without_a_repository(monkeypatch):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    seen = capture(monkeypatch)
    s.usage_ping()
    assert seen == []


def test_a_failed_ping_warns_and_the_run_goes_on(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    for error in (urllib.error.URLError("no route"), TimeoutError("timed out"),
                  urllib.error.HTTPError(URL, 503, "down", {}, None)):
        capture(monkeypatch, error)
        s.usage_ping()                      # does not raise or exit
        assert "::warning::The usage ping failed" in capsys.readouterr().out


def test_resolve_pings_first(repo, monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.setenv("SDLC_FEATURE", "001-add-greeting")
    seen = capture(monkeypatch)
    s.resolve(None)
    assert [call["body"] for call in seen] == [{"repository": "acme/app"}]
