"""
DIGITAL TWIN, STEP 2 -- does the twin reproduce weeks it never saw?

Calibrates the twin (digital_twin.py) on weeks 1-61, the first 60% of the
timeline, then runs it from week 50 to the end of the data and compares
parts that entered in weeks 62-98 with the real ones. The run starts with
the real queue for line 3 at week 50; the extra weeks let the twin's own
parts replace it. Two kinds of run, 3 seeds each:

    replay    real inputs: what entered each tick (line and batch size) and
              when line 3 ran each week (its hourly pattern). The twin
              supplies routes, timing, line 3's weekly plan, which queue it
              serves, and QC results.
    level     the replay with one more input: the comparison window's
              failure level. The real level halved after week 61 (0.69%
              -> 0.34%), which no calibration before it could know; this
              run checks the failure model's structure at the right level.
    planned   only a production plan: which entry lines ran each week.
              Arrivals and line-3 shift patterns come from calibration
              weeks of the same kind.

Measures: flow (finished per week, parts in production, time in
production and wait for line 3 by entry line), routes (station visits),
QC (failure rates overall, by line and through L3_S32, week-to-week
spread) and how failures cluster (lift within an entry batch and within
a line-3 tick, the batch-mate alert, the line monitor).

    .venv/bin/python src/twin_validate.py

Takes about 2 minutes. Writes results/twin_validation.csv and
results/plots/twin_validation.png.
"""

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from digital_twin import (
    NEVER,
    NOT_VISITED,
    TICKS_PER_HOUR_INT,
    TICKS_PER_WEEK,
    batch_alert,
    calibrate,
    dense_ids,
    fit_line3_policy,
    line_monitor,
    load_history,
    observed_lift,
    plan_weeks,
    rate_by,
    real_weeks,
    simulate,
    waiting_at,
)
from production_data import PLOTS_DIR, RESULTS_DIR

CALIBRATE_UNTIL = 62     # weeks 1-61
WARM_UP_FROM = 50
COMPARE = (62, 99)       # parts that entered in weeks 62-98
SEEDS = (0, 1, 2)

# ============================================================
# MEASURES (the same code for the real line and the twin)
# ============================================================

