"""
DIGITAL TWIN, STEP 3 -- what-if scenarios and monitor stress tests

The twin (digital_twin.py) calibrated on the most recent weeks (62-98),
"the twin as of the end of the data". Every run replays those weeks' real
arrivals and line-3 shift patterns, starting from the real line-3 queue,
and scenarios are compared with the twin's own baseline (twin vs twin), so
the twin's known gaps against the real line largely cancel.

1. Flow what-ifs (3 seeds each): more volume, a cap on line 3's weekly
   hours, a slower line 3, a longer L1 campaign. Line 3 plans its hours
   each week and can run at most 123 hours a week (its busiest real week)
   unless a scenario says otherwise.

2. Monitor stress tests: six kinds of injected quality problem, each at
   20 random times: line-3 problems and bad material in the L0-only weeks
   70-92, and bad lots (20 entry batches of a week failing at 30x) on L0
   then and on L1 during the L1 campaign of weeks 62-66. Each disturbed run is compared with the
   same seed's undisturbed run, so every extra failure is known to come
   from the problem. Does the line monitor raise a new alert, and how
   fast? Does the batch-mate alert flag the problem's failures before
   their own QC result?

The failure model is built from associations, so a disturbance is an
assumed extra risk, not a cause the data shows.

    .venv/bin/python src/twin_scenarios.py

Takes about 2 minutes. Writes results/twin_scenarios.csv,
results/twin_stress_tests.csv and results/plots/twin_scenarios.png, and
saves four runs to serving/twin/ for the dashboard's line map.
"""

import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from digital_twin import (
    HOURS_PER_WEEK,
    NEVER,
    TICKS_PER_HOUR_INT,
    TICKS_PER_WEEK,
    Disturbance,
    batch_alert,
    calibrate,
    fit_line3_policy,
    line_monitor,
    load_history,
    plan_weeks,
    real_weeks,
    simulate,
    waiting_at,
)
from line_map import TWIN_DIR
from production_data import PLOTS_DIR, RESULTS_DIR

FIRST, LAST = 62, 98          # calibration and replay window (weeks)
MEASURE = (66, 97)            # parts that entered in these weeks are compared
SEEDS = (0, 1, 2)
TRIALS = 20
STRESS_WEEKS = (70, 92)       # disturbances start in these weeks


# ============================================================
# THE TWIN AS OF THE END OF THE DATA
# ============================================================

h = load_history()
cal = calibrate(h, until_week=LAST + 1, first_week=FIRST)
fit_line3_policy(cal, h, FIRST, LAST + 1)
weeks = real_weeks(h, FIRST, LAST)
waiting = waiting_at(h, FIRST)
lo, hi = MEASURE[0] * TICKS_PER_WEEK, MEASURE[1] * TICKS_PER_WEEK


def run(seed, weeks=weeks, **kwargs):
    return simulate(cal, weeks, FIRST, seed=seed, waiting=waiting, **kwargs)


# ============================================================
# 1. FLOW WHAT-IFS
# ============================================================

