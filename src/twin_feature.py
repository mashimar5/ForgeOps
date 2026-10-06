"""
TWIN FEATURE TEST -- does "has a twin" improve the final-QC model?

About 4% of records share every measurement and timestamp with another
record ("twins", see production_data.twin_groups). Twins fail about 7x as
often as other records and hold about a quarter of all failures, so "has a
twin" looked like a strong candidate feature -- and since twins share their
timestamps, it looked known at final QC.

Checked forward in time, with the same 4 test periods and model as
train_xgboost.py:

1. Do twins fail more often in every test period, or only overall?
2. Do twin features improve the model? Measurements alone, plus "has a
   twin", plus twin-group size; each with 3 random seeds to show how much
   results move by chance alone.
3. Does the model already rank twins high?
4. Is twin status really known at final QC?

Result: the twin features raise forward-in-time lift from about 7.4x to
9.2x, in every period and with every seed -- but the gain is a leak. Within
a twin group the failures sit on the first record (lowest Id): in pairs
with one failure, the failing record comes first 95% of the time. Twins
are most likely repeat records of ONE part (tested again, with its
production record copied), and a failed test makes a repeat far more
likely. So "has a twin" isn't known when a part is first tested, and the
feature is not used.

Run from the project root:

    .venv/bin/python src/twin_feature.py

Takes about 6 minutes and ~25 GB of RAM.
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
    PLOTS_DIR,
    RESULTS_DIR,
    forward_folds,
    load_dates,
    twin_groups,
)

PLOTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CONFIGURATION
# ============================================================

FORWARD_BLOCKS = 5

# 42 is the seed train_xgboost.py uses. The model samples rows and
# columns at random, so other seeds show the run-to-run noise.
SEEDS = [42, 1, 2]

# Risk bands by rank within each test period (upper edge, as a
# fraction of the period's parts)
RISK_BANDS = [
    ("Top 1%", 0.01),
    ("1-5%", 0.05),
    ("5-20%", 0.20),
    ("Bottom 80%", 1.00),
]

METRICS = ["Lift", "Top 1% Recall (%)", "Top 5% Recall (%)"]

# Chart colors (light theme)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
ORANGE = "#eb6834"

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

def create_model(y, seed):
    """Same model as train_xgboost.py, with the random seed as a parameter."""

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
        random_state=seed,
    )


def recall_at(y_true, scores, fraction):
    """% of failures caught by inspecting the highest-scored fraction of parts."""

    n_inspect = int(len(y_true) * fraction)
    caught = y_true[np.argsort(-scores, kind="stable")[:n_inspect]].sum()

    return caught / y_true.sum() * 100


def evaluate(y_true, scores):
    return {
        "Lift": average_precision_score(y_true, scores) / y_true.mean(),
        "Top 1% Recall (%)": recall_at(y_true, scores, 0.01),
        "Top 5% Recall (%)": recall_at(y_true, scores, 0.05),
    }


def wilson_interval(failures, records, z=1.96):
    """95% confidence interval for a failure rate."""

    rate = failures / records
    center = (rate + z ** 2 / (2 * records)) / (1 + z ** 2 / records)
    half = z * np.sqrt(rate * (1 - rate) / records + z ** 2 / (4 * records ** 2)) / (1 + z ** 2 / records)

    return center - half, center + half


# ============================================================
# 1. LOAD DATA
# ============================================================

print("Loading measurements...")

header = pd.read_csv(DATA_DIR / "train_numeric.csv", nrows=0).columns
feature_names = [c for c in header if c.startswith("L")]

numeric = pd.read_csv(
    DATA_DIR / "train_numeric.csv",
    dtype={c: np.float32 for c in feature_names},
)

print("Loading timestamps...")

# Kept whole (not just load_part_times) to compare twins' full
# timestamp records below
dates = load_dates(None)
timestamps = dates.drop(columns="Id")

# Both files are sorted by Id, so row i is the same part in both
assert (dates["Id"].to_numpy() == numeric["Id"].to_numpy()).all()

ids = dates["Id"].to_numpy()
start = timestamps.min(axis=1).to_numpy()
end = timestamps.max(axis=1).to_numpy()
y = numeric["Response"].to_numpy()

print(f"Parts: {len(y):,} ({y.mean() * 100:.2f}% fail)")


# ============================================================
# 2. TWIN RECORDS
#
# has_twin         another record has exactly the same measurements
#                  and entered in the same tick
# twin_group_size  how many records share it (1 = no twin)
# ============================================================

twin_group = twin_groups(numeric[feature_names], start)

has_twin = twin_group >= 0
group_size = np.ones(len(y), dtype=np.int32)
group_size[has_twin] = np.bincount(twin_group[has_twin])[twin_group[has_twin]]

# Twins match on the whole timestamp record, not just entry and end
members = np.flatnonzero(has_twin)
stamp_bits = timestamps.iloc[members].to_numpy(np.float32).view(np.uint32)
first_member = pd.Series(np.arange(len(members))).groupby(twin_group[members]).transform("first").to_numpy()
same_timestamps = pd.Series((stamp_bits == stamp_bits[first_member]).all(axis=1)).groupby(twin_group[members]).all()

n_date_columns = timestamps.shape[1]
del dates, timestamps, stamp_bits

# Measurements first, then the twin features, so each feature set
# is just the first n columns
X = np.empty((len(y), len(feature_names) + 2), dtype=np.float32)
X[:, :len(feature_names)] = numeric[feature_names].to_numpy()
X[:, -2] = has_twin
X[:, -1] = group_size
del numeric

FEATURE_SETS = {
    "Measurements": len(feature_names),
    "+ has twin": len(feature_names) + 1,
    "+ has twin + group size": len(feature_names) + 2,
}

measured = (~np.isnan(X[:, :len(feature_names)])).sum(axis=1)
groups_by_size = pd.Series(np.bincount(twin_group[has_twin])).value_counts().sort_index()

print(f"\nTwin records: {has_twin.sum():,} ({has_twin.mean() * 100:.1f}%) in {twin_group.max() + 1:,} groups")
print("Groups by size:", ", ".join(f"{size}: {count:,}" for size, count in groups_by_size.items()))
print(f"Failure rate: twins {y[has_twin].mean() * 100:.2f}%, other records {y[~has_twin].mean() * 100:.2f}%")
print(f"Failures that are twins: {y[has_twin].sum():,} of {y.sum():,} ({y[has_twin].sum() / y.sum() * 100:.1f}%)")
print(
    f"Measurements per record (median): twins {np.median(measured[has_twin]):.0f}, "
    f"other records {np.median(measured[~has_twin]):.0f}"
)
print(
    f"Twin groups with identical timestamps in all {n_date_columns:,} date columns: "
    f"{same_timestamps.mean() * 100:.1f}%"
)


# ============================================================
# 3. TWINS IN EACH TEST PERIOD
#
# A feature only helps forward in time if its relationship with
# failure holds from one period to the next.
# ============================================================

print("\n==============================")
print("TWINS IN EACH TEST PERIOD")
print("==============================")

folds = forward_folds(start, end, n_blocks=FORWARD_BLOCKS)

by_period = []

for period, (_, test) in enumerate(folds, start=1):
    twins = has_twin[test]
    y_test = y[test]

    by_period.append(
        {
            "Test Period": period,
            "Parts": len(test),
            "Twin Parts (%)": twins.mean() * 100,
            "Twin Failure Rate (%)": y_test[twins].mean() * 100,
            "Other Failure Rate (%)": y_test[~twins].mean() * 100,
            "Rate Ratio": y_test[twins].mean() / y_test[~twins].mean(),
            "Failures That Are Twins (%)": y_test[twins].sum() / y_test.sum() * 100,
        }
    )

by_period = pd.DataFrame(by_period)

print(by_period.round(2).to_string(index=False))


# ============================================================
# 4. DO TWIN FEATURES IMPROVE THE MODEL?
#
# Every feature set is trained and tested on the same 4 forward
# folds with each seed. Seed 42 without twin features is exactly
# train_xgboost.py's setup.
# ============================================================

print("\n==============================")
print("DO TWIN FEATURES IMPROVE THE MODEL?")
print("==============================")

runs = []
test_scores = {}

for seed in SEEDS:
    for set_name, n_columns in FEATURE_SETS.items():
        for period, (train, test) in enumerate(folds, start=1):
            model = create_model(y[train], seed)
            model.fit(X[train, :n_columns], y[train])
            scores = model.predict_proba(X[test, :n_columns])[:, 1]

            test_scores[seed, set_name, period] = scores
            runs.append(
                {
                    "Seed": seed,
                    "Feature Set": set_name,
                    "Test Period": period,
                    **evaluate(y[test], scores),
                }
            )

        latest = pd.DataFrame(runs[-len(folds):])
        print(
            f"  seed {seed:2}  {set_name:24} "
            f"lift {latest['Lift'].mean():5.2f}x "
            f"[{latest['Lift'].min():4.1f}-{latest['Lift'].max():4.1f}] | "
            f"top 1% recall {latest['Top 1% Recall (%)'].mean():5.1f}% "
            f"[{latest['Top 1% Recall (%)'].min():4.1f}-{latest['Top 1% Recall (%)'].max():4.1f}]"
        )

runs = pd.DataFrame(runs)
runs.to_csv(RESULTS_DIR / "twin_feature_test.csv", index=False)

reference = pd.read_csv(RESULTS_DIR / "forward_test_periods.csv")
same_setup = runs[(runs["Seed"] == 42) & (runs["Feature Set"] == "Measurements")]
print(
    "\nLargest lift difference from train_xgboost.py's results "
    f"(same setup): {np.abs(same_setup['Lift'].to_numpy() - reference['Lift'].to_numpy()).max():.4f}"
)

mean_over_seeds = runs.groupby(["Feature Set", "Test Period"], sort=False)[METRICS].mean().reset_index()

for metric in METRICS:
    table = mean_over_seeds.pivot(index="Feature Set", columns="Test Period", values=metric)
    table = table.loc[list(FEATURE_SETS)]
    table["Mean"] = table.mean(axis=1)

    print(f"\n{metric}, mean of {len(SEEDS)} seeds, by test period:")
    print(table.round(2).to_string())

# Paired changes: same seed, same test period, only the features differ
baseline = runs[runs["Feature Set"] == "Measurements"].set_index(["Seed", "Test Period"])[METRICS]
seed_spread = baseline.groupby("Test Period").agg(lambda values: values.max() - values.min())

print("\nChange vs. measurements alone (same seed and test period):")

for set_name in list(FEATURE_SETS)[1:]:
    change = runs[runs["Feature Set"] == set_name].set_index(["Seed", "Test Period"])[METRICS] - baseline

    print(f"  {set_name}")
    for metric in METRICS:
        print(
            f"    {metric:18} mean {change[metric].mean():+6.2f}, "
            f"range {change[metric].min():+6.2f} to {change[metric].max():+6.2f}, "
            f"better in {(change[metric] > 0).sum()} of {len(change)} runs"
        )

print("\nRun-to-run noise: spread across seeds, measurements alone:")
print(seed_spread.round(2).to_string())


# ============================================================
# 5. DOES THE MODEL ALREADY RANK TWINS HIGH?
#
# Within a risk band of the measurements-only model (seed 42),
# do twins still fail more often than other parts? If so, twin
# status carries information the measurements don't.
#
# Also: the simplest use of twins -- inspect every twin -- against
# the models at the same inspection budget.
# ============================================================

print("\n==============================")
print("DOES THE MODEL ALREADY RANK TWINS HIGH?")
print("==============================")

pooled = []

for period, (_, test) in enumerate(folds, start=1):
    scores = test_scores[SEEDS[0], "Measurements", period]

    # 0 = riskiest part of the period, 1 = safest
    rank = np.empty(len(test))
    rank[np.argsort(-scores, kind="stable")] = np.arange(len(test)) / len(test)

    pooled.append(
        pd.DataFrame(
            {
                "Risk Band": np.select(
                    [rank < upper for _, upper in RISK_BANDS],
                    [band for band, _ in RISK_BANDS],
                    default=RISK_BANDS[-1][0],
                ),
                "Twin": has_twin[test],
                "Failed": y[test],
            }
        )
    )

pooled = pd.concat(pooled, ignore_index=True)

cells = pooled.groupby(["Risk Band", "Twin"])["Failed"].agg(["size", "sum"]).unstack("Twin")
cells = cells.loc[[band for band, _ in RISK_BANDS]]

risk_bands = pd.DataFrame(
    {
        "Parts": cells["size"].sum(axis=1),
        "Twin Parts (%)": cells["size"][True] / cells["size"].sum(axis=1) * 100,
        "Twin Failures": cells["sum"][True],
        "Twin Failure Rate (%)": cells["sum"][True] / cells["size"][True] * 100,
        "Other Failures": cells["sum"][False],
        "Other Failure Rate (%)": cells["sum"][False] / cells["size"][False] * 100,
    }
)
risk_bands["Rate Ratio"] = risk_bands["Twin Failure Rate (%)"] / risk_bands["Other Failure Rate (%)"]
risk_bands = risk_bands.reset_index()

print("Measurements-only model, all 4 test periods pooled:")
print(risk_bands.round(2).to_string(index=False))

risk_bands.to_csv(RESULTS_DIR / "twin_risk_bands.csv", index=False)

# Inspect every twin vs. each model's riskiest parts at the same
# budget (mean over seeds)
for set_name, column in [
    ("Measurements", "Model Recall At Same Budget (%)"),
    ("+ has twin", "Model + Twin Recall At Same Budget (%)"),
]:
    by_period[column] = [
        np.mean(
            [
                recall_at(y[test], test_scores[seed, set_name, period], has_twin[test].mean())
                for seed in SEEDS
            ]
        )
        for period, (_, test) in enumerate(folds, start=1)
    ]

print("\nFailures caught when inspecting as many parts as there are twins:")
print(
    by_period[
        [
            "Test Period",
            "Twin Parts (%)",
            "Failures That Are Twins (%)",
            "Model Recall At Same Budget (%)",
            "Model + Twin Recall At Same Budget (%)",
        ]
    ].round(1).to_string(index=False)
)

by_period.to_csv(RESULTS_DIR / "twin_by_period.csv", index=False)


# ============================================================
# 6. IS TWIN STATUS KNOWN AT FINAL QC?
#
# If twins were separate parts made together, every record in a
# twin group would fail equally often, whatever its Id. They
# don't: the failures sit on the first record (lowest Id), and in
# pairs with one failure the failing record comes first.
#
# With identical measurements AND timestamps, twins are most likely
# repeat records of ONE part: tested again, with its production
# record copied, so the shared timestamps don't show when a repeat
# was made. A failed test makes a repeat far more likely, so "has a
# twin" isn't known when a part is first tested -- it leaks the QC
# result.
# ============================================================

print("\n==============================")
print("IS TWIN STATUS KNOWN AT FINAL QC?")
print("==============================")

twin_rows = pd.DataFrame(
    {
        "Group": twin_group[has_twin],
        "Size": group_size[has_twin],
        "Failed": y[has_twin],
    }
)
# 1 = the group's lowest Id
order = pd.Series(ids[has_twin]).groupby(twin_group[has_twin]).rank(method="first").to_numpy()
twin_rows["Order"] = order

record = np.full(len(y), "No twin", dtype=object)
record[has_twin] = np.select(
    [order == 1, order == 2],
    ["1st record", "2nd record"],
    default="3rd+ record",
)

by_position = pd.DataFrame({"Record": record, "Failed": y}).groupby("Record")["Failed"].agg(["size", "sum"])
by_position = by_position.loc[["No twin", "1st record", "2nd record", "3rd+ record"]]
by_position.columns = ["Records", "Failures"]
by_position["Failure Rate (%)"] = by_position["Failures"] / by_position["Records"] * 100

print("Failure rate by record, in Id order within each twin group:")
print(by_position.round(2).to_string())

by_position.reset_index().to_csv(RESULTS_DIR / "twin_record_order.csv", index=False)

pairs = twin_rows[twin_rows["Size"] == 2]
one_failure = pairs.groupby("Group")["Failed"].transform("sum") == 1
first_fails = pairs[one_failure & (pairs["Order"] == 1)]["Failed"].mean()

print(
    f"\nPairs with exactly one failure: {one_failure.sum() // 2:,}. "
    f"The failing record comes first in {first_fails * 100:.1f}% of them "
    f"(50% if Id order were unrelated to failure)."
)


# ============================================================
# 7. PLOT: THE APPARENT GAIN AND WHY IT'S A LEAK
# ============================================================

fig, (gain_ax, order_ax) = plt.subplots(1, 2, figsize=(12.5, 4.8), gridspec_kw={"width_ratios": [1.2, 1]})

# Left: the apparent gain in each test period (mean of the seeds,
# lines show their range)
periods = np.arange(1, len(folds) + 1)
lift = runs.groupby(["Feature Set", "Test Period"])["Lift"].agg(["mean", "min", "max"])

for set_name, color, label, offset in [
    ("Measurements", MUTED, "Measurements only", -0.1),
    ("+ has twin", ORANGE, "Measurements + has a twin", 0.1),
]:
    values = lift.loc[set_name]
    gain_ax.vlines(periods + offset, values["min"], values["max"], color=color, linewidth=1.5, zorder=1)
    gain_ax.scatter(periods + offset, values["mean"], s=64, color=color, edgecolor=SURFACE, linewidth=1.5,
                    zorder=2, label=label)

change = (lift.loc["+ has twin"]["mean"] - lift.loc["Measurements"]["mean"]).to_numpy()
top = np.maximum(lift.loc["Measurements"]["max"], lift.loc["+ has twin"]["max"]).to_numpy()

for period, value, y_top in zip(periods, change, top):
    gain_ax.annotate(f"{value:+.1f}x", (period, y_top), xytext=(0, 6), textcoords="offset points",
                     ha="center", va="bottom", fontsize=9, color=INK)

gain_ax.set_xticks(periods)
gain_ax.set_xticklabels([f"Period {p}" for p in periods], fontsize=9)
gain_ax.set_xlim(0.5, len(periods) + 0.5)
gain_ax.set_ylim(0, top.max() * 1.2)
gain_ax.set_ylabel("Lift over random ranking (x)")
gain_ax.legend(frameon=False, fontsize=9, loc="upper left")
gain_ax.set_title("Adding “has a twin” raises lift in every test period",
                  loc="left", fontsize=11, fontweight="bold")

# Right: failure rate by record within twin groups
x = np.arange(len(by_position))
rate = by_position["Failure Rate (%)"].to_numpy()
low, high = wilson_interval(by_position["Failures"].to_numpy(), by_position["Records"].to_numpy())
low, high = low * 100, high * 100

order_ax.bar(x, rate, width=0.55, color=[MUTED] + [ORANGE] * (len(x) - 1))
order_ax.errorbar(x, rate, yerr=[rate - low, high - rate], fmt="none", ecolor=INK_SECONDARY, elinewidth=1.2, capsize=4)

for xi, value, y_top in zip(x, rate, high):
    order_ax.annotate(f"{value:.2g}%", (xi, y_top), xytext=(0, 4), textcoords="offset points",
                      ha="center", va="bottom", fontsize=9, color=INK)

order_ax.set_xticks(x)
order_ax.set_xticklabels(
    [f"{name}\n{records:,} records" for name, records in zip(by_position.index, by_position["Records"])],
    fontsize=9,
)
order_ax.set_ylim(0, high.max() * 1.2)
order_ax.set_ylabel("Failure rate (%)")
order_ax.set_title("…but the failures sit on each group’s first record",
                   loc="left", fontsize=11, fontweight="bold")

for ax in (gain_ax, order_ax):
    ax.grid(axis="y")
    ax.tick_params(axis="x", length=0)

fig.suptitle(
    "“Has a twin” looks like a strong feature, but it leaks the QC result",
    x=0.01, y=0.99, ha="left", fontsize=13, fontweight="bold",
)
fig.text(
    0.01, 0.925,
    "Twin records share every measurement and timestamp. If they were separate parts made together, each record "
    "in a group would fail equally often, whatever its Id.\nMost likely they are repeat tests of one part, and a "
    "failed test makes a repeat far more likely. Lift: mean of 3 seeds (lines: range). Failure rates: 95% CI.",
    fontsize=9, color=INK_SECONDARY, va="top",
)

plt.tight_layout(rect=(0, 0, 1, 0.87))
plt.savefig(PLOTS_DIR / "twin_feature.png", dpi=200, bbox_inches="tight")
plt.close()

print("\nSaved:")
print("  results/twin_feature_test.csv")
print("  results/twin_by_period.csv")
print("  results/twin_risk_bands.csv")
print("  results/twin_record_order.csv")
print("  results/plots/twin_feature.png")
