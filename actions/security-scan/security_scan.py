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
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from branch_scan import NEW_ROWS, OLD_ROWS, base_commit, cell, count, git, publish, run, table  # noqa: E402


def secrets(merge_base, workdir):
    out = Path(workdir, "gitleaks.json")
    if git("rev-list", "--count", f"{merge_base}..HEAD") == "0":
        return []
    run("gitleaks", "git", "--no-banner", "--redact", "--exit-code", "0",
        "--log-opts", f"{merge_base}..HEAD", "--report-format", "json", "--report-path", str(out), ".")
    return json.loads(out.read_text() or "[]") if out.exists() else []


def trivy(target, output, severity, ignorefile):
    cmd = ["trivy", "fs", "--quiet", "--scanners", "vuln,misconfig", "--severity", severity,
           "--ignore-unfixed", "--format", "json", "--output", str(output)]
    if ignorefile:
        cmd += ["--ignorefile", str(ignorefile)]
    run(*cmd, str(target))
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


def report(new_secrets, new, old, severity, base):
    ok = not new_secrets and not new
    vulns = [f for f in new if f["kind"] == "vuln"]
    misconfigs = [f for f in new if f["kind"] == "misconfig"]
    old_count = {kind: sum(1 for f in old if f["kind"] == kind) for kind in ("vuln", "misconfig")}
    lines = ["# Security scan", f"**Result: {'PASSED' if ok else 'FAILED'}**", "",
             f"Only what this branch adds can fail the scan. Compared with `{base}`; "
             f"vulnerabilities and misconfigurations at {severity.replace(',', ', ')}, "
             "vulnerabilities only when a fixed version exists.", ""]
    lines += table(["Check", "New on this branch", "Already on the base"], [
        ["Secrets (gitleaks, this branch's commits)", count(new_secrets), "not scanned"],
        ["Vulnerable dependencies (Trivy)", count(vulns), str(old_count["vuln"])],
        ["Misconfigurations (Trivy)", count(misconfigs), str(old_count["misconfig"])]], 3, "checks") + [""]
    sections = [
        ("New secrets", ["Rule", "Where", "Commit", "Fingerprint"],
         [[cell(s.get("RuleID")), f"`{cell(s.get('File'))}:{s.get('StartLine')}`", f"`{str(s.get('Commit', ''))[:7]}`",
           f"`{cell(s.get('Fingerprint'))}`"] for s in new_secrets], "secrets",
         "Remove the secret from the branch's history and rotate it. If it is a false positive, "
         "add its fingerprint to `.gitleaksignore`."),
        ("New vulnerable dependencies", ["Severity", "Package", "Installed", "Fixed in", "ID", "File"],
         [[f["severity"], cell(f["pkg"]), cell(f["installed"]), cell(f["fixed"]), f"[{f['id']}]({f['url']})",
           f"`{cell(f['target'])}`"] for f in vulns], "vulnerabilities",
         "Upgrade to the fixed version. To accept one, add its ID to `.trivyignore` with a comment saying why."),
        ("New misconfigurations", ["Severity", "ID", "File", "Issue"],
         [[f["severity"], f"[{f['id']}]({f['url']})", f"`{cell(f['target'])}`",
           f"{cell(f['title'])}: {cell(f['message'])}"] for f in misconfigs], "misconfigurations",
         "Fix the file, or add the ID to `.trivyignore` with a comment saying why."),
    ]
    for title, columns, rows, what, advice in sections:
        if rows:
            lines += [f"## {title}", "", *table(columns, rows, NEW_ROWS, what), "", advice, ""]
    if old:
        rows = [[f["severity"], f["kind"], f["id"], f"`{cell(f['target'])}`"
                 + (f" ({cell(f['pkg'])} {cell(f['installed'])})" if f["kind"] == "vuln" else "")]
                for f in sorted(old, key=lambda f: (f["kind"], f["target"], f["id"]))]
        lines += [f"<details><summary>Already on the base, not blocking ({len(old)})</summary>", "",
                  *table(["Severity", "Kind", "ID", "Where"], rows, OLD_ROWS, "findings"), "", "</details>", ""]
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

    publish(text, args.report)
    for f in new:   # the report may be capped; the log lists every new finding
        print(f"::warning::new {f['kind']} {f['id']} ({f['severity']}) in {f['target']}")
    if not ok:
        print("::error::The security scan found problems this branch adds; see the report above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
