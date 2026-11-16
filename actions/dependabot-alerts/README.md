# Dependabot alerts

Keeps one issue, labelled `dependabot`, listing a repository's open Dependabot alerts
grouped by package, with the version that clears them all. The reusable workflow
[`dependabot-monitor.yml`](../../.github/workflows/dependabot-monitor.yml) runs it; the
`sdlc-dependabot-remediate` skill in [ly2xxx/sdlc-github](https://github.com/ly2xxx/sdlc-github)
reads the issue and fixes the alerts through the SDLC Pipeline.

## Call it

```yaml
# .github/workflows/dependabot-monitor.yml in the repository to watch
name: Dependabot monitor
on:
  schedule:
    - cron: "17 6 * * 1"          # weekly
  workflow_dispatch:
  push:
    branches: [main]
    paths: ["**/requirements*.txt", "**/pyproject.toml", "**/package.json", "**/*.lock", "**/package-lock.json"]
  issue_comment:
    types: [created]              # "/refresh" on the tracking issue
permissions:
  contents: read
  issues: write
jobs:
  monitor:
    if: >-
      github.event_name != 'issue_comment' || (
        contains(github.event.issue.labels.*.name, 'dependabot') &&
        startsWith(github.event.comment.body, '/refresh') &&
        contains(fromJSON('["OWNER","MEMBER","COLLABORATOR"]'), github.event.comment.author_association))
    uses: ly2xxx/.github/.github/workflows/dependabot-monitor.yml@main
    with:
      wait-minutes: ${{ github.event_name == 'push' && 10 || 0 }}
    secrets:
      DEPENDABOT_ALERTS_TOKEN: ${{ secrets.DEPENDABOT_ALERTS_TOKEN }}
```

A push that changes a manifest waits 10 minutes first, so Dependabot has rescanned it.
A `/refresh` comment on the tracking issue by someone with write access runs it at once;
that is how a session that can't start workflow runs asks for fresh data.

## The token

The workflow's `GITHUB_TOKEN` can't read Dependabot alerts. Create a fine-grained
personal access token limited to the repository, with the repository permission
**Dependabot alerts: Read-only**, and save it as the Actions secret
`DEPENDABOT_ALERTS_TOKEN`. Without it, or when the API refuses it, the monitor scans the
checkout with [Trivy](https://github.com/aquasecurity/trivy) instead (pinned and
checksum-verified like [`security-scan`](../security-scan/README.md)), and the issue
says so. `fallback: false` makes that an error.

## The issue

- **One row per package and manifest**, worst severity first. **Upgrade to** is the
  highest first fixed version across the package's open alerts, so one upgrade clears
  them all; *no fix* means no release fixes an alert. Python names are normalized
  (`Streamlit` and `streamlit` are one row).
- **Data for tools**: a collapsed JSON block at the end with every alert (`version: 1`;
  `packages[].package, manifest, current, fix_to, no_fix, severity, alerts[]`). A very
  large report drops alert details, then some alerts, to fit an issue body, and sets
  `truncated: true`; the package rows always stay.
- **Changes** get a comment: alerts that appeared and alerts no longer open.
- **No alerts left**: the issue closes itself. The next alert opens a new one.

Only an open issue with the label *and* the monitor's marker is updated; other issues
with the label are left alone.

## Usage tracking

Every run reports the calling repository's name to the usage endpoint in
[`defaults.env`](../defaults.env), as the SDLC Pipeline does: see
[LICENSING.md](../../LICENSING.md#usage-tracking).

## Develop

```bash
python -m pytest -q tests/test_dependabot_alerts.py
```
