# test-harness — operator guide

Python benchmark harness for evaluating local LLMs as coding agents on Ollama. Captures throughput, GPU/CPU/VRAM usage, code-execution pass rates, and tool-call reliability across arbitrary (model × context × quant × prompt) matrices.

For **what to use and why**, see [`../docs/RECOMMENDATION.md`](../docs/RECOMMENDATION.md).
For **what we learned**, see [`../docs/FINDINGS.md`](../docs/FINDINGS.md).
This document is just the operator manual.

## Layout

```
test-harness/
├── bench.py                  # CLI entry — subcommands: run | quality | runall | compare | list
├── runner.py                 # Throughput orchestration (per-cell)
├── ollama_client.py          # stdlib HTTP wrapper for /api/generate
├── monitors/
│   ├── gpu_monitor.py        # background nvidia-smi sampler
│   ├── ollama_monitor.py     # background `ollama ps` sampler
│   └── cpu_monitor.py        # psutil CPU + RAM sampler
├── quality/
│   ├── runner.py             # quality-suite orchestration
│   ├── code_executor.py      # subprocess sandbox + timeout
│   └── response_parser.py    # extracts code / JSON from model responses
├── benchmarks/
│   ├── config.json           # default throughput matrix (legacy)
│   ├── config_models.json    # model sweep at fixed 16K
│   ├── config_quants.json    # quant sweep at fixed 16K
│   ├── plan_overnight.json   # the full matrix (used by overnight.ps1)
│   ├── plan_smoke.json       # tiny plan to validate runall
│   ├── prompts/              # throughput prompts (*.txt)
│   └── quality/
│       ├── suite.json        # default quality suite
│       ├── suite_models.json # quality model sweep
│       ├── suite_quants.json # quality quant sweep
│       ├── suite_long_module_retest.json  # targeted retest after long-context fix
│       ├── suite_smoke.json  # smallest valid suite
│       ├── problems/         # code problems (*.json)
│       └── tool_calls/       # tool-call problems (*.json)
├── scripts/
│   ├── overnight.ps1         # headless launcher: stops tray, starts ollama serve, runs plan
│   ├── setup-ollama-vram.ps1 # one-shot env-var setup
│   ├── analyze.py            # aggregates runs.csv + quality.csv per (model, ctx)
│   └── analyze_problems.py   # per-problem breakdown
└── results/
    ├── csv/
    │   ├── runs.csv          # one row per throughput cell
    │   └── quality.csv       # one row per quality problem
    ├── raw/                  # per-run JSON (timings, monitor samples, response text)
    └── overnight_*.log       # overnight run stdout (when launched headless)
```

## Setup

One-time:

```powershell
py -m pip install psutil
```

Verify Ollama is reachable at `http://localhost:11434`:

```powershell
(Invoke-WebRequest -Uri http://localhost:11434/api/tags -UseBasicParsing).StatusCode
# expect: 200
```

Set the VRAM-unlock env vars (see top-level `README.md`). The harness assumes they are active on the daemon.

## CLI

### List available prompts, models in matrix, and quality suite

```powershell
py bench.py list
```

### Throughput: ad-hoc single cell

```powershell
py bench.py run --model qwen2.5-coder:14b --context 16384 --prompt short-code
```

### Throughput: a configured matrix

```powershell
py bench.py run                                          # default config.json
py bench.py run --config benchmarks\config_models.json   # model sweep
py bench.py run --config benchmarks\config_quants.json   # quant sweep
```

### Quality: ad-hoc single (model, context)

```powershell
py bench.py quality --model qwen2.5-coder:14b --context 16384
```

### Quality: a configured suite

```powershell
py bench.py quality                                          # default suite.json
py bench.py quality --suite benchmarks\quality\suite_models.json
```

### The full overnight matrix (throughput + quality, every cell)

```powershell
py bench.py runall                                       # uses plan_overnight.json
py bench.py runall --plan benchmarks\plan_smoke.json     # quick validation
py bench.py runall --only-models qwen2.5-coder:14b --only-contexts 16384,32768
```

