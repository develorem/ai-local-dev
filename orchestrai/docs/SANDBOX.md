# OrchestrAi — Sandbox

The containerized execution environment where the agent's code runs. The orchestrator never executes agent-produced commands on the host. Everything happens inside a sandbox container that mounts the project workspace and nothing else useful.

## Why a sandbox at all

The agent is going to run arbitrary commands: `pytest`, `npm install`, `terraform apply`, `python somefile.py`, etc. Three things we want to guarantee:

1. **Host safety.** A bad command can't trash the dev machine.
2. **Linux-native tooling.** CI/CD, IaC, containers — most of it expects bash, gnu coreutils, Linux filesystems. Even though OrchestrAi runs on Windows, the agent's work happens in Linux.
3. **Determinism.** Same image, same behavior. We can rebuild from scratch if things drift.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  orchestrator container                                     │
│  - spawns sandboxes via the host Docker socket              │
│  - reads/writes diffs into the workspace via mounted vol    │
└──────────────────────────────┬──────────────────────────────┘
                               │ Docker API (UNIX socket
                               │ mounted into orchestrator)
                               ▼
┌─────────────────────────────────────────────────────────────┐
│  Host Docker daemon                                         │
└──────────────────────────────┬──────────────────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            ▼                                     ▼
   ┌──────────────────┐                  ┌──────────────────┐
   │ sandbox-goal-X1  │                  │ sandbox-goal-X2  │
   │ (per goal)       │                  │ (per goal)       │
   │ /workspace ←─────┼──── named vol ───┼──→ /workspace     │
   │                  │   (one per goal) │                  │
   └──────────────────┘                  └──────────────────┘
```

Important architectural choice: the orchestrator has the **host Docker socket** mounted inside it (`/var/run/docker.sock:/var/run/docker.sock`). It uses that to launch *sibling* sandbox containers rather than child containers ("docker in docker"). Sibling containers are simpler, faster, and don't require nested-Docker hacks.

This means the orchestrator container is effectively root-on-host (whoever can talk to the Docker daemon can do anything on the host). That's an accepted v1 trade-off: OrchestrAi runs on the user's own machine, and the user already has Docker privileges. Out-of-the-box configuration binds the orchestrator to `localhost` only and assumes single-user.

## Workspace mounting

Each goal gets its own **named Docker volume** (e.g. `orchestrai-workspace-<goal_id>`). This volume:
- Is initialized from a "template" (could be empty, could be a clone of the host repo)
- Is mounted at `/workspace` inside the sandbox
- Survives sandbox restarts (so the agent's work persists across task attempts)
- Can be snapshotted (in v2) for per-task rollback

For v1, a single sandbox per goal handles all tasks for that goal sequentially. The volume accumulates the agent's diffs over time.

In v2 we plan per-task sandboxes with workspace snapshots (essentially `git stash` at the volume level via `docker commit` of an intermediate image).

## Sandbox image

`Dockerfile.sandbox` — a long-running base image with the toolchain the agent might need:

```dockerfile
FROM python:3.12-slim

