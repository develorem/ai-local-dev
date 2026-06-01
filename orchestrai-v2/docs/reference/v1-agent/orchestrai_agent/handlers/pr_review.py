"""review_pr handler.

Reads PR details from task.payload (url, optional pre-fetched diff). If diff
isn't provided, fetches it via `gh pr diff <url>` (requires GITHUB_TOKEN in
the secrets vault and the task to declare it).

Submits review via `gh pr review` (approve / request-changes / comment).
"""

import logging
import os
import time
from pathlib import Path

from orchestrai_agent.config import config
from orchestrai_agent.handlers.implement import _gen_stats  # reuse stats helper
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.response_parser import extract_json
from orchestrai_agent.subprocess_util import run as run_subproc

log = logging.getLogger("orchestrai-agent.pr_review")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_TPL = (PROMPTS_DIR / "pr_reviewer.md").read_text(encoding="utf-8")


def _indent(text: str, prefix: str = "    ") -> str:
    text = text or ""
    return "\n".join(prefix + line for line in text.splitlines()) or (prefix + "(empty)")


async def _fetch_github_token(hub: HubClient) -> str | None:
    """Try to fetch GITHUB_TOKEN from the vault. Returns the value or None."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{hub.base}/api/secrets/GITHUB_TOKEN/value",
                headers={"Authorization": f"Bearer {hub.lease_token}"},
            )
            if r.status_code == 200:
                return r.json().get("value")
    except Exception:
        return None
    return None


async def _gh(args: list[str], token: str | None, cwd: str = "/tmp") -> tuple[int, str, str]:
    env = os.environ.copy()
    if token:
        env["GITHUB_TOKEN"] = token
        env["GH_TOKEN"] = token
    res = await run_subproc(["gh"] + args, cwd=cwd, env=env, timeout_sec=60)
    return res.exit_code, res.stdout, res.stderr


async def handle_review_pr(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    project = envelope.get("project") or {}
    task_id = task["id"]
    payload = task.get("payload") or {}

    pr_url = payload.get("pr_url") or payload.get("url")
    if not pr_url:
        await hub.task_result(task_id, {
            "outcome": "failed",
            "result": {"error": "no pr_url in payload"},
            "notes_md": "review_pr task missing pr_url in payload",
        })
        return

    # Get token
    token = await _fetch_github_token(hub)
    if not token:
        await hub.task_result(task_id, {
            "outcome": "needs_human",
            "result": {"reason": "github_token_required"},
            "questions": [{
                "kind": "clarification",
                "prompt_md": ("This review_pr task needs a `GITHUB_TOKEN` secret to fetch "
                              "and post on the PR. Add one in the Vault and ensure the "
                              "task's payload includes `secrets_needed: [\"GITHUB_TOKEN\"]`."),
            }],
            "notes_md": "GITHUB_TOKEN unavailable",
        })
        return

    # Fetch PR metadata
    await hub.task_event(task_id, "pr.fetch.started", {"url": pr_url})
    rc, out, err = await _gh(["pr", "view", pr_url, "--json",
                              "title,body,headRefName,baseRefName,additions,deletions,files"],
                             token)
    if rc != 0:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"gh pr view failed: {err[-500:]}"},
            "notes_md": f"gh pr view failed:\n{err[-500:]}",
        })
        return
    import json as _json
    try:
        meta = _json.loads(out)
    except Exception:
        meta = {"title": "(unknown)", "body": ""}

    # Fetch the diff
    rc, diff, err = await _gh(["pr", "diff", pr_url], token)
    if rc != 0:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"error": f"gh pr diff failed: {err[-500:]}"},
            "notes_md": f"gh pr diff failed:\n{err[-500:]}",
        })
        return

    # Cap diff to keep within context
    MAX_DIFF_CHARS = 30_000
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + f"\n... ({len(diff) - MAX_DIFF_CHARS} chars truncated)\n"

    prompt = _TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_context_indented=_indent(project.get("context_md") or "(no context)"),
        pr_title=meta.get("title", "(no title)"),
        pr_description=meta.get("body") or "(no description)",
        head_branch=meta.get("headRefName", "?"),
        base_branch=meta.get("baseRefName", "?"),
        pr_url=pr_url,
        additions=meta.get("additions", 0),
        deletions=meta.get("deletions", 0),
        file_count=len(meta.get("files") or []),
        diff=diff,
    )

    await hub.task_event(task_id, "llm.call.started", {
        "mode": "pr_reviewer", "prompt_chars": len(prompt),
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
            "result": {"error": f"ollama failed: {e}"},
            "notes_md": f"ollama_failed: {e}",
        })
        return
    raw["_wall_sec"] = time.perf_counter() - started
    raw["_response_text"] = raw.get("response", "")
    await hub.task_event(task_id, "llm.call.completed", _gen_stats(raw))

    parsed = extract_json(raw["_response_text"])
    if not isinstance(parsed, dict) or "verdict" not in parsed:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"raw_excerpt": raw["_response_text"][:1500]},
            "notes_md": "PR-reviewer output failed validation",
        })
        return

    verdict = parsed.get("verdict")
    summary = parsed.get("summary_md") or ""
    generals = parsed.get("general_comments_md") or []
    full_body = summary + (("\n\n" + "\n\n".join(f"- {g}" for g in generals)) if generals else "")

    # Submit the review via gh
    review_flag = {
        "approve": "--approve",
        "request_changes": "--request-changes",
        "comment_only": "--comment",
    }.get(verdict, "--comment")

    rc, out, err = await _gh(["pr", "review", pr_url, review_flag, "--body", full_body], token)
    if rc != 0:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"verdict": verdict, "body": full_body,
                       "submit_error": err[-500:]},
            "notes_md": f"gh pr review failed:\n{err[-500:]}",
        })
        return

    await hub.task_result(task_id, {
        "outcome": "success",
        "result": {
            "verdict": verdict,
            "summary_md": summary,
            "body_posted": full_body,
            "url": pr_url,
        },
    })
