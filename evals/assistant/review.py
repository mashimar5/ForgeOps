"""
A markdown review of one eval variant: for every graded case, the question,
the tools the assistant called, its answer, and the judge's verdict on each
checklist item with its reason. For reading grades next to their reasons;
the lite report.html shows the scores only.

    .venv/bin/python evals/assistant/review.py               # baseline
    .venv/bin/python evals/assistant/review.py --variant v1

Writes .claude/hillclimb/assistant/<variant>/review.md (no API calls).
"""

import argparse
import json
import re
from pathlib import Path

from run_eval import CASES_PATH, DEFAULT_FLOW, METRICS, cost_usd, final_answer


def fence(text):
    """A code fence longer than any run of backticks inside the text."""

    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(4, longest + 1)


def plain(text):
    """Model-written text outside a code fence: never let it become markup."""

    return str(text).replace("<", "&lt;").replace("\n", " ")


def main():
    parser = argparse.ArgumentParser(description="Write a markdown review of one eval variant.")
    parser.add_argument("--flow", type=Path, default=DEFAULT_FLOW)
    parser.add_argument("--variant", default="baseline")
    args = parser.parse_args()

    vdir = args.flow / args.variant
    state = json.loads((args.flow / "_state.json").read_text())
    prices = state.get("prices", {})
    cases = {c["id"]: c for c in map(json.loads, open(CASES_PATH))}
    order = list(cases)

    rows = [json.loads(line) for line in open(vdir / "results.jsonl")]
    rows.sort(key=lambda r: (order.index(r["prompt_id"]), r["rep"]))

    passed = sum(r["grade"]["pass"] for r in rows)
    app_cost = sum(cost_usd(r["model"], r["usage"], prices) or 0 for r in rows)
    judge_cost = sum(cost_usd(r["judge_model"], r["judge_usage"], prices) or 0 for r in rows)

    lines = [
        f"# Assistant eval review: {args.variant}",
        "",
        f"{len(rows)} graded answers, {passed} pass. Assistant {rows[0]['model']}, judge {rows[0]['judge_model']}. "
        f"Cost ${app_cost:.2f} assistant + ${judge_cost:.2f} judge.",
        "",
        "| case | rep | pass | facts | no bad claims | exact values | grounded |",
        "|---|---|---|---|---|---|---|",
        *[
            f"| {r['prompt_id']} | {r['rep']} | {'PASS' if r['grade']['pass'] else 'FAIL'} | "
            + " | ".join(f"{r['grade'][m]:.2g}" for m in METRICS[1:])
            + " |"
            for r in rows
        ],
        "",
    ]

    for r in rows:
        case = cases[r["prompt_id"]]
        trace_path = vdir / "traces" / f"{r['prompt_id']}_rep{r['rep']}.json"
        transcript = json.loads(trace_path.read_text())
        answer = r.get("meta", {}).get("answer") or final_answer(transcript)
        calls = [
            f"`{t['name']}({', '.join(f'{k}={v}' for k, v in json.loads(t['content']).items())})`"
            for t in transcript if t["role"] == "tool_call"
        ]
        verdict = r.get("meta", {}).get("verdict")
        cost = (cost_usd(r["model"], r["usage"], prices) or 0, cost_usd(r["judge_model"], r["judge_usage"], prices) or 0)

        lines += [
            f"## {r['prompt_id']} (rep {r['rep']}): {'PASS' if r['grade']['pass'] else 'FAIL'}",
            "",
            f"**Question:** {case['question']}",
            "",
            f"**Tools called:** {', '.join(calls) or 'none'}",
            "",
            f"**Cost:** ${cost[0]:.3f} assistant, ${cost[1]:.3f} judge; transcript: `{trace_path.relative_to(args.flow)}`",
            "",
            "**Answer:**",
            "",
            fence(answer) + "text",
            answer,
            fence(answer),
            "",
        ]

        if verdict:
            required = {e["item"]: e for e in verdict["required"]}
            forbidden = {e["item"]: e for e in verdict["forbidden"]}
            lines += ["**Required statements:**", ""]
            lines += [
                f"- {'✓ met' if required[i]['met'] else '✗ NOT met'}: {item} — {plain(required[i]['reason'])}"
                for i, item in enumerate(case["must_say"], start=1)
            ] or ["- (none)"]
            lines += ["", "**Forbidden claims:**", ""]
            lines += [
                f"- {'✗ MADE' if forbidden[i]['violated'] else '✓ not made'}: {item} — {plain(forbidden[i]['reason'])}"
                for i, item in enumerate(case["must_not_say"], start=1)
            ] or ["- (none)"]
            lines += ["", "**Values the judge double-checked:**", ""]
            lines += [
                f"- {'✓ grounded' if v['grounded'] else '✗ UNGROUNDED'}: {plain(v['value'])} — {plain(v['reason'])}"
                for v in verdict["values"]
            ] or ["- (none)"]
        else:
            lines += ["**Judge's notes:**", ""]
            lines += [f"- {metric}: {plain(r['explanation'][metric])}" for metric in METRICS[1:]]

        lines += ["", f"**Exact values:** {plain(r['explanation']['exact_values'])}", ""]

    out = vdir / "review.md"
    out.write_text("\n".join(lines))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
