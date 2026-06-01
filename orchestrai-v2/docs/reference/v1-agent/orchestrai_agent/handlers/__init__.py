"""Per-task-type handlers. Each handler is async and takes (hub, ollama, task_envelope)
and submits a result via hub.task_result(...)."""

from orchestrai_agent.handlers.plan import handle_plan
from orchestrai_agent.handlers.implement import handle_implement
from orchestrai_agent.handlers.review import handle_review
from orchestrai_agent.handlers.discuss import handle_discuss
from orchestrai_agent.handlers.revise import handle_revise
from orchestrai_agent.handlers.pr_review import handle_review_pr
from orchestrai_agent.handlers.ci_fix import handle_ci_failure
from orchestrai_agent.handlers.reindex import handle_reindex
from orchestrai_agent.handlers.preview import handle_preview

HANDLERS = {
    "plan": handle_plan,
    "implement": handle_implement,
    "review": handle_review,
    "discuss": handle_discuss,
    "revise": handle_revise,
    "review_pr": handle_review_pr,
    "respond_to_ci_failure": handle_ci_failure,
    "reindex": handle_reindex,
    "preview": handle_preview,
}


def handler_for(task_type: str):
    return HANDLERS.get(task_type)
