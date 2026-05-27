"""respond_to_ci_failure handler.

Reads the build log + branch from task.payload, diagnoses, produces a fix
(files-first like the Implementer), applies it in the workspace, runs the
verification command, and submits result.
"""

import logging
import time
from pathlib import Path

from orchestrai_agent.config import config
from orchestrai_agent.handlers.implement import _gen_stats
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.response_parser import extract_json
from orchestrai_agent.subprocess_util import run as run_subproc
from orchestrai_agent.workspace import (
    apply_diff, commit_all, ensure_workspace, list_tree, write_files,
)

log = logging.getLogger("orchestrai-agent.ci_fix")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_TPL = (PROMPTS_DIR / "ci_fixer.md").read_text(encoding="utf-8")


def _indent(text: str, prefix: str = "    ") -> str:
    text = text or ""
    return "\n".join(prefix + line for line in text.splitlines()) or (prefix + "(empty)")


async def handle_ci_failure(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    project = envelope.get("project") or {}
    task_id = task["id"]
    payload = task.get("payload") or {}
    project_slug = project.get("slug") or "default"

    workspace = await ensure_workspace(project_slug)
    tree = list_tree(workspace)

    log_tail = payload.get("log_tail") or "(no log provided)"
    workflow = payload.get("workflow", "(unknown)")
    step_name = payload.get("step_name", "(unknown)")
    build_url = payload.get("build_url", "(unknown)")
    branch_name = task.get("branch_name") or payload.get("branch_name") or "(unknown)"

    prompt = _TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_context_indented=_indent(project.get("context_md") or "(no context)"),
        branch_name=branch_name,
        workflow=workflow,
        step_name=step_name,
        build_url=build_url,
        log_tail=log_tail[-4000:],
        workspace_tree=tree,
    )

    await hub.task_event(task_id, "llm.call.started", {
        "mode": "ci_fixer", "prompt_chars": len(prompt),
    })

    started = time.perf_counter()
    try:
        raw = await ollama.generate(
            model=config.DEFAULT_MODEL,
            prompt=prompt,
            options={
                "num_ctx": config.DEFAULT_NUM_CTX,
                "temperature": 0,
                "seed": 42,
                "num_predict": 2048,
            },
        )
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"ollama failed: {e}"},
            "notes_md": f"ollama_failed: {e}",
        })
        return
    raw["_wall_sec"] = time.perf_counter() - started
    raw["_response_text"] = raw.get("response", "")
    await hub.task_event(task_id, "llm.call.completed", _gen_stats(raw))

    parsed = extract_json(raw["_response_text"])
    if not isinstance(parsed, dict):
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"raw_excerpt": raw["_response_text"][:1500]},
            "notes_md": "CI fixer output failed validation",
        })
        return

    if parsed.get("questions"):
        await hub.task_result(task_id, {
            "outcome": "needs_human",
            "result": {"diagnosis_md": parsed.get("diagnosis_md")},
            "questions": parsed["questions"],
            "notes_md": parsed.get("diagnosis_md") or "CI fixer escalated to human",
        })
        return

    files = parsed.get("files") or []
    diff = (parsed.get("diff") or "").strip()
    if not files and not diff:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"reason": "no fix produced",
                       "diagnosis_md": parsed.get("diagnosis_md")},
            "notes_md": "Empty fix from CI fixer",
        })
        return

    written: list[str] = []
    if files:
        ok, err, written = await write_files(workspace, files)
        if not ok:
            await hub.task_result(task_id, {
                "outcome": "fix_needed",
                "result": {"error": err, "files": [f.get("path") for f in files]},
                "notes_md": f"write_files failed: {err}",
            })
            return
    if diff:
        ok, err = await apply_diff(workspace, diff)
        if not ok:
            await hub.task_result(task_id, {
                "outcome": "fix_needed",
                "result": {"error": err, "diff": diff},
                "notes_md": f"apply_diff failed: {err}",
            })
            return

    commit_sha = await commit_all(workspace, f"orchestrai ci-fix: {task.get('title','')}")
    await hub.task_event(task_id, "workspace.commit", {"sha": commit_sha or "(empty)"})

    # Run verification command(s)
    cmd_results = []
    all_ok = True
    for cmd in (parsed.get("commands_to_run") or []):
        if not isinstance(cmd, str):
            continue
        res = await run_subproc(cmd, cwd=str(workspace), timeout_sec=120)
        cmd_results.append({
            "cmd": cmd, "exit": res.exit_code,
            "stdout": res.stdout[-1500:], "stderr": res.stderr[-1500:],
        })
        if res.exit_code != 0:
            all_ok = False

    outcome = "success" if all_ok else "fix_needed"
    await hub.task_result(task_id, {
        "outcome": outcome,
        "result": {
            "diagnosis_md": parsed.get("diagnosis_md"),
            "files_written": written,
            "diff": diff,
            "commit_sha": commit_sha,
            "commands_run": cmd_results,
        },
        "notes_md": parsed.get("notes_md") or parsed.get("diagnosis_md"),
    })
