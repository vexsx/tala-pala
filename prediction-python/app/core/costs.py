"""Single source of truth for the round-trip trading cost (Addendum 15).

Before this module the system carried FOUR different hurdles at once: the
signal engine's hardcoded 2.2%, the custom forecast's 2.2%, the backtest's
1.65% entry rule, and the UI's live dealer spread (~0.49%). The headline
signal could therefore say "below the ~2.2% cost threshold" for the very move
the action planner on the same page called "favors buying".

The honest cost is what a real round trip actually pays: buy at the dealer's
sell price, later sell at their buy price. Hamrah Gold publishes both sides,
so the observed spread IS that cost, and it is stored on every observation.

Resolution order:
  1. the most recent observed dealer spread (<= MAX_AGE_HOURS old);
  2. otherwise the conservative fixed assumption, clearly flagged as such.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..db import ensure_utc, raw_observations, utcnow

log = logging.getLogger(__name__)

# Conservative fallback: fee both sides + dealer spread + slippage both sides.
DEFAULT_FEE_PCT = 0.5
DEFAULT_SPREAD_PCT = 1.0
DEFAULT_SLIPPAGE_PCT = 0.1
FALLBACK_ROUND_TRIP_COST_PCT = (
    2 * DEFAULT_FEE_PCT + DEFAULT_SPREAD_PCT + 2 * DEFAULT_SLIPPAGE_PCT
)

SPREAD_PROVIDER = "hamrahgold"
MAX_AGE_HOURS = 72
# Sanity band: a spread outside this range is a parsing accident, not a quote.
MIN_SANE_PCT = 0.1
MAX_SANE_PCT = 10.0


# How many recent rows to scan for a usable quote. The newest row can be a
# suspect/outlier or a malformed payload; an older GOOD quote is far better
# evidence than the blanket 2.2% assumption, so we walk back a bounded window
# instead of giving up on the first bad row.
SCAN_LIMIT = 25


@dataclass(frozen=True)
class CostResolution:
    """Full provenance of the round-trip cost hurdle used by the decision layer."""

    cost_pct: float
    basis: str                      # "observed_spread" | "assumed"
    source: Optional[str]           # provider code when observed
    observed_at: Optional[object]   # UTC datetime of the quote used
    age_hours: Optional[float]
    reason: str                     # why this basis was chosen (audit trail)
    rejected: int = 0               # rows skipped before an acceptable one


def resolve_cost(engine: Engine) -> CostResolution:
    """Resolve the round-trip cost with full provenance.

    Only ``quality='ok'`` raw observations are eligible. This is load-bearing:
    the classifier marks MAD-failing rows ``suspect``/``outlier`` and they are
    deliberately kept out of ``prices``, but the spread lives on the RAW row —
    so without this filter a rejected quote could still set the hurdle that
    gates every buy/sell recommendation.
    """
    now = utcnow()
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    stmt = (
        select(raw_observations.c.raw_payload, raw_observations.c.observed_at)
        .where(
            raw_observations.c.provider_code == SPREAD_PROVIDER,
            raw_observations.c.quality == "ok",
            raw_observations.c.observed_at >= cutoff,
        )
        .order_by(raw_observations.c.observed_at.desc())
        .limit(SCAN_LIMIT)
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except Exception as exc:  # noqa: BLE001 — cost lookup must never sink a job
        log.warning("spread lookup failed: %s", exc)
        return CostResolution(
            FALLBACK_ROUND_TRIP_COST_PCT, "assumed", None, None, None,
            "spread lookup failed; using the conservative assumption",
        )

    rejected = 0
    for payload, observed_at in rows:
        if not isinstance(payload, dict):
            rejected += 1
            continue
        value = payload.get("spread_pct")
        if not isinstance(value, (int, float)):
            rejected += 1
            continue
        value = float(value)
        if not (MIN_SANE_PCT <= value <= MAX_SANE_PCT):
            rejected += 1  # parsing accident, not a quote
            continue
        observed_at = ensure_utc(observed_at)
        age = (now - observed_at).total_seconds() / 3600.0
        return CostResolution(
            value, "observed_spread", SPREAD_PROVIDER, observed_at, round(age, 2),
            f"observed {SPREAD_PROVIDER} buy/sell spread, {age:.1f}h old"
            + (f" ({rejected} newer row(s) skipped as unusable)" if rejected else ""),
            rejected,
        )

    reason = (
        f"no usable {SPREAD_PROVIDER} spread in the last {MAX_AGE_HOURS}h"
        if not rows
        else f"all {len(rows)} recent {SPREAD_PROVIDER} rows were unusable"
    )
    return CostResolution(
        FALLBACK_ROUND_TRIP_COST_PCT, "assumed", None, None, None, reason, rejected
    )


def observed_spread_pct(engine: Engine) -> Optional[float]:
    """Most recent USABLE observed dealer buy/sell spread in percent, or None."""
    res = resolve_cost(engine)
    return res.cost_pct if res.basis == "observed_spread" else None


def round_trip_cost_pct(engine: Engine) -> tuple[float, str]:
    """``(cost_pct, basis)`` where basis is ``observed_spread`` or ``assumed``."""
    res = resolve_cost(engine)
    return res.cost_pct, res.basis
