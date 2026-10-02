#!/usr/bin/env python3
"""An AI-native SDLC in GitHub Actions: Ollama Cloud writes the design, a
builder (Claude Code or a person) writes the code, and deterministic checks
decide whether it is done.

    sdlc_stage.py resolve        pick the feature folder, its branch and the first stage
    sdlc_stage.py stage NAME     write intent.md, spec.md or plan.md with Ollama Cloud
                                 and push it to the feature branch
    sdlc_stage.py freeze         tag the approved documents and hand the plan to the builder
    sdlc_stage.py wait           wait until build-log.md on the feature branch logs every phase
    sdlc_stage.py verify         check the built branch against the approved plan's phases
    sdlc_stage.py review         Ollama reviews the build against the spec (advisory)
    sdlc_stage.py pr             open the pull request for the feature branch

Between stages the workflow can wait on a GitHub environment with required
reviewers: a person reads the document on the feature branch, edits it there
if it needs changing, and approves. `freeze` then tags the approved documents
as sdlc/<feature>/approved, and `verify` fails if the builder changes them, so
the builder can't rewrite the plan it is checked against. `verify` runs locally
too, from a copy of this file:

    curl -sSfLo /tmp/sdlc_stage.py https://raw.githubusercontent.com/ly2xxx/.github/main/actions/sdlc-stage/sdlc_stage.py
    python /tmp/sdlc_stage.py verify --feature 002-x --phase 1 --test-command "python -m pytest -q"

Settings come from environment variables (the action sets them from its
inputs). Standard library only, so a runner needs nothing but python3, git and gh.
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
import urllib.request
from pathlib import Path

ACTION_DIR = Path(__file__).resolve().parent
STAGES = ("intent", "spec", "plan")
ORDER = (*STAGES, "build")
FEATURE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
PHASE_BRANCH = re.compile(rf"phase/(?P<feature>{FEATURE_NAME.pattern})/(?P<phase>[A-Za-z0-9._-]+)")
# A plan's machine-readable markers: <!-- phase: 1 -->, <!-- targets: a.py, tests/** -->, <!-- frozen: b.py -->
MARKER = re.compile(r"<!--\s*(?P<key>phase|targets|frozen)\s*:\s*(?P<value>.*?)\s*-->", re.IGNORECASE)
DEFAULTS = ACTION_DIR.parent / "defaults.env"   # the one place shared defaults are set
BOT = ("github-actions[bot]", "41898283+github-actions[bot]@users.noreply.github.com")
LOCAL_CHECK = "https://raw.githubusercontent.com/ly2xxx/.github/main/actions/sdlc-stage/sdlc_stage.py"


def env(name, default=""):
    return os.environ.get(name, "").strip() or default


def fail(message):
    print(f"::error::{message}", flush=True)
    sys.exit(1)


def sh(*cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        fail(f"{' '.join(cmd)[:200]} exited {r.returncode}: {r.stderr.strip()[-2000:]}")
    return r.stdout.strip()


def git(*args, check=True):
    return sh("git", "-c", "core.quotePath=false", *args, check=check)


def default(name):
    """A setting's default from actions/defaults.env."""
    for line in read(DEFAULTS, "").splitlines():
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip()
    fail(f"{name} is empty and {DEFAULTS} has no default for it.")


def read(path, missing=None):
    """A file's text. With `missing`, that instead when there is no path or no such file."""
    if missing is not None and not (path and Path(path).exists()):
        return missing
    return Path(path).read_text(encoding="utf-8")


def _append(variable, text):
    if path := os.environ.get(variable):
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def set_output(**values):
    _append("GITHUB_OUTPUT", "\n".join(f"{key}={value}" for key, value in values.items()))


def step_summary(text):
    _append("GITHUB_STEP_SUMMARY", text)


def publish(text, path):
    """A report goes to the log, the step summary and, if asked, a file."""
    if path:
        Path(path).write_text(text, encoding="utf-8")
    step_summary(text)
    print(text)


def repo_url(*parts):
    base = f"{env('GITHUB_SERVER_URL', 'https://github.com')}/{env('GITHUB_REPOSITORY')}"
    return "/".join([base, *parts])


def code_list(paths):
    return ", ".join(f"`{p}`" for p in paths)


# ---------------------------------------------------------------- the feature

def features_dir():
    return env("FEATURES_DIR", "sdlc/features").strip("/")


def folder_of(feature):
    return Path(features_dir(), feature)


def current_feature():
    return env("SDLC_FEATURE") or fail("SDLC_FEATURE is not set")


def branch_for(feature):
    return f"feature/{feature}"


def approved_tag(feature):
    return f"sdlc/{feature}/approved"


def wait_minutes():
    return int(env("BUILD_WAIT_MINUTES", "0") or 0)


def commit_as_bot():
    git("config", "user.name", BOT[0])
    git("config", "user.email", BOT[1])


# ---------------------------------------------------------------- model call

def ollama_model(command):
    """The model to call. Fails before any work when there is no API key."""
    env("OLLAMA_API_KEY") or fail(f"The {command} command needs ollama-api-key.")
    return env("OLLAMA_MODEL") or default("OLLAMA_MODEL")


