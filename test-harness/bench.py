"""CLI entry point for the local LLM benchmark harness.

Usage:
    python bench.py list
    python bench.py run                                                # full throughput matrix
    python bench.py run --config benchmarks/config_models.json         # specific config
    python bench.py run --model qwen2.5-coder:14b --context 16384 --prompt short-code
    python bench.py quality                                            # quality suite
    python bench.py quality --suite benchmarks/quality/suite.json
    python bench.py compare                # throughput results
    python bench.py compare --kind quality # quality results
"""

import argparse
import csv
import json
import os
import sys

from runner import BenchmarkRunner, load_prompt
from ollama_client import OllamaClient

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(ROOT, "benchmarks", "config.json")
PROMPTS_DIR = os.path.join(ROOT, "benchmarks", "prompts")
QUALITY_DIR = os.path.join(ROOT, "benchmarks", "quality")
QUALITY_SUITE_DEFAULT = os.path.join(QUALITY_DIR, "suite.json")
CSV_THROUGHPUT = os.path.join(ROOT, "results", "csv", "runs.csv")
CSV_QUALITY = os.path.join(ROOT, "results", "csv", "quality.csv")


def _check_ollama(host):
    if not OllamaClient(host=host).ping():
        print(f"[error] Ollama not reachable at {host}. Start it with `ollama serve` or check the host URL.", file=sys.stderr)
        return False
    return True


def cmd_run(args):
    if not _check_ollama(args.host):
        sys.exit(2)

    runner = BenchmarkRunner(host=args.host, sample_interval_ms=args.sample_ms)

    if args.model and args.prompt:
        prompt_text = load_prompt(args.prompt)
        print(f"[bench] ad-hoc: {args.model} ctx={args.context} prompt={args.prompt}")
        rec = runner.run_single(
            model=args.model,
            context=args.context,
            prompt_name=args.prompt,
            prompt_text=prompt_text,
        )
        d = rec["derived"]
        print(
            f"   gen={d['gen_tokens_per_sec'] or 0:.2f} tok/s  "
            f"prompt={d['prompt_tokens_per_sec'] or 0:.2f} tok/s  "
            f"wall={d['wall_clock_sec'] or 0:.2f}s"
        )
        return

    if args.model or args.prompt:
        print("[error] --model and --prompt must be given together for ad-hoc runs.", file=sys.stderr)
        sys.exit(2)

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    runner.run_matrix(cfg)


def cmd_quality(args):
    if not _check_ollama(args.host):
        sys.exit(2)
    # Lazy import so plain throughput runs don't pay the import cost.
    from quality.runner import QualityRunner, load_suite

    suite = load_suite(args.suite)
    if args.model and args.context:
        suite = dict(suite)
        suite["matrix"] = [{"model": args.model, "context": args.context}]

    runner = QualityRunner(host=args.host)
    runner.run_suite(suite)


