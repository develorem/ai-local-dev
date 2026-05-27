"""Per-task-type handlers. Each handler is async and takes (hub, ollama, task_envelope)
and submits a result via hub.task_result(...)."""

from orchestrai_agent.handlers.plan import handle_plan

HANDLERS = {
    "plan": handle_plan,
}


def handler_for(task_type: str):
    return HANDLERS.get(task_type)
