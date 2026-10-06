"""
ONE RECORD PER PART -- re-scoring the final-QC model

About 4% of train records are repeat tests of a part already in the data:
the 2nd and later records of twin groups found across both Kaggle files
(see twin_feature.py and kaggle_split_repeats.py). train_xgboost.py counts
every record, so a retested part is ranked and counted more than once, and
a repeat's label is a retest result. The decision the model supports --
which parts to inspect at final QC -- is made at a part's first test.

This re-scores the model forward in time, on the same 4 test periods:

A. Train on all records, test on all records (train_xgboost.py)
B. Train on all records, test on first tests only (one record per part)
C. Train on first tests only, test on first tests only

each with 3 random seeds. B vs. A shows what counting repeats did to the
headline; C vs. B whether repeats help or hurt training.

Needs data/derived/twin_records.csv from kaggle_split_repeats.py.

Run from the project root:

    .venv/bin/python src/one_record_per_part.py

Takes about 5 minutes and ~25 GB of RAM.
"""

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score
from xgboost import XGBClassifier

from production_data import (
    DATA_DIR,
    RESULTS_DIR,
    forward_folds,
    load_part_times,
    load_repeat_tests,
)


# ============================================================
# CONFIGURATION
# ============================================================

FORWARD_BLOCKS = 5

# 42 is the seed train_xgboost.py uses. The model samples rows and
# columns at random, so other seeds show the run-to-run noise.
SEEDS = [42, 1, 2]

SETUPS = {
    "A": "train all records, test all records",
    "B": "train all records, test first tests",
    "C": "train first tests, test first tests",
}

METRICS = ["Lift", "Top 1% Recall (%)", "Top 5% Recall (%)"]


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
        "Test Records": len(y_true),
        "Test Failure Rate (%)": y_true.mean() * 100,
        "Lift": average_precision_score(y_true, scores) / y_true.mean(),
        "Top 1% Recall (%)": recall_at(y_true, scores, 0.01),
        "Top 5% Recall (%)": recall_at(y_true, scores, 0.05),
    }


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

print("Loading part timestamps...")

part_times = load_part_times(None)

# Both files are sorted by Id, so row i is the same part in both
assert (part_times["Id"].to_numpy() == numeric["Id"].to_numpy()).all()

ids = numeric["Id"].to_numpy()
X = numeric[feature_names].to_numpy()
y = numeric["Response"].to_numpy()
del numeric

start = part_times["start"].to_numpy()
end = part_times["end"].to_numpy()

# Every record except the 2nd and later records of twin groups found
# across both Kaggle files
first_test = ~np.isin(ids, load_repeat_tests())

print(
    f"Records: {len(y):,} ({y.mean() * 100:.2f}% fail) | first tests: {first_test.sum():,} "
    f"({y[first_test].mean() * 100:.2f}% fail) | repeats: {(~first_test).sum():,} "
    f"({y[~first_test].mean() * 100:.2f}% fail)"
)


# ============================================================
# 2. REPEATS IN EACH TEST PERIOD
# ============================================================

print("\n==============================")
print("REPEATS IN EACH TEST PERIOD")
print("==============================")

# The same folds as train_xgboost.py; setups B and C only drop
# repeats from them
folds = forward_folds(start, end, n_blocks=FORWARD_BLOCKS)

for period, (train, test) in enumerate(folds, start=1):
    repeats = ~first_test[test]

    print(
        f"  period {period}: {len(test):,} test records, {repeats.mean() * 100:.1f}% repeats "
        f"({y[test][repeats].mean() * 100:.2f}% fail) | failure rate {y[test].mean() * 100:.3f}% -> "
        f"{y[test][~repeats].mean() * 100:.3f}% on first tests | "
        f"{(~first_test[train]).sum():,} repeats among {len(train):,} training records"
    )


