"""
PHASE 2 / STEP 1 -- understand train_date.csv

Questions answered (printed, and saved to results/):

1. Which date columns belong to which line / station?
2. Is station number the production order?
3. What does one time unit mean in real time?
4. When does each part start and finish?
5. How much time is left before final QC after each line?
6. Do failures cluster in time? (this matters for how models are evaluated)

Run from the project root:

    .venv/bin/python src/analyze_dates.py

Takes about a minute on the 500k-row sample.
"""

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from production_data import (
    HOURS_PER_UNIT,
    PLOTS_DIR,
    RESULTS_DIR,
    UNITS_PER_WEEK,
    line_of,
    load_dates,
    load_numeric,
    station_number,
    station_of,
    station_times,
)

PLOTS_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CONFIGURATION
# ============================================================

# Same rows as train_xgboost.py, so results line up with that model.
NUM_ROWS = 500_000

LINES = {
    "L0": "L0 (S0-S23)",
    "L1": "L1 (S24-S25)",
    "L2": "L2 (S26-S28)",
    "L3": "L3 (S29-S51)",
}

# Chart colors (light theme)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
BLUE = "#2a78d6"
BLUE_LIGHT = "#9ec5f4"

plt.rcParams.update({
    "figure.facecolor": SURFACE,
    "axes.facecolor": SURFACE,
    "savefig.facecolor": SURFACE,
    "axes.edgecolor": AXIS,
    "axes.labelcolor": INK_SECONDARY,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "grid.color": GRID,
    "grid.linewidth": 0.8,
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "text.color": INK,
})


# ============================================================
# 1. LOAD DATA
# ============================================================

print("Loading data...")

dates = load_dates(NUM_ROWS)
numeric = load_numeric(NUM_ROWS)

# Both files are sorted by Id, so row i is the same part in both.
assert (dates["Id"].to_numpy() == numeric["Id"].to_numpy()).all()

y = numeric["Response"].to_numpy()
baseline_rate = y.mean()

print("Parts loaded:", len(dates))
print(f"Baseline failure rate: {baseline_rate * 100:.3f}%")


# ============================================================
# 2. WHICH DATE COLUMNS BELONG TO WHICH STATION?
#
# Date columns share the L{line}_S{station} prefix of the
# numeric features, and their D-numbers interleave with the
# F-numbers, e.g. L0_S0_F0 -> L0_S0_D1. So each date column
# timestamps a group of features at one station.
# ============================================================

date_cols = [c for c in dates.columns if c.startswith("L")]
numeric_cols = [c for c in numeric.columns if c.startswith("L")]

numeric_cols_by_station = {}

for col in numeric_cols:
    numeric_cols_by_station.setdefault(station_of(col), []).append(col)

first_seen, last_seen = station_times(dates)
stations = list(first_seen.columns)
visited = first_seen.notna()

print("\n==============================")
print("DATE COLUMNS -> STATIONS")
print("==============================")

print("Date columns:", len(date_cols))
print("Stations with timestamps:", len(stations))
print(
    "Stations with timestamps but no numeric features:",
    [s for s in stations if s not in numeric_cols_by_station],
)

# Within a station, do all date columns carry the same timestamp?
print("\nStations that log more than one timestamp per visit:")

spread = last_seen - first_seen

for s in stations:
    multi = spread[s] > 0

    if multi.any():
        print(
            f"  {s}: {multi.sum() / visited[s].sum() * 100:.1f}% of visits, "
            f"median spread "
            f"{spread.loc[multi, s].median() * HOURS_PER_UNIT * 60:.0f} min"
        )

print("  (every other station: one timestamp per visit)")

# A part should have a timestamp at a station exactly when it
# has at least one numeric value there.
mismatches = 0

for s, cols in numeric_cols_by_station.items():
    has_numeric = numeric[cols].notna().any(axis=1)
    mismatches += (has_numeric != visited[s]).sum()

print(
    f"\nStation visits where date and numeric presence disagree: "
    f"{mismatches} of {visited.to_numpy().sum():,}"
)


# ============================================================
# 3. IS STATION NUMBER THE PRODUCTION ORDER?
#
# Walk each part's stations in station-number order and check
# that each one starts no earlier than the previous one ended.
#
# If this always holds, "measurements from stations <= k" is
# exactly "what was known when the part left station k" -- the
# foundation for the early-warning experiment.
# ============================================================

first = first_seen.to_numpy()
last = last_seen.to_numpy()

