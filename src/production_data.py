"""
Shared helpers for loading the Bosch production-line data.

Column names look like:

    L3_S33_F3867   numeric / categorical feature
    L3_S33_D3868   timestamp (date file)

    L3  = production line
    S33 = station (station numbers are global across lines and follow
          production order -- verified in analyze_dates.py)
"""

from pathlib import Path

import numpy as np
import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
RESULTS_DIR = PROJECT_ROOT / "results"
PLOTS_DIR = RESULTS_DIR / "plots"

# Twin records across both Kaggle files, written by kaggle_split_repeats.py.
# Derived from the Kaggle data, so it stays in data/ (not in git).
TWIN_RECORDS_PATH = DATA_DIR / "derived" / "twin_records.csv"

# Bosch timestamps are anonymized. analyze_dates.py shows production activity
# repeats every 2.4 units (one day) and every 16.8 units (one week), so:
HOURS_PER_UNIT = 10.0
UNITS_PER_WEEK = 16.8


# ============================================================
# COLUMN NAME HELPERS
# ============================================================

def station_of(column):
    """'L3_S33_F3867' -> 'L3_S33'"""
    line, station, _ = column.split("_")
    return f"{line}_{station}"


def station_number(station):
    """'L3_S33' -> 33"""
    return int(station.split("_S")[1])


def line_of(station):
    """'L3_S33' -> 'L3'"""
    return station.split("_")[0]


# ============================================================
# LOADERS
# ============================================================

def load_numeric(nrows):
    return pd.read_csv(
        DATA_DIR / "train_numeric.csv",
        nrows=nrows,
    )


def load_dates(nrows, split="train"):
    """
    Raw train_date.csv rows (split="test": test_date.csv, Kaggle's
    unlabelled records). float32 halves memory; timestamps only have two
    decimals, so nothing is lost.
    """

    path = DATA_DIR / f"{split}_date.csv"
    header = pd.read_csv(path, nrows=0).columns

    dtypes = {col: np.float32 for col in header}
    dtypes["Id"] = np.int64

    return pd.read_csv(
        path,
        nrows=nrows,
        dtype=dtypes,
    )


def station_times(dates):
    """
    Collapse the ~1,156 date columns into one timestamp per station.

    Returns two DataFrames (rows = parts, columns = stations in production
    order):

        first_seen = earliest timestamp recorded at that station
        last_seen  = latest timestamp recorded at that station

    They only differ at L1_S24 / L1_S25, which log several sub-steps.
    NaN means the part never visited that station.
    """

    station_columns = {}

    for col in dates.columns:
        if col.startswith("L"):
            station_columns.setdefault(station_of(col), []).append(col)

    stations = sorted(station_columns, key=station_number)

    first_seen = pd.DataFrame(
        {s: dates[station_columns[s]].min(axis=1) for s in stations}
    )
    last_seen = pd.DataFrame(
        {s: dates[station_columns[s]].max(axis=1) for s in stations}
    )

    return first_seen, last_seen


def load_part_times(nrows, split="train"):
    """
    When each part entered production (its first timestamp) and left it
    (its last timestamp), in date units. NaN for the few parts with no
    timestamps at all.
    """

    dates = load_dates(nrows, split)
    timestamps = dates.drop(columns="Id")

    return pd.DataFrame(
        {
            "Id": dates["Id"],
            "start": timestamps.min(axis=1),
            "end": timestamps.max(axis=1),
        }
    )


# ============================================================
# TWIN RECORDS
# ============================================================

def twin_groups(features, start):
    """
    Group records with exactly the same measurements that entered in the
    same tick ("twins", ~4% of records). Twins also share every timestamp.

    They are most likely repeat records of ONE part (tested again, with its
    production record copied): within a group, the failures sit on the
    first record (lowest Id). A failed test makes a repeat far more likely,
    so twin status isn't known when a part is first tested -- it leaks the
    QC result and must not be a model feature (see twin_feature.py).

    features: DataFrame of the numeric measurements (one row per record)
    start:    when each record entered production (NaN = no timestamps)

    Returns a twin-group id per record; -1 for records without a twin.
    """

    dated = ~np.isnan(start)
    record_hash = pd.util.hash_pandas_object(features, index=False).to_numpy()

    keys = pd.DataFrame({"record": record_hash[dated], "start": start[dated]})
    in_group = keys.groupby(["record", "start"])["record"].transform("size").to_numpy() > 1

    groups = np.full(len(features), -1, dtype=np.int32)
    groups[np.flatnonzero(dated)[in_group]] = keys[in_group].groupby(["record", "start"]).ngroup().to_numpy()

    # Number groups by their first record (row order), not by hash order,
    # so the labels don't depend on pandas' hash function (spark_etl.py
    # reproduces them exactly)
    members = np.flatnonzero(groups >= 0)
    _, first_seen_at, label = np.unique(groups[members], return_index=True, return_inverse=True)
    groups[members] = np.argsort(np.argsort(first_seen_at))[label]

    # Hashes could collide, so check every twin's record bit for bit
    # against the first member of its group
    bits = features.iloc[members].to_numpy(np.float32).view(np.uint32)
    first_member = pd.Series(np.arange(len(members))).groupby(groups[members]).transform("first").to_numpy()
    assert (bits == bits[first_member]).all(), "different records share a twin group"

    return groups


def load_repeat_tests():
    """
    Ids of repeat tests: the 2nd and later records (by Id) of every twin
    group found across BOTH Kaggle files. One record per part = every
    record except these. Written by kaggle_split_repeats.py.
    """

    if not TWIN_RECORDS_PATH.exists():
        raise FileNotFoundError(f"{TWIN_RECORDS_PATH} is missing; run src/kaggle_split_repeats.py first")

    twins = pd.read_csv(TWIN_RECORDS_PATH)

    return twins.loc[twins["Order"] >= 2, "Id"].to_numpy()


# ============================================================
# EVALUATION SPLITS
# ============================================================

def forward_folds(start, end, n_blocks=5):
    """
    Forward-in-time train / test folds.

    Sort parts by when they entered production and cut that timeline
    into n_blocks equal blocks. For every block except the first, train
    on parts whose LAST station came before the block began (their QC
    result was already known) and test on the block.

    Returns a list of (train_rows, test_rows) row-position arrays,
    oldest test period first. Parts with no timestamps are left out.
    """

    dated_rows = np.flatnonzero(~np.isnan(start))
    in_time_order = dated_rows[np.argsort(start[dated_rows], kind="stable")]

    folds = []

    for block in np.array_split(in_time_order, n_blocks)[1:]:
        block_start = start[block].min()
        train_rows = dated_rows[end[dated_rows] < block_start]

        folds.append((train_rows, block))

    return folds
