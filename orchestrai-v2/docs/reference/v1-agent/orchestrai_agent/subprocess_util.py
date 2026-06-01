"""Subprocess helper: timeout, capture, single place to log all shell-outs.

Critical detail: spawns each subprocess in its own process group so we can
SIGKILL the WHOLE group on timeout. Without this, a command like
`uvicorn main:app --reload` (which forks a worker process) will leave its
child alive after we kill the parent; the child keeps stdout/stderr pipes
open, and `proc.communicate()` then blocks forever waiting for EOF. That
specific bug previously wedged the agent's main loop for tens of minutes.
"""

import asyncio
import logging
import os
import signal
import sys
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("orchestrai-agent.subproc")

_MAX_CAPTURE_BYTES = 64 * 1024  # cap stdout/stderr at 64KB each
_POST_KILL_DRAIN_SEC = 3.0       # hard ceiling on the post-kill communicate()


@dataclass
class ProcResult:
    cmd: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    wall_sec: float


def _kill_process_group(proc) -> None:
    """SIGKILL the entire process group of `proc`. Tolerates already-dead
    processes and the (rare) Windows host where we can't get a pgid."""
    if sys.platform == "win32":
        # Best-effort: just kill the parent. Windows doesn't have POSIX pgids.
        try:
            proc.kill()
        except Exception:
            pass
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


async def run(
    cmd: list[str] | str,
    *,
    cwd: str,
    env: Optional[dict] = None,
    timeout_sec: float = 300.0,
    input_data: Optional[bytes] = None,
) -> ProcResult:
    """Run a subprocess with hard timeout. stdout/stderr captured + size-capped.

    If `cmd` is a string, runs via shell (`bash -lc <cmd>`); for a list it runs
    directly. Inheriting env defaults; pass `env=` to override.
    """
    import time

    if isinstance(cmd, str):
        argv = ["bash", "-lc", cmd]
    else:
        argv = list(cmd)

    started = time.perf_counter()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.PIPE if input_data else asyncio.subprocess.DEVNULL,
        cwd=cwd,
        env=env,
        # Put the child in its own process group so we can kill grandchildren too.
        start_new_session=(sys.platform != "win32"),
    )

    timed_out = False
    stdout = b""
    stderr = b""
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_data),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        timed_out = True
        _kill_process_group(proc)
        # Bound the post-kill drain so we don't hang even if a grandchild
        # somehow survived (e.g. detached double-fork). If it does, we lose
        # the captured bytes but the agent's main loop keeps moving.
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_POST_KILL_DRAIN_SEC,
            )
        except asyncio.TimeoutError:
            log.warning("subprocess pipes still held after kill; abandoning output")
            stdout, stderr = b"", b""
        except Exception:
            stdout, stderr = b"", b""

    wall = time.perf_counter() - started

    return ProcResult(
        cmd=argv,
        cwd=cwd,
        exit_code=proc.returncode if proc.returncode is not None else -1,
        stdout=(stdout or b"").decode("utf-8", "replace")[:_MAX_CAPTURE_BYTES],
        stderr=(stderr or b"").decode("utf-8", "replace")[:_MAX_CAPTURE_BYTES],
        timed_out=timed_out,
        wall_sec=wall,
    )
