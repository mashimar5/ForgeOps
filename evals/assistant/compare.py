"""
Compare two eval variants case by case (no API calls).

Each case's score is its mean over reps; the difference between variants is
taken per case, so case-to-case spread cancels out. The 95% interval is a
t-interval over the paired case differences.

    .venv/bin/python evals/assistant/compare.py baseline v1
"""

import argparse
import json
import math
import statistics
from pathlib import Path

from run_eval import COUNTS, DEFAULT_FLOW, METRICS, cost_usd

# Two-sided 95% t quantiles for small n (degrees of freedom -> t); 1.96 beyond
T95 = {10: 2.228, 20: 2.086, 30: 2.042, 40: 2.021, 45: 2.014, 60: 2.000, 120: 1.980}


def t95(df):
    return next((t for limit, t in sorted(T95.items()) if df <= limit), 1.96)


def load(flow, variant, prices):
    rows = [json.loads(line) for line in open(flow / variant / "results.jsonl")]
    by_case = {}
    for row in rows:
        if row["status"] != "ok":
            continue
        answer = row.get("meta", {}).get("answer", "")
        values = {**row["grade"]}
        values["answer_words"] = len(answer.split())
        values["output_tokens"] = row["usage"]["output_tokens"]
        values["cost_usd"] = (cost_usd(row["model"], row["usage"], prices) or 0)
        by_case.setdefault(row["prompt_id"], []).append(values)
    return rows, by_case


def main():
    parser = argparse.ArgumentParser(description="Compare two eval variants case by case.")
    parser.add_argument("a", help="variant, e.g. baseline")
    parser.add_argument("b", help="variant, e.g. v1")
    parser.add_argument("--flow", type=Path, default=DEFAULT_FLOW)
    args = parser.parse_args()

    prices = json.loads((args.flow / "_state.json").read_text()).get("prices", {})
    rows_a, a = load(args.flow, args.a, prices)
    rows_b, b = load(args.flow, args.b, prices)
    cases = [case for case in a if case in b]

    print(f"{args.a}: {len(rows_a)} rows; {args.b}: {len(rows_b)} rows; {len(cases)} cases in both\n")
    print(f"{'metric':20} {args.a[-9:]:>9} {args.b[-9:]:>9} {'change':>9}   {'95% interval':16}  cases up / down")

    metrics = [m for m in METRICS + COUNTS + ["answer_words", "output_tokens", "cost_usd"]
               if all(m in reps[0] for reps in list(a.values()) + list(b.values()))]

    for metric in metrics:
        mean_a = [statistics.mean(r[metric] for r in a[c]) for c in cases]
        mean_b = [statistics.mean(r[metric] for r in b[c]) for c in cases]
        diffs = [y - x for x, y in zip(mean_a, mean_b)]
        d = statistics.mean(diffs)
        half = t95(len(diffs) - 1) * statistics.stdev(diffs) / math.sqrt(len(diffs)) if len(diffs) > 1 else 0
        up = sum(x > 1e-9 for x in diffs)
        down = sum(x < -1e-9 for x in diffs)
        digits = 3 if metric == "cost_usd" else 2
        print(f"{metric:20} {statistics.mean(mean_a):9.{digits}f} {statistics.mean(mean_b):9.{digits}f} {d:+9.{digits}f}   "
              f"[{d - half:+.{digits}f}, {d + half:+.{digits}f}]{'':4}{up:>4} / {down:<4}")


if __name__ == "__main__":
    main()
