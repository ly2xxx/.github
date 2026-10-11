# Quality scan

Checks a branch for the lint issues and code smells it adds, with
[Qlty CLI](https://github.com/qltysh/qlty), and fails only on those:

- **Lint:** `qlty check` runs the linters that `.qlty/qlty.toml` enables, for example ruff,
  bandit and radarlint (SonarQube's rules) for Python, and actionlint, zizmor and hadolint for
  workflows and Dockerfiles. Formatters don't run.
- **Code smells:** `qlty smells` flags complex or deeply nested functions, long parameter lists,
  many returns and duplicated code. Tests are left out.

Both compare the branch with the commit where it left the base branch:

- a lint issue is new when it is on a line the branch changed (`qlty check --upstream`);
- a smell is new when the files the branch changed didn't have it at that commit. A smell a
  function already had doesn't count again when the function moves or its count changes; a new
  complex function does.

Issues count at the given level and above (`low`, `medium` or `high`; default `medium`). Old
issues never block, so a repository with old issues can still ship clean features.

The SDLC pipeline runs it twice:

- **5 · Quality scan**, beside verification. Step 6 opens the pull request only when it passes,
  and the report goes into the pull request.
- **Quality scan** on each phase pull request, beside the Phase check, so the builder sees the
  issues while still working on the phase.

Callers switch it off with `quality-scan: false` and change the level with `quality-level`.

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0, persist-credentials: false }
- uses: ly2xxx/.github/actions/quality-scan@main
  with:
    base: main
    report: ${{ runner.temp }}/quality-report.md
```

## Which linters run

A repository that commits `.qlty/qlty.toml` gets exactly the checks it enables
([reference](https://qlty.sh/d/qlty-toml)). Run `qlty init` to start one.

Without one, the scan writes the configuration `qlty init` would write, with three changes:

- It drops osv-scanner and trufflehog, because the [security scan](../security-scan/README.md)
  already checks dependencies and secrets, and ripgrep, which flags TODO comments.
- Bandit doesn't scan tests, where asserts are the point.
- Bandit's B404 and B603 (importing `subprocess`, and running it without a shell) are ignored.
  B602, B604 and B605 still flag running a shell.

The job log prints the configuration it used.

## Accepting an issue

When a rule is wrong for the repository, ignore it with a `[[triage]]` entry in
`.qlty/qlty.toml`, with a comment saying why:

```toml
# Scripts print their progress on purpose.
[[triage]]
match.rules = ["ruff:T201"]
match.file_patterns = ["scripts/**"]   # optional: only there
set.ignored = true
```

Linters read their own configuration files too. For example, zizmor flags an action not pinned to
a commit SHA; a repository that calls some actions by tag or branch on purpose says so in
`zizmor.yml`, as [this repository's](../../zizmor.yml) does.

## Pinned version

Qlty CLI 0.653.0 is downloaded from its GitHub release and checked against the SHA-256 checksum
pinned in `action.yml`. A mismatch fails the job before anything runs. Qlty then installs the
linters at the versions that release pins. To upgrade, change `qlty-version` and `qlty-sha256`
together, taking the checksum from the release's `.sha256` file. Telemetry is off
(`QLTY_TELEMETRY=off`). Linux x86_64 runners only.

To run it locally with `qlty` on PATH:

```bash
python3 actions/quality-scan/quality_scan.py --base main
```

## License

Qlty CLI is under the [Business Source License 1.1](https://github.com/qltysh/qlty/blob/main/LICENSE.md),
not an open source license. You may use it, but not for a "Code Quality Service", or to produce
an "Artificial Intelligence Coding Service". The license defines both as commercial offerings for
third parties other than your employees and contractors: the first gives them Qlty's code quality
features, the second lets them generate, edit or review code with machine learning or large
language models. Before you use this scan in, or to build, such a service,
check the license or ask [Qlty](https://qlty.sh) about other terms. On 2028-12-10 the license
becomes GPL-3.0.
