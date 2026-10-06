"""
API STEP 2 -- the evidence layer behind the API

Answers questions about the factory "as of" a production time, using only
what was known then: stations a part had already visited, and QC results
already reported (LABEL_DELAY_HOURS after a part's last station). The
model only scores parts it never trained on, and ranks a part only
against parts already scored at that time.

Twin records (identical measurements and timestamps) are most likely
repeat tests of one part (see twin_feature.py). A group's first record
(lowest Id) is the part; later "repeat records" only appear once the
part's QC result is reported, since a failed test is what usually leads
to a repeat. Part counts and lists count each part once; QC result
counts and failure rates include every record.

Times are production hours since the first timestamp in the data. The
data is anonymized, so there are no calendar dates.

Every method returns plain dicts, so the same functions can back the HTTP
API (api.py) and, later, tools for an AI assistant.

Build the data first:

    .venv/bin/python src/build_serving_data.py
"""

import json
from collections import Counter

import numpy as np
import pandas as pd
import shap
from xgboost import XGBClassifier

from production_data import PROJECT_ROOT, line_of, station_number, station_of
from qc_monitor import (
    NEVER,
    TICKS_PER_HOUR,
    TICKS_PER_UNIT,
    QCStream,
    hours_to_ticks,
    to_ticks,
)


SERVING_DIR = PROJECT_ROOT / "serving"
MODEL_PATH = PROJECT_ROOT / "models" / "xgboost_final.ubj"

# A QC result becomes known this long after the part's last station.
LABEL_DELAY_HOURS = 1

# Line monitor (see burst_monitoring.py)
LINE_WINDOW_HOURS = 72
LINE_ALERT_RATIO = 1.5
MIN_MONITOR_PARTS = 300

# Campaign status (see campaign_analysis.py)
CAMPAIGN_WINDOW_HOURS = 168
CAMPAIGN_L1_SHARE = 0.10
MIN_CAMPAIGN_PARTS = 1_000

# Risk percentiles need enough already-scored parts to rank against
MIN_REFERENCE_PARTS = 1_000
TOP_RISK_PERCENTILE = 99.0

# Station never visited (int32 station tick matrix)
NOT_VISITED = np.iinfo(np.int32).max

BATCH_ALERT_NOTE = (
    "Parts still in production whose entry batch-mate (same 6-minute entry tick) already "
    "failed final QC. In forward tests, flagged parts failed at about 2.6x the average rate "
    "(1.7% of production flagged, 4.4% of failures caught, about 4 days before final QC; "
    "each part counted once). "
    "The lead time comes from L1-entry campaigns."
)

LINE_STATUS_NOTE = (
    "The line monitor flags long high-failure stretches, with a lag. In forward tests it was "
    "not reliable day to day; treat the alert as an indicator, not a prediction."
)

RISK_NOTE = (
    "Risk scores rank finished parts for final-QC inspection; they are not calibrated "
    "probabilities. Percentiles rank a part against parts already scored at this time."
)

REPEAT_NOTE = (
    "About 2% of parts have more than one record (twin records: identical measurements and "
    "timestamps, most likely repeat tests), shown once the part's QC result is reported. "
    "Part counts count each part once; QC result counts and failure rates include every record."
)


class NotFound(Exception):
    """A part or station that can't be shown at this time."""


def rounded(value, digits):
    """Round for display; None stays None."""

    return None if value is None else round(float(value), digits)


