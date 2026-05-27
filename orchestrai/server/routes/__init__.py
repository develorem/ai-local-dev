from fastapi import APIRouter

from server.routes import health, projects, repos, goals, tasks, agents, events, questions

api = APIRouter(prefix="/api")
api.include_router(health.router)
api.include_router(projects.router)
api.include_router(repos.router)
api.include_router(goals.router)
api.include_router(tasks.router)
api.include_router(agents.router)
api.include_router(questions.router)
api.include_router(events.router)

__all__ = ["api"]
