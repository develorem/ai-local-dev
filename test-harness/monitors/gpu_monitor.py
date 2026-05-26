"""Background nvidia-smi sampler.

Polls nvidia-smi at a fixed interval and stores util / VRAM / temp samples.
"""

import subprocess
import threading
import time

_QUERY = "utilization.gpu,memory.used,memory.total,temperature.gpu"


class GpuMonitor:
    def __init__(self, interval_sec=0.25):
        self.interval = interval_sec
        self.samples = []
        self._stop = threading.Event()
        self._thread = None

    def _sample_once(self):
        try:
            out = subprocess.check_output(
                [
                    "nvidia-smi",
                    f"--query-gpu={_QUERY}",
                    "--format=csv,noheader,nounits",
                ],
                text=True,
                stderr=subprocess.STDOUT,
                timeout=5,
            )
            line = out.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            return {
                "ts": time.time(),
                "util_gpu_pct": float(parts[0]),
                "vram_used_mb": float(parts[1]),
                "vram_total_mb": float(parts[2]),
                "temp_c": float(parts[3]),
            }
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
        valid = [s for s in self.samples if "util_gpu_pct" in s]
        if not valid:
            return {"samples": 0}
        utils = [s["util_gpu_pct"] for s in valid]
        vram = [s["vram_used_mb"] for s in valid]
        temps = [s["temp_c"] for s in valid]
        return {
            "samples": len(valid),
            "util_gpu_pct_avg": sum(utils) / len(utils),
            "util_gpu_pct_max": max(utils),
            "vram_used_mb_avg": sum(vram) / len(vram),
            "vram_used_mb_max": max(vram),
            "vram_total_mb": valid[-1]["vram_total_mb"],
            "temp_c_max": max(temps),
        }
