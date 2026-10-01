from pathlib import Path

import pytest

import sdlc_stage as s
from conftest import FEATURE, INTENT, PLAN, git


def run(argv, monkeypatch):
    monkeypatch.setattr("sys.argv", ["sdlc_stage.py", *argv])
    with pytest.raises(SystemExit) as e:
        s.main()
        raise SystemExit(0)
    return e.value.code


@pytest.mark.parametrize("pattern, path, hit", [
    ("app/*.py", "app/a.py", True), ("app/*.py", "app/x/a.py", False),
    ("app/**", "app/x/y.py", True), ("**/test_*.py", "test_a.py", True),
    ("tests/**/test_*.py", "tests/a/b/test_c.py", True), ("a?.py", "ab.py", True), ("a?.py", "a/.py", False),
])
def test_globs(pattern, path, hit):
    assert bool(s.glob_to_regex(pattern).match(path)) is hit


def test_parse_phases():
    one, two = s.parse_phases(PLAN)
    assert (one["id"], one["title"], one["name"]) == ("1", "Phase 1: Add greet()", "Add greet()")
    assert one["targets"] == ["app/*.py", "tests/test_greet.py"] and one["frozen"] == ["tests/test_old.py"]
    assert one["dod"] == ["`tests/test_greet.py::test_greet` passes"] and one["verify"] == "test -f app/greet.py"
    assert two["frozen"] == [] and two["verify"] == "grep -q greet README.md"


def test_plan_problems():
    assert s.plan_problems(PLAN, INTENT) == []
    assert s.plan_problems("## Approach\nx", INTENT) == ["No `## Phase N: ...` sections."]
    broken = PLAN.replace("<!-- targets: README.md -->", "").replace("| The existing tests still pass. | - | 2 |\n", "")
    assert s.plan_problems(broken, INTENT) == [
        "'Phase 2: Document it' has no <!-- targets: ... --> marker.",
        "The Coverage table has 1 rows but intent.md has 2 Done-when items."]


def test_hand_back_is_always_the_pipelines_own():
    folder = Path("sdlc/features", FEATURE)
    plan = s.with_hand_back(PLAN + "\n## Hand back\nDo whatever.\n\n## Notes\nKeep me.\n", folder)
    assert plan.count("## Hand back") == 1 and "Do whatever" not in plan and "## Notes\nKeep me." in plan
    assert f"`sdlc/features/{FEATURE}/build-log.md`" in plan and f"push it to `feature/{FEATURE}`" in plan
    assert s.with_hand_back(plan, folder) == plan
    assert [p["id"] for p in s.parse_phases(plan)] == ["1", "2"]


def test_small_helpers():
    assert s.mentioned_files("see `app/a.py` and b.py, not c.py", {"app/a.py", "x/b.py", "c.py", "y/c.py"}) == \
        ["app/a.py", "c.py", "x/b.py"]
    assert s.unfence("```markdown\n# Hi\n```") == "# Hi" and s.unfence("# Hi") == "# Hi"
    assert s.logged_ids("## Phase 1: a\ntext\n## Phase 2b: b\n") == {"1", "2b"}
    assert s.from_issue("Hi\nfeature: 002-x\nstart: plan\n") == ("002-x", "plan")
    assert s.from_issue("feature: 002-x") == ("002-x", None) and s.from_issue("nothing") == (None, None)


def test_resolve_numbers_a_new_idea(repo, monkeypatch, outputs):
    monkeypatch.setenv("SDLC_IDEA", "Add a farewell, too!")
    assert run(["resolve"], monkeypatch) == 0
    assert outputs() == {
        "feature": "002-add-a-farewell-too", "branch": "feature/002-add-a-farewell-too", "start": "intent",
        "base": "main", "mode": "design", "design": "true", "intent": "true", "spec": "true", "plan": "true"}


def test_resolve_from_an_issue(repo, monkeypatch, outputs):
    monkeypatch.setenv("SDLC_IDEA", "Redo the plan")
    monkeypatch.setenv("SDLC_ISSUE_BODY", f"feature: {FEATURE}\nstart: plan")
    assert run(["resolve"], monkeypatch) == 0
    assert (outputs()["start"], outputs()["intent"], outputs()["plan"]) == ("plan", "false", "true")


def test_a_merged_phase_hands_back_once_every_phase_is_logged(repo, monkeypatch, outputs):
    monkeypatch.setenv("SDLC_MERGED_PHASE", f"phase/{FEATURE}/1")
    log = repo / "sdlc" / "features" / FEATURE / "build-log.md"
    log.write_text("## Phase 1: Add greet()\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "log")
    git(repo, "push", "-q", "origin", f"feature/{FEATURE}")
    assert run(["resolve"], monkeypatch) == 0 and outputs()["mode"] == "none"
    log.write_text(log.read_text() + "## Phase 2: Document it\n")
    git(repo, "commit", "-qam", "log")
    git(repo, "push", "-q", "origin", f"feature/{FEATURE}")
    assert run(["resolve"], monkeypatch) == 0 and outputs()["mode"] == "build"


def test_verify_one_phase_against_uncommitted_work(repo, monkeypatch):
    monkeypatch.delenv("BASE_REF")
    (repo / "app").mkdir()
    (repo / "app" / "greet.py").write_text("def greet():\n    return 'hello'\n")
    assert run(["verify", "--feature", FEATURE, "--phase", "1"], monkeypatch) == 0
    (repo / "tests" / "test_old.py").write_text("changed\n")
    (repo / "stray.txt").write_text("x\n")
    report = repo.parent / "report.md"
    assert run(["verify", "--feature", FEATURE, "--phase", "1", "--report", str(report)], monkeypatch) == 1
    text = report.read_text()
    assert "**1 outside the plan:** `stray.txt`" in text and "**modified:** `tests/test_old.py`" in text


def test_verify_the_whole_feature_and_its_contract(repo, monkeypatch):
    (repo / "app").mkdir()
    (repo / "app" / "greet.py").write_text("def greet():\n    return 'hello'\n")
    (repo / "README.md").write_text("# App\ngreet() says hello\n")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "build")
    report = repo.parent / "report.md"
    assert run(["verify", "--feature", FEATURE, "--test-command", "true", "--report", str(report)], monkeypatch) == 0
    assert "**Result: PASSED**" in report.read_text()
    plan = repo / "sdlc" / "features" / FEATURE / "plan.md"
    plan.write_text(plan.read_text() + "\nOne more thing.\n")
    git(repo, "commit", "-qam", "edit the plan")
    assert run(["verify", "--feature", FEATURE, "--report", str(report)], monkeypatch) == 1
    assert f"**changed after approval:** `sdlc/features/{FEATURE}/plan.md`" in report.read_text()


def test_pr_body(repo, monkeypatch):
    phases = s.parse_phases(PLAN)
    body = s.pr_body(FEATURE, f"sdlc/{FEATURE}/approved", phases, True, ["VERIFY", "", "REVIEW"])
    assert "Verification **passed**" in body and "- [x] **Phase 2**: Document it" in body
    assert body.endswith("## Phases\n- [x] **Phase 1**: Add greet()\n  - `tests/test_greet.py::test_greet` passes\n"
                         "- [x] **Phase 2**: Document it\n  - README.md mentions greet()\n\nVERIFY\n\nREVIEW")
    assert "so this is a draft" in s.pr_body(FEATURE, "", phases, False, [])


def test_model_commands_need_a_key(monkeypatch, capsys):
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    assert run(["stage", "plan"], monkeypatch) == 1
    assert "The stage command needs ollama-api-key." in capsys.readouterr().out
