#!/usr/bin/env python3
"""Quality scan for a branch with Qlty CLI: the lint issues and code smells
(complexity, nesting, duplication) the branch adds. Only those fail the scan,
so a repository with old issues can still ship clean features.

    quality_scan.py --base main [--level medium] [--report path]

- Lint: `qlty check --upstream <merge-base>` runs the linters `.qlty/qlty.toml`
  enables and reports the issues on lines the branch changed. A repository
  without that file gets the configuration `qlty init` would write, less the
  changes generated_config() lists.
- Smells: `qlty smells` on the files the branch changed (tests left out), on
  the branch and at the merge-base. A smell is new when the merge-base doesn't
  have it (see smell_key()).

An issue counts at --level (low, medium or high) and above. Accept one with a
`[[triage]]` entry in `.qlty/qlty.toml`.

Standard library only. Expects qlty on PATH, and full history
(actions/checkout with fetch-depth: 0).
"""
import argparse
import json
import re
import sys
import tempfile
import tomllib
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from branch_scan import NEW_ROWS, base_commit, cell, count, git, publish, run, table  # noqa: E402

CONFIG = Path(".qlty/qlty.toml")
LEVELS = ("low", "medium", "high")
SARIF_LEVELS = {"note": "low", "warning": "medium", "error": "high"}   # how Qlty writes its levels to SARIF
DROPPED = {"osv-scanner", "trufflehog",   # the security scan already checks dependencies and secrets
           "ripgrep"}                     # flags TODO comments
BANDIT = """
# Added by the quality scan: asserts are the point of a test, and importing or
# running subprocess without a shell (B404, B603) isn't an issue itself; B602,
# B604 and B605 still flag running a shell.
[[exclude]]
plugins = ["bandit"]
file_patterns = ["**/test/**", "**/tests/**", "**/test_*.*", "**/*_test.*"]

[[triage]]
match.rules = ["bandit:B404", "bandit:B603"]
set.ignored = true
"""


def plugin_name(section):
    if section.startswith("[[plugin]]"):
        m = re.search(r'^name\s*=\s*"([^"]+)"', section, flags=re.MULTILINE)
        return m and m.group(1)
    return None


def generated_config(init):
    """`qlty init`'s configuration without the plugins in DROPPED. With bandit
    enabled, bandit also skips tests and ignores B404 and B603 (see BANDIT)."""
    sections = [s for s in re.split(r"(?m)^(?=\[)", init) if plugin_name(s) not in DROPPED]
    return "".join(sections) + (BANDIT if any(plugin_name(s) == "bandit" for s in sections) else "")


def plugins(config):
    return [p["name"] for p in tomllib.loads(config).get("plugin", []) if p.get("mode") != "disabled"]


def issues(sarif, kind, level):
    """Qlty's SARIF results at `level` and above, as plain dicts sorted by place."""
    found = []
    for r in json.loads(sarif).get("runs") or []:
        docs = {rule["id"]: rule.get("helpUri", "") for rule in r.get("tool", {}).get("driver", {}).get("rules") or []}
        for res in r.get("results") or []:
            where = ((res.get("locations") or [{}])[0]).get("physicalLocation", {})
            found.append({"kind": kind, "rule": res.get("ruleId", ""),
                          "level": SARIF_LEVELS.get(res.get("level"), "medium"),
                          "file": where.get("artifactLocation", {}).get("uri", ""),
                          "line": where.get("region", {}).get("startLine") or 1,
                          "message": res.get("message", {}).get("text", ""), "url": docs.get(res.get("ruleId"), "")})
    return sorted((i for i in found if LEVELS.index(i["level"]) >= LEVELS.index(level)),
                  key=lambda i: (i["file"], i["line"], i["rule"]))


def smell_key(smell):
    """A smell without its line or counts, so one that only moved or grew isn't new."""
    return smell["rule"], smell["file"], re.sub(r"\d+", "#", smell["message"])


def new_smells(head, base):
    """The smells on the branch that the merge-base doesn't have. When a file has
    more of one smell than before, the later ones count as new."""
    before = Counter(smell_key(s) for s in base)
    new = []
    for smell in head:
        if before[smell_key(smell)]:
            before[smell_key(smell)] -= 1
        else:
            new.append(smell)
    return new


