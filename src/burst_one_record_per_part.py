"""
BURSTS AND THE BATCH-MATE ALERT, COUNTING EACH PART ONCE

About 4% of records are repeat tests of a part already in the data (see
kaggle_split_repeats.py). A repeat shares every timestamp with its part,
so it enters production in the same 6-minute tick, right after its part
in Id order -- and when the part fails, its retest fails too about a
third of the time. Counting repeats therefore inflates any "what happens
right after a failure" number.

This re-checks, forward in time where it applies, the numbers behind the
batch-mate alert and the case for forward-in-time evaluation:

1. P(fail | the part that entered just before failed)
2. Risk after a failure, by time window (by entry time and by QC time)
3. The batch-mate alert (QC results known 1 hour after a part's last
   station), on the 4 forward test periods

each counting every record (as reported before) and counting first tests
only. For the alert, "First Tests" scores first tests but lets every QC
result already known raise a flag, repeats' results included -- the
version burst_monitoring.py, batch_alert_cutoff.py and the API use. A
stricter version only lets first tests' results raise flags.

Only timestamps and Response are needed. Needs data/derived/twin_records.csv
from kaggle_split_repeats.py.

Run from the project root:

    .venv/bin/python src/burst_one_record_per_part.py

Takes about a minute.
"""

import numpy as np
import pandas as pd

from production_data import (
    DATA_DIR,
    RESULTS_DIR,
    forward_folds,
    load_part_times,
    load_repeat_tests,
)
from qc_monitor import (
    TICKS_PER_HOUR,
    QCStream,
    to_ticks,
)


# ============================================================
# CONFIGURATION
# ============================================================

LABEL_DELAY_HOURS = 1

# Same windows as burst_monitoring.py
LAG_WINDOWS = [
    ("same 6 min", 0, 0),
    ("0-1 h", 0, 1),
    ("1-6 h", 1, 6),
    ("6-24 h", 6, 24),
    ("1-3 days", 24, 72),
    ("3-7 days", 72, 168),
    ("1-2 weeks", 168, 336),
    ("2-4 weeks", 336, 672),
    ("4-8 weeks", 672, 1344),
]


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def failure_after_previous(ids, start, y):
    """P(fail | the part that entered just before failed); Id breaks ties."""

    in_time_order = y[np.lexsort((ids, start))]

    return in_time_order[1:][in_time_order[:-1] == 1].mean()


def risk_after_failures(times, y):
    """Failure rate in each window after a failed part, vs. after any part."""

    order = np.argsort(to_ticks(times), kind="stable")
    ticks = to_ticks(times)[order]
    outcome = y[order]
    cum_fail = np.r_[0, np.cumsum(outcome)]

    def failure_rate_after(anchors, low_hours, high_hours):
        t = ticks[anchors]

        if high_hours == 0:
            # Same 6-minute tick, excluding the anchor itself
            lo = np.searchsorted(ticks, t, "left")
            hi = np.searchsorted(ticks, t, "right")
            parts = hi - lo - 1
            fails = cum_fail[hi] - cum_fail[lo] - outcome[anchors]
        else:
            lo = np.searchsorted(ticks, t + low_hours * TICKS_PER_HOUR, "right")
            hi = np.searchsorted(ticks, t + high_hours * TICKS_PER_HOUR, "right")
            parts = hi - lo
            fails = cum_fail[hi] - cum_fail[lo]

        return fails.sum() / parts.sum()

    failed = np.flatnonzero(outcome == 1)
    everyone = np.arange(len(ticks))

    return [
        failure_rate_after(failed, low, high) / failure_rate_after(everyone, low, high)
        for _, low, high in LAG_WINDOWS
    ]


