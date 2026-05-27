"""Ollama HTTP client. Adapted from test-harness/ollama_client.py.

Async variant for the Agent (everything else in the Agent is async).
"""

from typing import Any, Optional

import httpx

from orchestrai_agent.config import config


class OllamaClient:
    def __init__(self, base_url: Optional[str] = None, timeout_sec: int = 0) -> None:
        self.base = (base_url or config.OLLAMA_URL).rstrip("/")
        self.timeout = httpx.Timeout(timeout_sec or config.LLM_TIMEOUT_SEC)
        self._client = httpx.AsyncClient(timeout=self.timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def generate(
        self,
        model: str,
        prompt: str,
        *,
        options: Optional[dict] = None,
        keep_alive: str = "30m",
        num_predict: Optional[int] = None,
    ) -> dict:
        body: dict[str, Any] = {
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
        r = await self._client.post(f"{self.base}/api/generate", json=body)
        r.raise_for_status()
        return r.json()
