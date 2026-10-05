"""
PHASE 3 / STEP 4 -- an honest check of the batch-mate alert's cutoff

monitor_model.py found that batch-mate flags are stronger when they fire
late, and suggested only alerting when the flag fires >= 72 h after the
part entered production. That 72 h was chosen after looking at the
pooled results of all four test periods -- a choice made with the test
data in view.

Here the cutoff is chosen the way a factory would have to: for each
forward test period, using only parts whose QC result was already known
before that period began. The chosen cutoff is then scored on the period.

Selection rule, fixed before running:
    the candidate cutoff with the highest F1 on the history period
    (F1 balances the alert's precision against how many failures it
    catches); ties go to the smaller cutoff.

Compared against: no cutoff (any flag) and the hindsight 72 h cutoff.

Only timestamps and Response are needed, so this uses all 1.18M parts.

Run from the project root:

    .venv/bin/python src/batch_alert_cutoff.py

Takes under a minute.
"""

import numpy as np
import pandas as pd

from production_data import (
    DATA_DIR,
    RESULTS_DIR,
    forward_folds,
    load_part_times,
)
from qc_monitor import (
    TICKS_PER_HOUR,
    QCStream,
    to_ticks,
)


# ============================================================
# CONFIGURATION
# ============================================================

# A QC result becomes known this long after the part's last station.
LABEL_DELAY_HOURS = 1

# Candidate cutoffs: only flag when the batch-mate's failure becomes
# known at least this many hours after the flagged part entered
CANDIDATE_CUTOFFS_HOURS = [0, 24, 48, 72, 96, 120, 168, 240, 336]

HINDSIGHT_CUTOFF_HOURS = 72

FORWARD_BLOCKS = 5


# ============================================================
# 1. LOAD DATA AND COMPUTE FLAGS
# ============================================================

print("Loading timestamps and QC results for all parts...")

part_times = load_part_times(None)
response = pd.read_csv(
    DATA_DIR / "train_numeric.csv",
    usecols=["Id", "Response"],
)
assert (part_times["Id"].to_numpy() == response["Id"].to_numpy()).all()

has_dates = part_times["start"].notna().to_numpy()

start = part_times["start"].to_numpy(np.float64)[has_dates]
end = part_times["end"].to_numpy(np.float64)[has_dates]
y = response["Response"].to_numpy()[has_dates]

qc = QCStream(start, end, y, LABEL_DELAY_HOURS)

# When the first failure in each part's entry batch became known. A part's
# own failure is only known after its last station, so "known before this
# part's end" always means a batch-mate failed first.
first_failure = qc.first_batch_failure_known(start)
entry_tick = to_ticks(start)
end_tick = to_ticks(end)

flagged = first_failure < end_tick
fired_after_entry_h = np.where(flagged, (first_failure - entry_tick) / TICKS_PER_HOUR, np.nan)
lead_h = np.where(flagged, (end_tick - first_failure) / TICKS_PER_HOUR, np.nan)

print("Parts with timestamps:", len(y))


# ============================================================
# 2. HELPERS
# ============================================================

def alert(cutoff_hours):
    """Flags that fire at least cutoff_hours after the part entered."""

    return flagged & (fired_after_entry_h >= cutoff_hours)


def score(rows, flag):
    """How an alert does on a set of parts."""

    hit = rows[flag[rows]]
    caught = hit[y[hit] == 1]

    precision = y[hit].mean() if len(hit) else 0.0
    recall = len(caught) / y[rows].sum()
    f1 = 2 * precision * recall / (precision + recall) if precision + recall > 0 else 0.0

    return {
        "Production Flagged (%)": len(hit) / len(rows) * 100,
        "Risk Ratio": precision / y[rows].mean(),
        "Failures Caught (%)": recall * 100,
        "F1": f1,
        "Flagged Failures": len(caught),
        "Median Lead Time (days)": np.median(lead_h[caught]) / 24 if len(caught) else np.nan,
    }


