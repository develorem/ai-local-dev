-- Agent kind. Every agent registers with OrchestrAi (the local worker already
-- does); `kind` distinguishes them so projects can be configured per kind:
--   'worker'   — the OrchestrAi autonomous worker (claims & runs tasks).
--   'external' — an outside agent registered from the portal (e.g. Claude Code),
--                identified by its lease token.
-- Existing rows are the worker.
ALTER TABLE agents ADD COLUMN kind TEXT NOT NULL DEFAULT 'worker';
