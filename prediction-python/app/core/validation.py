"""Observation validation: sanity ranges, outlier detection, dedupe keys.

Rules (docs/CONTRACTS.md + service design):

* every observation gets a deterministic dedupe key
  ``sha256("provider|symbol|observed_at_iso|value")`` matching the unique
  ``raw_observations.dedupe_key`` column;
* values outside per-symbol plausibility ranges are rejected outright
  (usually a unit mix-up: rial-vs-toman or gram-vs-ounce);
* a value deviating strongly from the recent window median (robust MAD test)
  or jumping more than ``MAX_JUMP_PCT`` vs the last good value is *suspect*
  and must be confirmed by a second source before entering ``prices``;
* several sources quoting the same symbol in one collect cycle are summarised
  by :func:`dispersion_summary` — the cross-provider quote uncertainty of
  Addendum 3, measured at collection time.
"""
from __future__ import annotations

import hashlib
import math
import statistics
from datetime import datetime, timezone
from typing import Mapping, Optional, Sequence

MAX_JUMP_PCT = 15.0          # > this vs last good => needs a second source
CONFIRM_TOLERANCE_PCT = 3.0  # two sources within this range confirm each other
MAD_THRESHOLD = 8.0          # robust z-score threshold vs the recent window
MAX_ABS_PREMIUM_PCT = 25.0   # |observed vs theoretical 18k premium| beyond this => suspect

# Plausibility ranges for normalized values (unit sanity checks).
SANITY_RANGES: dict[str, tuple[float, float]] = {
    "IR_GOLD_18K": (1e5, 1e9),      # IRT per gram
    "IR_COIN_EMAMI": (1e6, 1e12),   # IRT per coin
    "USD_IRT": (1e3, 1e7),          # IRT per USD
    "XAUUSD": (500.0, 20_000.0),    # USD per ozt
    "XAGUSD": (5.0, 500.0),         # USD per ozt
    "BRENT_OIL": (10.0, 500.0),     # USD per bbl
    "DXY": (50.0, 200.0),
    "US10Y": (0.0, 25.0),           # percent
    # TSE gold fund units (IRT/unit): wide bounds — funds split units and NAVs vary
    "IR_GOLD_FUND_AYAR": (1e2, 1e8),
    "IR_GOLD_FUND_TALA": (1e2, 1e8),
    "IR_GOLD_FUND_KAHRABA": (1e2, 1e8),
    "IR_GOLD_FUND_FLOW": (-100.0, 100.0),  # retail net flow, % of volume
}

# Oscillating series where sign flips between sessions are normal behaviour.
OSCILLATING_SYMBOLS = frozenset({"IR_GOLD_FUND_FLOW"})


def build_dedupe_key(provider: str, symbol: str, observed_at: datetime, value: float) -> str:
    """sha256 over ``provider|symbol|observed_at_iso|value`` (deterministic)."""
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    iso = observed_at.astimezone(timezone.utc).isoformat()
    payload = f"{provider}|{symbol}|{iso}|{format(float(value), '.10g')}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def sanity_ok(symbol: str, value: float) -> bool:
    """Reject obviously-wrong magnitudes (unit mix-ups)."""
    lo, hi = SANITY_RANGES.get(symbol, (0.0, float("inf")))
    return lo <= value <= hi


def is_mad_outlier(
    value: float, window: Sequence[float], threshold: float = MAD_THRESHOLD
) -> bool:
    """Median-absolute-deviation outlier test vs a recent window of good values."""
    vals = [float(v) for v in window]
    if len(vals) < 5:
        return False
    med = statistics.median(vals)
    mad = statistics.median(abs(v - med) for v in vals)
    scale = 1.4826 * mad if mad > 0 else max(abs(med) * 1e-4, 1e-12)
    return abs(value - med) / scale > threshold


def jump_pct(value: float, last_good: float) -> float:
    """Absolute percentage change vs the last accepted value."""
    if last_good == 0:
        return 0.0
    return abs(value - last_good) / abs(last_good) * 100.0


