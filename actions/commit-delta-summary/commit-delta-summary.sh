#!/usr/bin/env bash
# Summarise the delta between two revisions using Ollama Cloud.
#
#   commit-delta-summary.sh <base> <head> <outfile>
#
# Env: OLLAMA_API_KEY (required), OLLAMA_MODEL, MAX_DIFF_CHARS, OLLAMA_HOST,
#      CUSTOM_PROMPT, SYSTEM_PROMPT.
#
# Lives beside action.yml so it travels with the composite action -- callers do
# not need a copy in their own repo. That is the whole point of packaging it as
# an action rather than a bare workflow.
set -euo pipefail

base="${1:?usage: commit-delta-summary.sh <base> <head> <outfile> [custom_prompt] [system_prompt]}"
head_rev="${2:?}"
outfile="${3:?}"
custom_prompt_arg="${4:-}"
system_prompt_arg="${5:-}"

MODEL="${OLLAMA_MODEL:-deepseek-v4-flash:cloud}"
MAX_DIFF_CHARS="${MAX_DIFF_CHARS:-60000}"
HOST="${OLLAMA_HOST:-https://ollama.com}"
CUSTOM_PROMPT="${custom_prompt_arg:-${CUSTOM_PROMPT:-}}"
SYSTEM_PROMPT="${system_prompt_arg:-${SYSTEM_PROMPT:-}}"

: "${OLLAMA_API_KEY:?OLLAMA_API_KEY is required}"
command -v jq >/dev/null || { echo "::error::jq is required"; exit 1; }

# Resolve both ends up front so a bad ref fails loudly rather than producing an
# empty diff that silently summarises nothing.
base_sha="$(git rev-parse --verify "${base}^{commit}" 2>/dev/null)" || {
  echo "::error::cannot resolve base revision '${base}'"; exit 1; }
head_sha="$(git rev-parse --verify "${head_rev}^{commit}" 2>/dev/null)" || {
  echo "::error::cannot resolve head revision '${head_rev}'"; exit 1; }

stat_block="$(git diff --stat "${base_sha}" "${head_sha}" || true)"
log_block="$(git log --no-merges --format='- %s' "${base_sha}..${head_sha}" || true)"
diff_block="$(git diff --no-color "${base_sha}" "${head_sha}" || true)"

if [ -z "${diff_block}" ]; then
  printf '## Commit Delta Summary\n\n_No changes between `%s` and `%s`._\n' \
    "${base}" "${head_rev}" > "${outfile}"
  exit 0
fi

truncated=""
if [ "${#diff_block}" -gt "${MAX_DIFF_CHARS}" ]; then
  diff_block="${diff_block:0:${MAX_DIFF_CHARS}}"
  truncated=$'\n\n_(diff truncated at '"${MAX_DIFF_CHARS}"$' characters)_'
fi

if [ -n "${SYSTEM_PROMPT}" ]; then
  base_instruction="${SYSTEM_PROMPT}"
else
  read -r -d '' base_instruction <<'BASE_PROMPT' || true
You are reviewing a code change. Write a concise summary in Markdown for a
reviewer who has not seen the diff.

Use these sections (unless overridden or extended by additional instructions):
## What changed
## Why it matters
## Risks & things to check

Be specific and factual. Cite file paths. Do not invent rationale that is not
evident from the diff. If the change is trivial, say so briefly rather than
padding.
BASE_PROMPT
fi

extra_instructions=""
if [ -n "${CUSTOM_PROMPT}" ]; then
  extra_instructions=$'\n\nAdditional instructions:\n'"${CUSTOM_PROMPT}"
fi

read -r -d '' prompt <<PROMPT || true
${base_instruction}${extra_instructions}

Commits:
${log_block}

Diffstat:
${stat_block}

Diff:
${diff_block}
PROMPT

payload="$(jq -n --arg m "${MODEL}" --arg p "${prompt}" \
  '{model:$m, stream:false, messages:[{role:"user", content:$p}]}')"

http_code=0
response="$(curl -sS --fail-with-body -m 300 \
  -w '\n%{http_code}' \
  -H "Authorization: Bearer ${OLLAMA_API_KEY}" \
  -H 'Content-Type: application/json' \
  -d "${payload}" \
  "${HOST}/api/chat" 2>&1)" || http_code=$?

if [ "${http_code}" -ne 0 ]; then
  echo "::error::Ollama request failed (curl exit ${http_code})"
  printf '%s\n' "${response}" | tail -5
  exit 1
fi

body="$(printf '%s' "${response}" | sed '$d')"
summary="$(printf '%s' "${body}" | jq -r '.message.content // empty')"

if [ -z "${summary}" ]; then
  echo "::error::Ollama returned no content"
  printf '%s\n' "${body}" | head -20
  exit 1
fi

{
  printf '## Commit Delta Summary\n\n'
  printf '`%s` → `%s` · model `%s`\n\n' "${base_sha:0:8}" "${head_sha:0:8}" "${MODEL}"
  printf '%s' "${summary}"
  printf '%s\n' "${truncated}"
  printf '\n<details><summary>Diffstat</summary>\n\n```\n%s\n```\n</details>\n' "${stat_block}"
} > "${outfile}"

echo "wrote ${outfile}"