class FactoryService:

    def __init__(self, serving_dir=SERVING_DIR, model_path=MODEL_PATH):
        with open(serving_dir / "meta.json") as f:
            self.meta = json.load(f)

        self.stations = self.meta["stations"]
        self.feature_names = self.meta["feature_names"]
        self.first_hour = self.meta["first_hour"]
        self.last_hour = self.meta["last_hour"]

        parts = pd.read_parquet(serving_dir / "parts.parquet")

        self._row_of = pd.Index(parts["part_id"])
        self._part_id = parts["part_id"].to_numpy()
        self._entry_line = parts["entry_line"].to_numpy()
        self._response = parts["response"].to_numpy()
        self._scoring_row = parts["scoring_row"].to_numpy()
        self._risk_score = parts["risk_score"].to_numpy()
        self._scorable = self._scoring_row >= 0

        # Rows are in Id order; twin groups below rely on it
        assert (np.diff(self._part_id) > 0).all()

        # All times are integer 6-minute ticks; NEVER = no timestamps
        dated = parts["start"].notna().to_numpy()
        start_units = parts["start"].to_numpy()
        end_units = parts["end"].to_numpy()

        self._start = np.full(len(parts), NEVER, dtype=np.int64)
        self._end = np.full(len(parts), NEVER, dtype=np.int64)
        self._start[dated] = to_ticks(start_units[dated])
        self._end[dated] = to_ticks(end_units[dated])
        self._delay = hours_to_ticks(LABEL_DELAY_HOURS)

        # Twin records (see build_serving_data.py) and the rows in each
        # twin group, lowest Id first
        self._twin_group = parts["twin_group"].to_numpy()

        twin_rows = np.flatnonzero(self._twin_group >= 0)
        twin_rows = twin_rows[np.argsort(self._twin_group[twin_rows], kind="stable")]
        groups, first, size = np.unique(self._twin_group[twin_rows], return_index=True, return_counts=True)
        self._twin_members = {int(g): twin_rows[f:f + n] for g, f, n in zip(groups, first, size)}

        # A group's first record is the part; the rest are repeat records.
        # A part can be seen once it enters production, a repeat record
        # only once the part's QC result is reported (same tick as
        # _qc_known), because the repeat usually follows a failed test.
        # Only train-file records are served: a repeat whose first record
        # is in Kaggle's test file looks like a part here and stands in for
        # it (see kaggle_split_repeats.py).
        self._repeat = np.zeros(len(parts), dtype=bool)
        self._repeat[np.delete(twin_rows, first)] = True
        self._is_part = dated & ~self._repeat

        self._visible_from = self._start.copy()
        self._visible_from[self._repeat] = self._end[self._repeat] + self._delay + 1

        # When each station was first visited. Column-major, because
        # station metrics read whole columns.
        first_seen = np.load(serving_dir / "station_first_seen.npy")
        visited = ~np.isnan(first_seen)

        station_tick = np.full(first_seen.shape, NOT_VISITED, dtype=np.int32)
        station_tick[visited] = to_ticks(first_seen[visited])
        self._station_tick = np.asfortranarray(station_tick)
        del first_seen, visited, station_tick

        self._scoring_features = np.load(serving_dir / "scoring_features.npy", mmap_mode="r")

        # QC results, ordered by when each became known. Repeat records'
        # results are reported together with their part's own.
        self._qc = QCStream(start_units[dated], end_units[dated], self._response[dated], LABEL_DELAY_HOURS)

        self._first_batch_failure = np.full(len(parts), NEVER, dtype=np.int64)
        self._first_batch_failure[dated] = self._qc.first_batch_failure_known(start_units[dated])

        # Batch-mates are the other parts that entered in the same tick
        # (repeat records aren't parts). A part counts as failed if any of
        # its QC results failed: its repeat records' results are reported
        # together with its own.
        parts_by_entry = np.flatnonzero(self._is_part)
        self._parts_by_entry = parts_by_entry[np.argsort(self._start[parts_by_entry], kind="stable")]
        self._entry_ticks = self._start[self._parts_by_entry]
        self._batch_size = (
            np.searchsorted(self._entry_ticks, self._start, "right")
            - np.searchsorted(self._entry_ticks, self._start, "left")
        )

        self._part_failed = self._response.astype(bool)
        group_failed = np.zeros(self._twin_group.max() + 1, dtype=bool)
        np.logical_or.at(group_failed, self._twin_group[twin_rows], self._part_failed[twin_rows])
        self._part_failed[twin_rows] = group_failed[self._twin_group[twin_rows]]

        model = XGBClassifier()
        model.load_model(model_path)
        self._explainer = shap.TreeExplainer(model)

        # "L3_S32" and "S32" both find station L3_S32
        self._station_index = {}
        for j, s in enumerate(self.stations):
            self._station_index[s] = j
            self._station_index[s.split("_")[1]] = j

        self._features_per_station = Counter(station_of(f) for f in self.feature_names)

    # ============================================================
    # HELPERS
    # ============================================================

    def _tick(self, at_hour):
        """Production hour -> tick. Defaults to the end of the data."""

        hours = self.last_hour if at_hour is None else at_hour
        return int(round(hours * TICKS_PER_HOUR))

    @staticmethod
    def _hours(tick):
        return float(tick) / TICKS_PER_HOUR

    def _row(self, part_id, tick):
        """Row of a part that had entered production by `tick`."""

        try:
            row = self._row_of.get_loc(part_id)
        except KeyError:
            row = None

        # A future part (or a repeat record not yet shown) is reported
        # exactly like an unknown one
        if row is None or self._visible_from[row] > tick:
            raise NotFound(f"Part {part_id} is not known at hour {self._hours(tick):.1f}.")

        return row

    def _qc_known(self, tick):
        """Parts whose QC result was reported before `tick`."""

        return self._end < tick - self._delay

    def _scored(self, tick):
        """Parts with a risk score by `tick` (repeat records left out)."""

        return self._scorable & self._is_part & (self._end <= tick)

    def _percentile(self, score, tick):
        """Rank a score against parts already scored by `tick` (100 = highest)."""

        reference = self._risk_score[self._scored(tick)]

        if len(reference) < MIN_REFERENCE_PARTS:
            return None

        return rounded((reference < score).mean() * 100, 2)

    def _risk_summary(self, row, tick):
        if not self._scorable[row]:
            cutoff = self.meta["model"]["training_cutoff_hour"]
            return {
                "available": False,
                "reason": (
                    f"The model trained on this part (it finished before the model's training "
                    f"cutoff at hour {cutoff:.1f}), so it has no honest score."
                ),
            }

        if self._end[row] > tick:
            return {
                "available": False,
                "reason": "The model scores complete records; this part has not reached its last station yet.",
            }

        score = float(self._risk_score[row])
        percentile = self._percentile(score, tick)

        return {
            "available": True,
            "risk_score": rounded(score, 4),
            "risk_percentile": percentile,
            "top_1_percent": None if percentile is None else percentile >= TOP_RISK_PERCENTILE,
        }

    def _route(self, row, tick):
        ticks = self._station_tick[row]
        done = np.flatnonzero(ticks <= tick)  # stations are in production order

        return [
            {
                "station": self.stations[j],
                "hour": self._hours(ticks[j]),
                "hours_after_entry": self._hours(ticks[j] - self._start[row]),
            }
            for j in done
        ]

    def _twins(self, row):
        """Ids of the other records in this record's twin group (empty if none)."""

        group = int(self._twin_group[row])

        if group < 0:
            return []

        return sorted(int(self._part_id[r]) for r in self._twin_members[group] if r != row)

    def model_card(self):
        m = self.meta["model"]

        return {
            "description": (
                "XGBoost model scoring a part's complete measurement record at final QC. "
                "Scores rank parts for inspection; they are not calibrated probabilities."
            ),
            "evaluation": (
                "Forward in time: trained only on parts already through QC and tested on parts "
                "produced later, over 4 test periods, counting each part once (its first test)."
            ),
            "training_parts": m["training_parts"],
            "training_cutoff_hour": m["training_cutoff_hour"],
            "scorable_parts": m["scorable_parts"],
            "forward_lift_mean": rounded(m["forward_lift_mean"], 2),
            "forward_lift_range": [rounded(m["forward_lift_min"], 2), rounded(m["forward_lift_max"], 2)],
            "forward_top_1pct_recall_mean_pct": rounded(m["forward_top_1pct_recall_mean"], 1),
            "forward_top_1pct_recall_range_pct": [
                rounded(m["forward_top_1pct_recall_min"], 1),
                rounded(m["forward_top_1pct_recall_max"], 1),
            ],
        }

    # ============================================================
    # FACTORY
    # ============================================================

    def summary(self, at_hour=None):
        tick = self._tick(at_hour)

        entered = self._is_part & (self._start <= tick)
        finished = self._is_part & (self._end <= tick)
        known = self._qc_known(tick)

        return {
            "at_hour": self._hours(tick),
            "data_first_hour": self.first_hour,
            "data_last_hour": self.last_hour,
            "parts_entered": int(entered.sum()),
            "parts_in_production": int((entered & ~finished).sum()),
            "parts_finished": int(finished.sum()),
            "qc_results_known": int(known.sum()),
            "qc_failure_rate_pct": rounded(self._response[known].mean() * 100, 3) if known.any() else None,
            "model": self.model_card(),
            "note": REPEAT_NOTE,
        }

    def line_status(self, at_hour=None):
        tick = self._tick(at_hour)
        t = tick / TICKS_PER_UNIT

        rate, results = self._qc.failure_rate(t, LINE_WINDOW_HOURS)
        history = float(self._qc.historical_rate(t))
        ratio = rounded(rate / history, 2) if results > 0 and history > 0 else None

        # Which entry lines fed production over the last week
        recent = self._is_part & (self._start > tick - hours_to_ticks(CAMPAIGN_WINDOW_HOURS)) & (self._start <= tick)
        entered = {line: int((recent & (self._entry_line == line)).sum()) for line in ["L0", "L1"]}
        total = entered["L0"] + entered["L1"]
        l1_share = entered["L1"] / total if total else None

        if total < MIN_CAMPAIGN_PARTS:
            campaign = "too little production to tell"
        elif l1_share >= CAMPAIGN_L1_SHARE:
            campaign = "L1 campaign"
        else:
            campaign = "L0 only"

        in_production = self._is_part & (self._start <= tick) & (self._end > tick)

        return {
            "at_hour": self._hours(tick),
            "qc_results_last_72h": int(results),
            "qc_failure_rate_last_72h_pct": rounded(rate * 100, 3) if results > 0 else None,
            "qc_failure_rate_history_pct": rounded(history * 100, 3) if history > 0 else None,
            "ratio_to_history": ratio,
            "alert": bool(results >= MIN_MONITOR_PARTS and ratio is not None and ratio >= LINE_ALERT_RATIO),
            "parts_entered_last_7_days": entered,
            "l1_share_last_7_days_pct": rounded(None if l1_share is None else l1_share * 100, 1),
            "campaign": campaign,
            "parts_in_production": int(in_production.sum()),
            "note": LINE_STATUS_NOTE,
        }

    # ============================================================
    # PARTS
    # ============================================================

    def part(self, part_id, at_hour=None):
        tick = self._tick(at_hour)
        row = self._row(part_id, tick)

        finished = self._end[row] <= tick
        qc_known = self._end[row] < tick - self._delay

        batch_mates = None

        if not finished:
            # Batch-mates (parts) whose QC result was already reported
            lo = np.searchsorted(self._entry_ticks, self._start[row], "left")
            hi = np.searchsorted(self._entry_ticks, self._start[row], "right")
            mates = self._parts_by_entry[lo:hi]
            mates = mates[(mates != row) & (self._end[mates] < tick - self._delay)]
            failed = int(self._part_failed[mates].sum())

            first_failure = self._first_batch_failure[row]
            flagged = first_failure < tick

            batch_mates = {
                "flagged": bool(flagged),
                "batch_size": int(self._batch_size[row]),
                "batch_mates_failed_known": failed,
                "batch_mates_passed_known": int(len(mates)) - failed,
                "first_failure_known_hour": self._hours(first_failure) if flagged else None,
            }

        return {
            "part_id": int(self._part_id[row]),
            "at_hour": self._hours(tick),
            "status": "finished" if finished else "in production",
            "entry_line": str(self._entry_line[row]),
            "entered_hour": self._hours(self._start[row]),
            "finished_hour": self._hours(self._end[row]) if finished else None,
            "hours_in_production": self._hours(min(self._end[row], tick) - self._start[row]),
            "route_so_far": self._route(row, tick),
            "qc_result": ("failed" if self._response[row] == 1 else "passed") if qc_known else None,
            "batch_mates": batch_mates,
            # Repeat records only exist once the QC result is reported
            "twin_part_ids": self._twins(row) if qc_known else None,
            "risk": self._risk_summary(row, tick),
        }

    def part_risk(self, part_id, at_hour=None, top=10):
        tick = self._tick(at_hour)
        row = self._row(part_id, tick)

        result = {
            "part_id": int(self._part_id[row]),
            "at_hour": self._hours(tick),
            **self._risk_summary(row, tick),
            "explanation": None,
            "note": RISK_NOTE,
        }

        if not result["available"]:
            return result

        features = np.asarray(self._scoring_features[self._scoring_row[row]])
        frame = pd.DataFrame([features], columns=self.feature_names)

        contributions = self._explainer.shap_values(frame)[0]
        base = float(np.ravel(self._explainer.expected_value)[0])

        strongest = np.argsort(-np.abs(contributions), kind="stable")[:top]

        result["explanation"] = {
            "base_log_odds": rounded(base, 4),
            "log_odds": rounded(base + contributions.sum(), 4),
            "top_contributions": [
                {
                    "feature": self.feature_names[i],
                    "station": station_of(self.feature_names[i]),
                    "value": None if np.isnan(features[i]) else rounded(features[i], 3),
                    "contribution": rounded(contributions[i], 4),
                }
                for i in strongest
            ],
            "note": (
                "SHAP contributions in log-odds: positive values push toward failure. "
                "A missing value means the part skipped that station or measurement."
            ),
        }

        return result

    def inspection_queue(self, at_hour=None, hours=24, limit=50):
        """Parts that reached their last station in the last `hours`, riskiest first."""

        tick = self._tick(at_hour)
        window_start = tick - hours_to_ticks(hours)

        # Each part once: repeat records are later tests of a listed part
        just_finished = self._is_part & (self._end > window_start) & (self._end <= tick)
        rows = np.flatnonzero(just_finished & self._scorable)
        rows = rows[np.argsort(-self._risk_score[rows], kind="stable")][:limit]

        reference = np.sort(self._risk_score[self._scored(tick)])
        enough = len(reference) >= MIN_REFERENCE_PARTS

        items = []

        for row in rows:
            score = float(self._risk_score[row])
            percentile = rounded(np.searchsorted(reference, score, "left") / len(reference) * 100, 2) if enough else None

            items.append(
                {
                    "part_id": int(self._part_id[row]),
                    "entry_line": str(self._entry_line[row]),
                    "finished_hour": self._hours(self._end[row]),
                    "risk_score": rounded(score, 4),
                    "risk_percentile": percentile,
                    "top_1_percent": None if percentile is None else percentile >= TOP_RISK_PERCENTILE,
                }
            )

        cutoff = self.meta["model"]["training_cutoff_hour"]

        return {
            "at_hour": self._hours(tick),
            "window_hours": hours,
            "parts_finished_in_window": int(just_finished.sum()),
            "parts_scored_in_window": int((just_finished & self._scorable).sum()),
            "items": items,
            "note": (
                f"{RISK_NOTE} Each part is listed once; repeat test records (twin records) are "
                f"left out. The model scores parts that finished after hour {cutoff:.1f}."
            ),
        }

    def batch_alerts(self, at_hour=None, limit=100):
        """Parts in production whose entry batch-mate already failed final QC."""

        tick = self._tick(at_hour)

        in_production = self._is_part & (self._start <= tick) & (self._end > tick)
        flagged = np.flatnonzero(in_production & (self._first_batch_failure < tick))

        # Most recent flags first
        flagged = flagged[np.argsort(-self._first_batch_failure[flagged], kind="stable")]

        items = []
        for row in flagged[:limit]:
            done = np.flatnonzero(self._station_tick[row] <= tick)

            items.append(
                {
                    "part_id": int(self._part_id[row]),
                    "entry_line": str(self._entry_line[row]),
                    "entered_hour": self._hours(self._start[row]),
                    "hours_in_production": self._hours(tick - self._start[row]),
                    "first_failure_known_hour": self._hours(self._first_batch_failure[row]),
                    "hours_since_flag": self._hours(tick - self._first_batch_failure[row]),
                    "batch_size": int(self._batch_size[row]),
                    "stations_visited_so_far": int(len(done)),
                    "last_station_so_far": self.stations[done[-1]] if len(done) else None,
                }
            )

        return {
            "at_hour": self._hours(tick),
            "parts_in_production": int(in_production.sum()),
            "flagged_parts": int(len(flagged)),
            "items": items,
            "note": BATCH_ALERT_NOTE,
        }

    # ============================================================
    # STATIONS
    # ============================================================

    def station(self, station_id, at_hour=None):
        j = self._station_index.get(station_id.upper())

        if j is None:
            raise NotFound(f"Unknown station {station_id!r}; use e.g. 'L3_S32' or 'S32'.")

        tick = self._tick(at_hour)
        known = self._qc_known(tick)
        overall = self._response[known].mean() if known.any() else None

        return {"at_hour": self._hours(tick), **self._station_metrics(j, tick, known, overall)}

    def all_stations(self, at_hour=None):
        tick = self._tick(at_hour)
        known = self._qc_known(tick)
        overall = self._response[known].mean() if known.any() else None

        return {
            "at_hour": self._hours(tick),
            "stations": [self._station_metrics(j, tick, known, overall) for j in range(len(self.stations))],
        }

    def _station_metrics(self, j, tick, known, overall_rate):
        ticks = self._station_tick[:, j]

        # QC results include repeat records (known => already shown);
        # part counts and timing don't
        visited_known = (ticks <= tick) & known
        visited = self._is_part & (ticks <= tick)
        visited_finished = visited & (self._end <= tick)

        rate = self._response[visited_known].mean() if visited_known.any() else None

        def median_hours(values):
            return rounded(np.median(values) / TICKS_PER_HOUR, 1) if len(values) else None

        name = self.stations[j]

        return {
            "station": name,
            "line": line_of(name),
            "station_number": station_number(name),
            "numeric_features": self._features_per_station.get(name, 0),
            "parts_visited": int(visited.sum()),
            "qc_results_known": int(visited_known.sum()),
            "failure_rate_pct": rounded(None if rate is None else rate * 100, 3),
            "risk_lift": None if rate is None or not overall_rate else rounded(rate / overall_rate, 2),
            "median_hours_after_entry": median_hours(ticks[visited] - self._start[visited]),
            "median_hours_until_last_station": median_hours(self._end[visited_finished] - ticks[visited_finished]),
        }
