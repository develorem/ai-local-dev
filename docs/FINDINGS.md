# Findings

What we learned while building and exercising the local-agent harness on an RTX 5080 (16 GB). This document is for future-me coming back in 3 months trying to remember why we picked what we picked.

## The thing that mattered most

**Flash attention + KV cache quantization is the single biggest win on a 16 GB card.** Two Ollama daemon env vars:

```
OLLAMA_FLASH_ATTENTION = 1
OLLAMA_KV_CACHE_TYPE   = q8_0
```

Effect on the primary model, `qwen2.5-coder:14b` at ctx=16384, all else equal:

| Setting | tok/s | GPU split |
|---|---:|---:|
| Defaults | 29 | 92% (8% CPU spill) |
| Flash-attn + KV-q8 | **80** | **100%** |

That's a **2.8× speedup** for free. Without these, the entire model-selection question changes because everything bigger than 7B spills at 16K. The gotcha: the env vars **must be present in the Ollama daemon's environment** at launch. Setting them via `[Environment]::SetEnvironmentVariable("...", "...", "User")` writes the registry, but **the running Ollama process won't pick them up** — you have to fully restart the daemon, and even then the tray-app launch flow on Windows can ignore them. Most reliable approach: kill the tray, set `$env:OLLAMA_*` in the shell, `Start-Process ollama serve` from that shell. The daemon's stderr log on startup will print the config map; `OLLAMA_FLASH_ATTENTION:true OLLAMA_KV_CACHE_TYPE:q8_0` must appear, otherwise the env vars are not active.

## The hardware constraint dictates everything

A 16 GB card has roughly **14 GB of usable headroom** (Windows + driver eat ~2 GB) for model weights AND KV cache combined. The KV cache grows linearly with context. At 16K context with flash-attn + q8 KV, a 14B-class model (~9 GB weights) uses about another 4–5 GB of KV — fitting just under the limit. Push to 32K context and the KV cache doubles, forcing layers out to system RAM. Even a small spill (8% on CPU) costs **65% of throughput**. The cliff is sharp.

This single constraint explains every ranking in the suite:

- Models ≤ 9 GB at q4 fit fully on GPU at 16K → fast.
- Models 9–12 GB fit at 8K but spill at 16K → fast at short context, broken at long.
- Models > 12 GB spill at any context → unusable as a daily driver.
- Going to higher quants of an already-fitting model adds VRAM cost for no quality gain (see "Quantization is wasted VRAM" below).

## The context cliff per model (post flash-attn)

Generation tokens/sec at each context, fastest cell per model:

| Model | 4K | 8K | 16K | 32K |
|---|---:|---:|---:|---:|
| qwen2.5-coder:7b (q4) | 158 | 157 | 158 | **157** |
| codegemma:7b-instruct | 136 | 135 | 134 | **135** |
| granite-code:8b | 147 | 145 | 144 | **146** |
| qwen2.5-coder:7b-q8_0 | 102 | 100 | 101 | **100** |
| qwen2.5-coder:14b (q4) | 81 | 80 | 80 | 17 (cliff at 32K) |
| codestral:22b-q3_K_S | 70 | 69 | 34 (cliff at 16K) | 12 |
| codestral:22b-q3_K_M | 66 | 65 | 20 | 10 |
| deepseek-coder-v2:16b | **281** | **268** | 55 (cliff at 16K) | 38 |
| codestral:22b (default q4) | 23 | 18 | 11 | 7 |
| qwen2.5-coder:14b-q5_K_M | 73 | 74 | 28 (cliff at 16K) | 13 |

Three regimes are visible:

1. **Stays flat** at all contexts (≤ 8 GB models): the 7B class and granite 8B. These never spill.
2. **Stays flat through 16K** (9–10 GB models at q4): qwen2.5-coder:14b. The cliff is at 32K.
3. **Cliff at 16K** (> 10 GB models or higher quants of medium models): everything else.

## Quantization is wasted VRAM on the qwen2.5-coder family

We swept qwen2.5-coder:14b through q4_K_M, q5_K_M, q6_K, q8_0. Code accuracy and tool reliability at every quant: **identical**. 6/9 code and 5/5 tool, every single quant, every context. But:

