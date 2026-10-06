"""
REPEAT RECORDS ACROSS THE KAGGLE SPLIT

Twin records (identical measurements and timestamps) are most likely repeat
tests of one part: the first record (lowest Id) is the part's first test
(see twin_feature.py). The Kaggle competition split all records between a
train file (with QC results) and a test file (without), so one part's
records can sit in different files. Until now only the train file was
searched for twins.

This script groups twins across both files and asks:

1. How did Kaggle split twin groups between the files?
2. Which train records have a twin only in the test file, and are they
   first records or repeats?
3. Do they fail at the rates the repeat-test reading predicts? Their twins
   were invisible to twin_feature.py, so this is an out-of-sample check.
4. Which train records are a part's first test (one record per part), and
   which are repeats?

Run from the project root:

    .venv/bin/python src/kaggle_split_repeats.py

Takes about 5 minutes and ~25 GB of RAM.
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from production_data import (
    DATA_DIR,
    PLOTS_DIR,
    RESULTS_DIR,
    load_dates,
    twin_groups,
)

PLOTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CONFIGURATION
# ============================================================

SPLITS = ["train", "test"]

ROLES = ["No twin", "1st record", "2nd record", "3rd+ record"]

# Chart colors (light theme)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
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

def wilson_interval(failures, records, z=1.96):
    """95% confidence interval for a failure rate."""

    rate = failures / records
    center = (rate + z ** 2 / (2 * records)) / (1 + z ** 2 / records)
    half = z * np.sqrt(rate * (1 - rate) / records + z ** 2 / (4 * records ** 2)) / (1 + z ** 2 / records)

    return center - half, center + half


# ============================================================
# 1. LOAD BOTH FILES
#
# Records keep file order: all train rows, then all test rows.
# The test file has no QC results (Failed = -1).
# ============================================================

header = pd.read_csv(DATA_DIR / "train_numeric.csv", nrows=0).columns
feature_names = [c for c in header if c.startswith("L")]

measurements, ids, failed = [], [], []

for split in SPLITS:
    print(f"Loading {split} measurements...")

    numeric = pd.read_csv(
        DATA_DIR / f"{split}_numeric.csv",
        dtype={c: np.float32 for c in feature_names},
    )

    measurements.append(numeric[feature_names])
    ids.append(numeric["Id"].to_numpy())
    failed.append(numeric["Response"].to_numpy() if split == "train" else np.full(len(numeric), -1))
    del numeric

features = pd.concat(measurements, ignore_index=True)
del measurements

start, end, stamps = [], [], []

for split, split_ids in zip(SPLITS, ids):
    print(f"Loading {split} timestamps...")

    dates = load_dates(None, split)
    assert (dates["Id"].to_numpy() == split_ids).all()

    timestamps = dates.drop(columns="Id")
    start.append(timestamps.min(axis=1).to_numpy())
    end.append(timestamps.max(axis=1).to_numpy())
    stamps.append(timestamps)
    del dates

start = np.concatenate(start)
end = np.concatenate(end)

records = pd.DataFrame(
    {
        "Id": np.concatenate(ids),
        "File": np.repeat(SPLITS, [len(i) for i in ids]),
        "Failed": np.concatenate(failed),
    }
)
offsets = np.cumsum([0] + [len(i) for i in ids])

print(", ".join(f"{split}: {(records['File'] == split).sum():,} records" for split in SPLITS))


# ============================================================
# 2. TWIN GROUPS ACROSS BOTH FILES
# ============================================================

print("\n==============================")
print("TWIN GROUPS ACROSS BOTH FILES")
print("==============================")

twin_group = twin_groups(features, start)
del features

records["Group"] = twin_group
members = np.flatnonzero(twin_group >= 0)

# Twins across files must also share every timestamp
bits = np.empty((len(members), stamps[0].shape[1]), dtype=np.uint32)

for k, timestamps in enumerate(stamps):
    in_file = (members >= offsets[k]) & (members < offsets[k + 1])
    bits[in_file] = timestamps.iloc[members[in_file] - offsets[k]].to_numpy(np.float32).view(np.uint32)

first_member = pd.Series(np.arange(len(members))).groupby(twin_group[members]).transform("first").to_numpy()
same_timestamps = pd.Series((bits == bits[first_member]).all(axis=1)).groupby(twin_group[members]).all()
n_date_columns = bits.shape[1]
del stamps, bits

twins = records.iloc[members].copy()
twins["Order"] = twins.groupby("Group")["Id"].rank(method="first").astype(int)
twins["In Train"] = twins["File"] == "train"

groups = twins.groupby("Group").agg(Size=("Id", "size"), Train=("In Train", "sum"))
groups["Test"] = groups["Size"] - groups["Train"]

first_ids = twins[twins["Order"] == 1].set_index("Group")["Id"]
second_ids = twins[twins["Order"] == 2].set_index("Group")["Id"]
id_gap = (second_ids - first_ids.loc[second_ids.index])

print(f"Twin records: {len(twins):,} in {len(groups):,} groups "
      f"({(twins['File'] == 'train').sum():,} train, {(twins['File'] == 'test').sum():,} test)")
print(f"Groups with identical timestamps in all {n_date_columns:,} date columns: "
      f"{same_timestamps.mean() * 100:.1f}%")
print(f"First two records 1 Id apart: {(id_gap == 1).mean() * 100:.1f}% of groups")

composition = pd.DataFrame(
    {
        "Groups": [
            int((groups["Test"] == 0).sum()),
            int((groups["Train"] == 0).sum()),
            int(((groups["Train"] > 0) & (groups["Test"] > 0)).sum()),
        ],
    },
    index=["Train file only", "Test file only", "Both files"],
)
composition["Share (%)"] = composition["Groups"] / len(groups) * 100

print("\nWhere each group's records are:")
print(composition.round(1).to_string())

# If Kaggle assigned records to files at random, a pair would land in
# both files with probability 2p(1 - p)
p = (records["File"] == "train").mean()
pairs = groups[groups["Size"] == 2]
pair_split = pd.DataFrame(
    {
        "Pairs": [
            int((pairs["Train"] == 2).sum()),
            int((pairs["Train"] == 1).sum()),
            int((pairs["Train"] == 0).sum()),
        ],
        "Expected If Random (%)": [p ** 2 * 100, 2 * p * (1 - p) * 100, (1 - p) ** 2 * 100],
    },
    index=["Both in train", "One in each file", "Both in test"],
)
pair_split["Observed (%)"] = pair_split["Pairs"] / len(pairs) * 100

print(f"\nPairs ({len(pairs):,}) by file, vs. a random split of records:")
print(pair_split[["Pairs", "Observed (%)", "Expected If Random (%)"]].round(1).to_string())

pd.concat(
    [composition.assign(Kind="Group"), pair_split.rename(columns={"Pairs": "Groups"}).assign(Kind="Pair")]
).rename_axis("Where").reset_index().to_csv(RESULTS_DIR / "kaggle_split_groups.csv", index=False)


# ============================================================
# 3. TRAIN RECORDS, RECLASSIFIED
#
# Each train record's role in its group across both files (Id
# order), and whether twin_feature.py could see that it was a
# twin (another record of its group in the train file).
# ============================================================

print("\n==============================")
print("TRAIN RECORDS, RECLASSIFIED")
print("==============================")

records["Order"] = 0
records.loc[twins.index, "Order"] = twins["Order"]
records["Role"] = np.select(
    [records["Order"].to_numpy() == k for k in [0, 1, 2]],
    ROLES[:3],
    default=ROLES[3],
)
records["Train Twins"] = 0
records.loc[twins.index, "Train Twins"] = twins.groupby("Group")["In Train"].transform("sum")
records["Twin Seen In Train"] = records["Train Twins"] >= 2

train = records[records["File"] == "train"]
twin_rows = train["Role"] != "No twin"

roles = (
    train.assign(Where=np.where(~twin_rows, "-", np.where(train["Twin Seen In Train"], "Train file", "Test file only")))
    .groupby(["Role", "Where"])["Failed"]
    .agg(Records="size", Failures="sum")
    .reset_index()
    .rename(columns={"Where": "Other Records In"})
)
roles["Failure Rate (%)"] = roles["Failures"] / roles["Records"] * 100
roles["Role"] = pd.Categorical(roles["Role"], ROLES, ordered=True)
roles = roles.sort_values(["Role", "Other Records In"], ascending=[True, False]).reset_index(drop=True)

print("Train records by role in their twin group (both files, Id order):")
print(roles.round(2).to_string(index=False))

roles.to_csv(RESULTS_DIR / "kaggle_split_train_roles.csv", index=False)

hidden = twin_rows & ~train["Twin Seen In Train"]
print(
    f"\nTrain records whose twins are all in the test file: {hidden.sum():,} "
    f"(first records {(hidden & (train['Role'] == '1st record')).sum():,}, "
    f"repeats {(hidden & (train['Role'] != '1st record')).sum():,})"
)


# ============================================================
# 4. ONE RECORD PER PART (TRAIN FILE)
#
# A part's first test is its first record across both files. A
# train record is a first test if it has no twin or is its
# group's 1st record; otherwise it's a repeat, possibly of a part
# whose first test is in the test file (QC result unknown).
# ============================================================

print("\n==============================")
print("ONE RECORD PER PART (TRAIN FILE)")
print("==============================")

first_test = train["Role"].isin(["No twin", "1st record"])
group_first_file = twins[twins["Order"] == 1].set_index("Group")["File"]
repeat_of_test = ~first_test & train["Group"].map(group_first_file).eq("test")

# api.py / factory_service.py treat the lowest train Id of each train-only
# group as the part; some of those are repeats of a test-file record
lowest_train = (
    train[twin_rows & train["Twin Seen In Train"]]
    .sort_values("Id")
    .drop_duplicates("Group")
)
api_part_but_repeat = (lowest_train["Role"] != "1st record").sum()

repeats = ~first_test
twin_failures = train.loc[twin_rows, "Failed"].sum()

summary = pd.DataFrame(
    {
        "Records": [int(first_test.sum()), int(repeats.sum()), int(repeat_of_test.sum())],
        "Failures": [
            int(train.loc[first_test, "Failed"].sum()),
            int(train.loc[repeats, "Failed"].sum()),
            int(train.loc[repeat_of_test, "Failed"].sum()),
        ],
    },
    index=["First tests", "Repeats", "  of which repeat a test-file record"],
)
summary["Failure Rate (%)"] = summary["Failures"] / summary["Records"] * 100

print(summary.round(2).to_string())
print(
    f"\nTrain failures in parts with more than one record: {twin_failures:,} of "
    f"{train['Failed'].sum():,} ({twin_failures / train['Failed'].sum() * 100:.1f}%; "
    f"{train.loc[twin_rows & train['Twin Seen In Train'], 'Failed'].sum() / train['Failed'].sum() * 100:.1f}% "
    f"from the train file alone)"
)
print(
    f"Train records the API treats as a part that repeat a test-file record: {api_part_but_repeat:,} "
    f"(their part's first test isn't in the train file)"
)

summary.rename_axis("Train Records").reset_index().to_csv(RESULTS_DIR / "kaggle_split_one_record_per_part.csv", index=False)


# ============================================================
# 5. PLOT: HIDDEN TWINS FAIL LIKE THE ONES SEEN BEFORE
# ============================================================

# 2nd and later records merged: few 3rd+ records have all their twins in
# the test file
chart = roles.assign(Kind=roles["Role"].astype(str).replace({"2nd record": "Repeat record", "3rd+ record": "Repeat record"}))
chart = chart.groupby(["Kind", "Other Records In"], sort=False)[["Records", "Failures"]].sum()

kinds = ["No twin", "1st record", "Repeat record"]
labels = {
    "No twin": "No twin in\neither file",
    "1st record": "First record\n(first test)",
    "Repeat record": "Later records\n(repeat tests)",
}
series = [
    ("Train file", BLUE, "Twin also in the train file (seen before)"),
    ("Test file only", ORANGE, "Twins only in the test file (new)"),
]

fig, ax = plt.subplots(figsize=(9.5, 5))
width = 0.34

for i, kind in enumerate(kinds):
    if kind == "No twin":
        bars = [(i, MUTED, *chart.loc[(kind, "-")])]
    else:
        bars = [
            (i + (k - 0.5) * width, color, *chart.loc[(kind, where)])
            for k, (where, color, _) in enumerate(series)
        ]

    for x, color, n_records, n_failures in bars:
        rate = n_failures / n_records * 100
        low, high = (v * 100 for v in wilson_interval(n_failures, n_records))

        ax.bar(x, rate, width=width * 0.92, color=color)
        ax.errorbar(x, rate, yerr=[[rate - low], [high - rate]], fmt="none", ecolor=INK_SECONDARY,
                    elinewidth=1.2, capsize=3)
        ax.annotate(f"{rate:.2g}%", (x, high), xytext=(0, 4), textcoords="offset points",
                    ha="center", va="bottom", fontsize=9, color=INK)

for where, color, label in series:
    ax.bar(0, 0, color=color, label=label)

ax.set_xticks(range(len(kinds)))
ax.set_xticklabels([labels[k] for k in kinds], fontsize=9.5)
ax.set_ylabel("Failure rate of train records (%)")
ax.set_ylim(0, ax.get_ylim()[1] * 1.12)
ax.grid(axis="y")
ax.tick_params(axis="x", length=0)
ax.legend(frameon=False, fontsize=9, loc="upper right")

ax.set_title(
    "Twins hidden by the Kaggle split fail like the ones seen before",
    loc="left", fontsize=12, fontweight="bold", pad=34,
)
ax.text(
    0, 1.03,
    f"Train records by their place in their twin group, in Id order across both files. "
    f"{pair_split.loc['One in each file', 'Observed (%)']:.0f}% of twin pairs straddle the files\n"
    f"({pair_split.loc['One in each file', 'Expected If Random (%)']:.0f}% if Kaggle split records at random), "
    f"so the train file alone showed only part of each group. Bars show 95% CI.",
    transform=ax.transAxes, fontsize=9, color=INK_SECONDARY, va="bottom",
)

plt.tight_layout()
plt.savefig(PLOTS_DIR / "kaggle_split_repeats.png", dpi=200, bbox_inches="tight")
plt.close()

print("\nSaved:")
print("  results/kaggle_split_groups.csv")
print("  results/kaggle_split_train_roles.csv")
print("  results/kaggle_split_one_record_per_part.csv")
print("  results/plots/kaggle_split_repeats.png")
