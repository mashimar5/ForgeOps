"""
Checks that spark_etl.py reproduces the pandas build exactly.

Compares the PySpark output in serving/etl/ with what build_serving_data.py
writes (serving/parts.parquet, serving/station_first_seen.npy): every column
of every part, value for value, missing values in the same places.

    .venv/bin/python src/check_spark_etl.py

Exits with status 1 on any mismatch.
"""

import json
import sys

import numpy as np
import pandas as pd

from production_data import PROJECT_ROOT


SERVING_DIR = PROJECT_ROOT / "serving"


def compare(name, spark_values, pandas_values):
    """Report one column; True when every value matches."""

    a, b = np.asarray(spark_values), np.asarray(pandas_values)

    if a.dtype.kind == "f" or b.dtype.kind == "f":
        same = (a == b) | (np.isnan(a) & np.isnan(b))
    else:
        same = a.astype(str) == b.astype(str)

    mismatches = int((~same).sum())
    print(
        f"  {name:20} {'OK' if mismatches == 0 else 'MISMATCH'}"
        f"  ({len(a) - mismatches:,} of {len(a):,} match; spark {a.dtype}, pandas {b.dtype})"
    )
    return mismatches == 0


spark_parts = pd.read_parquet(SERVING_DIR / "etl" / "parts.parquet").sort_values("part_id", ignore_index=True)
pandas_parts = pd.read_parquet(SERVING_DIR / "parts.parquet")

print(f"parts: spark {len(spark_parts):,} rows, pandas {len(pandas_parts):,} rows")
assert len(spark_parts) == len(pandas_parts)

results = [compare(column, spark_parts[column], pandas_parts[column]) for column in spark_parts.columns]

with open(SERVING_DIR / "meta.json") as f:
    stations = json.load(f)["stations"]

spark_seen = pd.read_parquet(SERVING_DIR / "etl" / "station_first_seen.parquet").sort_values("part_id", ignore_index=True)
pandas_seen = np.load(SERVING_DIR / "station_first_seen.npy")

print(f"\nstation first-seen: spark {spark_seen.shape[1] - 1} stations, pandas {pandas_seen.shape[1]}")
results.append(compare("station order", np.array(spark_seen.columns[1:]), np.array(stations)))
results.append(compare("first-seen values", spark_seen[stations].to_numpy(np.float32).ravel(), pandas_seen.ravel()))

ok = all(results)
print("\nspark ETL:", "matches the pandas build exactly" if ok else "DOES NOT match the pandas build")
sys.exit(0 if ok else 1)
