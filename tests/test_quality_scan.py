import json
import os
import subprocess
import sys
import tomllib

import pytest

import quality_scan as q

# Trimmed from what `qlty init --dry-run`, `qlty check --sarif` and `qlty smells --sarif` print.
INIT = """config_version = "0"

[smells]
mode = "comment"

[[source]]
name = "default"
default = true


[[plugin]]
name = "bandit"

[[plugin]]
name = "osv-scanner"

[[plugin]]
name = "radarlint-python"
mode = "comment"

[[plugin]]
name = "ripgrep"
mode = "comment"

[[plugin]]
name = "ruff"
drivers = [
  "lint",
]

[[plugin]]
name = "trufflehog"
"""


def result(rule, level, uri, line, text):
    return {"ruleId": rule, "level": level, "message": {"text": text},
            "locations": [{"physicalLocation": {"artifactLocation": {"uri": uri}, "region": {"startLine": line}}}]}


CHECK = json.dumps({"runs": [{"tool": {"driver": {"name": "qlty", "rules": [
    {"id": "ruff:F401", "helpUri": "https://docs.astral.sh/ruff/rules/unused-import"}, {"id": "bandit:B602"}]}},
    "results": [result("bandit:B602", "error", "app/calc.py", 31, "subprocess call with shell=True identified."),
                result("ruff:F401", "warning", "app/calc.py", 1, "`os` imported but unused"),
                result("radarlint-python:python:S1481", "note", "app/calc.py", 30, "Remove the unused variable.")]}]})
SMELLS = json.dumps({"runs": [{"tool": {"driver": {"name": "qlty"}}, "results": [
    result("qlty:function-complexity", "warning", "app/calc.py", 5, "Function with high complexity (count = 40): f"),
    result("qlty:function-parameters", "warning", "README.md", 9, "Function with many parameters (count = 6): g")]}]})
BASE_SMELLS = json.dumps({"runs": [{"tool": {"driver": {"name": "qlty"}}, "results": [
    result("qlty:function-parameters", "warning", "README.md", 3, "Function with many parameters (count = 7): g")]}]})
EMPTY = json.dumps({"runs": [{"tool": {"driver": {"name": "qlty"}}, "results": []}]})


def test_generated_config_drops_the_security_scans_plugins_and_tunes_bandit():
    config = q.generated_config(INIT)
    assert q.plugins(config) == ["bandit", "radarlint-python", "ruff"]
    parsed = tomllib.loads(config)
    assert parsed["exclude"][0]["plugins"] == ["bandit"]
    assert parsed["triage"] == [{"match": {"rules": ["bandit:B404", "bandit:B603"]}, "set": {"ignored": True}}]
    assert "[[exclude]]" not in q.generated_config(INIT.replace('name = "bandit"', 'name = "mypy"'))


def test_plugins_leaves_out_disabled_ones():
    assert q.plugins('[[plugin]]\nname = "ruff"\n\n[[plugin]]\nname = "eslint"\nmode = "disabled"\n') == ["ruff"]


def test_issues_map_sarif_levels_filter_and_sort():
    found = q.issues(CHECK, "lint", "medium")
    assert [(i["rule"], i["level"], i["line"]) for i in found] == [("ruff:F401", "medium", 1), ("bandit:B602", "high", 31)]
    assert found[0]["url"] == "https://docs.astral.sh/ruff/rules/unused-import" and found[1]["url"] == ""
    assert len(q.issues(CHECK, "lint", "low")) == 3 and len(q.issues(CHECK, "lint", "high")) == 1


def test_a_smell_that_only_moved_or_grew_is_not_new():
    head, base = q.issues(SMELLS, "smell", "medium"), q.issues(BASE_SMELLS, "smell", "medium")
    params = [s for s in head if s["rule"] == "qlty:function-parameters"]
    assert [s["rule"] for s in q.new_smells(head, base)] == ["qlty:function-complexity"]
    assert q.new_smells(params * 2, base) == params   # a second one in the same file is new


