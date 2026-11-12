import security_scan as sc


def vuln(pkg, cve, version="1.0", severity="HIGH"):
    return {"PkgName": pkg, "VulnerabilityID": cve, "InstalledVersion": version, "FixedVersion": "9.9",
            "Severity": severity, "Title": "t", "PrimaryURL": f"https://x/{cve}"}


def test_findings_ignore_versions_and_passing_checks():
    head = sc.findings([{"Target": "requirements.txt", "Vulnerabilities": [vuln("a", "CVE-1", "1.1")],
                         "Misconfigurations": [{"ID": "DS-1", "Status": "PASS"}, {"ID": "DS-2", "Severity": "HIGH"}]}])
    base = sc.findings([{"Target": "requirements.txt", "Vulnerabilities": [vuln("a", "CVE-1", "1.0")]}])
    assert sorted(head) == [("misconfig", "requirements.txt", "DS-2"), ("vuln", "requirements.txt", "a", "CVE-1")]
    assert [k for k in head if k not in base] == [("misconfig", "requirements.txt", "DS-2")]


def test_report_passes_without_new_findings_and_lists_old_ones():
    old = list(sc.findings([{"Target": "r.txt", "Vulnerabilities": [vuln("a", "CVE-1")]}]).values())
    ok, text = sc.report([], [], old, "HIGH,CRITICAL", "main")
    assert ok and "**Result: PASSED**" in text and "Already on the base, not blocking (1)" in text


def test_report_fails_on_a_new_secret_and_caps_long_tables():
    secret = {"RuleID": "aws-access-token", "File": "a.py", "StartLine": 3, "Commit": "abcdef123", "Fingerprint": "f"}
    new = list(sc.findings([{"Target": "r.txt", "Vulnerabilities": [vuln(f"p{i}", f"CVE-{i}") for i in range(105)]}])
               .values())
    ok, text = sc.report([secret], new, [], "HIGH", "main")
    assert not ok and "| aws-access-token | `a.py:3` | `abcdef1` | `f` |" in text
    assert "| Vulnerable dependencies (Trivy) | ❌ 105 |" in text and "_…and 5 more vulnerabilities" in text


def test_base_worktree_needs_no_lfs_download(tmp_path, monkeypatch):
    import shutil
    import subprocess

    import pytest

    if not shutil.which("git-lfs"):
        pytest.skip("git-lfs is not installed")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "0")
    monkeypatch.delenv("GIT_LFS_SKIP_SMUDGE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)

    def g(*args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                       check=True, capture_output=True)

    g("init", "-q")
    # a pointer whose object exists nowhere, with an LFS server that can't be reached
    (repo / ".gitattributes").write_text("*.csv filter=lfs diff=lfs merge=lfs -text\n")
    (repo / "big.csv").write_text("version https://git-lfs.github.com/spec/v1\n"
                                  f"oid sha256:{'ab' * 32}\nsize 73000000\n")
    g("add", ".")
    g("commit", "-q", "-m", "base")
    g("lfs", "install", "--local")
    g("config", "lfs.url", "https://127.0.0.1:9/none")

    sc.add_base_worktree(tmp_path / "base", sc.git("rev-parse", "HEAD"))
    assert (tmp_path / "base" / "big.csv").read_text().startswith("version https://git-lfs")
