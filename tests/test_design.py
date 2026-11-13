"""Design references (`design: docs/x.md#section`), facts from the code, and the
Phase check that runs one phase's Verify block with the type-check."""
import json
from pathlib import Path

import pytest

import sdlc_stage as s
from conftest import FEATURE, git
from test_sdlc_stage import run

DESIGN = """# Team tasks

## Rules for every piece
- Keep the table.

## Piece 4: task dialog
Old piece.

## Piece 5: create tasks from Messages (admins only)
Assign work without leaving the conversation.

### NewTaskDialog
Title, Details, Assignee.

```markdown
## Not a heading: inside a fence
```

## Piece 6: board
Later.
"""


def test_design_refs_and_sections():
    assert s.design_refs("Build it\ndesign: docs/d.md#piece-5\n  design: docs/d.md  \ndesign: docs/d.md#piece-5\n") \
        == ["docs/d.md#piece-5", "docs/d.md"]
    assert s.slug("Piece 5: create tasks from Messages (admins only)") == "piece-5-create-tasks-from-messages-admins-only"
    piece = s.design_section(DESIGN, "piece-5")
    assert piece.startswith("## Piece 5: create tasks") and "### NewTaskDialog" in piece
    assert "## Not a heading: inside a fence" in piece and "Piece 6" not in piece and "Old piece" not in piece
    assert s.design_section(DESIGN, "piece-5-create-tasks-from-messages-admins-only") == piece
    assert s.design_section(DESIGN, "rules-for-every-piece") == "## Rules for every piece\n- Keep the table.\n"
    assert s.design_section(DESIGN, "piece-6").endswith("Later.\n")
    assert s.design_section(DESIGN, "piece-50") is None
    assert s.design_section(DESIGN, "piece") is None   # starts three headings: not a reference to one


def test_intent_names_its_design_and_keeps_one_section():
    refs = ["docs/d.md#rules-for-every-piece", "docs/d.md#piece-5"]
    intent = s.with_design("# Intent: X\n\n## Done when\n- a\n\n## Design\nmodel's own\n", refs)
    assert intent.count("## Design") == 1 and "model's own" not in intent
    assert s.refs_in_intent(intent) == refs
    assert s.with_design(intent, refs) == intent
    assert s.refs_in_intent(s.with_design(intent, [])) == [] and s.refs_in_intent("# Intent") == []


@pytest.fixture
def design_repo(repo):
    """The repository, with a design document and code on main."""
    (repo / "docs").mkdir()
    (repo / "docs" / "d.md").write_text(DESIGN)
    (repo / "web" / "src" / "lib").mkdir(parents=True)
    (repo / "web" / "src" / "lib" / "auth.ts").write_text(
        'export type UserRole = "admin" | "marketing" | "venue_staff";\n\n'
        "export interface CurrentUser {\n  id: number;\n  role: UserRole;\n}\n\n"
        "export function useCurrentUser(): { user: CurrentUser | undefined } {\n  return { user: undefined };\n}\n"
        "function internal() {}\n")
    (repo / "api").mkdir()
    (repo / "api" / "events.ts").write_text(
        'router.get("/events", async (req, res): Promise<void> => {\n'
        "  res.json({\n    data: events,\n    total: count,\n  });\n});\n")
    git(repo, "switch", "-q", "main")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "design and code")
    git(repo, "push", "-q", "origin", "main")
    return repo


def test_resolve_takes_design_lines_and_checks_them(design_repo, monkeypatch, outputs, capsys):
    monkeypatch.setenv("SDLC_IDEA", "Team tasks piece 5")
    monkeypatch.setenv("SDLC_ISSUE_BODY", "design: docs/d.md#rules-for-every-piece\ndesign: docs/d.md#piece-5")
    assert run(["resolve"], monkeypatch) == 0
    assert outputs()["design-docs"] == "docs/d.md#rules-for-every-piece docs/d.md#piece-5"
    assert outputs()["start"] == "intent"
    monkeypatch.setenv("SDLC_ISSUE_BODY", "design: docs/d.md#piece-9")
    assert run(["resolve"], monkeypatch) == 1
    assert "docs/d.md has no heading for #piece-9, or more than one starts with it" in capsys.readouterr().out
    monkeypatch.setenv("SDLC_ISSUE_BODY", "")
    monkeypatch.setenv("SDLC_DESIGN", "docs/missing.md")
    assert run(["resolve"], monkeypatch) == 1
    assert "The design document docs/missing.md" in capsys.readouterr().out


