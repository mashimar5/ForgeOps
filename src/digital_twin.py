"""
DIGITAL TWIN -- a flow simulation of the four production lines

A simulator calibrated on the real data. It makes parts the way the
factory does:

    arrivals   while an entry line runs, a batch of parts enters every
               6-minute tick. Whole weeks of real arrivals are copied,
               chosen by which entry lines ran that week.
    routes     each simulated batch copies a real batch: its parts' routes,
               their time on lines 0-2 and their time inside line 3.
    line 3     the shared bottleneck: two queues (L1 parts, everything
               else). Each week line 3 plans enough running hours, at its
               usual rate (~144 parts an hour), for the parts that will
               become ready plus half the gap between its backlog and its
               usual level; the real line's weekly output follows its
               hours (correlation 0.96). Hours serve one line: L0 work, until the oldest
               L1 part has waited long enough for an L1 block that clears
               the L1 queue. Waits come out of the queues, so they respond
               to load instead of being copied.
    QC         log-odds of failing = a route effect (logistic model on the
               stations visited and the entry line) + a batch effect +
               line-3 conditions that change by the hour and drift by the
               day. The random effects are tuned so simulated failures
               cluster like the real ones.
    repeats    repeat tests follow at the observed rates (they are much
               more likely after a failed test).

Flow only: no measurements, so simulated parts get no risk score. The
twin reproduces how parts move and how often they fail, not why: its
failure model is built from associations.

All times are 6-minute ticks (see qc_monitor.py). twin_validate.py
calibrates on the first 60% of the timeline and checks the rest.

    from digital_twin import load_history, calibrate, fit_line3_policy, real_weeks, simulate, waiting_at
    h = load_history()                          # builds serving/twin_history.npz once (~20 s)
    cal = calibrate(h, until_week=62)           # ~20 s
    fit_line3_policy(cal, h, 1, 62)             # ~10 s
    run = simulate(cal, real_weeks(h, 50, 102), 50, waiting=waiting_at(h, 50))
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.optimize import minimize
from scipy.signal import lfilter
from sklearn.linear_model import LogisticRegression

from production_data import DATA_DIR, PROJECT_ROOT, station_number
from qc_monitor import NEVER, TICKS_PER_HOUR, hours_to_ticks, to_ticks


SERVING_DIR = PROJECT_ROOT / "serving"
HISTORY_PATH = SERVING_DIR / "twin_history.npz"

LINES = ["L0", "L1", "L2", "L3"]
NOT_VISITED = np.iinfo(np.int32).max

TICKS_PER_HOUR_INT = int(TICKS_PER_HOUR)       # 10
HOURS_PER_WEEK = 168
TICKS_PER_WEEK = HOURS_PER_WEEK * TICKS_PER_HOUR_INT
QC_DELAY = hours_to_ticks(1)                   # QC results are known 1 hour after the last station

# A line counts as running in a week if it fed at least this share of the
# week's entries (campaign_analysis.py uses 10% for L1)
ACTIVE_SHARE = 0.10

# Line 3 is busy (so its starts per hour measure capacity) above this backlog
BUSY_BACKLOG = 1_000


# ============================================================
# REAL HISTORY
# ============================================================

@dataclass
class History:
    """
    Every real part once (its first test), sorted by entry, in ticks.
    Built from the API's serving files plus the L1 sub-step timestamps.
    """

    part_id: np.ndarray       # int64
    line: np.ndarray          # int8, index into LINES (entry line)
    start: np.ndarray         # int64, entered production
    ready: np.ndarray         # int64, last activity before line 3
    l3_start: np.ndarray      # int64, first line-3 station
    end: np.ndarray           # int64, last station
    response: np.ndarray      # int8, first test failed
    seen: np.ndarray          # int32 (parts, 52): first tick at each station
    repeats: np.ndarray       # int8, repeat test records of the part
    repeats_failed: np.ndarray  # int8, how many of those failed
    stations: list = field(default_factory=list)

    def __len__(self):
        return len(self.start)


def build_history():
    """Real parts in the twin's terms; cached in serving/twin_history.npz."""

    import json

    parts = pd.read_parquet(SERVING_DIR / "parts.parquet", columns=["part_id", "start", "end", "entry_line", "response", "twin_group"])
    with open(SERVING_DIR / "meta.json") as f:
        stations = json.load(f)["stations"]
    first_seen = np.load(SERVING_DIR / "station_first_seen.npy")

    # A twin group's first record (lowest Id) is the part; the rest are its
    # repeat tests (see twin_feature.py)
    group = parts["twin_group"].to_numpy()
    rows = np.arange(len(parts))
    first_row = pd.Series(rows).groupby(group).transform("min").to_numpy()
    repeat = (group >= 0) & (rows != first_row)

    response = parts["response"].to_numpy().astype(np.int8)
    repeats = np.zeros(len(parts), np.int8)
    repeats_failed = np.zeros(len(parts), np.int8)
    np.add.at(repeats, first_row[repeat], 1)
    np.add.at(repeats_failed, first_row[repeat], response[repeat])

    # L1_S24 and L1_S25 log several sub-steps, some spread over days: a
    # part is ready for line 3 only after the last of them
    header = pd.read_csv(DATA_DIR / "train_date.csv", nrows=0).columns
    sub_steps = [c for c in header if c.startswith(("L1_S24", "L1_S25"))]
    dates = pd.read_csv(DATA_DIR / "train_date.csv", usecols=["Id", *sub_steps], dtype={c: np.float32 for c in sub_steps})
    assert (dates["Id"].to_numpy() == parts["part_id"].to_numpy()).all()
    l1_last = dates[sub_steps].max(axis=1).to_numpy()

    numbers = np.array([station_number(s) for s in stations])
    before_l3 = numbers < 29

    keep = parts["start"].notna().to_numpy() & ~repeat
    seen_units = first_seen[keep]
    visited = ~np.isnan(seen_units)

    seen = np.full(seen_units.shape, NOT_VISITED, np.int32)
    seen[visited] = to_ticks(seen_units[visited])

    pre = np.where(visited[:, before_l3], seen[:, before_l3], -1).max(axis=1).astype(np.int64)
    l1 = l1_last[keep]
    pre = np.maximum(pre, np.where(np.isnan(l1), -1, to_ticks(np.nan_to_num(l1))))
    l3 = np.where(visited[:, ~before_l3], seen[:, ~before_l3], NOT_VISITED).min(axis=1).astype(np.int64)

    start = to_ticks(parts["start"].to_numpy()[keep])
    end = to_ticks(parts["end"].to_numpy()[keep])

    # Parts that start on line 3 are ready when they enter. A few log a
    # line-1 sub-step after reaching line 3; they count as ready then.
    ready = np.where(pre < 0, start, np.minimum(pre, l3))

    has_l3 = l3 != NOT_VISITED
    order = np.argsort(start[has_l3], kind="stable")
    pick = np.flatnonzero(has_l3)[order]

    line_code = pd.Categorical(parts["entry_line"].to_numpy()[keep], categories=LINES).codes.astype(np.int8)

    data = {
        "part_id": parts["part_id"].to_numpy()[keep][pick],
        "line": line_code[pick],
        "start": start[pick],
        "ready": ready[pick],
        "l3_start": l3[pick],
        "end": end[pick],
        "response": response[keep][pick],
        "seen": seen[pick],
        "repeats": repeats[keep][pick],
        "repeats_failed": repeats_failed[keep][pick],
    }
    np.savez(HISTORY_PATH, stations=np.array(stations), **data)
    return History(stations=stations, **data)


