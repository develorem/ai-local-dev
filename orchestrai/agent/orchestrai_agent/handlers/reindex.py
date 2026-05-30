"""Reindex handler.

Regenerates the one-line `purpose` (the routing hint) for project documents
whose content changed. The mechanical part of the index (title + headings) is
maintained synchronously by the Hub on save; this handler only writes the
judgment line, one tiny bounded call per document — a good fit for a small model.

Targets arrive in the claim envelope as `reindex_targets` (the Hub already
filtered to stale docs and stamped each with the signature its purpose will be
keyed to). We echo that signature back so the Hub can mark the doc fresh only
against the exact content the purpose was written for.
"""

import logging
import time
from pathlib import Path

from orchestrai_agent.config import config
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.response_parser import extract_json

log = logging.getLogger("orchestrai-agent.reindex")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_TPL = (PROMPTS_DIR / "reindexer.md").read_text(encoding="utf-8")

# Bound the work per task so one reindex can't monopolise the worker. Anything
# left stale is re-queued by the Hub when it applies this task's result.
_MAX_DOCS_PER_TASK = 25
_PURPOSE_MAX = 200


def _render(target: dict) -> str:
    headings = target.get("headings") or []
    headings_str = ", ".join(headings) if headings else "(none)"
    excerpt = (target.get("content_excerpt") or "").strip() or "(empty document)"
    excerpt = "\n".join("    " + ln for ln in excerpt.splitlines())
    return _TPL.format(
        doc_title=target.get("title") or "(untitled)",
        doc_headings=headings_str,
        doc_excerpt=excerpt,
    )


async def handle_reindex(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    task_id = task["id"]
    targets = envelope.get("reindex_targets") or []

    if not targets:
        # Nothing stale (another reindex already cleaned up, or docs deleted).
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"purposes": [], "note": "no stale documents"},
        })
        return

    purposes: list[dict] = []
    truncated = len(targets) > _MAX_DOCS_PER_TASK
    for target in targets[:_MAX_DOCS_PER_TASK]:
        prompt = _render(target)
        started = time.perf_counter()
        try:
            raw = await ollama.generate(
                model=config.DEFAULT_MODEL,
                prompt=prompt,
                options={"num_ctx": config.DEFAULT_NUM_CTX, "temperature": 0,
                         "seed": 42, "num_predict": 128},
            )
        except Exception as e:
            log.warning("reindex generate failed for %s: %s",
                        target.get("document_id"), e)
            continue
        wall = time.perf_counter() - started
        parsed = extract_json(raw.get("response", ""))
        purpose = ""
        if isinstance(parsed, dict):
            purpose = (parsed.get("purpose") or "").strip()
        if not purpose:
            # Fall back to a mechanical hint rather than leaving it blank forever.
            heads = ", ".join(target.get("headings") or [])
            purpose = (target.get("title") or "Document") + (f" — covers: {heads}" if heads else "")
        purpose = purpose.replace("\n", " ")[:_PURPOSE_MAX]
        purposes.append({
            "document_id": target.get("document_id"),
            "signature": target.get("signature"),
            "purpose": purpose,
        })
        await hub.task_event(task_id, "reindex.document_indexed", {
            "document_id": target.get("document_id"),
            "purpose": purpose,
            "wall_sec": round(wall, 2),
        })

    await hub.task_result(task_id, {
        "outcome": "success",
        "result": {"purposes": purposes, "indexed": len(purposes),
                   "truncated": truncated},
        "notes_md": (f"Reindexed {len(purposes)} document(s)."
                     + (" More remain; a follow-up reindex was queued." if truncated else "")),
    })
