#!/usr/bin/env python3
"""Security scan for a feature branch: secrets (gitleaks) and vulnerable
dependencies and misconfigurations (Trivy). Only what the branch adds fails
the scan. Findings already on the base branch are reported but don't block, so
a repository with old problems can still ship clean features.

    security_scan.py --base main [--severity HIGH,CRITICAL] [--report path]

- Secrets: gitleaks scans only the branch's own commits (merge-base..HEAD), so
  every finding is new. Accept one by adding its fingerprint to .gitleaksignore.
- Dependencies and misconfigurations: Trivy scans the branch and the merge-base.
  A finding blocks when the base doesn't have it. Vulnerabilities count only at
  the given severities and when a fixed version exists. Accept one by adding
  its ID to .trivyignore.

Standard library only. Expects gitleaks and trivy on PATH, and full history
(actions/checkout with fetch-depth: 0).
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def run(*cmd, check=True, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)
    if check and r.returncode != 0:
        sys.exit(f"{' '.join(cmd)} failed ({r.returncode}):\n{r.stderr.strip() or r.stdout.strip()}")
    return r


def git(*args, check=True):
    return run("git", "-c", "core.quotePath=false", *args, check=check).stdout.strip()


def base_commit(base):
    ref = f"origin/{base}"
    if git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False) == "":
        git("fetch", "-q", "--no-tags", "origin", f"+refs/heads/{base}:refs/remotes/{ref}")
    return git("merge-base", ref, "HEAD")


def secrets(merge_base, workdir):
    out = Path(workdir, "gitleaks.json")
    if git("rev-list", "--count", f"{merge_base}..HEAD") == "0":
        return []
    r = run("gitleaks", "git", "--no-banner", "--redact", "--exit-code", "0",
            "--log-opts", f"{merge_base}..HEAD", "--report-format", "json", "--report-path", str(out), ".",
            check=False)
    if r.returncode != 0:
        sys.exit(f"gitleaks failed ({r.returncode}):\n{r.stderr.strip()}")
    return json.loads(out.read_text() or "[]") if out.exists() else []


def trivy(target, output, severity, ignorefile):
    cmd = ["trivy", "fs", "--quiet", "--scanners", "vuln,misconfig", "--severity", severity,
           "--ignore-unfixed", "--format", "json", "--output", str(output)]
    if ignorefile:
        cmd += ["--ignorefile", str(ignorefile)]
    r = run(*cmd, str(target), check=False)
    if r.returncode != 0:
        sys.exit(f"trivy failed on {target} ({r.returncode}):\n{r.stderr.strip()}")
    return json.loads(output.read_text()).get("Results") or []


def findings(results):
    """Trivy's results as {key: finding}. Keys leave out versions and line
    numbers, so a bumped package or a moved line isn't a new finding."""
    found = {}
    for res in results:
        target = res.get("Target", "")
        for v in res.get("Vulnerabilities") or []:
            key = ("vuln", target, v.get("PkgName"), v.get("VulnerabilityID"))
            found[key] = {"kind": "vuln", "target": target, "id": v.get("VulnerabilityID"),
                          "pkg": v.get("PkgName"), "installed": v.get("InstalledVersion"),
                          "fixed": v.get("FixedVersion"), "severity": v.get("Severity"),
                          "title": v.get("Title") or "", "url": v.get("PrimaryURL") or ""}
        for m in res.get("Misconfigurations") or []:
            if m.get("Status", "FAIL") != "FAIL":
                continue
            key = ("misconfig", target, m.get("ID"))
            found[key] = {"kind": "misconfig", "target": target, "id": m.get("ID"),
                          "severity": m.get("Severity"), "title": m.get("Title") or "",
                          "message": m.get("Message") or "", "url": m.get("PrimaryURL") or ""}
    return found


NEW_ROWS, OLD_ROWS = 100, 50   # keeps the report, and the pull request body it goes into, a sane size


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def capped(rows, limit, what):
    if len(rows) <= limit:
        return rows
    return rows[:limit] + ["", f"_…and {len(rows) - limit} more {what}; the job log has them all._"]