def values_agree(a: float, b: float, tolerance_pct: float = CONFIRM_TOLERANCE_PCT) -> bool:
    """Do two independent sources confirm each other?"""
    if a == 0 and b == 0:
        return True
    ref = max(abs(a), abs(b))
    return abs(a - b) / ref * 100.0 <= tolerance_pct


def dispersion_summary(
    values_by_source: Mapping[str, float],
    tolerance_pct: float = CONFIRM_TOLERANCE_PCT,
) -> Optional[dict]:
    """Summarise how far several sources disagree about one symbol.

    Returns ``None`` for fewer than two usable quotes: a single source has
    *unknown* dispersion, not zero dispersion, and a 0.0 would let a lonely
    provider look like perfect cross-source agreement.  Percentages are taken
    against ``|median|`` so the numbers compare across symbols priced in
    toman, USD or percent; they are ``None`` when the median is 0 (an
    oscillating series such as ``IR_GOLD_FUND_FLOW``), where percent-of-median
    carries no meaning.

    ``values`` keeps the per-source NORMALIZED quotes: ``raw_observations``
    stores each provider's raw number (TGJU rials next to BrsApi tomans), so
    the canonical values must travel with the summary to stay comparable.
    """
    usable = {
        str(source): float(value)
        for source, value in values_by_source.items()
        if isinstance(value, (int, float)) and math.isfinite(float(value))
    }
    if len(usable) < 2:
        return None
    values = sorted(usable.values())
    med = statistics.median(values)
    spread = values[-1] - values[0]
    mad = statistics.median(abs(v - med) for v in values)
    ref = abs(med)
    return {
        "n_sources": len(values),
        "n_agreeing": sum(1 for v in values if values_agree(v, med, tolerance_pct)),
        "values": {src: round(val, 6) for src, val in sorted(usable.items())},
        "median": round(med, 6),
        "spread_abs": round(spread, 6),
        "spread_pct": round(spread / ref * 100.0, 4) if ref > 0 else None,
        "mad": round(mad, 6),
        "mad_pct": round(mad / ref * 100.0, 4) if ref > 0 else None,
        "tolerance_pct": tolerance_pct,
    }


def premium_suspect(
    observed_18k_irt: float, xau_usd: float, usd_irt: float
) -> Optional[str]:
    """Cross-check an 18k quote against the theoretical price.

    The observed premium is normally within a few percent (typically ±3%);
    beyond ``MAX_ABS_PREMIUM_PCT`` the quote is almost certainly a unit or
    parsing error.  Returns a reason string when suspect, else None.
    """
    from .formula import k18_theoretical_irt, premium_pct

    theo = k18_theoretical_irt(xau_usd, usd_irt)
    if theo <= 0:
        return None
    prem = premium_pct(observed_18k_irt, theo)
    if abs(prem) > MAX_ABS_PREMIUM_PCT:
        return (
            f"premium vs theoretical is {prem:.1f}% "
            f"(|premium| > {MAX_ABS_PREMIUM_PCT}%)"
        )
    return None


def classify_observation(
    symbol: str,
    value: float,
    recent_window: Sequence[float],
    last_good: Optional[float],
) -> tuple[str, Optional[str]]:
    """Classify a normalized value as ``ok`` / ``suspect`` / rejected (``outlier``).

    Returns ``(quality, reason)``.  ``suspect`` values are stored in
    ``raw_observations`` but only promoted to ``prices`` when confirmed by a
    second source (handled by the collect job).
    """
    if not sanity_ok(symbol, value):
        return "outlier", f"value {value} outside plausible range for {symbol}"
    if symbol in OSCILLATING_SYMBOLS:
        # sign-flipping percent series: relative-jump and MAD tests assume a
        # slowly drifting level and would strand every swing as a suspect
        # that a single-source symbol can never confirm
        return "ok", None
    if last_good is not None and jump_pct(value, last_good) > MAX_JUMP_PCT:
        return (
            "suspect",
            f"jump of {jump_pct(value, last_good):.1f}% vs last good value {last_good}",
        )
    if is_mad_outlier(value, recent_window):
        return "suspect", "deviates strongly from recent window median (MAD test)"
    return "ok", None
