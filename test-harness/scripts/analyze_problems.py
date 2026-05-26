"""Per-problem analysis: which problems are hard, which separate models."""

import csv
import os
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
qual = list(csv.DictReader(open(os.path.join(ROOT, "results", "csv", "quality.csv"), encoding="utf-8")))
qual = [r for r in qual if r.get("timestamp", "") >= "2026-05-26T21:56"]

# Pass rate per problem
by_problem = defaultdict(lambda: {"pass": 0, "n": 0})
for r in qual:
    key = (r["kind"], r["problem_id"])
    by_problem[key]["n"] += 1
    if r["pass"] in ("True", "true", True):
        by_problem[key]["pass"] += 1

print("=== Per-problem pass rates (all 88 cells) ===")
for (kind, pid), d in sorted(by_problem.items(), key=lambda x: -x[1]["pass"]/max(x[1]["n"],1)):
    rate = d["pass"]/d["n"] if d["n"] else 0
    print(f"  {kind:<10} {pid:<22} {d['pass']:>3}/{d['n']:<3}  {rate:>5.0%}")

# For the long_module_bug specifically — was it attempted at all? Did any pass?
print()
print("=== long_module_bug detail ===")
lmb = [r for r in qual if r.get("problem_id") == "long_module_bug"]
print(f"  attempted: {len(lmb)}")
print(f"  passed:    {sum(1 for r in lmb if r['pass'] in ('True','true',True))}")
print(f"  failed:    {sum(1 for r in lmb if r['pass'] in ('False','false',False))}")
if lmb:
    # show a sample of details/errors
    print("  sample failure details:")
    for r in lmb[:5]:
        print(f"    {r['model']:<35} ctx={r['context']:<5} {r.get('details','')[:90]}")

# Which problem differentiates the 7/9 models from the 6/9 models?
print()
print("=== merge_intervals and parse_csv_row pass rates by model ===")
for pid in ["merge_intervals", "parse_csv_row", "long_module_bug"]:
    print(f"  -- {pid} --")
    by_model = defaultdict(lambda: {"p": 0, "n": 0})
    for r in qual:
        if r.get("problem_id") != pid:
            continue
        by_model[r["model"]]["n"] += 1
        if r["pass"] in ("True", "true", True):
            by_model[r["model"]]["p"] += 1
    for m, d in sorted(by_model.items()):
        rate = d["p"]/d["n"] if d["n"] else 0
        print(f"     {m:<42} {d['p']}/{d['n']}  {rate:.0%}")
