-- Tool registry per project.
--
-- Stored as a JSON object so we can grow shapes without further migrations:
--   {
--     "python_packages": ["fastapi", "pillow", "..."],
--     "node_packages":   ["pino", "..."]
--   }
--
-- The planner emits `tools_required` on every plan; the Hub UNIONs it into
-- this column (never replaces) so once a project depends on a package, the
-- dependency sticks. The agent diffs against `pip freeze` at task claim
-- time and installs anything missing before the implementer runs.
ALTER TABLE projects ADD COLUMN tools TEXT NOT NULL DEFAULT '{}';

-- Per-plan record of what THIS plan proposed adding. Lets the approval UI
-- show "approving will install X, Y, Z" before the user commits, and lets us
-- audit which plan introduced which dependency.
ALTER TABLE plans ADD COLUMN tools_required TEXT NOT NULL DEFAULT '{}';
