# Security scan

Scans a branch for what it adds, and fails only on that:

- **Secrets** with [gitleaks](https://github.com/gitleaks/gitleaks), in the branch's own commits
  (merge-base..HEAD). The report shows the rule, file, line and fingerprint, never the secret.
- **Vulnerable dependencies and misconfigurations** with [Trivy](https://github.com/aquasecurity/trivy)
  (`trivy fs --scanners vuln,misconfig`), for any language's lockfiles and requirements, Dockerfiles,
  Kubernetes and Terraform. The branch and its merge-base are both scanned, and only findings the base
  doesn't have fail. Vulnerabilities count at the given severities (default HIGH, CRITICAL) and only
  when a fixed version exists.

Findings already on the base branch are listed in a collapsed section and never block, so a
repository with old problems can still ship clean features.

The SDLC pipeline runs it as **5 · Security scan**, beside verification, and step 6 opens the pull
request only when it passes; the report goes into the pull request. Callers switch it off with
`security-scan: false` and change the threshold with `security-severity`.

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0, persist-credentials: false }
- uses: ly2xxx/.github/actions/security-scan@main
  with:
    base: main
    report: ${{ runner.temp }}/security-report.md
```

## Accepting a finding

- **A false-positive secret:** add its fingerprint from the report to `.gitleaksignore`. A real one:
  remove it from the branch's history and rotate it.
- **A vulnerability or misconfiguration you accept:** add its ID (for example `CVE-2026-12345` or
  `DS-0002`) to `.trivyignore`, with a comment saying why.

Both files live at the repository root and are read from the branch being scanned.

## Pinned scanners

gitleaks 8.30.1 and Trivy 0.74.0 are downloaded from their GitHub releases and checked against
SHA-256 checksums pinned in `action.yml`; a mismatch fails the job before anything runs. To upgrade,
change a version input and its checksum together, taking the checksum from the release's
`*_checksums.txt`. Linux x86_64 runners only. Run it locally with both binaries on PATH:

```bash
python3 actions/security-scan/security_scan.py --base main
```
