"""
LINE MAP -- where the parts in production are at a given moment

Places every part in production at a tick: at the latest station it has
reached, or in the queue for line 3 once it has finished every station
before line 3 on its route and hasn't started line 3 yet. Positions come
from timestamps; the data has no physical layout, so the map is a
schematic in production order.

Shared by the real data (factory_service.py) and the digital twin's saved
runs (TwinRuns, written by twin_scenarios.py to serving/twin/).
"""

import json

import numpy as np

from production_data import PROJECT_ROOT, line_of, station_number
from qc_monitor import TICKS_PER_HOUR

TWIN_DIR = PROJECT_ROOT / "serving" / "twin"

NOT_VISITED = np.iinfo(np.int32).max
NEVER = np.iinfo(np.int64).max
HOUR = int(TICKS_PER_HOUR)
QC_DELAY = HOUR      # QC results are reported 1 hour after a part's last station

MAP_NOTE = (
    "Positions come from timestamps: each part in production is at the latest station it has "
    "reached, or waiting for line 3 once it has finished every station before line 3 on its route. "
    "L1 parts spend days at L1_S24/L1_S25. The data has no physical layout, so this is a schematic "
    "in production order."
)


def snapshot(tick, stations, start, end, l3_start, pre_done, entry_line, seen, failed):
    """
    Where the parts in production are at `tick`.

    start, end, l3_start, pre_done   int64 ticks per part (pre_done: last
                                     station before line 3, or entry)
    entry_line                       "L0".."L3" per part
    seen                             int32 (parts, stations): first tick at
                                     each station, NOT_VISITED if never
    failed                           bool per part: failed final QC
    """

    in_production = np.flatnonzero((start <= tick) & (end > tick))
    lines = entry_line[in_production]

    l3_numbers = np.array([station_number(s) >= 29 for s in stations])
    rows = seen[in_production].astype(np.int64)
    reached = np.where(rows <= tick, rows, -1)

    on_line3 = l3_start[in_production] <= tick
    waiting = ~on_line3 & (pre_done[in_production] <= tick)
    upstream = ~on_line3 & ~waiting

    # Latest station reached: on line 3 among its stations, otherwise among the earlier ones
    latest = np.full(len(in_production), -1)
    for mask, cols in ((on_line3, l3_numbers), (upstream, ~l3_numbers)):
        block = np.where(cols[None, :], reached[mask], -1)
        latest[mask] = np.where(block.max(axis=1) >= 0, block.argmax(axis=1), -1)

    placed = latest >= 0
    per_station = np.bincount(latest[placed], minlength=len(stations))

    started = (l3_start > tick - HOUR) & (l3_start <= tick)
    started_by_line = count_by_line(entry_line, started)
    serving = "off" if not started.any() else max(started_by_line, key=started_by_line.get)

    # A QC result is reported from the tick after end + QC_DELAY (factory_service._qc_known)
    known_at = end + QC_DELAY + 1
    reported = (known_at > tick - 24 * HOUR) & (known_at <= tick)
    queue = count_by_line(lines, waiting)

    return {
        "at_hour": tick / TICKS_PER_HOUR,
        "in_production": int(len(in_production)),
        "stations": [
            {"station": s, "line": line_of(s), "parts": int(per_station[j])} for j, s in enumerate(stations)
        ],
        "waiting_for_line3": {**queue, "total": int(waiting.sum())},
        "line3_serving": serving,
        "line3_started_last_hour": started_by_line,
        "entered_last_hour": count_by_line(entry_line, (start > tick - HOUR) & (start <= tick)),
        "finished_last_hour": int(((end > tick - HOUR) & (end <= tick)).sum()),
        "qc_reported_last_24h": int(reported.sum()),
        "qc_failed_last_24h": int((reported & failed).sum()),
        "note": MAP_NOTE,
    }


def count_by_line(entry_line, mask):
    """How many of the masked parts entered on L0, on L1, or elsewhere."""

    values, counts = np.unique(entry_line[mask], return_counts=True)
    out = {"L0": 0, "L1": 0, "other": 0}
    for value, count in zip(values, counts):
        out[value if value in ("L0", "L1") else "other"] += int(count)
    return out


# ============================================================
# THE TWIN'S SAVED RUNS
# ============================================================

class TwinRuns:
    """Saved digital-twin runs (serving/twin/), loaded on first use."""

    def __init__(self, directory=TWIN_DIR):
        self.directory = directory
        index = directory / "index.json"
        self.scenarios = json.loads(index.read_text()) if index.exists() else []
        self._loaded = {}

    def get(self, scenario_id):
        info = next((s for s in self.scenarios if s["id"] == scenario_id), None)
        if info is None:
            return None, None
        if scenario_id not in self._loaded:
            with np.load(self.directory / f"{scenario_id}.npz") as f:
                run = {key: f[key] for key in f.files}
            run["seen"] = np.load(self.directory / f"{scenario_id}_seen.npy", mmap_mode="r")
            run["entry_line"] = np.array(["L0", "L1", "L2", "L3"])[run["line"]]
            self._loaded[scenario_id] = run
        return info, self._loaded[scenario_id]

    def snapshot(self, scenario_id, at_hour, stations):
        info, run = self.get(scenario_id)
        if run is None:
            return None, None
        hour = info["last_hour"] if at_hour is None else at_hour
        tick = int(round(hour * TICKS_PER_HOUR))
        failed = (run["response"] > 0) | (run["repeats_failed"] > 0)
        out = snapshot(tick, stations, run["start"], run["end"], run["l3_start"], run["ready"],
                       run["entry_line"], run["seen"], failed)
        return info, out
