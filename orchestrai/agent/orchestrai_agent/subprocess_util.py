"""Subprocess helper: timeout, capture, single place to log all shell-outs."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("orchestrai-agent.subproc")

_MAX_CAPTURE_BYTES = 64 * 1024  # cap stdout/stderr at 64KB each


@dataclass
class ProcResult:
    cmd: list[str]
    cwd: str
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool
    wall_sec: float


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
    )

    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=input_data),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        try:
            stdout, stderr = await proc.communicate()
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
