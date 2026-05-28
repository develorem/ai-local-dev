"""Pydantic schemas for API request/response bodies.

Matches docs/SCHEMA.md plus the API contract in docs/API.md.
"""

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


# ---------- Projects --------------------------------------------------------

class ProjectCreate(_Base):
    name: str
    slug: str
    description_md: str = ""
    context_md: str = ""


class ProjectUpdate(_Base):
    name: Optional[str] = None
    description_md: Optional[str] = None
    context_md: Optional[str] = None


class Project(_Base):
    id: str
    name: str
    slug: str
    description_md: str
    context_md: str
    status: Literal["active", "archived"]
    created_at: str
    updated_at: str
    archived_at: Optional[str] = None
    tools: dict = Field(default_factory=dict)


# ---------- Repos -----------------------------------------------------------

class RepoCreate(_Base):
    name: str
    role: Optional[str] = None
    url: str
    default_branch: str = "main"
    description_md: str = ""


class RepoUpdate(_Base):
    name: Optional[str] = None
    role: Optional[str] = None
    url: Optional[str] = None
    default_branch: Optional[str] = None
    description_md: Optional[str] = None


class Repo(_Base):
    id: str
    project_id: str
    name: str
    role: Optional[str] = None
    url: str
    default_branch: str
    description_md: str
    created_at: str


# ---------- Goals -----------------------------------------------------------

GoalStatus = Literal["submitted", "planning", "active", "done", "rejected", "abandoned"]
Priority = Literal["low", "normal", "high", "critical"]


class GoalCreate(_Base):
    project_id: str
    title: str
    description_md: str
    priority: Priority = "normal"


class GoalUpdate(_Base):
    title: Optional[str] = None
    description_md: Optional[str] = None
    priority: Optional[Priority] = None


class Goal(_Base):
    id: str
    project_id: str
    title: str
    description_md: str
    status: GoalStatus
    priority: Priority
    created_at: str
    updated_at: str


# ---------- Tasks -----------------------------------------------------------

TaskType = Literal["plan", "implement", "review", "review_pr",
                   "respond_to_ci_failure", "discuss", "revise"]
TaskStatus = Literal["created", "ready", "in_progress",
                     "blocked_on_dep", "blocked_on_human",
                     "review", "done", "failed", "cancelled"]


class TaskCreate(_Base):
    project_id: str
    goal_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    repo_id: Optional[str] = None
    branch_name: Optional[str] = None
    type: TaskType
    title: str
    description_md: str
    priority: Priority = "normal"
    depends_on: list[str] = Field(default_factory=list)
    acceptance_criteria: list[Any] = Field(default_factory=list)
    payload: dict = Field(default_factory=dict)
    max_attempts: int = 3
    status: TaskStatus = "ready"


class TaskUpdate(_Base):
    title: Optional[str] = None
    description_md: Optional[str] = None
    priority: Optional[Priority] = None
    depends_on: Optional[list[str]] = None
    acceptance_criteria: Optional[list[Any]] = None
    max_attempts: Optional[int] = None


class Task(_Base):
    id: str
    project_id: str
    goal_id: Optional[str] = None
    parent_task_id: Optional[str] = None
    repo_id: Optional[str] = None
    branch_name: Optional[str] = None
    type: TaskType
    title: str
    description_md: str
    status: TaskStatus
    priority: Priority
    depends_on: list[str]
    acceptance_criteria: list[Any]
    payload: dict
    result: Optional[dict] = None
    error: Optional[str] = None
    notes: str
    attempt_count: int
    max_attempts: int
    assigned_agent_id: Optional[str] = None
    lease_expires_at: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


# ---------- Agents ----------------------------------------------------------

AgentStatus = Literal["connected", "idle", "busy", "lost", "released"]


class AgentRegister(_Base):
    name: str
    host: Optional[str] = None
    version: str
    capabilities: list[str] = Field(default_factory=list)
    # Host ports mapped into the agent container, available for hosting
    # demo/feedback servers. Identity-mapped (container port == host port).
    http_ports: list[int] = Field(default_factory=list)


class AgentRegisterResponse(_Base):
    agent_id: str
    lease_token: str
    hub_version: str
    heartbeat_interval_sec: int
    lease_timeout_sec: int


class Agent(_Base):
    id: str
    name: str
    host: Optional[str] = None
    version: str
    capabilities: list[str]
    status: AgentStatus
    last_heartbeat_at: Optional[str] = None
    current_task_id: Optional[str] = None
    registered_at: str
    released_at: Optional[str] = None


# ---------- Questions -------------------------------------------------------

QuestionKind = Literal["plan_approval", "clarification", "choice", "confirm", "discussion"]
QuestionStatus = Literal["pending", "answered", "dismissed"]


class Question(_Base):
    id: str
    task_id: Optional[str] = None
    kind: QuestionKind
    prompt_md: str
    options: list[dict]
    status: QuestionStatus
    answer_md: Optional[str] = None
    answer_value: Optional[str] = None
    created_at: str
    answered_at: Optional[str] = None


class AnswerQuestion(_Base):
    answer_md: Optional[str] = None
    answer_value: Optional[str] = None


# ---------- Events ----------------------------------------------------------

class Event(_Base):
    id: str
    ts: str
    kind: str
    entity_type: str
    entity_id: str
    project_id: Optional[str] = None
    goal_id: Optional[str] = None
    task_id: Optional[str] = None
    agent_id: Optional[str] = None
    actor: str
    detail: dict


class EventEnvelope(_Base):
    """WebSocket frame around an Event or a control message."""
    type: Literal["event", "control"] = "event"
    event: Optional[Event] = None
    kind: Optional[str] = None
    detail: Optional[dict] = None
