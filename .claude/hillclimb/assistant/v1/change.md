# v1: state numbers as the tools give them

**Target:** `worked_out_unasked` (values the assistant worked out itself that the question didn't ask for), lower is better. **Hold:** `pass` and `facts` within noise.

**Why.** Every real error in the 2026-10-06 baseline was a value the assistant worked out itself: a "top eight" ranking that included the 13th part, 38 and 16 counted from a 100-item list (35 and 13), 39 for 99, "about half" for 42 of 100, and forecasts given after saying the tools can't forecast. Re-graded with the judge that lists worked-out values, that baseline has 2.84 per answer, 1.75 of them not asked for, 0.06 wrong; only 3 of 46 cases have none. Wrong ones are too rare to measure a change in, so the round targets the behaviour that produces them.

**Change** (system prompt only; tools, tool descriptions, model and effort unchanged). The first guideline bullet in `src/assistant.py`:

```text
Before:
- Gather evidence with the tools before answering. Every number you state must come
  from a tool result. If the tools can't answer a question, say so instead of guessing.

After:
- Gather evidence with the tools before answering. State numbers as the tools give
  them. Work a number out yourself (a count, sum, difference, average, share, ranking,
  or a conversion such as hours to days) only when the question asks for it, and say
  that it is your own calculation. If the tools can't answer a question, say so,
  without an estimate of your own.
```

**Expected.** Fewer unasked worked-out values; asked-for calculations (most hard cases ask for a count, mean or gap) unchanged. Possible cost: terser answers that drop useful context such as a time gap between two stations, which pass and facts don't capture; compare answers side by side.

**Chosen by** the user (2026-10-06), from the handoff's plan; not proposed by an analyzer reading the scored cases. The rule is generic, but the cases it was motivated by are in the scored set, so the result is directional.
