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


def load_dates(nrows):
    """
    Raw train_date.csv rows. float32 halves memory; timestamps only have
    two decimals, so nothing is lost.
    """

    path = DATA_DIR / "train_date.csv"
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


def load_part_times(nrows):
    """
    When each part entered production (its first timestamp) and left it
    (its last timestamp), in date units. NaN for the few parts with no
    timestamps at all.
    """

    dates = load_dates(nrows)
    timestamps = dates.drop(columns="Id")

    return pd.DataFrame(
        {
            "Id": dates["Id"],
            "start": timestamps.min(axis=1),
            "end": timestamps.max(axis=1),
        }
    )


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