def load_history():
    if not HISTORY_PATH.exists():
        return build_history()

    with np.load(HISTORY_PATH) as f:
        data = {key: f[key] for key in f.files}
    return History(stations=list(data.pop("stations")), **data)


# ============================================================
# STATISTICS SHARED BY CALIBRATION AND VALIDATION
# ============================================================

def dense_ids(keys):
    """Arbitrary group keys -> 0..n-1"""
    return np.unique(keys, return_inverse=True)[1]


def observed_lift(y, group):
    """P(fail | another part of my group failed) / P(fail)"""

    failed = np.bincount(group, weights=y)
    other = failed[group] - y > 0
    return y[other].mean() / y.mean() if other.any() else np.nan


def expected_lift(p, group):
    """observed_lift's expectation when parts fail independently with probability p"""

    log_pass = np.log1p(-p)
    group_log_pass = np.bincount(group, weights=log_pass)
    other = 1 - np.exp(group_log_pass[group] - log_pass)
    return (p * other).sum() / other.sum() / p.mean()


def rate_by(index, values, min_count):
    """Mean of values per index, for indexes with at least min_count values (others NaN)"""

    count = np.bincount(index)
    total = np.bincount(index, weights=values, minlength=len(count))
    rate = np.full(len(count), np.nan)
    enough = count >= min_count
    rate[enough] = total[enough] / count[enough]
    return rate, count


def batch_alert(start, end, response, repeats_failed):
    """
    The batch-mate alert (factory_service.py) for every part: the tick it
    was flagged, i.e. when a part that entered in the same tick had a
    failed QC result reported while this one was still in production;
    NEVER if it never was. Repeat tests' failures count, as in the API.
    """

    failed = (response > 0) | (repeats_failed > 0)
    known = np.where(failed, end + QC_DELAY, NEVER)
    batch = dense_ids(start)
    first = np.full(batch.max() + 1, NEVER, np.int64)
    np.minimum.at(first, batch, known)
    flag = first[batch]
    return np.where(flag < end, flag, NEVER)


def line_monitor(end, response, repeats, repeats_failed, lo, hi, window_hours=72, ratio=1.5, minimum=300):
    """
    The line monitor (factory_service.py), hour by hour in [lo, hi) ticks:
    True when the QC failure rate of results reported in the last 72 hours
    is at least 1.5x the rate of all results reported before, with at
    least 300 recent results. Repeat tests count as results.
    """

    rows = np.repeat(np.arange(len(end)), repeats)
    nth = np.arange(len(rows)) - np.repeat(np.cumsum(repeats) - repeats, repeats)   # which repeat of its part
    known = np.r_[end, end[rows]] + QC_DELAY
    failed = np.r_[response > 0, nth < repeats_failed[rows]]
    order = np.argsort(known, kind="stable")
    k, cum = known[order], np.r_[0, np.cumsum(failed[order])]
    check = np.arange(lo, hi, TICKS_PER_HOUR_INT)
    n_now = np.searchsorted(k, check, "left")
    n_then = np.searchsorted(k, check - window_hours * TICKS_PER_HOUR_INT, "left")
    recent_n, recent_f = n_now - n_then, cum[n_now] - cum[n_then]
    history = cum[n_now] / np.maximum(n_now, 1)
    rate = recent_f / np.maximum(recent_n, 1)
    return (recent_n >= minimum) & (rate >= ratio * np.maximum(history, 1e-12))