previous_end = np.full(len(first), -np.inf)
out_of_order = np.zeros(len(first), dtype=bool)

for j in range(len(stations)):
    here = ~np.isnan(first[:, j])

    out_of_order |= here & (first[:, j] < previous_end)

    previous_end = np.where(
        here,
        np.maximum(previous_end, last[:, j]),
        previous_end,
    )

print("\n==============================")
print("STATION ORDER")
print("==============================")

print(
    f"Parts that start a station before finishing a lower-numbered one: "
    f"{out_of_order.sum()} of {len(first):,}"
)


# ============================================================
# 4. WHAT IS ONE TIME UNIT?
#
# Timestamps are anonymized numbers (about 0 - 1718, in steps
# of 0.01). Factories have daily and weekly rhythms (shifts,
# weekends), so look for repeating cycles in activity.
# ============================================================

timestamps = first[~np.isnan(first)]

# Station visits per 0.01-unit tick
activity = np.bincount(
    np.round(timestamps * 100).astype(int)
).astype(float)
activity -= activity.mean()

n_fft = 2 ** 20
power = np.abs(np.fft.rfft(activity, n=n_fft))[1:] ** 2
period = 1 / np.fft.rfftfreq(n_fft, d=0.01)[1:]


def strongest_period(shortest, longest):
    in_range = (period >= shortest) & (period <= longest)
    return period[in_range][np.argmax(power[in_range])]


day = strongest_period(1, 5)
week = strongest_period(5, 50)

print("\n==============================")
print("TIME UNIT")
print("==============================")

print(f"Strongest short cycle: {day:.2f} units")
print(f"Strongest long cycle:  {week:.2f} units")
print(f"Ratio: {week / day:.2f} -> day and week")
print(
    f"=> 1 unit = 24 h / {day:.2f} = {24 / day:.1f} hours, "
    f"so the 0.01 resolution is {0.01 * 24 / day * 60:.0f} minutes"
)


# ============================================================
# 5. PART TIMELINE
# ============================================================

start = first_seen.min(axis=1).to_numpy()
end = last_seen.max(axis=1).to_numpy()
has_dates = ~np.isnan(start)

hours_on_line = (end - start) * HOURS_PER_UNIT

print("\n==============================")
print("PART TIMELINE")
print("==============================")

print(
    f"Parts with no timestamps: {(~has_dates).sum()} "
    f"(they have no numeric values either)"
)
print(
    f"Time on the line (first -> last station): "
    f"median {np.nanmedian(hours_on_line):.0f} h, "
    f"10th pct {np.nanpercentile(hours_on_line, 10):.0f} h, "
    f"90th pct {np.nanpercentile(hours_on_line, 90):.0f} h"
)

duration_buckets = pd.cut(
    hours_on_line,
    bins=[-1, 12, 24, 48, 168, 336, np.inf],
    labels=["< 12 h", "12-24 h", "1-2 days", "2-7 days", "1-2 weeks", "> 2 weeks"],
)

print("\nFailure rate by time on the line:")
print(
    pd.DataFrame({"bucket": duration_buckets, "failed": y})
    .groupby("bucket", observed=True)["failed"]
    .agg(Parts="size", Failure_Rate="mean")
    .assign(Failure_Rate=lambda t: t["Failure_Rate"] * 100)
    .to_string(float_format=lambda v: f"{v:.2f}")
)


# ============================================================
# 6. STATION TIMING TABLE
#
# For every station: who visits it, how risky it is, and WHEN
# it happens in a part's life.
# ============================================================

station_rows = []

for j, s in enumerate(stations):
    here = ~np.isnan(first[:, j])

    if not here.any():
        continue

    station_rows.append(
        {
            "Station": s,
            "Numeric Features": len(numeric_cols_by_station.get(s, [])),
            "Parts Visited": int(here.sum()),
            "Failure Rate (%)": y[here].mean() * 100,
            "Risk Lift": y[here].mean() / baseline_rate,
            "Median Hours Since Start": np.median(first[here, j] - start[here]) * HOURS_PER_UNIT,
            "Median Hours Until Last Station": np.median(end[here] - last[here, j]) * HOURS_PER_UNIT,
        }
    )

station_timing = pd.DataFrame(station_rows)

print("\n==============================")
print("STATION TIMING")
print("==============================")

print(station_timing.to_string(index=False, float_format=lambda v: f"{v:.2f}"))

station_timing.to_csv(
    RESULTS_DIR / "station_timing.csv",
    index=False,
)