| Quant | Size | tok/s @ 16K | GPU split |
|---|---:|---:|---:|
| q4_K_M | 9 GB | 83 | 100% |
| q5_K_M | 10 GB | 28 | 90% (spill) |
| q6_K | 12 GB | 18 | 83% (spill) |
| q8_0 | 15 GB | 8 | 69% (spill) |

So going from q4 → q8 costs **10× the speed for 0% quality lift**. The "more bits = better answers" intuition is wrong on this model family, at least for the kind of problems we tested. Always use the default q4_K_M.

Same story for `qwen2.5-coder:7b`: q4_K_M (155 t/s) versus q8_0 (100 t/s), identical scores.

## Smaller quants of LARGER models can win

Inverse of the previous finding. `codestral:22b` at its default q4 is 12 GB — spills at every context, runs at 11 tok/s at 16K. But `codestral:22b-v0.1-q3_K_S` is 13 GB → fits at 8K, runs at 70 tok/s, and scores **the same or better** on code (7/9 vs 6–7/9 default). The lossy quant didn't cost anything we could measure but it freed enough VRAM to keep the model on GPU.

Rule of thumb: when a model is JUST too big to fit, **try the next quant down before giving up on it**.

## Tool-call reliability separates agents from completion models

Of 22 models tested, only these score **5/5** on the tool-call suite (the threshold for "agent-grade"):

- codegemma:7b-instruct
- codellama:13b-instruct
- entire codestral family
- entire deepseek-coder-v2 family
- entire qwen2.5-coder:14b family

4/5 (loses `multi_step_plan`):
- granite-code:8b
- entire qwen2.5-coder:7b family

2–3/5:
- phi3.5:3.8b
- starcoder2:7b, starcoder2:15b

`starcoder2` is a stark surprise: 0/9 on code, 1–2/5 on tool. It's a pure code-completion model that doesn't follow "write a function that does X" instructions — it just continues whatever text you give it. Don't even consider it for agent work, regardless of size or speed.

## merge_intervals is the model-quality differentiator

Of the 8 code problems we tested, the meaningful differentiator turned out to be a single one: `merge_intervals` (combine overlapping/touching intervals where touching ranges should merge). Pass rate by family:

- 100%: codegemma, granite-code, codestral q3 quants, deepseek q5 lite
- 50–75%: codestral defaults, codellama, deepseek default/q8
- **0%**: every qwen2.5-coder variant (7B + 14B, every quant we tested)
- 0%: starcoder2, phi3.5 (general weakness)

The qwen family consistently writes `if start < end:` where the correct logic is `if start <= end:` — a clear off-by-one on the boundary case. Quantization doesn't change this. Context size doesn't change this. The model just has this specific spec-comprehension blind spot.

This is salvageable in real agent use because:
1. Test feedback can catch wrong code on the first run.
2. A short system prompt asking the model to think explicitly about boundary semantics seems likely to close the gap, though we haven't formally verified.

## DeepSeek-Coder-V2:16b is a MoE in disguise

At 4K and 8K context, `deepseek-coder-v2:16b` runs at **267–281 tok/s** — faster than any 7B model we tested. The reason: it's a Mixture-of-Experts model with 16B total parameters but only ~2.4B active per token. The full weights still need to fit in VRAM (8.9 GB), and they do at 4K/8K, but the per-token compute is small. Result: speed of a 3B with the headroom of a 16B.

At 16K context the KV cache pushes a few layers to CPU (87% GPU), throughput falls to 55 tok/s. Still excellent but the speed advantage vanishes. At 32K it's 63% GPU and 38 tok/s — no longer worth running over alternatives.

The "lite-instruct" tags with explicit quants (`q5_K_M`, `q6_K`, `q8_0`) are different variants — slower (33–64 tok/s at 4K) but better at `merge_intervals` (100% pass on q5). Pick depends on use case.

## Long-context capability is harder to measure than expected

We built a `long_module_bug` problem: a ~13 KB rate-limiter module with one off-by-one bug. The model must read the whole module, find the bug, and produce a corrected single function. First-run result: **0/66 passes across all models**.

Investigation revealed the harness bug, not a model failure: the runner only prepended the module to the test execution, **never to the model's prompt**. The model was being asked "fix the bug in this module" while only being shown a description of the module. Naturally every model invented a class structure (because the description mentioned classes) and produced `def is_throttled(self, now)` — wrong signature.

