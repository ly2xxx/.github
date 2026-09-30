# SDLC pipeline: shared across repos

A feature goes from a one-line idea to a pull request. Ollama Cloud writes the
design, a builder writes the code, and deterministic checks decide whether it
is done.

```text
one run, one line
resolve → 1 · Ollama writes intent.md ✋ → 2 · spec.md ✋ → 3 · plan.md ✋
        → 4 · ⏸ hand off and wait for the build      the builder (Claude Code, or a person) builds
        → 5 · verify, 5 · security scan                  plan.md phase by phase meanwhile: each phase on
        → 5 · Ollama reviews the build
        → 6 · ✋ open the pull request                   phase/<feature>/<n> → PR into feature/<feature>
                                                          → Phase check → merge
```

| Piece | What it is |
| :-- | :-- |
| `.github/workflows/sdlc.yml` | Reusable workflow: both runs. A repository calls it from its own `workflow_dispatch` workflow. |
| `.github/workflows/sdlc-phase.yml` | Reusable workflow: checks one phase's pull request into the feature branch. |
| `actions/sdlc-stage` | Composite action and `sdlc_stage.py` (standard library only): `resolve`, `stage`, `freeze`, `verify`, `review`, `pr`. |
| `actions/python-env` | Composite action: `.venv` with the project and pytest installed, on PATH. |
| `actions/security-scan` | Composite action: gitleaks and Trivy, failing only on what the branch adds. See [its README](../security-scan/README.md). |

## How the run pauses at step 4

Step 4 is one job. It tags the approved `intent.md`, `spec.md` and `plan.md` as
`sdlc/<feature>/approved` and writes the hand-off for the chosen builder into
the run summary. Then it waits: it polls the feature branch until
`build-log.md` has a `## Phase <id>: ...` section for every phase of the
approved plan, for up to `build-wait-minutes` (default 180, at most 350 because
a hosted job runs for 6 hours at most). The same run then carries on to verify,
the Ollama review and the pull request. It is a polling job, not an
environment gate, so it pauses the same way in a private repository. It holds a
runner while it waits, so the wait counts towards Actions minutes.

If step 4 stops waiting, the run ends there. Once the build is done, running the
workflow again with the feature and start `build` does steps 5 and 6; steps
that run doesn't need show as skipped, in the same line. With
`build-wait-minutes: 0` every design run ends at the hand-off.

## Where an idea comes from

- **Run workflow**, with an idea (or with a feature, to redo a document).
- **An issue labelled `sdlc`.** Its title is the idea. A line `feature: <name>`
  in its body revises that feature instead of starting a new one; add
  `start: build` (or `spec`, `plan`) to run that feature from that stage
  instead, for example to verify it and open the pull request again. Only people
  with triage access can add labels, so this is safe in public repositories.
  It also suits people and tools that can open issues but can't start workflow
  runs. The caller triggers on `issues: types: [labeled]`.

## What keeps the builder honest

- **The plan it is checked against is the approved one.** `verify` reads
  `plan.md` from the tag and fails if any of the three documents changed on the
  branch. A builder that can't build the plan asks for a new one (the plan
  stage again) and can't edit it.
- **Scope is mechanical.** A changed file outside the phase's `targets`, or any
  `frozen` file, fails the check. `*` doesn't cross `/`; `**` does.
- **"Tests pass" is an exit code**, from each phase's Verify block and the
  whole suite. A suite that collects no tests fails.
- **Security is scanned, and only new problems block.** Step 5's security scan
  fails on secrets in the branch's commits and on HIGH or CRITICAL vulnerable
  dependencies (with a fix available) or misconfigurations that the base branch
  doesn't have. The pull request waits for it and carries its report. Callers
  can switch it off with `security-scan: false`.
- **Ollama's review is advisory.** It compares the diff with the spec and goes
  into the pull request, and it never blocks one.

## Adopting it

`ly2xxx/interview-playground` is the reference caller: `.github/workflows/sdlc.yml`,
`.github/workflows/sdlc-phase.yml` and `sdlc/README.md`. You need an `OLLAMA_API_KEY` secret. Also turn on "Allow GitHub
Actions to create and approve pull requests", or add an `SDLC_PR_TOKEN` secret. Without
either, step 6 fails but prints the pull request's title and body in its log, so
the builder can open exactly that pull request itself.

Run one phase's check locally:

```bash
curl -sSfLo /tmp/sdlc_stage.py https://raw.githubusercontent.com/ly2xxx/.github/main/actions/sdlc-stage/sdlc_stage.py
python /tmp/sdlc_stage.py verify --feature <feature> --phase <n> --test-command "python -m pytest -q"
```

Everything here tracks `@main`. A paused run picks up whatever `main` holds when
its later jobs start, so tag a release and pin callers to it if that matters.
