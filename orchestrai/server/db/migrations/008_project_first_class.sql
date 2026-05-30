-- Make projects first-class: documents, agent roles, scheduled tasks, and a
-- GitHub auth secret on repos. (Project-scoped secrets are migration 009.)

-- 1. Project context documents (human- and agent-readable).
CREATE TABLE project_documents (
  id            TEXT PRIMARY KEY,
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  content_md    TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
CREATE INDEX idx_project_documents_project ON project_documents(project_id);

-- 2. Agent role on a project grant. 'any' = does everything (back-compat for
--    existing grants); otherwise the agent may only claim that kind of task.
--    Used for both routing (which agent type claims which task type) and the
--    coverage check (a project is AI-completable only if plan+implement+review
--    are all covered by some granted agent).
ALTER TABLE project_agents ADD COLUMN role TEXT NOT NULL DEFAULT 'any'
  CHECK (role IN ('any', 'plan', 'implement', 'review'));

-- 3. Scheduled tasks: a cron spec + a task template. When due, the scheduler
--    materialises a real task into the backlog.
CREATE TABLE scheduled_tasks (
  id                   TEXT PRIMARY KEY,
  project_id           TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name                 TEXT NOT NULL,
  cron                 TEXT NOT NULL,
  task_type            TEXT NOT NULL DEFAULT 'implement',
  title                TEXT NOT NULL,
  description_md       TEXT NOT NULL DEFAULT '',
  priority             TEXT NOT NULL DEFAULT 'normal',
  acceptance_criteria  TEXT NOT NULL DEFAULT '[]',
  enabled              INTEGER NOT NULL DEFAULT 1,
  last_run_at          TEXT,
  next_run_at          TEXT,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL
);
CREATE INDEX idx_scheduled_tasks_due ON scheduled_tasks(enabled, next_run_at);

-- 4. GitHub on repos: which vault secret holds the token used to clone/push.
--    (project_repos already has url + default_branch.) If a project has no repo
--    row at all, the agent falls back to a local-only git repo with a warning.
ALTER TABLE project_repos ADD COLUMN auth_secret_name TEXT;
