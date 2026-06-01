"""Implement handler — two-pass.

Pass 1: tell the model the task + workspace tree, get back which files to
read and which to write, plus verification commands.

Pass 2: send the requested file contents back, get a unified diff.

Apply the diff, run verification commands, submit the result.
"""

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from orchestrai_agent.config import config
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.prompt_context import (
    documents_index_block, requested_documents_block, secrets_block,
)
from orchestrai_agent.prompt_metrics import emit as emit_prompt_metrics
from orchestrai_agent.response_parser import extract_json
from orchestrai_agent.subprocess_util import run as run_subproc
from orchestrai_agent.file_outline import maybe_outline
from orchestrai_agent.workspace import (
    apply_diff, commit_all, ensure_workspace, prepare_workspace, list_tree_relevant, read_files, write_files,
)

log = logging.getLogger("orchestrai-agent.implement")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PASS1_TPL = (PROMPTS_DIR / "implementer_pass1.md").read_text(encoding="utf-8")
_PASS2_TPL = (PROMPTS_DIR / "implementer_pass2.md").read_text(encoding="utf-8")
_FIX_TPL = (PROMPTS_DIR / "implementer_fix.md").read_text(encoding="utf-8")
_DIFF_RECOVER_TPL = (PROMPTS_DIR / "implementer_diff_recover.md").read_text(encoding="utf-8")

_DIFF_GIT_RE = re.compile(r"^diff --git a/\S+ b/(\S+)", re.M)
_DIFF_PATH_RE = re.compile(r"^(?:\+\+\+|---) [ab]/(.+?)\s*$", re.M)


def _diff_target_paths(diff: str, fallback: list[str]) -> list[str]:
    """Best-effort: the file paths a (possibly corrupt) diff targets. Even diffs
    that fail to apply usually have parseable +++/diff --git headers; fall back
    to pass1's declared paths if none are found."""
    found: list[str] = [m.group(1) for m in _DIFF_GIT_RE.finditer(diff)]
    for m in _DIFF_PATH_RE.finditer(diff):
        p = m.group(1).strip()
        if p and p != "/dev/null":
            found.append(p)
    seen: set[str] = set()
    out: list[str] = []
    for p in found + list(fallback):
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _indent(text: str, prefix: str = "    ") -> str:
    text = text or ""
    return "\n".join(prefix + line for line in text.splitlines()) or (prefix + "(empty)")


def _format_criteria(criteria: list) -> str:
    if not criteria:
        return "    (none)"
    out = []
    for c in criteria:
        if isinstance(c, str):
            out.append(f"  - {c}")
        else:
            out.append(f"  - {c}")
    return "\n".join(out)


# Retry context is the single biggest unbounded source of context bloat. Old
# code emitted ~22 KB on multi-attempt failures (5 stamped notes × ~500 chars,
# 2 commands × ~3 KB outputs, all prior files × ~2.5 KB tails, ~1.5 KB rules
# prose). Now hard-capped so the model still gets the *useful* information:
# the last failing command's actual error message, a one-line summary of each
# prior attempt, and the on-disk contents of files the last attempt touched.
_RETRY_BUDGET_TOTAL = 4000          # whole retry block (rules + outputs + files)
_RETRY_LAST_CMD_CAP = 1400          # stderr+stdout of the SINGLE last failure
_RETRY_PRIOR_SUMMARY_LINES = 3      # how many stamped notes to enumerate
_RETRY_PRIOR_SUMMARY_LINE_CAP = 200 # truncate each stamped note
_RETRY_PRIOR_FILE_CAP = 1200        # per-file tail when prior_files supplied

_RETRY_RULES = (
    "RULES FOR THIS RETRY:\n"
    "- Do NOT repeat the previous approach. The error above tells you what to fix.\n"
    "- AssertionError actual==expected: if spec doesn't pin the value, fix the\n"
    "  TEST (use a property check); if spec does, fix the implementation.\n"
    "- ModuleNotFoundError: the package is missing from the project tool list —\n"
    "  ask via `questions[]`, do NOT pip-install inline.\n"
    "- 'corrupt patch' / 'does not apply': use `files[]` with full content, not `diff`.\n"
    "- exit 127 (command not found): pick a different verification approach.\n"
    "- Stuck? Surface a `questions[]` entry instead of resubmitting the same broken output."
)