To run end-to-end with a headless Ollama daemon:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\overnight.ps1
```

### Compare results

```powershell
py bench.py compare                            # throughput, last 40 rows
py bench.py compare --limit 10
py bench.py compare --kind quality             # per-row quality table
py bench.py compare --kind quality --summary   # aggregated pass rates per (model, ctx)
```

### Analysis helpers (do not require the CLI)

```powershell
py scripts\analyze.py            # full cross-reference table + top-by-composite-score
py scripts\analyze_problems.py   # per-problem pass rates + per-model breakdown for specific problems
```

## Adding things

### A new model or quant to the overnight matrix

Edit `benchmarks/plan_overnight.json`, add the tag string to the `models` array. The harness will:
- attempt to run every cell with every context
- skip the cell with a logged error if the tag returns 404 (i.e. not pulled)

### A new context size

Edit `benchmarks/plan_overnight.json`'s `contexts` array. If you add a very small context (e.g. 2048), use the `skip_throughput_below_context` and `skip_quality_below_context` maps to exclude prompts/problems that wouldn't fit.

### A new throughput prompt

Drop a `.txt` file under `benchmarks/prompts/`. The filename (without `.txt`) is the prompt's name. Then reference it in any throughput config's `prompts` array, optionally with an `expected_contains` substring check:

```json
{"name": "my-new-prompt", "expected_contains": ["def "]}
```

### A new code-quality problem

Drop a `.json` file under `benchmarks/quality/problems/`. Schema:

```json
{
  "id": "problem_name",
  "max_tokens": 1024,
  "timeout_sec": 10,
  "prompt": "...the question to send to the model...",
  "tests": [
    "assert my_function(input) == expected",
    "..."
  ]
}
```

Optional fields:
- `prefix_code`: a string of Python that's prepended both to the model's prompt (via `{prefix_code}` placeholder substitution) AND to the test execution. Used for "fix this bug in this module" problems.

Then add the problem id to whichever suite you want it in (`suite.json` etc.).

### A new tool-call problem

Drop a `.json` file under `benchmarks/quality/tool_calls/`. Schema:

```json
{
  "id": "problem_name",
  "max_tokens": 256,
  "prompt": "...question that asks for JSON tool-call output...",
  "checks": {
    "must_have_keys": ["tool", "args"],   // top-level keys required
    "tool_in": ["read_file", "grep"],     // tool field must be one of these
    "args_required": ["path"],            // args dict must include these keys
    "must_call_in_order": ["list_files", "read_file"],   // for multi-step
    "must_refuse": true,                  // for refusal tests
    "forbidden_tools": ["delete_file"]    // tools that must NOT appear
  }
}
```

All `checks` are optional; combine the ones relevant to your test.

## Result formats

### `results/csv/runs.csv` (throughput)

One row per (model, context, prompt) cell:

| column | meaning |
|---|---|
| `run_id`, `timestamp`, `model`, `context`, `prompt_name` | identity |
| `gen_tps`, `prompt_tps` | tokens/sec (generation, prompt eval) |
| `wall_clock_sec`, `load_duration_sec` | timings |
| `gpu_util_avg`, `gpu_util_max` | sampled from nvidia-smi during the run |
| `vram_used_max_mb`, `gpu_temp_max` | peak VRAM + GPU temp |
| `cpu_util_avg`, `cpu_util_max` | from psutil |
| `ollama_cpu_pct_avg`, `ollama_gpu_pct_avg` | parsed from `ollama ps` PROCESSOR column |
| `eval_count`, `prompt_eval_count` | tokens generated / consumed |
| `response_chars` | length of the model's response |
| `expected_pass` | True/False/blank — substring check from prompt config |

### `results/csv/quality.csv` (correctness)

One row per (model, context, problem):

| column | meaning |
|---|---|
| `run_id`, `timestamp`, `model`, `context` | identity |
| `kind` | `code` or `tool_call` |
| `problem_id` | which problem |
| `pass` | True/False |
| `details` | for code: stderr tail on fail; for tool: JSON of checks dict |
| `gen_tps`, `wall_sec`, `response_chars` | per-problem perf |

### `results/raw/*.json`

Full per-cell data: every monitor sample (GPU at 250ms, ollama ps at 1s, CPU at 250ms), the full model response text, all timings. Used for deep-diving anomalies.

## Known harness issues

Documented in detail in `../docs/FINDINGS.md`. Quick summary:

1. **`parse_csv_row` problem is unbeatable.** Currently failed by every model in the matrix. The "no `csv` module + doubled-quote escape from scratch" combination appears too strict. Not yet rewritten.
2. **`long_module_bug` had a prompt substitution bug** that made it un-passable for all models in the overnight run. Fixed on 2026-05-27; the targeted retest showed 10/10 passes on the top 5 finalists. The overnight CSV's `long_module_bug` rows are stale — re-run the suite if you need a clean number.
3. **Empty-list filter quirk:** `quality_runner.run_suite` treats `"tool_calls": []` as "no filter" instead of "no tool-calls", so a quality suite with empty tool_calls list will still run all of them. Cosmetic; matters only if you intentionally want zero tool calls. Workaround: don't include the key at all.
4. **Stdout buffering** in `py bench.py runall` makes redirected log files look empty for the first few minutes of a long run. The CSVs are the source of truth. Use `py -u bench.py runall ...` if you want real-time log tailing.
5. **The `overnight.ps1` API-check loop** had timing issues that aborted the script even when Ollama was actually up. Workaround: launch `py bench.py runall` directly against an already-running daemon instead of relying on the script to start one. See `docs/FINDINGS.md` "Other gotchas".

## When to re-run the overnight matrix

- New model family released that you want to evaluate
- Major Ollama version bump (could change quantization behavior, flash-attn implementation)
- Hardware change
- You changed a problem definition (re-baseline all models on the new problem)

To re-run cleanly: archive the existing CSVs to `results/archive_<date>/` first, otherwise the new rows mix with the old.
