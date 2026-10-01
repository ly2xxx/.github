import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "actions" / "sdlc-stage"), str(ROOT / "actions" / "security-scan")]

FEATURE = "001-add-greeting"
INTENT = "# Intent: Add a greeting\n\n## Done when\n- greet() exists\n- The existing tests still pass.\n"
PLAN = """## Approach
Add one function.

## Coverage
| Done when (intent.md) | Spec behaviours | Phase |
| :-- | :-- | :-- |
| greet() exists | 1 | 1 |
| The existing tests still pass. | - | 2 |

## Phase 1: Add greet()
<!-- phase: 1 -->
<!-- targets: app/*.py, tests/test_greet.py -->
<!-- frozen: tests/test_old.py -->

**Definition of done:**
- [ ] `tests/test_greet.py::test_greet` passes

**Verify:**
```bash
test -f app/greet.py
```

## Phase 2: Document it
<!-- phase: 2 -->
<!-- targets: README.md -->

**Definition of done:**
- [ ] README.md mentions greet()

**Verify:**
```bash
grep -q greet README.md
```
"""


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone of a local origin whose main has one approved feature: intent,
    spec and plan on feature/<FEATURE>, tagged sdlc/<FEATURE>/approved."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(tmp_path, "clone", "-q", str(origin), str(work))
    for key, value in (("user.name", "t"), ("user.email", "t@example.com")):
        git(work, "config", key, value)
    (work / "tests").mkdir()
    (work / "tests" / "test_old.py").write_text("def test_old():\n    pass\n")
    (work / "README.md").write_text("# App\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "base")
    git(work, "push", "-q", "origin", "main")
    folder = work / "sdlc" / "features" / FEATURE
    folder.mkdir(parents=True)
    for name, text in (("intent", INTENT), ("spec", "## Behaviour\n1. greet() returns hello\n"), ("plan", PLAN)):
        (folder / f"{name}.md").write_text(text)
    git(work, "switch", "-q", "-c", f"feature/{FEATURE}")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "design")
    git(work, "tag", "-a", f"sdlc/{FEATURE}/approved", "-m", "approved")
    git(work, "push", "-q", "origin", f"feature/{FEATURE}", "--tags")
    monkeypatch.chdir(work)
    for name in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
        (tmp_path / name).write_text("")
        monkeypatch.setenv(name, str(tmp_path / name))
    for name in ("SDLC_IDEA", "SDLC_FEATURE", "SDLC_ISSUE_BODY", "SDLC_MERGED_PHASE", "SDLC_START", "BASE_REF",
                 "FEATURES_DIR", "TEST_COMMAND", "BUILD_WAIT_MINUTES"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BASE_REF", "main")
    return work


@pytest.fixture
def outputs(tmp_path):
    """The step outputs written so far, as a dict."""
    return lambda: dict(line.split("=", 1) for line in (tmp_path / "GITHUB_OUTPUT").read_text().splitlines() if line)
