"""
The stream of final-QC results, ordered by when each result became known.

A monitor may only use QC results that were already known at the moment
it makes a decision. Everything here answers "what did we know at time t?".

Times are in date units (see production_data.py) and are converted to
integer 6-minute ticks, so "known before t" comparisons are exact.
"""

import numpy as np

from production_data import HOURS_PER_UNIT


# Timestamps have two decimals: 1 tick = 0.01 units = 6 minutes
TICKS_PER_UNIT = 100
TICKS_PER_HOUR = TICKS_PER_UNIT / HOURS_PER_UNIT

# Batch keys = entry tick * BATCH_KEY_SCALE + tick. Ticks stay far below
# this (the data spans ~172,000 ticks), so batches never overlap.
BATCH_KEY_SCALE = 10_000_000

# "Never happened" as a tick
NEVER = np.iinfo(np.int64).max


def to_ticks(times):
    """Date units -> integer 6-minute ticks."""
    return np.round(np.asarray(times, dtype=np.float64) * TICKS_PER_UNIT).astype(np.int64)


def hours_to_ticks(hours):
    return int(round(hours * TICKS_PER_HOUR))


class QCStream:

    def __init__(self, start, end, y, delay_hours):
        """
        start, end   first / last timestamp of every labelled part
        y            final-QC result (1 = failed)
        delay_hours  how long after a part's last station its result is known
        """

        known = to_ticks(end) + hours_to_ticks(delay_hours)
        entry = to_ticks(start)

        # All results, in the order they became known
        order = np.argsort(known, kind="stable")
        self._known = known[order]
        self._cum_fail = np.r_[0, np.cumsum(y[order])]

        # Results grouped by entry batch (parts that entered production in
        # the same tick), and by when they became known within each batch
        keys = entry * BATCH_KEY_SCALE + known
        order = np.argsort(keys, kind="stable")
        self._batch_keys = keys[order]
        self._batch_cum_fail = np.r_[0, np.cumsum(y[order])]

        # Failures only, by batch and then by when they became known
        self._failure_keys = np.sort(keys[y == 1])

        self._entry_sorted = np.sort(entry)

    def failure_rate(self, t, window_hours):
        """Failure rate of results that became known in [t - window, t), and how many."""

        t = to_ticks(t)
        hi = np.searchsorted(self._known, t, "left")
        lo = np.searchsorted(self._known, t - hours_to_ticks(window_hours), "left")
        parts = hi - lo

        return (self._cum_fail[hi] - self._cum_fail[lo]) / np.maximum(parts, 1), parts

    def historical_rate(self, t):
        """Failure rate of every result known before t."""

        hi = np.searchsorted(self._known, to_ticks(t), "left")

        return self._cum_fail[hi] / np.maximum(hi, 1)

    def batch_mates_known(self, start, t):
        """
        For parts that entered production at `start`: how many batch-mates
        had a known QC result before t -- (failed, passed).

        A part's own result is not known before its last station, so it is
        never counted as long as t <= its end.
        """

        batch = to_ticks(start) * BATCH_KEY_SCALE
        lo = np.searchsorted(self._batch_keys, batch, "left")
        hi = np.searchsorted(self._batch_keys, batch + to_ticks(t), "left")

        failed = self._batch_cum_fail[hi] - self._batch_cum_fail[lo]

        return failed, (hi - lo) - failed

    def first_batch_failure_known(self, start):
        """
        Tick at which the first failure in this part's entry batch became
        known; NEVER if the batch has no failures.

        Compare with the part's own end tick: a part's own failure is only
        known after its last station, so "first failure known < end" always
        means a batch-mate failed first.
        """

        batch = to_ticks(start)
        pos = np.searchsorted(self._failure_keys, batch * BATCH_KEY_SCALE, "left")
        key = self._failure_keys[np.minimum(pos, len(self._failure_keys) - 1)]

        same_batch = (pos < len(self._failure_keys)) & (key // BATCH_KEY_SCALE == batch)

        return np.where(same_batch, key % BATCH_KEY_SCALE, NEVER)

    def batch_size(self, start):
        """How many labelled parts entered production in the same tick (including itself)."""

        entry = to_ticks(start)

        return (
            np.searchsorted(self._entry_sorted, entry, "right")
            - np.searchsorted(self._entry_sorted, entry, "left")
        )
