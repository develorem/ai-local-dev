"""Emit per-section character counts for LLM prompts.

Every prompt the agent builds is the sum of:
  - task-specific data (project / goal / criteria / file contents / retry info)
  - reusable guidance ("how to write tests", "how to choose ports", etc.)

The goal of this module is to make the breakdown VISIBLE in the event stream
so we can spot when guidance starts crowding out the real signal in the
fixed 16K-token context. One `prompt.metrics` event per LLM call.

Char counts are reported, not tokens — close enough for ranking (qwen
tokenises at ~3.5 chars/token average) and avoids loading a tokenizer.
"""

import logging
from typing import Optional

log = logging.getLogger("orchestrai-agent.prompt_metrics")


async def emit(hub, task_id: str, mode: str, prompt: str,
               sections: dict[str, int],
               kind_hint: Optional[str] = None) -> None:
    """Fire-and-forget event with the size breakdown of `prompt`.

    `sections` is {label: char_count}. Anything in the rendered prompt not
    captured by a section gets attributed to a synthetic "_other" entry so
    the numbers add up.
    """
    total = len(prompt)
    accounted = sum(int(v or 0) for v in sections.values())
    other = max(0, total - accounted)
    payload = {
        "mode": mode,
        "total_chars": total,
        "sections": {**sections, "_other": other},
    }
    if kind_hint:
        payload["kind_hint"] = kind_hint
    try:
        await hub.task_event(task_id, "prompt.metrics", payload)
    except Exception as e:
        log.debug("prompt.metrics emit failed: %s", e)