# ============================================================
# 7. ROUTE FAMILIES
#
# Parts enter at L0 or L1, may pass through L2, and finish on
# one of two L3 paths.
# ============================================================

station_nums = np.array([station_number(s) for s in stations])


def visited_any(lowest, highest):
    cols = (station_nums >= lowest) & (station_nums <= highest)
    return visited.to_numpy()[:, cols].any(axis=1)


first_station = np.where(
    has_dates,
    np.argmax(visited.to_numpy(), axis=1),
    -1,
)

routes = pd.DataFrame(
    {
        "Entry Line": [line_of(stations[j]) if j >= 0 else "none" for j in first_station],
        "Through L2": visited_any(26, 28),
        "L3 Path": np.select(
            [visited_any(29, 38), visited_any(39, 51)],
            ["S29-S38", "S39-S51"],
            default="none",
        ),
        "failed": y,
    }
)

route_summary = (
    routes
    .groupby(["Entry Line", "Through L2", "L3 Path"])["failed"]
    .agg(Parts="size", Failure_Rate="mean")
    .assign(Failure_Rate=lambda t: t["Failure_Rate"] * 100)
    .sort_values("Parts", ascending=False)
    .reset_index()
)

print("\n==============================")
print("ROUTE FAMILIES")
print("==============================")

print(route_summary.head(10).to_string(index=False, float_format=lambda v: f"{v:.2f}"))

route_summary.to_csv(
    RESULTS_DIR / "route_families.csv",
    index=False,
)


# ============================================================
# 8. HOW MUCH TIME IS LEFT AFTER EACH LINE?
#
# Measured from a part's first timestamp on a line until its
# last station overall. This is the lead time an alert based
# on that line's measurements could give.
# ============================================================

hours_left_by_line = {}

for line in LINES:
    cols = [s for s in stations if line_of(s) == line]
    line_start = first_seen[cols].min(axis=1).to_numpy()
    here = ~np.isnan(line_start)

    hours_left_by_line[line] = (end[here] - line_start[here]) * HOURS_PER_UNIT

print("\n==============================")
print("TIME LEFT AFTER EACH LINE")
print("==============================")

for line, hours in hours_left_by_line.items():
    print(
        f"{LINES[line]:14} parts {len(hours):8,} | hours left: "
        f"median {np.median(hours):6.1f}, "
        f"middle 50% {np.percentile(hours, 25):6.1f} - {np.percentile(hours, 75):6.1f}"
    )

# What share of a part's numeric measurements is taken before L3?
present = numeric[numeric_cols].notna()
pre_l3_cols = [c for c in numeric_cols if line_of(station_of(c)) != "L3"]

share_before_l3 = (
    present[pre_l3_cols].sum(axis=1)
    / present.sum(axis=1).replace(0, np.nan)
)

print(
    f"\nShare of a part's numeric measurements taken before L3: "
    f"median {share_before_l3.median() * 100:.0f}%, "
    f"mean {share_before_l3.mean() * 100:.0f}%"
)

def format_hours(hours):
    if hours < 1:
        return f"{hours * 60:.0f} min"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} days"


# Plot: dot = median, bar = middle 50%, thin line = 10th-90th pct
fig, ax = plt.subplots(figsize=(9, 3.8))

floor = 0.05  # 3 minutes; keeps zero-length gaps on the log axis

for row, (line, hours) in enumerate(hours_left_by_line.items()):
    hours = np.maximum(hours, floor)
    p10, p25, p50, p75, p90 = np.percentile(hours, [10, 25, 50, 75, 90])

    ax.plot([p10, p90], [row, row], color=AXIS, linewidth=1.5, solid_capstyle="round", zorder=1)
    ax.plot([p25, p75], [row, row], color=BLUE_LIGHT, linewidth=6, solid_capstyle="round", zorder=2)
    ax.plot(p50, row, "o", color=BLUE, markersize=9, markeredgecolor=SURFACE, markeredgewidth=2, zorder=3)

    ax.annotate(
        format_hours(p50),
        (p50, row),
        xytext=(0, 11),
        textcoords="offset points",
        ha="center",
        fontsize=9,
        color=INK,
    )

ax.set_yticks(range(len(LINES)))
ax.set_yticklabels(
    [f"{LINES[line]}\n{len(h):,} parts" for line, h in hours_left_by_line.items()],
    color=INK_SECONDARY,
    fontsize=9,
)
ax.invert_yaxis()
ax.set_ylim(len(LINES) - 0.4, -0.7)

