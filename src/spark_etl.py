"""
ETL STEP -- the per-part tables, built with PySpark

A PySpark version of the per-part transformations in build_serving_data.py
(and production_data.station_times / twin_groups). It reads the raw Kaggle
CSVs and writes Parquet:

    serving/etl/parts.parquet               one row per part: start, end, entry line,
                                            stations visited, through L2, L3 path,
                                            QC result, twin group
    serving/etl/station_first_seen.parquet  part_id + one column per station (in
                                            production order): earliest timestamp
                                            there, null = not visited

check_spark_etl.py confirms the output matches the pandas build value for
value. Model scoring stays in Python (build_serving_data.py).

Spark runs in local mode here: about 25 s on an 18-core Mac, against 46 s
for the same steps in pandas, mostly because Spark parses the CSVs in
parallel. The bigger point is that the job would run unchanged on a
cluster as the data grows.

Needs Java 17, 21 or 25. Run from the project root:

    .venv/bin/python src/spark_etl.py

Takes about 25 seconds. --data and --out run it on other inputs in the same
format, such as the digital twin's data at scale (twin_scale_data.py), whose
CSVs are folders of part files and carry no measurements (so no twin groups):

    .venv/bin/python src/spark_etl.py --data data/twin_scale --out serving/etl_scale
"""

import argparse
import operator
import time
import warnings
from pathlib import Path
from functools import reduce

from pyspark.sql import SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

from production_data import (
    DATA_DIR,
    PROJECT_ROOT,
    line_of,
    station_number,
    station_of,
)

ETL_DIR = PROJECT_ROOT / "serving" / "etl"

# PySpark warns about pandas 3 for its pandas interop, which this job doesn't use
warnings.filterwarnings("ignore", message="PySpark does not yet fully support pandas")

# Stations 29-38 and 39-51 are the two paths through L3 (analyze_dates.py)
L3_PATHS = [("S29-S38", 29, 38), ("S39-S51", 39, 51)]


def any_of(conditions):
    return reduce(operator.or_, conditions)


def lowest(columns):
    return columns[0] if len(columns) == 1 else F.least(*columns)


def highest(columns):
    return columns[0] if len(columns) == 1 else F.greatest(*columns)


def read_csv(spark, name, column_type):
    """A Kaggle CSV with an explicit schema (no inference pass over 1,000 columns)."""

    path = DATA / name
    if not path.exists():
        path = DATA / name.removesuffix(".csv")       # a folder of CSV part files
    first = path if path.is_file() else sorted(path.glob("part-*.csv"))[0]
    with open(first) as f:
        header = f.readline().strip().split(",")

    schema = T.StructType([T.StructField(c, column_type(c), True) for c in header])
    return spark.read.csv(str(path), header=True, schema=schema), header


parser = argparse.ArgumentParser(description="Per-part ETL in PySpark")
parser.add_argument("--data", type=Path, default=DATA_DIR, help="folder with train_date and train_numeric")
parser.add_argument("--out", type=Path, default=ETL_DIR, help="folder for the Parquet output")
parser.add_argument("--memory", default="16g", help="Spark driver memory")
parser.add_argument("--files", type=int, default=1, help="Parquet files per table")
args = parser.parse_args()
DATA, ETL_DIR = args.data.resolve(), args.out.resolve()

started = time.time()

spark = (
    SparkSession.builder
    .master("local[*]")
    .appName("forgeops-etl")
    .config("spark.driver.memory", args.memory)
    .config("spark.sql.shuffle.partitions", "64")
    .config("spark.ui.enabled", "false")
    .config("spark.ui.showConsoleProgress", "false")
    .getOrCreate()
)
spark.sparkContext.setLogLevel("ERROR")


# ============================================================
# 1. READ THE RAW CSVS
#
# Numbers are parsed as doubles and stored as float32, the same
# path as the pandas loaders (empty fields are null).
# ============================================================

dates, date_columns = read_csv(
    spark, "train_date.csv",
    lambda c: T.LongType() if c == "Id" else T.DoubleType(),
)
numeric, numeric_columns = read_csv(
    spark, "train_numeric.csv",
    lambda c: T.LongType() if c == "Id" else T.IntegerType() if c == "Response" else T.DoubleType(),
)


