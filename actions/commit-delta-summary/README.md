# Commit Delta Summary

Summarises the changes between two revisions with Ollama Cloud: the commits, the
diffstat and the diff go to the model, and its Markdown summary is written to a file
and to the job summary. Posting it somewhere (a pull request comment, an artifact) is
up to the calling workflow.

```yaml
- uses: actions/checkout@v4
  with: { fetch-depth: 0 }          # it diffs two revisions, so it needs history
- id: summary
  uses: ly2xxx/.github/actions/commit-delta-summary@main
  with:
    ollama-api-key: ${{ secrets.OLLAMA_API_KEY }}
    custom-prompt: Call out any change to the public API.
- if: steps.summary.outputs.skipped == 'false'
  run: cat "${{ steps.summary.outputs.summary-file }}"
```

[`ly2xxx/interview-playground`](https://github.com/ly2xxx/interview-playground/blob/main/.github/workflows/commit-delta-summary.yml)
has a complete workflow that also comments on the pull request, updating one comment
instead of adding a new one each push.

| Input | Default | |
| :-- | :-- | :-- |
| `base`, `head` | from the event | revisions to compare; a push compares `before` with the pushed commit, a pull request its base with its head |
| `model` | `deepseek-v4-flash:cloud` | Ollama Cloud model |
| `max-diff-chars` | `60000` | the diff is truncated beyond this, with a note |
| `outfile` | `commit-delta-summary.md` | where the summary is written |
| `ollama-api-key` | | empty: the action skips and succeeds |
| `custom-prompt` | | instructions added to the default prompt |
| `system-prompt` | | replaces the default prompt |

Outputs: `summary-file` (empty when skipped) and `skipped`.

- **No key, no failure.** Pull requests from forks get no secrets, so without a key the
  action notes it in the job summary and succeeds.
- **A branch's first push** has no `before`; it compares the pushed commit with its parent.
- **A revision that doesn't resolve fails the step**, rather than summarising an empty diff.