def _last_failing_command(task: dict) -> Optional[str]:
    """Return a single rendered block for the LAST failing command from the
    previous attempt — that's where the real signal is (an AssertionError,
    a ModuleNotFoundError, etc.). Earlier failed commands are skipped."""
    result = task.get("result") or {}
    last_fail: Optional[dict] = None
    for c in (result.get("commands_run") or []):
        if not isinstance(c, dict):
            continue
        if int(c.get("exit", 0)) == 0 and not c.get("timed_out"):
            continue
        last_fail = c
    if not last_fail:
        return None
    cmd = last_fail.get("cmd", "(unknown)")
    err = (last_fail.get("stderr") or "").strip()
    out = (last_fail.get("stdout") or "").strip()
    exit_code = last_fail.get("exit", "?")
    timed_out = last_fail.get("timed_out", False)
    head = f"$ {cmd}\n[exit={exit_code}{', TIMEOUT' if timed_out else ''}]"
    # Prefer stderr — that's where Python tracebacks and pytest failures go.
    body_parts: list[str] = []
    remaining = _RETRY_LAST_CMD_CAP
    if err:
        snippet = err[-min(remaining, len(err)):]
        body_parts.append(f"--- stderr ---\n{snippet}")
        remaining -= len(snippet)
    if out and remaining > 200:
        snippet = out[-min(remaining, len(out)):]
        body_parts.append(f"--- stdout ---\n{snippet}")
    return head + ("\n" + "\n".join(body_parts) if body_parts else "")


def _prior_summary(task: dict) -> str:
    """Last N stamped notes, each truncated, as a compact summary."""
    notes = (task.get("notes") or "").strip()
    lines = [ln for ln in notes.splitlines() if ln.startswith("[")]
    if not lines:
        return "(no specific failure reasons recorded)"
    out: list[str] = []
    for ln in lines[-_RETRY_PRIOR_SUMMARY_LINES:]:
        out.append(ln if len(ln) <= _RETRY_PRIOR_SUMMARY_LINE_CAP
                   else ln[:_RETRY_PRIOR_SUMMARY_LINE_CAP] + "…")
    return "\n".join(out)


