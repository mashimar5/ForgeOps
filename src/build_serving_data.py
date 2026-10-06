"""
API STEP 1 -- build the data the API serves from

Re-reading ~5 GB of CSVs every time the API starts would take a minute,
so this script writes compact files to serving/ once:

    parts.parquet            one row per part: timing, route, QC result, risk score, twin group
    station_first_seen.npy   parts x stations: earliest timestamp (NaN = not visited)
    scoring_features.npy     measurements of the parts the model may score
    meta.json                station and feature names, data range, model card

The model only scores parts it never trained on: parts whose last station
came after its training cutoff (see train_xgboost.py).

Needs models/xgboost_final.ubj -- run src/train_xgboost.py first.

Run from the project root:

    .venv/bin/python src/build_serving_data.py

Takes about a minute.
"""

import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

from production_data import (
    DATA_DIR,
    HOURS_PER_UNIT,
    PROJECT_ROOT,
    RESULTS_DIR,
    line_of,
    load_dates,
    station_number,
    station_times,
)


SERVING_DIR = PROJECT_ROOT / "serving"
MODELS_DIR = PROJECT_ROOT / "models"

SERVING_DIR.mkdir(exist_ok=True)


# ============================================================
# 1. LOAD DATA
# ============================================================

print("Loading timestamps...")

dates = load_dates(None)
first_seen, last_seen = station_times(dates)
ids = dates["Id"].to_numpy()
del dates

print("Loading measurements...")

header = pd.read_csv(DATA_DIR / "train_numeric.csv", nrows=0).columns
feature_names = [c for c in header if c.startswith("L")]

numeric = pd.read_csv(
    DATA_DIR / "train_numeric.csv",
    dtype={c: np.float32 for c in feature_names},
)

# Both files are sorted by Id, so row i is the same part in both.
assert (numeric["Id"].to_numpy() == ids).all()

start = first_seen.min(axis=1).to_numpy(np.float64)
end = last_seen.max(axis=1).to_numpy(np.float64)
y = numeric["Response"].to_numpy(np.int8)

print("Parts:", len(ids))


# ============================================================
# 2. ROUTES
# ============================================================

stations = list(first_seen.columns)
visited = first_seen.notna().to_numpy()
station_lines = np.array([line_of(s) for s in stations])
station_nums = np.array([station_number(s) for s in stations])

entry_line = np.where(
    visited.any(axis=1),
    station_lines[np.argmax(visited, axis=1)],
    "none",
)

l3_path = np.select(
    [
        visited[:, (station_nums >= 29) & (station_nums <= 38)].any(axis=1),
        visited[:, station_nums >= 39].any(axis=1),
    ],
    ["S29-S38", "S39-S51"],
    default="none",
)


# ============================================================
# 3. RISK SCORES FOR THE PARTS THE MODEL NEVER SAW
#
# The model trained on parts whose last station came before the
# start of its test period. Every part that finished at or after
# that cutoff is out of sample, so it can be scored honestly.
# ============================================================

print("Scoring parts the model never trained on...")

model = XGBClassifier()
model.load_model(MODELS_DIR / "xgboost_final.ubj")

with open(MODELS_DIR / "xgboost_final_metadata.json") as f:
    model_metadata = json.load(f)

training_cutoff = model_metadata["test_period_start"]
scorable = ~np.isnan(end) & (end >= training_cutoff)
scoring_rows = np.flatnonzero(scorable)

scoring_features = numeric.loc[scorable, feature_names]
risk = model.predict_proba(scoring_features)[:, 1]

# Percentiles are not stored: ranking a part against the whole scoring
# period would use parts that finish later. The API ranks each part
# against the parts already scored at the time it is asked about.
scoring_row = np.full(len(ids), -1, dtype=np.int32)
scoring_row[scoring_rows] = np.arange(len(scoring_rows))

risk_score = np.full(len(ids), np.nan, dtype=np.float32)
risk_score[scoring_rows] = risk

print(f"Scorable parts: {len(scoring_rows):,} ({y[scorable].sum():,} failures)")


# ============================================================
# 4. TWIN RECORDS
#
# About 4% of parts have exactly the same measurement record as
# another part. Twins always enter and finish together (same
# entry and end tick) and ~96% of twin groups share their QC
# result, so they look like separate parts processed together.
# Their risk scores are identical, so the API lists each group
# once.
# ============================================================

dated = ~np.isnan(start)
record_hash = pd.util.hash_pandas_object(numeric[feature_names], index=False).to_numpy()

keys = pd.DataFrame({"record": record_hash[dated], "start": start[dated]})
in_group = keys.groupby(["record", "start"])["record"].transform("size").to_numpy() > 1

twin_group = np.full(len(ids), -1, dtype=np.int32)
twin_group[np.flatnonzero(dated)[in_group]] = keys[in_group].groupby(["record", "start"]).ngroup().to_numpy()

# Hashes could collide, so check every twin's record bit for bit
# against the first member of its group
members = np.flatnonzero(twin_group >= 0)
bits = numeric.iloc[members][feature_names].to_numpy(np.float32).view(np.uint32)
first_member = pd.Series(np.arange(len(members))).groupby(twin_group[members]).transform("first").to_numpy()
assert (bits == bits[first_member]).all(), "different records share a twin group"

print(f"Twin records: {len(members):,} parts in {twin_group.max() + 1:,} groups")


# ============================================================
# 5. WRITE THE SERVING FILES
# ============================================================

parts = pd.DataFrame(
    {
        "part_id": ids,
        "start": start,
        "end": end,
        "entry_line": entry_line,
        "stations_visited": visited.sum(axis=1).astype(np.int16),
        "through_l2": visited[:, station_lines == "L2"].any(axis=1),
        "l3_path": l3_path,
        "response": y,
        "scoring_row": scoring_row,
        "risk_score": risk_score,
        "twin_group": twin_group,
    }
)

parts.to_parquet(SERVING_DIR / "parts.parquet", index=False)

np.save(SERVING_DIR / "station_first_seen.npy", first_seen.to_numpy(np.float32))
np.save(SERVING_DIR / "scoring_features.npy", scoring_features.to_numpy(np.float32))

# Forward-in-time results across the 4 test periods (train_xgboost.py)
periods = pd.read_csv(RESULTS_DIR / "forward_test_periods.csv")

meta = {
    "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "hours_per_unit": HOURS_PER_UNIT,
    "first_hour": round(float(np.nanmin(start)) * HOURS_PER_UNIT, 1),
    "last_hour": round(float(np.nanmax(end)) * HOURS_PER_UNIT, 1),
    "stations": stations,
    "feature_names": feature_names,
    "model": {
        "training_parts": model_metadata["training_parts"],
        "training_cutoff_hour": round(training_cutoff * HOURS_PER_UNIT, 1),
        "scorable_parts": int(len(scoring_rows)),
        "forward_lift_mean": float(periods["Lift"].mean()),
        "forward_lift_min": float(periods["Lift"].min()),
        "forward_lift_max": float(periods["Lift"].max()),
        "forward_top_1pct_recall_mean": float(periods["Top 1% Recall (%)"].mean()),
        "forward_top_1pct_recall_min": float(periods["Top 1% Recall (%)"].min()),
        "forward_top_1pct_recall_max": float(periods["Top 1% Recall (%)"].max()),
    },
}

with open(SERVING_DIR / "meta.json", "w") as f:
    json.dump(meta, f, indent=2)

print("\nSaved to serving/:")
for path in sorted(SERVING_DIR.iterdir()):
    print(f"  {path.name:26} {path.stat().st_size / 1e6:8.1f} MB")