def flow_measures(r):
    inside = (r.start >= lo) & (r.start < hi)
    out = {}
    l3_hours = r.l3_capacity.reshape(-1, HOURS_PER_WEEK) > 0
    span = slice(MEASURE[0] - FIRST, MEASURE[1] - FIRST)
    out["line-3 hours per week"] = l3_hours[span].sum(axis=1).mean()
    out["parts finished per week"] = np.bincount(r.end[inside] // TICKS_PER_WEEK - MEASURE[0], minlength=MEASURE[1] - MEASURE[0])[:-3].mean()

    hours = np.arange(lo, hi, 6 * TICKS_PER_HOUR_INT)
    queue = np.searchsorted(np.sort(r.ready), hours, "right") - np.searchsorted(np.sort(r.l3_start), hours, "right")
    in_production = np.searchsorted(np.sort(r.start), hours, "right") - np.searchsorted(np.sort(r.end), hours, "right")
    out["line-3 queue, mean"] = queue.mean()
    out["line-3 queue, p90"] = np.percentile(queue, 90)
    out["parts in production, mean"] = in_production.mean()
    for code, name in ((0, "L0"), (1, "L1")):
        m = inside & (r.line == code)
        if m.sum() < 100:
            continue
        wait = (r.l3_start[m] - r.ready[m]) / TICKS_PER_HOUR_INT
        out[f"{name} wait for line 3, median h"] = np.median(wait)
        out[f"{name} wait for line 3, p90 h"] = np.percentile(wait, 90)
        out[f"{name} hours in production, median"] = np.median((r.end[m] - r.start[m]) / TICKS_PER_HOUR_INT)
    return out, queue


rng = np.random.default_rng(7)
campaign = [w.index for w in weeks if 72 <= w.index <= 79]
longer_l1 = [w if w.index not in campaign else plan_weeks(cal, ["L1" if any(x.kind == "L1" for x in cal.weeks) else "L0+L1"], rng)[0] for w in weeks]

SCENARIOS = {
    "today": {},
    "volume +10%": {"volume": 1.1},
    "volume +20%": {"volume": 1.2},
    "volume +30%": {"volume": 1.3},
    "volume +20%, line 3 up to 140 h/week": {"volume": 1.2, "max_hours": 140},
    "line 3 capped at 80 h/week": {"max_hours": 80},
    "line 3 10% slower": {"capacity_scale": 0.9},
    "L1 campaign through weeks 72-79": {"weeks": longer_l1},
}

# Runs saved for the dashboard's line map (serving/twin/, read by line_map.TwinRuns)
SAVED = {
    "today": ("today", "Twin: today", "The twin replaying weeks 62-98 as they were"),
    "volume +20%": ("volume-20", "Twin: volume +20%", "20% more parts in every batch; line 3 adds hours up to 123 a week"),
    "volume +20%, line 3 up to 140 h/week": ("volume-20-140h", "Twin: +20%, line 3 to 140 h", "20% more parts; line 3 may run up to 140 hours a week"),
    "L1 campaign through weeks 72-79": ("longer-l1", "Twin: longer L1 campaign", "An L1 campaign instead of the L0-only weeks 72-79"),
}
TWIN_DIR.mkdir(parents=True, exist_ok=True)
index = []


def save_run(name, r):
    run_id, label, description = SAVED[name]
    np.savez_compressed(TWIN_DIR / f"{run_id}.npz", start=r.start, end=r.end, l3_start=r.l3_start, ready=r.ready,
                        line=r.line, response=r.response, repeats_failed=r.repeats_failed)
    np.save(TWIN_DIR / f"{run_id}_seen.npy", r.seen(cal))
    # From week 64, once the twin's own parts fill the line, to a day before the data ends
    index.append({"id": run_id, "label": label, "description": description,
                  "first_hour": float((FIRST + 2) * HOURS_PER_WEEK), "last_hour": float(h.end.max() / TICKS_PER_HOUR_INT - 24)})


rows, queues = [], {}
for name, settings in SCENARIOS.items():
    results = []
    for seed in SEEDS:
        r = run(seed, **settings)
        out, queue = flow_measures(r)
        results.append(out)
        if seed == 0:
            queues[name] = queue
            if name in SAVED:
                save_run(name, r)
    rows.append({"scenario": name, **{k: np.mean([r.get(k, np.nan) for r in results]) for k in results[0]}})
    print(f"  {name}: done")

(TWIN_DIR / "index.json").write_text(json.dumps(index, indent=2))

flow = pd.DataFrame(rows)
flow.to_csv(RESULTS_DIR / "twin_scenarios.csv", index=False)
with pd.option_context("display.width", 250, "display.max_columns", 30):
    print("\nFlow what-ifs (parts that entered in weeks %d-%d; mean of %d seeds)\n" % (MEASURE[0], MEASURE[1] - 1, len(SEEDS)))
    print(flow.set_index("scenario").T.to_string(float_format=lambda v: f"{v:,.1f}"))


# ============================================================
# 2. MONITOR STRESS TESTS
# ============================================================

L0_WEEKS, L1_CAMPAIGN = STRESS_WEEKS, (62.5, 66)
STRESS = {
    "line 3, all parts, 72 h at 2x": (L0_WEEKS, dict(hours=72, log_odds=np.log(2))),
    "line 3, all parts, 72 h at 3x": (L0_WEEKS, dict(hours=72, log_odds=np.log(3))),
    "station L3_S32 only, 72 h at 10x": (L0_WEEKS, dict(hours=72, log_odds=np.log(10), station="L3_S32")),
    "bad material: parts entering in 72 h, 3x": (L0_WEEKS, dict(hours=72, log_odds=np.log(3), by="entry")),
    "bad lots on L0: 20 entry batches at 30x": (L0_WEEKS, dict(lots="L0")),
    "bad lots on L1 in an L1 campaign: 20 entry batches at 30x": (L1_CAMPAIGN, dict(lots="L1")),
}
LOTS, LOT_ODDS, LOT_WINDOW = 20, np.log(30), 168

# Entry batches (tick, line) of the replayed weeks, to pick bad lots from
batch_ticks = {code: np.concatenate([w.index * TICKS_PER_WEEK + w.batch_tick[(w.batch_line == code) & (w.batch_size >= 5)] for w in weeks])
               for code in (0, 1)}


def disturbances_for(spec, start_hour, rng):
    if "lots" not in spec:
        return [Disturbance(start_hour=start_hour, **spec)], spec["hours"]
    code = 0 if spec["lots"] == "L0" else 1
    ticks = batch_ticks[code]
    ticks = ticks[(ticks >= start_hour * TICKS_PER_HOUR_INT) & (ticks < (start_hour + LOT_WINDOW) * TICKS_PER_HOUR_INT)]
    chosen = rng.choice(ticks, min(LOTS, len(ticks)), replace=False)
    return [Disturbance(start_hour=t / TICKS_PER_HOUR_INT, hours=0.1, log_odds=LOT_ODDS, by="entry", line=spec["lots"]) for t in chosen], LOT_WINDOW


monitor_lo = (FIRST + 1) * TICKS_PER_WEEK      # the line monitor needs a week of results first
draws = np.random.default_rng(11).uniform(0, 1, TRIALS)
baselines = {}
for trial in range(TRIALS):
    b = run(100 + trial)
    flags = batch_alert(b.start, b.end, b.response, b.repeats_failed)
    alerts = line_monitor(b.end, b.response, b.repeats, b.repeats_failed, monitor_lo, hi)
    baselines[trial] = (b, flags, alerts)
print("\nbaselines done")

stress_rows = []
for name, (span, spec) in STRESS.items():
    starts = (span[0] + draws * (span[1] - span[0])) * HOURS_PER_WEEK
    lot_rng = np.random.default_rng(21)
    for trial in range(TRIALS):
        b, b_flags, b_alerts = baselines[trial]
        problem, window = disturbances_for(spec, float(starts[trial]), lot_rng)
        d = run(100 + trial, disturbances=problem)
        assert len(d) == len(b) and (d.start == b.start).all()     # same parts: only QC outcomes differ

        caused = (d.response == 1) & (b.response == 0)              # failures the problem added
        flags = batch_alert(d.start, d.end, d.response, d.repeats_failed)
        alerts = line_monitor(d.end, d.response, d.repeats, d.repeats_failed, monitor_lo, hi)

        # A new line-monitor alert hour between the start and 72 h after the
        # window (bad material reaches QC later, so its window runs on to
        # when the hit parts have finished)
        first = int(starts[trial]) - monitor_lo // TICKS_PER_HOUR_INT
        stop = first + window + 72
        if (spec.get("by") == "entry" or "lots" in spec) and d.disturbed.any():
            stop = max(stop, int(np.percentile(d.end[d.disturbed], 95)) // TICKS_PER_HOUR_INT - monitor_lo // TICKS_PER_HOUR_INT + 72)
        new = alerts[first:stop] & ~b_alerts[first:stop]
        caught_at = np.flatnonzero(new)

        stress_rows.append({
            "problem": name,
            "trial": trial,
            "parts hit": int(d.disturbed.sum()),
            "extra failures": int(caused.sum()),
            "line monitor: new alert": bool(len(caught_at)),
            "line monitor: hours to first new alert": float(caught_at[0]) if len(caught_at) else np.nan,
            "line monitor: baseline alerting in the window": float(b_alerts[first:stop].mean()),
            "batch-mate alert: extra failures flagged before their QC": float((flags[caused] != NEVER).mean()) if caused.any() else np.nan,
            "batch-mate alert: extra parts flagged": int(((flags != NEVER) & (b_flags == NEVER)).sum()),
        })
    print(f"  {name}: done")

stress = pd.DataFrame(stress_rows)
stress.to_csv(RESULTS_DIR / "twin_stress_tests.csv", index=False)
summary = stress.groupby("problem", sort=False).agg(
    parts_hit=("parts hit", "mean"),
    extra_failures=("extra failures", "mean"),
    monitor_caught=("line monitor: new alert", "mean"),
    monitor_hours=("line monitor: hours to first new alert", "median"),
    monitor_background=("line monitor: baseline alerting in the window", "mean"),
    alert_flagged_early=("batch-mate alert: extra failures flagged before their QC", "mean"),
    alert_extra_flags=("batch-mate alert: extra parts flagged", "mean"),
)
with pd.option_context("display.width", 250):
    print(f"\nStress tests ({TRIALS} trials each; caught = a new line-monitor alert by 72 h after the problem's parts reach QC)\n")
    print(summary.to_string(float_format=lambda v: f"{v:,.2f}"))


# ============================================================
# PLOT
# ============================================================

fig, axes = plt.subplots(2, 1, figsize=(10, 8))
x = MEASURE[0] + np.arange(len(queues["today"])) / 28
for name, color in [("today", "#0b0b0b"), ("volume +10%", "#2a78d6"), ("volume +20%", "#eb6834"),
                    ("volume +30%", "#e34948"), ("volume +20%, line 3 up to 140 h/week", "#1baf7a")]:
    axes[0].plot(x, queues[name], color=color, lw=1.2, label=name)
axes[0].set_title("Parts waiting for line 3 (twin, seed 0)")
axes[0].set_xlabel("week")
axes[0].legend(frameon=False, fontsize=8)
axes[0].grid(alpha=0.3)

labels = list(summary.index)
y = np.arange(len(labels))
axes[1].barh(y - 0.18, summary["monitor_caught"], height=0.34, color="#2a78d6", label="line monitor: new alert in time")
axes[1].barh(y + 0.18, summary["alert_flagged_early"], height=0.34, color="#eb6834", label="batch-mate alert: problem's failures flagged before QC")
axes[1].set_yticks(y, labels, fontsize=8)
axes[1].set_xlim(0, 1)
axes[1].invert_yaxis()
axes[1].set_title(f"Stress tests: share caught ({TRIALS} trials each)")
axes[1].legend(frameon=False, fontsize=8, loc="lower right")
axes[1].grid(alpha=0.3, axis="x")
fig.tight_layout()
fig.savefig(PLOTS_DIR / "twin_scenarios.png", dpi=130)
print(f"\nwrote {RESULTS_DIR / 'twin_scenarios.csv'}, {RESULTS_DIR / 'twin_stress_tests.csv'} and {PLOTS_DIR / 'twin_scenarios.png'}")
