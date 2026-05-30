"""Preview handler — launch (or stop) a project's app on an agent port.

On 'start': set up the workspace (clone the repo), then run the project's
start_command via orchestrai-serve, which backgrounds it (setsid) and waits
until the port is reachable. The server keeps running after this task finishes
so a human can open it from the UI. We report the app's health back; the task
itself always completes (outcome=success) — preview_status carries running/failed.

On 'stop': kill whatever orchestrai-serve launched on that port.

$PORT in the command is replaced with the assigned port.
"""

import logging

from orchestrai_agent.hub_client import HubClient
from orchestrai_agent.ollama_client import OllamaClient
from orchestrai_agent.subprocess_util import run as run_subproc
from orchestrai_agent.workspace import prepare_workspace

log = logging.getLogger("orchestrai-agent.preview")


async def handle_preview(hub: HubClient, ollama: OllamaClient, envelope: dict) -> None:
    task = envelope["task"]
    task_id = task["id"]
    payload = task.get("payload") or {}
    action = payload.get("action") or "start"
    port = payload.get("port")
    preview_id = payload.get("preview_id")

    if not port:
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"preview_id": preview_id, "preview_status": "failed",
                       "detail": "no port in payload"}})
        return

    if action == "stop":
        await run_subproc(f"orchestrai-serve --stop {port}", cwd="/tmp", timeout_sec=15)
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"preview_id": preview_id, "preview_status": "stopped",
                       "port": port}})
        return

    # action == "start"
    try:
        workspace = await prepare_workspace(hub, envelope)
    except Exception as e:
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"preview_id": preview_id, "preview_status": "failed",
                       "port": port, "detail": f"workspace setup failed: {e}"}})
        return

    command = (payload.get("command") or "").replace("$PORT", str(port))
    if not command.strip():
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"preview_id": preview_id, "preview_status": "failed",
                       "port": port, "detail": "empty start command"}})
        return

    await hub.task_event(task_id, "preview.starting", {"port": port, "command": command})
    # Free any prior server on this port first, then launch + wait for readiness.
    await run_subproc(f"orchestrai-serve --stop {port}", cwd=str(workspace), timeout_sec=10)
    serve = await run_subproc(
        f"orchestrai-serve --port {port} --wait-sec 30 -- {command}",
        cwd=str(workspace), timeout_sec=75)

    if serve.exit_code == 0:
        await hub.task_event(task_id, "preview.running", {"port": port})
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"preview_id": preview_id, "preview_status": "running",
                       "port": port}})
    else:
        detail = (serve.stderr or serve.stdout or "")[-800:]
        await hub.task_result(task_id, {
            "outcome": "success",
            "result": {"preview_id": preview_id, "preview_status": "failed",
                       "port": port, "detail": detail},
            "notes_md": f"Preview failed to start on port {port}."})
