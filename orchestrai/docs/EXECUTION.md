# OrchestrAi — Execution Environment

How and where the **Agent** runs the work. There is no separate sandbox layer anymore — the Agent container itself IS the execution environment. The Hub never executes agent-produced commands.

## Boundary

The Agent container is the security boundary. It:
- Holds **no host filesystem access** (no host volume mounts other than what the user explicitly grants)
- Has **no host Docker socket** (it cannot spawn sibling containers, so it cannot escape)
- Sees no host cookies, tokens, SSH keys, browser state
- Has its own filesystem, its own toolchain, its own clean state every restart

What the agent CAN do:
- Read/write files inside its own container, including `/workspace/<repo>` for git work
- Run any subprocess available in the image (Python, Node, git, gcc, etc.)
- Network out (clone repos, install packages, push commits)
- Talk to the Hub over the internal compose network
- Talk to Ollama over the internal compose network

What the agent CANNOT do (without future design):
- Run Docker (no socket; deliberate)
- See the host filesystem
- Persist anything outside its container (state lives in git origin + Hub DB)

This means a fully compromised agent can:
- Damage its own container state — recoverable, just recreate
- Push bad code to a feature branch on origin — recoverable, you don't merge it
- Use network outbound — for v1 we accept this (it has to clone, push, install)

It cannot:
- Touch your host files
- Steal your host credentials
- Corrupt your other projects
- Read your browser cookies

## Container image

`Dockerfile.agent` — long-running base with everything an agent needs.

```dockerfile
FROM python:3.12-slim

# System tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget jq make build-essential ca-certificates \
    sqlite3 unzip openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Node.js for JS/TS workloads
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*

# Python tooling (for the agent's own runtime AND for project work)
RUN pip install --no-cache-dir \
    httpx pydantic \
    pytest pytest-asyncio ruff black mypy \
    requests sqlalchemy alembic

# GitHub CLI (for PR review / comments)
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] \
        https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y gh && rm -rf /var/lib/apt/lists/*

# The agent's own code (separate Python package)
COPY agent/ /opt/orchestrai-agent/
RUN pip install --no-cache-dir -e /opt/orchestrai-agent

# Helper CLI scripts on PATH
COPY agent/bin/orchestrai-secrets /usr/local/bin/orchestrai-secrets
COPY agent/bin/orchestrai-report  /usr/local/bin/orchestrai-report
COPY agent/bin/orchestrai-context /usr/local/bin/orchestrai-context
RUN chmod +x /usr/local/bin/orchestrai-*

# Workspace mount point for clones
WORKDIR /workspace

ENV ORCHESTRAI_HUB_URL=http://hub:8080

# The entrypoint is the agent loop — registers with Hub, claims tasks, executes
ENTRYPOINT ["python", "-m", "orchestrai_agent"]
```

The image is ~600-800 MB built. Cached layers make rebuilds fast.

## Agent loop

The entrypoint (`python -m orchestrai_agent`) does:

```python
async def main():
    creds = register_with_hub()         # POST /api/agents/register
    while not shutdown:
        await heartbeat(creds)          # async; runs every 10s
        task = await claim_next(creds)  # POST /api/agents/{id}/claim
        if task is None:
            await asyncio.sleep(5)
            continue
        try:
            await handle_task(creds, task)
        except CriticalAgentError:
            await release_task(task)
            break                       # exit the container; supervisor restarts us
    await release(creds)
```

`handle_task` dispatches by `task.type` to specialized handlers (planner, implementer, reviewer, etc.) defined per `PROMPTS.md`.

## Workspace within the container

Each repo the agent works on is cloned into `/workspace/<repo-name>/`. On first task that touches repo R:

```python
def ensure_repo(repo):
    path = f"/workspace/{repo.name}"
    if not Path(path).exists():
        run(["git", "clone", repo.url, path])
    return path
```

Subsequent tasks on the same repo reuse the existing clone. The agent always:
1. `git fetch origin`
2. `git checkout -B <branch_name> origin/<base_branch>` (creates or resets the branch)
3. Does its work
4. `git add -A && git commit -m "..."`
5. `git push origin <branch_name>` (force-with-lease for safety)

If the container is recreated, `/workspace/` is empty — but the next task triggers a fresh clone. The agent never relies on persistent local state.

## Secret injection

When a task needs a secret (e.g. GITHUB_TOKEN for `gh pr review`):

```bash
# The agent's handler runs this BEFORE executing the user-facing command:
orchestrai-secrets inject GITHUB_TOKEN
# → fetches via GET /api/secrets/GITHUB_TOKEN/value
# → exports it into the current subprocess env via a sourceable .env
# → after command completes, .env is shredded
```

The LLM prompt for the implementer does NOT include the secret value — only references it as `$GITHUB_TOKEN`. The model's diff or commands reference the variable name. When the agent executes the command, it sources the temp env first.

Full secret protocol: see `SECRETS.md`.

## Subprocess execution

Every agent-spawned subprocess goes through a single helper:

```python
def run_subprocess(
    cmd: list[str],
    *,
    cwd: str = "/workspace",
    env: dict[str, str] | None = None,
    timeout_sec: int = 300,
    capture: bool = True,
) -> ProcessResult:
    """
    Centralized subprocess wrapper:
      - enforces timeout
      - captures stdout + stderr (size-capped at 64KB each)
      - logs the command to the Hub via POST /api/tasks/{id}/events
      - injects secrets via env (never via stdin / arguments)
    """
```

This is the only place the agent shells out. Easy to audit, easy to add tracing later.

## Resource limits (Docker-level)

Set in `docker-compose.yml` for the agent service:

```yaml
agent:
  build: { context: ., dockerfile: Dockerfile.agent }
  deploy:
    resources:
      limits:
        memory: 8G
        cpus: '4'
        pids: 512
  environment:
    ORCHESTRAI_HUB_URL: http://hub:8080
  networks: [orchestrai-net]
  restart: unless-stopped
```

If the agent gets stuck or runaway, Docker enforces the ceiling. `restart: unless-stopped` makes a crashed agent come back on its own — the Hub will reissue work via the reaper.

## Adding more agents

For multi-machine (future) or extra parallelism on one machine: add another `agent` service in compose with a different container name. Each runs the same image, registers separately with the Hub, gets its own lease token.

```yaml
agent-2:
  <<: *agent-base
  container_name: orchestrai-agent-2
```

For another PC: deploy the same `Dockerfile.agent` there, point `ORCHESTRAI_HUB_URL` at the Hub's network-reachable address. The Hub itself stays on one machine; agents are the distributed part.

## What the agent does NOT touch

- The Hub's database
- Other agents' workspaces
- The host filesystem
- Other containers (no Docker socket)
- The browser

Even if a malicious model output told it to "run rm -rf /", the worst case is the agent destroys its own container state. Restart, continue.

## Cleanup

When the agent shuts down (or its container is removed):

- All `/workspace/*` clones disappear with the container — git origin is the only persistence
- All cached pip/npm installs disappear with the container — re-downloaded next boot
- The agent's row in `agents` table flips to `released` or `lost`
- Any task it was holding goes back to `ready` after lease expiry

There is nothing to clean up on the Hub side. By design.

## What's gone vs the previous design

- No per-goal sandbox containers spawned by the orchestrator
- No host Docker socket mount
- No snapshot lifecycle
- No workspace volumes managed by the Hub
- No git-commit-at-task-boundaries (agent's local commits push to origin instead; rollback is `git reset --hard origin/<branch>`)

All replaced by: the Agent IS the execution environment, workspaces are transient inside it, source of truth is the git origin remote.
