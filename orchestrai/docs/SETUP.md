# OrchestrAi — Setup

How to get OrchestrAi running on a clean machine. Target host: Windows 11 with an NVIDIA GPU. Linux and Mac (Apple Silicon — different inference path) work too with minor adjustments.

## Prerequisites

| Item | Why | How |
|---|---|---|
| **Docker Desktop** (latest stable) | Runs the Hub, Agent, Ollama | https://www.docker.com/products/docker-desktop |
| **WSL2** (Windows only) | Docker backend with GPU passthrough | `wsl --install` from admin PowerShell, then reboot |
| **NVIDIA driver** (555+ for RTX 50-series) | CUDA in containers | https://www.nvidia.com/drivers |
| **Git** | Clone this repo | https://git-scm.com/downloads |

No Python install, no Ollama install on the host, no Node — everything ships in containers.

## WSL2 tuning (Windows only)

Create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=24GB
processors=8
swap=8GB
localhostForwarding=true

[experimental]
autoMemoryReclaim=gradual
```

Adjust `memory` to your host RAM — leave ~16 GB for Windows.

Restart WSL: `wsl --shutdown` then re-open Docker Desktop.

## Verify GPU passthrough

```powershell
docker run --rm --gpus=all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

Expected: full nvidia-smi output showing your GPU. If not, fix this before going further (see "Common issues").

## Clone

```powershell
cd C:\me\dev\github
git clone <orchestrai-repo-url> orchestrai
cd orchestrai
```

(In our repo layout, `orchestrai/` is a subfolder of `ai-local-dev/`.)

## Master key

The Hub encrypts secrets with a master key. Generate it once and protect it like an SSH key.

```powershell
# Generates 32 random bytes, base64-encodes, saves to a host file
$bytes = New-Object byte[] 32
[System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
[System.Convert]::ToBase64String($bytes) | Out-File -Encoding ASCII C:\me\.orchestrai\master_key
```

The compose file mounts this into the Hub. **Back it up somewhere safe (password manager, USB drive).** If you lose it, all stored secrets are unrecoverable.

## First run

```powershell
docker compose up -d
```

What happens:
1. Pulls `ollama/ollama` image (~600 MB)
2. Builds the `hub` image (~250 MB)
3. Builds the `agent` image (~700 MB — toolchain + helper CLIs)
4. Starts `hub`, `ollama`, and one `agent` on the `orchestrai-net` network
5. The agent auto-registers with the Hub and starts polling for work

First-time setup is 5-10 minutes mostly downloading. Subsequent starts are seconds.

## Pull the primary model

```powershell
docker compose exec ollama ollama pull qwen2.5-coder:14b
```

~9 GB download. Lives in the `ollama-models` named volume; persists across restarts.

## Verify everything is working

```powershell
# Hub healthy
curl http://localhost:6724/api/health

# Agent registered
curl http://localhost:6724/api/agents
```

The Hub health response should show `ollama.reachable=true` and at least one agent listed.

```powershell
# Open the UI
start http://localhost:6724
```

You should see the Agents screen with one connected agent in `idle` status.

## docker-compose.yml (reference)

A full copy lives in the repo root. The key services:

```yaml
services:
  ollama:
    image: ollama/ollama:latest
    container_name: orchestrai-ollama
    deploy:
      resources:
        reservations:
          devices:
            - capabilities: [gpu]
              count: all
    environment:
      OLLAMA_FLASH_ATTENTION: "1"
      OLLAMA_KV_CACHE_TYPE: "q8_0"
    volumes:
      - ollama-models:/root/.ollama
    networks: [orchestrai-net]
    restart: unless-stopped

  hub:
    build:
      context: .
      dockerfile: Dockerfile.hub
    container_name: orchestrai-hub
    depends_on: [ollama]
    environment:
      OLLAMA_URL: http://ollama:11434
      DATA_DIR: /data
      MASTER_KEY_PATH: /run/secrets/master_key
    volumes:
      - ./data:/data
      - /c/me/.orchestrai/master_key:/run/secrets/master_key:ro
    ports:
      - "6724:6724"
    networks: [orchestrai-net]
    restart: unless-stopped

  agent:
    build:
      context: .
      dockerfile: Dockerfile.agent
    container_name: orchestrai-agent
    depends_on: [hub]
    environment:
      ORCHESTRAI_HUB_URL: http://hub:6724
      AGENT_NAME: "agent@${HOSTNAME:-dev}"
    networks: [orchestrai-net]
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 8G
          cpus: '4'

volumes:
  ollama-models:

networks:
  orchestrai-net:
    driver: bridge
```

