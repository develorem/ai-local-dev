"""Overnight results analyzer — aggregates throughput + quality CSVs into a single ranked table.

Filters to overnight rows (timestamp >= 2026-05-26T21:56).
"""

import csv
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_THROUGHPUT = os.path.join(ROOT, "results", "csv", "runs.csv")
CSV_QUALITY = os.path.join(ROOT, "results", "csv", "quality.csv")
OVERNIGHT_AFTER = "2026-05-26T21:56"


def load(path):
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def overnight(r):
    return r.get("timestamp", "") >= OVERNIGHT_AFTER


def main():
    runs = [r for r in load(CSV_THROUGHPUT) if overnight(r)]
    qual = [r for r in load(CSV_QUALITY) if overnight(r)]

    agg = defaultdict(lambda: {
        "tps": [], "prompt_tps": [], "ollama_gpu": [], "vram": [],
        "code_pass": 0, "code_n": 0,
        "tool_pass": 0, "tool_n": 0,
        "long_pass": None,
    })

    for r in runs:
        key = (r["model"], int(r["context"]))
        try:
            agg[key]["tps"].append(float(r["gen_tps"]))
            if r["prompt_tps"]:
                agg[key]["prompt_tps"].append(float(r["prompt_tps"]))
            if r["ollama_gpu_pct_avg"]:
                agg[key]["ollama_gpu"].append(float(r["ollama_gpu_pct_avg"]))
            if r["vram_used_max_mb"]:
                agg[key]["vram"].append(float(r["vram_used_max_mb"]))
        except (ValueError, KeyError):
            pass

    for r in qual:
        key = (r["model"], int(r["context"]))
        is_pass = r["pass"] in ("True", "true", True)
        if r["kind"] == "code":
            agg[key]["code_n"] += 1
            if is_pass:
                agg[key]["code_pass"] += 1
            if r["problem_id"] == "long_module_bug":
                agg[key]["long_pass"] = is_pass
        else:
            agg[key]["tool_n"] += 1
            if is_pass:
                agg[key]["tool_pass"] += 1

    print(f"{'model':<40} {'ctx':>5}  {'tps':>6}  {'gpu%':>5}  {'vram':>5}  {'code':>5}  {'tool':>5}  long")
    print("-" * 92)
    for (model, ctx), v in sorted(agg.items()):
        tps = sum(v["tps"]) / len(v["tps"]) if v["tps"] else 0
        gpu = sum(v["ollama_gpu"]) / len(v["ollama_gpu"]) if v["ollama_gpu"] else None
        vram = max(v["vram"]) if v["vram"] else 0
        code = f"{v['code_pass']}/{v['code_n']}" if v["code_n"] else "-"
        tool = f"{v['tool_pass']}/{v['tool_n']}" if v["tool_n"] else "-"
        long = "P" if v["long_pass"] is True else ("F" if v["long_pass"] is False else "-")
        gpu_s = f"{gpu:.0f}" if gpu is not None else "100"
        print(f"{model:<40} {ctx:>5}  {tps:>6.1f}  {gpu_s:>5}  {vram:>5.0f}  {code:>5}  {tool:>5}  {long:>4}")

    # Top picks
    print()
    print("=== Top by composite score (code_pass + tool_pass) at tok/s >= 40 (usable speed floor) ===")
    rows = []
    for (m, ctx), v in agg.items():
        if not v["tps"]:
            continue
        tps = sum(v["tps"]) / len(v["tps"])
        if tps < 40:
            continue
        code = v["code_pass"]
        tool = v["tool_pass"]
        score = code + tool * 2  # tool-call reliability weighted 2x — it's what kills agents
        rows.append((score, code, tool, tps, m, ctx))
    rows.sort(reverse=True)
    print(f"{'score':>5}  {'code':>4}  {'tool':>4}  {'tps':>6}  ctx     model")
    for score, code, tool, tps, m, ctx in rows[:20]:
        print(f"{score:>5}  {code:>4}  {tool:>4}  {tps:>6.1f}  {ctx:>5}   {m}")


if __name__ == "__main__":
    main()
