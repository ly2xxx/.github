# Commit Delta Summary — shared across repos

Summarises the diff between two revisions with Ollama Cloud, writes it to the job
summary, uploads it as an artifact, and posts/updates one comment on a PR.

## Why it is packaged this way

The original workflow shelled out to `scripts/commit-delta-summary.sh` **in the
repo being built**. That cannot be shared: every consuming repo would need its own
copy of the script, and they would drift.

So the logic lives in a **composite action**, where the script sits next to
`action.yml` and resolves through `${{ github.action_path }}`. It travels with the
action. Consuming repos need nothing but a reference.

A **reusable workflow** wraps the action to also carry the triggers, artifact
upload and PR comment, so the common case is a ~6-line file.

## Adopting it in another repo

### Option A — reusable workflow (recommended, ~6 lines)

`.github/workflows/commit-delta-summary.yml`:

```yaml
name: Commit Delta Summary
on:
  push:
    branches: ["feature/**", "claude/**"]
  pull_request:
    branches: [main]
permissions:
  contents: read
  pull-requests: write      # required: the caller grants the token scope
jobs:
  summary:
    uses: ly2xxx/langgraph_ollama/.github/workflows/commit-delta-summary.yml@main
    secrets: inherit
```

`secrets: inherit` passes `OLLAMA_API_KEY` through. Without it, pass explicitly:

```yaml
    secrets:
      OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}
```

### Option B — the action directly (when you want your own job)

```yaml
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }        # required: the summariser diffs two revisions
      - uses: ly2xxx/langgraph_ollama/.github/actions/commit-delta-summary@main
        with:
          ollama-api-key: ${{ secrets.OLLAMA_API_KEY }}
          model: gpt-oss:120b-cloud
```

## One-time setup per repo

1. **Secret** `OLLAMA_API_KEY` — Settings → Secrets and variables → Actions.
   For many repos set it once as an **account-level** secret and grant repo access,
   rather than pasting it N times.
2. Optional **variables** `OLLAMA_MODEL`, `MAX_DIFF_CHARS` to override defaults.

## Inputs

| Input | Default | |
|---|---|---|
| `base` / `head` | from the event | explicit revisions; omit to infer from push/PR |
| `model` | `gpt-oss:120b-cloud` | Ollama Cloud model |
| `max-diff-chars` | `60000` | diff is truncated beyond this, with a note |
| `outfile` | `commit-delta-summary.md` | |
| `ollama-api-key` | — | empty ⇒ skip cleanly, never fail the build |
| `custom-prompt` | — | additional instructions appended to the base prompt |
| `system-prompt` | — | overrides the default base prompt instructions |

Outputs: `summary-file`, `skipped`.

## Behaviour worth knowing

- **Forked PRs get no secrets.** The action detects the empty key, writes a note to
  the job summary and exits 0. It never fails a contributor's build.
- **A branch's first push** reports an all-zero `before`; it falls back to `head~1`.
- **Bad refs fail loudly** rather than producing an empty diff that silently
  summarises nothing.
- **PR comments are updated, not duplicated** — matched on a `<!-- commit-delta-summary -->`
  marker.
- `fetch-depth: 0` is required. A shallow checkout cannot diff two revisions.

## Pinning

`@main` tracks the latest. For repos where a CI change must not arrive
unannounced, tag this repo and pin: `...@v1`.

## If you later extract this to its own repo

Hosting shared CI inside an application repo works but reads oddly. Moving it to
`ly2xxx/github-actions` needs only a path change in consumers, plus the same change
in `.github/workflows/commit-delta-summary.yml`, which references the action by its
full path for exactly this reason.
