"""
PHASE 2 / STEP 2 -- how early does the failure signal become available?

For a series of "gates" along the production sequence, train the same
XGBoost model as train_xgboost.py using ONLY the numeric measurements
from stations at or before that gate. Station number = production order
(verified in analyze_dates.py), so a gate's features are exactly what
was known when the part left that station.

Each gate is evaluated three ways:

A. Random split     the fixed 400k / 100k split from train_xgboost.py.
                    The last gate reproduces its PR-AUC of 0.116.

B. Forward in time  train only on parts that had already passed final
                    QC, test on parts produced later (4 folds).

C. Weekly retrain   retrain every week and score the next week's parts.

B and C are how the model would actually be used. Failures come in
bursts (see analyze_dates.py), and a random split lets the model learn
which weeks were bad from other parts made in those same weeks.

Run from the project root:

    .venv/bin/python src/early_warning.py

Takes about 12 minutes (~125 XGBoost fits).
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import rankdata
from sklearn.metrics import (
    average_precision_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from xgboost import XGBClassifier

from production_data import (
    HOURS_PER_UNIT,
    PLOTS_DIR,
    RESULTS_DIR,
    UNITS_PER_WEEK,
    forward_folds,
    line_of,
    load_dates,
    load_numeric,
    station_number,
    station_of,
    station_times,
)

PLOTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CONFIGURATION
# ============================================================

NUM_ROWS = 500_000
TEST_SIZE = 0.20
RANDOM_STATE = 42

# "Inspect the top 1% highest-risk parts"
INSPECT_FRACTION = 0.01

# Each gate = (label, last station included). Gates are cumulative:
# "S33" means every station up to and including S33.
GATES = [
    ("After L0", 23),
    ("After L1", 25),
    ("After L2", 28),  # everything measured before L3
    ("S29", 29),
    ("S30", 30),
    ("S32", 32),
    ("S33", 33),
    ("S34", 34),
    ("S37", 37),
    ("S38", 38),
    ("Full record", 51),
]

FIRST_L3_STATION = 29

# B. Cut the timeline into equal blocks; test on every block except
#    the first (it has no history to train on).
FORWARD_BLOCKS = 5

# C. Weekly retraining
ROLLING_GATES = ["After L1", "After L2", "Full record"]
ROLLING_FIRST_WEEK = 30       # need some history to train on
ROLLING_EVERY_N_WEEKS = 3     # test every 3rd week to keep runtime down
ROLLING_MIN_PARTS = 2_000

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


def fit_predict(train_rows, test_rows, feature_cols):
    model = create_model(y[train_rows])

    model.fit(
        X[np.ix_(train_rows, feature_cols)],
        y[train_rows],
    )

    return model.predict_proba(
        X[np.ix_(test_rows, feature_cols)]
    )[:, 1]


def failures_caught(y_true, scores):
    """Failures caught by inspecting the highest-risk INSPECT_FRACTION of parts."""

    n_inspect = max(1, int(len(y_true) * INSPECT_FRACTION))
    top = np.argsort(-scores, kind="stable")[:n_inspect]

    return int(y_true[top].sum())


def evaluate(y_true, scores):
    pr_auc = average_precision_score(y_true, scores)
    caught = failures_caught(y_true, scores)

    return {
        "PR-AUC": pr_auc,
        "Lift": pr_auc / y_true.mean(),
        "ROC-AUC": roc_auc_score(y_true, scores),
        "Top 1% Caught": caught,
        "Top 1% Recall (%)": caught / y_true.sum() * 100,
    }


def gate_features(last_station):
    return np.flatnonzero(feature_station <= last_station)


def hours_left_at_gate(rows, last_station):
    """
    Hours between a part's last measurement at stations <= last_station
    and its last station overall. NaN if it had no measurement yet.
    """

    cols = np.flatnonzero(station_nums <= last_station)
    gate_time = last_seen.iloc[rows, cols].max(axis=1).to_numpy()

    return (end[rows] - gate_time) * HOURS_PER_UNIT


# ============================================================
# 1. LOAD DATA
# ============================================================

print("Loading data...")

numeric = load_numeric(NUM_ROWS)
dates = load_dates(NUM_ROWS)

# Both files are sorted by Id, so row i is the same part in both.
assert (dates["Id"].to_numpy() == numeric["Id"].to_numpy()).all()

first_seen, last_seen = station_times(dates)
del dates

feature_names = [c for c in numeric.columns if c.startswith("L")]

X = numeric[feature_names].to_numpy(dtype=np.float32)
y = numeric["Response"].to_numpy()
del numeric

feature_station = np.array([station_number(station_of(c)) for c in feature_names])
station_nums = np.array([station_number(s) for s in first_seen.columns])

start = first_seen.min(axis=1).to_numpy()
end = last_seen.max(axis=1).to_numpy()
has_dates = ~np.isnan(start)
dated_rows = np.flatnonzero(has_dates)

print("Rows loaded:", len(y))
print("Failure rate:", y.mean())


# ============================================================
# 2. HOW MUCH TIME IS LEFT AFTER EACH LINE?
#
# Median hours from a part's first measurement on a line until
# its last station (see analyze_dates.py for the full table).
# ============================================================

hours_left_after_line = {}

for line in ["L0", "L1", "L2", "L3"]:
    cols = [s for s in first_seen.columns if line_of(s) == line]
    line_start = first_seen[cols].min(axis=1).to_numpy()

    hours_left_after_line[line] = np.nanmedian(
        (end - line_start) * HOURS_PER_UNIT
    )

pre_l3_days = [hours_left_after_line[line] / 24 for line in ["L0", "L1", "L2"]]
l3_minutes = hours_left_after_line["L3"] * 60

print(
    f"\nMeasurements before L3 are taken "
    f"{min(pre_l3_days):.0f}-{max(pre_l3_days):.0f} days before a part's last station (median by line)."
)
print(f"L3 measurements are taken in its final ~{l3_minutes:.0f} minutes.")


# ============================================================
# 3. A. RANDOM SPLIT
#
# Exactly the split from train_xgboost.py, so the "Full record"
# gate reproduces PR-AUC 0.116.
# ============================================================

train_rows, test_rows = train_test_split(
    np.arange(len(y)),
    test_size=TEST_SIZE,
    random_state=RANDOM_STATE,
    stratify=y,
)

print("\n==============================")
print("A. RANDOM SPLIT")
print("==============================")

gate_results = []

for label, last_station in GATES:
    feature_cols = gate_features(last_station)

    scores = fit_predict(train_rows, test_rows, feature_cols)
    metrics = evaluate(y[test_rows], scores)

    test_failures = test_rows[y[test_rows] == 1]
    hours_left = hours_left_at_gate(test_failures, last_station)

    gate_results.append(
        {
            "Gate": label,
            "Last Station": f"S{last_station}",
            "Numeric Features": len(feature_cols),
            "Median Hours Left (failures)": np.nanmedian(hours_left),
            **{f"Random {name}": value for name, value in metrics.items()},
        }
    )

    print(
        f"{label:12} | features {len(feature_cols):4} | "
        f"PR-AUC {metrics['PR-AUC']:.4f} ({metrics['Lift']:4.1f}x) | "
        f"top 1% catches {metrics['Top 1% Caught']:3} "
        f"({metrics['Top 1% Recall (%)']:4.1f}%)"
    )


# ============================================================
# 4. B. FORWARD IN TIME
#
# Sort parts by when they entered production and cut the
# timeline into equal blocks. For each block, train only on
# parts whose LAST station came before the block began -- the
# parts whose QC result was already known -- and test on the
# block. Same split as train_xgboost.py.
# ============================================================

folds = forward_folds(start, end, n_blocks=FORWARD_BLOCKS)

print("\n==============================")
print("B. FORWARD IN TIME")
print("==============================")

print("Training sizes:", [f"{len(train):,}" for train, _ in folds])
print("Test sizes:    ", [f"{len(test):,}" for _, test in folds])

for result, (label, last_station) in zip(gate_results, GATES):
    feature_cols = gate_features(last_station)

    fold_metrics = pd.DataFrame(
        [
            evaluate(y[test], fit_predict(train, test, feature_cols))
            for train, test in folds
        ]
    )

    result["Forward Lift"] = fold_metrics["Lift"].mean()
    result["Forward Lift Min"] = fold_metrics["Lift"].min()
    result["Forward Lift Max"] = fold_metrics["Lift"].max()
    result["Forward Top 1% Recall (%)"] = fold_metrics["Top 1% Recall (%)"].mean()
    result["Forward Top 1% Recall Min (%)"] = fold_metrics["Top 1% Recall (%)"].min()
    result["Forward Top 1% Recall Max (%)"] = fold_metrics["Top 1% Recall (%)"].max()

    print(
        f"{label:12} | lift {result['Forward Lift']:4.1f}x "
        f"[{result['Forward Lift Min']:4.1f} - {result['Forward Lift Max']:4.1f}] | "
        f"top 1% catches {result['Forward Top 1% Recall (%)']:4.1f}% of failures"
    )


# ============================================================
# 5. C. WEEKLY RETRAINING
#
# The most favourable realistic setup: retrain at the start of
# every week on all parts already through QC, then score the
# parts that enter production that week.
#
# Inspection capacity is per week (top 1% of each week's
# parts), so week-to-week swings in the failure rate don't
# count as "signal".
# ============================================================

week_of_part = np.floor(start / UNITS_PER_WEEK)
parts_per_week = pd.Series(week_of_part[has_dates]).value_counts().sort_index()

eligible_weeks = [
    int(week)
    for week, parts in parts_per_week.items()
    if week >= ROLLING_FIRST_WEEK and parts >= ROLLING_MIN_PARTS
]
test_weeks = eligible_weeks[::ROLLING_EVERY_N_WEEKS]

print("\n==============================")
print("C. WEEKLY RETRAINING")
print("==============================")

print(f"Test weeks ({len(test_weeks)}):", test_weeks)

gate_lookup = dict(GATES)

for result in gate_results:
    label = result["Gate"]

    if label not in ROLLING_GATES:
        continue

    feature_cols = gate_features(gate_lookup[label])
    caught = 0
    pooled_y = []
    pooled_rank = []

    for week in test_weeks:
        week_start = week * UNITS_PER_WEEK

        train = dated_rows[end[dated_rows] < week_start]
        test = dated_rows[
            (start[dated_rows] >= week_start)
            & (start[dated_rows] < week_start + UNITS_PER_WEEK)
        ]

        scores = fit_predict(train, test, feature_cols)

        caught += failures_caught(y[test], scores)
        pooled_y.append(y[test])
        pooled_rank.append(rankdata(scores) / len(scores))

    pooled_y = np.concatenate(pooled_y)
    pooled_rank = np.concatenate(pooled_rank)

    result["Weekly Lift"] = average_precision_score(pooled_y, pooled_rank) / pooled_y.mean()
    result["Weekly Top 1% Recall (%)"] = caught / pooled_y.sum() * 100

    print(
        f"{label:12} | lift {result['Weekly Lift']:4.1f}x | "
        f"top 1% of each week catches {caught} of {pooled_y.sum()} failures "
        f"({result['Weekly Top 1% Recall (%)']:4.1f}%)"
    )


# ============================================================
# 6. WHY DOES THE RANDOM SPLIT LOOK SO MUCH BETTER?
#
# Label parts by alternating 4-week periods (even vs odd).
# Because the periods alternate, there is no trend to exploit:
# a model can only tell them apart if the measurements carry a
# fingerprint of WHEN the part was made (drift, calibration,
# product mix). Only parts that visited the line are used.
#
# ROC-AUC 0.5 = no time fingerprint.
# ============================================================

period = np.where(
    has_dates,
    np.floor(start / (4 * UNITS_PER_WEEK)) % 2,
    -1,
).astype(int)

print("\n==============================")
print("DO MEASUREMENTS ENCODE PRODUCTION TIME?")
print("==============================")

time_fingerprint = {}

for line, (lowest, highest) in {
    "L0": (0, 23),
    "L1": (24, 25),
    "L2": (26, 28),
    "L3": (29, 51),
}.items():
    feature_cols = np.flatnonzero(
        (feature_station >= lowest) & (feature_station <= highest)
    )
    visited_line = dated_rows[~np.isnan(X[np.ix_(dated_rows, feature_cols)]).all(axis=1)]

    fp_train, fp_test = train_test_split(
        visited_line,
        test_size=0.3,
        random_state=RANDOM_STATE,
    )

    model = XGBClassifier(
        n_estimators=200,
        max_depth=6,
        learning_rate=0.1,
        subsample=0.8,
        colsample_bytree=0.8,
        random_state=RANDOM_STATE,
    )
    model.fit(X[np.ix_(fp_train, feature_cols)], period[fp_train])

    time_fingerprint[line] = roc_auc_score(
        period[fp_test],
        model.predict_proba(X[np.ix_(fp_test, feature_cols)])[:, 1],
    )

    print(f"{line} measurements -> which 4-week period? ROC-AUC {time_fingerprint[line]:.3f}")


# ============================================================
# 7. SAVE RESULTS
# ============================================================

results = pd.DataFrame(gate_results)

results.to_csv(
    RESULTS_DIR / "early_warning_gates.csv",
    index=False,
)


# ============================================================
# 8. PLOT
# ============================================================

x = np.arange(len(results))
pre_l3_index = max(i for i, (_, s) in enumerate(GATES) if s < FIRST_L3_STATION)

random_recall = results["Random Top 1% Recall (%)"].to_numpy()
forward_recall = results["Forward Top 1% Recall (%)"].to_numpy()

fig, ax = plt.subplots(figsize=(10, 5.4))

ax.axvline(pre_l3_index + 0.5, color=AXIS, linewidth=1)
ax.axhline(INSPECT_FRACTION * 100, color=MUTED, linewidth=1)
ax.annotate(
    "random inspection",
    (x[-1], INSPECT_FRACTION * 100),
    xytext=(0, 4),
    textcoords="offset points",
    ha="right",
    va="bottom",
    fontsize=9,
    color=INK_SECONDARY,
)

ax.fill_between(
    x,
    results["Forward Top 1% Recall Min (%)"],
    results["Forward Top 1% Recall Max (%)"],
    color=BLUE,
    alpha=0.10,
    linewidth=0,
)

marker_style = dict(marker="o", markersize=8, markeredgecolor=SURFACE, markeredgewidth=2)

ax.plot(
    x, random_recall,
    color=MUTED, linewidth=2, label="Random split (original evaluation)", **marker_style,
)
ax.plot(
    x, forward_recall,
    color=BLUE, linewidth=2, label="Forward in time (train on the past, test on later parts)", **marker_style,
)

# Label only the last gate before L3 and the full record
for i in [pre_l3_index, len(x) - 1]:
    ax.annotate(f"{random_recall[i]:.1f}%", (x[i], random_recall[i]),
                xytext=(0, 10), textcoords="offset points", ha="center", fontsize=9, color=INK)
    ax.annotate(f"{forward_recall[i]:.1f}%", (x[i], forward_recall[i]),
                xytext=(0, 10), textcoords="offset points", ha="center", fontsize=9, color=INK)

top = random_recall.max() * 1.35
ax.set_ylim(0, top)
ax.set_xlim(-0.5, len(x) - 0.5)

ax.text(
    pre_l3_index / 2, top * 0.97,
    f"Before L3\n{min(pre_l3_days):.0f}-{max(pre_l3_days):.0f} days before the last station",
    ha="center", va="top", fontsize=9, color=INK_SECONDARY,
)
ax.text(
    (pre_l3_index + 1 + len(x) - 1) / 2, top * 0.97,
    f"L3\nfinal ~{l3_minutes:.0f} minutes",
    ha="center", va="top", fontsize=9, color=INK_SECONDARY,
)

ax.set_xticks(x)
ax.set_xticklabels([label.replace(" ", "\n") for label in results["Gate"]], fontsize=9)
ax.set_ylabel("Failures caught by inspecting the top 1% (%)")
ax.grid(axis="y")
ax.tick_params(axis="x", length=0)

ax.legend(
    loc="upper left",
    bbox_to_anchor=(0, 0.80),
    frameon=False,
    fontsize=9,
)

ax.set_title(
    "When does the failure signal become available?",
    loc="left",
    fontsize=12,
    fontweight="bold",
    pad=22,
)
ax.text(
    0, 1.04,
    "Same XGBoost model, trained only on measurements up to each point in production. "
    "Band = range across 4 forward folds.",
    transform=ax.transAxes,
    fontsize=9,
    color=INK_SECONDARY,
)

plt.tight_layout()
plt.savefig(
    PLOTS_DIR / "early_warning_curve.png",
    dpi=200,
    bbox_inches="tight",
)
plt.close()


# ============================================================
# 9. ANSWER
# ============================================================

before_l3 = results.iloc[pre_l3_index]
full = results.iloc[-1]

print("\n==============================")
print("ANSWER: HOW EARLY DOES THE SIGNAL ARRIVE?")
print("==============================")

print(f"Share of failures caught by inspecting the top 1% (random inspection: {INSPECT_FRACTION * 100:.0f}%)\n")
print(f"{'':24} {'Before L3':>10} {'Full record':>12}")

for name, column in [
    ("Random split", "Random Top 1% Recall (%)"),
    ("Forward in time", "Forward Top 1% Recall (%)"),
    ("Weekly retraining", "Weekly Top 1% Recall (%)"),
]:
    print(f"{name:24} {before_l3[column]:9.1f}% {full[column]:11.1f}%")

print(
    f"\nBefore-L3 measurements are taken {min(pre_l3_days):.0f}-{max(pre_l3_days):.0f} days "
    f"before the last station; L3 adds its signal in the final ~{l3_minutes:.0f} minutes."
)

print("\nSaved:")
print("  results/early_warning_gates.csv")
print("  results/plots/early_warning_curve.png")