def report(found, enabled, generated, level, base):
    lint, smells = ([i for i in found if i["kind"] == kind] for kind in ("lint", "smell"))
    ok = not found
    config = ("The repository has no `.qlty/qlty.toml`, so the linters are the ones `qlty init` picks for it, "
              "less those the security scan covers; commit one to choose them yourself." if generated else
              "The linters are the ones the repository's `.qlty/qlty.toml` enables.")
    lines = ["# Quality scan", f"**Result: {'PASSED' if ok else 'FAILED'}**", "",
             f"Only what this branch adds since it left `{base}` can fail the scan: [Qlty](https://qlty.sh) lint "
             "issues on the lines it changed, and code smells in the files it changed that weren't there before, "
             f"at {level} and above. {config}", ""]
    lines += table(["Check", "New on this branch"], [
        [f"Lint ({', '.join(enabled) or 'no linters enabled'})", count(lint)],
        ["Code smells (complexity, nesting, duplication)", count(smells)]], 2, "checks") + [""]
    sections = [
        ("New lint issues", "Rule", lint, "lint issues",
         "Fix them. If a rule is wrong for this repository, ignore it with a `[[triage]]` entry in `.qlty/qlty.toml`, "
         "with a comment saying why."),
        ("New code smells", "Smell", smells, "code smells",
         "Split the function or file, return early, or extract the repeated code."),
    ]
    for title, column, items, what, advice in sections:
        rows = [[i["level"], f"[{cell(i['rule'])}]({i['url']})" if i["url"] else cell(i["rule"]),
                 f"`{cell(i['file'])}:{i['line']}`", cell(i["message"])] for i in items]
        if rows:
            lines += [f"## {title}", "", *table(["Level", column, "Where", "Issue"], rows, NEW_ROWS, what), "",
                      advice, ""]
    return ok, "\n".join(lines)


def qlty(*args, cwd=None):
    return run("qlty", *args, "--no-upgrade-check", cwd=cwd).stdout


def base_smells(merge_base, config, level):
    """The smells at the merge-base, in the files the branch changed that were there
    already, with the branch's configuration."""
    files = git("diff", "--name-only", "--diff-filter=M", merge_base, "HEAD").splitlines()
    if not files:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        worktree = Path(tmp, "base")
        git("worktree", "add", "-q", "--detach", str(worktree), merge_base)
        try:
            (worktree / CONFIG).parent.mkdir(exist_ok=True)
            (worktree / CONFIG).write_text(config, encoding="utf-8")
            return issues(qlty("smells", "--sarif", "--quiet", *files, cwd=worktree), "smell", level)
        finally:
            git("worktree", "remove", "--force", str(worktree), check=False)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="branch the feature goes into, e.g. main")
    ap.add_argument("--level", default="medium", choices=LEVELS, help="lowest level that fails the scan")
    ap.add_argument("--report", help="write the Markdown report here too")
    args = ap.parse_args()

    merge_base = base_commit(args.base)
    print(f"Scanning {git('rev-parse', '--short', 'HEAD')} against {args.base} "
          f"(merge-base {merge_base[:7]}).", flush=True)
    generated = not CONFIG.exists()
    if generated:
        init = qlty("init", "--yes", "--dry-run")
        CONFIG.parent.mkdir(exist_ok=True)
        CONFIG.write_text(generated_config(init), encoding="utf-8")
    config = CONFIG.read_text(encoding="utf-8")
    print(f"::group::{CONFIG}{' (generated)' if generated else ''}\n{config}\n::endgroup::", flush=True)

    lint = issues(qlty("check", "--upstream", merge_base, "--sarif", "--no-progress", "--no-formatters",
                       "--no-fail", "--level", args.level), "lint", args.level)
    smells = new_smells(issues(qlty("smells", "--upstream", merge_base, "--sarif", "--quiet"), "smell", args.level),
                        base_smells(merge_base, config, args.level))
    ok, text = report(lint + smells, plugins(config), generated, args.level, args.base)

    publish(text, args.report)
    for i in lint + smells:   # the report may be capped; the annotations list every issue
        message = f"{i['rule']}: {i['message']}".replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::warning file={i['file']},line={i['line']}::{message}")
    if not ok:
        print("::error::The quality scan found issues this branch adds; see the report above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