def _build_retry_block(task: dict, prior_files: Optional[dict] = None) -> str:
    """Return the entire retry-context block bounded by `_RETRY_BUDGET_TOTAL`.

    Combines: header + last-failing-command output + prior-attempt summaries
    + on-disk content of files the last attempt modified. Empty string on
    first attempt.
    """
    # The claim SQL bumps attempt_count to 1 BEFORE the handler reads it, so
    # attempts==1 means "this is the first try". Retry context is only useful
    # from the second try onwards.
    attempts = int(task.get("attempt_count") or 0)
    if attempts <= 1:
        return ""

    last_cmd = _last_failing_command(task)
    summary = _prior_summary(task)
    parts = [
        f"▲ RETRY (attempt {attempts}) — fix the LAST FAILURE below, do not "
        f"redo the whole task ▲"
    ]
    if last_cmd:
        parts.append("LAST FAILING COMMAND (read the error here):\n```\n"
                     + last_cmd + "\n```")
    parts.append("PRIOR ATTEMPT SUMMARIES:\n" + summary)
    parts.append(_RETRY_RULES)
    block = "\n\n".join(parts) + "\n\n"

    if prior_files:
        # Whatever budget is left after the rules+errors goes to file contents.
        remaining = max(400, _RETRY_BUDGET_TOTAL - len(block))
        per_file = min(_RETRY_PRIOR_FILE_CAP,
                       max(200, remaining // max(1, len(prior_files))))
        snippets: list[str] = []
        used = 0
        for path, content in prior_files.items():
            if used >= remaining:
                snippets.append(f"--- {path} (omitted, retry budget exhausted) ---")
                continue
            tail = content[-per_file:] if content else "(empty)"
            snippets.append(f"--- {path} (current on-disk content) ---\n{tail}")
            used += len(tail) + len(path) + 40
        block += (
            "CURRENT CONTENT OF FILES THE LAST ATTEMPT MODIFIED (edit these, "
            "do not rewrite from scratch):\n```\n"
            + "\n\n".join(snippets) + "\n```\n\n"
        )

    # Final hard cap — extremely defensive; the bookkeeping above keeps us
    # under budget but if anything slips through, truncate the head (we'd
    # rather lose the rules than the last error message).
    if len(block) > _RETRY_BUDGET_TOTAL:
        overflow = len(block) - _RETRY_BUDGET_TOTAL
        block = "[…retry context truncated…]\n" + block[overflow + 32:]
    return block


def _kind_hint(task: dict) -> str:
    payload = task.get("payload") or {}
    return (payload.get("kind_hint") or "other").lower()


def _http_ports_block(task: dict) -> str:
    """Web-task only. Returns "" for non-web tasks so the prompt stays tight."""
    if _kind_hint(task) != "web" or not config.HTTP_PORTS:
        return ""
    ports = ",".join(str(p) for p in config.HTTP_PORTS)
    first = config.HTTP_PORTS[0]
    return (
        f"HOST PORTS: {ports} (bind 0.0.0.0:<port>, NOT 127.0.0.1). "
        f"Use `orchestrai-serve --port {first} -- <cmd>` to background a "
        f"server + exit 0 once reachable. Users hit http://localhost:<port>."
    )


_TEST_BLOCK = (
    "WRITING TESTS: include `from <module> import <name>` (NameError otherwise). "
    "Prefer property assertions (`assert f(0) == 100`, `isinstance(x, int)`) "
    "over guessed numerics (`assert f(0.3) == 27`)."
)


def _test_block(task: dict) -> str:
    """Only ship test-writing guidance to tasks that write tests."""
    return _TEST_BLOCK if _kind_hint(task) == "test" else ""


# Packages that ship in the base agent image. Keep in sync with Dockerfile.agent.
_PREINSTALLED_PY = [
    "httpx", "pydantic", "pytest", "pytest-asyncio", "ruff", "black", "mypy",
    "requests", "sqlalchemy", "alembic",
    "fastapi", "uvicorn[standard]", "jinja2", "python-multipart",
]


def _tools_block(project: dict) -> str:
    declared = ((project.get("tools") or {}).get("python_packages") or [])
    extra = [p for p in declared if p not in _PREINSTALLED_PY]
    avail = _PREINSTALLED_PY + extra
    line = "PY PACKAGES AVAILABLE (already installed, import directly): " + ", ".join(avail) + "."
    line += (" For any package NOT listed, STOP and ask via `questions[]` — "
             "do NOT pip-install inline. Prefer FastAPI over Flask/Django.")
    return line


def _render_pass1(project: dict, task: dict, workspace_tree: str,
                  prior_files: dict | None = None, documents: list | None = None,
                  secret_names: list | None = None) -> tuple[str, dict]:
    # Retry context is now bounded and folded together with prior_files
    # inside _build_retry_block (hard cap _RETRY_BUDGET_TOTAL).
    rs = _build_retry_block(task, prior_files=prior_files)
    project_description = project.get("description_md") or "(no description)"
    project_context = _indent(project.get("context_md") or "(no context)")
    docs_block = documents_index_block(documents)
    secrets_blk = secrets_block(secret_names)
    task_description = task.get("description_md") or "(no description)"
    criteria = _format_criteria(task.get("acceptance_criteria") or [])
    notes = _indent(task.get("notes") or "(none)")
    http_block = _http_ports_block(task)
    tools_block = _tools_block(project)
    test_block = _test_block(task)
    prompt = _PASS1_TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_description=project_description,
        project_context_indented=project_context,
        project_documents_block=docs_block,
        available_secrets_block=secrets_blk,
        task_title=task.get("title", "(no title)"),
        task_description=task_description,
        repo_name="(no specific repo)",
        branch_name=task.get("branch_name") or "(no branch)",
        acceptance_criteria_indented=criteria,
        notes_indented=notes,
        retry_section=rs,
        http_ports_block=http_block,
        tools_block=tools_block,
        test_block=test_block,
        workspace_tree=workspace_tree,
    )
    sections = {
        "project_description": len(project_description),
        "project_context": len(project_context),
        "project_documents_block": len(docs_block),
        "available_secrets_block": len(secrets_blk),
        "task_description": len(task_description),
        "acceptance_criteria": len(criteria),
        "task_notes": len(notes),
        "retry_section": len(rs),
        "http_ports_block": len(http_block),
        "tools_block": len(tools_block),
        "test_block": len(test_block),
        "workspace_tree": len(workspace_tree),
        "static_template": len(_PASS1_TPL),
    }
    return prompt, sections


def _render_pass2(project: dict, task: dict, pass1: dict, files_contents: dict,
                  documents: list | None = None,
                  secret_names: list | None = None,
                  requested_docs: dict | None = None) -> tuple[str, dict]:
    files_summary = ", ".join(
        f"{f['path']} ({f.get('intent','')})"
        for f in (pass1.get("files_to_write_or_modify") or [])
    ) or "(none)"
    if files_contents:
        rendered = []
        for path, content in files_contents.items():
            rendered.append(f"--- {path} ---\n{content}")
        files_block = "\n\n".join(rendered)
    else:
        files_block = "(no files to read — fresh workspace or no reads requested)"
    project_description = project.get("description_md") or "(no description)"
    project_context = _indent(project.get("context_md") or "(no context)")
    docs_block = documents_index_block(documents)
    requested_docs_block = requested_documents_block(requested_docs)
    secrets_blk = secrets_block(secret_names)
    task_description = task.get("description_md") or "(no description)"
    criteria = _format_criteria(task.get("acceptance_criteria") or [])
    retry = _build_retry_block(task)
    http_block = _http_ports_block(task)
    tools_block = _tools_block(project)
    test_block = _test_block(task)
    diff_plan = pass1.get("diff_plan_md") or "(no plan provided)"
    prompt = _PASS2_TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_description=project_description,
        project_context_indented=project_context,
        project_documents_block=docs_block,
        requested_documents_block=requested_docs_block,
        available_secrets_block=secrets_blk,
        task_title=task.get("title", "(no title)"),
        task_description=task_description,
        repo_name="(no specific repo)",
        branch_name=task.get("branch_name") or "(no branch)",
        acceptance_criteria_indented=criteria,
        retry_section=retry,
        http_ports_block=http_block,
        tools_block=tools_block,
        test_block=test_block,
        files_to_write_summary=files_summary,
        diff_plan_md=diff_plan,
        files_contents=files_block,
    )
    sections = {
        "project_description": len(project_description),
        "project_context": len(project_context),
        "project_documents_block": len(docs_block),
        "requested_documents_block": len(requested_docs_block),
        "available_secrets_block": len(secrets_blk),
        "task_description": len(task_description),
        "acceptance_criteria": len(criteria),
        "retry_section": len(retry),
        "http_ports_block": len(http_block),
        "tools_block": len(tools_block),
        "test_block": len(test_block),
        "files_to_write_summary": len(files_summary),
        "diff_plan": len(diff_plan),
        "files_contents": len(files_block),
        "static_template": len(_PASS2_TPL),
    }
    return prompt, sections


