# sdlc-stage

The steps of the [SDLC pipeline](../../.github/workflows/README.md), as one composite
action around `sdlc_stage.py` (Python standard library, git and `gh`). The reusable
workflows call it once per job with a `command`; its inputs are in [`action.yml`](action.yml).

| Command | Job | Does |
| :-- | :-- | :-- |
| `resolve` | Resolve the feature | Picks the feature folder, its `feature/<feature>` branch, the first stage and the run's mode, from an idea, a feature, an issue body or a merged phase branch. |
| `stage` | 1 · 2 · 3 | Asks Ollama for `intent.md`, `spec.md` or `plan.md` and pushes it to the feature branch. A plan that fails its checks is asked for once more. |
| `freeze` | 4 | Tags the approved documents `sdlc/<feature>/approved` and writes the hand-off for the builder. |
| `wait` | 4 | Polls the feature branch until `build-log.md` logs every phase. |
| `verify` | 5 · Phase check | Checks the build against the approved plan (below). |
| `review` | 5 | Ollama compares the diff with the approved spec and plan. Advisory. |
| `pr` | 6 | Opens or updates the pull request, as a draft if verification failed. |
| `toolchain` | 5 · Phase check | Reads what the repository is built with (Python; Node.js and its package manager) for [`project-env`](../project-env/action.yml), which sets it up. The plan stage describes the same setup to the model. |

## What a plan looks like to the checks

Every `## Phase <n>: <name>` section of `plan.md` carries three markers, a Definition
of done checklist and a Verify block:

````markdown
## Phase 1: Add greet()
<!-- phase: 1 -->
<!-- targets: app/*.py, tests/test_greet.py -->
<!-- frozen: tests/test_old.py -->

**Definition of done:**
- [ ] `tests/test_greet.py::test_greet` passes

**Verify:**
```bash
python -m pytest tests/test_greet.py -q
```
````

`plan.md` also needs a `## Coverage` table with a row for every "Done when" item in
`intent.md`. The pipeline appends the same "Hand back" section to every plan.

`verify` reads the plan from the approved tag, never from the branch, and fails when:

- **Scope:** a changed file matches none of the targets. `*` and `?` stop at `/`; `**`
  crosses it. Files in the feature's own folder (`build-log.md`) don't count.
- **Frozen:** a changed file outside the targets matches a `frozen` glob.
- **Contract:** `intent.md`, `spec.md` or `plan.md` differs from the approved tag.
- **Commands:** a Verify block, run with `bash -e -o pipefail`, or the whole test suite
  exits non-zero, or nothing changed at all.

With `--phase <n>` it checks that phase's targets and frozen files and runs the Verify
blocks of phases 1 to n: against uncommitted work, or with `--base` against the phase
branch's changes (the Phase check). Without it, it checks every phase against
everything the branch changed since `--base`.

## How the run pauses at step 4

Step 4 is one job: `freeze`, then `wait`. It polls the feature branch until
`build-log.md` has a `## Phase <id>: ...` section for every phase of the approved
plan, for up to `build-wait-minutes` (default 180, at most 350 because a hosted job
runs for 6 hours at most), and the same run carries on to verify, review and open
the pull request. Being a job rather than an environment gate, it works the same in
a private repository, and it holds a runner (and Actions minutes) while it waits.

If it stops waiting, the run ends there. Once the build is done, run the pipeline
again with the feature and start `build` to do steps 5 and 6. With
`build-wait-minutes: 0`, every design run ends at the hand-off.

## Run it locally

`verify` is the builder's own check, before pushing a phase:

```bash
curl -sSfLo /tmp/sdlc_stage.py https://raw.githubusercontent.com/ly2xxx/.github/main/actions/sdlc-stage/sdlc_stage.py
python /tmp/sdlc_stage.py verify --feature <feature> --phase <n> --test-command "python -m pytest -q"
```

The other commands read the same environment variables the action sets (`SDLC_FEATURE`,
`BASE_REF`, `OLLAMA_API_KEY` and so on; see the `env:` block in `action.yml`).
