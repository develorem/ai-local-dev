-- OrchestrAi initial schema. Matches docs/SCHEMA.md.

CREATE TABLE projects (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  slug            TEXT NOT NULL UNIQUE,
  description_md  TEXT NOT NULL DEFAULT '',
  context_md      TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'archived')),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  archived_at     TEXT
);

CREATE TABLE project_repos (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name            TEXT NOT NULL,
  role            TEXT,
  url             TEXT NOT NULL,
  default_branch  TEXT NOT NULL DEFAULT 'main',
  description_md  TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  UNIQUE(project_id, name)
);

CREATE TABLE agents (
  id                  TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  host                TEXT,
  version             TEXT NOT NULL,
  capabilities        TEXT NOT NULL DEFAULT '[]',
  status              TEXT NOT NULL
                      CHECK (status IN ('connected','idle','busy','lost','released')),
  lease_token         TEXT NOT NULL,
  last_heartbeat_at   TEXT,
  current_task_id     TEXT,
  registered_at       TEXT NOT NULL,
  released_at         TEXT
);
CREATE INDEX idx_agents_status ON agents(status);
CREATE INDEX idx_agents_heartbeat ON agents(last_heartbeat_at);

CREATE TABLE goals (
  id              TEXT PRIMARY KEY,
  project_id      TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title           TEXT NOT NULL,
  description_md  TEXT NOT NULL,
  status          TEXT NOT NULL
                  CHECK (status IN ('submitted','planning','active','done','rejected','abandoned')),
  priority        TEXT NOT NULL DEFAULT 'normal'
                  CHECK (priority IN ('low','normal','high','critical')),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
CREATE INDEX idx_goals_project ON goals(project_id, status);

CREATE TABLE tasks (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  goal_id               TEXT REFERENCES goals(id) ON DELETE CASCADE,
  parent_task_id        TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  repo_id               TEXT REFERENCES project_repos(id) ON DELETE SET NULL,
  branch_name           TEXT,
  type                  TEXT NOT NULL
                        CHECK (type IN ('plan','implement','review','review_pr',
                                        'respond_to_ci_failure','discuss','revise')),
  title                 TEXT NOT NULL,
  description_md        TEXT NOT NULL,
  status                TEXT NOT NULL
                        CHECK (status IN ('created','ready','in_progress',
                                          'blocked_on_dep','blocked_on_human',
                                          'review','done','failed','cancelled')),
  priority              TEXT NOT NULL DEFAULT 'normal'
                        CHECK (priority IN ('low','normal','high','critical')),
  depends_on            TEXT NOT NULL DEFAULT '[]',
  acceptance_criteria   TEXT NOT NULL DEFAULT '[]',
  payload               TEXT NOT NULL DEFAULT '{}',
  result                TEXT,
  error                 TEXT,
  notes                 TEXT NOT NULL DEFAULT '',
  attempt_count         INTEGER NOT NULL DEFAULT 0,
  max_attempts          INTEGER NOT NULL DEFAULT 3,
  assigned_agent_id     TEXT REFERENCES agents(id) ON DELETE SET NULL,
  lease_expires_at      TEXT,
  created_at            TEXT NOT NULL,
  started_at            TEXT,
  finished_at           TEXT
);
CREATE INDEX idx_tasks_pickup
  ON tasks(status, priority, created_at)
  WHERE status = 'ready' AND assigned_agent_id IS NULL;
CREATE INDEX idx_tasks_lease_expiry
  ON tasks(lease_expires_at)
  WHERE status = 'in_progress' AND lease_expires_at IS NOT NULL;
CREATE INDEX idx_tasks_project ON tasks(project_id, status);
CREATE INDEX idx_tasks_goal ON tasks(goal_id, status);
CREATE INDEX idx_tasks_repo_branch ON tasks(repo_id, branch_name);
CREATE INDEX idx_tasks_assigned ON tasks(assigned_agent_id) WHERE assigned_agent_id IS NOT NULL;

CREATE TABLE plans (
  id                    TEXT PRIMARY KEY,
  goal_id               TEXT NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
  version               INTEGER NOT NULL DEFAULT 1,
  content_md            TEXT NOT NULL,
  task_outline          TEXT NOT NULL DEFAULT '[]',
  status                TEXT NOT NULL
                        CHECK (status IN ('draft','approved','rejected','superseded')),
  approval_question_id  TEXT,
  created_at            TEXT NOT NULL,
  approved_at           TEXT,
  approval_notes        TEXT
);
CREATE UNIQUE INDEX idx_plans_goal_version ON plans(goal_id, version);

CREATE TABLE questions (
  id              TEXT PRIMARY KEY,
  task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  kind            TEXT NOT NULL
                  CHECK (kind IN ('plan_approval','clarification','choice','confirm','discussion')),
  prompt_md       TEXT NOT NULL,
  options_json    TEXT NOT NULL DEFAULT '[]',
  status          TEXT NOT NULL
                  CHECK (status IN ('pending','answered','dismissed')),
  answer_md       TEXT,
  answer_value    TEXT,
  created_at      TEXT NOT NULL,
  answered_at     TEXT
);
CREATE INDEX idx_questions_pending ON questions(status) WHERE status = 'pending';
CREATE INDEX idx_questions_task ON questions(task_id);

CREATE TABLE discussions (
  id              TEXT PRIMARY KEY,
  project_id      TEXT REFERENCES projects(id) ON DELETE CASCADE,
  goal_id         TEXT REFERENCES goals(id) ON DELETE CASCADE,
  task_id         TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  title           TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','closed')),
  created_at      TEXT NOT NULL,
  closed_at       TEXT
);

CREATE TABLE messages (
  id              TEXT PRIMARY KEY,
  discussion_id   TEXT NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
  role            TEXT NOT NULL CHECK (role IN ('user','agent','system')),
  content_md      TEXT NOT NULL,
  meta            TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL
);

CREATE TABLE proposed_actions (
  id              TEXT PRIMARY KEY,
  discussion_id   TEXT NOT NULL REFERENCES discussions(id) ON DELETE CASCADE,
  message_id      TEXT REFERENCES messages(id) ON DELETE CASCADE,
  action_type     TEXT NOT NULL
                  CHECK (action_type IN ('create_task','modify_task','cancel_task',
                                         'reorder_dependencies','edit_plan')),
  payload         TEXT NOT NULL,
  human_summary   TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'proposed'
                  CHECK (status IN ('proposed','applied','rejected','superseded')),
  created_at      TEXT NOT NULL,
  applied_at      TEXT,
  applied_by      TEXT
);

CREATE TABLE secrets (
  name                TEXT PRIMARY KEY,
  ciphertext          TEXT NOT NULL,
  description         TEXT NOT NULL DEFAULT '',
  scope               TEXT NOT NULL DEFAULT 'global',
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  last_accessed_at    TEXT,
  access_count        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE secret_accesses (
  id              TEXT PRIMARY KEY,
  secret_name     TEXT NOT NULL REFERENCES secrets(name) ON DELETE CASCADE,
  agent_id        TEXT REFERENCES agents(id) ON DELETE SET NULL,
  task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  ts              TEXT NOT NULL,
  result          TEXT NOT NULL CHECK (result IN ('issued','denied')),
  reason          TEXT
);
CREATE INDEX idx_secret_accesses_secret ON secret_accesses(secret_name, ts);
CREATE INDEX idx_secret_accesses_agent ON secret_accesses(agent_id, ts);

CREATE TABLE events (
  id              TEXT PRIMARY KEY,
  ts              TEXT NOT NULL,
  kind            TEXT NOT NULL,
  entity_type     TEXT NOT NULL,
  entity_id       TEXT NOT NULL,
  project_id      TEXT REFERENCES projects(id) ON DELETE SET NULL,
  goal_id         TEXT REFERENCES goals(id) ON DELETE SET NULL,
  task_id         TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  agent_id        TEXT REFERENCES agents(id) ON DELETE SET NULL,
  actor           TEXT NOT NULL DEFAULT 'system',
  detail          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_events_ts ON events(ts);
CREATE INDEX idx_events_entity ON events(entity_type, entity_id, ts);
CREATE INDEX idx_events_project ON events(project_id, ts) WHERE project_id IS NOT NULL;
CREATE INDEX idx_events_task ON events(task_id, ts) WHERE task_id IS NOT NULL;
CREATE INDEX idx_events_agent ON events(agent_id, ts) WHERE agent_id IS NOT NULL;

CREATE TABLE settings (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);

-- Seed default settings
INSERT INTO settings (key, value, updated_at) VALUES
  ('model.primary', 'qwen2.5-coder:14b', datetime('now')),
  ('inference.num_ctx', '16384', datetime('now')),
  ('inference.temperature', '0', datetime('now')),
  ('loop.max_attempts_default', '3', datetime('now')),
  ('loop.idle_poll_seconds', '5', datetime('now')),
  ('agent.heartbeat_interval_sec', '10', datetime('now')),
  ('agent.lease_timeout_sec', '30', datetime('now'));
