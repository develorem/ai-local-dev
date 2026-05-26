"""Benchmark runner — orchestrates one Ollama generate call surrounded by
GPU / CPU / ollama-ps sampling, then persists raw JSON + a CSV row.
"""

import csv
import datetime as dt
import json
import os
import uuid

from ollama_client import OllamaClient
from monitors.gpu_monitor import GpuMonitor
from monitors.ollama_monitor import OllamaMonitor
from monitors.cpu_monitor import CpuMonitor

ROOT = os.path.dirname(os.path.abspath(__file__))
RESULTS_RAW = os.path.join(ROOT, "results", "raw")
RESULTS_CSV_DIR = os.path.join(ROOT, "results", "csv")
PROMPTS_DIR = os.path.join(ROOT, "benchmarks", "prompts")
CSV_PATH = os.path.join(RESULTS_CSV_DIR, "runs.csv")

CSV_FIELDS = [
    "run_id", "timestamp", "model", "context", "prompt_name",
    "gen_tps", "prompt_tps", "wall_clock_sec", "load_duration_sec",
    "gpu_util_avg", "gpu_util_max", "vram_used_max_mb", "gpu_temp_max",
    "cpu_util_avg", "cpu_util_max",
    "ollama_cpu_pct_avg", "ollama_gpu_pct_avg",
    "eval_count", "prompt_eval_count", "response_chars",
    "expected_pass",
]


def load_prompt(name):
    path = os.path.join(PROMPTS_DIR, f"{name}.txt")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _ns_to_sec(ns):
    return None if ns is None else ns / 1e9


def _tps(count, duration_ns):
    if not count or not duration_ns:
        return None
    return count / (duration_ns / 1e9)


def _slug(s):
    return "".join(c if c.isalnum() else "_" for c in s)


def _append_csv_row(path, row):
    exists = os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def _fmt(v, digits=2):
    if v is None or v == "":
        return ""
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return v


