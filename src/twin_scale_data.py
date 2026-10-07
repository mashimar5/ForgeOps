"""
DIGITAL TWIN, STEP 5 -- data at scale for the Spark ETL

Generates N times the dataset's volume in the Kaggle CSV format, so the
PySpark ETL (spark_etl.py) can run at a size where one machine's memory
stops being enough for pandas.

1. Python: N runs of the twin (digital_twin.py), each over the real
   production plan (which entry lines ran in weeks 1-101), laid end to end:
   about N x 2 years of production. Writes the simulated parts to
   data/twin_scale/parts.parquet: Id, the real template part it copies,
   how far to shift its timestamps, and its simulated QC result.
2. Spark: joins that table with the real train_date.csv and shifts every
   date column of the template (stations before line 3 to the simulated
   entry, line-3 stations to the simulated line-3 start), then writes
   data/twin_scale/train_date/ and train_numeric/ (Id and Response only:
   the twin is flow only, so there are no measurements and no repeat
   tests) as folders of CSV part files.

    .venv/bin/python src/twin_scale_data.py --scale 10      # ~10 min, ~30 GB
    .venv/bin/python src/spark_etl.py --data data/twin_scale --out serving/etl_scale --files 32

The output is derived from the Kaggle data, so it stays in data/ (not in git).
"""

import argparse
import time
import warnings

import numpy as np
import pandas as pd

from digital_twin import TICKS_PER_WEEK, calibrate, fit_line3_policy, load_history, plan_weeks, real_weeks, simulate
from production_data import DATA_DIR, station_number, station_of

OUT_DIR = DATA_DIR / "twin_scale"
FIRST_WEEK, LAST_WEEK = 1, 101       # the real plan replayed in each run

parser = argparse.ArgumentParser(description="Generate the dataset at N times its volume with the digital twin")
parser.add_argument("--scale", type=int, default=10)
parser.add_argument("--seed", type=int, default=0)
args = parser.parse_args()
OUT_DIR.mkdir(parents=True, exist_ok=True)
started = time.time()


# ============================================================
# 1. THE TWIN: N RUNS OF THE REAL PRODUCTION PLAN
# ============================================================

h = load_history()
cal = calibrate(h, until_week=LAST_WEEK + 1, first_week=FIRST_WEEK)
fit_line3_policy(cal, h, FIRST_WEEK, LAST_WEEK + 1)
kinds = [w.kind for w in real_weeks(h, FIRST_WEEK, LAST_WEEK)]
span = LAST_WEEK - FIRST_WEEK + 1

frames = []
for k in range(args.scale):
    rng = np.random.default_rng(args.seed * 1000 + k)
    run = simulate(cal, plan_weeks(cal, kinds, rng), FIRST_WEEK + k * span, seed=args.seed * 1000 + k)
    row = cal.tpl_row[run.template]
    frames.append(pd.DataFrame({
        "template_id": h.part_id[row],
        "entry_shift": run.start - h.start[row],            # ticks
        "line3_shift": run.l3_start - h.l3_start[row],
        "start": run.start,
        "response": run.response,
    }))
    print(f"  run {k + 1}/{args.scale}: {len(run):,} parts ({time.time() - started:.0f} s)")

parts = pd.concat(frames, ignore_index=True).sort_values("start", kind="stable", ignore_index=True)
parts.insert(0, "Id", np.arange(1, len(parts) + 1, dtype=np.int64))
parts.drop(columns="start").to_parquet(OUT_DIR / "parts.parquet", index=False)
print(f"{len(parts):,} simulated parts ({len(parts) / len(h):.1f}x the real {len(h):,}), "
      f"failure rate {parts['response'].mean():.3%} -> {OUT_DIR / 'parts.parquet'}")
del frames, parts


# ============================================================
# 2. SPARK: THE TEMPLATES' TIMESTAMPS, SHIFTED, AS KAGGLE CSVS
# ============================================================

from pyspark.sql import SparkSession            # noqa: E402
from pyspark.sql import functions as F          # noqa: E402
from pyspark.sql import types as T              # noqa: E402

warnings.filterwarnings("ignore", message="PySpark does not yet fully support pandas")

spark = (
    SparkSession.builder
    .master("local[*]")
    .appName("forgeops-twin-scale")
    .config("spark.driver.memory", "24g")
    .config("spark.sql.shuffle.partitions", "200")
    .config("spark.ui.enabled", "false")
    .config("spark.ui.showConsoleProgress", "false")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("ERROR")

with open(DATA_DIR / "train_date.csv") as f:
    header = f.readline().strip().split(",")
schema = T.StructType([T.StructField(c, T.LongType() if c == "Id" else T.DoubleType(), True) for c in header])
dates = spark.read.csv(str(DATA_DIR / "train_date.csv"), header=True, schema=schema).withColumnRenamed("Id", "template_id")

plan = spark.read.parquet(str(OUT_DIR / "parts.parquet"))

# Date units: 1 unit = 100 ticks. Stations before line 3 move with the entry, line-3 stations with line 3.
date_columns = header[1:]
shifted = [
    F.round(F.col(c) + F.col("line3_shift" if station_number(station_of(c)) >= 29 else "entry_shift") / 100.0, 2).alias(c)
    for c in date_columns
]
table = plan.join(dates, "template_id").select("Id", *shifted, "response")

(table.select("Id", *date_columns)
 .write.mode("overwrite").option("header", True).csv(str(OUT_DIR / "train_date")))
(table.select("Id", F.col("response").alias("Response"))
 .write.mode("overwrite").option("header", True).csv(str(OUT_DIR / "train_numeric")))

size = sum(f.stat().st_size for f in (OUT_DIR / "train_date").glob("part-*.csv"))
print(f"wrote {OUT_DIR / 'train_date'} ({size / 2**30:.1f} GB) and train_numeric/ in {time.time() - started:.0f} s")
spark.stop()
