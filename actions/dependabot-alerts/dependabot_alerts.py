#!/usr/bin/env python3
"""Dependabot monitor: read a repository's open vulnerability alerts, group them by
package, and keep one tracking issue up to date with what to upgrade to.

    dependabot_alerts.py read    --out alerts.json   # the Dependabot alerts API
    dependabot_alerts.py trivy   --out alerts.json   # fallback: Trivy over the checkout
    dependabot_alerts.py publish --alerts alerts.json

- read: needs DEPENDABOT_ALERTS_TOKEN, a token that can read Dependabot alerts (the
  workflow's own GITHUB_TOKEN can't). Without one, or when the API refuses it, it
  writes nothing and sets the output source to empty, so the caller falls back to
  Trivy; with FALLBACK=false that is an error instead. It also sends the usage ping.
- trivy: expects trivy on PATH. Reads the manifests in the working directory.
- publish: one row per package, upgrade to the highest fixed version across the
  package's open alerts. Updates (or opens) the open issue labelled LABEL that
  carries the monitor's marker, comments when alerts appear or go away, and closes
  it when none are left. Uses GITHUB_TOKEN (issues: write) and GITHUB_REPOSITORY.

The issue body ends with the same data as JSON, for tools (the sdlc-dependabot-remediate
skill reads it). Standard library only.
"""
import argparse
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULTS = Path(__file__).resolve().parent.parent / "defaults.env"   # the one place shared defaults are set
API = "https://api.github.com"
MARKER = "<!-- dependabot-monitor -->"
SEVERITIES = ["critical", "high", "medium", "low", "unknown"]
MAX_IDS = 6           # IDs shown per package in the table; the JSON has them all
MAX_BODY = 60000      # GitHub's limit on an issue body is 65536 characters


def env(name, default=""):
    return os.environ.get(name, "").strip() or default


def fail(message):
    print(f"::error::{message}", flush=True)
    sys.exit(1)


