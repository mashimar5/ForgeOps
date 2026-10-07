# Assistant eval: hillclimb narrative

| round | change (one line) | pass | facts | worked out, not asked | worked out, wrong | words / answer | $ / answer | run cost |
|---|---|---|---|---|---|---|---|---|
| 0 | baseline (2026-10-06, fresh; prompt unchanged) | 0.91 | 1.00 | 1.57 | 0.07 | 248 | 0.066 | $12.53 |
| 1 | rule: numbers as the tools give them; work one out only when asked, and say so; no estimates | 0.95 (+0.04, -0.01 to +0.08) | 1.00 | 1.18 (-0.38, -0.66 to -0.11) | 0.04 (-0.03, -0.10 to +0.04) | 236 | 0.060 | $11.54 |

46 cases x 3 reps each, all scored every round (no split: the set is small, so these are directional numbers). Changes in brackets are per-case paired differences with 95% t-intervals (`evals/assistant/compare.py baseline v1`). Spend so far for this hillclimb: about $3.10 to re-grade the old baseline with the new judge, $12.53 baseline, $11.54 v1.

## Why this round

Hardening the eval (2026-10-06) showed every real error is a value the assistant works out itself. The judge now lists those values ("worked out": counted, summed, converted, ranked or estimated, not read from a tool), whether the question asked for each, and whether each is right. Re-grading the old baseline: 2.84 per answer, 1.75 not asked for, 0.06 wrong; the wrong ones matched the errors found by hand (a top-eight ranking with the 13th part, 38 for 35, 16 for 13, 39 for 99, "about half" for 42 of 100). A no-change control (old baseline vs fresh baseline, same prompt) moved "not asked" by -0.18 (-0.42 to +0.05), which sets the bar a real change has to clear.

## What v1 did

- **Fewer unasked worked-out values:** 1.57 to 1.18 per answer (-24%), 28 cases down and 11 up. About twice the no-change drift; a real but moderate effect.
- **Forecasts stopped:** the forecast question passed 1 of 3 times before, 3 of 3 with the rule (no volume x rate estimate after saying the tools can't forecast).
- **Labelling:** answers that say a number is the assistant's own calculation rose from 7 to 54 of 138.
- **Guardrails held:** pass 0.91 to 0.95 (126 to 131 of 138; within noise), facts unchanged. Hard cases (which ask for counts, means and gaps) 93.7% to 96.8%.
- **Still there:** 5 wrong worked-out values (9 in the baseline), all unasked, mostly counts over the 100-item alert lists (41 for 43, 98 for 91, 50 for about 42) and one "six times bigger" for 2.8.
- **Cost:** answers 5% shorter, $0.006 (9%) cheaper per answer.
- **Trade-off:** some useful, correct context goes too. On "Is station S32 causing our failures?", the baseline added "roughly 16% of failures go through S32 (my own rough math)"; v1 drops it. Pass and facts don't measure that.