# ============================================================
# 3. RE-SCORE THE MODEL
#
# A and B use the same model (trained on all records); B only
# scores it on first tests. C trains without the repeats.
# ============================================================

print("\n==============================")
print("RE-SCORE THE MODEL")
print("==============================")

runs = []

for seed in SEEDS:
    for period, (train, test) in enumerate(folds, start=1):
        firsts = first_test[test]

        model = create_model(y[train], seed)
        model.fit(X[train], y[train])
        scores = model.predict_proba(X[test])[:, 1]

        runs.append({"Seed": seed, "Setup": "A", "Test Period": period, **evaluate(y[test], scores)})
        runs.append({"Seed": seed, "Setup": "B", "Test Period": period, **evaluate(y[test][firsts], scores[firsts])})

        train_firsts = train[first_test[train]]
        test_firsts = test[firsts]

        model = create_model(y[train_firsts], seed)
        model.fit(X[train_firsts], y[train_firsts])
        scores = model.predict_proba(X[test_firsts])[:, 1]

        runs.append({"Seed": seed, "Setup": "C", "Test Period": period, **evaluate(y[test_firsts], scores)})

    latest = pd.DataFrame(runs[-3 * len(folds):])

    for setup, rows in latest.groupby("Setup"):
        print(
            f"  seed {seed:2}  {setup}: {SETUPS[setup]:38} "
            f"lift {rows['Lift'].mean():5.2f}x [{rows['Lift'].min():4.1f}-{rows['Lift'].max():4.1f}] | "
            f"top 1% recall {rows['Top 1% Recall (%)'].mean():5.1f}% "
            f"[{rows['Top 1% Recall (%)'].min():4.1f}-{rows['Top 1% Recall (%)'].max():4.1f}]"
        )

runs = pd.DataFrame(runs)
runs.to_csv(RESULTS_DIR / "one_record_per_part.csv", index=False)

# Seed 42 is train_xgboost.py: setup B is its headline, setup A its
# "All Records" columns
reference = pd.read_csv(RESULTS_DIR / "forward_test_periods.csv")

for setup, column in [("A", "Lift (All Records)"), ("B", "Lift")]:
    same_setup = runs[(runs["Seed"] == 42) & (runs["Setup"] == setup)]
    print(
        f"Largest lift difference from train_xgboost.py's {column!r} (setup {setup}, seed 42): "
        f"{np.abs(same_setup['Lift'].to_numpy() - reference[column].to_numpy()).max():.4f}"
    )

mean_over_seeds = runs.groupby(["Setup", "Test Period"])[METRICS].mean().reset_index()

for metric in METRICS:
    table = mean_over_seeds.pivot(index="Setup", columns="Test Period", values=metric)
    table["Mean"] = table.mean(axis=1)
    table.index = [f"{setup}: {SETUPS[setup]}" for setup in table.index]

    print(f"\n{metric}, mean of {len(SEEDS)} seeds, by test period:")
    print(table.round(2).to_string())

# Paired changes: same seed and test period
by_run = runs.set_index(["Setup", "Seed", "Test Period"])[METRICS]

print("\nPaired changes (same seed and test period):")

for later, earlier, meaning in [
    ("B", "A", "dropping repeats from the test sets"),
    ("C", "B", "also dropping them from training"),
]:
    change = by_run.loc[later] - by_run.loc[earlier]

    print(f"  {later} - {earlier} ({meaning})")
    for metric in METRICS:
        print(
            f"    {metric:18} mean {change[metric].mean():+6.2f}, "
            f"range {change[metric].min():+6.2f} to {change[metric].max():+6.2f}, "
            f"higher in {(change[metric] > 0).sum()} of {len(change)} runs"
        )

seed_spread = by_run.loc["A"].groupby("Test Period").agg(lambda values: values.max() - values.min())

print("\nRun-to-run noise: spread across seeds, setup A:")
print(seed_spread.round(2).to_string())

print("\nSaved:")
print("  results/one_record_per_part.csv")