def set_output(**values):
    path = env("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            for key, value in values.items():
                f.write(f"{key}={value}\n")


def default(name):
    """A setting's default from actions/defaults.env."""
    text = DEFAULTS.read_text() if DEFAULTS.exists() else ""
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""


def usage_ping():
    """Tell the usage endpoint that this repository ran the monitor. It sends the
    repository's name and nothing else, and it is part of the terms of use
    (LICENSING.md, "Usage tracking"). A ping that fails never fails the run."""
    repository, url = env("GITHUB_REPOSITORY"), env("SDLC_PING_URL") or default("SDLC_PING_URL")
    if not repository or not url:
        return
    req = urllib.request.Request(url, data=json.dumps({"repository": repository}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
        print(f"Usage ping sent: repository name {repository} (LICENSING.md, Usage tracking).", flush=True)
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
        print(f"::warning::The usage ping failed ({e}); the run goes on.", flush=True)


# ---------------------------------------------------------------- GitHub API

class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


def request(method, url, token, data=None):
    """(parsed JSON, the Link header) of one GitHub API call."""
    req = urllib.request.Request(url if url.startswith("http") else API + url, method=method,
                                 data=None if data is None else json.dumps(data).encode(),
                                 headers={"Authorization": f"Bearer {token}",
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read()
            return (json.loads(body) if body else None), r.headers.get("Link", "")
    except urllib.error.HTTPError as e:
        try:
            message = json.loads(e.read() or b"{}").get("message", e.reason)
        except ValueError:
            message = e.reason
        raise ApiError(e.code, message) from None


def next_link(link):
    m = re.search(r'<([^>]+)>;\s*rel="next"', link or "")
    return m and m.group(1)


def get_all(url, token):
    items = []
    while url:
        page, link = request("GET", url, token)
        items += page or []
        url = next_link(link)
    return items


# ---------------------------------------------------------------- versions and names

def normalize(name, ecosystem="pip"):
    """PEP 503 for Python packages (Streamlit, streamlit and stream_lit are one), else lower case."""
    name = (name or "").strip().lower()
    return re.sub(r"[-_.]+", "-", name) if ecosystem == "pip" else name


PRE = {"dev": 0, "a": 1, "alpha": 1, "b": 2, "beta": 2, "c": 3, "rc": 3, "pre": 3, "preview": 3}


def version_key(version):
    """A sort key for a version: release numbers compared as numbers, and a
    pre-release (1.0rc1, 1.0-beta.2) before its release."""
    v = (version or "").strip().lower().lstrip("v")
    m = re.match(r"(\d+(?:\.\d+)*)(?:[-_.]?([a-z]+)[-_.]?(\d*))?", v)
    if not m:
        return ((), 0, 0)
    release = tuple(int(p) for p in m.group(1).split("."))
    while release and release[-1] == 0:
        release = release[:-1]
    tag = m.group(2)
    rank = PRE.get(tag, 9) if tag else 9             # unknown suffixes (post, build) sort as the release
    return (release, rank, int(m.group(3) or 0) if tag else 0)


def highest(versions):
    versions = [v for v in versions if v]
    return max(versions, key=version_key) if versions else ""


def pinned_versions(path):
    """{normalized name: version} for `name==version` lines of a requirements file."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:
        return {}
    pins = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].split(";", 1)[0].strip().lstrip("\ufeff")
        m = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*===?\s*([^\s,]+)$", line)
        if m:
            pins[normalize(m.group(1))] = m.group(2)
    return pins


def clears(version, vulnerable_range):
    """True when `version` is above every upper bound of a range like "<= 2.6.0" or
    ">= 3.7.0, < 3.17.0". A range with no upper bound is never cleared."""
    bounds = re.findall(r"(<=?)\s*([^\s,]+)", vulnerable_range or "")
    if not version or not bounds:
        return False
    return all(version_key(version) > version_key(v) if op == "<=" else version_key(version) >= version_key(v)
               for op, v in bounds)


def severity(value):
    value = (value or "unknown").lower()
    return {"moderate": "medium"}.get(value, value if value in SEVERITIES else "unknown")


# ---------------------------------------------------------------- read

def from_dependabot(alert):
    dep, adv, vuln = alert.get("dependency") or {}, alert.get("security_advisory") or {}, \
        alert.get("security_vulnerability") or {}
    pkg = dep.get("package") or vuln.get("package") or {}
    ecosystem = (pkg.get("ecosystem") or "").lower()
    return {"package": normalize(pkg.get("name"), ecosystem), "ecosystem": ecosystem,
            "manifest": dep.get("manifest_path") or "",
            "id": adv.get("cve_id") or adv.get("ghsa_id") or str(alert.get("number", "")),
            "ghsa": adv.get("ghsa_id") or "", "severity": severity(vuln.get("severity") or adv.get("severity")),
            "fixed": ((vuln.get("first_patched_version") or {}).get("identifier") or ""),
            "range": vuln.get("vulnerable_version_range") or "", "current": "",
            "summary": adv.get("summary") or "", "url": alert.get("html_url") or ""}


def read(args):
    usage_ping()
    token, repository = env("DEPENDABOT_ALERTS_TOKEN"), env("GITHUB_REPOSITORY")
    fallback = env("FALLBACK", "true").lower() != "false"
    why = ""
    if not token:
        why = "no DEPENDABOT_ALERTS_TOKEN secret"
    else:
        try:
            raw = get_all(f"/repos/{repository}/dependabot/alerts?state=open&per_page=100", token)
        except ApiError as e:
            if e.status not in (401, 403, 404):
                fail(f"Reading the Dependabot alerts failed: {e}")
            why = f"the token can't read the alerts ({e}); it needs the Dependabot alerts: read permission"
        except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
            fail(f"Reading the Dependabot alerts failed: {e}")
    if why:
        if not fallback:
            fail(f"Can't read the Dependabot alerts: {why}.")
        print(f"::notice::Using Trivy instead of the Dependabot alerts: {why}.", flush=True)
        set_output(source="")
        return
    alerts = [from_dependabot(a) for a in raw]
    pins = {}
    for a in alerts:
        if a["ecosystem"] == "pip" and a["manifest"]:
            pins.setdefault(a["manifest"], pinned_versions(a["manifest"]))
            a["current"] = pins[a["manifest"]].get(a["package"], "")
    Path(args.out).write_text(json.dumps({"source": "dependabot", "alerts": alerts}, indent=1))
    print(f"Read {len(alerts)} open Dependabot alerts.", flush=True)
    set_output(source="dependabot")


ECOSYSTEMS = {"pip": "pip", "pipenv": "pip", "poetry": "pip", "uv": "pip", "python-pkg": "pip",
              "npm": "npm", "yarn": "npm", "pnpm": "npm", "gomod": "go", "cargo": "rust", "bundler": "rubygems",
              "composer": "composer", "nuget": "nuget", "pom": "maven", "gradle": "maven"}


def fixed_for(installed, fixed):
    """Trivy lists one fixed version per release line ("2.2.5, 3.0.1"): the lowest
    one above the installed version, else the highest."""
    options = [v.strip() for v in (fixed or "").split(",") if v.strip()]
    above = [v for v in options if version_key(v) > version_key(installed)]
    return min(above, key=version_key) if above else highest(options)


def from_trivy(results):
    alerts = []
    for res in results:
        ecosystem = ECOSYSTEMS.get((res.get("Type") or "").lower(), (res.get("Type") or "").lower())
        for v in res.get("Vulnerabilities") or []:
            alerts.append({"package": normalize(v.get("PkgName"), ecosystem), "ecosystem": ecosystem,
                           "manifest": res.get("Target") or "", "id": v.get("VulnerabilityID") or "", "ghsa": "",
                           "severity": severity(v.get("Severity")),
                           "fixed": fixed_for(v.get("InstalledVersion"), v.get("FixedVersion")),
                           "range": "", "current": v.get("InstalledVersion") or "",
                           "summary": v.get("Title") or "", "url": v.get("PrimaryURL") or ""})
    return alerts


def trivy(args):
    out = Path(args.out).with_suffix(".trivy.json")
    cmd = ["trivy", "fs", "--quiet", "--scanners", "vuln", "--format", "json", "--output", str(out)]
    if Path(".trivyignore").exists():
        cmd += ["--ignorefile", ".trivyignore"]
    r = subprocess.run(cmd + ["."], capture_output=True, text=True)
    if r.returncode != 0:
        fail(f"trivy failed ({r.returncode}): {r.stderr.strip() or r.stdout.strip()}")
    alerts = from_trivy(json.loads(out.read_text()).get("Results") or [])
    Path(args.out).write_text(json.dumps({"source": "trivy", "alerts": alerts}, indent=1))
    print(f"Trivy found {len(alerts)} known vulnerabilities.", flush=True)
    set_output(source="trivy")


# ---------------------------------------------------------------- group and report

def group(alerts):
    """One entry per (manifest, package), worst first."""
    packages = {}
    for a in alerts:
        p = packages.setdefault((a["manifest"], a["package"]), {
            "package": a["package"], "ecosystem": a["ecosystem"], "manifest": a["manifest"],
            "current": a["current"], "alerts": []})
        p["current"] = p["current"] or a["current"]
        p["alerts"].append({k: a[k] for k in ("id", "ghsa", "severity", "fixed", "range", "summary", "url")})
    for p in packages.values():
        p["alerts"].sort(key=lambda a: (SEVERITIES.index(a["severity"]), a["id"]))
        p["severity"] = p["alerts"][0]["severity"]
        p["fix_to"] = highest(a["fixed"] for a in p["alerts"])
        # An alert with no fixed version is still cleared by fix_to when fix_to is above
        # its vulnerable range ("<= 2.6.0"); only the others need a decision.
        p["no_fix"] = sorted(a["id"] for a in p["alerts"] if not a["fixed"] and not clears(p["fix_to"], a["range"]))
    return sorted(packages.values(), key=lambda p: (SEVERITIES.index(p["severity"]), -len(p["alerts"]),
                                                    p["package"], p["manifest"]))


def counts(alerts):
    return {s: n for s in SEVERITIES if (n := sum(a["severity"] == s for a in alerts))}


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ") or "—"


def render(data, packages, run_url, now):
    alerts = data["alerts"]
    source = ("Dependabot alerts API" if data["source"] == "dependabot"
              else "Trivy scan of the default branch (no token that can read Dependabot alerts)")
    lines = [MARKER, "# Dependabot alerts", "",
             f"Source: {source} · Read {now}" + (f" · [Run]({run_url})" if run_url else ""), ""]
    if not alerts:
        lines += ["**No open alerts.** This issue closes itself; the monitor opens a new one when alerts appear.", ""]
    else:
        by = ", ".join(f"{n} {s}" for s, n in counts(alerts).items())
        lines += [f"**{len(alerts)} open alerts in {len(packages)} packages:** {by}.", "",
                  "| Package | Manifest | Current | Upgrade to | Alerts | Worst | IDs |",
                  "| :-- | :-- | :-- | :-- | --: | :-- | :-- |"]
        for p in packages:
            ids = [a["id"] for a in p["alerts"]]
            shown = ", ".join(ids[:MAX_IDS]) + (f" +{len(ids) - MAX_IDS} more" if len(ids) > MAX_IDS else "")
            upgrade = p["fix_to"] or "no fix"
            if p["fix_to"] and p["no_fix"]:
                upgrade += f" ({len(p['no_fix'])} with no fix)"
            lines.append(f"| `{cell(p['package'])}` | `{cell(p['manifest'])}` | {cell(p['current'])} | "
                         f"**{cell(upgrade)}** | {len(ids)} | {p['severity']} | {cell(shown)} |")
        lines += ["", "**Upgrade to** is the highest first fixed version across the package's open alerts, "
                  "so one upgrade clears them all. *No fix*: no release fixes it; remove or replace the "
                  "package, or dismiss the alert with a reason.", ""]
    payload = {"version": 1, "source": data["source"], "repository": env("GITHUB_REPOSITORY"), "read_at": now,
               "open": len(alerts), "severities": counts(alerts), "packages": packages}
    def body():
        blob = json.dumps(payload, separators=(",", ":"))
        return "\n".join(lines + ["<details><summary>Data for tools</summary>", "", "```json", blob, "```", "",
                                  "</details>", ""])

    # Shrink until it fits an issue body: drop the alerts' details, then the alerts of
    # the packages with most of them (the package rows, with fix_to, always stay).
    for drop in ([], ["summary"], ["url", "range"], ["ghsa"]):
        for p in payload["packages"]:
            for a in p["alerts"]:
                for k in drop:
                    a.pop(k, None)
        if len(body()) <= MAX_BODY:
            return body()
    payload["truncated"] = True
    while len(body()) > MAX_BODY and any(len(p["alerts"]) > 1 for p in payload["packages"]):
        biggest = max(payload["packages"], key=lambda p: len(p["alerts"]))
        del biggest["alerts"][len(biggest["alerts"]) // 2:]
    return body()


def previous_ids(body):
    """{(package, id)} from the JSON block of a report; None when it can't be read or
    was cut down to fit, since then a missing alert isn't a fixed one."""
    m = re.search(r"```json\n(.*?)\n```", body or "", flags=re.DOTALL)
    try:
        data = json.loads(m.group(1)) if m else None
    except ValueError:
        data = None
    if not isinstance(data, dict) or data.get("truncated"):
        return None
    return {(p["package"], a["id"]) for p in data.get("packages", []) for a in p.get("alerts", [])}


def changes(before, after):
    if before is None or after is None:
        return ""
    new, gone = sorted(after - before), sorted(before - after)
    if not new and not gone:
        return ""
    fmt = lambda items: ", ".join(f"`{p}` {i}" for p, i in items[:20]) + (f" +{len(items) - 20} more" if len(items) > 20 else "")
    parts = []
    if new:
        parts.append(f"**{len(new)} new:** {fmt(new)}")
    if gone:
        parts.append(f"**{len(gone)} no longer open:** {fmt(gone)}")
    return "\n\n".join(parts)


def ensure_label(repository, label, token):
    try:
        request("GET", f"/repos/{repository}/labels/{urllib.parse.quote(label)}", token)
    except ApiError as e:
        if e.status != 404:
            raise
        request("POST", f"/repos/{repository}/labels", token,
                {"name": label, "color": "d73a4a", "description": "Open dependency alerts, kept up to date by the Dependabot monitor"})


def find_issue(repository, label, token):
    issues = get_all(f"/repos/{repository}/issues?state=open&per_page=100&labels={urllib.parse.quote(label)}", token)
    mine = [i for i in issues if "pull_request" not in i and MARKER in (i.get("body") or "")]
    return min(mine, key=lambda i: i["number"]) if mine else None


def publish(args):
    data = json.loads(Path(args.alerts).read_text())
    packages = group(data["alerts"])
    repository, token, label = env("GITHUB_REPOSITORY"), env("GITHUB_TOKEN"), env("LABEL", "dependabot")
    run_url = (f"{env('GITHUB_SERVER_URL', 'https://github.com')}/{repository}/actions/runs/{env('GITHUB_RUN_ID')}"
               if env("GITHUB_RUN_ID") else "")
    body = render(data, packages, run_url, time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()))
    summary = env("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(body.split("<details>")[0])
    n = len(data["alerts"])
    title = f"Dependabot alerts: {n} open in {len(packages)} packages" if n else "Dependabot alerts: none open"
    issue_number = ""
    if not token or not repository:
        print(body)
        print("::warning::No GITHUB_TOKEN or GITHUB_REPOSITORY: the report went to the log only.")
    else:
        issue = find_issue(repository, label, token)
        if n == 0 and issue:
            request("PATCH", f"/repos/{repository}/issues/{issue['number']}", token,
                    {"title": title, "body": body, "state": "closed", "state_reason": "completed"})
            request("POST", f"/repos/{repository}/issues/{issue['number']}/comments", token,
                    {"body": "No open alerts are left. Closing; the monitor opens a new issue when alerts appear."})
            issue_number = str(issue["number"])
            print(f"No open alerts: closed issue #{issue_number}.")
        elif issue:
            note = changes(previous_ids(issue.get("body")), previous_ids(body))
            request("PATCH", f"/repos/{repository}/issues/{issue['number']}", token, {"title": title, "body": body})
            if note:
                request("POST", f"/repos/{repository}/issues/{issue['number']}/comments", token, {"body": note})
            issue_number = str(issue["number"])
            print(f"Updated issue #{issue_number}: {title}.")
        elif n:
            ensure_label(repository, label, token)
            created, _ = request("POST", f"/repos/{repository}/issues", token,
                                 {"title": title, "body": body, "labels": [label]})
            issue_number = str(created["number"])
            print(f"Opened issue #{issue_number}: {title}.")
        else:
            print("No open alerts and no tracking issue: nothing to do.")
    set_output(open=n, packages=len(packages), issue=issue_number, source=data["source"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("read", "trivy"):
        sub.add_parser(name).add_argument("--out", required=True)
    sub.add_parser("publish").add_argument("--alerts", required=True)
    args = parser.parse_args(argv)
    {"read": read, "trivy": trivy, "publish": publish}[args.command](args)


if __name__ == "__main__":
    main()