Note: the `hub` does NOT mount the host Docker socket and the `agent` does NOT either. The boundary is real. The agent has no host access — it just talks to the Hub and runs its own subprocesses inside its container.

## Adding more agents

For more parallelism on the same machine, copy the `agent` service block with a different name:

```yaml
agent-2:
  <<: *agent-template
  container_name: orchestrai-agent-2
  environment:
    AGENT_NAME: "agent-2@${HOSTNAME:-dev}"
```

For a different machine: deploy `Dockerfile.agent` there, set `ORCHESTRAI_HUB_URL` to the network-reachable Hub address, ensure the Hub's port is reachable. The agent registers and starts working.

## First project

Open the UI → Projects → Add Project:

```
Name:        My First Project
Slug:        my-first
Description: A small FastAPI app to test OrchestrAi end-to-end.
Context:
  Stack: python 3.12, fastapi
  Conventions: snake_case, pytest, ruff for linting
```

Then add a repo (Add Repo on the project detail screen) — give it a git URL the agent can clone.

Then submit a goal — "Add a /health endpoint with tests" — and watch the board.

## Adding your first secret

UI → Vault → Add Secret:

```
Name:        GITHUB_TOKEN
Value:       ghp_xxxxxxxx  (write-only; never readable again)
Description: GitHub access for clone, push, gh CLI
Scope:       Global
```

When agents take on tasks that declare `secrets_needed: ["GITHUB_TOKEN"]`, the Hub issues the value to that agent for the duration of the task. The audit log records every fetch.

## Common issues

### "could not select device driver" when starting ollama

GPU passthrough not working. Walk through "Verify GPU passthrough" with the bare `nvidia/cuda` image first.

### Hub starts but errors "master_key not found"

The host path in `docker-compose.yml` doesn't match where you saved the key. Fix the mount path.

### Agent registers, then immediately marked `lost`

Agent → Hub heartbeats are failing. Inside the agent: `curl http://hub:6724/api/health` should return 200. If not, check the compose network.

### Schema migration error on first start

If you're upgrading and a migration is unsafe, the Hub refuses to start. Logs name the failing migration. For a development reset: `docker compose down -v && rm -rf data && docker compose up -d` (loses all goals/tasks/history — fine early, bad later).

### Out-of-memory loading model

Model too big for VRAM. Check `docker compose logs ollama` for the load-time memory layout. Switch to a smaller model or quant per `../README.md` / `docs/RECOMMENDATION.md` in the parent repo.

## Shutting down

```powershell
docker compose down               # stop everything, keep volumes
docker compose down -v            # also delete all data (panic button)
```

`down -v` is the reset button — wipes the DB, model cache, everything.

## Upgrading

```powershell
git pull
docker compose build
docker compose up -d
```

Schema migrations run automatically on Hub startup. The Hub blocks API serving until they apply.

## Developer mode (running Hub on host, agent in container)

For active development on the Hub code:

```powershell
docker compose up -d ollama
cd orchestrai
py -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:OLLAMA_URL = "http://localhost:11434"
$env:DATA_DIR = ".\data"
$env:MASTER_KEY_PATH = "C:\me\.orchestrai\master_key"
uvicorn hub.main:app --reload --host 0.0.0.0 --port 6724
```

Then run an agent in a container pointed at the host: edit `docker-compose.dev.yml` to override `ORCHESTRAI_HUB_URL=http://host.docker.internal:6724` and start just the agent.