def _stream(req, model, deadline):
    """Read one streamed /api/chat reply. Streaming keeps bytes flowing while a
    model thinks, so no proxy drops the connection as idle, and it lets the log
    show progress instead of minutes of silence."""
    content, thinking, last_note = [], 0, time.monotonic()
    # The timeout is per socket read: the longest silence tolerated between chunks.
    with urllib.request.urlopen(req, timeout=int(env("OLLAMA_IDLE_TIMEOUT", "300"))) as resp:
        for line in resp:
            if not line.strip():
                continue
            chunk = json.loads(line)
            if chunk.get("error"):
                raise RuntimeError(chunk["error"])
            message = chunk.get("message", {})
            content.append(message.get("content", ""))
            thinking += len(message.get("thinking", ""))
            now = time.monotonic()
            if now > deadline:
                raise TimeoutError(f"no complete answer within {env('OLLAMA_TIMEOUT', '1200')}s")
            if now - last_note > 30:
                print(f"  {model}: {thinking} thinking and {sum(map(len, content))} answer characters so far",
                      flush=True)
                last_note = now
            if chunk.get("done"):
                return "".join(content)
    raise http.client.IncompleteRead(b"", None)


def chat(model, prompt):
    payload = {"model": model, "stream": True, "messages": [{"role": "user", "content": prompt}]}
    think = env("OLLAMA_THINK").lower()
    if think:
        # true/false, or low/medium/high for models that take a level.
        payload["think"] = {"true": True, "false": False}.get(think, think)
    req = urllib.request.Request(
        env("OLLAMA_HOST", "https://ollama.com").rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + env("OLLAMA_API_KEY"), "Content-Type": "application/json"})
    deadline = time.monotonic() + int(env("OLLAMA_TIMEOUT", "1200"))
    print(f"Calling {model}" + (f" (think={think})" if think else ""), flush=True)
    for attempt in (1, 2):
        try:
            content = _stream(req, model, deadline)
            break
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            if attempt == 2 or e.code < 500:
                fail(f"Ollama returned HTTP {e.code} for {model}: {body}")
        except TimeoutError as e:
            fail(f"{model}: {e}. Try a faster model or OLLAMA_THINK=false.")
        except (urllib.error.URLError, OSError, http.client.HTTPException, RuntimeError, ValueError) as e:
            if attempt == 2 or time.monotonic() > deadline:
                fail(f"Ollama request to {model} failed: {e}")
        print(f"::warning::Ollama request to {model} failed; retrying once.", flush=True)
    # Some models put their reasoning inline instead of in the thinking field.
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
    return content or fail(f"{model} returned no answer")


def unfence(text):
    """Drop a single code fence wrapped around the whole answer."""
    m = re.fullmatch(r"```[a-zA-Z]*\n(.*)\n```", text.strip(), flags=re.DOTALL)
    return m.group(1) if m else text


# ---------------------------------------------------------------- repository context

def repo_tree(files, limit=400):
    shown = sorted(f for f in files if not f.startswith(("archive/", "image/", "demo/")))
    extra = len(shown) - limit
    return "\n".join(shown[:limit]) + (f"\n... and {extra} more" if extra > 0 else "")


def mentioned_files(text, files):
    """Tracked files that `text` names by path or by unique basename."""
    by_name = {}
    for f in files:
        by_name.setdefault(Path(f).name, []).append(f)
    hits = set()
    for token in set(re.findall(r"[\w./-]+\.\w+", text)):
        token = token.strip("./")
        if token in files:
            hits.add(token)
        elif len(by_name.get(token, [])) == 1:
            hits.add(by_name[token][0])
    return sorted(hits)


def file_block(path, budget):
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    if len(text) > budget:
        text = text[:budget] + "\n... (truncated)"
    return f"### {path}\n```\n{text}\n```\n"


# ---------------------------------------------------------------- plan.md

def glob_to_regex(pattern):
    """`*` and `?` stop at `/`; `**` crosses it, and `**/` also matches no directory."""
    out, i = [], 0
    while i < len(pattern):
        for token, regex in (("**/", r"(?:[^/]+/)*"), ("**", r".*"), ("*", r"[^/]*"), ("?", r"[^/]")):
            if pattern.startswith(token, i):
                out.append(regex)
                i += len(token)
                break
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path, patterns):
    return any(glob_to_regex(p).match(path) for p in patterns)


def parse_phases(text):
    """Each `## Phase` section: its id, title and short name, target and frozen
    globs, Definition of done checkboxes and the commands in its Verify block."""
    phases = []
    for section in re.split(r"^## (?=Phase\b)", text, flags=re.MULTILINE)[1:]:
        title = section.splitlines()[0].strip()
        marks = {"phase": "", "targets": [], "frozen": []}
        for m in MARKER.finditer(section):
            key, value = m.group("key").lower(), m.group("value")
            marks[key] = value.strip() if key == "phase" else [p.strip() for p in value.split(",") if p.strip()]
        verify = re.search(r"\*\*Verify[^\n]*\n+```[a-z]*\n(.*?)\n```", section, flags=re.DOTALL | re.IGNORECASE)
        phases.append({
            "id": marks["phase"], "title": title, "name": title.split(":", 1)[-1].strip(),
            "targets": marks["targets"], "frozen": marks["frozen"],
            "dod": re.findall(r"^- \[[ xX]\] (.+)$", section, flags=re.MULTILINE),
            "verify": verify.group(1).strip() if verify else "",
        })
    return phases


