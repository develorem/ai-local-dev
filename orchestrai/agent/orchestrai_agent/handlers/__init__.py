"""Per-task-type handlers. Each handler is async and takes (hub, ollama, task_envelope)
and submits a result via hub.task_result(...)."""

from orchestrai_agent.handlers.plan import handle_plan
from orchestrai_agent.handlers.implement import handle_implement
from orchestrai_agent.handlers.review import handle_review

HANDLERS = {
    "plan": handle_plan,
    "implement": handle_implement,
    "review": handle_review,
}


def handler_for(task_type: str):
    return HANDLERS.get(task_type)