def test_the_stages_quote_the_design_and_add_the_facts(design_repo, monkeypatch):
    files = set(git(design_repo, "ls-files").splitlines())
    folder = Path("sdlc/features", FEATURE)
    monkeypatch.setenv("SDLC_DESIGN", "docs/d.md#piece-5")
    parts, sources = s.prompt_parts("intent", folder, files, 60000, "Team tasks piece 5")
    text = "\n\n".join(parts)
    assert "## Design: docs/d.md#piece-5 (word for word" in text and "### NewTaskDialog\nTitle, Details" in text
    assert "Piece 6" not in text and "### docs/d.md\n" not in text and "docs/d.md#piece-5" in sources

    # The spec and plan stages read the design from intent.md, and get the code's facts.
    monkeypatch.delenv("SDLC_DESIGN")
    intent = s.with_design("# Intent: Tasks\n\nUse `@/lib/auth` and `api/events.ts`.\n", ["docs/d.md#piece-5"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "intent.md").write_text(intent)
    (folder / "spec.md").write_text("## Behaviour\n1. It works.\n")
    parts, sources = s.prompt_parts("plan", folder, files, 60000, "")
    text = "\n\n".join(parts)
    assert "## Design: docs/d.md#piece-5" in text and "### docs/d.md\n" not in text
    facts = text.split("## Facts from the code", 1)[1].split("## Repository files", 1)[0]
    assert 'export type UserRole = "admin" | "marketing" | "venue_staff";' in facts
    assert "export interface CurrentUser {\n  id: number;\n  role: UserRole;\n}" in facts
    assert "export function useCurrentUser(): { user: CurrentUser | undefined }" in facts and "internal" not in facts
    assert 'router.get("/events" …' in facts and "res.json({\ndata: events,\ntotal: count,\n});" in facts
    assert "Facts from the code" not in "\n".join(s.prompt_parts("intent", folder, files, 60000, "x")[0])


def test_module_paths_name_code_files():
    files = {"web/src/lib/auth.ts", "api/src/lib/auth.ts", "web/src/lib/api-request.ts", "web/src/ui/index.tsx",
             "a/lib/x.ts", "b/lib/x.ts", "c/lib/x.ts", "d/lib/x.ts", "docs/team-tasks.md"}
    assert s.module_files("`@/lib/api-request` and src/ui and docs/team-tasks.md", files) == \
        ["web/src/lib/api-request.ts", "web/src/ui/index.tsx"]
    assert s.module_files("@/lib/auth", files) == ["api/src/lib/auth.ts", "web/src/lib/auth.ts"]
    assert s.module_files("lib/x", files) == []   # four files: too vague to name any of them
    assert s.module_files("/api/team-tasks/people and https://x.org/a/b", files) == []


def test_python_facts(tmp_path):
    path = tmp_path / "m.py"
    path.write_text("import os\n\ndef greet(name: str,\n          loud: bool = False) -> str:\n    return name\n\n"
                    "class Repo:\n    def get(self, id: int) -> dict:\n        def inner():\n            pass\n")
    assert s.code_facts(str(path)) == ["def greet(name: str,", "          loud: bool = False) -> str",
                                       "class Repo", "    def get(self, id: int) -> dict"]


def test_one_phase_runs_its_block_and_the_typecheck(repo, monkeypatch):
    monkeypatch.delenv("BASE_REF")
    # Phase 2 alone: README.md is its target. Phase 1's block (test -f app/greet.py) would fail.
    (repo / "README.md").write_text("# App\ngreet\n")
    report = repo.parent / "report.md"
    args = ["verify", "--feature", FEATURE, "--phase", "2", "--test-command", "false", "--report", str(report)]
    assert run(args, monkeypatch) == 0
    text = report.read_text()
    assert "| Phase 2: Document it | ✅ passed |" in text and "Phase 1" not in text and "Whole test suite" not in text
    assert "Type-check" not in text   # a Python repository has none to find
    assert run([*args, "--typecheck-command", "false"], monkeypatch) == 1
    assert "| Type-check | ❌ exit 1 |" in report.read_text()
    assert run([*args, "--typecheck-command", "none"], monkeypatch) == 0
    assert run([*args, "--full"], monkeypatch) == 1
    text = report.read_text()
    assert "| Whole test suite | ❌ exit 1 |" in text and "| Phase 1: Add greet() | ❌ exit 1 |" in text


@pytest.mark.parametrize("files, command", [
    ({}, ""),
    ({"requirements.txt": ""}, ""),
    ({"package.json": {}}, ""),
    ({"package.json": {}, "tsconfig.json": "{}"}, "npx tsc --noEmit"),
    ({"package.json": {}, "tsconfig.json": '{"files": [], "references": [{"path": "./a"}]}'}, "npx tsc --build"),
    ({"package.json": {"scripts": {"typecheck": "tsc"}}, "pnpm-lock.yaml": "", "tsconfig.json": "{}"},
     "pnpm run typecheck"),
    ({"package.json": {"scripts": {"typecheck": "tsc"}}, "yarn.lock": ""}, "yarn typecheck"),
    ({"package.json": {"scripts": {"typecheck": "tsc"}}}, "npm run typecheck"),
])
def test_typecheck_command(tmp_path, files, command):
    for name, text in files.items():
        (tmp_path / name).write_text(text if isinstance(text, str) else json.dumps(text))
    assert s.typecheck_command(tmp_path) == command


def test_typescript_facts_drop_comments_and_keep_short_values(tmp_path):
    path = tmp_path / "schema.ts"
    path.write_text(
        'export const teamTaskStatus = pgEnum("team_task_status", [\n  "todo",\n  "done",\n]);\n'
        "export const bigTable = pgTable(\"t\", {\n" + "".join(f"  col{i}: text(\"c{i}\"),\n" for i in range(40))
        + "});\n"
        "export interface User {\n  // who they are\n  id: number; // the key\n  /* the role */\n  role: string;\n}\n"
        'const url = "https://x.org"; // not exported\n'
        'res.status(403).json({ error: "no_account" });\n')
    facts = s.code_facts(str(path))
    assert facts[:5] == ['export const teamTaskStatus = pgEnum("team_task_status", [', '  "todo",', '  "done",', "]);",
                         "export const bigTable = …"]
    assert "export interface User {\n  id: number;\n  role: string;\n}" in "\n".join(facts)
    assert 'res.status(403).json({ error: "no_account" });' in facts and not any("who they are" in f for f in facts)


def test_module_paths_prefer_the_package_the_other_files_are_in():
    files = {"web/src/lib/auth.ts", "api/src/lib/auth.ts", "ios/src/lib/auth.ts", "web/src/pages/a.tsx"}
    assert s.module_files("@/lib/auth", files) == ["api/src/lib/auth.ts", "ios/src/lib/auth.ts", "web/src/lib/auth.ts"]
    assert s.module_files("@/lib/auth", files, near=["web/src/pages/a.tsx"]) == ["web/src/lib/auth.ts"]
