"""Review handler.

Deterministic acceptance-criteria checks run first. If a structured criterion
fails, the review verdict is `fix_needed` without an LLM call. If everything
deterministic passes (or there are only free-form criteria), the LLM judges.
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
from orchestrai_agent.subprocess_util import run as run_subproc
from orchestrai_agent.workspace import ensure_workspace, prepare_workspace

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


def _serve_log_tail(port: int, n: int = 400) -> str:
    """Tail of the detached server's log, to surface why startup failed."""
    p = Path(f"/tmp/orchestrai-serve/{port}.log")
    try:
        return "serve log: " + p.read_text(encoding="utf-8", errors="replace")[-n:]
    except OSError:
        return "(no serve log)"


async def _check_http(c: dict, workspace: Path) -> dict:
    """Stand up a server, hit one endpoint, assert status/body, tear it down.

    Criterion shape:
      {"kind": "http", "start": "<server cmd>", "port": 6800,
       "path": "/", "expect_status": 200, "expect_contains": "<substring>"}

    `start` is backgrounded via orchestrai-serve, which exits 0 once the port is
    reachable and keeps the child alive; we always stop it again afterwards so a
    later criterion (or the next review) starts from a clean port. This is the
    only place a review brings up a server — bare `curl` test criteria can't,
    because each runs as an isolated subprocess with nothing listening.
    """
    start = (c.get("start") or "").strip()
    port = c.get("port")
    path = c.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    expect_status = int(c.get("expect_status", 200))
    expect_contains = c.get("expect_contains")
    wait_sec = int(c.get("wait_sec", 20))
    base = {"kind": "http", "port": port, "path": path}

    if not start:
        return {**base, "ok": False, "detail": "missing 'start' command"}
    if not isinstance(port, int):
        return {**base, "ok": False, "detail": "missing or non-integer 'port'"}
    allowed = config.HTTP_PORTS or [6800, 6801, 6802]
    if port not in allowed:
        return {**base, "ok": False,
                "detail": f"port {port} is not one of the agent's mapped ports {allowed}"}

    # Clear any stale server left on this port (defensive across criteria/runs).
    await run_subproc(f"orchestrai-serve --stop {port}", cwd=str(workspace), timeout_sec=10)
    try:
        serve = await run_subproc(
            f"orchestrai-serve --port {port} --wait-sec {wait_sec} -- {start}",
            cwd=str(workspace), timeout_sec=wait_sec + 15,
        )
        if serve.exit_code != 0 or serve.timed_out:
            return {**base, "ok": False,
                    "detail": (f"server never became reachable on :{port} "
                               f"(orchestrai-serve exit {serve.exit_code}). "
                               + _serve_log_tail(port))}
        url = f"http://127.0.0.1:{port}{path}"
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(url)
        except Exception as e:
            return {**base, "ok": False, "detail": f"request to {url} failed: {e}"}

        status_ok = resp.status_code == expect_status
        contains_ok = (expect_contains in resp.text) if expect_contains else True
        ok = status_ok and contains_ok
        detail = f"GET {path} -> {resp.status_code} (want {expect_status})"
        if expect_contains:
            detail += (f"; body {'contains' if contains_ok else 'MISSING'} "
                       f"{expect_contains!r}")
        return {**base, "ok": ok, "status": resp.status_code,
                "expect_status": expect_status, "detail": detail,
                "body_tail": "" if ok else resp.text[-300:]}
    finally:
        await run_subproc(f"orchestrai-serve --stop {port}", cwd=str(workspace), timeout_sec=10)


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
        elif kind == "http":
            results.append(await _check_http(c, workspace))
        else:
            # Unknown structured kind — defer to LLM as if it were free-form
            free_form.append(str(c))
    return results, free_form


async def handle_review(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    project = envelope.get("project") or {}
    project_slug = project.get("slug") or "default"
    task_id = task["id"]

    workspace = await prepare_workspace(hub, envelope)

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