# ============================================================
# 3. CHOOSE THE CUTOFF ON HISTORY, SCORE IT ON THE NEXT PERIOD
#
# History = parts whose last station came before the test period
# began (their QC result was known) -- the same rows the forward
# folds train on.
# ============================================================

folds = forward_folds(start, end, n_blocks=FORWARD_BLOCKS)

history_rows = []
period_rows = []

for period, (history, test) in enumerate(folds, start=1):

    on_history = pd.DataFrame(
        [{"Cutoff (h)": c, **score(history, alert(c))} for c in CANDIDATE_CUTOFFS_HOURS]
    )
    on_history.insert(0, "Test Period", period)
    history_rows.append(on_history)

    # Highest F1; idxmax returns the first (smallest) cutoff on ties
    chosen = int(on_history.loc[on_history["F1"].idxmax(), "Cutoff (h)"])

    for rule, cutoff in [
        ("Chosen on history", chosen),
        ("Any flag", 0),
        (f"Hindsight {HINDSIGHT_CUTOFF_HOURS} h", HINDSIGHT_CUTOFF_HOURS),
    ]:
        period_rows.append(
            {
                "Test Period": period,
                "Rule": rule,
                "Cutoff (h)": cutoff,
                **score(test, alert(cutoff)),
            }
        )

history_table = pd.concat(history_rows, ignore_index=True)
results = pd.DataFrame(period_rows)

print("\n==============================")
print("F1 OF EACH CUTOFF ON THE HISTORY BEFORE EACH TEST PERIOD")
print("==============================")

print(
    history_table
    .pivot(index="Cutoff (h)", columns="Test Period", values="F1")
    .mul(100)
    .to_string(float_format=lambda v: f"{v:.2f}")
)
print("(F1 x 100; history before period 1 is the first 20% of the timeline)")

print("\n==============================")
print("OUT OF SAMPLE: EACH TEST PERIOD")
print("==============================")

print(
    results[["Test Period", "Rule", "Cutoff (h)", "Production Flagged (%)", "Risk Ratio",
             "Failures Caught (%)", "Median Lead Time (days)"]]
    .to_string(index=False, float_format=lambda v: f"{v:.2f}")
)


# ============================================================
# 4. POOLED OUT-OF-SAMPLE RESULT
#
# Each period uses its own rule (the chosen cutoff varies by
# period); results are pooled over all test parts.
# ============================================================

evaluation = np.concatenate([test for _, test in folds])
pooled_rows = []

for rule in results["Rule"].unique():
    pooled_flag = np.zeros(len(y), dtype=bool)

    for period, (_, test) in enumerate(folds, start=1):
        cutoff = results.loc[(results["Test Period"] == period) & (results["Rule"] == rule), "Cutoff (h)"].item()
        pooled_flag[test] = alert(cutoff)[test]

    pooled_rows.append({"Rule": rule, **score(evaluation, pooled_flag)})

pooled = pd.DataFrame(pooled_rows)

print("\n==============================")
print("OUT OF SAMPLE: ALL 4 TEST PERIODS POOLED")
print("==============================")

print(
    pooled[["Rule", "Production Flagged (%)", "Risk Ratio", "Failures Caught (%)", "Median Lead Time (days)"]]
    .to_string(index=False, float_format=lambda v: f"{v:.2f}")
)

history_table.to_csv(RESULTS_DIR / "batch_alert_cutoff_history.csv", index=False)
results.to_csv(RESULTS_DIR / "batch_alert_cutoff_by_period.csv", index=False)
pooled.to_csv(RESULTS_DIR / "batch_alert_cutoff_pooled.csv", index=False)

print("\nSaved:")
print("  results/batch_alert_cutoff_history.csv")
print("  results/batch_alert_cutoff_by_period.csv")
print("  results/batch_alert_cutoff_pooled.csv")