def cmd_runall(args):
    """Execute a master plan: throughput + quality for every (model, context) cell."""
    if not _check_ollama(args.host):
        sys.exit(2)
    from quality.runner import QualityRunner
    import datetime as dt
    import time as _time

    with open(args.plan, "r", encoding="utf-8") as f:
        plan = json.load(f)

    models = plan["models"]
    contexts = plan["contexts"]
    skip_q_below = plan.get("skip_quality_below_context", {})
    skip_t_below = plan.get("skip_throughput_below_context", {})

    if args.only_models:
        wanted = set(args.only_models.split(","))
        models = [m for m in models if m in wanted]
    if args.only_contexts:
        wanted = {int(c) for c in args.only_contexts.split(",")}
        contexts = [c for c in contexts if c in wanted]

    bench_runner = BenchmarkRunner(host=args.host, sample_interval_ms=args.sample_ms)
    qual_runner = QualityRunner(host=args.host)

    total_cells = len(models) * len(contexts)
    start = _time.time()
    print(f"[runall] plan={plan.get('name','?')}  models={len(models)}  contexts={len(contexts)}  cells={total_cells}")
    print(f"[runall] started at {dt.datetime.now().isoformat(timespec='seconds')}")

    summary = []
    for mi, model in enumerate(models, 1):
        for ci, context in enumerate(contexts, 1):
            cell_idx = (mi - 1) * len(contexts) + ci
            elapsed = _time.time() - start
            print()
            print(f"[runall] === cell {cell_idx}/{total_cells}  model={model}  ctx={context}  elapsed={elapsed/60:.1f}m ===")

            # Throughput
            t_prompts = []
            for p in plan.get("throughput_prompts", []):
                name = p["name"] if isinstance(p, dict) else p
                if context < skip_t_below.get(name, 0):
                    print(f"[runall] skip throughput prompt {name} at ctx={context} (< {skip_t_below[name]})")
                    continue
                t_prompts.append(p)
            if t_prompts:
                try:
                    bench_runner.run_matrix({"matrix": [{"model": model, "context": context, "prompts": t_prompts}]})
                except Exception as e:
                    print(f"[runall] throughput ERROR for {model}@{context}: {type(e).__name__}: {e}")

            # Quality
            q_problems = [pid for pid in plan.get("quality_problems", []) if context >= skip_q_below.get(pid, 0)]
            q_tools = list(plan.get("quality_tool_calls", []))
            if q_problems or q_tools:
                try:
                    qual_runner.run_suite({
                        "matrix": [{"model": model, "context": context}],
                        "problems": q_problems,
                        "tool_calls": q_tools,
                    })
                except Exception as e:
                    print(f"[runall] quality ERROR for {model}@{context}: {type(e).__name__}: {e}")

            summary.append({"model": model, "context": context})

    total_elapsed = _time.time() - start
    print()
    print(f"[runall] done at {dt.datetime.now().isoformat(timespec='seconds')}  total={total_elapsed/60:.1f}m  cells_attempted={len(summary)}")