class BenchmarkRunner:
    def __init__(self, host="http://localhost:11434", sample_interval_ms=250):
        self.client = OllamaClient(host=host)
        self.interval = sample_interval_ms / 1000.0
        os.makedirs(RESULTS_RAW, exist_ok=True)
        os.makedirs(RESULTS_CSV_DIR, exist_ok=True)

    def warmup(self, model, context):
        self.client.generate(
            model=model,
            prompt="ping",
            options={"num_ctx": context, "temperature": 0, "seed": 42},
            num_predict=1,
        )

    def run_single(self, model, context, prompt_name, prompt_text,
                   expected_contains=None, options_extra=None, warmup=True):
        if warmup:
            self.warmup(model, context)

        opts = {"num_ctx": context, "temperature": 0, "seed": 42}
        if options_extra:
            opts.update(options_extra)

        gpu = GpuMonitor(interval_sec=self.interval)
        oll = OllamaMonitor(interval_sec=max(self.interval, 1.0))
        cpu = CpuMonitor(interval_sec=self.interval)

        gpu.start()
        oll.start()
        cpu.start()
        try:
            result = self.client.generate(model=model, prompt=prompt_text, options=opts)
        finally:
            gpu.stop()
            oll.stop()
            cpu.stop()

        eval_count = result.get("eval_count")
        prompt_eval_count = result.get("prompt_eval_count")
        gen_tps = _tps(eval_count, result.get("eval_duration"))
        prompt_tps = _tps(prompt_eval_count, result.get("prompt_eval_duration"))
        wall_clock = result.get("_wall_clock_sec")
        response_text = result.get("response", "")

        expected_pass = None
        if expected_contains:
            expected_pass = all(s.lower() in response_text.lower() for s in expected_contains)

        run_id = f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
        timestamp = dt.datetime.now().isoformat(timespec="seconds")

        gpu_sum = gpu.summary()
        oll_sum = oll.summary()
        cpu_sum = cpu.summary()

        record = {
            "run_id": run_id,
            "timestamp": timestamp,
            "config": {
                "model": model,
                "context": context,
                "prompt_name": prompt_name,
                "prompt_chars": len(prompt_text),
                "ollama_options": opts,
            },
            "ollama_timings_ns": {
                "total_duration": result.get("total_duration"),
                "load_duration": result.get("load_duration"),
                "prompt_eval_duration": result.get("prompt_eval_duration"),
                "eval_duration": result.get("eval_duration"),
            },
            "ollama_counts": {
                "prompt_eval_count": prompt_eval_count,
                "eval_count": eval_count,
            },
            "derived": {
                "gen_tokens_per_sec": gen_tps,
                "prompt_tokens_per_sec": prompt_tps,
                "wall_clock_sec": wall_clock,
                "total_duration_sec": _ns_to_sec(result.get("total_duration")),
                "load_duration_sec": _ns_to_sec(result.get("load_duration")),
                "prompt_eval_duration_sec": _ns_to_sec(result.get("prompt_eval_duration")),
                "eval_duration_sec": _ns_to_sec(result.get("eval_duration")),
            },
            "gpu": {"samples": gpu.samples, "summary": gpu_sum},
            "cpu": {"samples": cpu.samples, "summary": cpu_sum},
            "ollama_ps": {"samples": oll.samples, "summary": oll_sum},
            "response": {
                "chars": len(response_text),
                "text": response_text,
                "expected_contains": expected_contains,
                "expected_pass": expected_pass,
            },
        }

        raw_name = f"{run_id}_{_slug(model)}_{context}_{_slug(prompt_name)}.json"
        raw_path = os.path.join(RESULTS_RAW, raw_name)
        with open(raw_path, "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)

        row = {
            "run_id": run_id,
            "timestamp": timestamp,
            "model": model,
            "context": context,
            "prompt_name": prompt_name,
            "gen_tps": _fmt(gen_tps),
            "prompt_tps": _fmt(prompt_tps),
            "wall_clock_sec": _fmt(wall_clock),
            "load_duration_sec": _fmt(_ns_to_sec(result.get("load_duration"))),
            "gpu_util_avg": _fmt(gpu_sum.get("util_gpu_pct_avg"), 1),
            "gpu_util_max": _fmt(gpu_sum.get("util_gpu_pct_max"), 1),
            "vram_used_max_mb": _fmt(gpu_sum.get("vram_used_mb_max"), 0),
            "gpu_temp_max": _fmt(gpu_sum.get("temp_c_max"), 0),
            "cpu_util_avg": _fmt(cpu_sum.get("cpu_pct_avg"), 1),
            "cpu_util_max": _fmt(cpu_sum.get("cpu_pct_max"), 1),
            "ollama_cpu_pct_avg": _fmt(oll_sum.get("cpu_pct_avg"), 0),
            "ollama_gpu_pct_avg": _fmt(oll_sum.get("gpu_pct_avg"), 0),
            "eval_count": eval_count if eval_count is not None else "",
            "prompt_eval_count": prompt_eval_count if prompt_eval_count is not None else "",
            "response_chars": len(response_text),
            "expected_pass": "" if expected_pass is None else expected_pass,
        }
        _append_csv_row(CSV_PATH, row)

        return record

    def run_matrix(self, config):
        prompt_cache = {}
        failures = []
        for spec in config.get("matrix", []):
            model = spec["model"]
            context = spec["context"]
            for p in spec.get("prompts", []):
                if isinstance(p, str):
                    pname, exp = p, None
                else:
                    pname, exp = p["name"], p.get("expected_contains")
                if pname not in prompt_cache:
                    try:
                        prompt_cache[pname] = load_prompt(pname)
                    except FileNotFoundError as e:
                        print(f"[bench] SKIP  prompt={pname}  ({e})")
                        failures.append({"model": model, "context": context, "prompt": pname, "error": str(e)})
                        continue
                print(f"[bench] {model}  ctx={context}  prompt={pname}")
                try:
                    rec = self.run_single(
                        model=model,
                        context=context,
                        prompt_name=pname,
                        prompt_text=prompt_cache[pname],
                        expected_contains=exp,
                    )
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    print(f"   FAIL  {msg}")
                    failures.append({"model": model, "context": context, "prompt": pname, "error": msg})
                    continue

                d = rec["derived"]
                gtps = d["gen_tokens_per_sec"] or 0
                ptps = d["prompt_tokens_per_sec"] or 0
                wall = d["wall_clock_sec"] or 0
                gpu_avg = rec["gpu"]["summary"].get("util_gpu_pct_avg", 0) or 0
                oll_gpu = rec["ollama_ps"]["summary"].get("gpu_pct_avg", 0) or 0
                print(
                    f"   gen={gtps:.2f} tok/s  prompt={ptps:.2f} tok/s  "
                    f"wall={wall:.2f}s  gpu_util_avg={gpu_avg:.1f}%  "
                    f"ollama_gpu={oll_gpu:.0f}%"
                )
        return failures
