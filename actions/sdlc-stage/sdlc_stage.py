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

Standard library only, so a runner needs nothing but python3, git and gh.
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
FEATURE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
# The markers deterministic-coding's phase_check.py reads, so the same plan
# works with that script locally.
MARKER = re.compile(r"<!--\s*(?P<key>phase|targets|frozen)\s*:\s*(?P<value>.*?)\s*-->", re.IGNORECASE)


def env(name, default=""):
    return os.environ.get(name, "").strip() or default


def fail(message):
    print(f"::error::{message}", flush=True)
    sys.exit(1)


def sh(*cmd, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if check and r.returncode:
        fail(f"{' '.join(cmd[:3])} exited {r.returncode}: {r.stderr.strip()[-2000:]}")
    return r.stdout.strip()


def git(*args, check=True):
    return sh("git", *args, check=check)


def set_output(**values):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            for key, value in values.items():
                fh.write(f"{key}={value}\n")


def step_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")


def repo_url(*parts):
    base = f"{env('GITHUB_SERVER_URL', 'https://github.com')}/{env('GITHUB_REPOSITORY')}"
    return "/".join([base, *parts])


# ---------------------------------------------------------------- model call

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


def chat(model, prompt, want_json=False):
    payload = {"model": model, "stream": True,
               "messages": [{"role": "user", "content": prompt}]}
    if want_json:
        payload["format"] = "json"
    think = env("OLLAMA_THINK").lower()
    if think:
        # true/false, or low/medium/high for models that take a level.
        payload["think"] = {"true": True, "false": False}.get(think, think)
    req = urllib.request.Request(
        env("OLLAMA_HOST", "https://ollama.com").rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + env("OLLAMA_API_KEY"),
                 "Content-Type": "application/json"})
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
    if not content:
        fail(f"{model} returned no answer")
    return content


def unfence(text):
    """Drop a single code fence wrapped around the whole answer."""
    m = re.fullmatch(r"```[a-zA-Z]*\n(.*)\n```", text.strip(), flags=re.DOTALL)
    return m.group(1) if m else text


# ---------------------------------------------------------------- repository context

def tracked_files():
    return set(git("ls-files").splitlines())


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
    """phase_check.py's glob rules: `*` stops at `/`, `**` crosses it."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append(r"(?:[^/]+/)*"); i += 3
        elif pattern.startswith("**", i):
            out.append(r".*"); i += 2
        elif pattern[i] == "*":
            out.append(r"[^/]*"); i += 1
        elif pattern[i] == "?":
            out.append(r"[^/]"); i += 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path, patterns):
    return any(glob_to_regex(p).match(path) for p in patterns)


def parse_phases(text):
    """Each `## Phase` section: its id, title, target and frozen globs, Definition
    of Done checkboxes and the commands in its Verify block."""
    phases = []
    sections = re.split(r"^## (?=Phase\b)", text, flags=re.MULTILINE)[1:]
    for section in sections:
        title = section.splitlines()[0].strip()
        marks = {"phase": "", "targets": [], "frozen": []}
        for m in MARKER.finditer(section):
            key, value = m.group("key").lower(), m.group("value")
            marks[key] = value.strip() if key == "phase" else [p.strip() for p in value.split(",") if p.strip()]
        verify = re.search(r"\*\*Verify[^\n]*\n+```[a-z]*\n(.*?)\n```", section, flags=re.DOTALL | re.IGNORECASE)
        phases.append({
            "id": marks["phase"], "title": title, "targets": marks["targets"], "frozen": marks["frozen"],
            "dod": re.findall(r"^- \[[ xX]\] (.+)$", section, flags=re.MULTILINE),
            "verify": verify.group(1).strip() if verify else "",
        })
    return phases


def bullets(markdown, heading):
    m = re.search(rf"^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)", markdown, flags=re.MULTILINE | re.DOTALL)
    return re.findall(r"^\s*[-*] (.+)$", m.group(1), flags=re.MULTILINE) if m else []


def plan_problems(plan, intent):
    phases = parse_phases(plan)
    if not phases:
        return ["No `## Phase N: ...` sections."]
    problems = []
    for p in phases:
        name = f"'{p['title']}'"
        if not p["id"]:
            problems.append(f"{name} has no <!-- phase: N --> marker.")
        if not p["targets"]:
            problems.append(f"{name} has no <!-- targets: ... --> marker.")
        if not p["dod"]:
            problems.append(f"{name} has no Definition of done checklist (- [ ] ...).")
        if not p["verify"]:
            problems.append(f"{name} has no **Verify:** block with a fenced command.")
    rows = re.search(r"^## Coverage\s*\n(.*?)(?=^## |\Z)", plan, flags=re.MULTILINE | re.DOTALL)
    wanted = len(bullets(intent, "Done when"))
    got = len([r for r in rows.group(1).splitlines() if r.startswith("|")]) - 2 if rows else 0
    if not rows:
        problems.append("No `## Coverage` table mapping intent.md's Done-when items to phases.")
    elif got < wanted:
        problems.append(f"The Coverage table has {got} rows but intent.md has {wanted} Done-when items.")
    return problems


HAND_BACK = """## Hand back
When every phase is built and its Verify block passes:
1. Create `{folder}/build-log.md` with one section per phase, in order. Head each
   one `## Phase <n>: <title>`, then list the files changed, the Verify command
   you ran and its result, and any deviation from this plan (or "none").
