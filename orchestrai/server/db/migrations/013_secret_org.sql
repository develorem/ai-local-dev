-- Secrets become org-scoped so 'global' means "global within this org", not
-- across all tenants. Backfill: project-scoped secrets inherit their project's
-- org; everything else goes to the default org.
ALTER TABLE secrets ADD COLUMN org_id TEXT REFERENCES organizations(id) ON DELETE CASCADE;

UPDATE secrets SET org_id = (
  SELECT p.org_id FROM projects p WHERE 'project:' || p.id = secrets.scope
) WHERE scope LIKE 'project:%';

UPDATE secrets SET org_id = 'org_default' WHERE org_id IS NULL;

CREATE INDEX idx_secrets_org ON secrets(org_id);