_MAX_REQUESTED_DOCS = 6
_REQUESTED_DOC_CHARS = 8000


async def _fetch_requested_documents(hub, task_id: str, doc_index: list | None,
                                     requested: list | None, workspace=None) -> dict:
    """Resolve pass-1's `documents_to_read` (titles) against the envelope index
    and fetch the full text of each. Returns {title: content}. Manual docs are
    fetched from the Hub; repo docs are read straight from the checked-out
    workspace (the repo is the source of truth — never copied into the Hub).
    Bounded in count and per-doc size to protect the pass-2 budget.
    """
    requested = [r for r in (requested or []) if isinstance(r, str) and r.strip()]
    if not requested:
        return {}
    by_title = {(d.get("title") or "").strip().lower(): d for d in (doc_index or [])}
    out: dict[str, str] = {}
    for req in requested[:_MAX_REQUESTED_DOCS]:
        d = by_title.get(req.strip().lower())
        if not d:
            continue
        title = d.get("title") or req
        try:
            if d.get("source") == "repo" and d.get("repo_path") and workspace is not None:
                contents, _missing = read_files(
                    workspace, [d["repo_path"]], max_chars=_REQUESTED_DOC_CHARS)
                body = contents.get(d["repo_path"])
                if body is not None:
                    out[title] = body
            else:
                full = await hub.get_document(d["id"])
                out[title] = (full.get("content_md") or "")[:_REQUESTED_DOC_CHARS]
        except Exception as e:
            log.warning("document fetch failed for %r: %s", req, e)
    if out:
        await hub.task_event(task_id, "implementer.documents_fetched",
                             {"titles": list(out.keys())})
    return out


async def _ollama_generate(ollama: OllamaClient, prompt: str, num_predict: int) -> tuple[Optional[dict], dict]:
    """Run an Ollama generate call. Returns (parsed_json_or_none, raw_result_dict)."""
    started = time.perf_counter()
    raw = await ollama.generate(
        model=config.DEFAULT_MODEL,
        prompt=prompt,
        options={
            "num_ctx": config.DEFAULT_NUM_CTX,
            "temperature": 0,
            "seed": 42,
            "num_predict": num_predict,
        },
    )
    wall = time.perf_counter() - started
    response = raw.get("response", "")
    parsed = extract_json(response)
    raw["_wall_sec"] = wall
    raw["_response_text"] = response
    return parsed, raw


