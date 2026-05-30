"""Planner handler.

Reads the goal description from the task envelope, calls the model with the
planner prompt, parses structured JSON, and submits the result. The Hub
turns the result into a Plan row + plan_approval Question.
"""

import logging
import re
import time
from pathlib import Path
from typing import Any

from orchestrai_agent.config import config
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.prompt_metrics import emit as emit_prompt_metrics
from orchestrai_agent.response_parser import extract_json

log = logging.getLogger("orchestrai-agent.plan")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_PROMPT_TEMPLATE = (PROMPTS_DIR / "planner.md").read_text(encoding="utf-8")


# pytest-cov is NOT in the agent image, so any --cov* flag makes pytest exit 4
# ("unrecognized arguments: --cov"). Planners reach for coverage flags
# unprompted; strip them deterministically rather than let the criterion fail
# forever. Order matters: longer / value-less variants precede bare --cov so it
# can't match the prefix of --cov-report etc. The leading \s* swallows the
# preceding space and the lookahead anchors each flag at a token boundary.
_COV_FLAG_RE = re.compile(
    r"\s*(?:"
    r"--cov-report(?:[= ]\S+)?"
    r"|--cov-config(?:[= ]\S+)?"
    r"|--cov-fail-under(?:[= ]\S+)?"
    r"|--cov-context(?:[= ]\S+)?"
    r"|--cov-branch"
    r"|--cov-append"
    r"|--no-cov-on-fail"
    r"|--no-cov"
    r"|--cov(?:=\S+)?"
    r")(?=\s|$)"
)


def _strip_coverage_flags(cmd: str) -> str:
    cleaned = _COV_FLAG_RE.sub("", cmd)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def sanitize_acceptance_criteria(criteria: list) -> list:
    """Deterministically fix acceptance criteria the agent image can't satisfy.

    Today this strips pytest coverage flags (pytest-cov isn't installed, so they
    error). Returns a new list and logs each rewrite so planner/repair drift is
    visible. Used by both the planner output validator and task-repair.
    """
    out: list = []
    for c in criteria or []:
        if (isinstance(c, dict) and c.get("kind") == "test"
                and isinstance(c.get("cmd"), str)):
            fixed = _strip_coverage_flags(c["cmd"])
            if fixed != c["cmd"]:
                log.info("sanitized acceptance criterion: %r -> %r", c["cmd"], fixed)
                c = {**c, "cmd": fixed}
        out.append(c)
    return out


def _render_prompt(project: dict, goal: dict) -> tuple[str, dict]:
    """Render the planner prompt and return (prompt, section_sizes)."""
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

    tools = project.get("tools") or {}
    py_pkgs = tools.get("python_packages") or []
    node_pkgs = tools.get("node_packages") or []
    if py_pkgs or node_pkgs:
        lines = []
        if py_pkgs:
            lines.append("  python_packages: " + ", ".join(py_pkgs))
        if node_pkgs:
            lines.append("  node_packages:   " + ", ".join(node_pkgs))
        existing_tools_block = "\n".join(lines)
        existing_tools_block += ("\n  (Inherit these. Do NOT redeclare; only add NEW tools "
                                 "this goal genuinely needs.)")
    else:
        existing_tools_block = "  (none yet — this is the first plan for the project)"

    project_description = project.get("description_md") or "(no description)"
    goal_description = goal.get("description_md", "(no description)")
    prompt = _PROMPT_TEMPLATE.format(
        project_name=project.get("name", "(unnamed project)"),
        project_slug=project.get("slug", ""),
        project_description=project_description,
        project_context_indented=indented,
        goal_title=goal.get("title", "(no title)"),
        goal_description=goal_description,
        http_ports_block=http_ports_block,
        existing_tools_block=existing_tools_block,
    )
    sections = {
        "project_description": len(project_description),
        "project_context": len(indented),
        "goal_description": len(goal_description),
        "http_ports_block": len(http_ports_block),
        "existing_tools_block": len(existing_tools_block),
        "static_template": len(_PROMPT_TEMPLATE),  # rough: includes placeholders
    }
    return prompt, sections


_ALLOWED_PLANNER_TASK_TYPES = {"implement", "review"}
_ALLOWED_PRIORITIES = {"low", "normal", "high", "critical"}
_ALLOWED_KIND_HINTS = {"web", "test", "algo", "refactor", "data", "other"}


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
            t.setdefault("kind_hint", "other")
            if t["kind_hint"] not in _ALLOWED_KIND_HINTS:
                t["kind_hint"] = "other"
            # Stash the hint in payload so it survives instantiation.
            payload = t.get("payload") or {}
            payload["kind_hint"] = t["kind_hint"]
            t["payload"] = payload
            t.setdefault("description_md", "")
            t.setdefault("acceptance_criteria", [])
            t["acceptance_criteria"] = sanitize_acceptance_criteria(
                t["acceptance_criteria"])
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
            goal = data["outcome"]
        except Exception as e:
            await hub.task_event(task_id, "task.warning",
                                 {"message": f"could not fetch goal: {e}"})

    prompt, sections = _render_prompt(project, goal)
    await emit_prompt_metrics(hub, task_id, "planner", prompt, sections)

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
    tools_required = parsed.get("tools_required") or {}
    # Normalise: only keep the keys we understand, only string entries, deduped.
    def _clean_pkg_list(raw) -> list[str]:
        if not isinstance(raw, list):
            return []
        seen: set[str] = set()
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, str):
                s = entry.strip()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
        return out
    tools_clean = {
        "python_packages": _clean_pkg_list(tools_required.get("python_packages")),
        "node_packages":   _clean_pkg_list(tools_required.get("node_packages")),
    }

    await hub.task_result(task_id, {
        "outcome": "success",
        "result": {
            "plan_md": plan_md,
            "tasks": tasks_out,
            "questions": questions,
            "tools_required": tools_clean,
        },
    })
