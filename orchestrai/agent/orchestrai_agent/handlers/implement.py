"""Implement handler — two-pass.

Pass 1: tell the model the task + workspace tree, get back which files to
read and which to write, plus verification commands.

Pass 2: send the requested file contents back, get a unified diff.

Apply the diff, run verification commands, submit the result.
"""

import logging
import time
from pathlib import Path
from typing import Any, Optional

from orchestrai_agent.config import config
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.response_parser import extract_json
from orchestrai_agent.subprocess_util import run as run_subproc
from orchestrai_agent.workspace import (
    apply_diff, commit_all, ensure_workspace, list_tree, read_files, write_files,
)

log = logging.getLogger("orchestrai-agent.implement")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PASS1_TPL = (PROMPTS_DIR / "implementer_pass1.md").read_text(encoding="utf-8")
_PASS2_TPL = (PROMPTS_DIR / "implementer_pass2.md").read_text(encoding="utf-8")
_FIX_TPL = (PROMPTS_DIR / "implementer_fix.md").read_text(encoding="utf-8")


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


def _retry_section(task: dict) -> str:
    """Produce a prominent block enumerating prior failures and demanding a
    different approach. Empty string on the first attempt — added only when
    attempt_count > 0 so we don't add noise to first-try prompts.

    The notes already contain per-attempt stamped reasons. We re-render them
    here at top-level so the LLM can't miss them, plus include the actual
    stdout/stderr from the most recent failing verification command (where
    the real error message lives — e.g. an AssertionError with the actual
    vs expected values), plus explicit guidance.
    """
    attempts = int(task.get("attempt_count") or 0)
    if attempts <= 0:
        return ""

    notes = (task.get("notes") or "").strip()
    failure_lines = [ln for ln in notes.splitlines() if ln.startswith("[")][-5:]
    failure_block = "\n".join(failure_lines) or "(no specific failure reasons recorded)"

    # Grab the most recent failed command's stdout/stderr from the previous
    # attempt's result. This is what tells the LLM e.g. WHICH assertion failed
    # — far more useful than the generic "verification commands failed" note.
    last_result = task.get("result") or {}
    cmd_outputs: list[str] = []
    for c in (last_result.get("commands_run") or []):
        if not isinstance(c, dict):
            continue
        if int(c.get("exit", 0)) == 0 and not c.get("timed_out"):
            continue
        cmd = c.get("cmd", "(unknown)")
        out = (c.get("stdout") or "").strip()
        err = (c.get("stderr") or "").strip()
        exit_code = c.get("exit", "?")
        timed_out = c.get("timed_out", False)
        body = f"$ {cmd}\n[exit={exit_code}{', TIMEOUT' if timed_out else ''}]"
        if err:
            body += f"\n--- stderr ---\n{err[-1500:]}"
        if out:
            body += f"\n--- stdout ---\n{out[-1500:]}"
        cmd_outputs.append(body)
    cmd_outputs_block = (
        "\n\nRAW OUTPUT OF FAILING COMMAND(S) FROM LAST ATTEMPT (READ THIS — the\n"
        "actual error / assertion message tells you what to fix):\n```\n"
        + ("\n\n".join(cmd_outputs[:2]))
        + "\n```"
    ) if cmd_outputs else ""

    return (
        "▲▲▲ PREVIOUS ATTEMPT(S) FAILED — READ THIS BEFORE ANYTHING ELSE ▲▲▲\n"
        f"This is attempt {attempts + 1}. Earlier attempts produced the following errors:\n"
        f"{failure_block}"
        f"{cmd_outputs_block}\n\n"
        "MANDATORY RULES FOR THIS ATTEMPT:\n"
        "  1. Do NOT repeat the same approach that failed above. Try something different.\n"
        "  2. READ the raw failing-command output above (if any). The specific error\n"
        "     message — `AssertionError: assert 7 == 42`, `ModuleNotFoundError: No module\n"
        "     named 'X'`, `SyntaxError: ...`, etc. — tells you precisely what to fix.\n"
        "     Don't rewrite unrelated files; fix the SPECIFIC thing that broke.\n"
        "  3. When a pytest assertion fails with `assert actual == expected`:\n"
        "       - If the implementation matches the spec, the EXPECTED value in the test\n"
        "         is probably wrong (LLMs often guess test values). Update the test to\n"
        "         match the actual value, OR rewrite the test as a property check that\n"
        "         doesn't depend on a specific numeric output.\n"
        "       - If the spec is clear on the value (e.g. \"add(2,3) returns 5\") then\n"
        "         the implementation is wrong. Fix the implementation.\n"
        "  4. If a previous attempt's diff was rejected (`corrupt patch`, `does not apply`),\n"
        "     DO NOT produce a diff this time — use the `files[]` array with full file\n"
        "     contents instead.\n"
        "  5. If exit 127 (`command not found`), the tool is not installed. Either add a\n"
        "     `pip install` / `npm install` step EARLIER in `commands_to_run`, or use a\n"
        "     different verification (e.g. `python -c \"import mymodule\"`).\n"
        "  6. If a verification command hangs (uvicorn --reload, npm start), it is NOT a\n"
        "     valid acceptance check. Use a one-shot import/assertion instead, AND surface\n"
        "     a `questions[]` entry asking the human to fix the criterion.\n"
        "  7. If you cannot find a different approach, surface a clarifying question in\n"
        "     `questions[]` rather than producing the same broken output again.\n"
        "▲▲▲\n\n"
    )