# ============================================================
# 2. STATION TIMES
#
# One timestamp per station: the earliest and latest of its date
# columns (only L1_S24 / L1_S25 log several sub-steps).
# ============================================================

station_columns = {}
for c in date_columns[1:]:
    station_columns.setdefault(station_of(c), []).append(c)

stations = sorted(station_columns, key=station_number)

station_times = dates.select(
    F.col("Id").alias("part_id"),
    *[lowest([F.col(c).cast("float") for c in station_columns[s]]).alias(s) for s in stations],
    *[highest([F.col(c).cast("float") for c in station_columns[s]]).alias(f"{s}__last") for s in stations],
).cache()


# ============================================================
# 3. ROUTES
# ============================================================

def visited(station):
    return F.col(station).isNotNull()


# The first L3 path the part touched wins, as in np.select
l3_path = F.lit("none")
for label, low, high in reversed(L3_PATHS):
    l3_path = F.when(any_of([visited(s) for s in stations if low <= station_number(s) <= high]), label).otherwise(l3_path)

routes = station_times.select(
    "part_id",
    lowest([F.col(s) for s in stations]).cast("double").alias("start"),
    highest([F.col(f"{s}__last") for s in stations]).cast("double").alias("end"),
    # Line of the first station visited, in production order
    F.coalesce(*[F.when(visited(s), F.lit(line_of(s))) for s in stations], F.lit("none")).alias("entry_line"),
    reduce(operator.add, [visited(s).cast("short") for s in stations]).cast("short").alias("stations_visited"),
    any_of([visited(s) for s in stations if line_of(s) == "L2"]).alias("through_l2"),
    l3_path.alias("l3_path"),
)


# ============================================================
# 4. QC RESULTS AND TWIN RECORDS
#
# Twins share every measurement (missing values included) and the
# entry tick. A record's key is a SHA-256 over its 968 float32
# values written out exactly (null stays empty), so equal keys
# mean identical records. Groups are numbered by their first
# record in Id order, as production_data.twin_groups does.
# ============================================================

features = [c for c in numeric_columns if c.startswith("L")]

# Without measurements (the twin's data) records can't be compared: no twin groups
record_key = F.sha2(
    F.concat_ws("|", *[F.coalesce(F.col(c).cast("float").cast("string"), F.lit("")) for c in features]),
    256,
) if features else F.col("Id").cast("string")

parts = routes.join(
    numeric.select(
        F.col("Id").alias("part_id"),
        F.col("Response").cast("byte").alias("response"),
        record_key.alias("record"),
    ),
    "part_id",
).cache()

twins = (
    parts.where(F.col("start").isNotNull())
    .groupBy("record", "start")
    .agg(F.count("*").alias("size"), F.min("part_id").alias("first_part"))
    .where(F.col("size") > 1)
    .withColumn("twin_group", (F.row_number().over(Window.orderBy("first_part")) - 1).cast("int"))
)

parts = (
    parts.join(twins.select("record", "start", "twin_group"), ["record", "start"], "left")
    .fillna(-1, subset=["twin_group"])
    .select(
        "part_id", "start", "end", "entry_line", "stations_visited",
        "through_l2", "l3_path", "response", "twin_group",
    )
)


# ============================================================
# 5. WRITE PARQUET
# ============================================================

def write(frame, name):
    frame.orderBy("part_id").coalesce(args.files).write.mode("overwrite").parquet(str(ETL_DIR / name))


write(parts, "parts.parquet")
write(station_times.select("part_id", *stations), "station_first_seen.parquet")

summary = spark.read.parquet(str(ETL_DIR / "parts.parquet")).agg(
    F.count("*").alias("parts"),
    F.sum((F.col("twin_group") >= 0).cast("int")).alias("twin_records"),
    F.max("twin_group").alias("last_group"),
).first()

print(
    f"Spark {spark.version}: {summary['parts']:,} parts, {summary['twin_records']:,} twin records in "
    f"{summary['last_group'] + 1:,} groups, {len(stations)} stations -> {ETL_DIR.relative_to(PROJECT_ROOT)}/ "
    f"in {time.time() - started:.0f} s"
)

spark.stop()
