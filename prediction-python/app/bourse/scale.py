"""The power-of-ten correction for TSETMC index histories, and its verdict.

WHAT IS BEING CORRECTED
-----------------------
Some index values in ``GetIndexB2History`` are stored off by exactly a factor
of ten.  Measured 2026-09-29 over all 71 indices: 26 single-session steps of
x10 or /10 across six of them.  No index can move tenfold in a session — TSE's
daily price limits hold TEDPIX's largest measured session to -5.51% — so a
step in the band ``[10**0.9, 10**1.1]`` (7.94x to 12.59x, either way) is a
change of SCALE, not of value.  Some revert the next session; some persist:
the second-market index has been stored /10 since 2026-08-16.

HOW
---
Walk the series and keep a running exponent that changes by one at every such
step, so ``close * 10**exp`` is continuous.  That fixes every value relative to
every other; what remains is which scale is the true one.  The answer used is
the scale MOST rows share — 4,263 of the second-market index's rows against
31 — and it is then CHECKED against the exchange's own live value at fetch
time rather than assumed: a corrected history whose newest value disagrees
with TSETMC's live figure by more than a session could move is refused.

The raw close is never rewritten.  The exponent is stored beside it, exactly as
a corporate action's factor is stored beside a raw bar in 0028, and applied at
read time.

WHAT THIS DOES NOT DO
---------------------
It does not smooth, interpolate or filter.  A 40% session in a thin sector
index is left alone and REPORTED (``largest_move``), because a one-company
sector reopening after a long halt can genuinely do that, and this module
cannot tell that apart from an error.  Only the one defect that is
unambiguous — a factor of ten — is corrected.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Optional, Sequence

# Bump when the detection rule changes, so a new verdict lands beside the old
# one (market_index_checks is keyed by version) instead of over it.
CHECK_VERSION = "decimal-shift-v1"

STATUS_VALIDATED = "validated"
STATUS_REFUSED = "refused"

# A step whose ratio lies in [10**0.9, 10**1.1], or whose inverse does, is a
# change of scale.  The band is wide enough to absorb the session's own move
# on the day the scale changed (the second-market step is 9,878,360 ->
# 1,006,020, a ratio of 0.1018) and narrow enough that no genuine move reaches
# it: the largest measured single-session move in any index AFTER correction
# is +244.7% (x3.45), far from x7.94.
SCALE_BAND_LO = 10 ** 0.9
SCALE_BAND_HI = 10 ** 1.1

# The CHECK constraint on market_index_values.scale_exp.  A history needing
# more than three decades of correction is not a decimal-shift defect; it is
# something this module does not understand, and it is refused.
MAX_ABS_EXP = 3

# Corrected-newest / live must fall in this band.  Deliberately loose: the live
# figure may be several sessions after the newest settled one (a Thursday
# fetch, a halt), and a sector index can move tens of percent in that time.
# What the check must catch is a wrong DECADE — a ratio near 10 or 0.1 — and
# [0.75, 1.33] separates those from any plausible drift by a wide margin.
LIVE_RATIO_LO = 0.75
LIVE_RATIO_HI = 1.0 / 0.75


def scale_exponents(closes: Sequence[float]) -> tuple[list[int], list[int]]:
    """Per-row exponents such that ``closes[i] * 10**exps[i]`` is continuous,
    anchored on the scale most rows share.  Also the indices of the steps at
    which the scale changed.  Pure function (unit tested).

    Ties in the majority are broken toward the exponent nearest zero, then the
    larger one — a deterministic rule for a case (two scales with exactly equal
    row counts) that only a tiny series can produce.
    """
    if not closes:
        return [], []
    for c in closes:
        if not (c > 0) or math.isinf(c):
            raise ValueError(f"scale_exponents needs positive finite closes, got {c!r}")
    rel = [0]
    breaks: list[int] = []
    for i in range(1, len(closes)):
        ratio = closes[i] / closes[i - 1]
        k = rel[-1]
        if SCALE_BAND_LO <= ratio <= SCALE_BAND_HI:
            # The raw value jumped tenfold: from here on it must be divided.
            k -= 1
            breaks.append(i)
        elif SCALE_BAND_LO <= 1.0 / ratio <= SCALE_BAND_HI:
            k += 1
            breaks.append(i)
        rel.append(k)
    counts = Counter(rel)
    reference = max(counts.items(), key=lambda kv: (kv[1], -abs(kv[0]), kv[0]))[0]
    return [k - reference for k in rel], breaks


@dataclass(frozen=True)
class IndexVerdict:
    status: str
    values_total: int
    dropped_nonpositive: int
    scale_breaks: int
    rows_rescaled: int
    first_break: Optional[date]
    last_break: Optional[date]
    largest_move: Optional[float]  # a fraction, after correction; signed
    largest_move_date: Optional[date]
    live_value: Optional[float]
    live_ratio: Optional[float]
    refusal_reason: str


def verdict(
    dates: Sequence[date],
    closes: Sequence[float],
    exps: Sequence[int],
    breaks: Sequence[int],
    dropped_nonpositive: int,
    live_value: Optional[float],
) -> IndexVerdict:
    """The gate: validated unless the correction is out of range or disagrees
    with the exchange's own live figure.  Pure function (unit tested)."""
    corrected = [c * 10 ** e for c, e in zip(closes, exps)]
    largest: Optional[float] = None
    largest_date: Optional[date] = None
    for i in range(1, len(corrected)):
        move = corrected[i] / corrected[i - 1] - 1
        if largest is None or abs(move) > abs(largest):
            largest, largest_date = move, dates[i]

    reasons: list[str] = []
    worst_exp = max((abs(e) for e in exps), default=0)
    if worst_exp > MAX_ABS_EXP:
        reasons.append(
            f"the correction needs a power of ten of {worst_exp}, beyond the "
            f"{MAX_ABS_EXP} a decimal-shift defect can explain; this history has "
            "a scale problem this check does not understand"
        )

    ratio: Optional[float] = None
    if live_value is not None and live_value > 0 and corrected:
        ratio = corrected[-1] / live_value
        if not (LIVE_RATIO_LO <= ratio <= LIVE_RATIO_HI):
            reasons.append(
                f"after correction the newest value ({corrected[-1]:,.2f} on "
                f"{dates[-1].isoformat()}) is {ratio:.4f} times TSETMC's own live "
                f"figure ({live_value:,.2f}); a session cannot move that far, so the "
                "majority scale this correction anchored on is not the exchange's"
            )

    return IndexVerdict(
        status=STATUS_REFUSED if reasons else STATUS_VALIDATED,
        values_total=len(closes),
        dropped_nonpositive=dropped_nonpositive,
        scale_breaks=len(breaks),
        rows_rescaled=sum(1 for e in exps if e != 0),
        first_break=dates[breaks[0]] if breaks else None,
        last_break=dates[breaks[-1]] if breaks else None,
        largest_move=largest,
        largest_move_date=largest_date,
        live_value=live_value,
        live_ratio=ratio,
        refusal_reason="; ".join(reasons),
    )