After fixing the harness to actually substitute the module text into the prompt: **10/10 passes** on the top-5 finalists at 16K AND 32K. So:
- Long-context bug-find IS achievable by all agent-grade models we tested.
- The 0/66 told us nothing about capability — it told us about prompt engineering.
- Lesson: when every model fails a problem, suspect the problem before the models.

## Other gotchas

### Python output buffering vs detached overnight runs

`py bench.py runall` writes per-cell CSV rows that flush atomically, but its stdout to a redirected log file **buffers in 4 KB chunks**. During a long overnight run the live log may look empty for the first few cells. The CSVs are the source of truth; the log is for debugging. Future me: use `py -u` for unbuffered if you want real-time log tailing.

### Windows + PowerShell execution policy

Running `.\overnight.ps1` directly is blocked by default execution policy. Use:
```powershell
powershell -ExecutionPolicy Bypass -File .\test-harness\scripts\overnight.ps1
```
Or inline the commands. Don't change the system execution policy — too broad.

### The `localhost` vs `127.0.0.1` Ollama startup race

When restarting the Ollama daemon from a PowerShell session, there's a 30–60 second window where `Invoke-WebRequest http://localhost:11434/api/tags` times out even though the daemon is listening. The first iteration of `overnight.ps1` died here. Workaround: bump the API-check retry window to 60+ attempts and/or use `127.0.0.1` directly. The daemon does come up; the polling just needs longer than expected.

### `ollama ps` output format changes when fully on GPU

The default Ollama parsing assumes "12%/88% CPU/GPU" format, but when the model is 100% GPU-resident it prints just "100% GPU" with no split. The harness now handles both forms — but if you write your own monitoring code, watch for this.

## What's still uncertain

Honest list of things this repo's data doesn't answer:

- **Real OpenCode workload behavior.** Our suite is 13 self-contained problems plus 5 tool-call problems. Building an actual application with OpenCode involves multi-file context, accumulated tool history, and longer turns. Rankings could move ±15% on real work. The natural next experiment is to define a "fix a known bug in this real repo" task and run OpenCode end-to-end with each finalist; we haven't built that yet.
- **Does the system-prompt nudge actually fix the qwen boundary blind spot?** Logical hypothesis, no measurement.
- **`parse_csv_row` failing 0/88.** Likely the problem is too strict (no `csv` module + doubled-quote escape from scratch is hard). Could be re-written to test something subtler. Currently it doesn't differentiate models because everyone fails.
- **Bigger MoE models we haven't tried.** `mixtral:8x7b` at smaller quants might fit and surprise. Not in our matrix.
- **Newer / unreleased model families.** Whatever comes out next month isn't in here. Re-run the harness when something interesting drops.
- **OpenCode-specific tool-calling.** Our tool-call problems ask the model to emit JSON describing tool calls. OpenCode uses Ollama's native `/api/chat` tool-calling protocol, which is slightly different. Should re-test on that protocol before fully trusting our 5/5 scores.

## Chronology of discoveries

For anyone reconstructing the journey:

1. **Bootstrap**: Initial attempt with `qwen3-coder-next:80b` — too large for 16 GB, heavy CPU spill, abandoned.
2. **Baseline**: `qwen2.5-coder:14b` at 8K/16K/32K shows context cliff (82 → 29 → 12 tok/s) on the default `OLLAMA_*` config.
3. **Quality suite built**: 8 self-contained Python problems + 5 tool-call problems.
4. **Model sweep**: at 16K, qwen 14B scores 6/8 code, 5/5 tool. deepseek-coder-v2 is the surprise top scorer at 7/8 but spills heavily.
5. **Quant sweep**: qwen 14B at q4/q5/q6/q8 all score identically — quantization is not a quality lever for this family.
6. **The VRAM unlock**: flash-attn + KV-q8 turns 14B@16K from 29 tok/s into 80 tok/s. Re-baseline everything.
7. **Overnight matrix**: 22 models × 4 contexts × (throughput + quality) = 88 cells, 3h 22m total.
8. **The long-context test was lying**: 0/66 on `long_module_bug`. Investigation found the harness bug (module text never reached the model's prompt). Fix made, re-tested top 5 — all pass at 16K and 32K.
9. **Decision**: qwen2.5-coder:14b at 16K for primary; codegemma:7b at 32K and codestral:22b-q3_K_S at 8K as hedges.
