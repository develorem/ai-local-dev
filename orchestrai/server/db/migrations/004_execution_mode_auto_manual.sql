-- Reframe the autopilot flag to be agent-agnostic.
--
-- The OrchestrAi worker is just one API client; its only special power is the
-- autonomous claim loop. So this flag means "should the worker auto-run this
-- project's tasks?" — an opt-in, not "internal vs external":
--   'auto'   — the OrchestrAi worker claims & runs ready tasks (was 'managed').
--   'manual' — DEFAULT; whatever agent/human is connected drives it (was 'external').
--
-- SQLite can't alter a column's CHECK/DEFAULT in place, so rebuild `projects`.
-- Only auto-indexes (PK + UNIQUE slug) exist, recreated by the new table def.
-- Foreign keys are OFF during migrations, so dependent rows are untouched and
-- the preserved ids keep every reference valid.
BEGIN;

CREATE TABLE projects_new (
  id              TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  slug            TEXT NOT NULL UNIQUE,
  description_md  TEXT NOT NULL DEFAULT '',
  context_md      TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'archived')),
  tools           TEXT NOT NULL DEFAULT '{}',
  execution_mode  TEXT NOT NULL DEFAULT 'manual'
                  CHECK (execution_mode IN ('auto', 'manual')),
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL,
  archived_at     TEXT
);

INSERT INTO projects_new (id, name, slug, description_md, context_md, status,
                          tools, execution_mode, created_at, updated_at, archived_at)
SELECT id, name, slug, description_md, context_md, status, tools,
       CASE execution_mode WHEN 'managed'  THEN 'auto'
                           WHEN 'external'  THEN 'manual'
                           ELSE 'manual' END,
       created_at, updated_at, archived_at
FROM projects;

DROP TABLE projects;
ALTER TABLE projects_new RENAME TO projects;

COMMIT;
