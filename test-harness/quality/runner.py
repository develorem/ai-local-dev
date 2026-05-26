"""Quality benchmark runner.

For each (model, context) entry in the suite, runs every coding problem
and every tool-call problem, scoring pass/fail and persisting raw JSON +
a flat CSV summary.
"""

import csv
import datetime as dt
import json
import os
import sys
import uuid

# Allow running this module standalone or via `python -m quality.runner`
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ollama_client import OllamaClient
from quality.code_executor import execute as exec_code
from quality.response_parser import extract_code, extract_json

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBLEMS_DIR = os.path.join(ROOT, "benchmarks", "quality", "problems")
TOOL_CALLS_DIR = os.path.join(ROOT, "benchmarks", "quality", "tool_calls")
SUITE_PATH = os.path.join(ROOT, "benchmarks", "quality", "suite.json")
RESULTS_RAW = os.path.join(ROOT, "results", "raw")
RESULTS_CSV_DIR = os.path.join(ROOT, "results", "csv")
QUALITY_CSV = os.path.join(RESULTS_CSV_DIR, "quality.csv")

QUALITY_CSV_FIELDS = [
    "run_id", "timestamp", "model", "context",
    "kind", "problem_id", "pass", "details",
    "gen_tps", "wall_sec", "response_chars",
]


def _slug(s):
    return "".join(c if c.isalnum() else "_" for c in s)


def _append_csv_row(path, row):
    exists = os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=QUALITY_CSV_FIELDS)
        if not exists:
            w.writeheader()
        w.writerow(row)


def _load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _list_problems(dir_path, allow_ids=None):
    if not os.path.isdir(dir_path):
        return []
    out = []
    for name in sorted(os.listdir(dir_path)):
        if not name.endswith(".json"):
            continue
        pid = name[:-5]
        if allow_ids and pid not in allow_ids:
            continue
        out.append(_load_json(os.path.join(dir_path, name)))
    return out


def _tps(count, duration_ns):
    if not count or not duration_ns:
        return None
    return count / (duration_ns / 1e9)


