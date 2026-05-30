from fastapi import APIRouter

from server.routes import (
    health, projects, repos, outcomes, tasks, agents, events, questions,
    plans, secrets as secrets_routes, discussions, webhooks,
)

api = APIRouter(prefix="/api")
api.include_router(health.router)
api.include_router(projects.router)
api.include_router(repos.router)
api.include_router(outcomes.router)
api.include_router(tasks.router)
api.include_router(agents.router)
api.include_router(questions.router)
api.include_router(plans.router)
api.include_router(secrets_routes.router)
api.include_router(discussions.router)
api.include_router(webhooks.router)
api.include_router(events.router)

__all__ = ["api"]