2. Commit it and push it to `{branch}`.

The pipeline waits for this file. Once it has a section for every phase, it
verifies the whole branch and opens the pull request.
"""


def with_hand_back(plan, folder):
    """Every plan tells its builder, whoever or whatever it is, how to hand back."""
    if re.search(r"^## Hand back\b", plan, flags=re.MULTILINE):
        return plan
    return plan.rstrip() + "\n\n" + HAND_BACK.format(folder=folder.as_posix(), branch=branch_for(folder.name))


# ---------------------------------------------------------------- branches

def branch_for(feature):
    return f"feature/{feature}"


def remote_has(branch):
    return bool(git("ls-remote", "--heads", "origin", branch))


def use_feature_branch(branch):
    """Check out the feature branch, creating it if new from the base branch
    (BASE_REF) or else the current commit. Called after the approval gate, so a
    person's edits on the branch are in."""
    if remote_has(branch):
        git("fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
        git("checkout", "-q", "-B", branch, f"origin/{branch}")
        return
    base = env("BASE_REF")
    if base:
        git("fetch", "-q", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}", check=False)
    if base and git("rev-parse", "--verify", "-q", f"origin/{base}", check=False):
        git("checkout", "-q", "-B", branch, f"origin/{base}")
    else:
        git("checkout", "-q", "-B", branch)


def approved_tag(feature):
    return f"sdlc/{feature}/approved"


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
    return path.read_text(encoding="utf-8") if path.exists() else ""


def contract_changes(ref, folder):
    """The approved documents that now differ from the working tree."""
    changed = []
    for name in STAGES:
        path = folder / f"{name}.md"
        if blob(ref, path.as_posix()) != (path.read_bytes() if path.exists() else None):
            changed.append(str(path))
    return changed


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

PHASE_BRANCH = re.compile(r"phase/(?P<feature>[a-z0-9][a-z0-9-]{0,63})/(?P<phase>[A-Za-z0-9._-]+)")


def logged_ids(text):
    """The phase ids a build log has a `## Phase <id>: ...` section for."""
    return set(re.findall(r"^## Phase ([^\s:]+)", text, flags=re.MULTILINE))


def logged_phases(folder):
    log = folder / "build-log.md"
    return logged_ids(log.read_text(encoding="utf-8") if log.exists() else "")


def resolve(args):
    idea, start = env("SDLC_IDEA"), env("SDLC_START", "auto")
    features_dir = env("FEATURES_DIR", "sdlc/features").strip("/")
    feature = env("SDLC_FEATURE")
    # A merged pull request from phase/<feature>/<n> into the feature branch is
    # the builder handing back: once build-log.md logs every phase of the
    # approved plan, this is the build run. That needs nothing from the builder
    # but the merge, so a builder that can't start workflow runs hands back too.
    # An issue labelled for the pipeline starts a run: its title is the idea, a
    # `feature: <name>` line in its body revises that feature, and a
    # `start: <stage>` line with it runs that feature from the stage instead
    # (`start: build` verifies it and opens the pull request again).
    body = env("SDLC_ISSUE_BODY")
    if idea and not feature and body:
        m = re.search(r"^\s*feature:\s*([a-z0-9][a-z0-9-]{0,63})\s*$", body, flags=re.MULTILINE)
        if m:
            feature = m.group(1)
            s = re.search(r"^\s*start:\s*(spec|plan|build)\s*$", body, flags=re.MULTILINE)
            if s:
                idea, start = "", s.group(1)
                print(f"The issue asks for feature {feature} from {start}.")
            else:
                print(f"The issue names feature {feature}: revising it.")
    merged = env("SDLC_MERGED_PHASE")
    if merged and not (feature or idea):
        m = PHASE_BRANCH.fullmatch(merged) or fail(f"{merged} isn't a phase/<feature>/<phase> branch.")
        feature, start = m.group("feature"), "build"
        branch = branch_for(feature)
        use_feature_branch(branch)
        folder = Path(features_dir, feature)
        planned = [p["id"] for p in parse_phases(approved_text(approved_ref(feature), folder / "plan.md"))]
        todo = [i for i in planned if i not in logged_phases(folder)]
        if todo:
            print(f"{merged} is merged. Still to build (not in build-log.md): phase {', '.join(todo)}.")
            step_summary(f"**Feature** `{feature}`: `{merged}` is merged. The build run starts when build-log.md "
                         f"logs every phase; still to build: phase {', '.join(todo)}.")
            set_output(feature=feature, branch=branch, start="build", base=env("BASE_REF"), mode="none",
                       design="false", **{s: "false" for s in STAGES})
            return
        print(f"{merged} is merged and build-log.md logs every phase: handing back for the build run.")
    if start not in ("auto", "intent", "spec", "plan", "build"):
        fail(f"Unknown start stage: {start}")
    if idea and start not in ("auto", "intent"):
        fail("An idea starts at the intent stage. Leave start on auto or intent.")
    if start == "intent" and not idea:
        fail("The intent stage needs an idea.")
    if not feature:
        if not idea:
            fail("Give an idea to start a new feature, or name an existing feature folder.")
        feature = new_feature_name(features_dir, idea)
    if not FEATURE_NAME.fullmatch(feature):
        fail(f"Bad feature name '{feature}': use lowercase letters, digits and hyphens.")
    branch = branch_for(feature)
    use_feature_branch(branch)
    folder = Path(features_dir, feature)
    have = {s: (folder / f"{s}.md").exists() for s in STAGES}
    if start == "auto":
        start = "intent" if idea else next((s for s in STAGES if not have[s]), "build")
    if start != "intent" and not have["intent"]:
        fail(f"{folder}/intent.md doesn't exist on {branch} or the base. Start with an idea.")
    if start == "plan" and not have["spec"]:
        fail(f"{folder}/spec.md doesn't exist yet. Start at spec.")
    if start == "build" and not have["plan"]:
        fail(f"{folder}/plan.md doesn't exist yet. Start at plan.")
    order = ("intent", "spec", "plan", "build")
    runs = {s: order.index(s) >= order.index(start) for s in STAGES}
    if start == "intent" and have["intent"]:
        later = [f"{s}.md" for s in ("spec", "plan") if have[s]]
        print(f"::warning::{feature} already has an intent.md. This run revises it"
              + (f" and then regenerates {', '.join(later)}." if later else "."))
    print(f"Feature {feature} on {branch}, starting at {start}.")
    step_summary(f"**Feature** `{feature}` · **branch** [`{branch}`]({repo_url('tree', branch)}) · "
                 f"**starts at** {start}")
    # design: this run writes documents and ends by handing the plan to the builder.
    # Otherwise it is the build run: verify, review and open the pull request.
    set_output(feature=feature, branch=branch, start=start, base=env("BASE_REF"),
               mode="design" if start != "build" else "build", design=str(start != "build").lower(),
               **{s: str(runs[s]).lower() for s in STAGES})


# ---------------------------------------------------------------- stage

def header(stage, model, sources):
    return (f"<!-- sdlc stage={stage} model={model} "
            f"from={','.join(sources)}@{git('rev-parse', '--short', 'HEAD')} -->\n")


def prompt_parts(stage, folder, files, budget, idea):
    parts = [(ACTION_DIR / "prompts" / f"{stage}.md").read_text(encoding="utf-8")]
    sources = []
    if stage == "intent":
        parts += [f"## Owner\n@{env('ACTOR', 'unknown')}", "## The idea\n" + idea]
        sources.append("idea")
        if (folder / "intent.md").exists():
            parts.append("## Existing intent.md (revise this; keep what the idea does not change)\n"
                         + (folder / "intent.md").read_text(encoding="utf-8"))
            sources.append("intent.md")
    else:
        for name in ("intent", "spec")[: STAGES.index(stage)]:
            parts.append(f"## {name}.md\n" + (folder / f"{name}.md").read_text(encoding="utf-8"))
            sources.append(f"{name}.md")
    named = mentioned_files("\n".join(parts[1:]), files)
    if stage == "plan":
        parts.append(f"## Feature\nFolder: `{folder.as_posix()}`. Branch: `{branch_for(folder.name)}`. "
                     f"Approved tag: `{approved_tag(folder.name)}`, created when this plan is approved; it marks the "
                     "approved documents and the code before the build.")
    if stage == "plan" and env("TEST_COMMAND"):
        parts.append("## How verification runs\nThe verify job installs the project into a virtualenv that is on "
                     "PATH (from pyproject.toml if there is one, otherwise requirements.txt, plus pytest). It "
                     "runs each phase's Verify block from the repository root, then the whole suite with "
                     f"`{env('TEST_COMMAND')}`, which fails if it collects no tests.")
    parts.append("## Repository files\n" + repo_tree(files))
    if Path("README.md").exists():
        parts.append("## README.md (start)\n" + Path("README.md").read_text(encoding="utf-8")[:4000])
    used = sum(map(len, parts))
    for path in named:
        if path.startswith(str(folder)) or budget - used < 1000:
            continue
        block = file_block(path, budget - used)
        parts.append(block)
        used += len(block)
    return parts, sources


def stage(args):
    name = args.name
    if name not in STAGES:
        fail(f"Unknown stage {name}")
    feature = env("SDLC_FEATURE") or fail("SDLC_FEATURE is not set")
    features_dir = env("FEATURES_DIR", "sdlc/features").strip("/")
    branch = branch_for(feature)
    use_feature_branch(branch)
    folder = Path(features_dir, feature)
    files = tracked_files()
    model = env("OLLAMA_MODEL", "deepseek-v4-flash:cloud")
    parts, sources = prompt_parts(name, folder, files, int(env("MAX_CONTEXT_CHARS", "60000")), env("SDLC_IDEA"))
    print(f"Writing {name}.md for {feature}", flush=True)
    text = unfence(chat(model, "\n\n".join(parts)))
    problems = []
    if name == "plan":
        intent = (folder / "intent.md").read_text(encoding="utf-8")
        problems = plan_problems(text, intent)
        if problems:
            print("::warning::The plan is incomplete; asking once more: " + " ".join(problems), flush=True)
            retry = ("\n\n## Your previous plan was rejected\n" + "\n".join(f"- {p}" for p in problems)
                     + "\n\n## Your previous plan\n" + text + "\n\nWrite the whole plan again, fixed.")
            text = unfence(chat(model, "\n\n".join(parts) + retry))
            problems = plan_problems(text, intent)
        text = with_hand_back(text, folder)

    path = folder / f"{name}.md"
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(header(name, model, sources) + text + "\n", encoding="utf-8")
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898283+github-actions[bot]@users.noreply.github.com")
    git("add", str(path))
    if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        print(f"{path} is unchanged.")
    else:
        git("commit", "-q", "-m", f"sdlc({feature}): {name}\n\nWritten by the {name} stage with {model}.")
        git("push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    view, edit = repo_url("blob", branch, str(path)), repo_url("edit", branch, str(path))
    summary = [f"## {name}.md is ready for review", "",
               f"[View]({view}) · [Edit on the branch]({edit}) · model `{model}`", "",
               "Read it, edit it on the branch if it needs changing, then approve the next "
               "**Review** job (Review deployments). Rejecting stops the run. Where the review "
               "environment has no required reviewers, the run carries on by itself.", ""]
    if problems:
        summary += ["> [!WARNING]", "> The plan still has problems. Fix them on the branch before approving:"]
        summary += [f"> - {p}" for p in problems] + [""]
        for p in problems:
            print(f"::warning::{p}")
    step_summary("\n".join(summary) + "\n---\n\n" + path.read_text(encoding="utf-8"))
    set_output(path=str(path))


# ---------------------------------------------------------------- verify

def changed_since(base, include_worktree):
    if include_worktree:
        tracked = git("diff", "--name-only", "HEAD").split()
        untracked = git("ls-files", "--others", "--exclude-standard").split()
        return sorted(set(tracked) | set(untracked))
    return sorted(set(git("diff", "--name-only", f"{base}...HEAD").split()))


def run_check(command, timeout=900):
    try:
        r = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", command], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, (r.stdout + r.stderr)[-4000:]
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def verify(args):
    features_dir = env("FEATURES_DIR", "sdlc/features").strip("/")
    feature = args.feature or env("SDLC_FEATURE") or fail("Name the feature (--feature).")
    folder = Path(features_dir, feature)
    plan_path = folder / "plan.md"
    # The plan the builder is checked against is the approved one, not whatever
    # plan.md says on the branch the builder pushes to.
    tag = approved_ref(feature)
    plan = approved_text(tag, plan_path)
    if not plan:
        fail(f"{plan_path} doesn't exist" + (f" at {tag}" if tag else ""))
    phases = parse_phases(plan)
    ids = [p["id"] for p in phases]
    if args.phase and args.phase not in ids:
        fail(f"plan.md has no phase {args.phase}")
    # A phase is scoped to its own targets and frozen files and re-runs the Verify
    # blocks of every phase up to it; the whole feature uses every phase.
    upto = phases[: ids.index(args.phase) + 1] if args.phase else phases
    scoped = upto[-1:] if args.phase else phases
    base = args.base or env("BASE_REF")
    # One phase is checked against uncommitted work, like phase_check.py, or with a
    # base against what its phase branch changed; the whole feature against
    # everything the branch changed since the base.
    if args.phase and not base:
        changed, against = changed_since(None, True), "uncommitted changes"
    else:
        if not base:
            fail("Give --base (the branch the feature goes into).")
        git("fetch", "-q", "origin", base, check=False)
        ref = f"origin/{base}" if git("rev-parse", "--verify", "-q", f"origin/{base}", check=False) else base
        changed, against = changed_since(ref, False), f"changes since `{base}`"
    changed = [p for p in changed if not p.startswith(str(folder) + "/")]
    targets = [t for p in scoped for t in p["targets"]]
    if args.phase:
        frozen = [f for p in scoped for f in p["frozen"]]
    else:
        # In whole-feature verification, files targeted by any phase are intentional changes.
        # Only files outside all phase targets can be considered frozen violations.
        frozen = [f for p in phases for f in p["frozen"] if not matches_any(f, targets)]
    frozen_hit = [p for p in changed if matches_any(p, frozen) and not matches_any(p, targets)]
    outside = [p for p in changed if not matches_any(p, targets) and p not in frozen_hit]
    tampered = contract_changes(tag, folder) if tag else []

    rows, ok = [], True
    report = [f"# Verification: {feature}" + (f", phase {args.phase}" if args.phase else ""), ""]
    if not changed:
        report.append(f"> [!WARNING]\n> No code changed ({against}). Nothing has been built yet.\n")
        ok = False
    if not tag:
        contract = f"no `{approved_tag(feature)}` tag, so plan.md was read from the branch unchecked."
    elif not tampered:
        contract = f"intent.md, spec.md and plan.md match `{tag}`."
    else:
        contract = ("**changed after approval:** " + ", ".join(f"`{p}`" for p in tampered)
                    + ". Run the design stages again instead of editing them.")
    report += [f"**Scope** ({against}): {len(changed)} file(s) changed. "
               + ("All inside the plan's targets." if not outside else f"**{len(outside)} outside the plan:** "
                  + ", ".join(f"`{p}`" for p in outside)),
               f"**Frozen files:** " + ("none touched." if not frozen_hit else "**modified:** "
                                         + ", ".join(f"`{p}`" for p in frozen_hit)),
               f"**Contract:** {contract}", ""]
    ok = ok and not outside and not frozen_hit and not tampered
    test_command = args.test_command or env("TEST_COMMAND")
    if test_command:
        code, out = run_check(test_command)
        rows.append(("Whole test suite", test_command, code, out))
    for p in upto:
        if p["verify"]:
            code, out = run_check(p["verify"])
            rows.append((f"Phase {p['id']}: {p['title'].split(':', 1)[-1].strip()}", p["verify"], code, out))
        else:
            rows.append((f"Phase {p['id']}", "(no Verify block)", 1, "plan.md gives this phase no Verify command"))
    report += ["| Check | Result |", "| :-- | :-- |"]
    for name, _, code, _ in rows:
        report.append(f"| {name} | {'✅ passed' if code == 0 else f'❌ exit {code}'} |")
        ok = ok and code == 0
    report.append("")
    for name, command, code, out in rows:
        report += [f"<details><summary>{name}: {'passed' if code == 0 else 'failed'}</summary>", "",
                   "```bash", command, "```", "```", out.strip(), "```", "</details>", ""]
    report.insert(1, f"**Result: {'PASSED' if ok else 'FAILED'}**\n")
    text = "\n".join(report)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
    step_summary(text)
    print(text)
    set_output(passed=str(ok).lower())
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- freeze

LOCAL_CHECK = "https://raw.githubusercontent.com/ly2xxx/.github/main/actions/sdlc-stage/sdlc_stage.py"


def handoff(feature, branch, tag, sha, phases, builder):
    """The step summary that pauses the pipeline for the builder."""
    test_command = env("TEST_COMMAND", "python -m pytest -q")
    who = "Claude Code" if builder == "claude" else "a person"
    lines = [f"## ⏸ Step 4: waiting for the build (builder: {who})", "",
             f"The design is frozen: `{tag}` marks intent.md, spec.md and plan.md at `{sha}` on "
             f"[`{branch}`]({repo_url('tree', branch)}). Verification fails if any of them change, so the "
             "builder builds the plan but can't rewrite it. Changing it means running the design stages again.", "",
             "**Phases to build:**"]
    lines += [f"{i}. **Phase {p['id']}**: {p['title'].split(':', 1)[-1].strip()} "
              f"({', '.join(f'`{t}`' for t in p['targets'])})" for i, p in enumerate(phases, 1)]
    lines.append("")
    log = Path(env("FEATURES_DIR", "sdlc/features").strip("/"), feature, "build-log.md").as_posix()
    lines += ["**Build it**, one phase at a time, changing only that phase's targets:", "", "```bash",
              f"git fetch origin && git switch -c phase/{feature}/<n> origin/{branch}",
              f"curl -sSfLo /tmp/sdlc_stage.py {LOCAL_CHECK}",
              f'python /tmp/sdlc_stage.py verify --feature {feature} --phase <n> --test-command "{test_command}"',
              "```", "",
              f"Add a `## Phase <n>: <title>` section to `{log}` for each phase. Then open a pull request "
              f"into `{branch}`, where the phase check runs, and merge it; or push straight to `{branch}`.", ""]
    minutes = int(env("BUILD_WAIT_MINUTES", "0") or 0)
    if minutes > 0:
        lines += [f"**Hand back.** This job now waits, for up to {minutes} minutes, until `build-log.md` on "
                  f"`{branch}` has a `## Phase <id>: ...` section for every phase. Then this same run carries "
                  "on: step 5 verifies the build and Ollama reviews it, and step 6 opens the pull request. "
                  f"If it stops waiting first, run **SDLC Pipeline** with feature `{feature}` and start "
                  "`build` once the build is done."]
    else:
        lines += [f"**Hand back.** When every phase is in `{branch}`, run **SDLC Pipeline** with feature "
                  f"`{feature}` (start `auto` or `build`). That run verifies the build, has Ollama review it, "
                  "and opens the pull request."]
    return "\n".join(lines)


def freeze(args):
    features_dir = env("FEATURES_DIR", "sdlc/features").strip("/")
    feature = env("SDLC_FEATURE") or fail("SDLC_FEATURE is not set")
    builder = env("SDLC_BUILDER", "claude").lower()
    if builder not in ("claude", "human"):
        fail(f"builder must be claude or human, not {builder}")
    branch = branch_for(feature)
    use_feature_branch(branch)
    folder = Path(features_dir, feature)
    missing = [f"{s}.md" for s in STAGES if not (folder / f"{s}.md").exists()]
    if missing:
        fail(f"{folder} has no {', '.join(missing)}, so there is nothing to build yet.")
    plan = (folder / "plan.md").read_text(encoding="utf-8")
    problems = plan_problems(plan, (folder / "intent.md").read_text(encoding="utf-8"))
    if problems:
        step_summary("## plan.md can't be built yet\n\n" + "\n".join(f"- {p}" for p in problems))
        fail("plan.md can't be built yet: " + " ".join(problems)
             + " Fix it on the branch and re-run this job, or run the plan stage again.")
    tag, sha = approved_tag(feature), git("rev-parse", "--short", "HEAD")
    git("config", "user.name", "github-actions[bot]")
    git("config", "user.email", "41898283+github-actions[bot]@users.noreply.github.com")
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
    features_dir = env("FEATURES_DIR", "sdlc/features").strip("/")
    feature = env("SDLC_FEATURE") or fail("SDLC_FEATURE is not set")
    minutes = int(env("BUILD_WAIT_MINUTES", "0") or 0)
    if minutes <= 0:
        print("Not waiting for the build (build-wait-minutes is 0): this run ends at the hand-off.")
        set_output(built="false")
        return
    branch, folder = branch_for(feature), Path(features_dir, feature)
    tag = approved_ref(feature) or fail(f"{approved_tag(feature)} doesn't exist: freeze the design first.")
    planned = [p["id"] for p in parse_phases(approved_text(tag, folder / "plan.md"))]
    log_path = (folder / "build-log.md").as_posix()
    interval, deadline, last = int(env("BUILD_POLL_SECONDS", "30")), time.monotonic() + minutes * 60, None
    print(f"Waiting up to {minutes} minutes for {branch} to log phase(s) {', '.join(planned)} in build-log.md.",
          flush=True)
    while True:
        git("fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", check=False)
        log = blob(f"origin/{branch}", log_path)
        todo = [i for i in planned if i not in logged_ids(log.decode("utf-8") if log else "")]
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
    features_dir = env("FEATURES_DIR", "sdlc/features").strip("/")
    feature = env("SDLC_FEATURE") or fail("SDLC_FEATURE is not set")
    base = env("BASE_REF") or fail("BASE_REF is not set")
    folder = Path(features_dir, feature)
    tag = approved_ref(feature)
    git("fetch", "-q", "origin", base, check=False)
    ref = f"origin/{base}" if git("rev-parse", "--verify", "-q", f"origin/{base}", check=False) else base
    skip = [f":(exclude){(folder / f'{s}.md').as_posix()}" for s in STAGES]
    stat = git("diff", "--stat", f"{ref}...HEAD", "--", ".", *skip)
    diff = git("diff", "--no-color", f"{ref}...HEAD", "--", ".", *skip)
    if not diff:
        fail(f"Nothing changed since {base}, so there is nothing to review.")
    budget = int(env("MAX_CONTEXT_CHARS", "60000"))
    note = ""
    if len(diff) > budget:
        diff, note = diff[:budget], f"\n\n(The diff is truncated at {budget} characters.)"
    parts = [(ACTION_DIR / "prompts" / "review.md").read_text(encoding="utf-8")]
    parts += [f"## {s}.md (approved)\n" + approved_text(tag, folder / f"{s}.md") for s in STAGES]
    parts += [f"## Diffstat\n```\n{stat}\n```", f"## Diff\n```diff\n{diff}\n```{note}"]
    model = env("OLLAMA_MODEL", "deepseek-v4-flash:cloud")
    print(f"Reviewing {feature} with {model}", flush=True)
    text = unfence(chat(model, "\n\n".join(parts)))
    out = "\n".join([f"<!-- sdlc stage=review model={model} -->", "## Ollama review (advisory)", "",
                     f"`{model}` compared the diff with the approved spec and plan. Its review informs the "
                     "merge but doesn't gate it; the verification above does.", "", text, ""])
    if args.report:
        Path(args.report).write_text(out, encoding="utf-8")
    print(out)
    step_summary(out)
    set_output(path=args.report or "")


# ---------------------------------------------------------------- pr

def pr(args):
    features_dir = env("FEATURES_DIR", "sdlc/features").strip("/")
    feature = env("SDLC_FEATURE") or fail("SDLC_FEATURE is not set")
    base = env("BASE_REF") or fail("BASE_REF is not set")
    branch = branch_for(feature)
    folder = Path(features_dir, feature)
    passed = env("VERIFY_PASSED") == "true"
    intent = (folder / "intent.md").read_text(encoding="utf-8")
    title_m = re.search(r"^# Intent:\s*(.+)$", intent, flags=re.MULTILINE)
    title = f"{feature}: {title_m.group(1).strip() if title_m else 'feature'}"
    tag = approved_ref(feature)
    phases = parse_phases(approved_text(tag, folder / "plan.md"))
    report_path = Path(args.report) if args.report else None
    report = report_path.read_text(encoding="utf-8") if report_path and report_path.exists() else \
        "_The verification report is missing._"
    review_path = Path(args.review) if args.review else None
    review_text = review_path.read_text(encoding="utf-8") if review_path and review_path.exists() else \
        ("_The Ollama review didn't run._" if args.review else "")
    docs = [n + ".md" for n in STAGES] + (["build-log.md"] if (folder / "build-log.md").exists() else [])
    links = " · ".join(f"[{d}]({repo_url('blob', branch, str(folder / d))})" for d in docs)
    frozen = f", frozen at [`{tag}`]({repo_url('tree', tag)})" if tag else ""
    body = [f"Built from {links}{frozen}, each approved in the SDLC Pipeline run.", "",
            ("Verification **passed**. Review the code, then merge." if passed else
             "Verification **failed**, so this is a draft. Push fixes to "
             f"`{branch}` and re-run the failed jobs."), "", "## Phases"]
    for p in phases:
        body.append(f"- [{'x' if passed else ' '}] **Phase {p['id']}**: {p['title'].split(':', 1)[-1].strip()}")
        body += [f"  - {d}" for d in p["dod"]]
    body += ["", report]
    if review_text:
        body += ["", review_text]
    body_text = "\n".join(body)[:60000]
    existing = sh("gh", "pr", "list", "--head", branch, "--state", "open", "--json", "url", "--jq", ".[0].url")
    if existing:
        sh("gh", "pr", "edit", existing, "--title", title, "--body", body_text)
        if passed:
            sh("gh", "pr", "ready", existing, check=False)
        url = existing
    else:
        cmd = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body_text]
        r = subprocess.run([*cmd, *([] if passed else ["--draft"])], capture_output=True, text=True)
        if r.returncode:
            hint = ""
            if "not permitted" in r.stderr:
                hint = (" Allow it under Settings → Actions → General → Workflow permissions (\"Allow GitHub Actions "
                        "to create and approve pull requests\"), or add an SDLC_PR_TOKEN secret. Until then the "
                        "builder can open it: the title and body are in this job's log and summary.")
                # The pull request this run would have opened, so a builder that can open
                # pull requests but not change the setting can open exactly this one.
                print(f"----- pull request: {branch} -> {base} -----\n{title}\n----- body -----\n{body_text}\n"
                      "----- end of pull request -----", flush=True)
                step_summary(f"## The pull request to open\n\n`{branch}` → `{base}`: **{title}**\n\n---\n\n{body_text}")
            fail(f"gh pr create exited {r.returncode}: {r.stderr.strip()[-1000:]}{hint}")
        url = r.stdout.strip()
    print(f"Pull request: {url}")
    step_summary(f"Pull request: {url}")
    set_output(pr=url)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("resolve")
    s = sub.add_parser("stage")
    s.add_argument("name", choices=STAGES)
    sub.add_parser("freeze")
    sub.add_parser("wait")
    v = sub.add_parser("verify")
    v.add_argument("--feature")
    v.add_argument("--phase", help="check one phase: uncommitted changes, or with --base its phase branch")
    v.add_argument("--base", help="branch the feature (or, with --phase, the phase) goes into")
    v.add_argument("--test-command")
    v.add_argument("--report", help="also write the report to this file")
    r = sub.add_parser("review")
    r.add_argument("--report", help="also write the review to this file")
    p = sub.add_parser("pr")
    p.add_argument("--report")
    p.add_argument("--review")
    args = ap.parse_args()
    {"resolve": resolve, "stage": stage, "freeze": freeze, "wait": wait, "verify": verify,
     "review": review, "pr": pr}[args.cmd](args)


if __name__ == "__main__":
    main()