def _gen_stats(raw: dict) -> dict:
    eval_count = raw.get("eval_count") or 0
    eval_duration = raw.get("eval_duration") or 0
    tps = (eval_count / (eval_duration / 1e9)) if eval_count and eval_duration else 0.0
    return {
        "wall_sec": round(raw.get("_wall_sec", 0), 2),
        "gen_tps": round(tps, 1),
        "prompt_tokens": raw.get("prompt_eval_count"),
        "completion_tokens": eval_count,
    }


async def handle_implement(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    project = envelope.get("project") or {}
    project_slug = project.get("slug") or "default"
    task_id = task["id"]

    # 1. Workspace
    try:
        workspace = await prepare_workspace(hub, envelope)
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"workspace setup failed: {e}"},
            "notes_md": f"workspace setup failed: {e}",
        })
        return

    tree = list_tree_relevant(workspace, task=task, max_chars=1200)
    await hub.task_event(task_id, "workspace.ready", {
        "path": str(workspace),
        "tree_chars": len(tree),
    })

    # 2. Pass 1: planning — on retry, hand the LLM the actual current contents
    # of files the previous attempt modified so it can EDIT in place rather
    # than regenerating from a blank slate (which is how identical bugs keep
    # appearing across attempts).
    prior_files: dict[str, str] = {}
    # Same off-by-one as in _build_retry_block: claim bumps attempt_count to 1
    # on the first try, so "actually retrying" starts at > 1.
    if int(task.get("attempt_count") or 0) > 1:
        prev_result = task.get("result") or {}
        prev_paths = (
            (prev_result.get("files_written") or [])
            + (prev_result.get("files_modified") or [])
        )
        # Dedupe while preserving order
        seen = set()
        unique_paths = [p for p in prev_paths if p and not (p in seen or seen.add(p))]
        prior_contents, _missing = read_files(workspace, unique_paths, max_chars=12_000)
        prior_files = prior_contents
    pass1_prompt, pass1_sections = _render_pass1(project, task, tree, prior_files=prior_files, documents=envelope.get('documents'), secret_names=envelope.get('secret_names'))
    await emit_prompt_metrics(hub, task_id, "implementer_pass1", pass1_prompt,
                              pass1_sections, kind_hint=_kind_hint(task))
    await hub.task_event(task_id, "llm.call.started", {
        "mode": "implementer_pass1",
        "model": config.DEFAULT_MODEL,
        "prompt_chars": len(pass1_prompt),
    })
    try:
        pass1, raw1 = await _ollama_generate(ollama, pass1_prompt, num_predict=1024)
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"pass1 ollama call failed: {e}"},
            "notes_md": f"pass1 ollama call failed: {e}",
        })
        return
    await hub.task_event(task_id, "llm.call.completed", {"pass": 1, **_gen_stats(raw1)})

    if not isinstance(pass1, dict):
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"reason": "pass1 invalid output",
                       "raw_excerpt": (raw1.get("_response_text") or "")[:1500]},
            "notes_md": "Implementer Pass 1 produced unparseable output.",
        })
        return

    # If pass1 surfaced questions, route to human
    if pass1.get("questions"):
        await hub.task_result(task_id, {
            "outcome": "needs_human",
            "result": pass1,
            "questions": pass1["questions"],
            "notes_md": "Implementer Pass 1 raised clarifying questions.",
        })
        return

    files_to_read = pass1.get("files_to_read") or []
    files_to_write = pass1.get("files_to_write_or_modify") or []
    verify_cmds_pass1 = pass1.get("commands_to_run_for_verification") or []

    # 3. Read the requested files from the workspace, outlining any that
    # exceed the size threshold. Keep names of functions that match task
    # keywords (and the names of files the LLM said it intends to write to —
    # those are the ones whose bodies it almost certainly needs to see).
    contents, missing = read_files(workspace, files_to_read)
    if missing:
        await hub.task_event(task_id, "implementer.files_missing", {"missing": missing})

    from orchestrai_agent.workspace import tokenize
    keep_names = set()
    keep_names |= tokenize(task.get("title") or "")
    keep_names |= tokenize(task.get("description_md") or "")
    for f in (pass1.get("files_to_write_or_modify") or []):
        if isinstance(f, dict):
            keep_names |= tokenize(f.get("intent") or "")
            keep_names |= tokenize((f.get("path") or "").rsplit("/", 1)[-1])

    outline_summary: list[dict] = []
    for path, raw in list(contents.items()):
        compacted, info = maybe_outline(path, raw, keep_names)
        contents[path] = compacted
        if info["kind"] != "full":
            outline_summary.append({"path": path, **info})
    if outline_summary:
        saved = sum(o["original_chars"] - o["final_chars"] for o in outline_summary)
        await hub.task_event(task_id, "implementer.files_outlined", {
            "outlined": outline_summary,
            "chars_saved": saved,
        })

    # 3b. Fetch the full text of any project documents pass-1 asked to read.
    # The agent saw only the index (title/purpose/headings); now it pulls the
    # bodies it decided it needs — by title, matched against the envelope index.
    requested_docs = await _fetch_requested_documents(
        hub, task_id, envelope.get("documents"), pass1.get("documents_to_read"),
        workspace=workspace)

    # 4. Pass 2: generate the diff
    pass2_prompt, pass2_sections = _render_pass2(project, task, pass1, contents, documents=envelope.get('documents'), secret_names=envelope.get('secret_names'), requested_docs=requested_docs)
    await emit_prompt_metrics(hub, task_id, "implementer_pass2", pass2_prompt,
                              pass2_sections, kind_hint=_kind_hint(task))
    await hub.task_event(task_id, "llm.call.started", {
        "mode": "implementer_pass2",
        "model": config.DEFAULT_MODEL,
        "prompt_chars": len(pass2_prompt),
    })
    try:
        pass2, raw2 = await _ollama_generate(ollama, pass2_prompt, num_predict=4096)
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"pass2 ollama call failed: {e}"},
            "notes_md": f"pass2 ollama call failed: {e}",
        })
        return
    await hub.task_event(task_id, "llm.call.completed", {"pass": 2, **_gen_stats(raw2)})

    if not isinstance(pass2, dict):
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"reason": "pass2 invalid output",
                       "raw_excerpt": (raw2.get("_response_text") or "")[:1500]},
            "notes_md": "Implementer Pass 2 produced unparseable output.",
        })
        return

    files = pass2.get("files") or []
    diff = pass2.get("diff") or ""
    if not files and not diff.strip():
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"reason": "pass2 has neither files nor diff",
                       "raw_excerpt": (raw2.get("_response_text") or "")[:1500]},
            "notes_md": "Implementer Pass 2 produced neither files nor a diff.",
        })
        return

    commands = pass2.get("commands_to_run") or verify_cmds_pass1

    # 5a. Apply full-content files first (preferred path for new/rewritten files)
    written_paths: list[str] = []
    if files:
        ok, err, written_paths = await write_files(workspace, files)
        if not ok:
            await hub.task_event(task_id, "implementer.files_write_failed", {"error": err[:500]})
            await hub.task_result(task_id, {
                "outcome": "fix_needed",
                "result": {"reason": "files_write_failed", "error": err,
                           "files": [f.get("path") for f in files]},
                "notes_md": f"Could not write files:\n{err[:500]}",
            })
            return
        await hub.task_event(task_id, "implementer.files_written", {"paths": written_paths})

    # 5b. Apply diff for modifications (optional)
    if diff.strip():
        ok, err = await apply_diff(workspace, diff)
        if not ok:
            await hub.task_event(task_id, "implementer.diff_apply_failed", {"error": err[:500]})
            # A corrupt / inapplicable diff would otherwise dead-end the whole
            # attempt (qwen's diffs are unreliable). Recover by resending the
            # affected files as full content, which always applies.
            recover = await _recover_diff_as_files(
                hub=hub, ollama=ollama, task=task, project=project,
                workspace=workspace, diff=diff, apply_err=err,
                fallback_paths=[f.get("path") for f in files_to_write
                                if isinstance(f, dict) and f.get("path")],
            )
            if recover["ok"]:
                written_paths = list(dict.fromkeys(written_paths + recover["applied_files"]))
                await hub.task_event(task_id, "implementer.diff_recovered_as_files",
                                     {"paths": recover["applied_files"]})
            else:
                await hub.task_result(task_id, {
                    "outcome": "fix_needed",
                    "result": {"reason": "diff_apply_failed", "error": err, "diff": diff,
                               "files_written": written_paths,
                               "recovery_reason": recover["reason"]},
                    "notes_md": (f"Diff did not apply and full-file recovery failed:"
                                 f"\n{err[:400]}\n({recover['reason']})"),
                })
                return

    # 6. Auto-commit
    commit_sha = await commit_all(workspace, f"orchestrai: {task.get('title','task')}")
    await hub.task_event(task_id, "workspace.commit", {"sha": commit_sha or "(empty)"})

    # 7. Run verification commands (initial pass)
    cmd_results = await _run_verification(hub, task_id, commands, str(workspace))
    all_passed = all(c.get("exit") == 0 and not c.get("timed_out") for c in cmd_results)

    # 7b. Inline fix loop — if verification failed, give the LLM up to MAX_FIX_ITER
    # tight iterations to fix its own work BEFORE giving up and submitting fix_needed.
    # Each iteration sees: the actual failing output + the current file contents.
    fix_history: list[dict] = []
    if not all_passed:
        scope_paths = _collect_modified_paths(files, written_paths, files_to_write)
        for fix_iter in range(1, _MAX_FIX_ITERATIONS + 1):
            await hub.task_event(task_id, "implementer.fix_loop.iter_start", {
                "iter": fix_iter, "max_iter": _MAX_FIX_ITERATIONS,
            })
            fix_outcome = await _try_inline_fix(
                hub=hub, ollama=ollama, task=task, project=project,
                workspace=workspace, scope_paths=scope_paths,
                cmd_results=cmd_results, iter_num=fix_iter,
            )
            fix_history.append(fix_outcome)
            await hub.task_event(task_id, "implementer.fix_loop.iter_done", {
                "iter": fix_iter, "applied_files": fix_outcome.get("applied_files") or [],
                "gave_up": fix_outcome.get("gave_up", False),
                "diagnosis": (fix_outcome.get("diagnosis_md") or "")[:200],
            })
            if fix_outcome.get("gave_up") or not fix_outcome.get("applied_files"):
                # LLM couldn't help. Stop the loop and let the outer attempt-level
                # retry take over (with our full retry-section context).
                break
            # Re-run the verification commands
            cmd_results = await _run_verification(hub, task_id, commands, str(workspace))
            all_passed = all(c.get("exit") == 0 and not c.get("timed_out") for c in cmd_results)
            if all_passed:
                # Commit the inline fix so the workspace state is durable
                fix_sha = await commit_all(workspace, f"orchestrai inline-fix: iter {fix_iter}")
                await hub.task_event(task_id, "workspace.commit", {"sha": fix_sha or "(empty)"})
                break

    # 8. Result
    outcome = "success" if all_passed else "fix_needed"
    result = {
        "diff": diff,
        "files_written": written_paths,
        "commit_sha": commit_sha,
        "commands_run": cmd_results,
        "files_modified": [f.get("path") for f in files_to_write],
        "fix_iterations": fix_history,
        "notes": pass2.get("notes_md"),
    }
    notes_md = pass2.get("notes_md") or ""
    if fix_history:
        notes_md = (notes_md + f"\n\nInline fix loop: {len(fix_history)} iteration(s); "
                    f"final outcome={'success' if all_passed else 'still failing'}").strip()
    if not all_passed:
        failed = [c["cmd"] for c in cmd_results if c.get("exit", 0) != 0]
        notes_md = (notes_md + "\n\nVerification commands failed: " + ", ".join(failed)).strip()

    await hub.task_result(task_id, {
        "outcome": outcome,
        "result": result,
        "notes_md": notes_md or None,
    })


