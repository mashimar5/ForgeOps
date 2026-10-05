"""
PHASE 3 / STEP 2 -- do burst-monitor features help the XGBoost model?

burst_monitoring.py found monitor signals that hold up forward in time: a
line-level QC failure-rate monitor and a batch-mate alert. This script
checks, forward in time, whether they improve the model:

A. Final-QC triage   score each part at its last station
                     (full measurement record + monitor features at that moment)
B. Early warning     score each part still in production 1, 3 and 7 days
                     after it entered (measurements so far + monitors then)
C. Batch-mate alert  the event-driven rule, split by WHEN its flag fires

Monitor features only use QC results already known at the decision time
(1-hour delay). Measurements come from the usual first 500k rows; the QC
stream uses all 1.18M labelled parts. Same 4 forward test periods as
train_xgboost.py.

Run from the project root:

    .venv/bin/python src/monitor_model.py

Takes about 5 minutes.
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score
from xgboost import XGBClassifier

from production_data import (
    DATA_DIR,
    HOURS_PER_UNIT,
    PLOTS_DIR,
    RESULTS_DIR,
    forward_folds,
    load_dates,
    load_numeric,
    station_of,
    station_times,
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

NUM_ROWS = 500_000
RANDOM_STATE = 42
FORWARD_BLOCKS = 5

# A QC result becomes known this long after the part's last station.
LABEL_DELAY_HOURS = 1

# B. Early-warning checkpoints: hours after a part entered production
CHECKPOINT_HOURS = [24, 72, 168]

# C. When the batch-mate flag fires, relative to the flagged part's entry
FLAG_TIMING_BUCKETS = [
    ("< 1 day", 0, 24),
    ("1-3 days", 24, 72),
    ("3-7 days", 72, 168),
    ("1-2 weeks", 168, 336),
    ("> 2 weeks", 336, np.inf),
]
# Refined rule: only flags that fire >= 3 days after entry. This cutoff was
# chosen after seeing the pooled results; batch_alert_cutoff.py checks it
# honestly (chosen on past data only, it gives 3.3x instead of 4.6x).
LATE_FLAG_HOURS = 72

# Chart colors (light theme)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"

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
# HELPER FUNCTIONS
# ============================================================

def create_model(y):
    """Same model as train_xgboost.py."""

    positive = y.sum()
    negative = len(y) - positive

    return XGBClassifier(
        n_estimators=300,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=negative / positive,
        eval_metric="aucpr",
        random_state=RANDOM_STATE,
    )


def evaluate(y_true, scores, all_test_failures):
    """Lift, top-1% recall, and the share of ALL test-period failures caught."""

    n_inspect = int(len(y_true) * 0.01)
    caught = y_true[np.argsort(-scores, kind="stable")[:n_inspect]].sum()

    return {
        "Lift": average_precision_score(y_true, scores) / y_true.mean(),
        "Top 1% Recall (%)": caught / y_true.sum() * 100,
        "Of All Test Failures (%)": caught / all_test_failures * 100,
    }


def monitor_features(rows, t, raw_rates=False):
    """
    Monitor features for parts `rows` at times t, from QC results known
    before t.

    Default (clean) set: recent failure rates RELATIVE to the historical
    rate, plus batch-mate counts.

    raw_rates=True adds absolute rates and the historical rate itself.
    The historical rate drifts steadily over time, so it acts like a
    clock -- it lets the model learn period-specific patterns.
    """

    history = qc.historical_rate(t)
    columns = []

    for window in [6, 72]:
        rate, parts = qc.failure_rate(t, window)
        columns.append(
            np.divide(rate, history, out=np.full(len(rate), np.nan), where=(parts > 0) & (history > 0))
        )

    failed, passed = qc.batch_mates_known(start[rows], t)
    columns += [failed, passed]

    if raw_rates:
        for window in [6, 24, 72, 168]:
            rate, parts = qc.failure_rate(t, window)
            columns.append(np.where(parts > 0, rate, np.nan))

        columns += [history, qc.batch_size(start[rows])]

    return np.column_stack(columns).astype(np.float32)


def compare_feature_sets(decision_point, rows, feature_sets):
    """Train and test every feature set on the 4 forward folds."""

    position = np.full(len(y), -1)
    position[rows] = np.arange(len(rows))

    results = []

    for set_name, features in feature_sets.items():
        fold_metrics = []

        for train, test in folds:
            train_pos = position[train][position[train] >= 0]
            test_pos = position[test][position[test] >= 0]
            y_rows = y[rows]

            model = create_model(y_rows[train_pos])
            model.fit(features[train_pos], y_rows[train_pos])
            scores = model.predict_proba(features[test_pos])[:, 1]

            fold_metrics.append(evaluate(y_rows[test_pos], scores, y[test].sum()))

        fold_metrics = pd.DataFrame(fold_metrics)

        results.append(
            {
                "Decision Point": decision_point,
                "Feature Set": set_name,
                "Parts In Production": len(rows),
                "Lift": fold_metrics["Lift"].mean(),
                "Lift Min": fold_metrics["Lift"].min(),
                "Lift Max": fold_metrics["Lift"].max(),
                "Top 1% Recall (%)": fold_metrics["Top 1% Recall (%)"].mean(),
                "Top 1% Recall Min (%)": fold_metrics["Top 1% Recall (%)"].min(),
                "Top 1% Recall Max (%)": fold_metrics["Top 1% Recall (%)"].max(),
                "Of All Test Failures (%)": fold_metrics["Of All Test Failures (%)"].mean(),
            }
        )

        print(
            f"  {set_name:32} lift {results[-1]['Lift']:5.2f}x "
            f"[{results[-1]['Lift Min']:4.1f}-{results[-1]['Lift Max']:4.1f}] | "
            f"top 1% recall {results[-1]['Top 1% Recall (%)']:5.1f}% "
            f"[{results[-1]['Top 1% Recall Min (%)']:4.1f}-{results[-1]['Top 1% Recall Max (%)']:4.1f}]"
        )

    return results


# ============================================================
# 1. LOAD DATA
# ============================================================

print("Loading timestamps for all parts...")

dates = load_dates(None)
first_seen, last_seen = station_times(dates)
all_ids = dates["Id"].to_numpy()
del dates

response = pd.read_csv(
    DATA_DIR / "train_numeric.csv",
    usecols=["Id", "Response"],
)
assert (response["Id"].to_numpy() == all_ids).all()

all_start = first_seen.min(axis=1).to_numpy(np.float64)
all_end = last_seen.max(axis=1).to_numpy(np.float64)
all_y = response["Response"].to_numpy()
labelled = ~np.isnan(all_start)

qc = QCStream(
    all_start[labelled],
    all_end[labelled],
    all_y[labelled],
    LABEL_DELAY_HOURS,
)

print("Loading measurements...")

numeric = load_numeric(NUM_ROWS)

# The first NUM_ROWS rows of every file are the same parts
assert (numeric["Id"].to_numpy() == all_ids[:NUM_ROWS]).all()

feature_names = [c for c in numeric.columns if c.startswith("L")]
X = numeric[feature_names].to_numpy(dtype=np.float32)
y = numeric["Response"].to_numpy()
del numeric

start = all_start[:NUM_ROWS]
end = all_end[:NUM_ROWS]
has_dates = ~np.isnan(start)

# When each station was finished (NaN = never visited), and which
# station every feature belongs to
station_done = last_seen.to_numpy()[:NUM_ROWS]
station_list = list(last_seen.columns)
feature_station = np.array([station_list.index(station_of(c)) for c in feature_names])

folds = forward_folds(start, end, n_blocks=FORWARD_BLOCKS)

print("Parts with measurements:", len(y))


# ============================================================
# 2. A. FINAL-QC TRIAGE
#
# Score each part at its last station: full measurement record
# plus monitor features computed at that moment.
# ============================================================

print("\n==============================")
print("A. FINAL-QC TRIAGE")
print("==============================")

rows = np.flatnonzero(has_dates)
t = end[rows]

clean_monitors = monitor_features(rows, t)
raw_monitors = monitor_features(rows, t, raw_rates=True)

results = compare_feature_sets(
    "At final QC",
    rows,
    {
        "Measurements": X[rows],
        "Monitors only": clean_monitors,
        "Measurements + monitors": np.hstack([X[rows], clean_monitors]),
        "Measurements + raw monitor rates": np.hstack([X[rows], raw_monitors]),
    },
)


# ============================================================
# 3. B. EARLY WARNING AT FIXED CHECKPOINTS
#
# Parts still in production h hours after they entered. Only
# measurements from stations finished by then are kept.
# ============================================================

print("\n==============================")
print("B. EARLY WARNING AT FIXED CHECKPOINTS")
print("==============================")

for hours in CHECKPOINT_HOURS:
    checkpoint = start + hours / HOURS_PER_UNIT

    rows = np.flatnonzero(has_dates & (end > checkpoint))
    t = checkpoint[rows]

    # Hide measurements from stations not finished by the checkpoint
    measured = X[rows].copy()

    for s in range(len(station_list)):
        not_done_yet = np.flatnonzero(~(station_done[rows, s] <= t))
        feature_cols = np.flatnonzero(feature_station == s)

        if len(not_done_yet) and len(feature_cols):
            measured[np.ix_(not_done_yet, feature_cols)] = np.nan

    monitors = monitor_features(rows, t)

    print(f"\n{hours} h after entry: {len(rows):,} parts still in production")

    results += compare_feature_sets(
        f"{hours} h after entry",
        rows,
        {
            "Measurements": measured,
            "Monitors only": monitors,
            "Measurements + monitors": np.hstack([measured, monitors]),
        },
    )

    del measured

results = pd.DataFrame(results)

results.to_csv(
    RESULTS_DIR / "monitor_model.csv",
    index=False,
)


# ============================================================
# 4. C. BATCH-MATE ALERT BY FLAG TIMING
#
# The event-driven rule from burst_monitoring.py, on all labelled
# parts: flag a part when a batch-mate's failure becomes known
# before this part reaches its last station. Split by how long
# after the flagged part's entry the flag fired, for every
# forward test period separately.
# ============================================================

all_rows = np.flatnonzero(labelled)
s_all, e_all, y_lab = all_start[all_rows], all_end[all_rows], all_y[all_rows]

periods = [test for _, test in forward_folds(s_all, e_all, n_blocks=FORWARD_BLOCKS)]
evaluation = np.concatenate(periods)

first_failure = qc.first_batch_failure_known(s_all)
entry_tick = to_ticks(s_all)
end_tick = to_ticks(e_all)

flagged = first_failure < end_tick
fired_after_entry_h = np.where(flagged, (first_failure - entry_tick) / TICKS_PER_HOUR, np.nan)
lead_h = np.where(flagged, (end_tick - first_failure) / TICKS_PER_HOUR, np.nan)

timing_rows = []

for label, low, high in FLAG_TIMING_BUCKETS:
    in_bucket = flagged & (fired_after_entry_h >= low) & (fired_after_entry_h < high)

    row = {"Flag Fired After Entry": label}

    for name, rows in [("All", evaluation)] + [(str(i), p) for i, p in enumerate(periods, start=1)]:
        hit = rows[in_bucket[rows]]
        row[f"Parts ({name})"] = len(hit)
        row[f"Failures ({name})"] = int(y_lab[hit].sum())
        row[f"Risk Ratio ({name})"] = y_lab[hit].mean() / y_lab[rows].mean() if len(hit) else np.nan

    timing_rows.append(row)

timing = pd.DataFrame(timing_rows)

print("\n==============================")
print("C. BATCH-MATE ALERT BY FLAG TIMING")
print("==============================")

print("Risk ratio of flagged parts vs. the period's failure rate, by when the flag fired:")
print(
    timing[["Flag Fired After Entry", "Parts (All)", "Risk Ratio (All)"]
           + [f"Risk Ratio ({i})" for i in range(1, len(periods) + 1)]]
    .to_string(index=False, float_format=lambda v: f"{v:.2f}")
)

timing.to_csv(
    RESULTS_DIR / "batch_alert_timing.csv",
    index=False,
)

# Refined rule: only flags that fire >= LATE_FLAG_HOURS after entry
late = flagged & (fired_after_entry_h >= LATE_FLAG_HOURS)

print(f"\nRefined rule: alert only when the flag fires >= {LATE_FLAG_HOURS} h after entry")

for name, rows in [("All", evaluation)] + [(str(i), p) for i, p in enumerate(periods, start=1)]:
    for rule_name, flag in [("any flag", flagged), ("late flags only", late)]:
        hit = rows[flag[rows]]
        caught = hit[y_lab[hit] == 1]

        print(
            f"  period {name:3} {rule_name:15} | {len(hit) / len(rows) * 100:4.2f}% of production | "
            f"risk {y_lab[hit].mean() / y_lab[rows].mean():4.2f}x | "
            f"catches {len(caught) / y_lab[rows].sum() * 100:4.1f}% of failures | "
            f"median lead {np.median(lead_h[caught]) / 24:4.1f} days"
        )

# The alert needs long waits. Long waits come from parts that enter at L1
# (S24/S25), and the factory runs L1 in campaigns -- it was barely used in
# the most recent period.
hours_on_line = (e_all - s_all) * HOURS_PER_UNIT

print("\nShare of parts on the line for more than a week, by period:")
for i, rows in enumerate(periods, start=1):
    print(f"  period {i}: {(hours_on_line[rows] > 168).mean() * 100:4.1f}%")


# ============================================================
# 5. PLOT: FLAG STRENGTH BY WHEN IT FIRES
# ============================================================

def wilson_interval(failures, parts, z=1.96):
    """95% confidence interval for a failure rate."""

    rate = failures / parts
    center = (rate + z ** 2 / (2 * parts)) / (1 + z ** 2 / parts)
    half = z * np.sqrt(rate * (1 - rate) / parts + z ** 2 / (4 * parts ** 2)) / (1 + z ** 2 / parts)

    return center - half, center + half


x = np.arange(len(timing))
pooled = timing["Risk Ratio (All)"].to_numpy()

# Per-period values are too noisy to plot (often only 1-3 expected
# failures per bucket), so show the pooled value with its 95% CI
base_rate = y_lab[evaluation].mean()
low, high = wilson_interval(timing["Failures (All)"].to_numpy(), timing["Parts (All)"].to_numpy())
low, high = low / base_rate, high / base_rate

fig, ax = plt.subplots(figsize=(9, 4.6))

ax.axhline(1, color=MUTED, linewidth=1)
ax.bar(x, pooled, width=0.5, color=BLUE)
ax.errorbar(x, pooled, yerr=[pooled - low, high - pooled], fmt="none", ecolor=INK_SECONDARY, elinewidth=1.2, capsize=4)

for xi, value, top in zip(x, pooled, high):
    ax.annotate(f"{value:.1f}x", (xi, top), xytext=(0, 4), textcoords="offset points",
                ha="center", va="bottom", fontsize=9, color=INK)

# In the gap between the first two bars, just under the reference line
ax.annotate("no extra risk", ((x[0] + x[1]) / 2, 1), xytext=(0, -3), textcoords="offset points",
            ha="center", va="top", fontsize=9, color=INK_SECONDARY)

ax.set_xticks(x)
ax.set_xticklabels(
    [f"{label}\n{parts:,} parts" for label, parts in zip(timing["Flag Fired After Entry"], timing["Parts (All)"])],
    fontsize=9,
)
ax.set_xlabel("When the batch-mate's failure became known, after the flagged part entered production")
ax.set_ylabel("Failure rate vs. average (x)")
ax.set_ylim(0, high.max() * 1.15)
ax.grid(axis="y")
ax.tick_params(axis="x", length=0)

ax.set_title(
    "Batch-mate failures that surface late are the strongest warning",
    loc="left",
    fontsize=12,
    fontweight="bold",
    pad=22,
)
ax.text(
    0, 1.04,
    "Parts still in production when a batch-mate fails final QC (4 forward test periods pooled; bars show 95% CI)",
    transform=ax.transAxes,
    fontsize=9,
    color=INK_SECONDARY,
)

plt.tight_layout()
plt.savefig(PLOTS_DIR / "batch_alert_timing.png", dpi=200, bbox_inches="tight")
plt.close()

print("\nSaved:")
print("  results/monitor_model.csv")
print("  results/batch_alert_timing.csv")
print("  results/plots/batch_alert_timing.png")
