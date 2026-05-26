"""Background `ollama ps` sampler.

Output looks roughly like:
    NAME                ID              SIZE      PROCESSOR          CONTEXT    UNTIL
    qwen2.5-coder:14b   abc123          11 GB     12%/88% CPU/GPU    16384      29 minutes from now

We only need the first data row (the running model) and the CPU/GPU split.
"""

import re
import subprocess
import threading
import time

_SPLIT_RE = re.compile(r"(\d+)\s*%\s*/\s*(\d+)\s*%\s*(CPU/GPU|GPU/CPU)", re.IGNORECASE)
_SINGLE_RE = re.compile(r"(\d+)\s*%\s*(GPU|CPU)\b", re.IGNORECASE)
_SIZE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(GB|MB|KB|TB)\b", re.IGNORECASE)
_CTX_RE = re.compile(r"\b(\d{3,7})\b")


class OllamaMonitor:
    def __init__(self, interval_sec=1.0):
        self.interval = interval_sec
        self.samples = []
        self._stop = threading.Event()
        self._thread = None

    def _parse(self, output):
        lines = [l for l in output.strip().splitlines() if l.strip()]
        if len(lines) < 2:
            return {"raw": output.strip()}

        row = lines[1]
        cpu_pct = gpu_pct = None

        m = _SPLIT_RE.search(row)
        if m:
            a, b, order = int(m.group(1)), int(m.group(2)), m.group(3).upper()
            if order.startswith("CPU"):
                cpu_pct, gpu_pct = a, b
            else:
                gpu_pct, cpu_pct = a, b
        else:
            ms = _SINGLE_RE.search(row)
            if ms:
                pct, kind = int(ms.group(1)), ms.group(2).upper()
                if kind == "GPU":
                    gpu_pct, cpu_pct = pct, 100 - pct
                else:
                    cpu_pct, gpu_pct = pct, 100 - pct

        size = None
        sm = _SIZE_RE.search(row)
        if sm:
            size = f"{sm.group(1)} {sm.group(2).upper()}"

        context = None
        # CONTEXT column is the only bare integer in the row (sizes are paired with units;
        # percentages have % signs); the regex picks the first such integer.
        for token in row.split():
            if token.isdigit() and 256 <= int(token) <= 2_000_000:
                context = int(token)
                break

        return {
            "raw": row,
            "cpu_pct": cpu_pct,
            "gpu_pct": gpu_pct,
            "size": size,
            "context": context,
        }

    def _sample_once(self):
        try:
            out = subprocess.check_output(
                ["ollama", "ps"],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=5,
            )
            return {"ts": time.time(), **self._parse(out)}
        except Exception as e:
            return {"ts": time.time(), "error": str(e)}

    def _loop(self):
        while not self._stop.is_set():
            self.samples.append(self._sample_once())
            self._stop.wait(self.interval)

    def start(self):
        self.samples = []
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def summary(self):
        valid = [s for s in self.samples if s.get("cpu_pct") is not None]
        if not valid:
            return {"samples": 0}
        cpus = [s["cpu_pct"] for s in valid]
        gpus = [s["gpu_pct"] for s in valid]
        return {
            "samples": len(valid),
            "cpu_pct_avg": sum(cpus) / len(cpus),
            "gpu_pct_avg": sum(gpus) / len(gpus),
            "cpu_pct_last": cpus[-1],
            "gpu_pct_last": gpus[-1],
        }