# ---------- Inline fix loop helpers -----------------------------------------

_MAX_FIX_ITERATIONS = 3


async def _run_verification(hub, task_id, commands, workspace_path) -> list[dict]:
    """Run all commands, emit events, return list of result dicts."""
    out: list[dict] = []
    for cmd in commands:
        if not isinstance(cmd, str):
            continue
        await hub.task_event(task_id, "subprocess.started", {"cmd": cmd})
        res = await run_subproc(cmd, cwd=workspace_path, timeout_sec=120)
        out.append({
            "cmd": cmd, "exit": res.exit_code,
            "stdout": res.stdout[-2000:], "stderr": res.stderr[-2000:],
            "wall_sec": round(res.wall_sec, 2), "timed_out": res.timed_out,
        })
        await hub.task_event(task_id, "subprocess.completed", {
            "cmd": cmd, "exit": res.exit_code, "wall_sec": round(res.wall_sec, 2),
        })
    return out


def _collect_modified_paths(files: list, written_paths: list[str],
                            files_to_write: list[dict]) -> list[str]:
    """Union of files we've touched in this attempt, deduped, preserves order."""
    paths: list[str] = []
    seen: set[str] = set()
    for p in written_paths or []:
        if p and p not in seen:
            paths.append(p); seen.add(p)
    for f in files_to_write or []:
        p = f.get("path") if isinstance(f, dict) else None
        if p and p not in seen:
            paths.append(p); seen.add(p)
    return paths


