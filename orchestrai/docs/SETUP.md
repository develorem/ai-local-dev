# OrchestrAi — Setup

How to get OrchestrAi running on a clean machine. Target host: Windows 11 with an NVIDIA GPU. Linux and Mac work too — Mac without an NVIDIA GPU means CPU-only Ollama, which won't be usable for serious work.

## Prerequisites

| Item | Why | How to install |
|---|---|---|
| **Docker Desktop** (latest stable) | Runs everything | https://www.docker.com/products/docker-desktop |
| **WSL2** (Windows only) | Docker backend that supports GPU passthrough | `wsl --install` from an admin PowerShell, then reboot |
| **NVIDIA driver** (recent, e.g. 555+ on Windows) | Required for CUDA in containers | https://www.nvidia.com/drivers — newer is better, RTX 50-series needs a 555+ driver |
| **Git** | clone this repo | https://git-scm.com/downloads |

That's all. No Python install, no Ollama install, no Node — everything else runs in containers.

## WSL2 tuning (Windows only — important)

WSL2 defaults to ~50% of host RAM and can swap aggressively. For LLM workloads on a 32–64 GB machine, that's not enough. Create `C:\Users\<you>\.wslconfig`:

```ini
[wsl2]
memory=24GB
processors=8
swap=8GB
localhostForwarding=true

[experimental]
autoMemoryReclaim=gradual
```

Adjust `memory` and `processors` to your host capacity:
- Memory: leave ~16 GB for Windows itself, give WSL2 the rest. On 64 GB host → `memory=48GB`. On 32 GB host → `memory=20GB`.
- Processors: leave 4 logical cores for Windows.

After editing, restart WSL: `wsl --shutdown` in PowerShell, then re-open Docker Desktop.

## Verify GPU passthrough works

Before installing OrchestrAi, confirm Docker can see your GPU. From any PowerShell (Docker Desktop must be running):

```powershell
docker run --rm --gpus=all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

Expected: nvidia-smi output showing your GPU, driver version, and a "No running processes found." If you instead see "could not select device driver" or similar, fix that before proceeding. Common fixes:

- Driver too old → update from NVIDIA
- Docker Desktop using Hyper-V backend instead of WSL2 → Settings → General → "Use the WSL 2 based engine"
- WSL2 distro without NVIDIA support → Docker Desktop installs this; if you've customized things, run `wsl --update`

## Clone

```powershell
cd C:\me\dev\github
git clone <orchestrai-repo-url> orchestrai
cd orchestrai
```

(In our repo layout, `orchestrai/` is a subfolder of `ai-local-dev/`. Same idea.)

## First run

```powershell
docker compose up -d
```

What happens:
1. Pulls the `ollama/ollama` image (~600 MB first time)
2. Builds the `orchestrator` image from `Dockerfile.orchestrator` (~200 MB)
3. Builds the `sandbox` image from `Dockerfile.sandbox` (~600 MB — Python + Node + tooling)
4. Starts all three services on the `orchestrai-net` network

First-time setup is 5-10 minutes mostly downloading. Subsequent starts are seconds.

## Pull the primary model

Once the Ollama container is running, pull qwen2.5-coder:14b (the model chosen in our test-harness work):

```powershell
docker compose exec ollama ollama pull qwen2.5-coder:14b
```

This is a ~9 GB download. The model lives in the `ollama-models` named volume and persists across restarts. Pull additional models the same way if you want to experiment (see `docs/RECOMMENDATION.md` at the repo root for picks).

## Verify everything is working

```powershell
# 1. Orchestrator is up
curl http://localhost:8080/api/health
```

Expected:
```json
{
  "status": "ok",
  "version": "0.1.0",
  "ollama": {"reachable": true, "host": "http://ollama:11434"},
  "db": {"schema_version": <N>, "ok": true},
  "worker": {"running": true, "last_picked_at": null, "current_task_id": null}
}
```

```powershell
# 2. Ollama can run the model
docker compose exec ollama ollama run qwen2.5-coder:14b "write a one-line Python function that returns 42"
```

Expected: model loads (~5s first time) and prints a function. If the model loads to CPU instead of GPU, check `docker compose logs ollama` for the env var dump — you should see `OLLAMA_FLASH_ATTENTION:true OLLAMA_KV_CACHE_TYPE:q8_0`. If those aren't set, the `docker-compose.yml` got edited or the image is stale; rebuild.

```powershell
# 3. UI is reachable
start http://localhost:8080
```

Should open a browser tab with the OrchestrAi UI (empty board on first run).

## docker-compose.yml (what you're actually running)

A reference copy lives in the repo root. The relevant moving parts:

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

  orchestrator:
    build:
      context: .
      dockerfile: Dockerfile.orchestrator
    container_name: orchestrai-orchestrator
    depends_on: [ollama]
    environment:
      OLLAMA_URL: http://ollama:11434
      DATA_DIR: /data
    volumes:
      - ./data:/data                                # SQLite + plan docs
      - /var/run/docker.sock:/var/run/docker.sock   # spawn sandbox siblings
    ports:
      - "8080:8080"
    networks: [orchestrai-net]
    restart: unless-stopped

volumes:
  ollama-models:

networks:
  orchestrai-net:
    driver: bridge
```

