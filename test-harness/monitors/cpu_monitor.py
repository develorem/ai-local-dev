"""Background CPU + RAM sampler via psutil.

If psutil isn't installed, the monitor reports unavailable but won't crash the
benchmark — GPU and ollama-ps samples are still captured.
"""

import threading
import time

try:
    import psutil

    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


class CpuMonitor:
    def __init__(self, interval_sec=0.5):
        self.interval = interval_sec
        self.samples = []
        self._stop = threading.Event()
        self._thread = None

    def _loop(self):
        psutil.cpu_percent(interval=None)
        while not self._stop.is_set():
            self.samples.append(
                {
                    "ts": time.time(),
                    "cpu_pct": psutil.cpu_percent(interval=None),
                    "ram_used_mb": psutil.virtual_memory().used / (1024 * 1024),
                }
            )
            self._stop.wait(self.interval)

    def start(self):
        self.samples = []
        self._stop.clear()
        if not HAS_PSUTIL:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def summary(self):
        if not self.samples:
            return {"samples": 0, "available": HAS_PSUTIL}
        cpus = [s["cpu_pct"] for s in self.samples]
        rams = [s["ram_used_mb"] for s in self.samples]
        return {
            "samples": len(self.samples),
            "cpu_pct_avg": sum(cpus) / len(cpus),
            "cpu_pct_max": max(cpus),
            "ram_used_mb_max": max(rams),
            "available": True,
        }
