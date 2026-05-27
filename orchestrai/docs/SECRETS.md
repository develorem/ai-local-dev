# OrchestrAi — Secrets

How credentials (GitHub tokens, API keys, MCP tokens, etc.) are stored, requested, and used — without the LLM ever seeing the raw values.

## Threat model

The agent runs a local LLM. The LLM is not trustworthy by default:
- Its prompts include user input and tool output
- Its outputs are executed as commands
- A bad output could leak a token to a log, to a remote endpoint, or into a git commit

So the principle is: **the LLM prompt and the LLM output never contain secret values.** Secrets only enter the picture at the subprocess-execution boundary — and even then, only in environment variables for the duration of the command.

## Storage

The Hub holds an encrypted-at-rest vault.

- **Algorithm**: AES-256-GCM
- **Master key**: 32 random bytes, loaded from a path outside the DB volume (mounted in via Docker secret or env var on Hub startup)
- **Per-secret**: random 12-byte nonce, ciphertext is `nonce || ciphertext || tag`, stored base64
- **Backup**: the user is responsible for the master key. Losing it loses all secrets. The DB without the key is opaque ciphertext.

```
/run/secrets/master_key       (mounted into the Hub at runtime; not in the DB volume)
/data/orchestrai.db           (encrypted secret values live here)
```

## Vault schema

See `SCHEMA.md` for the full DDL. Summary:

```
secrets(name, ciphertext, description, scope, timestamps)
secret_accesses(secret_name, agent_id, task_id, ts, result, reason)
```

`scope` can be:
- `global` — any task can ask
- `project:<project_id>` — only tasks under that project can ask
- `repo:<repo_id>` — only tasks touching that repo can ask

## UI workflows (managing secrets)

The Key Vault screen (see `UI.md`):

- **Add**: name, value (write-only field), description, scope → encrypted, stored
- **Edit metadata**: change description / scope without re-entering the value
- **Rotate value**: enter a new value; updates ciphertext, marks `updated_at`
- **Delete**: removes the row (cascades to access log)
- **View audit**: per-secret log of when it was fetched, by which agent, for which task

Values are **write-only via the UI**. There is no "show value" button. If you forget what's stored, rotate.

## How agents get secrets at runtime

```
1. Task is dispatched to agent. The task's payload (constructed by the Planner
   or by a user editing the task) declares which secrets it needs:

      {
        "type": "implement",
        ...,
        "secrets_needed": ["GITHUB_TOKEN"]
      }

2. The agent's handler, when about to run a subprocess that needs the secret:

      from orchestrai_agent.secrets import inject
      with inject(["GITHUB_TOKEN"]) as env:
          run_subprocess(["gh", "pr", "review", "--approve", pr_url], env=env)

3. `inject` does:
   a. GET /api/secrets/GITHUB_TOKEN/value
      → Hub checks: agent lease_token valid? current task declares this secret?
        scope matches? If yes, returns plaintext and logs secret_accesses(issued).
        If no, returns 403 and logs secret_accesses(denied, reason).
   b. Adds the value to the subprocess env dict
   c. On context-exit, deletes the value from memory (best-effort)

4. The Hub broadcasts a `secret.accessed` event so the UI's audit log updates live.
```

## The LLM never sees the value

The agent's LLM prompt (Implementer mode) lists `secrets_available: ["GITHUB_TOKEN"]` — names only. The model's diff or commands reference the variable name:

```bash
# Good — model output references the name
gh pr review --approve $PR_URL --token "$GITHUB_TOKEN"

# The agent at execution time:
#   - sees model output
#   - sees "$GITHUB_TOKEN" placeholder
#   - calls inject(["GITHUB_TOKEN"])
#   - subprocess runs with the env var set
```

If a model output ever included a literal secret value, the agent's pre-execution sanitizer (a regex check) rejects the output and treats the task as failed with reason "secret leaked in output". This is a belt-and-braces safety; should never fire if the prompt is correct.

## Access-control rules enforced by the Hub

A fetch is denied (HTTP 403, logged) when:

| Condition | Reason |
|---|---|
| Bearer token invalid | `invalid_lease` |
| Agent has no current task | `no_active_task` |
| Task's `secrets_needed` doesn't include this name | `not_declared` |
| Secret scope doesn't include this task's project/repo | `scope_mismatch` |
| Task is not in `in_progress` status | `task_not_running` |

This means: simply having a valid agent + lease isn't enough. The agent must be actively running a task that declared it needs this specific secret.

## What `secrets_needed` looks like

Declared at task creation time by either:
- The Planner (LLM), in its task outline, listing the names it expects each task will need
- A human editing a task in the UI
- A discussion's proposed-action

The agent doesn't get to invent new entries at execution time — if it discovers it needs an undeclared secret, it must surface a Question and let the human approve adding it.

## Backups and recovery

The user (you) needs to:
1. **Back up the master key** offline (e.g. password manager, hardware key)
2. **Back up the DB** regularly (it's a single file; `sqlite3 .backup` is reliable)

Losing the master key with the DB intact = secrets are unrecoverable. The Hub will not start without the master key. If lost, you must:
1. Provision a new master key
2. Re-enter every secret via the UI

This is by design — you can't avoid the key/DB pair being the trust anchor.

## Rotation strategy

For each secret:
- On the source service (e.g. GitHub), issue a new token
- In OrchestrAi UI: rotate value → enter new value
- Old token revoked on the source
- Audit log shows the transition

Future enhancement (v2): scheduled rotation reminders, programmatic rotation via service-specific APIs (GitHub PAT rotation, etc.).

## Common secrets users will set

| Name | Purpose | Scope |
|---|---|---|
| `GITHUB_TOKEN` | clone + push, PR comments, gh CLI | global (or per-project) |
| `OPENAI_API_KEY` | only if a future task uses a cloud model | global |
| `ANTHROPIC_API_KEY` | same | global |
| `NPM_TOKEN` | private npm packages | scope to repos that need it |
| `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | terraform / deployments | scope tightly |
| `<service>_API_KEY` | any project-specific API | project scope |
| `MCP_<name>_TOKEN` | MCP server access | global |

The Hub is opinionated about NOTHING here — any name is fine. The convention is just `UPPERCASE_WITH_UNDERSCORES` so they look like real env vars.

## What this protects against

| Threat | Protected? |
|---|---|
| LLM hallucinates a token into a commit | ✓ — model never sees value |
| LLM exfiltrates a token via curl | ✓ — model never sees value |
| Compromised agent container | Partial — agent could fetch declared secrets and exfiltrate. Mitigated by: scope, declaration requirement, audit log. Strong mitigation = revoke fast. |
| Stolen Hub DB without master key | ✓ — opaque ciphertext |
| Stolen Hub DB WITH master key | ✗ — full secret access. Protect the key like you'd protect a `.ssh/id_rsa`. |
| User accidentally shares the DB volume | ✗ — same as above |

## Audit trail

Every `secret_accesses` row contains:
- Secret name
- Agent ID
- Task ID
- Timestamp
- Result (issued | denied) + reason if denied

The UI shows per-secret access counts on the list view and a per-secret detail page with the chronological log. Spikes or denials are usually worth investigating.

Events also broadcast every access via `secret.accessed` so the UI updates in real time. The event detail includes agent + task IDs (NEVER the value).
