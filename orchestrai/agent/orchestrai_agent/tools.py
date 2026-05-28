"""Claim-time tool installation.

The Hub keeps a per-project list of declared tools (project.tools, populated
when a plan is approved). Before the implementer LLM runs, the agent diffs
this list against what's actually installed in the container and pip-installs
anything missing. This is the load-bearing piece of the tool registry — once
a project says it needs FastAPI, every implement task for that project can
trust FastAPI is importable.

We intentionally do NOT touch the implement task's verification commands here.
Tasks should never have to `pip install` in their verification step; that
keeps the prompt focused on the actual code change.
"""

import logging
import re
from typing import Optional

from orchestrai_agent.subprocess_util import run

log = logging.getLogger("orchestrai-agent.tools")

# Strip pip-style extras and version pins so we can compare against `pip freeze`.
# "uvicorn[standard]==0.48.0" → "uvicorn"
_EXTRA_OR_VERSION = re.compile(r"\s*(?:\[[^\]]*\])?\s*(?:[<>=!~].*)?$")


def _base_name(spec: str) -> str:
    return _EXTRA_OR_VERSION.sub("", (spec or "").strip()).lower().replace("_", "-")


async def _installed_python_packages() -> set[str]:
    """Return the set of base package names visible to the current Python."""
    res = await run(["pip", "freeze", "--disable-pip-version-check"],
                    cwd="/tmp", timeout_sec=20)
    if res.exit_code != 0:
        log.warning("pip freeze failed (%s): %s", res.exit_code, res.stderr[:300])
        return set()
    names: set[str] = set()
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Lines look like  "fastapi==0.136.3" or  "-e git+https://...#egg=foo"
        # or  "package @ file:///..."
        if line.startswith("-e ") or "@" in line.split("==", 1)[0]:
            # Best-effort: skip; freeze rarely produces these in our image.
            continue
        head = re.split(r"[=<>!~ ]", line, maxsplit=1)[0]
        if head:
            names.add(head.lower().replace("_", "-"))
    return names


async def ensure_project_tools(project: Optional[dict],
                               hub,
                               task_id: str) -> tuple[bool, Optional[str]]:
    """Install any project tools not already present. Returns (ok, error).

    `hub` is the HubClient (we only use it for task_event emission).
    On error, returns (False, "<human-readable description with stderr>").
    """
    if not project:
        return True, None
    tools = project.get("tools") or {}
    declared = list(tools.get("python_packages") or [])
    if not declared:
        return True, None

    already = await _installed_python_packages()
    missing: list[str] = []
    for spec in declared:
        base = _base_name(spec)
        if not base:
            continue
        if base not in already:
            missing.append(spec)

    if not missing:
        await hub.task_event(task_id, "tools.satisfied", {
            "declared": declared,
            "missing": [],
        })
        return True, None

    log.info("installing project tools: %s", missing)
    await hub.task_event(task_id, "tools.install_started", {
        "missing": missing,
    })
    res = await run(
        ["pip", "install", "--no-cache-dir", "--disable-pip-version-check", *missing],
        cwd="/tmp", timeout_sec=240,
    )
    if res.exit_code != 0:
        tail = (res.stderr or res.stdout or "").splitlines()[-30:]
        msg = "\n".join(tail)[-3500:]
        await hub.task_event(task_id, "tools.install_failed", {
            "missing": missing,
            "exit_code": res.exit_code,
            "stderr_tail": msg,
        })
        return False, (
            f"pip install failed for {missing} (exit={res.exit_code}). "
            f"Recent output:\n{msg}"
        )
    await hub.task_event(task_id, "tools.install_completed", {
        "installed": missing,
        "wall_sec": round(res.wall_sec, 2),
    })
    return True, None
