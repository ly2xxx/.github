# Shared GitHub Actions

Reusable workflows and composite actions for an AI-assisted SDLC: Ollama Cloud
designs a feature, a builder (a coding agent or a person) builds it, and
deterministic checks decide whether it is done.

| Piece | What it does |
| :-- | :-- |
| [`.github/workflows/sdlc.yml`](.github/workflows/README.md) | The SDLC Pipeline: one run from a one-line idea to a pull request. |
| [`.github/workflows/sdlc-phase.yml`](.github/workflows/README.md) | Checks one phase's pull request into the feature branch. |
| [`actions/sdlc-stage`](actions/sdlc-stage/README.md) | The pipeline's steps (`sdlc_stage.py`, standard library only). |
| [`actions/python-env`](actions/python-env/action.yml) | A `.venv` with the project and pytest installed, on `PATH`. |
| [`actions/security-scan`](actions/security-scan/README.md) | gitleaks and Trivy, failing only on what a branch adds. |
| [`actions/commit-delta-summary`](actions/commit-delta-summary/README.md) | An Ollama summary of the changes between two revisions. |

## Use it

Call the two workflows from your repository; [the pipeline guide](.github/workflows/README.md)
covers the one-time setup and how a run goes.

```yaml
# .github/workflows/sdlc.yml
on:
  workflow_dispatch:
    inputs:
      idea: { type: string, default: "" }
      feature: { type: string, default: "" }
permissions:
  contents: write         # the pipeline pushes documents and the approved tag
  pull-requests: write    # and opens the pull request
jobs:
  sdlc:
    uses: ly2xxx/.github/.github/workflows/sdlc.yml@main
    with:
      idea: ${{ inputs.idea }}
      feature: ${{ inputs.feature }}
    secrets:
      OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}
```

```yaml
# .github/workflows/sdlc-phase.yml
on:
  pull_request:
    branches: ["feature/**"]
permissions:
  contents: read
jobs:
  phase:
    uses: ly2xxx/.github/.github/workflows/sdlc-phase.yml@main
```

[`ly2xxx/interview-playground`](https://github.com/ly2xxx/interview-playground) is a
complete caller, with an issue trigger and every input.

## Defaults

Settings a caller leaves empty, such as the Ollama model, fall back to
[`actions/defaults.env`](actions/defaults.env), the one place each default is set.

## Versions

`@main` tracks the latest. The `v1` tag is a fixed snapshot whose workflows call
the actions at `@v1` too, so a run uses one version throughout. A paused run on
`@main` picks up whatever `main` holds when its later jobs start.

## Develop

```bash
python -m pytest -q        # tests/: the pipeline script and the security report, offline
```

CI runs the tests and [actionlint](https://github.com/rhysd/actionlint) on every push and pull request.
