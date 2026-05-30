-- Multi-tenancy: users (Google login), organizations, membership, invitations,
-- sessions. Projects and agents become org-scoped. Existing data is migrated
-- into a 'Default Organization' owned by a seed superadmin (the operator).

CREATE TABLE users (
  id             TEXT PRIMARY KEY,
  email          TEXT NOT NULL UNIQUE,
  name           TEXT NOT NULL DEFAULT '',
  picture_url    TEXT,
  google_sub     TEXT UNIQUE,           -- Google OpenID 'sub' (stable user id)
  is_superadmin  INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT NOT NULL,
  last_login_at  TEXT
);

CREATE TABLE organizations (
  id                     TEXT PRIMARY KEY,
  name                   TEXT NOT NULL,
  slug                   TEXT NOT NULL UNIQUE,
  owner_user_id          TEXT REFERENCES users(id) ON DELETE SET NULL,
  plan                   TEXT NOT NULL DEFAULT 'free'
                         CHECK (plan IN ('free','pro','team')),
  stripe_customer_id     TEXT,
  stripe_subscription_id TEXT,
  subscription_status    TEXT,          -- active, trialing, past_due, canceled, …
  created_at             TEXT NOT NULL,
  updated_at             TEXT NOT NULL
);

CREATE TABLE org_members (
  org_id      TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role        TEXT NOT NULL DEFAULT 'member'
              CHECK (role IN ('owner','admin','member')),
  created_at  TEXT NOT NULL,
  PRIMARY KEY (org_id, user_id)
);
CREATE INDEX idx_org_members_user ON org_members(user_id);

CREATE TABLE org_invitations (
  id                  TEXT PRIMARY KEY,
  org_id              TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  email               TEXT NOT NULL,
  role                TEXT NOT NULL DEFAULT 'member'
                      CHECK (role IN ('admin','member')),
  token               TEXT NOT NULL UNIQUE,
  invited_by_user_id  TEXT REFERENCES users(id) ON DELETE SET NULL,
  status              TEXT NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','accepted','revoked')),
  created_at          TEXT NOT NULL,
  accepted_at         TEXT
);
CREATE INDEX idx_org_invitations_email ON org_invitations(email, status);

CREATE TABLE sessions (
  token       TEXT PRIMARY KEY,
  user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at  TEXT NOT NULL,
  expires_at  TEXT NOT NULL
);
CREATE INDEX idx_sessions_user ON sessions(user_id);

-- Org scope on the two plan-limited resources.
ALTER TABLE projects ADD COLUMN org_id TEXT REFERENCES organizations(id) ON DELETE CASCADE;
ALTER TABLE agents   ADD COLUMN org_id TEXT REFERENCES organizations(id) ON DELETE SET NULL;
CREATE INDEX idx_projects_org ON projects(org_id);
CREATE INDEX idx_agents_org ON agents(org_id);

-- Seed: a superadmin operator user + a Default Organization (plan 'team' so the
-- existing data is never constrained by limits), and migrate all existing
-- projects/agents into it.
INSERT INTO users (id, email, name, is_superadmin, created_at)
  VALUES ('user_operator', 'operator@localhost', 'Operator', 1,
          strftime('%Y-%m-%dT%H:%M:%S+00:00','now'));
INSERT INTO organizations (id, name, slug, owner_user_id, plan, created_at, updated_at)
  VALUES ('org_default', 'Default Organization', 'default', 'user_operator', 'team',
          strftime('%Y-%m-%dT%H:%M:%S+00:00','now'),
          strftime('%Y-%m-%dT%H:%M:%S+00:00','now'));
INSERT INTO org_members (org_id, user_id, role, created_at)
  VALUES ('org_default', 'user_operator', 'owner',
          strftime('%Y-%m-%dT%H:%M:%S+00:00','now'));
UPDATE projects SET org_id = 'org_default' WHERE org_id IS NULL;
UPDATE agents   SET org_id = 'org_default' WHERE org_id IS NULL;