class QualityRunner:
    def __init__(self, host="http://localhost:11434"):
        self.client = OllamaClient(host=host)
        os.makedirs(RESULTS_RAW, exist_ok=True)
        os.makedirs(RESULTS_CSV_DIR, exist_ok=True)

    # --- code problems --------------------------------------------------

    def run_code_problem(self, model, context, problem):
        opts = {
            "num_ctx": context,
            "temperature": 0,
            "seed": 42,
            "num_predict": problem.get("max_tokens", 1024),
        }

        prefix = problem.get("prefix_code", "")
        prompt_text = problem["prompt"]
        # If the prompt has a {prefix_code} placeholder, substitute the prefix
        # in-line so the model actually sees the long module it's reasoning about.
        if "{prefix_code}" in prompt_text:
            prompt_text = prompt_text.replace("{prefix_code}", prefix)

        result = self.client.generate(
            model=model, prompt=prompt_text, options=opts
        )
        response = result.get("response", "")
        code = extract_code(response)

        # Prepend the prefix at test exec time too so the model's function
        # rebinds whatever the prefix already defined.
        combined_code = (prefix + "\n\n" + code) if prefix else code

        tests = "\n".join(problem.get("tests", []))
        execution = exec_code(
            model_code=combined_code,
            tests_code=tests,
            timeout_sec=problem.get("timeout_sec", 10),
        )

        gen_tps = _tps(result.get("eval_count"), result.get("eval_duration"))
        return {
            "kind": "code",
            "problem_id": problem["id"],
            "pass": execution["pass"],
            "execution": execution,
            "response_text": response,
            "extracted_code": code,
            "ollama_timings_ns": {
                "total_duration": result.get("total_duration"),
                "load_duration": result.get("load_duration"),
                "prompt_eval_duration": result.get("prompt_eval_duration"),
                "eval_duration": result.get("eval_duration"),
            },
            "gen_tokens_per_sec": gen_tps,
            "wall_clock_sec": result.get("_wall_clock_sec"),
        }

    # --- tool-call problems ---------------------------------------------

    def run_tool_call_problem(self, model, context, problem):
        opts = {
            "num_ctx": context,
            "temperature": 0,
            "seed": 42,
            "num_predict": problem.get("max_tokens", 512),
        }
        result = self.client.generate(
            model=model, prompt=problem["prompt"], options=opts
        )
        response = result.get("response", "")
        raw_json = extract_json(response)

        parsed = None
        parse_ok = False
        try:
            parsed = json.loads(raw_json)
            parse_ok = True
        except Exception:
            pass

        checks = problem.get("checks", {})
        details = {"parse_ok": parse_ok}
        ok = parse_ok and (parsed is not None)

        if ok and "must_have_keys" in checks:
            missing = [k for k in checks["must_have_keys"] if k not in parsed]
            details["missing_keys"] = missing
            if missing:
                ok = False

        if ok and "tool_in" in checks:
            tool = parsed.get("tool") if isinstance(parsed, dict) else None
            details["tool"] = tool
            if tool not in checks["tool_in"]:
                ok = False

        if ok and "must_call_in_order" in checks:
            calls = parsed.get("calls") if isinstance(parsed, dict) else None
            names = [c.get("tool") for c in calls] if isinstance(calls, list) else []
            details["call_names"] = names
            expected = checks["must_call_in_order"]
            # require expected to appear as a (not-necessarily-contiguous) subsequence
            it = iter(names)
            if not all(any(n == want for n in it) for want in expected):
                ok = False

        if ok and "args_required" in checks:
            args = parsed.get("args") if isinstance(parsed, dict) else None
            if not isinstance(args, dict):
                ok = False
                details["args_missing"] = True
            else:
                missing = [k for k in checks["args_required"] if k not in args]
                details["args_missing_keys"] = missing
                if missing:
                    ok = False

        if ok and "must_refuse" in checks and checks["must_refuse"]:
            # 'refuse' means: did NOT call the dangerous tool
            forbidden = set(checks.get("forbidden_tools", []))
            called = set()
            if isinstance(parsed, dict):
                if "tool" in parsed:
                    called.add(parsed["tool"])
                if isinstance(parsed.get("calls"), list):
                    called |= {c.get("tool") for c in parsed["calls"] if isinstance(c, dict)}
            details["called_tools"] = sorted(t for t in called if t)
            if called & forbidden:
                ok = False

        gen_tps = _tps(result.get("eval_count"), result.get("eval_duration"))
        return {
            "kind": "tool_call",
            "problem_id": problem["id"],
            "pass": ok,
            "details": details,
            "parsed": parsed,
            "raw_json": raw_json,
            "response_text": response,
            "ollama_timings_ns": {
                "total_duration": result.get("total_duration"),
                "load_duration": result.get("load_duration"),
                "prompt_eval_duration": result.get("prompt_eval_duration"),
                "eval_duration": result.get("eval_duration"),
            },
            "gen_tokens_per_sec": gen_tps,
            "wall_clock_sec": result.get("_wall_clock_sec"),
        }

    # --- suite orchestration --------------------------------------------

    def run_suite(self, suite):
        runs = []
        failures = []
        problem_ids = suite.get("problems")
        tool_call_ids = suite.get("tool_calls")
        problems = _list_problems(PROBLEMS_DIR, allow_ids=problem_ids)
        tool_calls = _list_problems(TOOL_CALLS_DIR, allow_ids=tool_call_ids)

        for entry in suite.get("matrix", []):
            model = entry["model"]
            context = entry["context"]
            run_id = f"{dt.datetime.now().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:6]}"
            timestamp = dt.datetime.now().isoformat(timespec="seconds")

            print(f"[quality] {model}  ctx={context} — warming up")
            try:
                self.client.generate(
                    model=model, prompt="ping",
                    options={"num_ctx": context, "temperature": 0, "seed": 42},
                    num_predict=1,
                )
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                print(f"[quality] SKIP  {model}  ctx={context}  warmup failed: {msg}")
                failures.append({"model": model, "context": context, "stage": "warmup", "error": msg})
                continue

            session = {
                "run_id": run_id,
                "timestamp": timestamp,
                "config": {"model": model, "context": context},
                "code": [],
                "tool_call": [],
            }

            print(f"[quality] {model}  ctx={context}  — running {len(problems)} code + {len(tool_calls)} tool-call problems")
            for p in problems:
                try:
                    r = self.run_code_problem(model, context, p)
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    print(f"   code  {p['id']:<22} ERROR  {msg}")
                    failures.append({"model": model, "context": context, "stage": "code", "problem": p["id"], "error": msg})
                    continue
                session["code"].append(r)
                self._write_csv(run_id, timestamp, model, context, r,
                                details=self._code_detail(r))
                print(f"   code  {p['id']:<22} {'PASS' if r['pass'] else 'FAIL'}  "
                      f"gen={(r['gen_tokens_per_sec'] or 0):.1f}t/s  wall={r['wall_clock_sec']:.1f}s")

            for tp in tool_calls:
                try:
                    r = self.run_tool_call_problem(model, context, tp)
                except Exception as e:
                    msg = f"{type(e).__name__}: {e}"
                    print(f"   tool  {tp['id']:<22} ERROR  {msg}")
                    failures.append({"model": model, "context": context, "stage": "tool", "problem": tp["id"], "error": msg})
                    continue
                session["tool_call"].append(r)
                self._write_csv(run_id, timestamp, model, context, r,
                                details=json.dumps(r["details"], default=str))
                print(f"   tool  {tp['id']:<22} {'PASS' if r['pass'] else 'FAIL'}  "
                      f"gen={(r['gen_tokens_per_sec'] or 0):.1f}t/s  wall={r['wall_clock_sec']:.1f}s")

            session["summary"] = {
                "code_pass": sum(1 for r in session["code"] if r["pass"]),
                "code_total": len(session["code"]),
                "tool_pass": sum(1 for r in session["tool_call"] if r["pass"]),
                "tool_total": len(session["tool_call"]),
            }
            s = session["summary"]
            print(f"[quality] {model}  ctx={context}  -> "
                  f"code {s['code_pass']}/{s['code_total']}  "
                  f"tool {s['tool_pass']}/{s['tool_total']}")

            raw_name = f"{run_id}_quality_{_slug(model)}_{context}.json"
            with open(os.path.join(RESULTS_RAW, raw_name), "w", encoding="utf-8") as f:
                json.dump(session, f, indent=2, default=str)
            runs.append(session)
        return {"runs": runs, "failures": failures}

    def _code_detail(self, r):
        ex = r["execution"]
        if r["pass"]:
            return "ok"
        if ex.get("timed_out"):
            return "timeout"
        err = (ex.get("stderr") or "").strip().splitlines()
        return ("err:" + err[-1])[:160] if err else "exit_nonzero"

    def _write_csv(self, run_id, timestamp, model, context, r, details):
        row = {
            "run_id": run_id,
            "timestamp": timestamp,
            "model": model,
            "context": context,
            "kind": r["kind"],
            "problem_id": r["problem_id"],
            "pass": r["pass"],
            "details": details,
            "gen_tps": round(r["gen_tokens_per_sec"], 2) if r.get("gen_tokens_per_sec") else "",
            "wall_sec": round(r["wall_clock_sec"], 2) if r.get("wall_clock_sec") else "",
            "response_chars": len(r.get("response_text", "")),
        }
        _append_csv_row(QUALITY_CSV, row)


def load_suite(path=SUITE_PATH):
    return _load_json(path)
