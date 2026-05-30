-- Document index: a seek structure (NOT a content summary) so agents can decide
-- WHICH doc to fetch and WHEN, then pull the full doc on demand.
--   - headings: mechanical (markdown ATX headings), recomputed synchronously on
--     every save — always fresh, no model needed.
--   - purpose: one-line "what this is / when to consult", written by the model
--     via a 'reindex' task. Stable routing intent, refreshed only when content
--     changes (tracked by indexed_hash).
-- Repo-sourced docs (./docs, ./ai-docs, …) live here too with source='repo':
-- only their INDEX metadata is stored (content_md holds a bounded excerpt used
-- to generate the purpose); the full body is fetched from the agent's checked-out
-- workspace on demand, so the git repo stays the single source of truth.

-- 1. Index columns on project_documents.
ALTER TABLE project_documents ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE project_documents ADD COLUMN repo_id TEXT REFERENCES project_repos(id) ON DELETE CASCADE;
ALTER TABLE project_documents ADD COLUMN repo_path TEXT;
ALTER TABLE project_documents ADD COLUMN headings TEXT NOT NULL DEFAULT '[]';
ALTER TABLE project_documents ADD COLUMN purpose TEXT NOT NULL DEFAULT '';
ALTER TABLE project_documents ADD COLUMN indexed_hash TEXT;

-- Repo docs are reconciled by (project, repo, path) on every clone/pull/checkout;
-- manual docs (repo_id/repo_path NULL) are unaffected — NULLs are distinct here.
CREATE UNIQUE INDEX idx_project_documents_repo
  ON project_documents(project_id, repo_id, repo_path) WHERE source = 'repo';

-- 2. Add the 'reindex' task type. SQLite can't alter a CHECK constraint, so
--    rebuild tasks (FKs are OFF during migrations). Column set + FKs match the
--    live v9 schema exactly (outcome_id came from migration 009).
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
                                        'reindex')),
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

-- Recreate the task indexes (dropped with the old table). Matches live v9.
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
