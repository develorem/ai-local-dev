"""Review handler.

Deterministic acceptance-criteria checks run first. If a structured criterion
fails, the review verdict is `fix_needed` without an LLM call. If everything
deterministic passes (or there are only free-form criteria), the LLM judges.
"""

import logging
import time
from pathlib import Path
from typing import Any

from orchestrai_agent.config import config
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.response_parser import extract_json
from orchestrai_agent.subprocess_util import run as run_subproc
from orchestrai_agent.workspace import ensure_workspace

log = logging.getLogger("orchestrai-agent.review")

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
_REVIEW_TPL = (PROMPTS_DIR / "reviewer.md").read_text(encoding="utf-8")


def _indent(text: str, prefix: str = "    ") -> str:
    text = text or ""
    return "\n".join(prefix + line for line in text.splitlines()) or (prefix + "(empty)")


def _format_criteria(criteria: list) -> str:
    if not criteria:
        return "    (none)"
    out = []
    for c in criteria:
        out.append(f"  - {c}" if isinstance(c, str) else f"  - {c}")
    return "\n".join(out)


async def _check_structured(criteria: list, workspace: Path) -> tuple[list[dict], list[str]]:
    """Run structured acceptance criteria deterministically.

    Returns (results, free_form_criteria_for_llm).
    Each structured result: {kind, ok, detail}.
    """
    results: list[dict] = []
    free_form: list[str] = []

    for c in criteria or []:
        if isinstance(c, str):
            free_form.append(c)
            continue
        if not isinstance(c, dict):
            continue
        kind = c.get("kind")
        if kind == "file_exists":
            path = c.get("path", "")
            ok = (workspace / path).is_file() if path else False
            results.append({"kind": kind, "path": path, "ok": ok})
        elif kind == "test":
            cmd = c.get("cmd")
            expect_exit = int(c.get("expect_exit", 0))
            if not cmd:
                results.append({"kind": kind, "ok": False, "detail": "missing cmd"})
                continue
            res = await run_subproc(cmd, cwd=str(workspace), timeout_sec=120)
            ok = res.exit_code == expect_exit and not res.timed_out
            results.append({
                "kind": kind, "cmd": cmd, "ok": ok,
                "exit": res.exit_code, "expect_exit": expect_exit,
                "stdout_tail": res.stdout[-500:], "stderr_tail": res.stderr[-500:],
            })
        else:
            # Unknown structured kind — defer to LLM as if it were free-form
            free_form.append(str(c))
    return results, free_form


async def handle_review(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    project = envelope.get("project") or {}
    project_slug = project.get("slug") or "default"
    task_id = task["id"]

    workspace = await ensure_workspace(project_slug)

    # 1. Deterministic structured checks
    criteria = task.get("acceptance_criteria") or []
    structured_results, free_form = await _check_structured(criteria, workspace)
    deterministic_passed = all(r.get("ok") for r in structured_results)

    await hub.task_event(task_id, "review.deterministic_checks", {
        "passed": deterministic_passed,
        "results": structured_results,
    })

    # If structured checks fail, short-circuit
    if structured_results and not deterministic_passed:
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"structured_results": structured_results,
                       "free_form": free_form},
            "notes_md": "Deterministic acceptance criteria failed:\n" +
                        "\n".join(f"- {r}" for r in structured_results if not r.get("ok")),
        })
        return

    # 2. If only structured (and they all passed), skip LLM
    if not free_form:
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"structured_results": structured_results,
                       "llm_judged": False},
        })
        return

    # 3. LLM judges the free-form criteria
    # Show the most recent diff produced for this task's predecessor implement step,
    # if available, by pulling the most recent commit's diff.
    diff_res = await run_subproc(
        "git show -1 --format= --stat=200 HEAD",
        cwd=str(workspace),
        timeout_sec=10,
    )
    last_commit = diff_res.stdout.strip() or "(no commits)"

    det_summary = "\n".join(
        f"  ✓ {r.get('kind')} {r.get('cmd') or r.get('path') or ''}"
        for r in structured_results if r.get("ok")
    ) or "  (none)"

    prompt = _REVIEW_TPL.format(
        project_name=project.get("name", "(unnamed)"),
        project_context_indented=_indent(project.get("context_md") or "(none)"),
        task_title=task.get("title", "(no title)"),
        task_description=task.get("description_md") or "(no description)",
        acceptance_criteria_indented=_format_criteria(free_form),
        deterministic_summary=det_summary,
        diff=last_commit[:6000] or "(no diff available)",
        command_outputs="(no per-command outputs available — covered by deterministic checks above)",
    )

    await hub.task_event(task_id, "llm.call.started", {
        "mode": "reviewer",
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
            "result": {"error": f"reviewer ollama call failed: {e}"},
            "notes_md": f"Reviewer ollama call failed: {e}",
        })
        return

    wall = time.perf_counter() - started
    response = raw.get("response", "")
    parsed = extract_json(response)

    eval_count = raw.get("eval_count") or 0
    eval_duration = raw.get("eval_duration") or 0
    tps = (eval_count / (eval_duration / 1e9)) if eval_count and eval_duration else 0.0
    await hub.task_event(task_id, "llm.call.completed", {
        "wall_sec": round(wall, 2), "gen_tps": round(tps, 1),
        "completion_tokens": eval_count,
    })

    if not isinstance(parsed, dict) or "verdict" not in parsed:
        # Failed to parse — assume pass (be lenient on reviewer)
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"structured_results": structured_results,
                       "reviewer_unparseable": True,
                       "raw_excerpt": response[:1500]},
        })
        return

    verdict = parsed.get("verdict", "pass")
    if verdict == "pass":
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"structured_results": structured_results,
                       "reviewer": parsed},
        })
    elif verdict == "fix_needed":
        await hub.task_result(task_id, {
            "outcome": "fix_needed",
            "result": {"structured_results": structured_results,
                       "reviewer": parsed},
            "notes_md": "Reviewer requested fixes:\n" +
                        "\n".join(f"- {fr}" for fr in (parsed.get("fix_recommendations") or [])),
        })
    else:
        await hub.task_result(task_id, {
            "outcome": "needs_human",
            "result": {"structured_results": structured_results,
                       "reviewer": parsed},
            "questions": parsed.get("questions") or [{
                "kind": "clarification",
                "prompt_md": parsed.get("rationale_md") or
                             "Reviewer escalated to human; rationale not provided.",
            }],
        })