def report(new_secrets, new, old, severity, base):
    ok = not new_secrets and not new
    vulns = [f for f in new if f["kind"] == "vuln"]
    misconfigs = [f for f in new if f["kind"] == "misconfig"]
    lines = ["# Security scan", f"**Result: {'PASSED' if ok else 'FAILED'}**", "",
             f"Only what this branch adds can fail the scan. Compared with `{base}`; "
             f"vulnerabilities and misconfigurations at {severity.replace(',', ', ')}, "
             "vulnerabilities only when a fixed version exists.", "",
             "| Check | New on this branch | Already on the base |", "| :-- | :-- | :-- |",
             f"| Secrets (gitleaks, this branch's commits) | {'✅ none' if not new_secrets else f'❌ {len(new_secrets)}'} | not scanned |",
             f"| Vulnerable dependencies (Trivy) | {'✅ none' if not vulns else f'❌ {len(vulns)}'} | "
             f"{sum(1 for f in old if f['kind'] == 'vuln')} |",
             f"| Misconfigurations (Trivy) | {'✅ none' if not misconfigs else f'❌ {len(misconfigs)}'} | "
             f"{sum(1 for f in old if f['kind'] == 'misconfig')} |", ""]
    if new_secrets:
        lines += ["## New secrets", "", "| Rule | Where | Commit | Fingerprint |", "| :-- | :-- | :-- | :-- |"]
        lines += capped([f"| {cell(s.get('RuleID'))} | `{cell(s.get('File'))}:{s.get('StartLine')}` | "
                         f"`{str(s.get('Commit', ''))[:7]}` | `{cell(s.get('Fingerprint'))}` |" for s in new_secrets],
                        NEW_ROWS, "secrets")
        lines += ["", "Remove the secret from the branch's history and rotate it. If it is a false positive, "
                  "add its fingerprint to `.gitleaksignore`.", ""]
    if vulns:
        lines += ["## New vulnerable dependencies", "",
                  "| Severity | Package | Installed | Fixed in | ID | File |", "| :-- | :-- | :-- | :-- | :-- | :-- |"]
        lines += capped([f"| {f['severity']} | {cell(f['pkg'])} | {cell(f['installed'])} | {cell(f['fixed'])} | "
                         f"[{f['id']}]({f['url']}) | `{cell(f['target'])}` |" for f in vulns],
                        NEW_ROWS, "vulnerabilities")
        lines += ["", "Upgrade to the fixed version. To accept one, add its ID to `.trivyignore` with a comment "
                  "saying why.", ""]
    if misconfigs:
        lines += ["## New misconfigurations", "", "| Severity | ID | File | Issue |", "| :-- | :-- | :-- | :-- |"]
        lines += capped([f"| {f['severity']} | [{f['id']}]({f['url']}) | `{cell(f['target'])}` | "
                         f"{cell(f['title'])}: {cell(f['message'])} |" for f in misconfigs],
                        NEW_ROWS, "misconfigurations")
        lines += ["", "Fix the file, or add the ID to `.trivyignore` with a comment saying why.", ""]
    if old:
        lines += ["<details><summary>Already on the base, not blocking "
                  f"({len(old)})</summary>", "", "| Severity | Kind | ID | Where |", "| :-- | :-- | :-- | :-- |"]
        lines += capped([f"| {f['severity']} | {f['kind']} | {f['id']} | `{cell(f['target'])}`"
                         + (f" ({cell(f['pkg'])} {cell(f['installed'])})" if f["kind"] == "vuln" else "") + " |"
                         for f in sorted(old, key=lambda f: (f["kind"], f["target"], f["id"]))],
                        OLD_ROWS, "findings")
        lines += ["", "</details>", ""]
    return ok, "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", required=True, help="branch the feature goes into, e.g. main")
    ap.add_argument("--severity", default="HIGH,CRITICAL")
    ap.add_argument("--report", help="write the Markdown report here too")
    args = ap.parse_args()

    merge_base = base_commit(args.base)
    print(f"Scanning {git('rev-parse', '--short', 'HEAD')} against {args.base} "
          f"(merge-base {merge_base[:7]}).", flush=True)
    ignorefile = Path(".trivyignore").resolve() if Path(".trivyignore").exists() else None
    with tempfile.TemporaryDirectory() as tmp:
        new_secrets = secrets(merge_base, tmp)
        head = findings(trivy(Path.cwd(), Path(tmp, "head.json"), args.severity, ignorefile))
        worktree = Path(tmp, "base")
        git("worktree", "add", "-q", "--detach", str(worktree), merge_base)
        try:
            base = findings(trivy(worktree, Path(tmp, "base.json"), args.severity, ignorefile))
        finally:
            git("worktree", "remove", "--force", str(worktree), check=False)
    new = [f for k, f in head.items() if k not in base]
    old = [f for k, f in head.items() if k in base]
    ok, text = report(new_secrets, new, old, args.severity, args.base)

    print(text)
    for f in new:   # the report may be capped; the log lists every new finding
        print(f"::warning::new {f['kind']} {f['id']} ({f['severity']}) in {f['target']}")
    if args.report:
        Path(args.report).write_text(text + "\n", encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(text + "\n")
    if not ok:
        print("::error::The security scan found problems this branch adds; see the report above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