async def _recover_diff_as_files(*, hub, ollama, task: dict, project: dict,
                                 workspace, diff: str, apply_err: str,
                                 fallback_paths: list[str]) -> dict:
    """A corrupt / inapplicable diff would dead-end the whole attempt. Recover by
    asking the model to resend the affected files as full content — whole-file
    writes always apply, and qwen's diffs are unreliable. Returns
    {ok, applied_files, reason}; writes the recovered files into the workspace.
    """
    targets = _diff_target_paths(diff, fallback_paths)
    contents, _missing = read_files(workspace, targets, max_chars=12_000)
    parts = []
    for p in targets:
        body = contents.get(p)
        parts.append(f"--- {p} (CURRENT) ---\n{body}" if body is not None
                     else f"--- {p} (does not exist yet) ---")
    files_block = "\n\n".join(parts) or "(no target files identified)"

    prompt = _DIFF_RECOVER_TPL.format(
        task_title=task.get("title", "(no title)"),
        task_description=task.get("description_md") or "(no description)",
        failed_diff=diff[:3000],
        apply_error=(apply_err or "")[:600],
        files_block=files_block,
    )
    parsed, raw = await _ollama_generate(ollama, prompt, num_predict=4096)
    await hub.task_event(task["id"], "llm.call.completed",
                         {"mode": "implementer_diff_recover", **_gen_stats(raw)})
    if not isinstance(parsed, dict) or not (parsed.get("files") or []):
        return {"ok": False, "applied_files": [], "reason": "recovery produced no files"}
    ok, err, written = await write_files(workspace, parsed["files"])
    if not ok:
        return {"ok": False, "applied_files": [], "reason": f"write failed: {err}"}
    return {"ok": True, "applied_files": written, "reason": ""}


