"""The Dependabot monitor: alerts read from the API or Trivy, grouped by package with
the version that clears them all, and one tracking issue kept up to date."""
import json
import re
import urllib.error

import pytest

import dependabot_alerts as d
from conftest import ROOT


def alert(name, cve, fixed, sev="high", manifest="requirements.txt", ecosystem="pip", n=1):
    return {"number": n, "html_url": f"https://github.com/a/b/security/dependabot/{n}",
            "dependency": {"package": {"ecosystem": ecosystem, "name": name}, "manifest_path": manifest},
            "security_advisory": {"cve_id": cve, "ghsa_id": f"GHSA-{n}", "severity": sev, "summary": f"bug {n}"},
            "security_vulnerability": {"severity": sev, "vulnerable_version_range": "< 9",
                                       "first_patched_version": fixed and {"identifier": fixed}}}


@pytest.fixture
def gh(monkeypatch, tmp_path):
    """A fake GitHub API: alerts, labels and issues in memory; every call recorded."""
    state = {"alerts": [], "labels": set(), "issues": {}, "calls": [], "refuse": None}

    def request(method, url, token, data=None):
        state["calls"].append((method, url, data))
        path = url.replace(d.API, "")
        if "/dependabot/alerts" in path:
            if state["refuse"]:
                raise d.ApiError(state["refuse"], "Resource not accessible by integration")
            page = int(re.search(r"page=(\d+)", path).group(1)) if "page=" in path.split("per_page")[0] + path.split("per_page=100")[-1] else 1
            items = state["alerts"][(page - 1) * 2: page * 2]
            more = page * 2 < len(state["alerts"])
            return items, (f'<{d.API}/x/dependabot/alerts?page={page + 1}>; rel="next"' if more else "")
        if m := re.fullmatch(r"/repos/a/b/labels/(.+)", path):
            if m.group(1) not in state["labels"]:
                raise d.ApiError(404, "Not Found")
            return {}, ""
        if path == "/repos/a/b/labels":
            state["labels"].add(data["name"])
            return {}, ""
        if path.startswith("/repos/a/b/issues?"):
            return [i for i in state["issues"].values() if i["state"] == "open"], ""
        if path == "/repos/a/b/issues":
            n = len(state["issues"]) + 1
            state["issues"][n] = {"number": n, "state": "open", "comments": [], **data}
            return state["issues"][n], ""
        if m := re.fullmatch(r"/repos/a/b/issues/(\d+)/comments", path):
            state["issues"][int(m.group(1))]["comments"].append(data["body"])
            return {}, ""
        if m := re.fullmatch(r"/repos/a/b/issues/(\d+)", path):
            state["issues"][int(m.group(1))].update(data)
            return state["issues"][int(m.group(1))], ""
        raise AssertionError(f"unexpected call {method} {url}")

    monkeypatch.setattr(d, "request", request)
    monkeypatch.setattr(d, "usage_ping", lambda: None)
    monkeypatch.chdir(tmp_path)
    for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        (tmp_path / name).write_text("")
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.setenv("GITHUB_REPOSITORY", "a/b")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("DEPENDABOT_ALERTS_TOKEN", "alerts-token")
    for name in ("FALLBACK", "LABEL", "GITHUB_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    state["outputs"] = lambda: dict(l.split("=", 1) for l in (tmp_path / "GITHUB_OUTPUT").read_text().splitlines() if l)
    return state


def run(gh, tmp_path):
    d.main(["read", "--out", str(tmp_path / "alerts.json")])
    d.main(["publish", "--alerts", str(tmp_path / "alerts.json")])
    return [i for i in gh["issues"].values()]


def data_of(body):
    return json.loads(re.search(r"```json\n(.*?)\n```", body, flags=re.DOTALL).group(1))


def test_versions_compare_as_numbers_and_pre_releases_come_first():
    assert d.highest(["2.2.5", "2.10.0", "2.9"]) == "2.10.0"
    assert d.highest(["1.0rc1", "1.0", "0.9"]) == "1.0"
    assert d.version_key("1.0") == d.version_key("1.0.0") and d.version_key("v3.17") > d.version_key("3.9.0")
    assert d.highest(["", None]) == ""


def test_names_are_normalized_so_one_package_is_one_row():
    assert d.normalize("Streamlit") == d.normalize("streamlit") == "streamlit"
    assert d.normalize("Sentence_Transformers") == "sentence-transformers"
    assert d.normalize("@Scope/Pkg", "npm") == "@scope/pkg"


def test_pins_are_read_from_a_requirements_file_with_a_bom(tmp_path):
    (tmp_path / "requirements.txt").write_text(
        "﻿langchain==0.2.11\n# torch==1\nStreamlit==1.31.1 ; python_version>'3'\nfaiss-cpu\nst-pages>=0.4.3\n"
        "uvicorn[standard]==0.30.0  # web\n", encoding="utf-8")
    assert d.pinned_versions(tmp_path / "requirements.txt") == {
        "langchain": "0.2.11", "streamlit": "1.31.1", "uvicorn": "0.30.0"}
    assert d.pinned_versions(tmp_path / "missing.txt") == {}


def test_one_row_per_package_upgrades_to_the_highest_fix_worst_first():
    alerts = [d.from_dependabot(a) for a in [
        alert("flask", "CVE-2", "2.2.5", "low", n=1), alert("torch", "CVE-3", "2.2.0", "high", n=2),
        alert("torch", "CVE-4", "2.6.0", "critical", n=3), alert("Flask", "CVE-1", "2.3.2", "high", n=4),
        alert("pandasai", "CVE-5", None, "high", n=5), alert("streamlit", None, "1.37.0", "moderate", n=6)]]
    packages = d.group(alerts)
    assert [(p["package"], p["fix_to"], p["severity"], len(p["alerts"])) for p in packages] == [
        ("torch", "2.6.0", "critical", 2), ("flask", "2.3.2", "high", 2),
        ("pandasai", "", "high", 1), ("streamlit", "1.37.0", "medium", 1)]
    assert packages[2]["no_fix"] == ["CVE-5"] and packages[3]["alerts"][0]["id"] == "GHSA-6"


def test_read_pages_through_the_api_and_notes_the_pinned_version(gh, tmp_path):
    (tmp_path / "requirements.txt").write_text("torch==2.1.0\nflask==2.0.2\n")
    gh["alerts"] = [alert("torch", "CVE-3", "2.2.0", n=1), alert("torch", "CVE-4", "2.6.0", "critical", n=2),
                    alert("flask", "CVE-1", "2.2.5", n=3)]
    d.main(["read", "--out", str(tmp_path / "alerts.json")])
    data = json.loads((tmp_path / "alerts.json").read_text())
    assert data["source"] == "dependabot" and len(data["alerts"]) == 3
    assert {a["package"]: a["current"] for a in data["alerts"]} == {"torch": "2.1.0", "flask": "2.0.2"}
    assert gh["outputs"]()["source"] == "dependabot"
    assert sum("/dependabot/alerts" in c[1] for c in gh["calls"]) == 2          # two pages


@pytest.mark.parametrize("token,refuse", [("", None), ("t", 403), ("t", 404)])
def test_read_falls_back_to_trivy_without_a_working_token(gh, tmp_path, monkeypatch, capsys, token, refuse):
    monkeypatch.setenv("DEPENDABOT_ALERTS_TOKEN", token)
    gh["refuse"] = refuse
    d.main(["read", "--out", str(tmp_path / "alerts.json")])
    assert gh["outputs"]()["source"] == "" and not (tmp_path / "alerts.json").exists()
    assert "Using Trivy instead" in capsys.readouterr().out


def test_read_fails_without_a_token_when_fallback_is_off(gh, tmp_path, monkeypatch):
    monkeypatch.setenv("DEPENDABOT_ALERTS_TOKEN", "")
    monkeypatch.setenv("FALLBACK", "false")
    with pytest.raises(SystemExit):
        d.main(["read", "--out", str(tmp_path / "alerts.json")])


def test_read_fails_on_a_server_error(gh, tmp_path):
    gh["refuse"] = 500
    with pytest.raises(SystemExit):
        d.main(["read", "--out", str(tmp_path / "alerts.json")])


def test_trivy_results_pick_the_fix_on_the_installed_line():
    results = [{"Target": "requirements.txt", "Type": "pip", "Vulnerabilities": [
        {"PkgName": "Flask", "VulnerabilityID": "CVE-1", "InstalledVersion": "2.0.2", "FixedVersion": "2.2.5, 2.3.2",
         "Severity": "HIGH", "PrimaryURL": "u", "Title": "t"},
        {"PkgName": "pandasai", "VulnerabilityID": "CVE-5", "InstalledVersion": "0.2.15", "Severity": "CRITICAL"}]}]
    alerts = d.from_trivy(results)
    assert [(a["package"], a["fixed"], a["current"], a["severity"]) for a in alerts] == [
        ("flask", "2.2.5", "2.0.2", "high"), ("pandasai", "", "0.2.15", "critical")]
    assert d.fixed_for("2.2.6", "2.2.5, 2.3.2") == "2.3.2" and d.fixed_for("3.0", "2.2.5, 2.3.2") == "2.3.2"


def test_publish_opens_one_labelled_issue_with_a_table_and_data(gh, tmp_path):
    gh["alerts"] = [alert("torch", "CVE-3", "2.2.0", n=1), alert("torch", "CVE-4", "2.6.0", "critical", n=2),
                    alert("pandasai", "CVE-5", None, n=3)]
    issues = run(gh, tmp_path)
    assert len(issues) == 1 and issues[0]["labels"] == ["dependabot"] and "dependabot" in gh["labels"]
    issue = issues[0]
    assert issue["title"] == "Dependabot alerts: 3 open in 2 packages"
    assert issue["body"].startswith(d.MARKER)
    assert "| `torch` | `requirements.txt` | — | **2.6.0** | 2 | critical | CVE-4, CVE-3 |" in issue["body"]
    assert "**no fix**" in issue["body"]
    data = data_of(issue["body"])
    assert data["version"] == 1 and data["open"] == 3 and data["source"] == "dependabot"
    assert [p["package"] for p in data["packages"]] == ["torch", "pandasai"]
    assert gh["outputs"]()["issue"] == "1" and gh["outputs"]()["open"] == "3"
    assert "| `torch`" in (tmp_path / "GITHUB_STEP_SUMMARY").read_text()


def test_publish_updates_the_issue_and_comments_on_changes(gh, tmp_path):
    gh["alerts"] = [alert("torch", "CVE-3", "2.2.0", n=1), alert("flask", "CVE-1", "2.2.5", n=2)]
    run(gh, tmp_path)
    run(gh, tmp_path)                                   # nothing changed: no comment
    assert gh["issues"][1]["comments"] == []
    gh["alerts"] = [alert("torch", "CVE-3", "2.2.0", n=1), alert("torch", "CVE-9", "2.6.0", n=3)]
    issues = run(gh, tmp_path)
    assert len(issues) == 1
    assert issues[0]["comments"] == ["**1 new:** `torch` CVE-9\n\n**1 no longer open:** `flask` CVE-1"]


def test_publish_closes_the_issue_when_no_alerts_are_left(gh, tmp_path):
    gh["alerts"] = [alert("torch", "CVE-3", "2.2.0")]
    run(gh, tmp_path)
    gh["alerts"] = []
    run(gh, tmp_path)
    issue = gh["issues"][1]
    assert issue["state"] == "closed" and issue["state_reason"] == "completed"
    assert issue["title"] == "Dependabot alerts: none open" and "No open alerts" in issue["comments"][-1]
    run(gh, tmp_path)                                   # still none: no new issue
    assert len(gh["issues"]) == 1


def test_publish_ignores_other_issues_with_the_label(gh, tmp_path):
    gh["issues"][1] = {"number": 1, "state": "open", "body": "someone else's", "labels": ["dependabot"], "comments": []}
    gh["labels"].add("dependabot")
    gh["alerts"] = [alert("torch", "CVE-3", "2.2.0")]
    run(gh, tmp_path)
    assert len(gh["issues"]) == 2 and gh["issues"][1]["body"] == "someone else's"


def test_a_huge_report_still_fits_an_issue_body():
    alerts = [d.from_dependabot(alert(f"pkg{i % 40}", f"CVE-2026-{i:05}", "1.0", n=i)) for i in range(900)]
    for a in alerts:
        a["summary"] = "x" * 200
    body = d.render({"source": "dependabot", "alerts": alerts}, d.group(alerts), "", "now")
    data = data_of(body)
    assert len(body) <= d.MAX_BODY and data["open"] == 900 and data["truncated"] and len(data["packages"]) == 40


def test_the_trivy_pin_matches_the_security_scan():
    pins = lambda f: re.findall(r"trivy-(?:version|sha256):\s*\n(?:.*\n)?\s*default:\s*(\S+)",
                                (ROOT / "actions" / f / "action.yml").read_text())
    assert len(pins("dependabot-alerts")) == 2 and pins("dependabot-alerts") == pins("security-scan")


def test_the_ping_sends_only_the_repository_name(monkeypatch):
    seen = []

    class Ok:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def urlopen(req, timeout=None):
        seen.append((req.full_url, json.loads(req.data)))
        return Ok()

    monkeypatch.setattr(d.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("GITHUB_REPOSITORY", "acme/app")
    monkeypatch.delenv("SDLC_PING_URL", raising=False)
    d.usage_ping()
    url = re.search(r"^SDLC_PING_URL=(.+)$", (ROOT / "actions" / "defaults.env").read_text(), re.MULTILINE).group(1)
    assert seen == [(url, {"repository": "acme/app"})]

    def down(req, timeout=None):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr(d.urllib.request, "urlopen", down)
    d.usage_ping()                                      # warns, doesn't raise


def test_no_change_comment_when_a_report_was_cut_down():
    assert d.changes(None, {("a", "1")}) == "" and d.changes({("a", "1")}, None) == ""
    assert d.previous_ids("no json here") is None
    assert d.previous_ids('```json\n{"truncated":true,"packages":[]}\n```') is None
