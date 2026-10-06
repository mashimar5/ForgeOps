"""
PHASE 3 / STEP 1 -- burst monitoring

Failures cluster in time (see analyze_dates.py). This script asks whether
a monitor that only uses QC results that are ALREADY KNOWN can flag risk
early -- measured forward in time.

1. How long does elevated risk last after a failure?
2. Line monitor: alert when the recent QC failure rate runs well above
   its historical level. Do parts entering production on alert days fail
   more often?
3. Batch-mate alert: parts enter production in batches (same 6-minute
   tick). When one part of a batch fails final QC, flag its batch-mates
   that are still on the line.

Only timestamps and Response are needed, so this uses all 1.18M labelled
parts. Results are measured on the last 80% of the timeline -- the same
four test periods as the forward-in-time folds.

Each part is counted once: repeat tests (about 4% of records, see
kaggle_split_repeats.py) are left out of everything that is scored,
while their QC results still count as known results, as in the API.
burst_one_record_per_part.py compares with counting every record.

Run from the project root:

    .venv/bin/python src/burst_monitoring.py

Takes about a minute.
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

from production_data import (
    DATA_DIR,
    HOURS_PER_UNIT,
    PLOTS_DIR,
    RESULTS_DIR,
    forward_folds,
    load_part_times,
    load_repeat_tests,
)
from qc_monitor import (
    TICKS_PER_HOUR,
    QCStream,
    to_ticks,
)

PLOTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CONFIGURATION
# ============================================================

# A QC result becomes known this long after the part's last station.
LABEL_DELAY_HOURS = 1

# Line monitor
MONITOR_WINDOW_HOURS = 72   # QC results from the last 3 days
ALERT_RATIO = 1.5           # alert when that rate >= 1.5x the historical rate
MIN_MONITOR_PARTS = 300     # never alert on a tiny sample

# "How long does risk last": windows of time AFTER a failed part
LAG_WINDOWS = [
    ("same 6 min", 0, 0),
    ("0-1 h", 0, 1),
    ("1-6 h", 1, 6),
    ("6-24 h", 6, 24),
    ("1-3 days", 24, 72),
    ("3-7 days", 72, 168),
    ("1-2 weeks", 168, 336),
    ("2-4 weeks", 336, 672),
    ("4-8 weeks", 672, 1344),
]

DAY_UNITS = 24 / HOURS_PER_UNIT
TICKS_PER_DAY = int(24 * TICKS_PER_HOUR)

# Chart colors (light theme)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
ORANGE = "#eb6834"
WARNING = "#fab219"

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_SECONDARY,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "text.color": INK,
})


# ============================================================
# 1. LOAD DATA
# ============================================================

print("Loading timestamps and QC results for all parts...")

part_times = load_part_times(None)
response = pd.read_csv(
    DATA_DIR / "train_numeric.csv",
    usecols=["Id", "Response"],
)

# Both files are sorted by Id, so row i is the same part in both.
assert (part_times["Id"].to_numpy() == response["Id"].to_numpy()).all()

has_dates = part_times["start"].notna().to_numpy()

start = part_times["start"].to_numpy(np.float64)[has_dates]
end = part_times["end"].to_numpy(np.float64)[has_dates]
y = response["Response"].to_numpy()[has_dates]

# Records that are a part's first test. Repeat tests are left out of
# every population below; their QC results stay in the QC stream.
first_test = ~np.isin(part_times["Id"].to_numpy()[has_dates], load_repeat_tests())

print("Parts with timestamps:", len(y))
print(f"First tests: {first_test.sum():,} ({y[first_test].mean() * 100:.3f}% fail)")
print(f"Failure rate: {y.mean() * 100:.3f}%")

# Evaluate on the four forward test periods (the last 80% of the timeline)
periods = [test_rows[first_test[test_rows]] for _, test_rows in forward_folds(start, end, n_blocks=5)]
evaluation = np.concatenate(periods)

print(f"Evaluation parts: {len(evaluation):,} ({y[evaluation].sum():,} failures)")


def summarize_flag(rows, flagged):
    """How much production a flag covers, and how many failures it catches."""

    hit = flagged[rows]
    outcome = y[rows]

    return {
        "Production Flagged (%)": hit.mean() * 100,
        "Failure Rate Flagged (%)": outcome[hit].mean() * 100 if hit.any() else np.nan,
        "Failure Rate Not Flagged (%)": outcome[~hit].mean() * 100,
        "Risk Ratio": outcome[hit].mean() / outcome[~hit].mean() if hit.any() else np.nan,
        "Failures Caught (%)": outcome[hit].sum() / outcome.sum() * 100,
    }


# ============================================================
# 2. HOW LONG DOES ELEVATED RISK LAST?
#
# For every failed part, look at the parts that came 0-1 h,
# 1-6 h, ... after it, and compare their failure rate with the
# parts that came the same time after ANY part. Done twice:
# by entry time and by final-QC time.
#
# This is descriptive (it uses hindsight). It tells us which
# time scales a monitor could exploit. First tests only: a repeat
# enters with its own part, so it would count as a failing
# neighbour of itself.
# ============================================================

def risk_after_failures(times, y):
    ticks = to_ticks(times)
    order = np.argsort(ticks, kind="stable")
    ticks = ticks[order]
    outcome = y[order]
    cum_fail = np.r_[0, np.cumsum(outcome)]

    def failure_rate_after(anchors, low_hours, high_hours):
        t = ticks[anchors]

        if high_hours == 0:
            # Same 6-minute tick, excluding the anchor itself
            lo = np.searchsorted(ticks, t, "left")
            hi = np.searchsorted(ticks, t, "right")
            parts = hi - lo - 1
            fails = cum_fail[hi] - cum_fail[lo] - outcome[anchors]
        else:
            lo = np.searchsorted(ticks, t + low_hours * TICKS_PER_HOUR, "right")
            hi = np.searchsorted(ticks, t + high_hours * TICKS_PER_HOUR, "right")
            parts = hi - lo
            fails = cum_fail[hi] - cum_fail[lo]

        return fails.sum() / parts.sum()

    failed = np.flatnonzero(outcome == 1)
    everyone = np.arange(len(ticks))

    return [
        failure_rate_after(failed, low, high) / failure_rate_after(everyone, low, high)
        for _, low, high in LAG_WINDOWS
    ]


lag_risk = pd.DataFrame(
    {
        "Window After A Failure": [label for label, _, _ in LAG_WINDOWS],
        "Risk Ratio (by entry time)": risk_after_failures(start[first_test], y[first_test]),
        "Risk Ratio (by final-QC time)": risk_after_failures(end[first_test], y[first_test]),
    }
)

print("\n==============================")
print("HOW LONG DOES ELEVATED RISK LAST?")
print("==============================")

print(lag_risk.to_string(index=False, float_format=lambda v: f"{v:.2f}x"))

lag_risk.to_csv(
    RESULTS_DIR / "burst_lag_risk.csv",
    index=False,
)


# ============================================================
# 3. LINE MONITOR
#
# At the start of every production day, compare the QC failure
# rate over the last MONITOR_WINDOW_HOURS with the historical
# rate -- both using only QC results already known. Parts that
# enter production on an alert day are "flagged".
# ============================================================

qc = QCStream(start, end, y, LABEL_DELAY_HOURS)

entry_day = to_ticks(start) // TICKS_PER_DAY
day_start = np.arange(entry_day.max() + 1) * DAY_UNITS


def alert_days(window_hours, ratio):
    rate, parts = qc.failure_rate(day_start, window_hours)
    history = qc.historical_rate(day_start)

    return (parts >= MIN_MONITOR_PARTS) & (rate >= ratio * history)


on_alert_day = alert_days(MONITOR_WINDOW_HOURS, ALERT_RATIO)
line_flag = on_alert_day[entry_day]

line_monitor = pd.DataFrame(
    [
        {"Period": "All", **summarize_flag(evaluation, line_flag)},
        *[
            {"Period": str(i), **summarize_flag(rows, line_flag)}
            for i, rows in enumerate(periods, start=1)
        ],
    ]
)

lead_hours_on_alert = (end - start)[evaluation][line_flag[evaluation] & (y[evaluation] == 1)] * HOURS_PER_UNIT

print("\n==============================")
print("LINE MONITOR")
print("==============================")

print(
    f"Alert at the start of a production day when the QC failure rate over the last "
    f"{MONITOR_WINDOW_HOURS} h >= {ALERT_RATIO}x the historical rate."
)
print(line_monitor.to_string(index=False, float_format=lambda v: f"{v:.2f}"))
print(
    f"Failing parts that entered on alert days were flagged a median of "
    f"{np.median(lead_hours_on_alert):.0f} h before their last station."
)

# Sensitivity to the window and threshold (pooled over the evaluation period)
sensitivity = pd.DataFrame(
    [
        {
            "Window (h)": window,
            "Alert Ratio": ratio,
            **summarize_flag(evaluation, alert_days(window, ratio)[entry_day]),
        }
        for window in [24, 72, 168]
        for ratio in [1.25, 1.5, 2.0]
    ]
)

print("\nSensitivity:")
print(sensitivity.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

line_monitor.to_csv(RESULTS_DIR / "burst_line_monitor.csv", index=False)
sensitivity.to_csv(RESULTS_DIR / "burst_line_monitor_sensitivity.csv", index=False)


# ============================================================
# 4. BATCH-MATE ALERT
#
# Parts that enter production in the same 6-minute tick form a
# batch. Batch-mates take different routes, so they reach final
# QC at different times. As soon as one batch-mate's failure is
# known, flag every batch-mate that has not reached its last
# station yet.
# ============================================================

end_tick = to_ticks(end)

batch_rows = []

for delay in [1, 6, 24]:
    # When the first failure in each part's entry batch became known.
    # A part's own failure is only known after its last station, so
    # "known before this part's end" always means a batch-mate failed.
    first_failure = QCStream(start, end, y, delay).first_batch_failure_known(start)

    flagged = first_failure < end_tick
    caught = evaluation[flagged[evaluation] & (y[evaluation] == 1)]
    lead_hours = (end_tick[caught] - first_failure[caught]) / TICKS_PER_HOUR

    batch_rows.append(
        {
            "QC Result Delay (h)": delay,
            **summarize_flag(evaluation, flagged),
            "Median Lead Time (h)": np.median(lead_hours),
        }
    )

    if delay == LABEL_DELAY_HOURS:
        batch_periods = pd.DataFrame(
            [
                {"Period": str(i), **summarize_flag(rows, flagged)}
                for i, rows in enumerate(periods, start=1)
            ]
        )

batch_alert = pd.DataFrame(batch_rows)

print("\n==============================")
print("BATCH-MATE ALERT")
print("==============================")

print(
    "Flag a part when a batch-mate (same entry tick) has already failed final QC "
    "and this part has not reached its last station yet."
)
print(batch_alert.to_string(index=False, float_format=lambda v: f"{v:.2f}"))
print(f"\nBy forward test period (QC result delay {LABEL_DELAY_HOURS} h):")
print(batch_periods.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

batch_alert.to_csv(RESULTS_DIR / "burst_batch_alert.csv", index=False)


# ============================================================
# 5. PLOT: HOW LONG DOES ELEVATED RISK LAST?
# ============================================================

x = np.arange(len(lag_risk))

fig, ax = plt.subplots(figsize=(10, 4.8))

ax.axhline(1, color=MUTED, linewidth=1)
ax.annotate(
    "no clustering",
    (x[-1], 1),
    xytext=(0, -4),
    textcoords="offset points",
    ha="right",
    va="top",
    fontsize=9,
    color=INK_SECONDARY,
)

marker_style = dict(marker="o", markersize=8, markeredgecolor=SURFACE, markeredgewidth=2)

for column, color, label in [
    ("Risk Ratio (by final-QC time)", BLUE, "Parts reaching final QC after a failed part"),
    ("Risk Ratio (by entry time)", ORANGE, "Parts entering production after a failed part"),
]:
    values = lag_risk[column].to_numpy()
    ax.plot(x, values, color=color, linewidth=2, label=label, **marker_style)
    ax.annotate(
        f"{values[0]:.1f}x",
        (x[0], values[0]),
        xytext=(10, 0),
        textcoords="offset points",
        va="center",
        fontsize=9,
        color=INK,
    )

ax.set_xticks(x)
ax.set_xticklabels(lag_risk["Window After A Failure"], fontsize=9)
ax.set_xlim(-0.4, len(x) - 0.6)
ax.set_ylim(0, lag_risk.iloc[:, 1:].to_numpy().max() * 1.15)
ax.set_ylabel("Failure rate vs. after any part (x)")
ax.set_xlabel("Time after the failed part")
ax.grid(axis="y")
ax.tick_params(axis="x", length=0)
ax.legend(loc="upper right", frameon=False, fontsize=9)

ax.set_title(
    "How long does elevated risk last after a failure?",
    loc="left",
    fontsize=12,
    fontweight="bold",
    pad=22,
)
ax.text(
    0, 1.04,
    "A sharp batch effect within the hour, then a weak ~1.2x elevation that lasts for weeks",
    transform=ax.transAxes,
    fontsize=9,
    color=INK_SECONDARY,
)

plt.tight_layout()
plt.savefig(PLOTS_DIR / "burst_decay.png", dpi=200, bbox_inches="tight")
plt.close()


# ============================================================
# 6. PLOT: LINE MONITOR OVER TIME
#
# Daily failure rate of parts entering production (7-day
# average, for readability only), with alert days shaded.
# ============================================================

eval_days = np.unique(entry_day[evaluation])
first_day, last_day = eval_days.min(), eval_days.max()

daily = (
    pd.DataFrame({"day": entry_day[first_test], "failed": y[first_test]})
    .groupby("day")["failed"]
    .agg(["sum", "size"])
    .reindex(np.arange(first_day, last_day + 1), fill_value=0)
)
parts_in_window = daily["size"].rolling(7, center=True, min_periods=4).sum()

# Leave gaps blank where the line was (nearly) shut down
smoothed = (
    daily["sum"].rolling(7, center=True, min_periods=4).sum()
    / parts_in_window
    * 100
).where(parts_in_window >= 2_000)

fig, ax = plt.subplots(figsize=(10, 4.4))

alert_runs = np.flatnonzero(on_alert_day[first_day:last_day + 1])

for day in alert_runs + first_day:
    ax.axvspan(day - 0.5, day + 0.5, color=WARNING, alpha=0.25, linewidth=0)

ax.plot(daily.index, smoothed, color=BLUE, linewidth=2)
ax.axhline(y[evaluation].mean() * 100, color=MUTED, linewidth=1)

ticks = np.arange(first_day, last_day + 1, 70)
ax.set_xticks(ticks)
ax.set_xticklabels([f"week {int((t - first_day) / 7)}" for t in ticks])
ax.set_xlim(first_day, last_day)
ax.set_ylim(0, smoothed.max() * 1.45)  # headroom for the legend
ax.set_ylabel("Failure rate of entering parts (%)")
ax.grid(axis="y")

ax.legend(
    handles=[
        plt.Line2D([], [], color=BLUE, linewidth=2, label="Failure rate of parts entering that day (7-day average)"),
        plt.Line2D([], [], color=MUTED, linewidth=1, label="Average over the period"),
        Patch(color=WARNING, alpha=0.4, label="Alert day (decided from QC results already known)"),
    ],
    loc="upper right",
    frameon=False,
    fontsize=9,
)

pooled = line_monitor.iloc[0]

ax.set_title(
    "Line monitor: alert when recent QC failures run high",
    loc="left",
    fontsize=12,
    fontweight="bold",
    pad=22,
)
ax.text(
    0, 1.04,
    f"Parts entering on alert days failed at {pooled['Failure Rate Flagged (%)']:.2f}% vs "
    f"{pooled['Failure Rate Not Flagged (%)']:.2f}% on other days",
    transform=ax.transAxes,
    fontsize=9,
    color=INK_SECONDARY,
)

plt.tight_layout()
plt.savefig(PLOTS_DIR / "burst_line_monitor.png", dpi=200, bbox_inches="tight")
plt.close()


# ============================================================
# 7. SUMMARY
# ============================================================

batch = batch_alert.loc[batch_alert["QC Result Delay (h)"] == LABEL_DELAY_HOURS].iloc[0]

print("\n==============================")
print("SUMMARY (forward in time, last 80% of the timeline)")
print("==============================")

print(
    f"Line monitor:     flags {pooled['Production Flagged (%)']:.1f}% of production, "
    f"{pooled['Risk Ratio']:.2f}x failure rate, "
    f"catches {pooled['Failures Caught (%)']:.1f}% of failures "
    f"(median {np.median(lead_hours_on_alert):.0f} h before the last station)"
)
print(
    f"Batch-mate alert: flags {batch['Production Flagged (%)']:.1f}% of production, "
    f"{batch['Risk Ratio']:.2f}x failure rate, "
    f"catches {batch['Failures Caught (%)']:.1f}% of failures "
    f"(median {batch['Median Lead Time (h)']:.0f} h before the last station)"
)

print("\nSaved:")
print("  results/burst_lag_risk.csv")
print("  results/burst_line_monitor.csv")
print("  results/burst_line_monitor_sensitivity.csv")
print("  results/burst_batch_alert.csv")
print("  results/plots/burst_decay.png")
print("  results/plots/burst_line_monitor.png")
