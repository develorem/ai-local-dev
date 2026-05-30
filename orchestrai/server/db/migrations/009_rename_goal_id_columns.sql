-- Finish the goals -> outcomes rename: the foreign-key COLUMNS that point at the
-- outcomes table become outcome_id (they were left as goal_id by migration 007).
-- SQLite >= 3.25 RENAME COLUMN updates the column's FK clause and any indexes
-- that reference it; FKs are off during migrations.
ALTER TABLE tasks       RENAME COLUMN goal_id TO outcome_id;
ALTER TABLE plans       RENAME COLUMN goal_id TO outcome_id;
ALTER TABLE discussions RENAME COLUMN goal_id TO outcome_id;
ALTER TABLE events      RENAME COLUMN goal_id TO outcome_id;