def _http_ports_block() -> str:
    if not config.HTTP_PORTS:
        return ("HOST-REACHABLE HTTP PORTS: (none available — do not bind any "
                "server to a port expecting host visibility)")
    ports_csv = ", ".join(str(p) for p in config.HTTP_PORTS)
    first = config.HTTP_PORTS[0]
    return (
        f"HOST-REACHABLE HTTP PORTS: {ports_csv}\n"
        f"  If this task hosts a server for human review, bind to "
        f"{config.HTTP_BIND_HOST}:<port> (NOT 127.0.0.1) on one of those ports.\n"
        f"  Use `orchestrai-serve --port <port> -- <command...>` to start the\n"
        f"  server detached AND have the verification command exit 0 once the\n"
        f"  port is reachable. Example:\n"
        f"    orchestrai-serve --port {first} -- uvicorn main:app --host 0.0.0.0 --port {first}\n"
        f"  Users reach the app at http://localhost:<port> on the Docker host."
    )


# Packages that ship in the base agent image. Keep this in sync with
# Dockerfile.agent — anything here can be imported with no install step.
_PREINSTALLED_PY = [
    "httpx", "pydantic", "pytest", "pytest-asyncio", "ruff", "black", "mypy",
    "requests", "sqlalchemy", "alembic",
    "fastapi", "uvicorn[standard]", "jinja2", "python-multipart",
]


def _tools_block(project: dict) -> str:
    declared = ((project.get("tools") or {}).get("python_packages") or [])
    lines = ["AVAILABLE PYTHON PACKAGES — import these directly, NO pip install step needed:"]
    lines.append("  preinstalled: " + ", ".join(_PREINSTALLED_PY))
    if declared:
        lines.append("  project-declared (already installed at task-claim time): "
                     + ", ".join(declared))
    else:
        lines.append("  project-declared: (none beyond preinstalled)")
    lines.append("  IF you need a package NOT in either list, STOP and add a "
                 "`questions[]` entry asking for it to be added to the project's "
                 "tool registry. DO NOT pip-install inline; tasks must not "
                 "modify the project's tool list directly.")
    lines.append("  Strongly prefer the preinstalled web stack (FastAPI + uvicorn "
                 "+ jinja2) over Flask/Django/Bottle.")
    return "\n".join(lines)


def _render_pass1(project: dict, task: dict, workspace_tree: str,
                  prior_files: dict | None = None) -> str:
    # On retry, prior_files holds {path: contents} for the files the previous
    # attempt modified. We splice them into the retry section so the LLM sees
    # what's currently on disk instead of regenerating from scratch.
    rs = _retry_section(task)
    if rs and prior_files:
        snippets = []
        for path, content in prior_files.items():
            snippets.append(f"--- {path} (CURRENT CONTENT) ---\n{content[-2500:]}")
        rs += (
            "CURRENT CONTENTS OF FILES YOU MODIFIED LAST ATTEMPT (they are still there;\n"
            "EDIT them, do not rewrite from scratch and reintroduce the same bug):\n```\n"
            + "\n\n".join(snippets) + "\n```\n\n"
        )
    return _PASS1_TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_description=project.get("description_md") or "(no description)",
        project_context_indented=_indent(project.get("context_md") or "(no context)"),
        task_title=task.get("title", "(no title)"),
        task_description=task.get("description_md") or "(no description)",
        repo_name="(no specific repo)",
        branch_name=task.get("branch_name") or "(no branch)",
        acceptance_criteria_indented=_format_criteria(task.get("acceptance_criteria") or []),
        notes_indented=_indent(task.get("notes") or "(none)"),
        retry_section=rs,
        http_ports_block=_http_ports_block(),
        tools_block=_tools_block(project),
        workspace_tree=workspace_tree,
    )


def _render_pass2(project: dict, task: dict, pass1: dict, files_contents: dict) -> str:
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
    return _PASS2_TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_description=project.get("description_md") or "(no description)",
        project_context_indented=_indent(project.get("context_md") or "(no context)"),
        task_title=task.get("title", "(no title)"),
        task_description=task.get("description_md") or "(no description)",
        repo_name="(no specific repo)",
        branch_name=task.get("branch_name") or "(no branch)",
        acceptance_criteria_indented=_format_criteria(task.get("acceptance_criteria") or []),
        retry_section=_retry_section(task),
        http_ports_block=_http_ports_block(),
        tools_block=_tools_block(project),
        files_to_write_summary=files_summary,
        diff_plan_md=pass1.get("diff_plan_md") or "(no plan provided)",
        files_contents=files_block,
    )


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
        workspace = await ensure_workspace(project_slug)
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"workspace setup failed: {e}"},
            "notes_md": f"workspace setup failed: {e}",
        })
        return

    tree = list_tree(workspace)
    await hub.task_event(task_id, "workspace.ready", {
        "path": str(workspace),
        "tree_chars": len(tree),
    })

    # 2. Pass 1: planning — on retry, hand the LLM the actual current contents
    # of files the previous attempt modified so it can EDIT in place rather
    # than regenerating from a blank slate (which is how identical bugs keep
    # appearing across attempts).
    prior_files: dict[str, str] = {}
    if int(task.get("attempt_count") or 0) > 0:
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
    pass1_prompt = _render_pass1(project, task, tree, prior_files=prior_files)
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

    # 3. Read the requested files from the workspace
    contents, missing = read_files(workspace, files_to_read)
    if missing:
        await hub.task_event(task_id, "implementer.files_missing", {"missing": missing})

    # 4. Pass 2: generate the diff
    pass2_prompt = _render_pass2(project, task, pass1, contents)
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
            await hub.task_result(task_id, {
                "outcome": "fix_needed",
                "result": {"reason": "diff_apply_failed", "error": err, "diff": diff,
                           "files_written": written_paths},
                "notes_md": f"Diff did not apply cleanly:\n{err[:500]}",
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
