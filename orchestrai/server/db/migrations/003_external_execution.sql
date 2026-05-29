-- External execution mode.
--
-- A project's tasks are normally claimed and run by the OrchestrAi worker
-- agent. When an OUTSIDE agent (e.g. Claude Code via the MCP server) is the
-- one doing the work and OrchestrAi is used purely to track + manage those
-- tasks, the worker must NOT claim them. `execution_mode` distinguishes the two:
--   'managed'  (default) — the OrchestrAi agent claims & runs ready tasks.
--   'external'           — tracked only; the agent's claim query skips them.
ALTER TABLE projects ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'managed'
  CHECK (execution_mode IN ('managed', 'external'));