def measure(p, visited, s32):
    """
    p        parts: start, ready, l3_start, end, line, response, repeats, repeats_failed
    visited  bool (parts, 52): stations visited
    s32      column of L3_S32
    """

    lo, hi = COMPARE[0] * TICKS_PER_WEEK, COMPARE[1] * TICKS_PER_WEEK
    inside = (p["start"] >= lo) & (p["start"] < hi)
    y = p["response"].astype(float)
    out = {}

    # ---- Flow
    finished_week = p["end"] // TICKS_PER_WEEK
    weekly = np.bincount(finished_week[inside], minlength=COMPARE[1])[COMPARE[0]:COMPARE[1] - 2]
    out["parts finished per week"] = weekly.mean()

    hours = np.arange(lo // TICKS_PER_HOUR_INT, hi // TICKS_PER_HOUR_INT, 6)
    t = hours * TICKS_PER_HOUR_INT
    entered = np.searchsorted(np.sort(p["start"]), t, "right")
    left = np.searchsorted(np.sort(p["end"]), t, "right")
    wip = entered - left
    out["parts in production (mean)"] = wip.mean()
    out["parts in production (p90)"] = np.percentile(wip, 90)

    for code, name in ((0, "L0"), (1, "L1")):
        m = inside & (p["line"] == code)
        total = (p["end"][m] - p["start"][m]) / TICKS_PER_HOUR_INT
        wait = (p["l3_start"][m] - p["ready"][m]) / TICKS_PER_HOUR_INT
        out[f"{name} hours in production (median)"] = np.median(total)
        out[f"{name} hours in production (p90)"] = np.percentile(total, 90)
        out[f"{name} wait for line 3 (median h)"] = np.median(wait)
        out[f"{name} share of parts"] = m.sum() / inside.sum()

    share = visited[inside].mean(axis=0)
    out["station visit shares (mean abs. difference)"] = share     # compared below
    out["share through L3_S32"] = share[s32]

    # ---- QC
    out["failure rate"] = y[inside].mean()
    for code, name in ((0, "L0"), (1, "L1")):
        out[f"{name} failure rate"] = y[inside & (p["line"] == code)].mean()
    out["failure rate through L3_S32"] = y[inside & visited[:, s32]].mean()
    week = p["l3_start"][inside] // TICKS_PER_WEEK
    rates, _ = rate_by(week, y[inside], 2_000)       # indexed by week number
    out["weekly failure rate SD"] = np.nanstd(rates)

    # ---- Clustering
    out["lift, same entry batch"] = observed_lift(y[inside], dense_ids(p["start"][inside]))
    out["lift, same line-3 tick"] = observed_lift(y[inside], dense_ids(p["l3_start"][inside]))

    # The batch-mate alert and the line monitor, as the API computes them
    flagged = (batch_alert(p["start"], p["end"], p["response"], p["repeats_failed"]) != NEVER) & inside
    out["alert: share of parts flagged"] = flagged.sum() / inside.sum()
    out["alert: failure lift of flagged parts"] = y[flagged].mean() / y[inside].mean()
    out["alert: share of failures flagged"] = y[flagged].sum() / y[inside].sum()
    alerts = line_monitor(p["end"], p["response"], p["repeats"], p["repeats_failed"], lo, hi)
    out["line monitor: share of hours alerting"] = alerts.mean()

    return out, weekly, wip, rates


def real_parts(h):
    keep = h.start >= WARM_UP_FROM * TICKS_PER_WEEK
    cols = ("start", "ready", "l3_start", "end", "line", "response", "repeats", "repeats_failed")
    return {c: getattr(h, c)[keep] for c in cols}, h.seen[keep] != NOT_VISITED


def run_parts(run, cal):
    cols = ("start", "ready", "l3_start", "end", "line", "response", "repeats", "repeats_failed")
    return {c: getattr(run, c) for c in cols}, run.seen(cal) != NOT_VISITED


# ============================================================
# RUN
# ============================================================

h = load_history()
cal = calibrate(h, until_week=CALIBRATE_UNTIL)
fit_line3_policy(cal, h, 1, CALIBRATE_UNTIL)

s32 = h.stations.index("L3_S32")
last_week = int(h.end.max() // TICKS_PER_WEEK)
weeks = real_weeks(h, WARM_UP_FROM, last_week)

parts, visited = real_parts(h)
real, real_weekly, real_wip, real_rates = measure(parts, visited, s32)

results = {"real": [real]}
series = {"real": (real_weekly, real_wip, real_rates)}
KINDS = ("replay", "level", "planned")

# The failure level of the comparison window, as log-odds against the calibration's
calibration_rate = h.response[(h.start >= TICKS_PER_WEEK) & (h.end < CALIBRATE_UNTIL * TICKS_PER_WEEK)].mean()
logit = lambda r: np.log(r / (1 - r))
level_shift = logit(real["failure rate"]) - logit(calibration_rate)
print(f"failure level: calibration {calibration_rate:.3%}, comparison window {real['failure rate']:.3%} "
      f"({level_shift:+.2f} log-odds for the 'level' run)")

for kind in KINDS:
    results[kind] = []
    for seed in SEEDS:
        rng = np.random.default_rng(100 + seed)
        run_weeks = plan_weeks(cal, [w.kind for w in weeks], rng) if kind == "planned" else weeks
        run = simulate(cal, run_weeks, WARM_UP_FROM, seed=seed, level_shift=level_shift if kind == "level" else 0.0,
                       waiting=waiting_at(h, WARM_UP_FROM))
        out, weekly, wip, rates = measure(*run_parts(run, cal), s32)
        results[kind].append(out)
        if seed == 0:
            series[kind] = (weekly, wip, rates)
    print(f"{kind}: {len(run):,} parts finished in the last run, {run.unfinished:,} still in production at the end")


# ============================================================
# REPORT
# ============================================================

rows = []
for name in real:
    if name.startswith("station visit shares"):
        diffs = {k: [np.abs(r[name] - real[name]).mean() for r in results[k]] for k in KINDS}
        rows.append({"measure": name, "real": 0.0,
                     **{k: np.mean(v) for k, v in diffs.items()},
                     **{f"{k} range": f"{min(v):.4f}-{max(v):.4f}" for k, v in diffs.items()}})
        continue
    row = {"measure": name, "real": real[name]}
    for k in KINDS:
        values = [r[name] for r in results[k]]
        row[k] = np.mean(values)
        row[f"{k} range"] = f"{min(values):.4g}-{max(values):.4g}"
    rows.append(row)

table = pd.DataFrame(rows)
table.to_csv(RESULTS_DIR / "twin_validation.csv", index=False)

with pd.option_context("display.width", 200, "display.max_colwidth", 60):
    print("\nParts that entered in weeks %d-%d (twin: mean of %d seeds, range in the next column)\n" % (COMPARE[0], COMPARE[1] - 1, len(SEEDS)))
    print(table.to_string(index=False, float_format=lambda v: f"{v:.4g}"))

# ---- Plot: weekly flow and failure rates, real vs twin
fig, axes = plt.subplots(3, 1, figsize=(10, 9))
colors = {"real": "#0b0b0b", "replay": "#2a78d6", "level": "#1baf7a", "planned": "#eb6834"}
x_weeks = np.arange(COMPARE[0], COMPARE[1] - 2)
for kind, (weekly, wip, rates) in series.items():
    axes[0].plot(x_weeks, weekly, color=colors[kind], lw=1.6, label=kind)
    axes[1].plot(np.arange(len(wip)) / 28 + COMPARE[0], wip, color=colors[kind], lw=1.0, label=kind)
    axes[2].plot(np.arange(len(rates)), rates * 100, color=colors[kind], lw=1.6, label=kind)
axes[0].set_title("Parts finished per week (replay and level share the same flow)")
axes[1].set_title("Parts in production")
axes[2].set_title("Weekly QC failure rate (%), by line-3 week")
for ax in axes:
    ax.grid(alpha=0.3)
    ax.legend(frameon=False, ncol=3)
axes[2].set_xlim(COMPARE[0], COMPARE[1] + 2)
axes[2].set_xlabel("week")
fig.tight_layout()
fig.savefig(PLOTS_DIR / "twin_validation.png", dpi=130)
print(f"\nwrote {RESULTS_DIR / 'twin_validation.csv'} and {PLOTS_DIR / 'twin_validation.png'}")
