"""Planner handler.

Reads the goal description from the task envelope, calls the model with the
planner prompt, parses structured JSON, and submits the result. The Hub
turns the result into a Plan row + plan_approval Question.
"""

import time
from pathlib import Path
from typing import Any

from orchestrai_agent.config import config
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.response_parser import extract_json

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_TEMPLATE = (PROMPTS_DIR / "planner.md").read_text(encoding="utf-8")


def _render_prompt(project: dict, goal: dict) -> str:
    context = project.get("context_md") or "(no project context provided)"
    indented = "\n".join("    " + line for line in context.splitlines())
    if config.HTTP_PORTS:
        ports_line = ", ".join(str(p) for p in config.HTTP_PORTS)
        http_ports_block = (
            f"  HTTP demo ports mapped to the host: {ports_line}\n"
            f"  (bind to {config.HTTP_BIND_HOST}:<port> in-container; users reach "
            f"them at http://localhost:<port>)"
        )
    else:
        http_ports_block = "  HTTP demo ports: (none — no host-reachable ports available)"
    return _PROMPT_TEMPLATE.format(
        project_name=project.get("name", "(unnamed project)"),
        project_slug=project.get("slug", ""),
        project_description=project.get("description_md") or "(no description)",
        project_context_indented=indented,
        goal_title=goal.get("title", "(no title)"),
        goal_description=goal.get("description_md", "(no description)"),
        http_ports_block=http_ports_block,
    )


_ALLOWED_PLANNER_TASK_TYPES = {"implement", "review"}
_ALLOWED_PRIORITIES = {"low", "normal", "high", "critical"}


def _validate_plan_output(parsed: Any) -> tuple[bool, str]:
    if not isinstance(parsed, dict):
        return False, "output is not a JSON object"
    plan_md = parsed.get("plan_md")
    tasks = parsed.get("tasks")
    questions = parsed.get("questions") or []
    if not plan_md and not questions:
        return False, "neither plan_md nor questions present"
    if plan_md is not None and not isinstance(plan_md, str):
        return False, "plan_md must be a string"
    if plan_md and (not isinstance(tasks, list) or not tasks):
        return False, "plan present but tasks missing/empty"
    if isinstance(tasks, list):
        titles = set()
        for i, t in enumerate(tasks):
            if not isinstance(t, dict):
                return False, f"task[{i}] is not an object"
            if not t.get("title"):
                return False, f"task[{i}] missing title"
            if t["title"] in titles:
                return False, f"duplicate task title: {t['title']}"
            titles.add(t["title"])
            # Coerce unknown enum values to safe defaults rather than failing
            t.setdefault("type", "implement")
            if t["type"] not in _ALLOWED_PLANNER_TASK_TYPES:
                t["type"] = "implement"
            t.setdefault("priority", "normal")
            if t["priority"] not in _ALLOWED_PRIORITIES:
                t["priority"] = "normal"
            t.setdefault("description_md", "")
            t.setdefault("acceptance_criteria", [])
            for dep in t.get("depends_on_titles") or []:
                if dep not in titles:
                    return False, f"task[{i}] depends_on_titles references "\
                                  f"unknown or later task '{dep}'"
    return True, "ok"


async def handle_plan(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    project = envelope.get("project") or {}
    task_id = task["id"]

    # Fetch the goal — the task envelope doesn't include it directly
    goal_id = task.get("goal_id")
    goal: dict = {"title": task["title"], "description_md": task["description_md"]}
    if goal_id:
        try:
            data = await hub.get_goal(goal_id)
            goal = data["goal"]
        except Exception as e:
            await hub.task_event(task_id, "task.warning",
                                 {"message": f"could not fetch goal: {e}"})

    prompt = _render_prompt(project, goal)

    await hub.task_event(task_id, "llm.call.started", {
        "mode": "planner",
        "model": config.DEFAULT_MODEL,
        "num_ctx": config.DEFAULT_NUM_CTX,
        "prompt_chars": len(prompt),
    })

    started = time.perf_counter()
    try:
        result = await ollama.generate(
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
            "result": {"error": f"ollama call failed: {e}"},
            "notes_md": f"LLM call failed: {e}",
        })
        return

    wall = time.perf_counter() - started
    response = result.get("response", "")

    eval_count = result.get("eval_count") or 0
    eval_duration = result.get("eval_duration") or 0
    gen_tps = (eval_count / (eval_duration / 1e9)) if eval_count and eval_duration else 0.0

    await hub.task_event(task_id, "llm.call.completed", {
        "wall_sec": round(wall, 2),
        "gen_tps": round(gen_tps, 1),
        "prompt_tokens": result.get("prompt_eval_count"),
        "completion_tokens": eval_count,
    })

    parsed = extract_json(response)
    ok, reason = _validate_plan_output(parsed)
    if not ok:
        await hub.task_event(task_id, "planner.invalid_output", {"reason": reason})
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"raw_response_excerpt": response[:1500], "reason": reason},
            "notes_md": f"Planner output failed validation: {reason}",
        })
        return

    # Success path
    plan_md = parsed.get("plan_md") or ""
    tasks_out = parsed.get("tasks") or []
    questions = parsed.get("questions") or []

    await hub.task_result(task_id, {
        "outcome": "success",
        "result": {
            "plan_md": plan_md,
            "tasks": tasks_out,
            "questions": questions,
        },
    })