def test_report_lists_new_issues_and_says_where_the_linters_came_from():
    found = q.issues(CHECK, "lint", "medium") + [s for s in q.issues(SMELLS, "smell", "medium")
                                                 if s["rule"] == "qlty:function-complexity"]
    ok, text = q.report(found, ["bandit", "ruff"], True, "medium", "main")
    assert not ok and "**Result: FAILED**" in text and "| Lint (bandit, ruff) | ❌ 2 |" in text
    assert ("| medium | [ruff:F401](https://docs.astral.sh/ruff/rules/unused-import) | `app/calc.py:1` | "
            "`os` imported but unused |") in text
    assert "| Code smells (complexity, nesting, duplication) | ❌ 1 |" in text
    assert "| medium | qlty:function-complexity | `app/calc.py:5` |" in text and "has no `.qlty/qlty.toml`" in text
    ok, text = q.report([], [], False, "high", "main")
    assert ok and "| Code smells (complexity, nesting, duplication) | ✅ none |" in text and "at high and above" in text


@pytest.fixture
def fake_qlty(tmp_path, monkeypatch):
    """A `qlty` on PATH that logs its arguments and prints the output its JSON file holds for
    the command ("smells-base" for smells without --upstream). Returns a function that sets
    those outputs, and the log."""
    bin_dir, log, canned = tmp_path / "bin", tmp_path / "qlty.log", tmp_path / "qlty.json"
    bin_dir.mkdir()
    script = bin_dir / "qlty"
    script.write_text(f"#!{sys.executable}\nimport json, sys\n"
                      f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                      "base = '-base' if sys.argv[1] == 'smells' and '--upstream' not in sys.argv else ''\n"
                      f"sys.stdout.write(json.load(open({str(canned)!r}))[sys.argv[1] + base])\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    def outputs(**commands):
        canned.write_text(json.dumps({"init": INIT, "check": CHECK, "smells": SMELLS, "smells-base": BASE_SMELLS,
                                      **commands}))
    outputs()
    return outputs, log


def scan(*args):
    return subprocess.run([sys.executable, q.__file__, "--base", "main", *args], capture_output=True, text=True)


def test_scan_writes_the_config_compares_with_the_merge_base_and_fails_on_new_issues(repo, fake_qlty, tmp_path):
    _, log = fake_qlty
    (repo / "README.md").write_text("# App\n\nChanged.\n")
    subprocess.run(["git", "commit", "-qam", "change the readme"], check=True)
    merge_base = subprocess.run(["git", "merge-base", "origin/main", "HEAD"], capture_output=True, text=True,
                                check=True).stdout.strip()
    r = scan("--report", str(tmp_path / "report.md"))
    assert r.returncode == 1, r.stderr
    assert "::warning file=app/calc.py,line=31::bandit:B602: subprocess call" in r.stdout
    assert "qlty:function-complexity" in r.stdout and "qlty:function-parameters" not in r.stdout
    assert q.plugins((repo / ".qlty" / "qlty.toml").read_text()) == ["bandit", "radarlint-python", "ruff"]
    calls = log.read_text().splitlines()
    assert calls[0] == "init --yes --dry-run --no-upgrade-check"
    assert all(f"--upstream {merge_base}" in c for c in calls[1:3]) and "--level medium" in calls[1]
    assert calls[3] == "smells --sarif --quiet README.md --no-upgrade-check"   # the merge-base, in a worktree
    assert subprocess.run(["git", "worktree", "list"], capture_output=True, text=True).stdout.count("\n") == 1
    assert "**Result: FAILED**" in (tmp_path / "report.md").read_text()


def test_scan_keeps_the_repositorys_config_and_passes_without_new_issues(repo, fake_qlty):
    outputs, log = fake_qlty
    outputs(check=EMPTY, smells=EMPTY)
    (repo / ".qlty").mkdir()
    (repo / ".qlty" / "qlty.toml").write_text('config_version = "0"\n\n[[plugin]]\nname = "ruff"\n')
    r = scan("--level", "high")
    assert r.returncode == 0, r.stderr
    assert "**Result: PASSED**" in r.stdout and "| Lint (ruff) | ✅ none |" in r.stdout
    assert not log.read_text().startswith("init")