def cmd_list(args):
    print("Throughput prompts:")
    if os.path.isdir(PROMPTS_DIR):
        for name in sorted(os.listdir(PROMPTS_DIR)):
            if name.endswith(".txt"):
                path = os.path.join(PROMPTS_DIR, name)
                size = os.path.getsize(path)
                print(f"  {name[:-4]:<24} ({size} bytes)")
    print()
    print("Throughput matrix:")
    if os.path.exists(args.config):
        with open(args.config, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        for spec in cfg.get("matrix", []):
            prompts = ", ".join(p if isinstance(p, str) else p["name"] for p in spec.get("prompts", []))
            print(f"  {spec['model']:<32} ctx={spec['context']:<6} prompts=[{prompts}]")
    else:
        print(f"  (no config at {args.config})")

    print()
    print("Quality suite:")
    if os.path.exists(args.suite):
        with open(args.suite, "r", encoding="utf-8") as f:
            suite = json.load(f)
        for entry in suite.get("matrix", []):
            print(f"  {entry['model']:<32} ctx={entry['context']}")
        print(f"  problems   ({len(suite.get('problems', []))}): {', '.join(suite.get('problems', []))}")
        print(f"  tool_calls ({len(suite.get('tool_calls', []))}): {', '.join(suite.get('tool_calls', []))}")
    else:
        print(f"  (no suite at {args.suite})")


def cmd_compare(args):
    if args.kind == "quality":
        _compare_quality(args)
    else:
        _compare_throughput(args)


def _compare_throughput(args):
    if not os.path.exists(CSV_THROUGHPUT):
        print("No throughput results yet — run some benchmarks first.")
        return
    with open(CSV_THROUGHPUT, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("No results yet.")
        return
    keys = [
        "timestamp", "model", "context", "prompt_name",
        "gen_tps", "prompt_tps", "wall_clock_sec",
        "gpu_util_avg", "vram_used_max_mb", "ollama_gpu_pct_avg",
        "expected_pass",
    ]
    _print_table(rows[-args.limit:], keys)


def _compare_quality(args):
    if not os.path.exists(CSV_QUALITY):
        print("No quality results yet — run `bench.py quality` first.")
        return
    with open(CSV_QUALITY, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("No quality results yet.")
        return

    if args.summary:
        # Aggregate by (model, context) within the last N rows.
        agg = {}
        for r in rows[-args.limit:]:
            key = (r["model"], r["context"])
            d = agg.setdefault(key, {"code_pass": 0, "code_n": 0, "tool_pass": 0, "tool_n": 0,
                                     "tps_sum": 0.0, "tps_n": 0})
            if r["kind"] == "code":
                d["code_n"] += 1
                d["code_pass"] += 1 if r["pass"] in ("True", True, "true") else 0
            else:
                d["tool_n"] += 1
                d["tool_pass"] += 1 if r["pass"] in ("True", True, "true") else 0
            try:
                d["tps_sum"] += float(r.get("gen_tps") or 0)
                d["tps_n"] += 1 if r.get("gen_tps") else 0
            except ValueError:
                pass

        keys = ["model", "context", "code", "tool_call", "gen_tps_avg"]
        out_rows = []
        for (m, c), d in agg.items():
            out_rows.append({
                "model": m,
                "context": c,
                "code": f"{d['code_pass']}/{d['code_n']}",
                "tool_call": f"{d['tool_pass']}/{d['tool_n']}",
                "gen_tps_avg": f"{d['tps_sum']/d['tps_n']:.1f}" if d["tps_n"] else "",
            })
        _print_table(out_rows, keys)
        return

    keys = ["timestamp", "model", "context", "kind", "problem_id", "pass", "gen_tps", "wall_sec", "details"]
    _print_table(rows[-args.limit:], keys, trunc={"details": 60})


def _print_table(rows, keys, trunc=None):
    trunc = trunc or {}
    def cell(r, k):
        v = str(r.get(k, "") or "")
        m = trunc.get(k)
        if m and len(v) > m:
            return v[: m - 1] + "…"
        return v
    widths = {k: max(len(k), max(len(cell(r, k)) for r in rows)) for k in keys}
    print("  ".join(k.ljust(widths[k]) for k in keys))
    print("  ".join("-" * widths[k] for k in keys))
    for r in rows:
        print("  ".join(cell(r, k).ljust(widths[k]) for k in keys))


def main():
    p = argparse.ArgumentParser(prog="bench", description="Local LLM benchmark harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="Run throughput matrix (default) or a single ad-hoc benchmark")
    pr.add_argument("--config", default=CONFIG_PATH)
    pr.add_argument("--host", default="http://localhost:11434")
    pr.add_argument("--sample-ms", dest="sample_ms", type=int, default=250)
    pr.add_argument("--model", help="Override: ad-hoc model (requires --prompt)")
    pr.add_argument("--context", type=int, default=16384)
    pr.add_argument("--prompt", help="Override: prompt name without .txt (requires --model)")
    pr.set_defaults(func=cmd_run)

    pq = sub.add_parser("quality", help="Run the quality suite (coding problems + tool-call reliability)")
    pq.add_argument("--suite", default=QUALITY_SUITE_DEFAULT)
    pq.add_argument("--host", default="http://localhost:11434")
    pq.add_argument("--model", help="Override the suite matrix and run a single (model, context)")
    pq.add_argument("--context", type=int)
    pq.set_defaults(func=cmd_quality)

    pa = sub.add_parser("runall", help="Run a master plan: throughput + quality across all (model, context) cells")
    pa.add_argument("--plan", default=os.path.join(ROOT, "benchmarks", "plan_overnight.json"))
    pa.add_argument("--host", default="http://localhost:11434")
    pa.add_argument("--sample-ms", dest="sample_ms", type=int, default=250)
    pa.add_argument("--only-models", help="Comma-separated subset of model tags to run")
    pa.add_argument("--only-contexts", help="Comma-separated subset of context sizes to run")
    pa.set_defaults(func=cmd_runall)

    pl = sub.add_parser("list", help="List throughput prompts, throughput matrix, and quality suite")
    pl.add_argument("--config", default=CONFIG_PATH)
    pl.add_argument("--suite", default=QUALITY_SUITE_DEFAULT)
    pl.set_defaults(func=cmd_list)

    pc = sub.add_parser("compare", help="Print rows of runs.csv or quality.csv")
    pc.add_argument("--kind", choices=["throughput", "quality"], default="throughput")
    pc.add_argument("--limit", type=int, default=40)
    pc.add_argument("--summary", action="store_true",
                    help="With --kind quality, aggregate pass rates per (model, context)")
    pc.set_defaults(func=cmd_compare)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
