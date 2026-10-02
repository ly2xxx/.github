import http.server
import json
import os
import re
import subprocess
import threading

import sdlc_stage as s
from conftest import ROOT

DEFAULTS = (ROOT / "actions" / "defaults.env").read_text()
MODEL = re.search(r"^OLLAMA_MODEL=(.+)$", DEFAULTS, flags=re.MULTILINE).group(1)


def test_models_are_named_only_in_defaults_env():
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    named = [f for f in tracked.splitlines()
             if f != "actions/defaults.env" and re.search(r"[\w.-]+:cloud\b", (ROOT / f).read_text(errors="ignore"))]
    assert named == []


def test_sdlc_stage_falls_back_to_it(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "k")
    monkeypatch.setenv("OLLAMA_MODEL", "")
    assert s.ollama_model("stage") == MODEL
    monkeypatch.setenv("OLLAMA_MODEL", "some-other:model")
    assert s.ollama_model("stage") == "some-other:model"


class FakeOllama(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        model = json.loads(self.rfile.read(int(self.headers["Content-Length"])))["model"]
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"message": {"content": f"answered by {model}"}}).encode())

    def log_message(self, *args):
        pass


def test_commit_delta_summary_falls_back_to_it(repo, tmp_path):
    server = http.server.HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {**os.environ, "OLLAMA_API_KEY": "k", "OLLAMA_MODEL": "", "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1",
           "OLLAMA_HOST": f"http://127.0.0.1:{server.server_port}"}
    out = tmp_path / "summary.md"
    try:
        subprocess.run(["bash", str(ROOT / "actions/commit-delta-summary/commit-delta-summary.sh"), "HEAD~1", "HEAD",
                        str(out)], cwd=repo, env=env, check=True, capture_output=True)
    finally:
        server.shutdown()
    assert f"answered by {MODEL}" in out.read_text() and f"model `{MODEL}`" in out.read_text()
