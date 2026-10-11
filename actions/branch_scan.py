"""What the branch scans (security-scan, quality-scan) share: git, the merge-base a
branch is compared with, and the Markdown report each one prints, writes and adds
to the step summary. Standard library only."""
import os
import subprocess
import sys
from pathlib import Path

NEW_ROWS, OLD_ROWS = 100, 50   # keeps a report, and the pull request body it goes into, a sane size


def run(*cmd, check=True, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if check and r.returncode != 0:
        sys.exit(f"{' '.join(cmd)} failed ({r.returncode}):\n{r.stderr.strip() or r.stdout.strip()}")
    return r


def git(*args, check=True):
    return run("git", "-c", "core.quotePath=false", *args, check=check).stdout.strip()


def base_commit(base):
    """Where the branch left `base`: only what it adds since then counts."""
    ref = f"origin/{base}"
    if git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False) == "":
        git("fetch", "-q", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/{ref}")
    return git("merge-base", ref, "HEAD")


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def table(columns, rows, limit, what):
    lines = ["| " + " | ".join(columns) + " |", "|" + " :-- |" * len(columns)]
    lines += ["| " + " | ".join(row) + " |" for row in rows[:limit]]
    if len(rows) > limit:
        lines += ["", f"_…and {len(rows) - limit} more {what}; the job log has them all._"]
    return lines


def count(items):
    return f"❌ {len(items)}" if items else "✅ none"


def publish(text, report=None):
    """Print the report, write it to `report` and add it to the step summary."""
    print(text)
    if report:
        Path(report).write_text(text + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")
