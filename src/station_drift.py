"""
STATION DRIFT -- do a station's measurements shift when failures rise?

Failure regimes are line-wide (campaign_analysis.py), and no single part's
early measurements predict its own failure forward in time
(early_warning.py). This asks a different, station-level question: on a
given day, have the measurements a station records shifted against its own
recent past, and do the parts that passed it on such days fail more often?

Drift score. For every measurement and day, the mean of the values recorded
that day is compared with the previous 28 days: z = (today's mean - the
28-day mean) / the 28-day spread of daily means. It is computed within each
product (route family, plant_names.py), so a change in product mix, such as
an L1 campaign, isn't drift. A station's score for the day is the weighted
root mean square of its z values over measurements and products. Only
measurements recorded by the end of the day are used, so the score is known
then; the parts' QC results come later.

Each part is counted once (repeat tests left out). Discovery is weeks 0-61
and validation weeks 62 and later, the split the digital twin uses: a
station is picked and a threshold set on discovery, then checked on
validation.

Run from the project root (after build_serving_data.py):

    .venv/bin/python src/station_drift.py

The first run reads train_numeric.csv (about 2 minutes) and caches daily
summaries in serving/drift/.
"""

import json

import matplotlib
matplotlib.use("Agg")

import numpy as np
import pandas as pd

from plant_names import PRODUCTS, product_index
from production_data import DATA_DIR, HOURS_PER_UNIT, PROJECT_ROOT, load_repeat_tests

SERVING_DIR = PROJECT_ROOT / "serving"
CACHE = SERVING_DIR / "drift" / "daily.npz"

DAY_HOURS = 24
REFERENCE_DAYS = 28      # the trailing window a day is compared with
MIN_REFERENCE_DAYS = 10  # days in that window with enough values
MIN_VALUES = 30          # values a (measurement, product, day) needs to count
Z_CLIP = 10              # one measurement with a tiny spread can't dominate
DISCOVERY_WEEKS = 62     # weeks 0-61 discovery, 62+ validation


# ============================================================
# DAILY SUMMARIES (cached)
# ============================================================

