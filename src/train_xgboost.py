from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import shap

from sklearn.model_selection import train_test_split
from sklearn.metrics import (
    average_precision_score,
    roc_auc_score,
    matthews_corrcoef,
)
from xgboost import XGBClassifier

from production_data import (
    forward_folds,
    load_part_times,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

DATA_PATH = PROJECT_ROOT / "data" / "train_numeric.csv"
PLOTS_DIR = PROJECT_ROOT / "results" / "plots"
PLOTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CONFIGURATION
# ============================================================

NUM_ROWS = 500_000
RANDOM_STATE = 42

# Forward-in-time split: cut the production timeline into 5 equal
# blocks and test on the most recent one (~20% of parts).
FORWARD_BLOCKS = 5

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = PROJECT_ROOT / "data" / "train_numeric.csv"


# ============================================================
# HELPER FUNCTION
# ============================================================

def create_model(y):
    """
    Create an XGBoost model with class weighting based on
    the number of passing and failing parts in the training data.
    """

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


# ============================================================
# 1. LOAD DATA
# ============================================================

print("Loading data...")

df = pd.read_csv(
    DATA_PATH,
    nrows=NUM_ROWS,
)

print("Rows loaded:", len(df))
print("Failure rate:", df["Response"].mean())

print("Loading part timestamps...")

part_times = load_part_times(NUM_ROWS)

# Both files are sorted by Id, so row i is the same part in both.
assert (part_times["Id"].to_numpy() == df["Id"].to_numpy()).all()


# ============================================================
# 2. CREATE FEATURES AND TARGET
# ============================================================

X = df.drop(columns=["Id", "Response"])
y = df["Response"]


# ============================================================
# 3. TRAIN / TEST SPLIT (FORWARD IN TIME)
#
# Test on the most recent ~20% of parts (by when they entered
# production). Train only on parts whose last station came
# BEFORE that period began -- the parts whose QC result was
# already known. This is how the model would be used.
#
# Why not a random split: failures come in bursts, and the
# measurements carry a fingerprint of when a part was made.
# A random split puts parts from the same bad week in both
# train and test, so the model can score well by recognising
# the week instead of the defect (see early_warning.py).
#
# IMPORTANT:
# We still split ONCE.
#
# Every model in the learning-curve experiment will use
# this exact same X_test and y_test.
# ============================================================

folds = forward_folds(
    part_times["start"].to_numpy(),
    part_times["end"].to_numpy(),
    n_blocks=FORWARD_BLOCKS,
)

# The most recent period is the main test set.
train_rows, test_rows = folds[-1]

X_train, X_test = X.iloc[train_rows], X.iloc[test_rows]
y_train, y_test = y.iloc[train_rows], y.iloc[test_rows]

print("\nTraining rows:", len(X_train))
print("Test rows:", len(X_test))
print("Test failures:", y_test.sum())
print(f"Training failure rate: {y_train.mean() * 100:.2f}%")
print(f"Test failure rate: {y_test.mean() * 100:.2f}%")
print(
    "Parts left out (no timestamps, or still in production "
    "when the test period began):",
    len(X) - len(X_train) - len(X_test),
)


# ============================================================
# 4. LEARNING CURVE
#
# Train models with increasing amounts of training data
# while keeping the test set fixed.
#
# Subsets are random samples of the training period, so this
# measures the effect of sample size alone.
# ============================================================

requested_sizes = [
    50_000,
    100_000,
    200_000,
]

# Only keep sizes smaller than the entire training set,
# then add the complete training set as the final experiment.
training_sizes = [
    size
    for size in requested_sizes
    if size < len(X_train)
]

training_sizes.append(len(X_train))

experiment_results = []

print("\n==============================")
print("LEARNING CURVE")
print("==============================")

for size in training_sizes:

    # If using the entire training set, no need to split again.
    if size == len(X_train):
        X_subset = X_train
        y_subset = y_train

    else:
        X_subset, _, y_subset, _ = train_test_split(
            X_train,
            y_train,
            train_size=size,
            random_state=RANDOM_STATE,
            stratify=y_train,
        )

    model = create_model(y_subset)

    model.fit(
        X_subset,
        y_subset,
    )

    probabilities = model.predict_proba(X_test)[:, 1]

    pr_auc = average_precision_score(
        y_test,
        probabilities,
    )

    baseline = y_test.mean()
    lift = pr_auc / baseline

    experiment_results.append(
        {
            "Training Size": size,
            "PR-AUC": pr_auc,
            "Lift": lift,
        }
    )

    print(f"\nTraining size: {size:,}")
    print("Baseline PR-AUC:", baseline)
    print("XGBoost PR-AUC:", pr_auc)
    print("Lift:", lift)


learning_curve_df = pd.DataFrame(
    experiment_results
)

print("\nLearning curve results:")
print(learning_curve_df.to_string(index=False))


# ============================================================
# 5. TRAIN FINAL MODEL
#
# Use ALL available training data.
# ============================================================

print("\n==============================")
print("TRAINING FINAL MODEL")
print("==============================")

final_model = create_model(y_train)

final_model.fit(
    X_train,
    y_train,
)

probabilities = final_model.predict_proba(
    X_test
)[:, 1]


# ============================================================
# 6. BASIC MODEL METRICS
# ============================================================

baseline_pr_auc = y_test.mean()

pr_auc = average_precision_score(
    y_test,
    probabilities,
)

roc_auc = roc_auc_score(
    y_test,
    probabilities,
)

print("\n==============================")
print("FINAL MODEL PERFORMANCE")
print("==============================")

print("Baseline PR-AUC:", baseline_pr_auc)
print("XGBoost PR-AUC:", pr_auc)
print("Lift:", pr_auc / baseline_pr_auc)
print("ROC-AUC:", roc_auc)


# ============================================================
# 7. MCC
#
# Note:
# 0.5 is just a default threshold.
# We can optimize this threshold later.
# ============================================================

predictions = (
    probabilities >= 0.5
).astype(int)

mcc = matthews_corrcoef(
    y_test,
    predictions,
)

print("MCC @ 0.5 threshold:", mcc)


# ============================================================
# 8. INSPECTION CAPACITY ANALYSIS
#
# Sort parts from highest predicted risk to lowest.
# Then ask:
#
# If quality engineers inspect the top X% of parts,
# how many failures do we catch?
# ============================================================

results = pd.DataFrame(
    {
        "actual": y_test.to_numpy(),
        "probability": probabilities,
    }
)

results = results.sort_values(
    "probability",
    ascending=False,
).reset_index(drop=True)

total_failures = results["actual"].sum()

print("\n==============================")
print("INSPECTION CAPACITY ANALYSIS")
print("==============================")

print("Total test failures:", total_failures)

for pct in [0.01, 0.02, 0.05, 0.10]:

    number_to_inspect = int(
        len(results) * pct
    )

    inspected_parts = results.head(
        number_to_inspect
    )

    failures_caught = (
        inspected_parts["actual"].sum()
    )

    recall = (
        failures_caught
        / total_failures
    )

    precision = (
        failures_caught
        / number_to_inspect
    )

    print(
        f"Top {pct * 100:.0f}% inspected | "
        f"Parts inspected: {number_to_inspect:,} | "
        f"Failures caught: {failures_caught} | "
        f"Precision: {precision * 100:.2f}% | "
        f"Recall: {recall * 100:.2f}%"
    )


# ============================================================
# 9. PERFORMANCE ACROSS TIME PERIODS
#
# One test period can be unusually easy or hard: the failure
# rate swings a lot from week to week. Repeat the forward
# split with each earlier period as the test set too, and
# report the range. These are the numbers to quote.
#
# (The last period is the main test set above, so its row
# repeats the final model's numbers.)
# ============================================================

print("\n==============================")
print("PERFORMANCE ACROSS TIME PERIODS")
print("==============================")

period_results = []

for period, (period_train, period_test) in enumerate(folds, start=1):

    period_model = create_model(y.iloc[period_train])

    period_model.fit(
        X.iloc[period_train],
        y.iloc[period_train],
    )

    period_probabilities = period_model.predict_proba(
        X.iloc[period_test]
    )[:, 1]

    period_y = y.iloc[period_test]

    period_pr_auc = average_precision_score(
        period_y,
        period_probabilities,
    )

    # Failures caught by inspecting the top 1%
    ranked = pd.DataFrame(
        {
            "actual": period_y.to_numpy(),
            "probability": period_probabilities,
        }
    ).sort_values(
        "probability",
        ascending=False,
    )

    caught = ranked.head(int(len(ranked) * 0.01))["actual"].sum()

    period_results.append(
        {
            "Test Period": period,
            "Training Rows": len(period_train),
            "Test Rows": len(period_test),
            "Test Failure Rate (%)": period_y.mean() * 100,
            "PR-AUC": period_pr_auc,
            "Lift": period_pr_auc / period_y.mean(),
            "Top 1% Recall (%)": caught / period_y.sum() * 100,
        }
    )

period_df = pd.DataFrame(period_results)

print(period_df.to_string(index=False))

print(
    f"\nLift across test periods: "
    f"mean {period_df['Lift'].mean():.1f}x, "
    f"range {period_df['Lift'].min():.1f}x - {period_df['Lift'].max():.1f}x"
)
print(
    f"Top 1% recall across test periods: "
    f"mean {period_df['Top 1% Recall (%)'].mean():.1f}%, "
    f"range {period_df['Top 1% Recall (%)'].min():.1f}% - "
    f"{period_df['Top 1% Recall (%)'].max():.1f}%"
)

period_df.to_csv(
    PROJECT_ROOT / "results" / "forward_test_periods.csv",
    index=False,
)


# ============================================================
# 10. XGBOOST FEATURE IMPORTANCE
# ============================================================

importance = pd.DataFrame(
    {
        "Feature": X.columns,
        "Importance": final_model.feature_importances_,
    }
)

importance = importance.sort_values(
    "Importance",
    ascending=False,
).reset_index(drop=True)

print("\n==============================")
print("TOP 20 XGBOOST FEATURES")
print("==============================")

print(
    importance.head(20).to_string(
        index=False
    )
)


# ============================================================
# 11. SHAP EXPLANATIONS
#
# SHAP tells us how features contribute to predictions.
# ============================================================

print("\nCalculating SHAP values...")

explainer = shap.TreeExplainer(
    final_model
)

X_sample = X_test.sample(
    min(2000, len(X_test)),
    random_state=RANDOM_STATE,
)

shap_values = explainer(
    X_sample
)


# ============================================================
# 12. GLOBAL SHAP IMPORTANCE
# ============================================================

shap.plots.bar(
    shap_values,
    max_display=20,
    show=False,
)

plt.title("Top Features Driving Failure Predictions")
plt.tight_layout()
plt.savefig(
    PLOTS_DIR / "shap_bar.png",
    dpi=200,
    bbox_inches="tight",
)
plt.close()


# ============================================================
# 13. SHAP BEESWARM
#
# Shows:
# - feature importance
# - direction of effect
# - magnitude of effect
# ============================================================

shap.plots.beeswarm(
    shap_values,
    max_display=20,
    show=False,
)

plt.tight_layout()
plt.savefig(
    PLOTS_DIR / "shap_beeswarm.png",
    dpi=200,
    bbox_inches="tight",
)
plt.close()


# ============================================================
# 14. FIND HIGH-RISK ACTUAL FAILURES
# ============================================================

results_with_index = pd.DataFrame(
    {
        "index": X_test.index,
        "actual": y_test.to_numpy(),
        "probability": probabilities,
    }
)

failed_parts = (
    results_with_index[
        results_with_index["actual"] == 1
    ]
    .sort_values(
        "probability",
        ascending=False,
    )
    .reset_index(drop=True)
)

print("\n==============================")
print("HIGHEST-RISK ACTUAL FAILURES")
print("==============================")

print(
    failed_parts.head().to_string(
        index=False
    )
)


# ============================================================
# 15. EXPLAIN ONE HIGH-RISK FAILED PART
# ============================================================

part_index = int(
    failed_parts.iloc[0]["index"]
)

part = X_test.loc[
    [part_index]
]

part_probability = final_model.predict_proba(
    part
)[0, 1]

print("\nExample failed part:")
print("Dataset index:", part_index)
print(
    "Predicted failure probability:",
    part_probability,
)

part_shap = explainer(
    part
)

shap.plots.waterfall(
    part_shap[0],
    max_display=20,
    show=False,
)

plt.tight_layout()
plt.savefig(
    PLOTS_DIR / "shap_waterfall.png",
    dpi=200,
    bbox_inches="tight",
)
plt.close()

learning_curve_df.to_csv(
    PROJECT_ROOT / "results" / "learning_curve.csv",
    index=False
)