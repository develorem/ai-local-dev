"""Thin Ollama HTTP client for the benchmark harness.

Uses stdlib urllib so the harness has zero HTTP dependencies. Streaming is
disabled — we want one final JSON response containing the timing fields
(total_duration, load_duration, prompt_eval_*, eval_*).
"""

import json
import time
import urllib.request
import urllib.error

DEFAULT_HOST = "http://localhost:11434"


class OllamaClient:
    def __init__(self, host=DEFAULT_HOST, timeout_sec=900):
        self.host = host.rstrip("/")
        self.timeout = timeout_sec

    def generate(self, model, prompt, options=None, keep_alive="30m", num_predict=None):
        body = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": keep_alive,
        }
        opts = dict(options or {})
        if num_predict is not None:
            opts["num_predict"] = num_predict
        if opts:
            body["options"] = opts

        req = urllib.request.Request(
            f"{self.host}/api/generate",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        start = time.perf_counter()
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        payload["_wall_clock_sec"] = time.perf_counter() - start
        return payload

    def ping(self):
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as resp:
                return resp.status == 200
        except (urllib.error.URLError, TimeoutError):
            return False