def lag1(series):
    a, b = series[:-1], series[1:]
    ok = ~np.isnan(a) & ~np.isnan(b)
    return np.corrcoef(a[ok], b[ok])[0, 1]


# ============================================================
# CALIBRATION
# ============================================================

WEEK_MIN_PARTS = 2_000     # weeks and days used for failure-rate statistics
DAY_MIN_PARTS = 300


@dataclass
class Calibration:
    until: int                    # calibrated on parts that entered and finished before this tick
    stations: list
    # Template parts (real parts of the calibration window), by row
    tpl_row: np.ndarray           # row in History
    tpl_line: np.ndarray
    tpl_pre: np.ndarray           # int32 (parts, pre-line-3 stations): ticks after entry
    tpl_l3: np.ndarray            # int32 (parts, line-3 stations): ticks after line-3 start
    tpl_ready: np.ndarray         # ticks from entry to ready for line 3
    tpl_end: np.ndarray           # ticks from line-3 start to last station
    tpl_route: np.ndarray         # route effect (log-odds) of each template part
    # Template batches, per entry line: (first template row, size)
    batches: dict
    # Real weeks: arrivals and line-3 shifts
    weeks: list
    capacity: np.ndarray          # parts started per running line-3 hour, when busy
    l1_trigger: float             # hours the oldest L1 part waits before line 3 runs an L1 block
    plan: dict                    # line 3's weekly plan: usual backlog, correction gain, rate per running hour,
                                  # most hours per week, how often each hour of the week runs
    order_noise: np.ndarray       # hours, per queue (non-L1, L1)
    failure: dict                 # random-effect sizes and the intercept shift
    repeat_p: np.ndarray          # P(repeat test | first test passed / failed)
    repeat_fail: np.ndarray       # P(a repeat fails | first test passed / failed)
    order_target: np.ndarray = None   # real in-order shares per queue (fit_line3_policy matches them)
    failure_fit: dict = None          # the clustering statistics, real and fitted


@dataclass
class Week:
    """One real week: its batch arrivals and what line 3 did each hour."""

    index: int
    batch_tick: np.ndarray        # ticks after the week's start
    batch_line: np.ndarray
    batch_size: np.ndarray
    l3_hours: np.ndarray          # 168 values: 0 off, 1 non-L1 work, 2 L1 work
    l3_capacity: np.ndarray       # 168 values: parts line 3 started that hour
    kind: str                     # "L0", "L1", "L0+L1" or "idle"


def real_weeks(h, first_week, last_week):
    """The arrivals and line-3 shifts of real weeks [first_week, last_week]."""

    is_l1 = h.line == 1
    hour = h.l3_start // TICKS_PER_HOUR_INT
    hours = int(hour.max()) + 1
    starts_l1 = np.bincount(hour[is_l1], minlength=hours)
    starts_other = np.bincount(hour[~is_l1], minlength=hours)
    hour_type = np.where(starts_l1 > starts_other, 2, np.where(starts_other > 0, 1, 0)).astype(np.int8)

    batch_key = h.start * 4 + h.line          # one batch per entry tick and line
    keys, size = np.unique(batch_key, return_counts=True)
    tick, line = keys // 4, (keys % 4).astype(np.int8)
    week_of = tick // TICKS_PER_WEEK

    weeks = []
    for w in range(first_week, last_week + 1):
        m = week_of == w
        entries = np.bincount(line[m], weights=size[m], minlength=4)
        total = entries.sum()
        active = [LINES[i] for i in (0, 1) if total and entries[i] / total >= ACTIVE_SHARE and entries[i] >= 500]
        lo = w * HOURS_PER_WEEK
        pad = lambda a: np.pad(a[lo:lo + HOURS_PER_WEEK], (0, max(0, lo + HOURS_PER_WEEK - len(a))))
        l3, capacity = pad(hour_type), pad(starts_l1 + starts_other)
        weeks.append(Week(w, tick[m] - w * TICKS_PER_WEEK, line[m], size[m], l3, capacity, "+".join(active) or "idle"))
    return weeks


def order_concordance(ready, l3_start, rng, pairs=20_000, reach=500):
    """Share of pairs where the part that became ready later also started line 3 no earlier."""

    order = np.argsort(ready, kind="stable")
    r, s = ready[order], l3_start[order]
    i = rng.integers(0, len(r) - reach, pairs)
    j = i + rng.integers(1, reach, pairs)
    later = r[j] > r[i]
    return (s[j][later] >= s[i][later]).mean()


def route_matrix(h_seen, line):
    """Route features: stations visited plus the entry line."""

    visited = (h_seen != NOT_VISITED).astype(np.float32)
    lines = np.eye(len(LINES), dtype=np.float32)[line]
    return np.hstack([visited, lines])