def build_daily():
    """Per measurement, product and day (of the station's time): count, sum
    and sum of squares, for all parts and for parts that later passed QC;
    per station, product and day: parts and failures."""

    meta = json.loads((SERVING_DIR / "meta.json").read_text())
    stations = meta["stations"]
    features = meta["feature_names"]

    parts = pd.read_parquet(
        SERVING_DIR / "parts.parquet",
        columns=["part_id", "entry_line", "through_l2", "l3_path", "response", "end"],
    )
    first_seen = np.load(SERVING_DIR / "station_first_seen.npy")

    once = ~np.isin(parts["part_id"].to_numpy(), load_repeat_tests())
    product = product_index(parts["entry_line"].to_numpy(), parts["through_l2"].to_numpy(), parts["l3_path"].to_numpy())
    failed = parts["response"].to_numpy() == 1

    hours = first_seen * HOURS_PER_UNIT
    days = int(np.nanmax(hours) // DAY_HOURS) + 1
    n_products = len(PRODUCTS)

    def key(station_hours, rows):
        day = np.floor(station_hours / DAY_HOURS).astype(np.int64)
        return day * n_products + product[rows]

    print("Reading train_numeric.csv ...")
    numeric = pd.read_csv(
        DATA_DIR / "train_numeric.csv", usecols=features, dtype={f: np.float32 for f in features}
    )[features].to_numpy()

    size = days * n_products
    shape = (len(features), days, n_products)
    stats = {name: np.zeros(shape) for name in ["n", "sum", "sumsq", "n_pass", "sum_pass", "sumsq_pass"]}

    station_index = {s: i for i, s in enumerate(stations)}
    for f, feature in enumerate(features):
        t = hours[:, station_index[feature.rsplit("_", 1)[0]]]
        x = numeric[:, f]
        ok = once & ~np.isnan(x) & ~np.isnan(t)
        for suffix, rows in [("", ok), ("_pass", ok & ~failed)]:
            k = key(t[rows], rows)
            v = x[rows].astype(np.float64)
            stats["n" + suffix][f] = np.bincount(k, minlength=size).reshape(days, n_products)
            stats["sum" + suffix][f] = np.bincount(k, weights=v, minlength=size).reshape(days, n_products)
            stats["sumsq" + suffix][f] = np.bincount(k, weights=v * v, minlength=size).reshape(days, n_products)

    # Parts passing each station, and how many of them failed QC
    visits = np.zeros((len(stations), days, n_products))
    failures = np.zeros((len(stations), days, n_products))
    for s in range(len(stations)):
        rows = once & ~np.isnan(hours[:, s])
        k = key(hours[rows, s], rows)
        visits[s] = np.bincount(k, minlength=size).reshape(days, n_products)
        failures[s] = np.bincount(k, weights=failed[rows], minlength=size).reshape(days, n_products)

    # The line: parts finishing each day (QC known an hour later) and failures
    end_day = np.floor(parts["end"].to_numpy() * HOURS_PER_UNIT / DAY_HOURS)
    rows = once & ~np.isnan(end_day)
    finished = np.bincount(end_day[rows].astype(np.int64), minlength=days)[:days]
    finished_failed = np.bincount(end_day[rows].astype(np.int64), weights=failed[rows], minlength=days)[:days]

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(
        CACHE, stations=np.array(stations), features=np.array(features), visits=visits, failures=failures,
        finished=finished, finished_failed=finished_failed, **stats,
    )
    print(f"Cached {CACHE.relative_to(PROJECT_ROOT)}")


def load_daily():
    if not CACHE.exists():
        build_daily()
    return dict(np.load(CACHE))


# ============================================================
# DRIFT SCORES
# ============================================================

def drift_z(n, total):
    """z of each (measurement, day, product) mean against the trailing
    REFERENCE_DAYS days' daily means; NaN where there's too little data."""

    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(n >= MIN_VALUES, total / n, np.nan)

    features, days, products = mean.shape
    flat = pd.DataFrame(mean.transpose(1, 0, 2).reshape(days, features * products))

    # Shifted by a day: the reference never includes the day itself
    window = flat.rolling(REFERENCE_DAYS, min_periods=MIN_REFERENCE_DAYS)
    reference_mean = window.mean().shift(1).to_numpy()
    reference_sd = window.std().shift(1).to_numpy()

    with np.errstate(invalid="ignore", divide="ignore"):
        z = (flat.to_numpy() - reference_mean) / reference_sd
    z[~np.isfinite(z)] = np.nan
    z = np.clip(z, -Z_CLIP, Z_CLIP)

    return z.reshape(days, features, products).transpose(1, 0, 2)


def station_scores(daily, z, counts, how="rms"):
    """Per station and day: the weighted root mean square of its z values,
    or (how="max") the largest |z| among them."""

    feature_station = np.array([f.rsplit("_", 1)[0] for f in daily["features"]])
    scores = np.full((len(daily["stations"]), z.shape[1]), np.nan)

    for s, station in enumerate(daily["stations"]):
        rows = feature_station == station
        if not rows.any():
            continue
        zs = z[rows]
        with np.errstate(invalid="ignore", divide="ignore"):
            if how == "max":
                scores[s] = np.nanmax(np.abs(zs), axis=(0, 2)) if np.isfinite(zs).any() else np.nan
            else:
                w = np.where(np.isnan(zs), 0, counts[rows])
                scores[s] = np.sqrt(np.nansum(w * zs**2, axis=(0, 2)) / w.sum(axis=(0, 2)))
    return scores


def coverage_z(daily):
    """The same z, for the share of a station's parts that have each
    measurement: drift in what is measured rather than in the values."""

    station_row = {s: i for i, s in enumerate(daily["stations"])}
    visits = np.stack([daily["visits"][station_row[f.rsplit("_", 1)[0]]] for f in daily["features"]])
    return drift_z(visits, daily["n"]), visits


def all_scores(daily):
    """The drift scores compared below, named before any check was run."""

    z = drift_z(daily["n"], daily["sum"])
    z_pass = drift_z(daily["n_pass"], daily["sum_pass"])
    z_cov, visits = coverage_z(daily)

    return {
        "rms": station_scores(daily, z, daily["n"]),
        "max": station_scores(daily, z, daily["n"], how="max"),
        "coverage": station_scores(daily, z_cov, visits),
        # The same as rms, from parts that later passed QC: drift that isn't
        # just the failing parts' own measurements
        "rms_passing": station_scores(daily, z_pass, daily["n_pass"]),
    }


# ============================================================
# CHECKS
# ============================================================

FLAG_PERCENTILE = 90     # a station's top 10% of discovery days are its threshold
MIN_PERIOD_DAYS = 30     # scored days a station needs in each period
MIN_WEEKS = 12           # weeks a correlation needs
MIN_WEEK_PARTS = 500     # parts a weekly failure rate needs
MIN_WEEK_VALUES = 200    # values a weekly measurement mean needs


def spearman(a, b, min_n=MIN_WEEKS):
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < min_n or np.ptp(a[ok]) == 0 or np.ptp(b[ok]) == 0:
        return np.nan
    return pd.Series(a[ok]).corr(pd.Series(b[ok]), method="spearman")


def weekly(values, day_week, weeks, how="mean"):
    grouped = pd.Series(values).groupby(day_week)
    return (grouped.mean() if how == "mean" else grouped.sum()).reindex(range(weeks)).to_numpy()


def flag_check(scores, daily, discovery_days):
    """Flag a station's days at or above its discovery threshold. Lift: the
    failure rate of parts that passed it on flagged days, over the rate of
    all parts that passed it on scored days, in each period."""

    visits = daily["visits"].sum(axis=2)
    failures = daily["failures"].sum(axis=2)
    rows = []

    for s, station in enumerate(daily["stations"]):
        score = scores[s]
        scored = np.isfinite(score)
        if min((scored & discovery_days).sum(), (scored & ~discovery_days).sum()) < MIN_PERIOD_DAYS:
            continue

        threshold = np.percentile(score[scored & discovery_days], FLAG_PERCENTILE)
        row = {"station": station, "threshold": round(threshold, 3)}

        for name, period in [("discovery", discovery_days), ("validation", ~discovery_days)]:
            days = scored & period
            flagged = days & (score >= threshold)
            parts, fails = visits[s, days].sum(), failures[s, days].sum()
            flagged_parts, flagged_fails = visits[s, flagged].sum(), failures[s, flagged].sum()
            row[f"{name}_days"] = int(days.sum())
            row[f"{name}_flagged_parts_pct"] = round(100 * flagged_parts / parts, 1) if parts else np.nan
            row[f"{name}_flagged_failures"] = int(flagged_fails)
            row[f"{name}_lift"] = (
                round((flagged_fails / flagged_parts) / (fails / parts), 2) if flagged_parts and fails else np.nan
            )
        rows.append(row)

    return pd.DataFrame(rows)


def regime_check(scores, daily, day_week, weeks, discovery_weeks):
    """Spearman correlation of a station's weekly mean drift with the line's
    weekly QC failure rate (parts finishing that week), in each period."""

    finished = weekly(daily["finished"], day_week, weeks, "sum")
    rate = np.where(finished >= MIN_WEEK_PARTS, weekly(daily["finished_failed"], day_week, weeks, "sum") / np.maximum(finished, 1), np.nan)

    rows = []
    for s, station in enumerate(daily["stations"]):
        week_score = weekly(scores[s], day_week, weeks)
        rows.append({
            "station": station,
            "discovery_weekly_corr": spearman(week_score[discovery_weeks], rate[discovery_weeks]),
            "validation_weekly_corr": spearman(week_score[~discovery_weeks], rate[~discovery_weeks]),
        })
    return pd.DataFrame(rows)


def measurement_check(daily, day_week, weeks, product=0):
    """For each measurement, within one product (Standard ECU by default):
    Spearman correlation of its week-to-week change in mean with the change
    in the failure rate of the parts passing its station. Changes, not
    levels, so slow trends in both don't count."""

    station_row = {s: i for i, s in enumerate(daily["stations"])}
    week_number = np.arange(1, weeks)
    rows = []

    for f, feature in enumerate(daily["features"]):
        s = station_row[feature.rsplit("_", 1)[0]]
        n = weekly(daily["n"][f, :, product], day_week, weeks, "sum")
        mean = np.where(n >= MIN_WEEK_VALUES, weekly(daily["sum"][f, :, product], day_week, weeks, "sum") / np.maximum(n, 1), np.nan)
        parts = weekly(daily["visits"][s, :, product], day_week, weeks, "sum")
        rate = np.where(parts >= MIN_WEEK_PARTS, weekly(daily["failures"][s, :, product], day_week, weeks, "sum") / np.maximum(parts, 1), np.nan)
        change_mean, change_rate = np.diff(mean), np.diff(rate)
        rows.append({
            "measurement": feature,
            "discovery_corr": spearman(change_mean[week_number < DISCOVERY_WEEKS], change_rate[week_number < DISCOVERY_WEEKS], 15),
            "validation_corr": spearman(change_mean[week_number >= DISCOVERY_WEEKS], change_rate[week_number >= DISCOVERY_WEEKS], 15),
        })
    return pd.DataFrame(rows).dropna().reset_index(drop=True)


def agreement(a, b):
    """Do stations (or measurements) that look strong on discovery look strong on validation?"""

    return pd.Series(a).corr(pd.Series(b), method="spearman")


def permutation_null(a, b, trials=1000, seed=0):
    rng = np.random.default_rng(seed)
    a, b = np.asarray(a), np.asarray(b)
    return np.percentile([agreement(a, rng.permutation(b)) for _ in range(trials)], [2.5, 97.5])


# ============================================================
# PLOT
# ============================================================

LINE_COLORS = {"L0": "#2a78d6", "L1": "#eb6834", "L2": "#1baf7a", "L3": "#eda100"}  # the dashboard's palette


def plot(flags, measurements, path):
    import matplotlib.pyplot as plt

    fig, (left, right) = plt.subplots(1, 2, figsize=(12, 5.2))

    for line, color in LINE_COLORS.items():
        rows = flags[flags["station"].str.startswith(line)].dropna(subset=["discovery_lift", "validation_lift"])
        if rows.empty:
            continue
        left.scatter(rows["discovery_lift"], rows["validation_lift"], s=46, color=color, edgecolor="white", linewidth=1, label=line, zorder=3)
    top = flags.nlargest(3, "discovery_lift")
    for _, row in top.iterrows():
        left.annotate(row["station"], (row["discovery_lift"], row["validation_lift"]), xytext=(6, 4), textcoords="offset points", fontsize=9, color="#52514e")
    for ax in (left,):
        ax.axhline(1, color="#c3c2b7", lw=1, zorder=1)
        ax.axvline(1, color="#c3c2b7", lw=1, zorder=1)
    left.set_xlabel("Discovery lift (weeks 0-61)")
    left.set_ylabel("Validation lift (weeks 62+)")
    left.set_title("Parts passing a station on its top-10% drift days:\nfailure rate vs the station's average", fontsize=11, loc="left")
    left.legend(title="Line", frameon=False, loc="upper right")

    right.scatter(measurements["discovery_corr"], measurements["validation_corr"], s=14, color="#2a78d6", alpha=0.55, edgecolor="none", zorder=3)
    right.axhline(0, color="#c3c2b7", lw=1, zorder=1)
    right.axvline(0, color="#c3c2b7", lw=1, zorder=1)
    right.set_xlabel("Discovery correlation (weeks 0-61)")
    right.set_ylabel("Validation correlation (weeks 62+)")
    right.set_title("Each measurement: weekly change in mean vs\nchange in failure rate (Standard ECU)", fontsize=11, loc="left")

    for ax in (left, right):
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(color="#e1e0d9", lw=0.6, zorder=0)

    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


# ============================================================
# MAIN
# ============================================================

def main():
    from production_data import PLOTS_DIR, RESULTS_DIR

    daily = load_daily()
    days = daily["n"].shape[1]
    day_week = np.arange(days) * DAY_HOURS // 168
    weeks = int(day_week.max()) + 1
    discovery_days = day_week < DISCOVERY_WEEKS
    discovery_weeks = np.arange(weeks) < DISCOVERY_WEEKS

    print(f"{days} days, {weeks} weeks; discovery weeks 0-{DISCOVERY_WEEKS - 1}, validation {DISCOVERY_WEEKS}-{weeks - 1}\n")

    tables = []
    for name, scores in all_scores(daily).items():
        flags = flag_check(scores, daily, discovery_days)
        regime = regime_check(scores, daily, day_week, weeks, discovery_weeks)
        table = flags.merge(regime, on="station", how="left")
        table.insert(0, "score", name)
        tables.append(table)

        top = table.nlargest(5, "discovery_lift")
        lifts = table.dropna(subset=["discovery_lift", "validation_lift"])
        weekly_ok = table.dropna(subset=["discovery_weekly_corr", "validation_weekly_corr"])
        print(f"[{name}] {len(table)} stations with {MIN_PERIOD_DAYS}+ scored days in each period")
        print(f"  flag lift, discovery vs validation agreement: {agreement(lifts['discovery_lift'], lifts['validation_lift']):+.2f}"
              f" (shuffled 95%: {permutation_null(lifts['discovery_lift'], lifts['validation_lift'])[0]:+.2f} to {permutation_null(lifts['discovery_lift'], lifts['validation_lift'])[1]:+.2f})")
        print(f"  validation lift: median {lifts['validation_lift'].median():.2f}; above 1.2 at {int((lifts['validation_lift'] > 1.2).sum())} of {len(lifts)} stations")
        print("  top 5 on discovery -> validation: " + ", ".join(f"{r.station} {r.discovery_lift:.2f}->{r.validation_lift:.2f}" for r in top.itertuples()))
        print(f"  weekly drift vs line failure rate, agreement: {agreement(weekly_ok['discovery_weekly_corr'], weekly_ok['validation_weekly_corr']):+.2f} over {len(weekly_ok)} stations\n")

    stations = pd.concat(tables, ignore_index=True)
    stations.to_csv(RESULTS_DIR / "station_drift.csv", index=False)

    measurements = measurement_check(daily, day_week, weeks)
    measurements.to_csv(RESULTS_DIR / "station_drift_measurements.csv", index=False)
    low, high = permutation_null(measurements["discovery_corr"], measurements["validation_corr"])
    strongest = measurements.reindex(measurements["discovery_corr"].abs().sort_values(ascending=False).index).head(20)
    kept = int((np.sign(strongest["discovery_corr"]) == np.sign(strongest["validation_corr"])).sum())
    print(f"[measurements] {len(measurements)} with enough weeks (Standard ECU)")
    print(f"  discovery vs validation agreement: {agreement(measurements['discovery_corr'], measurements['validation_corr']):+.2f} (shuffled 95%: {low:+.2f} to {high:+.2f})")
    print(f"  the 20 strongest on discovery keep their sign on validation: {kept} of 20")

    plot(stations[stations["score"] == "rms"], measurements, PLOTS_DIR / "station_drift.png")
    print(f"\nWrote results/station_drift.csv, results/station_drift_measurements.csv, results/plots/station_drift.png")


if __name__ == "__main__":
    main()