ax.set_xscale("log")
ax.set_xticks([0.1, 1, 10, 24, 168, 720])
ax.set_xticklabels(["6 min", "1 h", "10 h", "1 day", "1 week", "30 days"])
ax.set_xlabel("Time from the line's first measurement to the part's last station")
ax.grid(axis="x")
ax.tick_params(axis="y", length=0)
ax.spines["left"].set_visible(False)

ax.set_title(
    "How much time is left after each production line?",
    loc="left",
    fontsize=12,
    fontweight="bold",
    pad=22,
)
ax.text(
    0, 1.04,
    "Dot = median, bar = middle 50% of parts, line = 10th-90th percentile",
    transform=ax.transAxes,
    fontsize=9,
    color=INK_SECONDARY,
)

plt.tight_layout()
plt.savefig(
    PLOTS_DIR / "time_left_by_line.png",
    dpi=200,
    bbox_inches="tight",
)
plt.close()


# ============================================================
# 9. DO FAILURES CLUSTER IN TIME?
#
# If failures arrive in bursts, a random train/test split puts
# parts from the same bad week on both sides of the split, and
# the model can score well by recognising the week rather than
# the defect. early_warning.py tests this directly.
# ============================================================

ids = numeric["Id"].to_numpy()

# Order parts by when they entered production (Id breaks ties)
in_time_order = np.lexsort((ids[has_dates], start[has_dates]))
y_in_time_order = y[has_dates][in_time_order]

after_failure = y_in_time_order[1:][y_in_time_order[:-1] == 1].mean()

weekly = (
    pd.DataFrame(
        {
            "Week": np.floor(start[has_dates] / UNITS_PER_WEEK).astype(int),
            "failed": y[has_dates],
        }
    )
    .groupby("Week")["failed"]
    .agg(Parts="size", Failure_Rate="mean")
)
weekly["Failure_Rate"] *= 100
busy_weeks = weekly[weekly["Parts"] >= 1000]

print("\n==============================")
print("DO FAILURES CLUSTER IN TIME?")
print("==============================")

print(f"P(fail):                                          {baseline_rate * 100:.2f}%")
print(f"P(fail | the part started just before it failed): {after_failure * 100:.2f}%")
print(
    f"Weekly failure rate across {len(busy_weeks)} weeks with >= 1,000 parts: "
    f"min {busy_weeks['Failure_Rate'].min():.2f}%, "
    f"median {busy_weeks['Failure_Rate'].median():.2f}%, "
    f"max {busy_weeks['Failure_Rate'].max():.2f}%"
)

weekly.to_csv(RESULTS_DIR / "weekly_failure_rate.csv")

# Plot weekly failure rate (gaps where a week had < 1,000 parts)
all_weeks = np.arange(weekly.index.min(), weekly.index.max() + 1)
rate = busy_weeks["Failure_Rate"].reindex(all_weeks)

fig, ax = plt.subplots(figsize=(10, 3.8))

ax.plot(all_weeks, rate, color=BLUE, linewidth=2, solid_joinstyle="round")
ax.axhline(baseline_rate * 100, color=MUTED, linewidth=1)
ax.annotate(
    f"overall {baseline_rate * 100:.2f}%",
    (all_weeks[-1], baseline_rate * 100),
    xytext=(4, 4),
    textcoords="offset points",
    fontsize=9,
    color=INK_SECONDARY,
)

ax.set_xlabel("Production week (parts grouped by the week they entered the line)")
ax.set_ylabel("Failure rate (%)")
ax.set_ylim(0, None)
ax.grid(axis="y")

ax.set_title(
    "Failures come in bursts",
    loc="left",
    fontsize=12,
    fontweight="bold",
    pad=22,
)
ax.text(
    0, 1.04,
    f"Weekly failure rate ranges from {busy_weeks['Failure_Rate'].min():.2f}% to "
    f"{busy_weeks['Failure_Rate'].max():.2f}% (weeks with >= 1,000 parts)",
    transform=ax.transAxes,
    fontsize=9,
    color=INK_SECONDARY,
)

plt.tight_layout()
plt.savefig(
    PLOTS_DIR / "weekly_failure_rate.png",
    dpi=200,
    bbox_inches="tight",
)
plt.close()

print("\nSaved:")
print("  results/station_timing.csv")
print("  results/route_families.csv")
print("  results/weekly_failure_rate.csv")
print("  results/plots/time_left_by_line.png")
print("  results/plots/weekly_failure_rate.png")
