"""Main agent loop: register → claim → handle → repeat.

A background heartbeat task extends the Hub-side lease while we hold a task.
"""

import asyncio
import logging
from typing import Optional

from orchestrai_agent.config import config
from orchestrai_agent.handlers import handler_for
from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient

log = logging.getLogger("orchestrai-agent")


class AgentLoop:
    def __init__(self) -> None:
        self.hub = HubClient()
        self.ollama = OllamaClient()
        self._stop = asyncio.Event()
        self._current_task_id: Optional[str] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    async def _wait_for_hub(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                h = await self.hub.health()
                if h.get("status") in ("ok", "degraded"):
                    log.info("hub reachable: %s (v%s)", h.get("status"), h.get("version"))
                    return
            except Exception as e:
                log.info("hub not ready (%s) — retrying in %ds", e, int(backoff))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 15.0)

    async def _heartbeat_loop(self) -> None:
        interval = max(1, self.hub.heartbeat_interval_sec)
        try:
            while not self._stop.is_set():
                try:
                    await self.hub.heartbeat(self._current_task_id)
                except Exception as e:
                    log.warning("heartbeat failed: %s", e)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass

    async def run(self) -> None:
        await self._wait_for_hub()
        info = await self.hub.register()
        log.info("registered as agent_id=%s (heartbeat=%ds, lease=%ds)",
                 info["agent_id"], info["heartbeat_interval_sec"],
                 info["lease_timeout_sec"])

        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            while not self._stop.is_set():
                try:
                    envelope = await self.hub.claim()
                except Exception as e:
                    log.warning("claim failed: %s — backing off", e)
                    await asyncio.sleep(min(15.0, config.POLL_IDLE_SEC * 3))
                    continue

                if envelope is None:
                    await asyncio.sleep(config.POLL_IDLE_SEC)
                    continue

                task = envelope["task"]
                ttype = task["type"]
                tid = task["id"]
                self._current_task_id = tid

                log.info("claimed task %s (type=%s) — %s", tid, ttype, task["title"])

                handler = handler_for(ttype)
                if handler is None:
                    log.warning("no handler for task type '%s' — submitting needs_human", ttype)
                    try:
                        await self.hub.task_result(tid, {
                            "outcome": "needs_human",
                            "result": {},
                            "notes_md": f"Agent has no handler for task type '{ttype}'.",
                            "questions": [{
                                "kind": "clarification",
                                "prompt_md": (f"This agent doesn't have a handler for task "
                                              f"type '{ttype}'. Either implement one or "
                                              "manually transition the task."),
                            }],
                        })
                    except Exception as e:
                        log.error("failed to submit needs_human: %s", e)
                    self._current_task_id = None
                    continue

                try:
                    await handler(self.hub, self.ollama, envelope)
                except Exception as e:
                    log.exception("handler crashed: %s", e)
                    try:
                        await self.hub.task_result(tid, {
                            "outcome": "fix_needed",
                            "result": {"error": str(e)},
                            "notes_md": f"Handler raised: {type(e).__name__}: {e}",
                        })
                    except Exception:
                        pass

                self._current_task_id = None
                # Loop continues — next claim
        finally:
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
            await self.hub.release(release_task=True)
            await self.hub.close()
            await self.ollama.close()

    def request_stop(self) -> None:
        self._stop.set()