def section(markdown, heading):
    """The body of a `## heading` section, or None if there is none."""
    m = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", markdown, flags=re.MULTILINE | re.DOTALL)
    return m.group(1) if m else None


def bullets(markdown, heading):
    return re.findall(r"^\s*[-*] (.+)$", section(markdown, heading) or "", flags=re.MULTILINE)


def plan_problems(plan, intent):
    phases = parse_phases(plan)
    if not phases:
        return ["No `## Phase N: ...` sections."]
    problems = []
    for p in phases:
        for missing, problem in ((not p["id"], "has no <!-- phase: N --> marker."),
                                 (not p["targets"], "has no <!-- targets: ... --> marker."),
                                 (not p["dod"], "has no Definition of done checklist (- [ ] ...)."),
                                 (not p["verify"], "has no **Verify:** block with a fenced command.")):
            if missing:
                problems.append(f"'{p['title']}' {problem}")
    rows, wanted = section(plan, "Coverage"), len(bullets(intent, "Done when"))
    if rows is None:
        problems.append("No `## Coverage` table mapping intent.md's Done-when items to phases.")
    elif (got := len([r for r in rows.splitlines() if r.startswith("|")]) - 2) < wanted:
        problems.append(f"The Coverage table has {got} rows but intent.md has {wanted} Done-when items.")
    return problems


HAND_BACK = """## Hand back
When every phase is built and its Verify block passes:
1. Create `{folder}/build-log.md` with one section per phase, in order. Head each
   one `## Phase <n>: <title>`, then list the files changed, the Verify command
   you ran and its result, and any deviation from this plan (or "none").
2. Commit it and push it to `{branch}`.

Commit only this plan's targets and `build-log.md`. Leave every other file alone,
including other features' documents under `sdlc/features/`, even for formatting;
verification fails on any file outside the targets.

The pipeline waits for this file. Once it has a section for every phase, it
verifies the whole branch and opens the pull request.
"""


def with_hand_back(plan, folder):
    """Every plan ends with the same instructions for its builder, whoever or
    whatever it is: this text, replacing any the model wrote."""
    plan = re.sub(r"^## Hand back\b.*?(?=^## |\Z)", "", plan, flags=re.MULTILINE | re.DOTALL)
    return plan.rstrip() + "\n\n" + HAND_BACK.format(folder=folder.as_posix(), branch=branch_for(folder.name))


def logged_ids(text):
    """The phase ids a build log has a `## Phase <id>: ...` section for."""
    return set(re.findall(r"^## Phase ([^\s:]+)", text, flags=re.MULTILINE))


# ---------------------------------------------------------------- branches and the approved tag

