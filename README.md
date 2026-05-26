# ai-local-dev

A discovery + decisions repo for setting up a **local AI coding agent** on consumer hardware. This is where the experiments, the harness, the data, and the final picks all live.

## The decision (TL;DR)

On an **RTX 5080 (16 GB VRAM)** running **Ollama + OpenCode**, use:

| Role | Model | Context |
|---|---|---:|
| **Primary daily driver** | `qwen2.5-coder:14b` (q4_K_M default) | 16384 |
| Long-session fallback | `codegemma:7b-instruct` | 32768 |
| Hard-problem second opinion | `codestral:22b-v0.1-q3_K_S` | 8192 |

These two **Ollama env vars are mandatory** — without them the primary pick runs at ~29 tok/s instead of ~80 tok/s on the same hardware:

```
OLLAMA_FLASH_ATTENTION = 1
OLLAMA_KV_CACHE_TYPE   = q8_0
```

Full rationale, hedges, and known weaknesses: see [`docs/RECOMMENDATION.md`](docs/RECOMMENDATION.md).
How we got here and what surprised us: see [`docs/FINDINGS.md`](docs/FINDINGS.md).

## Hardware context

- GPU: NVIDIA RTX 5080, 16 GB VRAM
- OS: Windows 11
- Inference: Ollama (CUDA backend)
- Coding agent: OpenCode (CLI)

Everything in this repo is calibrated to **16 GB of VRAM**. If your card is different the same harness applies but the answers will move.

## Setup

### One-time install

```powershell
winget install Ollama.Ollama
npm install -g opencode-ai
```

Restart the terminal so `ollama` is on PATH.

### One-time Ollama tuning (the big VRAM unlock)

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_FLASH_ATTENTION", "1",    "User")
[Environment]::SetEnvironmentVariable("OLLAMA_KV_CACHE_TYPE",   "q8_0", "User")
```

Then **restart Ollama** (right-click the tray icon → Quit, then relaunch) so the daemon picks them up. To verify they took effect, the daemon's stderr log should include `OLLAMA_FLASH_ATTENTION:true OLLAMA_KV_CACHE_TYPE:q8_0` on startup.

### Pull the primary model

```powershell
ollama pull qwen2.5-coder:14b
```

### Configure OpenCode to use it

Set `qwen2.5-coder:14b` as your model and `num_ctx: 16384` in the OpenCode config. Optional one-line system prompt that closes the model's known blind spot on boundary conditions:

> When you write code that handles range or interval boundaries (e.g. "touching", "overlapping", "inclusive vs exclusive end"), explicitly think through whether comparisons should be `<` or `<=` and `>` or `>=`. Verify with a small test before claiming done.

## Repo layout

```
.
├── README.md                # this file
├── docs/
│   ├── RECOMMENDATION.md    # what to use, why, settings, hedges
│   └── FINDINGS.md          # discovery journey, surprises, open questions
└── test-harness/            # the benchmark harness we built
    ├── README.md            # operator guide
    ├── bench.py             # CLI: run / quality / runall / compare
    ├── runner.py            # throughput orchestration
    ├── ollama_client.py     # Ollama HTTP wrapper
    ├── monitors/            # GPU, CPU, ollama-ps samplers
    ├── quality/             # quality-suite runner + code executor
    ├── benchmarks/          # configs, plans, prompts, quality problems
    ├── scripts/             # overnight launcher, analysis helpers
    └── results/             # CSVs, raw JSON, log files
```

## Reproducing the experiments

The harness can run any subset of model × context × prompt × quality-problem combinations:

```powershell
# the full overnight matrix (~3-4 hours on this hardware)
.\test-harness\scripts\overnight.ps1

# a one-off probe
py .\test-harness\bench.py run --model qwen2.5-coder:14b --context 16384 --prompt short-code

# the quality suite against one model
py .\test-harness\bench.py quality --model qwen2.5-coder:14b --context 16384

# inspect results
py .\test-harness\bench.py compare
py .\test-harness\bench.py compare --kind quality --summary
```

See [`test-harness/README.md`](test-harness/README.md) for the full operator guide.

## Status

- **Decision**: made (qwen2.5-coder:14b at 16K)
- **Coverage**: 88-cell sweep across 22 models × 4 contexts, complete
- **Known harness limitations**: `parse_csv_row` problem is universally failed by every model — likely too strict, not yet rewritten; see `docs/FINDINGS.md`
- **Open questions**: real long-context performance on actual OpenCode multi-file tasks (vs synthetic prompts) is not yet measured
