"""HTTP client for the Hub. One per agent process."""

from typing import Any, Optional

import httpx

from orchestrai_agent.config import config


class HubClient:
    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base = (base_url or config.HUB_URL).rstrip("/")
        self.agent_id: Optional[str] = None
        self.lease_token: Optional[str] = None
        self.heartbeat_interval_sec: int = config.HEARTBEAT_DEFAULT_SEC
        self.lease_timeout_sec: int = 30
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.lease_token:
            h["Authorization"] = f"Bearer {self.lease_token}"
        return h

    async def health(self) -> dict:
        r = await self._client.get(f"{self.base}/api/health")
        r.raise_for_status()
        return r.json()

    async def register(self) -> dict:
        # Advertise mapped HTTP ports as `port:<n>:http` capability strings.
        # No schema change required on the Hub side — the UI parses these out.
        port_caps = [f"port:{p}:http" for p in config.HTTP_PORTS]
        body = {
            "name": config.AGENT_NAME,
            "host": config.AGENT_HOST,
            "version": config.AGENT_VERSION,
            "capabilities": config.CAPABILITIES + port_caps,
            "http_ports": config.HTTP_PORTS,
        }
        # Registration is an operator action — present the operator token. After
        # this, calls authenticate with the returned per-agent lease token.
        reg_headers = {"Content-Type": "application/json"}
        if config.OPERATOR_TOKEN:
            reg_headers["Authorization"] = f"Bearer {config.OPERATOR_TOKEN}"
        r = await self._client.post(f"{self.base}/api/agents/register", json=body,
                                    headers=reg_headers)
        r.raise_for_status()
        data = r.json()
        self.agent_id = data["agent_id"]
        self.lease_token = data["lease_token"]
        self.heartbeat_interval_sec = data.get("heartbeat_interval_sec", 10)
        self.lease_timeout_sec = data.get("lease_timeout_sec", 30)
        return data

    async def heartbeat(self, current_task_id: Optional[str] = None) -> None:
        r = await self._client.post(
            f"{self.base}/api/agents/{self.agent_id}/heartbeat",
            json={"current_task_id": current_task_id},
            headers=self._headers(),
        )
        r.raise_for_status()

    async def claim(self) -> Optional[dict]:
        r = await self._client.post(
            f"{self.base}/api/agents/{self.agent_id}/claim",
            json={},
            headers=self._headers(),
        )
        r.raise_for_status()
        data = r.json()
        return data if data.get("task") else None

    async def release(self, release_task: bool = True) -> None:
        if not self.agent_id:
            return
        try:
            await self._client.post(
                f"{self.base}/api/agents/{self.agent_id}/release",
                json={"release_task": release_task},
                headers=self._headers(),
            )
        except Exception:
            pass

    async def task_event(self, task_id: str, kind: str, detail: dict) -> None:
        try:
            await self._client.post(
                f"{self.base}/api/tasks/{task_id}/events",
                json={"kind": kind, "detail": detail},
                headers=self._headers(),
            )
        except Exception:
            pass

    async def task_result(self, task_id: str, body: dict) -> dict:
        r = await self._client.post(
            f"{self.base}/api/tasks/{task_id}/result",
            json=body,
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()

    async def get_goal(self, goal_id: str) -> dict:
        r = await self._client.get(
            f"{self.base}/api/outcomes/{goal_id}",
            headers=self._headers(),
        )
        r.raise_for_status()
        return r.json()
