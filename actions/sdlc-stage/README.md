# SDLC pipeline: shared across repos

A feature goes from a one-line idea to a pull request. Ollama Cloud writes the
design, a builder writes the code, and deterministic checks decide whether it
is done.

```text
design run   1 · Ollama writes intent.md ✋ → 2 · spec.md ✋ → 3 · plan.md ✋ → 4 · ⏸ hand off to the builder
                                                                          │
   the builder (Claude Code, or a person) builds plan.md phase by phase ◀─┘
   each phase: phase/<feature>/<n> → pull request into feature/<feature> → Phase check → merge
                                                                          │
build run    5 · verify → 5 · Ollama reviews the build → 6 · ✋ open the pull request ◀─┘
```

| Piece | What it is |
| :-- | :-- |
| `.github/workflows/sdlc.yml` | Reusable workflow: both runs. A repository calls it from its own `workflow_dispatch` workflow. |
| `.github/workflows/sdlc-phase.yml` | Reusable workflow: checks one phase's pull request into the feature branch. |
| `actions/sdlc-stage` | Composite action and `sdlc_stage.py` (standard library only): `resolve`, `stage`, `freeze`, `verify`, `review`, `pr`. |
| `actions/python-env` | Composite action: `.venv` with the project and pytest installed, on PATH. |

## How the run pauses at step 4

Step 4 tags the approved `intent.md`, `spec.md` and `plan.md` as
`sdlc/<feature>/approved`, writes the hand-off instructions for the chosen
builder into the run summary, and ends the run. That works the same in a
private repository, where a ✋ job can't wait, as in a public one. The builder
hands back by running the workflow again with just the feature name: once all
three documents exist, `start: auto` means the build run.

Merging phase pull requests hands back too. When a pull request from
`phase/<feature>/<n>` into `feature/<feature>` is merged and `build-log.md` logs
every phase of the approved plan (`## Phase <id>: ...`), the build run starts
by itself. That is how Claude Code hands back: its GitHub access can push,
open and merge pull requests, but can't start workflow runs. It also can't
start a design run, so only a person can ask for a new plan. The caller's
workflow needs `pull_request: types: [closed], branches: ["feature/**"]` next to
`workflow_dispatch`, and a job condition that lets only merged `phase/`
pull requests through.

## What keeps the builder honest

- **The plan it is checked against is the approved one.** `verify` reads
  `plan.md` from the tag and fails if any of the three documents changed on the
  branch. A builder that can't build the plan asks for a new one (the plan
  stage again) and can't edit it.
- **Scope is mechanical.** A changed file outside the phase's `targets`, or any
  `frozen` file, fails the check. `*` doesn't cross `/`; `**` does.
- **"Tests pass" is an exit code**, from each phase's Verify block and the
  whole suite. A suite that collects no tests fails.
- **Ollama's review is advisory.** It compares the diff with the spec and goes
  into the pull request, and it never blocks one.

## Adopting it

`ly2xxx/interview-playground` is the reference caller: `.github/workflows/sdlc.yml`,
`.github/workflows/sdlc-phase.yml`, `.claude/skills/sdlc-build/SKILL.md` and
`sdlc/README.md`. You need an `OLLAMA_API_KEY` secret. Also turn on "Allow GitHub
Actions to create and approve pull requests", or add an `SDLC_PR_TOKEN` secret.

Run one phase's check locally:

```bash
curl -sSfLo /tmp/sdlc_stage.py https://raw.githubusercontent.com/ly2xxx/.github/main/actions/sdlc-stage/sdlc_stage.py
python /tmp/sdlc_stage.py verify --feature <feature> --phase <n> --test-command "python -m pytest -q"
```

Everything here tracks `@main`. A paused run picks up whatever `main` holds when
its later jobs start, so tag a release and pin callers to it if that matters.
