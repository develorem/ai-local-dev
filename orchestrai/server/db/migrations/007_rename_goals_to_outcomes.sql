-- Rename "goals" -> "outcomes" (an outcome has tasks; nothing else changes).
-- SQLite >= 3.25 RENAME TABLE updates foreign-key references and indexes in
-- place (legacy_alter_table is off), and FKs are disabled during migrations,
-- so this is a metadata-only rename with no data copy.
--
-- NOTE: the foreign-key COLUMNS that point at this table stay named `goal_id`
-- (tasks/plans/discussions/events) to limit churn; the entity is "outcome"
-- everywhere user-facing (table, API routes, models, UI).
ALTER TABLE goals RENAME TO outcomes;
