-- Per-project agent access (supersedes execution_mode as the worker's gate).
--
-- A grant says "this agent may pick up / act on this project's tasks". The
-- grantee is either a specific agent (by id) or a whole kind (e.g. all 'worker'
-- instances — the worker re-registers with a fresh id each start, so it must be
-- granted by kind). A project with NO grants is picked up by nobody — the
-- desired default for a new project.
CREATE TABLE project_agents (
  project_id    TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  grantee_type  TEXT NOT NULL CHECK (grantee_type IN ('agent', 'kind')),
  grantee       TEXT NOT NULL,
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (project_id, grantee_type, grantee)
);

-- Preserve current behaviour: every 'auto' project grants the 'worker' kind.
-- 'manual' projects get no grant (the worker already ignored them).
INSERT INTO project_agents (project_id, grantee_type, grantee)
SELECT id, 'kind', 'worker' FROM projects WHERE execution_mode = 'auto';