def fetch_branch(branch):
    git("fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)


def base_ref(base):
    """origin/<base> when the remote has that branch, otherwise <base> itself (a tag, say)."""
    fetch_branch(base)
    return f"origin/{base}" if git("rev-parse", "--verify", "-q", f"origin/{base}", check=False) else base


def use_feature_branch(branch):
    """Check out the feature branch, creating it if new from the base branch
    (BASE_REF) or else the current commit. Called after the approval gate, so a
    person's edits on the branch are in."""
    if git("ls-remote", "--heads", "origin", branch):
        fetch_branch(branch)
        git("checkout", "-q", "-B", branch, f"origin/{branch}")
    elif (base := env("BASE_REF")) and base_ref(base) != base:
        git("checkout", "-q", "-B", branch, f"origin/{base}")
    else:
        git("checkout", "-q", "-B", branch)


def blob(ref, path):
    """The file at `ref` as bytes, or None if it isn't there."""
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def approved_ref(feature):
    """The tag `freeze` put on the approved documents, or "" if there is none."""
    tag = approved_tag(feature)
    git("fetch", "-q", "--no-tags", "origin", f"+refs/tags/{tag}:refs/tags/{tag}", check=False)
    return tag if git("rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}", check=False) else ""


def approved_text(ref, path):
    """A document as approved, or as it is on the branch when nothing was approved."""
    if ref:
        data = blob(ref, path.as_posix())
        return data.decode("utf-8") if data is not None else ""
    return read(path, "")


def approved_plan(feature):
    """The approved tag ("" if there is none) and plan.md as approved."""
    tag = approved_ref(feature)
    return tag, approved_text(tag, folder_of(feature) / "plan.md")


def unlogged(feature, phase_ids):
    """The phase ids that build-log.md on the pushed feature branch doesn't log yet."""
    branch = branch_for(feature)
    fetch_branch(branch)
    log = blob(f"origin/{branch}", (folder_of(feature) / "build-log.md").as_posix())
    logged = logged_ids(log.decode("utf-8") if log else "")
    return [i for i in phase_ids if i not in logged]


def contract_changes(ref, folder):
    """The approved documents that now differ from the working tree."""
    paths = [folder / f"{name}.md" for name in STAGES]
    return [str(p) for p in paths if blob(ref, p.as_posix()) != (p.read_bytes() if p.exists() else None)]


def new_feature_name(features_dir, idea):
    """NNN-slug-of-the-idea, numbered after the highest feature on the base or on
    any feature/ branch, so two ideas in flight don't share a number."""
    root = Path(features_dir)
    names = [d.name for d in root.iterdir()] if root.is_dir() else []
    names += [ref.rsplit("/", 1)[-1] for ref in git("ls-remote", "--heads", "origin", "feature/*").split()]
    numbers = [int(m.group(1)) for n in names if (m := re.match(r"(\d{3})-", n))]
    slug = "-".join(re.findall(r"[a-z0-9]+", idea.lower())[:6])[:40].strip("-") or "feature"
    return f"{max(numbers, default=0) + 1:03d}-{slug}"


# ---------------------------------------------------------------- resolve

def from_issue(body):
    """The `feature: <name>` and `start: <stage>` lines of an issue body (None when absent)."""
    feature = re.search(rf"^\s*feature:\s*({FEATURE_NAME.pattern})\s*$", body, flags=re.MULTILINE)
    start = re.search(r"^\s*start:\s*(spec|plan|build)\s*$", body, flags=re.MULTILINE)
    return (feature and feature.group(1)), (start and start.group(1))


def resolved(feature, start, mode, runs=()):
    """mode: design (this run writes documents and hands the plan to the builder),
    build (it verifies, reviews and opens the pull request) or none."""
    set_output(feature=feature, branch=branch_for(feature), start=start, base=env("BASE_REF"), mode=mode,
               design=str(mode == "design").lower(), **{s: str(s in runs).lower() for s in STAGES})


def resolve(args):
    idea, start, feature = env("SDLC_IDEA"), env("SDLC_START", "auto"), env("SDLC_FEATURE")
    # An issue labelled for the pipeline starts a run: its title is the idea, a
    # `feature: <name>` line in its body revises that feature, and a
    # `start: <stage>` line with it runs that feature from the stage instead
    # (`start: build` verifies it and opens the pull request again).
    body = env("SDLC_ISSUE_BODY")
    if idea and not feature and body:
        named, from_stage = from_issue(body)
        if named and from_stage:
            feature, idea, start = named, "", from_stage
            print(f"The issue asks for feature {feature} from {start}.")
        elif named:
            feature = named
            print(f"The issue names feature {feature}: revising it.")
    # A merged pull request from phase/<feature>/<n> into the feature branch is
    # the builder handing back: once build-log.md logs every phase of the
    # approved plan, this is the build run. That needs nothing from the builder
    # but the merge, so a builder that can't start workflow runs hands back too.
    merged = env("SDLC_MERGED_PHASE")
    if merged and not (feature or idea):
        m = PHASE_BRANCH.fullmatch(merged) or fail(f"{merged} isn't a phase/<feature>/<phase> branch.")
        feature, start = m.group("feature"), "build"
        todo = unlogged(feature, [p["id"] for p in parse_phases(approved_plan(feature)[1])])
        if todo:
            print(f"{merged} is merged. Still to build (not in build-log.md): phase {', '.join(todo)}.")
            step_summary(f"**Feature** `{feature}`: `{merged}` is merged. The build run starts when build-log.md "
                         f"logs every phase; still to build: phase {', '.join(todo)}.")
            resolved(feature, "build", "none")
            return
        print(f"{merged} is merged and build-log.md logs every phase: handing back for the build run.")
    if start != "auto" and start not in ORDER:
        fail(f"Unknown start stage: {start}")
    if idea and start not in ("auto", "intent"):
        fail("An idea starts at the intent stage. Leave start on auto or intent.")
    if start == "intent" and not idea:
        fail("The intent stage needs an idea.")
    if not feature:
        if not idea:
            fail("Give an idea to start a new feature, or name an existing feature folder.")
        feature = new_feature_name(features_dir(), idea)
    if not FEATURE_NAME.fullmatch(feature):
        fail(f"Bad feature name '{feature}': use lowercase letters, digits and hyphens.")
    branch, folder = branch_for(feature), folder_of(feature)
    use_feature_branch(branch)
    have = {s: (folder / f"{s}.md").exists() for s in STAGES}
    if start == "auto":
        start = "intent" if idea else next((s for s in STAGES if not have[s]), "build")
    if start != "intent" and not have["intent"]:
        fail(f"{folder}/intent.md doesn't exist on {branch} or the base. Start with an idea.")
    if start == "plan" and not have["spec"]:
        fail(f"{folder}/spec.md doesn't exist yet. Start at spec.")
    if start == "build" and not have["plan"]:
        fail(f"{folder}/plan.md doesn't exist yet. Start at plan.")
    if start == "intent" and have["intent"]:
        later = [f"{s}.md" for s in ("spec", "plan") if have[s]]
        print(f"::warning::{feature} already has an intent.md. This run revises it"
              + (f" and then regenerates {', '.join(later)}." if later else "."))
    print(f"Feature {feature} on {branch}, starting at {start}.")
    step_summary(f"**Feature** `{feature}` · **branch** [`{branch}`]({repo_url('tree', branch)}) · "
                 f"**starts at** {start}")
    resolved(feature, start, "build" if start == "build" else "design", ORDER[ORDER.index(start):])


# ---------------------------------------------------------------- stage

def header(stage, model, sources):
    return (f"<!-- sdlc stage={stage} model={model} "
            f"from={','.join(sources)}@{git('rev-parse', '--short', 'HEAD')} -->\n")


def prompt_parts(stage, folder, files, budget, idea):
    parts, sources = [read(ACTION_DIR / "prompts" / f"{stage}.md")], []
    if stage == "intent":
        parts += [f"## Owner\n@{env('ACTOR', 'unknown')}", "## The idea\n" + idea]
        sources.append("idea")
        if (folder / "intent.md").exists():
            parts.append("## Existing intent.md (revise this; keep what the idea does not change)\n"
                         + read(folder / "intent.md"))
            sources.append("intent.md")
    else:
        for name in STAGES[: STAGES.index(stage)]:
            parts.append(f"## {name}.md\n" + read(folder / f"{name}.md"))
            sources.append(f"{name}.md")
    named = mentioned_files("\n".join(parts[1:]), files)
    if stage == "plan":
        parts.append(f"## Feature\nFolder: `{folder.as_posix()}`. Branch: `{branch_for(folder.name)}`. "
                     f"Approved tag: `{approved_tag(folder.name)}`, created when this plan is approved; it marks the "
                     "approved documents and the code before the build.")
        if env("TEST_COMMAND"):
            parts.append("## How verification runs\nThe verify job installs the project into a virtualenv that is "
                         "on PATH (from pyproject.toml if there is one, otherwise requirements.txt, plus pytest). It "
                         "runs each phase's Verify block from the repository root, then the whole suite with "
                         f"`{env('TEST_COMMAND')}`, which fails if it collects no tests.")
    parts.append("## Repository files\n" + repo_tree(files))
    if Path("README.md").exists():
        parts.append("## README.md (start)\n" + read("README.md")[:4000])
    used = sum(map(len, parts))
    for path in named:
        if path.startswith(str(folder)) or budget - used < 1000:
            continue
        parts.append(file_block(path, budget - used))
        used += len(parts[-1])
    return parts, sources


def stage(args):
    name = args.name
    if name not in STAGES:
        fail(f"Unknown stage {name}")
    model, feature = ollama_model("stage"), current_feature()
    branch, folder = branch_for(feature), folder_of(feature)
    use_feature_branch(branch)
    parts, sources = prompt_parts(name, folder, set(git("ls-files").splitlines()),
                                  int(env("MAX_CONTEXT_CHARS", "60000")), env("SDLC_IDEA"))
    print(f"Writing {name}.md for {feature}", flush=True)
    prompt = "\n\n".join(parts)
    text, problems = unfence(chat(model, prompt)), []
    if name == "plan":
        intent = read(folder / "intent.md")
        if problems := plan_problems(text, intent):
            print("::warning::The plan is incomplete; asking once more: " + " ".join(problems), flush=True)
            retry = ("\n\n## Your previous plan was rejected\n" + "\n".join(f"- {p}" for p in problems)
                     + "\n\n## Your previous plan\n" + text + "\n\nWrite the whole plan again, fixed.")
            text = unfence(chat(model, prompt + retry))
            problems = plan_problems(text, intent)
        text = with_hand_back(text, folder)

    path = folder / f"{name}.md"
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(header(name, model, sources) + text + "\n", encoding="utf-8")
    commit_as_bot()
    git("add", str(path))
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        print(f"{path} is unchanged.")
    else:
        git("commit", "-q", "-m", f"sdlc({feature}): {name}\n\nWritten by the {name} stage with {model}.")
        git("push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    summary = [f"## {name}.md is ready for review", "",
               f"[View]({repo_url('blob', branch, str(path))}) · [Edit on the branch]"
               f"({repo_url('edit', branch, str(path))}) · model `{model}`", "",
               "Read it, edit it on the branch if it needs changing, then approve the next "
               "**Review** job (Review deployments). Rejecting stops the run. Where the review "
               "environment has no required reviewers, the run carries on by itself.", ""]
    if problems:
        summary += ["> [!WARNING]", "> The plan still has problems. Fix them on the branch before approving:"]
        summary += [f"> - {p}" for p in problems] + [""]
        for p in problems:
            print(f"::warning::{p}")
    step_summary("\n".join(summary) + "\n---\n\n" + read(path))
    set_output(path=str(path))


# ---------------------------------------------------------------- verify

def changed_files(phase, base, folder):
    """The files the build changed, outside the feature folder, and what they were
    compared with. One phase is checked against uncommitted work, or with a base
    against what its phase branch changed; the whole feature against everything
    the branch changed since the base."""
    if phase and not base:
        changed = git("diff", "--name-only", "HEAD").splitlines()
        changed += git("ls-files", "--others", "--exclude-standard").splitlines()
        against = "uncommitted changes"
    else:
        base or fail("Give --base (the branch the feature goes into).")
        changed = git("diff", "--name-only", f"{base_ref(base)}...HEAD").splitlines()
        against = f"changes since `{base}`"
    return sorted({p for p in changed if p and not p.startswith(f"{folder}/")}), against


def scope(changed, phases):
    """(outside, frozen): the changed files that no phase in `phases` targets,
    split into the ones those phases freeze and the rest."""
    targets = [t for p in phases for t in p["targets"]]
    loose = [p for p in changed if not matches_any(p, targets)]
    frozen = [p for p in loose if matches_any(p, [f for ph in phases for f in ph["frozen"]])]
    return [p for p in loose if p not in frozen], frozen


def run_check(command, timeout=900):
    try:
        r = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def verify(args):
    feature = args.feature or fail("Name the feature (--feature).")
    folder = folder_of(feature)
    # The plan the builder is checked against is the approved one, not whatever
    # plan.md says on the branch the builder pushes to.
    tag, plan = approved_plan(feature)
    if not plan:
        fail(f"{folder / 'plan.md'} doesn't exist" + (f" at {tag}" if tag else ""))
    phases = parse_phases(plan)
    ids = [p["id"] for p in phases]
    if args.phase and args.phase not in ids:
        fail(f"plan.md has no phase {args.phase}")
    # A phase is scoped to its own targets and frozen files and re-runs the Verify
    # blocks of every phase up to it; the whole feature uses every phase.
    upto = phases[: ids.index(args.phase) + 1] if args.phase else phases
    changed, against = changed_files(args.phase, args.base, folder)
    outside, frozen = scope(changed, upto[-1:] if args.phase else phases)
    tampered = contract_changes(tag, folder) if tag else []

    rows = [("Whole test suite", args.test_command, *run_check(args.test_command))] if args.test_command else []
    for p in upto:
        rows.append((f"Phase {p['id']}: {p['name']}", p["verify"], *run_check(p["verify"])) if p["verify"] else
                    (f"Phase {p['id']}", "(no Verify block)", 1, "plan.md gives this phase no Verify command"))
    ok = bool(changed) and not (outside or frozen or tampered) and all(code == 0 for _, _, code, _ in rows)

    if not tag:
        contract = f"no `{approved_tag(feature)}` tag, so plan.md was read from the branch unchecked."
    elif not tampered:
        contract = f"intent.md, spec.md and plan.md match `{tag}`."
    else:
        contract = (f"**changed after approval:** {code_list(tampered)}. Run the design stages again instead "
                    "of editing them.")
    report = [f"# Verification: {feature}" + (f", phase {args.phase}" if args.phase else ""),
              f"**Result: {'PASSED' if ok else 'FAILED'}**\n", ""]
    if not changed:
        report.append(f"> [!WARNING]\n> No code changed ({against}). Nothing has been built yet.\n")
    report += [f"**Scope** ({against}): {len(changed)} file(s) changed. "
               + (f"**{len(outside)} outside the plan:** {code_list(outside)}" if outside else
                  "All inside the plan's targets."),
               "**Frozen files:** " + (f"**modified:** {code_list(frozen)}" if frozen else "none touched."),
               f"**Contract:** {contract}", "", "| Check | Result |", "| :-- | :-- |"]
    report += [f"| {name} | {'✅ passed' if code == 0 else f'❌ exit {code}'} |" for name, _, code, _ in rows]
    report.append("")
    for name, command, code, out in rows:
        report += [f"<details><summary>{name}: {'passed' if code == 0 else 'failed'}</summary>", "",
                   "```bash", command, "```", "```", out.strip(), "```", "</details>", ""]
    publish("\n".join(report), args.report)
    set_output(passed=str(ok).lower())
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- freeze

def handoff(feature, branch, tag, sha, phases, builder):
    """The step summary that pauses the pipeline for the builder."""
    test_command = env("TEST_COMMAND", "python -m pytest -q")
    who = "Claude Code" if builder == "claude" else "a person"
    log = (folder_of(feature) / "build-log.md").as_posix()
    lines = [f"## ⏸ Step 4: waiting for the build (builder: {who})", "",
             f"The design is frozen: `{tag}` marks intent.md, spec.md and plan.md at `{sha}` on "
             f"[`{branch}`]({repo_url('tree', branch)}). Verification fails if any of them change, so the "
             "builder builds the plan but can't rewrite it. Changing it means running the design stages again.", "",
             "**Phases to build:**"]
    lines += [f"{i}. **Phase {p['id']}**: {p['name']} ({code_list(p['targets'])})" for i, p in enumerate(phases, 1)]
    lines += ["", "**Build it**, one phase at a time, changing only that phase's targets:", "", "```bash",
              f"git fetch origin && git switch -c phase/{feature}/<n> origin/{branch}",
              f"curl -sSfLo /tmp/sdlc_stage.py {LOCAL_CHECK}",
              f'python /tmp/sdlc_stage.py verify --feature {feature} --phase <n> --test-command "{test_command}"',
              "```", "",
              f"Add a `## Phase <n>: <title>` section to `{log}` for each phase. Then open a pull request "
              f"into `{branch}`, where the phase check runs, and merge it; or push straight to `{branch}`.", ""]
    if (minutes := wait_minutes()) > 0:
        lines.append(f"**Hand back.** This job now waits, for up to {minutes} minutes, until `build-log.md` on "
                     f"`{branch}` has a `## Phase <id>: ...` section for every phase. Then this same run carries "
                     "on: step 5 verifies the build and Ollama reviews it, and step 6 opens the pull request. "
                     f"If it stops waiting first, run **SDLC Pipeline** with feature `{feature}` and start "
                     "`build` once the build is done.")
    else:
        lines.append(f"**Hand back.** When every phase is in `{branch}`, run **SDLC Pipeline** with feature "
                     f"`{feature}` (start `auto` or `build`). That run verifies the build, has Ollama review it, "
                     "and opens the pull request.")
    return "\n".join(lines)


def freeze(args):
    feature, builder = current_feature(), env("SDLC_BUILDER", "claude").lower()
    if builder not in ("claude", "human"):
        fail(f"builder must be claude or human, not {builder}")
    branch, folder = branch_for(feature), folder_of(feature)
    use_feature_branch(branch)
    if missing := [f"{s}.md" for s in STAGES if not (folder / f"{s}.md").exists()]:
        fail(f"{folder} has no {', '.join(missing)}, so there is nothing to build yet.")
    plan = read(folder / "plan.md")
    if problems := plan_problems(plan, read(folder / "intent.md")):
        step_summary("## plan.md can't be built yet\n\n" + "\n".join(f"- {p}" for p in problems))
        fail("plan.md can't be built yet: " + " ".join(problems)
             + " Fix it on the branch and re-run this job, or run the plan stage again.")
    tag, sha = approved_tag(feature), git("rev-parse", "--short", "HEAD")
    commit_as_bot()
    git("tag", "-f", "-a", tag, "-m", f"Approved intent, spec and plan for {feature}\n\nBuilder: {builder}")
    git("push", "-f", "-q", "origin", f"refs/tags/{tag}")
    print(f"Tagged {tag} at {sha}. Waiting for {builder} to build {branch}.")
    step_summary(handoff(feature, branch, tag, sha, parse_phases(plan), builder))
    set_output(tag=tag, sha=sha)


# ---------------------------------------------------------------- wait

def wait(args):
    """Step 4's pause, inside the run: poll the feature branch until build-log.md
    logs every phase of the approved plan, so the same run goes on to verify
    and open the pull request. Needs no environment gate, so it works in a
    private repository too."""
    feature, minutes = current_feature(), wait_minutes()
    if minutes <= 0:
        print("Not waiting for the build (build-wait-minutes is 0): this run ends at the hand-off.")
        set_output(built="false")
        return
    branch = branch_for(feature)
    tag = approved_ref(feature) or fail(f"{approved_tag(feature)} doesn't exist: freeze the design first.")
    planned = [p["id"] for p in parse_phases(approved_text(tag, folder_of(feature) / "plan.md"))]
    interval, deadline, last = int(env("BUILD_POLL_SECONDS", "30")), time.monotonic() + minutes * 60, None
    print(f"Waiting up to {minutes} minutes for {branch} to log phase(s) {', '.join(planned)} in build-log.md.",
          flush=True)
    while True:
        todo = unlogged(feature, planned)
        if todo != last:
            head = git("rev-parse", "--short", f"origin/{branch}", check=False)
            print(f"{time.strftime('%H:%M:%S')} {branch}@{head}: "
                  + (f"still to build: phase {', '.join(todo)}" if todo else "every phase is logged"), flush=True)
            last = todo
        if not todo:
            step_summary(f"## ▶ Built\n\n`build-log.md` on `{branch}` logs every phase "
                         f"({', '.join(planned)}). Carrying on with step 5.")
            set_output(built="true")
            return
        if time.monotonic() > deadline:
            step_summary(f"## ⏸ Stopped waiting after {minutes} minutes\n\nStill to build: phase "
                         f"{', '.join(todo)}. When the build is done, run **SDLC Pipeline** with feature "
                         f"`{feature}` and start `build`, or open an `sdlc` issue with `feature: {feature}` "
                         "and `start: build` in its body.")
            fail(f"No complete build within {minutes} minutes (still to build: phase {', '.join(todo)}).")
        time.sleep(interval)


# ---------------------------------------------------------------- review

def review(args):
    """Ollama reads the approved spec and plan and the whole diff, and says
    whether the build does what the spec asks. Advisory: the checks decide."""
    model, feature = ollama_model("review"), current_feature()
    base = env("BASE_REF") or fail("BASE_REF is not set")
    folder, tag = folder_of(feature), approved_ref(feature)
    ref = base_ref(base)
    skip = [f":(exclude){(folder / f'{s}.md').as_posix()}" for s in STAGES]
    stat = git("diff", "--stat", f"{ref}...HEAD", "--", ".", *skip)
    diff = git("diff", "--no-color", f"{ref}...HEAD", "--", ".", *skip) or \
        fail(f"Nothing changed since {base}, so there is nothing to review.")
    budget, note = int(env("MAX_CONTEXT_CHARS", "60000")), ""
    if len(diff) > budget:
        diff, note = diff[:budget], f"\n\n(The diff is truncated at {budget} characters.)"
    parts = [read(ACTION_DIR / "prompts" / "review.md")]
    parts += [f"## {s}.md (approved)\n" + approved_text(tag, folder / f"{s}.md") for s in STAGES]
    parts += [f"## Diffstat\n```\n{stat}\n```", f"## Diff\n```diff\n{diff}\n```{note}"]
    print(f"Reviewing {feature} with {model}", flush=True)
    text = unfence(chat(model, "\n\n".join(parts)))
    publish("\n".join([f"<!-- sdlc stage=review model={model} -->", "## Ollama review (advisory)", "",
                       f"`{model}` compared the diff with the approved spec and plan. Its review informs the "
                       "merge but doesn't gate it; the verification above does.", "", text, ""]), args.report)
    set_output(path=args.report or "")


# ---------------------------------------------------------------- pr

def pr_body(feature, tag, phases, passed, sections):
    folder, branch = folder_of(feature), branch_for(feature)
    docs = [n + ".md" for n in STAGES] + (["build-log.md"] if (folder / "build-log.md").exists() else [])
    links = " · ".join(f"[{d}]({repo_url('blob', branch, str(folder / d))})" for d in docs)
    frozen = f", frozen at [`{tag}`]({repo_url('tree', tag)})" if tag else ""
    body = [f"Built from {links}{frozen}, each approved in the SDLC Pipeline run.", "",
            ("Verification **passed**. Review the code, then merge." if passed else
             "Verification **failed**, so this is a draft. Push fixes to "
             f"`{branch}` and re-run the failed jobs."), "", "## Phases"]
    for p in phases:
        body.append(f"- [{'x' if passed else ' '}] **Phase {p['id']}**: {p['name']}")
        body += [f"  - {d}" for d in p["dod"]]
    for text in sections:
        if text:
            body += ["", text]
    return "\n".join(body)[:60000]


def pr(args):
    feature = current_feature()
    base = env("BASE_REF") or fail("BASE_REF is not set")
    branch, passed = branch_for(feature), env("VERIFY_PASSED") == "true"
    title_m = re.search(r"^# Intent:\s*(.+)$", read(folder_of(feature) / "intent.md"), flags=re.MULTILINE)
    title = f"{feature}: {title_m.group(1).strip() if title_m else 'feature'}"
    tag, plan = approved_plan(feature)
    body = pr_body(feature, tag, parse_phases(plan), passed, [
        read(args.report, "_The verification report is missing._"), read(args.security, ""),
        read(args.review, "_The Ollama review didn't run._") if args.review else ""])
    if url := sh("gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url"):
        sh("gh", "pr", "edit", url, "--title", title, "--body", body)
        if passed:
            sh("gh", "pr", "ready", url, check=False)
    else:
        r = subprocess.run(["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body,
                            *([] if passed else ["--draft"])], capture_output=True, text=True)
        if r.returncode:
            hint = ""
            if "not permitted" in r.stderr:
                hint = (" Allow it under Settings → Actions → General → Workflow permissions (\"Allow GitHub "
                        "Actions to create and approve pull requests\"), or add an SDLC_PR_TOKEN secret. Until then "
                        "the builder can open it: the title and body are in this job's log and summary.")
                # The pull request this run would have opened, so a builder that can open
                # pull requests but not change the setting can open exactly this one.
                print(f"----- pull request: {branch} -> {base} -----\n{title}\n----- body -----\n{body}\n"
                      "----- end of pull request -----", flush=True)
                step_summary(f"## The pull request to open\n\n`{branch}` → `{base}`: **{title}**\n\n---\n\n{body}")
            fail(f"gh pr create exited {r.returncode}: {r.stderr.strip()[-1000:]}{hint}")
        url = r.stdout.strip()
    print(f"Pull request: {url}")
    step_summary(f"Pull request: {url}")
    set_output(pr=url)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    commands = {}
    for name, func in (("resolve", resolve), ("stage", stage), ("freeze", freeze), ("wait", wait),
                       ("verify", verify), ("review", review), ("pr", pr)):
        commands[name] = sub.add_parser(name)
        commands[name].set_defaults(func=func)
    # Each option defaults to the environment variable the action sets.
    commands["stage"].add_argument("name", nargs="?", default=env("STAGE"), help="intent, spec or plan")
    v = commands["verify"]
    v.add_argument("--feature", default=env("SDLC_FEATURE"))
    v.add_argument("--phase", default=env("SDLC_PHASE"),
                   help="check one phase: uncommitted changes, or with --base its phase branch")
    v.add_argument("--base", default=env("BASE_REF"), help="branch the feature (or, with --phase, the phase) goes into")
    v.add_argument("--test-command", default=env("TEST_COMMAND"))
    for name, option, variable, text in (("verify", "--report", "REPORT", "also write the report to this file"),
                                         ("review", "--report", "REPORT", "also write the review to this file"),
                                         ("pr", "--report", "REPORT", "the verification report"),
                                         ("pr", "--review", "REVIEW", "the Ollama review"),
                                         ("pr", "--security", "SECURITY", "the security scan report")):
        commands[name].add_argument(option, default=env(variable), help=text)
    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
