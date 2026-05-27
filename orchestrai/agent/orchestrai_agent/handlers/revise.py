"""Revise handler — two modes:

1. PLAN REVISE (payload contains `plan_id`): regenerates the whole plan
   based on a human edit_request.
2. TASK REPAIR (payload contains `failed_task_id`): a task hit max_attempts;
   the revise handler diagnoses why and rewrites that single task's
   description/criteria so a fresh attempt can succeed.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx

from orchestrai_agent.config import config
from orchestrai_agent.handlers.plan import _ALLOWED_PLANNER_TASK_TYPES, _ALLOWED_PRIORITIES, _validate_plan_output
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.response_parser import extract_json

log = logging.getLogger("orchestrai-agent.revise")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_TPL = (PROMPTS_DIR / "revisor.md").read_text(encoding="utf-8")
_REPAIR_TPL = (PROMPTS_DIR / "task_repair.md").read_text(encoding="utf-8")


def _indent(text: str, prefix: str = "    ") -> str:
    text = text or ""
    return "\n".join(prefix + line for line in text.splitlines()) or (prefix + "(empty)")


async def handle_revise(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    project = envelope.get("project") or {}
    task_id = task["id"]
    payload = task.get("payload") or {}

    # Task-repair branch wins when both are present (defensive)
    if payload.get("failed_task_id"):
        await _handle_task_repair(hub, ollama, envelope, payload)
        return

    plan_id = payload.get("plan_id")
    edit_request = payload.get("edit_request", "")

    if not plan_id:
        await hub.task_result(task_id, {
            "outcome": "failed",
            "result": {"error": "no plan_id in payload"},
            "notes_md": "revise task missing plan_id",
        })
        return

    # Fetch the existing plan and goal
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{hub.base}/api/goals/{task.get('goal_id') or ''}",
                                 headers={"Authorization": f"Bearer {hub.lease_token}"})
            r.raise_for_status()
            goal_data = r.json()
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"could not fetch goal: {e}"},
            "notes_md": f"fetch_goal_failed: {e}",
        })
        return

    goal = goal_data.get("goal") or {}
    plans = goal_data.get("plans") or []
    prev = next((p for p in plans if p.get("id") == plan_id), None)
    if not prev:
        # Fall back: use the highest-version plan on this goal
        prev = max(plans, key=lambda p: p.get("version", 0), default=None)

    previous_version = (prev or {}).get("version", 1)

    # We need the FULL plan content + outline. Pull directly.
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{hub.base}/api/plans/{(prev or {}).get('id', '')}",
                                 headers={"Authorization": f"Bearer {hub.lease_token}"})
            r.raise_for_status()
            full = r.json().get("plan") or {}
    except Exception:
        full = {}
    plan_md = full.get("content_md") or "(previous plan not available)"
    task_outline = full.get("task_outline") or []
    if isinstance(task_outline, str):
        try:
            task_outline = json.loads(task_outline)
        except Exception:
            task_outline = []

    outline_rendered = "\n".join(
        f"- {t.get('title','(untitled)')}: {t.get('description_md','')[:120]}"
        for t in task_outline
    ) or "(no tasks in previous plan)"

    prompt = _TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_context_indented=_indent(project.get("context_md") or "(no context)"),
        goal_title=goal.get("title", "(no title)"),
        goal_description=goal.get("description_md") or "(no description)",
        previous_version=previous_version,
        plan_md=plan_md,
        task_outline_rendered=outline_rendered,
        edit_request=edit_request or "(no specific edits provided)",
    )

    await hub.task_event(task_id, "llm.call.started", {
        "mode": "revisor", "prompt_chars": len(prompt),
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
                "num_predict": 4096,
            },
        )
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"ollama failed: {e}"},
            "notes_md": f"ollama_failed: {e}",
        })
        return

    wall = time.perf_counter() - started
    response = raw.get("response", "")
    eval_count = raw.get("eval_count") or 0
    eval_duration = raw.get("eval_duration") or 0
    tps = (eval_count / (eval_duration / 1e9)) if eval_count and eval_duration else 0.0
    await hub.task_event(task_id, "llm.call.completed", {
        "wall_sec": round(wall, 2), "gen_tps": round(tps, 1),
        "completion_tokens": eval_count,
    })

    parsed = extract_json(response)
    ok, reason = _validate_plan_output(parsed)
    if not ok:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"raw_excerpt": response[:1500], "reason": reason},
            "notes_md": f"Revisor output failed validation: {reason}",
        })
        return

    # Submit as a regular plan-task result so the Hub creates a new plan version
    await hub.task_result(task_id, {
        "outcome": "success",
        "result": {
            "plan_md": parsed.get("plan_md") or "",
            "tasks": parsed.get("tasks") or [],
            "questions": parsed.get("questions") or [],
        },
    })


# ----------------------------------------------------------------------------
# Task-repair mode: rewrite an individual failed task so it can succeed.
# ----------------------------------------------------------------------------

async def _handle_task_repair(hub: HubClient, ollama: OllamaClient,
                              envelope: dict, payload: dict) -> None:
    project = envelope.get("project") or {}
    repair_task = envelope["task"]
    repair_task_id = repair_task["id"]
    failed_task_id = payload["failed_task_id"]

    # Fetch the failed task in full
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{hub.base}/api/tasks/{failed_task_id}",
                headers={"Authorization": f"Bearer {hub.lease_token}"},
            )
            r.raise_for_status()
            failed_data = r.json()
    except Exception as e:
        await hub.task_result(repair_task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"could not fetch failed task: {e}"},
            "notes_md": f"fetch_failed_task: {e}",
        })
        return

    failed = failed_data.get("task") or {}
    criteria = failed.get("acceptance_criteria") or []
    criteria_indented = "\n".join(
        f"  - {c if isinstance(c, str) else json.dumps(c)}" for c in criteria
    ) or "    (none)"

    last_result = failed.get("result") or {}
    last_result_excerpt = json.dumps(last_result, indent=2)[:1500] if last_result else "(empty)"

    prompt = _REPAIR_TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_context_indented=_indent(project.get("context_md") or "(no context)"),
        task_title=failed.get("title", "(no title)"),
        task_description=failed.get("description_md") or "(no description)",
        acceptance_criteria_indented=criteria_indented,
        attempt_count=failed.get("attempt_count", 0),
        max_attempts=failed.get("max_attempts", 3),
        notes_indented=_indent(failed.get("notes") or "(none)"),
        last_result_excerpt=last_result_excerpt,
    )

    await hub.task_event(repair_task_id, "llm.call.started", {
        "mode": "task_repair", "prompt_chars": len(prompt),
        "failed_task_id": failed_task_id,
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
                "num_predict": 1536,
            },
        )
    except Exception as e:
        await hub.task_result(repair_task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"ollama failed: {e}"},
            "notes_md": f"ollama_failed: {e}",
        })
        return

    wall = time.perf_counter() - started
    response = raw.get("response", "")
    eval_count = raw.get("eval_count") or 0
    eval_duration = raw.get("eval_duration") or 0
    tps = (eval_count / (eval_duration / 1e9)) if eval_count and eval_duration else 0.0
    await hub.task_event(repair_task_id, "llm.call.completed", {
        "wall_sec": round(wall, 2), "gen_tps": round(tps, 1),
        "completion_tokens": eval_count,
    })

    parsed = extract_json(response)
    if not isinstance(parsed, dict) or "verdict" not in parsed:
        await hub.task_result(repair_task_id, {
            "outcome": "fix_needed",
            "result": {"raw_excerpt": response[:1500]},
            "notes_md": "Task-repair output failed validation",
        })
        return

    verdict = parsed.get("verdict")
    if verdict == "escalate_to_human":
        await hub.task_result(repair_task_id, {
            "outcome": "success",
            "result": {
                "verdict": "escalate_to_human",
                "diagnosis_md": parsed.get("diagnosis_md"),
                "failed_task_id": failed_task_id,
            },
            "questions": [{
                "kind": "clarification",
                "prompt_md": (
                    f"Task `{failed.get('title','(no title)')}` could not be auto-repaired.\n\n"
                    f"**Diagnosis:** {parsed.get('diagnosis_md') or '(no diagnosis)'}\n\n"
                    f"**Question for you:** {parsed.get('human_question') or 'How should this task be reworked?'}"
                ),
            }],
            "notes_md": "Task-repair escalated to human.",
        })
        return

    # verdict == "rewrite": apply the new fields to the failed task and reset it
    new_title = (parsed.get("new_title") or failed.get("title", "")).strip()
    new_desc = parsed.get("new_description_md") or failed.get("description_md", "")
    new_criteria = parsed.get("new_acceptance_criteria") or []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # 1. PATCH the failed task with the rewritten content
            patch_body: dict = {
                "title": new_title,
                "description_md": new_desc,
                "acceptance_criteria": new_criteria,
            }
            r1 = await client.patch(
                f"{hub.base}/api/tasks/{failed_task_id}",
                json=patch_body,
                headers={"Authorization": f"Bearer {hub.lease_token}"},
            )
            r1.raise_for_status()
            # 2. Retry the failed task — resets attempts to 0, status → ready
            r2 = await client.post(
                f"{hub.base}/api/tasks/{failed_task_id}/retry",
                json={},
                headers={"Authorization": f"Bearer {hub.lease_token}"},
            )
            r2.raise_for_status()
            # 3. Annotate that this task was auto-repaired (loop guard)
            await client.post(
                f"{hub.base}/api/tasks/{failed_task_id}/notes",
                json={"note_md": "[task_repair] auto-rewritten and re-queued by repair task "
                                  f"{repair_task_id}. Diagnosis: {parsed.get('diagnosis_md','')}"},
                headers={"Authorization": f"Bearer {hub.lease_token}"},
            )
    except Exception as e:
        await hub.task_result(repair_task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"could not apply repair: {e}"},
            "notes_md": f"apply_repair_failed: {e}",
        })
        return

    await hub.task_result(repair_task_id, {
        "outcome": "success",
        "result": {
            "verdict": "rewrite",
            "diagnosis_md": parsed.get("diagnosis_md"),
            "failed_task_id": failed_task_id,
            "new_title": new_title,
            "new_acceptance_criteria_count": len(new_criteria),
        },
    })