async def _try_inline_fix(*, hub, ollama, task: dict, project: dict,
                          workspace, scope_paths: list[str],
                          cmd_results: list[dict], iter_num: int) -> dict:
    """Run one fix-LLM cycle.

    Returns dict with keys: applied_files (list[str]), diagnosis_md (str),
    gave_up (bool), gave_up_reason (str).
    Side effect: writes the corrected files into the workspace.
    """
    # Render the fixer prompt
    contents, _missing = read_files(workspace, scope_paths, max_chars=12_000)
    files_block = "\n\n".join(f"--- {p} (CURRENT) ---\n{c}" for p, c in contents.items()) \
                   or "(no files in scope — unexpected)"

    failing = [c for c in cmd_results if c.get("exit", 0) != 0 or c.get("timed_out")]
    cmd_outputs = "\n\n".join(
        f"$ {c['cmd']}\n[exit={c['exit']}{', TIMEOUT' if c.get('timed_out') else ''}]"
        + (f"\n--- stderr ---\n{(c.get('stderr') or '')[-1500:]}" if c.get('stderr') else "")
        + (f"\n--- stdout ---\n{(c.get('stdout') or '')[-1500:]}" if c.get('stdout') else "")
        for c in failing[:3]
    ) or "(no failing commands?)"

    prompt = _FIX_TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_context_indented=_indent(project.get("context_md") or "(no context)"),
        task_title=task.get("title", "(no title)"),
        task_description=task.get("description_md") or "(no description)",
        acceptance_criteria_indented=_format_criteria(task.get("acceptance_criteria") or []),
        iter_num=iter_num,
        max_iter=_MAX_FIX_ITERATIONS,
        files_block=files_block,
        cmd_outputs=cmd_outputs,
    )

    parsed, raw = await _ollama_generate(ollama, prompt, num_predict=2048)
    await hub.task_event(task['id'], "llm.call.completed",
                         {"mode": f"implementer_fix_iter_{iter_num}", **_gen_stats(raw)})

    if not isinstance(parsed, dict):
        return {"applied_files": [], "diagnosis_md": "",
                "gave_up": True, "gave_up_reason": "fixer output unparseable"}

    reason = parsed.get("give_up_reason") or ""
    files = parsed.get("files") or []
    if reason or not files:
        return {"applied_files": [], "diagnosis_md": parsed.get("diagnosis_md") or "",
                "gave_up": True, "gave_up_reason": reason or "fixer returned no files"}

    ok, err, written = await write_files(workspace, files)
    if not ok:
        return {"applied_files": [], "diagnosis_md": parsed.get("diagnosis_md") or "",
                "gave_up": True, "gave_up_reason": f"could not write fix files: {err}"}
    return {"applied_files": written, "diagnosis_md": parsed.get("diagnosis_md") or "",
            "gave_up": False, "notes_md": parsed.get("notes_md") or ""}
