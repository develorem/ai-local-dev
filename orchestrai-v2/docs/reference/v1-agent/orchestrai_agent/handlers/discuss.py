"""Discuss handler.

Reads the discussion thread, calls the LLM to produce an agent reply (+ optional
proposed actions), POSTs the reply to the Hub, marks the task done.
"""

import logging
import time
from pathlib import Path
from typing import Any

import httpx

from orchestrai_agent.config import config
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.response_parser import extract_json

log = logging.getLogger("orchestrai-agent.discuss")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_TPL = (PROMPTS_DIR / "discusser.md").read_text(encoding="utf-8")


def _indent(text: str, prefix: str = "    ") -> str:
    text = text or ""
    return "\n".join(prefix + line for line in text.splitlines()) or (prefix + "(empty)")


def _render(project: dict, discussion: dict, messages: list, linked_summary: str) -> str:
    msg_lines = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content_md", "")
        msg_lines.append(f"[{role}] {content}")
    return _TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_description=project.get("description_md") or "(no description)",
        project_context_indented=_indent(project.get("context_md") or "(no context)"),
        discussion_title=discussion.get("title", "(no title)"),
        linked_summary=linked_summary or "(no linked entity)",
        messages_rendered="\n\n".join(msg_lines) or "(no messages yet)",
    )


async def handle_discuss(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    project = envelope.get("project") or {}
    task_id = task["id"]
    payload = task.get("payload") or {}
    discussion_id = payload.get("discussion_id")
    if not discussion_id:
        await hub.task_result(task_id, {
            "outcome": "failed",
            "result": {"error": "no discussion_id in task payload"},
            "notes_md": "discuss task missing discussion_id",
        })
        return

    # Fetch discussion + messages
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{hub.base}/api/discussions/{discussion_id}",
                                 headers={"Authorization": f"Bearer {hub.lease_token}"})
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"could not fetch discussion: {e}"},
            "notes_md": f"fetch_discussion_failed: {e}",
        })
        return

    discussion = data.get("discussion") or {}
    messages = data.get("messages") or []

    # Linked summary
    linked = "(general)"
    if discussion.get("task_id"):
        linked = f"task {discussion['task_id']}"
    elif discussion.get("outcome_id"):
        linked = f"goal {discussion['outcome_id']}"

    prompt = _render(project, discussion, messages, linked)

    await hub.task_event(task_id, "llm.call.started", {
        "mode": "discusser",
        "prompt_chars": len(prompt),
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
                "num_predict": 1024,
            },
        )
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"ollama call failed: {e}"},
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
    message_md: str = ""
    proposed: list = []
    if isinstance(parsed, dict):
        message_md = parsed.get("message_md") or ""
        proposed = parsed.get("proposed_actions") or []
    if not message_md:
        # Be lenient: fall back to raw response
        message_md = response.strip()[:5000] or "(agent produced no reply)"

    # POST the agent's reply (and any proposed actions) to the Hub
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{hub.base}/api/discussions/{discussion_id}/agent-message",
                json={"content_md": message_md, "proposed_actions": proposed},
                headers={"Authorization": f"Bearer {hub.lease_token}"},
            )
            r.raise_for_status()
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"could not post reply: {e}"},
            "notes_md": f"post_reply_failed: {e}",
        })
        return

    await hub.task_result(task_id, {
        "outcome": "success",
        "result": {
            "message_chars": len(message_md),
            "proposed_actions_count": len(proposed),
        },
    })
