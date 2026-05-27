"""Revise handler.

Reads the existing plan + the user's edit request from task.payload, produces
a new plan version. Hub-side, this acts like a new plan submission (creates a
new plans row with higher version + a fresh approval question, supersedes the
previous draft).
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


def _indent(text: str, prefix: str = "    ") -> str:
    text = text or ""
    return "\n".join(prefix + line for line in text.splitlines()) or (prefix + "(empty)")


async def handle_revise(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    project = envelope.get("project") or {}
    task_id = task["id"]
    payload = task.get("payload") or {}
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