# Common system tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl wget jq make build-essential \
    sqlite3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Node + npm for JS/TS workloads
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs && rm -rf /var/lib/apt/lists/*

# Python tooling
RUN pip install --no-cache-dir \
    pytest pytest-asyncio \
    ruff black mypy \
    httpx requests \
    sqlalchemy

# Docker CLI (so the agent can run `docker build`, `docker compose`, etc.
# against the host daemon — we'll mount the host socket into the sandbox
# when this capability is needed)
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-24.0.7.tgz \
    | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

# Workspace mount point
WORKDIR /workspace

# Idle entrypoint — the orchestrator execs commands into the running container
CMD ["sleep", "infinity"]
```

Agents can install per-project deps inside the running sandbox (`pip install`, `npm install`) as part of their task work. The image is a "good baseline" — not exhaustive.

## Lifecycle

### Spawn (v1: per goal, on goal activation)

When a goal transitions to `active` (plan approved), the orchestrator:

1. Creates the workspace volume (`docker volume create orchestrai-workspace-<goal_id>`)
2. If the user supplied a "starter repo" (URL or local path), initializes the volume with its contents
3. Starts the sandbox container:
   ```
   docker run -d \
     --name orchestrai-sandbox-<goal_id> \
     --network orchestrai-net \
     --hostname sandbox \
     --memory 8g \
     --cpus 4 \
     --pids-limit 512 \
     -v orchestrai-workspace-<goal_id>:/workspace \
     orchestrai-sandbox:latest
   ```
4. Records the container ID in `goals.sandbox_container_id` (column added via migration)

### Exec (per task)

The orchestrator runs commands inside the sandbox via `docker exec`. Either:

- **Apply diff**: write a file to `/workspace/.orchestrai/pending.diff`, then `docker exec ... git apply /workspace/.orchestrai/pending.diff`
- **Run command**: `docker exec ... bash -lc "<cmd>"`, capturing stdout/stderr and exit code
- **Read file**: `docker exec ... cat <path>`, capturing output

Each exec has a per-command timeout (default 5 minutes; configurable per task via `payload.exec_timeout_sec`).

### Cleanup

When a goal transitions to `done` or `abandoned`:
- Sandbox container stopped + removed
- Workspace volume retained by default (the user might want the produced code); deleted on explicit "purge" action via UI

If the orchestrator crashes, sandbox containers keep running and are reattached on next startup.

## Working tree and the diff workflow

The agent produces unified diffs (see `PROMPTS.md` → Implementer mode). The orchestrator:

1. Writes the diff to `/workspace/.orchestrai/proposals/<task_id>-<attempt>.diff`
2. Verifies it applies: `git apply --check <diff>` inside the sandbox
3. If clean: applies it: `git apply <diff>`
4. Records the diff in `tasks.result` for audit/UI display

Optionally, when `sandbox.lifetime = per_task` (v2), the workspace is `git commit`-ed (an automated bot commit) at task boundaries, giving per-task rollback for free.

For v1, the workspace state is just whatever the running diffs have produced. The user can `git init` / `git diff` inside the volume themselves to inspect changes.

## Networking

By default the sandbox is on `orchestrai-net` (a custom Docker network) and can reach:
- Each other (orchestrator ↔ sandbox)
- The Ollama service (also on this network)
- **The internet** (for `pip install`, `npm install`, etc.)

Future hardening (v2):
- Optional "no-internet" mode (`--network none` plus a proxy for explicit allow-lists)
- Egress logging
- Filesystem read-only mode for stricter task types

For v1 we trust the agent enough to let it install packages, but the human is reviewing diffs before they leave the sandbox via apply.

## Resource limits

Sandbox containers are bounded so a runaway process can't starve the host:

| Resource | Default | Source |
|---|---|---|
| Memory | 8 GB | `--memory 8g` |
| CPUs | 4 | `--cpus 4` |
| PIDs | 512 | `--pids-limit 512` |
| Disk | (uncapped, volume-resident) | — |
| Per-command timeout | 5 min | enforced by orchestrator on `docker exec` |
| Per-task wall-clock | 30 min | enforced by orchestrator |

Configurable in `settings`.

## Sandbox-side helpers

A tiny set of utility scripts is installed into `/usr/local/bin` of the sandbox image to make orchestrator-driven workflows clean:

```
/usr/local/bin/
├── orchestrai-apply-diff      # wrapper around `git apply` with better error msgs
├── orchestrai-snapshot        # for v2: docker commit current state
├── orchestrai-test-run        # standardized test runner with structured exit codes
└── orchestrai-info            # prints workspace tree, python version, npm version, etc.
```

These let prompts reference `orchestrai-apply-diff` rather than raw `git apply`, and give us a versioning seam if behavior needs to change later.

## Security boundary

Honest description of what the v1 sandbox does and doesn't protect against:

| Threat | v1 protection | Notes |
|---|---|---|
| Agent writes outside `/workspace` | ✓ (container FS isolation) | |
| Agent breaks host filesystem | ✓ | |
| Agent network-scans the host | ✗ | Container can reach localhost-on-host via gateway IP. Document this; harden in v2. |
| Agent fork-bombs the host | ✓ (pids-limit) | |
| Agent fills disk | ✗ | Workspace volume can grow unbounded. Add quota in v2. |
| Agent exfiltrates files via curl | ✗ | Internet egress is open. Acceptable for single-user local dev. |
| Agent commits malicious code that user later runs outside the sandbox | (out of scope) | The user is the last reviewer. |

This is single-user developer-machine threat modeling. If OrchestrAi ever runs in a shared environment, all of the ✗ items become design priorities.

## Crash recovery

On orchestrator startup, the recovery routine:

1. For each goal in status `active`, check if `sandbox_container_id` is still running
2. If running → re-attach (no action needed)
3. If stopped → restart it (`docker start <id>`)
4. If gone → recreate it from the goal's workspace volume

For each `in_progress` task at startup: transition back to `ready` and append a `notes` entry: "Orchestrator restarted mid-task; re-queueing." (Idempotent design assumed in handlers.)

## Open design questions (parked for v2)

- **Per-task sandboxes**: spawn fresh container per task, mount snapshot of goal volume, commit on success. Gives per-task rollback. Cost: container startup time per task (~2-5s).
- **Networking allow-lists**: which hostnames can the sandbox reach. Particularly relevant for `npm install` (npm registry only) vs `terraform apply` (cloud APIs).
- **GPU passthrough into sandbox**: would a task ever need its own GPU access? E.g. training a small model as part of the agent's work. We currently say no for v1.
- **Sandbox-side caching layer**: an HTTP proxy that caches `pip install` and `npm install` artifacts across goals, dramatically speeding up sandbox spin-up. Nice-to-have.
- **Pre-seeded language stacks**: optional `Dockerfile.sandbox-python`, `Dockerfile.sandbox-node`, `Dockerfile.sandbox-rust`, picked per goal. Reduces image size and install-time variance.

## Sequence: task that runs tests inside the sandbox

```
1. Worker picks up implement-task-007
2. Implementer Pass 1 → list of files to read, files to write, verification commands
3. Worker: docker exec sandbox cat <each file> → assemble context
4. Implementer Pass 2 → diff + commands_to_run
5. Worker: write diff to /workspace/.orchestrai/proposals/...
6. Worker: docker exec sandbox git apply --check <diff>     (verify)
7. Worker: docker exec sandbox git apply <diff>             (apply)
8. Worker: for each command in commands_to_run:
   - docker exec sandbox bash -lc "<cmd>" (with timeout)
   - capture stdout/stderr/exit
9. Worker runs Reviewer logic (deterministic + LLM as needed)
10. Task → done | fix_needed | needs_human
11. Event emitted with full diff + command outputs
```

All file IO is via the sandbox. The orchestrator never reads or writes files in `/workspace` directly — it always goes through `docker exec`. This gives a clean security boundary and keeps the orchestrator container thin.
