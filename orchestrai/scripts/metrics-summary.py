#!/usr/bin/env python3
"""Aggregate prompt.metrics events from the Hub's events table.

Usage (from any host with docker access):
    docker exec orchestrai-hub python /opt/orchestrai-hub/scripts/metrics-summary.py
    docker exec orchestrai-hub python /opt/orchestrai-hub/scripts/metrics-summary.py --since 2026-05-29
    docker exec orchestrai-hub python /opt/orchestrai-hub/scripts/metrics-summary.py --task 01KS...

Or copy the script out and point it at the DB directly:
    python metrics-summary.py --db /path/to/orchestrai.db

Reports median/min/max prompt sizes by (mode, kind_hint), the heaviest section
per bucket, and any outlier events whose total exceeds 80% of the 16K window.
"""

import argparse
import json
import sqlite3
import statistics
import sys
from collections import defaultdict

DEFAULT_DB = "/data/orchestrai.db"
NUM_CTX = 16384         # qwen2.5-coder context window (tokens)
NUM_PREDICT = 4096      # reserved for the response (tokens)
CHARS_PER_TOKEN = 3.5   # rough average for English+code

# Prompt budget = (window - response reservation) translated to chars.
# Warn at 80% of THAT (not 80% of the whole window — the response slice is
# already off-limits to the prompt).
PROMPT_BUDGET_CHARS = int((NUM_CTX - NUM_PREDICT) * CHARS_PER_TOKEN)
WARN_THRESHOLD_CHARS = int(PROMPT_BUDGET_CHARS * 0.80)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--since", default=None,
                    help="ISO timestamp lower bound (e.g. 2026-05-29)")
    ap.add_argument("--task", default=None, help="restrict to one task_id suffix")
    ap.add_argument("--detail", action="store_true",
                    help="also list every event, not just aggregates")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    q = "SELECT ts, task_id, detail FROM events WHERE kind='prompt.metrics'"
    params: list = []
    if args.since:
        q += " AND ts >= ?"
        params.append(args.since)
    if args.task:
        q += " AND task_id LIKE ?"
        params.append(f"%{args.task}%")
    q += " ORDER BY ts ASC"
    rows = conn.execute(q, params).fetchall()
    if not rows:
        print("(no prompt.metrics events match)")
        return 0

    by_mk: dict = defaultdict(list)
    sections: dict = defaultdict(lambda: defaultdict(list))
    outliers: list = []
    for r in rows:
        d = json.loads(r["detail"])
        mode = d["mode"]
        kh = d.get("kind_hint") or "-"
        key = (mode, kh)
        by_mk[key].append(d["total_chars"])
        for sk, sv in d["sections"].items():
            sections[key][sk].append(sv)
        if d["total_chars"] >= WARN_THRESHOLD_CHARS:
            outliers.append((r["ts"], mode, kh, d["total_chars"], r["task_id"]))

    print(f"events: {len(rows)}  prompt-budget: {PROMPT_BUDGET_CHARS} chars "
          f"({NUM_CTX - NUM_PREDICT} tokens)  warn ≥ {WARN_THRESHOLD_CHARS} (80%)")
    print()

    print(f"{'mode':18} {'kind':6} {'n':>3} {'min':>5} {'med':>5} {'p95':>5} {'max':>5}")
    print("-" * 60)
    for key, totals in sorted(by_mk.items()):
        ts = sorted(totals)
        p95 = ts[int(len(ts) * 0.95)] if len(ts) > 1 else ts[0]
        print(f"{key[0]:18} {key[1]:6} {len(ts):3d} "
              f"{min(ts):5d} {int(statistics.median(ts)):5d} {p95:5d} {max(ts):5d}")

    print()
    print("HEAVIEST SECTIONS per (mode, kind) — top 5 by average:")
    for key, secmap in sorted(sections.items()):
        avgs = sorted(((k, statistics.mean(v)) for k, v in secmap.items()),
                      key=lambda kv: -kv[1])
        head = ", ".join(f"{k}={int(v)}" for k, v in avgs[:5])
        print(f"  {key[0]:18} {key[1]:6}  {head}")

    if outliers:
        print()
        print(f"OUTLIERS (≥ {WARN_THRESHOLD_CHARS} chars):")
        for ts, mode, kh, total, tid in outliers:
            print(f"  {ts[:19]}  {mode:18} kh={kh:5} total={total:6d}  task={tid[-6:]}")
    else:
        print()
        print("OUTLIERS: none (every event well under window)")

    if args.detail:
        print()
        print("ALL EVENTS:")
        for r in rows:
            d = json.loads(r["detail"])
            print(f"  {r['ts'][:19]}  {d['mode']:18} kh={d.get('kind_hint','-'):5} "
                  f"total={d['total_chars']:5d}  task={r['task_id'][-6:]}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