Note the `sandbox` is *not* a service — it's only built (`docker compose build sandbox` once) and then the orchestrator runs sandbox containers on demand via the host Docker socket.

## Common issues

### "could not select device driver" when starting ollama

GPU passthrough not working. Walk through the "Verify GPU passthrough works" section above with the bare `nvidia/cuda` image. Fix the env first; OrchestrAi inherits whatever you've configured.

### Ollama starts but answers are slow / CPU bound

Run `docker compose logs ollama | grep "inference compute"`. You should see `library=CUDA ... name=CUDA0 ... NVIDIA GeForce RTX 5080`. If you see `library=cpu` instead, GPU passthrough isn't connecting. Re-check the `deploy.resources.reservations.devices` block in compose; it needs `capabilities: [gpu]` exactly.

### Orchestrator can't reach Ollama

From inside the orchestrator container: `curl http://ollama:11434/api/tags`. If that fails, the compose network isn't right. `docker compose down -v` and `docker compose up -d` to recreate.

### WSL2 eating all available memory

Symptom: laptop fan + sluggish Windows. Caused by WSL2 not releasing memory after model load. Check that your `.wslconfig` has `experimental.autoMemoryReclaim=gradual`. If still bad, `wsl --shutdown` and restart Docker Desktop.

### "Cannot connect to the Docker daemon" from inside the orchestrator

The host Docker socket mount isn't working. On Windows, `/var/run/docker.sock` is virtualized; Docker Desktop handles it correctly only when you've installed it (vs an older Docker Toolbox). Reinstall Docker Desktop if you're seeing this.

### Schema migration error on first start

Orchestrator logs will name the failing migration. Usually means the `data/` volume has stale state from a previous version. For development, simplest fix: `docker compose down`, `rm -rf data/orchestrai.db*`, `docker compose up -d`. (Will lose any existing goals — fine for early development, NOT fine once you have real work in there.)

### "Out of memory" loading a model

The model is too big for your VRAM. Check `docker compose logs ollama` for the VRAM math — Ollama prints the model's memory layout when loading. Switch to a smaller model or a smaller quant. See `docs/RECOMMENDATION.md` at the repo root for picks within 16 GB.

## Shutting down

```powershell
docker compose down               # stop services, keep volumes (models, db)
docker compose down -v            # also delete volumes (resets everything)
```

`down -v` is the panic button — it nukes the local state. Useful during development, careful in real use.

## Upgrading

```powershell
git pull
docker compose build
docker compose up -d
```

Migrations apply automatically on startup. If a migration is unsafe (drop column, etc.), the orchestrator refuses to start and logs which migration. You then decide whether to rollback the code or apply the migration manually.

## Building from source (developer mode)

For active development on the orchestrator code:

```powershell
docker compose up -d ollama       # start ollama only
# Run the orchestrator on the host (faster iteration)
cd orchestrai
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn server.main:app --reload --host 0.0.0.0 --port 8080
```

In this mode you need to manually set `OLLAMA_URL=http://localhost:11434` and `DATA_DIR=./data`, and the orchestrator can't spawn sandboxes (no socket access). Tests that don't need sandboxes still run fine. Once you want full integration, go back to `docker compose up -d`.
