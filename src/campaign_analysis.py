"""
PHASE 3 / STEP 3 -- production campaigns

Parts enter production at one of two lines: L0 (S0-S23) or L1 (S24-S25).
The factory alternates between them in campaigns, and several earlier
results swing with that mix. This script:

1. Profiles L0-entry vs L1-entry parts
2. Maps the campaigns week by week
3. Checks whether failure regimes are line-wide or belong to one entry line
4. Re-runs the model by entry line, forward in time:
     - does the current (pooled) model work equally well for both?
     - do separate models per entry line do better?
     - do pre-L3 measurements give early warning for either line?

Campaigns use all 1.18M labelled parts (timestamps only). The model
section uses the usual first 500k rows and the same 4 forward test
periods as train_xgboost.py.

Run from the project root:

    .venv/bin/python src/campaign_analysis.py

Takes about 2 minutes.
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
RANDOM_STATE = 42
FORWARD_BLOCKS = 5

ENTRY_LINES = ["L0", "L1"]

# A week belongs to an L1 campaign when >= 10% of parts entered at L1.
# Weeks with fewer parts than this count as shutdowns, not campaigns.
CAMPAIGN_L1_SHARE = 0.10
MIN_PRODUCTION_PARTS = 1_000

# Only show / compare weekly failure rates with at least this many parts
MIN_WEEK_PARTS = 300

# Skip a model comparison when a fold has fewer failures than this
MIN_FOLD_FAILURES = 20

LAST_STATION_BEFORE_L3 = 28

# Chart colors (light theme)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
LINE_COLORS = {"L0": "#2a78d6", "L1": "#eb6834"}

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


def evaluate(y_true, scores):
    n_inspect = max(1, int(len(y_true) * 0.01))
    caught = y_true[np.argsort(-scores, kind="stable")[:n_inspect]].sum()

    return {
        "Lift": average_precision_score(y_true, scores) / y_true.mean(),
        "Top 1% Recall (%)": caught / y_true.sum() * 100,
    }


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

all_y = response["Response"].to_numpy()
all_start = first_seen.min(axis=1).to_numpy(np.float64)
all_end = last_seen.max(axis=1).to_numpy(np.float64)

# Entry line = line of the first station a part visited
visited = first_seen.notna().to_numpy()
station_lines = np.array([line_of(s) for s in first_seen.columns])
station_nums = np.array([station_number(s) for s in first_seen.columns])

all_entry = np.where(
    visited.any(axis=1),
    station_lines[np.argmax(visited, axis=1)],
    "none",
)

through_l2 = visited[:, station_lines == "L2"].any(axis=1)
late_l3_path = visited[:, station_nums >= 39].any(axis=1)

hours_on_line = (all_end - all_start) * HOURS_PER_UNIT
week = np.floor(all_start / UNITS_PER_WEEK)

print("Labelled parts:", len(all_y))


# ============================================================
# 2. L0-ENTRY vs L1-ENTRY PARTS
# ============================================================

profiles = pd.DataFrame(
    [
        {
            "Entry Line": line,
            "Parts": int((all_entry == line).sum()),
            "Failure Rate (%)": all_y[all_entry == line].mean() * 100,
            "Median Hours On Line": np.median(hours_on_line[all_entry == line]),
            "Through L2 (%)": through_l2[all_entry == line].mean() * 100,
            "L3 Path S39-S51 (%)": late_l3_path[all_entry == line].mean() * 100,
        }
        for line in ENTRY_LINES
    ]
)

print("\n==============================")
print("L0-ENTRY vs L1-ENTRY PARTS")
print("==============================")

print(profiles.to_string(index=False, float_format=lambda v: f"{v:.2f}"))
print(
    f"Other parts: {(~np.isin(all_entry, ENTRY_LINES)).sum():,} "
    f"(entered at L2/L3 or have no timestamps)"
)


# ============================================================
# 3. CAMPAIGN MAP
#
# Weekly parts and failure rate by entry line. A week is part of
# an L1 campaign when >= CAMPAIGN_L1_SHARE of its parts entered at
# L1. Consecutive campaign weeks form one campaign.
# ============================================================

main = np.isin(all_entry, ENTRY_LINES) & ~np.isnan(week)

weekly = (
    pd.DataFrame({"Week": week[main].astype(int), "Entry": all_entry[main], "failed": all_y[main]})
    .pivot_table(index="Week", columns="Entry", values="failed", aggfunc=["size", "mean"])
)
weekly.columns = [f"{'Parts' if stat == 'size' else 'Failure Rate'} {line}" for stat, line in weekly.columns]
weekly = weekly.reindex(np.arange(weekly.index.min(), weekly.index.max() + 1))
weekly[["Parts L0", "Parts L1"]] = weekly[["Parts L0", "Parts L1"]].fillna(0).astype(int)
weekly["L1 Share"] = weekly["Parts L1"] / (weekly["Parts L0"] + weekly["Parts L1"]).replace(0, np.nan)
weekly["Production Week"] = (weekly["Parts L0"] + weekly["Parts L1"]) >= MIN_PRODUCTION_PARTS
weekly["L1 Campaign"] = weekly["Production Week"] & (weekly["L1 Share"] >= CAMPAIGN_L1_SHARE)

for line in ENTRY_LINES:
    too_few = weekly[f"Parts {line}"] < MIN_WEEK_PARTS
    weekly.loc[too_few, f"Failure Rate {line}"] = np.nan
    weekly[f"Failure Rate {line}"] *= 100

weekly.to_csv(RESULTS_DIR / "campaign_weeks.csv")

# Group consecutive campaign weeks
campaign_id = (weekly["L1 Campaign"] != weekly["L1 Campaign"].shift()).cumsum()
campaigns = []

for _, block in weekly[weekly["L1 Campaign"]].groupby(campaign_id[weekly["L1 Campaign"]]):
    campaigns.append(
        {
            "First Week": block.index.min(),
            "Last Week": block.index.max(),
            "Weeks": len(block),
            "L1-Only Weeks": int((block["L1 Share"] >= 0.95).sum()),
            "L1 Parts": int(block["Parts L1"].sum()),
            "L0 Parts": int(block["Parts L0"].sum()),
        }
    )

campaigns = pd.DataFrame(campaigns)

print("\n==============================")
print("L1 CAMPAIGNS")
print("==============================")

print(campaigns.to_string(index=False))
print(
    f"\n{weekly['L1 Campaign'].sum()} of {weekly['Production Week'].sum()} production weeks "
    f"(>= {MIN_PRODUCTION_PARTS:,} parts) were L1-campaign weeks."
)

campaigns.to_csv(RESULTS_DIR / "campaigns.csv", index=False)


# ============================================================
# 4. ARE FAILURE REGIMES LINE-WIDE?
#
# In weeks when both lines ran, do L0-entry and L1-entry failure
# rates move together? If yes, the bursts come from something
# both share (downstream lines, common supply), not the mix.
# ============================================================

both_ran = weekly[(weekly["Parts L0"] >= 1000) & (weekly["Parts L1"] >= 1000)]
regime_correlation = both_ran["Failure Rate L0"].corr(both_ran["Failure Rate L1"])

print("\n==============================")
print("ARE FAILURE REGIMES LINE-WIDE?")
print("==============================")

print(
    f"Weeks with >= 1,000 parts from both lines: {len(both_ran)} | "
    f"correlation of their weekly failure rates: {regime_correlation:.2f}"
)


# ============================================================
# 5. THE MODEL BY ENTRY LINE (FORWARD IN TIME)
# ============================================================

print("\nLoading measurements...")

numeric = load_numeric(NUM_ROWS)

# The first NUM_ROWS rows of every file are the same parts
assert (numeric["Id"].to_numpy() == all_ids[:NUM_ROWS]).all()

feature_names = [c for c in numeric.columns if c.startswith("L")]
X = numeric[feature_names].to_numpy(dtype=np.float32)
y = numeric["Response"].to_numpy()
del numeric

entry = all_entry[:NUM_ROWS]
folds = forward_folds(all_start[:NUM_ROWS], all_end[:NUM_ROWS], n_blocks=FORWARD_BLOCKS)

all_features = np.arange(X.shape[1])
pre_l3_features = np.flatnonzero(
    np.array([station_number(station_of(c)) for c in feature_names]) <= LAST_STATION_BEFORE_L3
)

print("\n==============================")
print("THE MODEL BY ENTRY LINE (FORWARD IN TIME)")
print("==============================")

for i, (train, test) in enumerate(folds, start=1):
    print(
        f"Test period {i}: "
        + " | ".join(
            f"{line} {(entry[test] == line).sum():6,} parts, {y[test][entry[test] == line].sum():3} failures"
            for line in ENTRY_LINES
        )
        + f" | L1 parts in training: {(entry[train] == 'L1').sum():,}"
    )

model_rows = []


def record(model_name, line, period, metrics):
    model_rows.append({"Model": model_name, "Evaluated On": f"{line}-entry parts", "Test Period": period, **metrics})


for period, (train, test) in enumerate(folds, start=1):

    # The current model: one model for all parts, scored per entry line
    pooled = create_model(y[train])
    pooled.fit(X[train], y[train])
    scores = pooled.predict_proba(X[test])[:, 1]

    for line in ENTRY_LINES:
        in_line = entry[test] == line

        if y[test][in_line].sum() >= MIN_FOLD_FAILURES:
            record("Pooled model, all measurements", line, period, evaluate(y[test][in_line], scores[in_line]))

    # Separate models per entry line
    for line in ENTRY_LINES:
        line_train = train[entry[train] == line]
        line_test = test[entry[test] == line]

        if y[line_train].sum() < MIN_FOLD_FAILURES or y[line_test].sum() < MIN_FOLD_FAILURES:
            continue

        for model_name, feature_cols in [
            ("Own-line model, all measurements", all_features),
            ("Own-line model, pre-L3 measurements", pre_l3_features),
        ]:
            model = create_model(y[line_train])
            model.fit(X[np.ix_(line_train, feature_cols)], y[line_train])
            line_scores = model.predict_proba(X[np.ix_(line_test, feature_cols)])[:, 1]

            record(model_name, line, period, evaluate(y[line_test], line_scores))

    print(f"Test period {period} done")

by_period = pd.DataFrame(model_rows)

model_summary = (
    by_period
    .groupby(["Model", "Evaluated On"], sort=False)
    .agg(
        Periods=("Test Period", lambda p: ",".join(map(str, p))),
        Lift=("Lift", "mean"),
        Lift_Min=("Lift", "min"),
        Lift_Max=("Lift", "max"),
        Top_1_Recall=("Top 1% Recall (%)", "mean"),
    )
    .reset_index()
    .rename(columns={"Lift_Min": "Lift Min", "Lift_Max": "Lift Max", "Top_1_Recall": "Top 1% Recall (%)"})
)

print()
print(model_summary.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

by_period.to_csv(RESULTS_DIR / "campaign_model_by_period.csv", index=False)
model_summary.to_csv(RESULTS_DIR / "campaign_model.csv", index=False)


# ============================================================
# 6. PLOT: CAMPAIGN TIMELINE
#
# Top: weekly parts by entry line. Bottom: weekly failure rate by
# entry line (weeks with >= MIN_WEEK_PARTS parts from that line).
# ============================================================

fig, (top, bottom) = plt.subplots(
    2, 1, figsize=(10, 6.4), sharex=True, gridspec_kw={"height_ratios": [1, 1], "hspace": 0.25}
)

weeks = weekly.index.to_numpy()

bar_style = dict(width=0.9, edgecolor=SURFACE, linewidth=0.6)

top.bar(weeks, weekly["Parts L0"] / 1000, color=LINE_COLORS["L0"], label="Entered at L0", **bar_style)
top.bar(
    weeks, weekly["Parts L1"] / 1000, bottom=weekly["Parts L0"] / 1000,
    color=LINE_COLORS["L1"], label="Entered at L1", **bar_style,
)
top.set_ylabel("Parts entering (thousands)")
top.grid(axis="y")
top.legend(loc="upper left", frameon=False, fontsize=9, ncol=2)

for line in ENTRY_LINES:
    bottom.plot(
        weeks, weekly[f"Failure Rate {line}"],
        color=LINE_COLORS[line], linewidth=2, marker="o", markersize=4,
        label=f"Parts that entered at {line}",
    )

bottom.set_ylabel("Weekly failure rate (%)")
bottom.set_xlabel("Production week")
bottom.set_ylim(0, None)
bottom.grid(axis="y")
bottom.legend(loc="upper right", frameon=False, fontsize=9)

top.set_title(
    "The factory alternates between two entry lines",
    loc="left",
    fontsize=12,
    fontweight="bold",
    pad=22,
)
top.text(
    0, 1.06,
    f"L1 campaigns ramp up alongside L0, then run alone; failure rates of both lines move together "
    f"(r = {regime_correlation:.2f})",
    transform=top.transAxes,
    fontsize=9,
    color=INK_SECONDARY,
)

plt.savefig(PLOTS_DIR / "campaign_timeline.png", dpi=200, bbox_inches="tight")
plt.close()

print("\nSaved:")
print("  results/campaign_weeks.csv")
print("  results/campaigns.csv")
print("  results/campaign_model.csv")
print("  results/campaign_model_by_period.csv")
print("  results/plots/campaign_timeline.png")
