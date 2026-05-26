"""Execute model-produced Python code against hidden tests in an isolated subprocess.

Safety notes:
    - Always run in a subprocess so an infinite loop or sys.exit can't kill the harness.
    - Hard timeout.
    - We do NOT sandbox filesystem/network — these are user-trusted local benchmarks.
"""

import subprocess
import sys
import tempfile
import os
import time


def execute(model_code: str, tests_code: str, timeout_sec: float = 10.0) -> dict:
    """Run `model_code` followed by `tests_code` in a subprocess.

    Returns {pass, stderr, stdout, wall_sec, timed_out, exit_code}.
    Pass is True iff exit_code == 0 and no timeout.
    """
    combined = f"{model_code}\n\n# ---- tests ----\n{tests_code}\n"

    fd, path = tempfile.mkstemp(suffix=".py", prefix="qual_")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(combined)

        start = time.perf_counter()
        try:
            proc = subprocess.run(
                [sys.executable, path],
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            wall = time.perf_counter() - start
            return {
                "pass": proc.returncode == 0,
                "exit_code": proc.returncode,
                "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-2000:],
                "wall_sec": wall,
                "timed_out": False,
            }
        except subprocess.TimeoutExpired as e:
            return {
                "pass": False,
                "exit_code": None,
                "stdout": (e.stdout or b"").decode("utf-8", "replace")[-2000:] if e.stdout else "",
                "stderr": (e.stderr or b"").decode("utf-8", "replace")[-2000:] if e.stderr else "",
                "wall_sec": time.perf_counter() - start,
                "timed_out": True,
            }
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
