# Model recommendation

For a local AI coding agent on an **RTX 5080 (16 GB VRAM)** doing full-stack work — building applications, CI/CD pipelines, infrastructure as code, unit and integration tests, running and verifying changes.

## The primary pick

**`qwen2.5-coder:14b`** at `num_ctx=16384`.

The default q4_K_M quant — do **not** use higher quants (see "Anti-recommendations" below).

### Why this and not the alternatives

The workload is **agentic**: read files, write code, run tests, observe output, iterate. The constraints that actually matter for that loop, in priority order:

| # | Constraint | Why for full-app dev | qwen 14B |
|---|---|---|---|
| 1 | Tool-call reliability | One failed tool call breaks the agent loop | **5/5** ✅ |
| 2 | Stable 100% GPU at 16K | Multi-file work + tool history accumulates fast → 16K is where you live | **100% GPU @ 16K** ✅ |
| 3 | Iteration speed at 16K | Long sessions = thousands of generations; sluggishness compounds | **80 tok/s** ✅ |
| 4 | Reasoning depth | IaC + CI/CD + tests = cross-file logic | dense **14B** ✅ |
| 5 | Code accuracy | Bad code wastes tool-call cycles | 6/9 in tests, effectively 7/9 with long-context retest ✅ |

Other finalists fail on one of these:

- **`deepseek-coder-v2:16b`** is 3× faster at 8K (267 tok/s) but **spills to CPU at 16K** (drops to 55 tok/s, 87% GPU). In real agent sessions you hit 16K within a few turns. Unpredictable latency in the tool loop is worse than slower-but-stable.
- **`codestral:22b-v0.1-q3_K_S`** has the best code accuracy on paper (7–8/9 effective) — but **34 tok/s at 16K and 8 tok/s at 32K**. Iteration loops feel sluggish. Keep as a hedge (below).
- **`codegemma:7b-instruct`** is the only model that stays 134 tok/s **flat** at 32K — but 7B has a real reasoning ceiling that will show up on hard multi-file IaC or non-obvious integration tests. Keep as a hedge (below).
- **`qwen2.5-coder:7b`** matches 14B on most problems at twice the speed — but loses one tool-call problem (`multi_step_plan`), and 4/5 tool reliability is a no-go for sustained agent work.

## Required Ollama env vars

These are not optional. They turned the primary pick from "29 tok/s, spilling to CPU" into "80 tok/s, 100% GPU" on the same hardware:

```
OLLAMA_FLASH_ATTENTION = 1
OLLAMA_KV_CACHE_TYPE   = q8_0
```

Set at User scope, restart Ollama. The daemon's startup log should show both as enabled.

## OpenCode configuration

```
model:    qwen2.5-coder:14b
num_ctx:  16384
options.temperature: 0     # determinism for coding; raise to ~0.2 if you want some variety
```

### Recommended system-prompt addition

The 14B has a specific blind spot: it consistently uses `<` instead of `<=` (or vice versa) on touching/overlapping range boundaries. It missed `merge_intervals` 100% of the time in our tests, where every other agent-grade model got it right. This one-liner mostly closes the gap:

> When you write code that handles range or interval boundaries (e.g. "touching", "overlapping", "inclusive vs exclusive end"), explicitly think through whether comparisons should be `<` or `<=` and `>` or `>=`. Verify with a small test before claiming done.

## Hedge models

OpenCode lets you switch the model per task. Pull both, name them in your config as alternates:

### Hedge 1: long sessions blow past 16K

```
ollama pull codegemma:7b-instruct
```

Use at `num_ctx=32768`. 134 tok/s flat across all contexts, 100% GPU, 5/5 tool. Code quality is one step below qwen 14B but better than acceptable for routine work. Switch to it when a single OpenCode session is genuinely accumulating beyond 16K of working context.

### Hedge 2: "I'm stuck, give me a deeper opinion"

```
ollama pull codestral:22b-v0.1-q3_K_S
```

Use at `num_ctx=8192`. 70 tok/s, 100% GPU, top code accuracy (7/9 in our suite; 8/9 with the long-context retest). Reach for it when qwen 14B produces wrong code twice in a row on the same task — codestral may catch the subtlety qwen misses. Don't make it the default; the latency is noticeable.

## Anti-recommendations

What **not** to use, and why:

| Model / setting | Why not |
|---|---|
| `qwen2.5-coder:14b-instruct-q5_K_M` / `q6_K` / `q8_0` | **Zero quality improvement** over q4_K_M in our suite, but 3–11× slower because of CPU spillover. Higher quants are wasted VRAM on this family. |
| `qwen2.5-coder:14b` at `num_ctx=32768` | Cliff: 80 → 16 tok/s, GPU drops to 80%. Use 16K or move to a smaller model for long contexts. |
| `codestral:22b` (default q4) | 11 tok/s at 16K. Strictly dominated by the `q3_K_S` variant, which is faster AND scores same on quality. |
| `deepseek-coder-v2:16b` at ≥16K | Spills to CPU. Use it at 4K–8K or switch model. |
| `starcoder2:7b` / `starcoder2:15b` | **0/9 code, 1–2/5 tool**. Pure code-completion model; doesn't follow instructions. Unsuitable as an agent. |
| `phi3.5:3.8b` | Fast (225 tok/s) but only 2–3/9 code, 2–3/5 tool. Too small for non-trivial coding work. |
| `qwen3-coder-next` (80B) | ~3× too large for 16 GB VRAM. Heavy CPU spillover ruins throughput. |

## Verification commands

After setup, sanity-check that the primary pick really runs as documented:

```powershell
# Should report ~80 tok/s with ollama_gpu_pct_avg = 100
py .\test-harness\bench.py run --model qwen2.5-coder:14b --context 16384 --prompt short-code

# Should report code 6/9 (or 7/9 with the fixed long_module_bug prompt) and tool 5/5
py .\test-harness\bench.py quality --model qwen2.5-coder:14b --context 16384
```

If the throughput is closer to 29 tok/s with `ollama_gpu_pct_avg` ≈ 92, **the env vars are not being read by the Ollama daemon** — see `docs/FINDINGS.md` "The VRAM unlock" for the gotcha.

## What's still uncertain

Honest caveats:

- These rankings come from a synthetic benchmark suite (8 self-contained coding problems + 5 tool-call problems + 1 long-context bug-find). They are **not** an end-to-end test of OpenCode building a real application. Real-world performance on cross-file work could move things by ±15%.
- The merge_intervals system-prompt workaround is logical but unproven. We haven't measured whether it actually closes the gap in practice.
- `parse_csv_row` failed 0/88 across all models in our suite. This is more likely a problem-design issue (too strict on "no imports" + doubled-quote escape) than a universal capability gap.
- The 32K context tier is only thinly characterized; OpenCode session-scale validation is open work.

See `docs/FINDINGS.md` for what we learned and what remains uncertain.
