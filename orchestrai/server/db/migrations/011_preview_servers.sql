-- Preview servers: let the agent run a project's app on one of its published
-- ports (6800-6802) and report it, so the UI can show a clickable link to test
-- the running app.

-- 1. How to start the app — lives on the repo config (alongside url/branch).
--    $PORT in the command is substituted with the assigned port at launch.
ALTER TABLE project_repos ADD COLUMN start_command TEXT;

-- 2. The registry of running (or recently-run) preview servers.
CREATE TABLE preview_servers (
  id           TEXT PRIMARY KEY,
  project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  repo_id      TEXT REFERENCES project_repos(id) ON DELETE SET NULL,
  port         INTEGER NOT NULL,
  command      TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'starting'
               CHECK (status IN ('starting','running','stopped','failed')),
  agent_id     TEXT REFERENCES agents(id) ON DELETE SET NULL,
  task_id      TEXT REFERENCES tasks(id) ON DELETE SET NULL,
  detail       TEXT NOT NULL DEFAULT '',
  started_at   TEXT NOT NULL,
  last_seen_at TEXT
);
CREATE INDEX idx_preview_servers_project ON preview_servers(project_id, status);

-- 3. Add the 'preview' task type (launch/stop an app). SQLite can't alter a
--    CHECK, so rebuild tasks (FKs off during migrations). Column set/indexes
--    match the live v10 schema; 'reindex' from migration 010 is preserved.
CREATE TABLE tasks_new (
  id                    TEXT PRIMARY KEY,
  project_id            TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  outcome_id            TEXT REFERENCES outcomes(id) ON DELETE CASCADE,
  parent_task_id        TEXT REFERENCES tasks(id) ON DELETE CASCADE,
  repo_id               TEXT REFERENCES project_repos(id) ON DELETE SET NULL,
  branch_name           TEXT,
  type                  TEXT NOT NULL
                        CHECK (type IN ('plan','implement','review','review_pr',
                                        'respond_to_ci_failure','discuss','revise',
                                        'reindex','preview')),
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

INSERT INTO tasks_new (
  id, project_id, outcome_id, parent_task_id, repo_id, branch_name, type, title,
  description_md, status, priority, depends_on, acceptance_criteria, payload,
  result, error, notes, attempt_count, max_attempts, assigned_agent_id,
  lease_expires_at, created_at, started_at, finished_at)
SELECT
  id, project_id, outcome_id, parent_task_id, repo_id, branch_name, type, title,
  description_md, status, priority, depends_on, acceptance_criteria, payload,
  result, error, notes, attempt_count, max_attempts, assigned_agent_id,
  lease_expires_at, created_at, started_at, finished_at
FROM tasks;

DROP TABLE tasks;
ALTER TABLE tasks_new RENAME TO tasks;

CREATE INDEX idx_tasks_pickup
  ON tasks(status, priority, created_at)
  WHERE status = 'ready' AND assigned_agent_id IS NULL;
CREATE INDEX idx_tasks_lease_expiry
  ON tasks(lease_expires_at)
  WHERE status = 'in_progress' AND lease_expires_at IS NOT NULL;
CREATE INDEX idx_tasks_project ON tasks(project_id, status);
CREATE INDEX idx_tasks_goal ON tasks(outcome_id, status);
CREATE INDEX idx_tasks_repo_branch ON tasks(repo_id, branch_name);
CREATE INDEX idx_tasks_assigned ON tasks(assigned_agent_id) WHERE assigned_agent_id IS NOT NULL;