def calibrate(h, until_week, first_week=1, fit_effects=True, seed=0, verbose=True):
    """
    Calibrate on real weeks [first_week, until_week): parts that entered
    and finished in that window. Week 0 is left out: the data starts with
    parts already part-way through production.
    """

    lo, until = first_week * TICKS_PER_WEEK, until_week * TICKS_PER_WEEK
    rows = np.flatnonzero((h.start >= lo) & (h.end < until))
    rng = np.random.default_rng(seed)

    numbers = np.array([station_number(s) for s in h.stations])
    pre_cols, l3_cols = np.flatnonzero(numbers < 29), np.flatnonzero(numbers >= 29)

    seen = h.seen[rows]
    start, l3_start = h.start[rows], h.l3_start[rows]

    def offsets(cols, origin):
        block = seen[:, cols].astype(np.int64)
        return np.where(block == NOT_VISITED, NOT_VISITED, block - origin[:, None]).astype(np.int32)

    # Route effect: logistic model of the first test on stations visited and entry line
    x = route_matrix(seen, h.line[rows])
    y = h.response[rows].astype(float)
    model = LogisticRegression(C=1.0, max_iter=2000)
    model.fit(x, y)
    route = model.decision_function(x).astype(np.float32)

    # Template batches: real batches (same entry tick and line), per line
    key = start * 4 + h.line[rows]
    order = np.argsort(key, kind="stable")
    rows, start, l3_start, seen, route, key = rows[order], start[order], l3_start[order], seen[order], route[order], key[order]
    pre, l3 = offsets(pre_cols, start), offsets(l3_cols, l3_start)
    first, size = np.unique(key, return_index=True, return_counts=True)[1:]
    batch_line = h.line[rows][first]
    batches = {code: (first[batch_line == code], size[batch_line == code]) for code in range(len(LINES))}

    # Line-3 capacity: parts started per running hour while the queue was long
    is_l1 = h.line == 1
    hours = int(h.l3_start.max()) // TICKS_PER_HOUR_INT + 1
    started = np.bincount(h.l3_start // TICKS_PER_HOUR_INT, minlength=hours)
    readied = np.bincount(np.minimum(h.ready // TICKS_PER_HOUR_INT, hours - 1), minlength=hours)
    backlog = np.cumsum(readied) - np.cumsum(started)
    window = np.arange(hours)
    busy = (started > 0) & (np.r_[0, backlog[:-1]] > BUSY_BACKLOG) & (window >= lo // 10) & (window < until // 10)
    capacity = started[busy]
    week_starts = np.arange(lo // 10, until // 10, HOURS_PER_WEEK)
    by_week = started[lo // 10:until // 10].reshape(-1, HOURS_PER_WEEK)
    running = by_week > 0
    active = running.any(axis=1)
    weekly_rate = by_week.sum(axis=1)[active] / running.sum(axis=1)[active]
    plan = {
        "backlog": float(np.median(backlog[week_starts])),        # usual queue at the start of a week
        "gain": 0.5,                                               # share of the excess backlog planned away each period
        "period": HOURS_PER_WEEK,                                  # hours planned at a time
        "rate": float(np.median(weekly_rate)),                     # usual parts per running hour
        "rate_max": float(np.percentile(weekly_rate, 90)),         # the busiest weeks' rate
        "hourly_max": float(np.percentile(by_week[running], 99.5)),
        "max_hours": int(running.sum(axis=1).max()),               # the most hours line 3 ran in a week
        "hour_preference": running.mean(axis=0),                   # how often each hour of the week runs
    }

    # How loosely each queue keeps first-in-first-out order (hours of noise on the ready time)
    in_window = (h.start >= lo) & (h.end < until)
    target = np.array([
        order_concordance(h.ready[in_window & ~is_l1], h.l3_start[in_window & ~is_l1], rng),
        order_concordance(h.ready[in_window & is_l1], h.l3_start[in_window & is_l1], rng),
    ])

    # Repeat tests
    first_failed = h.response[rows].astype(bool)
    reps, reps_failed = h.repeats[rows], h.repeats_failed[rows]
    repeat_p = np.array([(reps[~first_failed] > 0).mean(), (reps[first_failed] > 0).mean()])
    repeat_fail = np.array([
        reps_failed[~first_failed].sum() / max(reps[~first_failed].sum(), 1),
        reps_failed[first_failed].sum() / max(reps[first_failed].sum(), 1),
    ])

    cal = Calibration(
        until=until,
        stations=h.stations,
        tpl_row=rows,
        tpl_line=h.line[rows],
        tpl_pre=pre,
        tpl_l3=l3,
        tpl_ready=(h.ready[rows] - start).astype(np.int32),
        tpl_end=(h.end[rows] - l3_start).astype(np.int32),
        tpl_route=route,
        batches=batches,
        weeks=real_weeks(h, first_week, until_week - 1),
        capacity=capacity,
        l1_trigger=48.0,
        plan=plan,
        order_noise=np.array([2.0, 80.0]),
        failure={"sigma_batch": 0.0, "sigma_hour": 0.0, "sigma_day": 0.0, "phi_day": 0.9, "shift": 0.0},
        repeat_p=repeat_p,
        repeat_fail=repeat_fail,
        order_target=target,
    )

    if verbose:
        print(f"calibration: weeks {first_week}-{until_week - 1}, {len(rows):,} template parts, "
              f"{sum(len(b[0]) for b in batches.values()):,} batches, line-3 capacity {capacity.mean():.0f}/running hour")

    if fit_effects:
        fit_failure_effects(cal, h, rows, route, verbose=verbose)
    return cal


def fit_failure_effects(cal, h, rows, route, draws=3, seed=0, verbose=True):
    """
    Size the random effects so failures cluster like the real ones.

    On the calibration parts (their real routes, batches and line-3 times)
    four statistics are matched: the spread of weekly failure rates, how
    much a day's rate carries over to the next, and the failure lift
    within the same line-3 tick and within the same entry batch. The
    model's statistics are computed from failure probabilities rather
    than random draws, so the fit is smooth.
    """

    y = h.response[rows].astype(float)
    batch = dense_ids(h.start[rows])
    l3_tick = dense_ids(h.l3_start[rows])
    hour = h.l3_start[rows] // TICKS_PER_HOUR_INT
    hour = hour - hour.min()
    day, week = hour // 24, hour // HOURS_PER_WEEK

    weekly, _ = rate_by(week, y, WEEK_MIN_PARTS)
    daily, _ = rate_by(day, y, DAY_MIN_PARTS)
    target = np.array([np.nanstd(weekly), lag1(daily), observed_lift(y, l3_tick), observed_lift(y, batch)])

    rng = np.random.default_rng(seed)
    z_batch = rng.standard_normal((draws, batch.max() + 1))
    z_hour = rng.standard_normal((draws, hour.max() + 1))
    e_day = rng.standard_normal((draws, day.max() + 1))

    def probabilities(sb, sh, sd, phi, draw):
        drift = lfilter([1.0], [1.0, -phi], e_day[draw]) * sd * np.sqrt(1 - phi**2)
        logit = route + sb * z_batch[draw][batch] + sh * z_hour[draw][hour] + drift[day]
        # Shift the intercept so the mean failure rate stays the real one
        shift = 0.0
        for _ in range(6):
            p = 1 / (1 + np.exp(-(logit + shift)))
            shift += (y.mean() - p.mean()) / (p * (1 - p)).mean()
        return 1 / (1 + np.exp(-(logit + shift))), shift

    def statistics(p):
        wr, wn = rate_by(week, p, WEEK_MIN_PARTS)
        ok = ~np.isnan(wr)
        spread = np.sqrt(np.var(wr[ok]) + np.mean(wr[ok] * (1 - wr[ok]) / wn[ok]))
        dr, dn = rate_by(day, p, DAY_MIN_PARTS)
        a, b = dr[:-1], dr[1:]
        pair = ~np.isnan(a) & ~np.isnan(b)
        noise = np.nanmean(dr * (1 - dr) / np.maximum(dn, 1))
        carry = np.cov(a[pair], b[pair])[0, 1] / (np.nanvar(dr) + noise)
        return np.array([spread, carry, expected_lift(p, l3_tick), expected_lift(p, batch)])

    def unpack(theta):
        sb, sh, sd = np.exp(theta[:3])
        return sb, sh, sd, 1 / (1 + np.exp(-theta[3]))

    def loss(theta):
        stats = np.mean([statistics(probabilities(*unpack(theta), d)[0]) for d in range(draws)], axis=0)
        return float(np.sum((stats / target - 1) ** 2))

    start = np.array([np.log(0.5), np.log(0.5), np.log(0.5), np.log(0.9 / 0.1)])
    result = minimize(loss, start, method="Nelder-Mead", options={"xatol": 0.02, "fatol": 1e-4, "maxiter": 200})
    sb, sh, sd, phi = unpack(result.x)
    shift = float(np.mean([probabilities(sb, sh, sd, phi, d)[1] for d in range(draws)]))
    fitted = np.mean([statistics(probabilities(sb, sh, sd, phi, d)[0]) for d in range(draws)], axis=0)

    cal.failure = {"sigma_batch": float(sb), "sigma_hour": float(sh), "sigma_day": float(sd), "phi_day": float(phi), "shift": shift}
    cal.failure_fit = {"names": ["weekly rate SD", "daily rate carry-over", "same line-3 tick lift", "same entry batch lift"],
                       "target": target, "fitted": fitted}

    if verbose:
        print(f"failure effects: batch {sb:.2f}, hour {sh:.2f}, day {sd:.2f} (carry-over {phi:.2f}); "
              f"{result.nfev} evaluations")
        for name, t, f in zip(cal.failure_fit["names"], target, fitted):
            print(f"  {name:24} real {t:.4f}  twin {f:.4f}")


# ============================================================
# SIMULATION
# ============================================================

@dataclass
class Disturbance:
    """
    Extra log-odds of failing for parts inside a time window (a stress test):
    by="line3" hits parts that start line 3 in the window (a problem on
    line 3), by="entry" parts that entered production in it (a bad lot of
    material, say).
    """

    start_hour: float
    hours: float
    log_odds: float
    station: str | None = None     # only parts that visit this station
    line: str | None = None        # only parts from this entry line
    by: str = "line3"


@dataclass
class Run:
    """Simulated parts (each once, first tests), with their repeat tests."""

    line: np.ndarray
    batch: np.ndarray
    template: np.ndarray           # template part whose route and timing the part copied
    start: np.ndarray
    ready: np.ndarray
    l3_start: np.ndarray
    end: np.ndarray
    response: np.ndarray
    probability: np.ndarray
    disturbed: np.ndarray          # hit by a Disturbance
    repeats: np.ndarray
    repeats_failed: np.ndarray
    horizon: tuple                 # (first tick, last tick) simulated
    l3_hours: np.ndarray           # what line 3 did each hour: 0 off, 1 non-L1 work, 2 L1 work
    l3_capacity: np.ndarray        # starts line 3 planned each hour
    unfinished: int                # parts still in production at the end (dropped, as in the real data)

    def __len__(self):
        return len(self.start)

    def seen(self, cal):
        """First tick at each station, like History.seen (parts, 52)."""

        numbers = np.array([station_number(s) for s in cal.stations])
        out = np.full((len(self), len(numbers)), NOT_VISITED, np.int32)
        for cols, block, origin in ((numbers < 29, cal.tpl_pre, self.start), (numbers >= 29, cal.tpl_l3, self.l3_start)):
            offsets = block[self.template].astype(np.int64)
            out[:, cols] = np.where(offsets == NOT_VISITED, NOT_VISITED, offsets + origin[:, None])
        return out


def waiting_at(h, week):
    """Real parts in production at the start of `week` that hadn't reached line 3 yet."""

    t0 = week * TICKS_PER_WEEK
    m = (h.start < t0) & (h.l3_start >= t0)
    return {"ready": h.ready[m], "is_l1": h.line[m] == 1, "batch": dense_ids(h.start[m] * 4 + h.line[m])}


def plan_weeks(cal, kinds, rng):
    """A synthetic production plan: for each week kind, a real calibration week of that kind."""

    by_kind = {}
    for week in cal.weeks:
        by_kind.setdefault(week.kind, []).append(week)
    return [by_kind[kind][rng.integers(len(by_kind[kind]))] for kind in kinds]


def simulate(cal, weeks, start_week, seed=0, volume=1.0, capacity_scale=1.0, max_hours=None,
             level_shift=0.0, disturbances=(), waiting=None):
    """
    Run the twin over `weeks` (real or planned Week patterns), placed one
    after another from `start_week`.

    volume          multiplies the size of every arriving batch
    capacity_scale  multiplies line 3's rate per running hour
    max_hours       the most hours line 3 may run in a week (default: the
                    most it ran in a calibration week). Line 3 plans each
                    week's hours from the coming workload and its backlog,
                    preferring the copied week's own shifts (serve_line3).
    level_shift     log-odds added to every part's chance of failing (a
                    different overall quality level)
    disturbances    extra failure log-odds in time windows (Disturbance)
    waiting         parts already in production at the start (see waiting_at):
                    they hold line 3's capacity like the real backlog did,
                    then leave the run
    """

    rng = np.random.default_rng(seed)
    t0 = start_week * TICKS_PER_WEEK
    horizon_hours = len(weeks) * HOURS_PER_WEEK

    # ---- Arrivals: batches, each copying a real batch of the same line
    tick = np.concatenate([t0 + k * TICKS_PER_WEEK + w.batch_tick for k, w in enumerate(weeks)])
    line = np.concatenate([w.batch_line for w in weeks])
    size = np.concatenate([w.batch_size for w in weeks])
    if volume != 1.0:
        scaled = size * volume
        size = np.floor(scaled).astype(np.int64) + (rng.random(len(size)) < scaled % 1)
    keep = size > 0
    tick, line, size = tick[keep], line[keep], size[keep]

    tpl_first = np.empty(len(tick), np.int64)
    tpl_size = np.empty(len(tick), np.int64)
    for code in range(len(LINES)):
        m = line == code
        firsts, sizes = cal.batches[code]
        if len(firsts) == 0:
            # No batch of this entry line in the calibration window: copy L0 batches
            firsts, sizes = cal.batches[0]
        pick = rng.integers(0, len(firsts), m.sum())
        tpl_first[m], tpl_size[m] = firsts[pick], sizes[pick]

    batch = np.repeat(np.arange(len(tick)), size)
    within = np.arange(len(batch)) - np.repeat(np.cumsum(size) - size, size)
    rotate = rng.integers(0, tpl_size)
    template = tpl_first[batch] + (rotate[batch] + within) % tpl_size[batch]

    part_line = line[batch]
    start = tick[batch]
    ready = start + cal.tpl_ready[template]

    # ---- Line 3: two queues (L1 parts, everything else), served by the hour
    profile = np.concatenate([w.l3_capacity for w in weeks]).astype(float)
    plan = dict(cal.plan, rate=cal.plan["rate"] * capacity_scale, rate_max=cal.plan["rate_max"] * capacity_scale,
                hourly_max=cal.plan["hourly_max"] * capacity_scale,
                max_hours=cal.plan["max_hours"] if max_hours is None else max_hours)
    n_new = len(ready)
    if waiting is not None:
        ready_all = np.r_[ready, waiting["ready"]]
        l1_all = np.r_[part_line == 1, waiting["is_l1"]]
        batch_all = np.r_[batch, len(tick) + waiting["batch"]]
    else:
        ready_all, l1_all, batch_all = ready, part_line == 1, batch
    l3_start, schedule, capacity = serve_line3(ready_all, l1_all, batch_all, profile, t0, plan, cal.order_noise, cal.l1_trigger, rng)
    l3_start = l3_start[:n_new]

    end = l3_start + cal.tpl_end[template]
    last_tick = t0 + horizon_hours * TICKS_PER_HOUR_INT
    done = (l3_start != NEVER) & (end < last_tick)

    # ---- Final QC
    hour = np.where(done, l3_start // TICKS_PER_HOUR_INT, 0) - t0 // TICKS_PER_HOUR_INT
    hour = np.clip(hour, 0, None)
    day = hour // 24
    f = cal.failure
    drift = lfilter([1.0], [1.0, -f["phi_day"]], rng.standard_normal(day.max() + 1)) * f["sigma_day"] * np.sqrt(1 - f["phi_day"] ** 2)
    logit = (cal.tpl_route[template] + f["shift"] + level_shift
             + f["sigma_batch"] * rng.standard_normal(len(tick))[batch]
             + f["sigma_hour"] * rng.standard_normal(hour.max() + 1)[hour]
             + drift[day])

    numbers = [station_number(s) for s in cal.stations]
    disturbed = np.zeros(len(batch), bool)
    for d in disturbances:
        # Production hours, to the tick: a 0.1-hour window hits one entry batch
        when = (start if d.by == "entry" else np.where(done, l3_start, 0)) / TICKS_PER_HOUR_INT
        hit = (when >= d.start_hour) & (when < d.start_hour + d.hours) & done
        if d.line is not None:
            hit &= part_line == LINES.index(d.line)
        if d.station is not None:
            j = cal.stations.index(d.station)
            block, col = (cal.tpl_pre, j) if numbers[j] < 29 else (cal.tpl_l3, j - sum(n < 29 for n in numbers))
            hit &= block[template, col] != NOT_VISITED
        logit = logit + d.log_odds * hit
        disturbed |= hit

    probability = 1 / (1 + np.exp(-logit))
    response = (rng.random(len(batch)) < probability).astype(np.int8)

    # ---- Repeat tests (one at most here; real parts rarely have more)
    repeats = (rng.random(len(batch)) < cal.repeat_p[response]).astype(np.int8)
    repeats_failed = (repeats.astype(bool) & (rng.random(len(batch)) < cal.repeat_fail[response])).astype(np.int8)

    pick = np.flatnonzero(done)
    pick = pick[np.argsort(start[pick], kind="stable")]
    return Run(
        line=part_line[pick], batch=batch[pick], template=template[pick], start=start[pick], ready=ready[pick],
        l3_start=l3_start[pick], end=end[pick], response=response[pick], probability=probability[pick].astype(np.float32),
        disturbed=disturbed[pick],
        repeats=repeats[pick], repeats_failed=repeats_failed[pick], horizon=(t0, last_tick),
        l3_hours=schedule, l3_capacity=capacity, unfinished=int((~done).sum()),
    )


def serve_line3(ready, is_l1, batch, profile, t0, plan, noise_hours, l1_trigger_hours, rng):
    """
    Line 3's start tick for every part (NEVER if it isn't served in time),
    and what line 3 did each hour (0 off, 1 non-L1 work, 2 L1 work).

    Line 3 plans each week for the parts that become ready that week plus
    plan["gain"] times the gap between the backlog and its usual level.
    It runs the copied week's shifts, following that week's real hourly
    output (`profile`) scaled to the plan, at an average of at most
    plan["rate_max"] per running hour (the busiest real weeks). Beyond
    that it adds hours at plan["rate"], the most common hours of the week
    first, up to plan["max_hours"]. (The real line's weekly output follows
    its running hours, correlation 0.96, more than its rate, 0.76; its
    backlog stays near a steady level.) It works on L0
    parts (and the few that entered on L2 or L3) until the oldest waiting
    L1 part has waited l1_trigger_hours; then it runs an L1 block until no
    L1 part is waiting. The real line works the same way: 78% of its
    running hours serve only non-L1 parts and 22% only L1 parts, and the
    L1 backlog is usually near zero. An hour serves one line; when an L1
    block runs out of parts, the rest of the hour goes to L0 work. Within a queue, parts go in order of their ready time plus noise
    shared by their batch: batches stay together, while the noise loosens
    first-in-first-out to the measured degree.
    """

    import heapq

    l3_start = np.full(len(ready), NEVER, np.int64)
    served = np.zeros(len(ready), bool)
    queues = []
    for q, mask in enumerate((~is_l1, is_l1)):
        idx = np.flatnonzero(mask)
        idx = idx[np.argsort(ready[idx], kind="stable")]
        shift = rng.normal(0, noise_hours[q] * TICKS_PER_HOUR_INT, batch.max() + 1) if noise_hours[q] > 0 else np.zeros(batch.max() + 1)
        queues.append({"idx": idx, "ready": ready[idx], "key": ready[idx] + shift[batch[idx]], "next": 0, "heap": [], "oldest": 0})
    other, l1 = queues

    first_hour = t0 // TICKS_PER_HOUR_INT
    trigger = l1_trigger_hours * TICKS_PER_HOUR_INT
    hours = len(profile)
    hour_type = np.zeros(hours, np.int8)
    capacity = np.zeros(hours, np.int64)
    in_block = False

    # Parts becoming ready in each planning period of the run
    period = int(plan["period"])
    periods = (hours + period - 1) // period
    workload = np.bincount(np.clip((ready - t0) // (period * TICKS_PER_HOUR_INT), 0, periods - 1), minlength=periods)
    workload[0] += int(np.sum(ready < t0))

    for k in range(hours):
        hour = first_hour + k
        now = hour * TICKS_PER_HOUR_INT
        for q in queues:
            stop = np.searchsorted(q["ready"], now + TICKS_PER_HOUR_INT, "left")
            for i in range(q["next"], stop):
                heapq.heappush(q["heap"], (q["key"][i], q["idx"][i]))
            q["next"] = stop

        if k % period == 0:
            # This period's plan: the coming workload plus part of the excess backlog
            backlog = sum(q["next"] for q in queues) - int(served.sum())
            target = workload[k // period] + plan["gain"] * (backlog - plan["backlog"])
            target = max(target, 0.0)
            shape = profile[k:k + period]
            own = shape > 0
            # The week's own shifts, following its real hourly output, up to the busiest weeks' rate
            from_shifts = min(target, own.sum() * plan["rate_max"])
            rate = np.minimum(shape / shape.sum() * from_shifts, plan["hourly_max"]) if own.any() else np.zeros(len(shape))
            # More work than that: extra hours at the usual rate, the most common hours first
            extra_hours = int(np.ceil((target - rate.sum()) / plan["rate"])) if target > rate.sum() + 1 else 0
            extra_hours = min(extra_hours, max(0, int(plan["max_hours"] * period / HOURS_PER_WEEK) - int(own.sum())))
            if extra_hours:
                week_hour = (np.arange(k, k + len(shape)) + first_hour) % HOURS_PER_WEEK
                off = np.flatnonzero(~own)
                rate[off[np.argsort(-plan["hour_preference"][week_hour[off]], kind="stable")[:extra_hours]]] = plan["rate"]
            capacity[k:k + len(shape)] = np.floor(rate) + (rng.random(len(shape)) < rate % 1)

        budget = int(capacity[k])
        if budget <= 0:
            continue

        # An L1 block starts when the oldest waiting L1 part has waited long
        # enough, or when there is no other work
        while l1["oldest"] < l1["next"] and served[l1["idx"][l1["oldest"]]]:
            l1["oldest"] += 1
        l1_waiting = l1["oldest"] < l1["next"]
        if not in_block and l1_waiting and (now - l1["ready"][l1["oldest"]] >= trigger or not other["heap"]):
            in_block = True
        hour_type[k] = 2 if in_block else 1

        # Hours stay with one line, as on the real line (1% of its hours mix
        # them); only when an L1 block runs out does the rest go to L0 work
        picked = []
        for q in ((l1, other) if in_block else (other,)):
            while budget and q["heap"]:
                i = heapq.heappop(q["heap"])[1]
                served[i] = True
                picked.append(i)
                budget -= 1
        if in_block and not l1["heap"]:
            in_block = False

        if picked:
            picked = np.array(picked)
            slot = now + (np.arange(len(picked)) * TICKS_PER_HOUR_INT) // len(picked)
            l3_start[picked] = np.maximum(slot, ready[picked])

    return l3_start, hour_type, capacity


def fit_line3_policy(cal, h, start_week, until_week, seed=0, verbose=True,
                     triggers=(6, 12, 24, 48, 96, 144, 192, 264, 336), noises=(0, 1, 2, 4, 8, 16)):
    """
    Choose the L1 block trigger so the median L1 wait matches the real
    line, then the non-L1 order noise so its first-in-first-out share does.
    L1 parts are served in near-random order within a block (58% in
    order on the real line), so their noise is fixed at 80 hours.
    """

    weeks = real_weeks(h, start_week, until_week - 1)
    in_window = (h.start >= (start_week + 8) * TICKS_PER_WEEK) & (h.end < until_week * TICKS_PER_WEEK)
    is_l1 = h.line == 1
    real_wait = np.median((h.l3_start - h.ready)[in_window & is_l1]) / TICKS_PER_HOUR_INT

    def run_with(trigger, noise):
        cal.l1_trigger, cal.order_noise = trigger, np.array([noise, 80.0])
        run = simulate(cal, weeks, start_week, seed=seed, waiting=waiting_at(h, start_week))
        keep = run.start >= (start_week + 8) * TICKS_PER_WEEK
        return run, keep

    waits = []
    for trigger in triggers:
        run, keep = run_with(trigger, 2.0)
        m = keep & (run.line == 1)
        waits.append(np.median(run.l3_start[m] - run.ready[m]) / TICKS_PER_HOUR_INT)
    trigger = float(np.interp(real_wait, waits, triggers)) if np.all(np.diff(waits) > 0) else float(triggers[np.argmin(np.abs(np.array(waits) - real_wait))])

    shares = []
    rng = np.random.default_rng(seed)
    for noise in noises:
        run, keep = run_with(trigger, noise)
        m = keep & (run.line != 1)
        shares.append(order_concordance(run.ready[m], run.l3_start[m], rng))
    noise = float(np.interp(-cal.order_target[0], -np.array(shares), noises))

    cal.l1_trigger, cal.order_noise = trigger, np.array([noise, 80.0])
    if verbose:
        print(f"line 3: L1 block after the oldest L1 part waits {trigger:.0f} h "
              f"(median L1 wait {real_wait:.0f} h real; twin at each trigger: {', '.join(f'{w:.0f}' for w in waits)}); "
              f"non-L1 order noise {noise:.1f} h (in-order share {cal.order_target[0]:.0%})")