def batch_alert(scored, triggers):
    """
    The batch-mate alert on the forward test periods.

    scored    records the alert is scored on
    triggers  records whose QC results can flag their batch-mates
    """

    qc = QCStream(start[triggers], end[triggers], y[triggers], LABEL_DELAY_HOURS)

    # A record's own result is only known after its last station, so
    # "known before this record's end" always means a batch-mate failed
    first_failure = qc.first_batch_failure_known(start)
    flagged = first_failure < to_ticks(end)

    rows = evaluation[scored[evaluation]]
    hit, outcome = flagged[rows], y[rows]
    caught = rows[hit & (outcome == 1)]

    by_period = [
        y[p[scored[p]]][flagged[p[scored[p]]]].mean() / y[p[scored[p]]].mean()
        for p in periods
    ]

    return {
        "Production Flagged (%)": hit.mean() * 100,
        "Risk Ratio vs. Average": outcome[hit].mean() / outcome.mean(),
        "Risk Ratio vs. Not Flagged": outcome[hit].mean() / outcome[~hit].mean(),
        "Failures Caught (%)": outcome[hit].sum() / outcome.sum() * 100,
        "Median Lead Time (h)": np.median((to_ticks(end[caught]) - first_failure[caught]) / TICKS_PER_HOUR),
        **{f"Risk Ratio vs. Average, Period {i}": ratio for i, ratio in enumerate(by_period, start=1)},
    }


# ============================================================
# 1. LOAD DATA
# ============================================================

print("Loading timestamps and QC results for all parts...")

part_times = load_part_times(None)
response = pd.read_csv(DATA_DIR / "train_numeric.csv", usecols=["Id", "Response"])

# Both files are sorted by Id, so row i is the same part in both
assert (part_times["Id"].to_numpy() == response["Id"].to_numpy()).all()

has_dates = part_times["start"].notna().to_numpy()

ids = part_times["Id"].to_numpy()[has_dates]
start = part_times["start"].to_numpy(np.float64)[has_dates]
end = part_times["end"].to_numpy(np.float64)[has_dates]
y = response["Response"].to_numpy()[has_dates]

every_record = np.ones(len(y), dtype=bool)
first_test = ~np.isin(ids, load_repeat_tests())

# The same forward test periods for every version
periods = [test for _, test in forward_folds(start, end, n_blocks=5)]
evaluation = np.concatenate(periods)

print(
    f"Records with timestamps: {len(y):,} ({y.mean() * 100:.3f}% fail) | first tests: "
    f"{first_test.sum():,} ({y[first_test].mean() * 100:.3f}% fail)"
)


# ============================================================
# 2. COMPARE
# ============================================================

table = {}

for counting, keep in [("All Records", every_record), ("First Tests", first_test)]:
    column = {
        "Failure Rate (%)": y[keep].mean() * 100,
        "P(Fail | Part Entering Just Before Failed) (%)": failure_after_previous(ids[keep], start[keep], y[keep]) * 100,
    }

    for times, label in [(start, "by entry time"), (end, "by QC time")]:
        ratios = risk_after_failures(times[keep], y[keep])
        column.update({f"Risk {window}, {label} (x)": r for (window, _, _), r in zip(LAG_WINDOWS, ratios)})

    table[counting] = column

table = pd.DataFrame(table)

alerts = pd.DataFrame(
    {
        "All Records": batch_alert(every_record, every_record),
        "First Tests": batch_alert(first_test, every_record),
        "First Tests, First Results Only": batch_alert(first_test, first_test),
    }
)

print("\n==============================")
print("BURSTS: COUNTING EVERY RECORD vs. FIRST TESTS")
print("==============================")
print(table.to_string(float_format=lambda v: f"{v:.2f}"))

print("\n==============================")
print(f"BATCH-MATE ALERT ({LABEL_DELAY_HOURS} H QC DELAY, 4 FORWARD TEST PERIODS)")
print("==============================")
print("First Tests = scored on first tests; every QC result already known can flag (as in the API)")
print(alerts.to_string(float_format=lambda v: f"{v:.2f}"))

pd.concat([table, alerts]).rename_axis("Measure").to_csv(RESULTS_DIR / "burst_one_record_per_part.csv")

print("\nSaved:")
print("  results/burst_one_record_per_part.csv")
