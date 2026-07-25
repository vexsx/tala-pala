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
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..db import raw_observations, utcnow

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


def observed_spread_pct(engine: Engine) -> Optional[float]:
    """Most recent observed dealer buy/sell spread in percent, or None."""
    cutoff = utcnow() - __import__("datetime").timedelta(hours=MAX_AGE_HOURS)
    stmt = (
        select(raw_observations.c.raw_payload)
        .where(
            raw_observations.c.provider_code == SPREAD_PROVIDER,
            raw_observations.c.observed_at >= cutoff,
        )
        .order_by(raw_observations.c.observed_at.desc())
        .limit(1)
    )
    try:
        with engine.connect() as conn:
            row = conn.execute(stmt).first()
    except Exception as exc:  # noqa: BLE001 — cost lookup must never sink a job
        log.warning("spread lookup failed: %s", exc)
        return None
    if not row or not isinstance(row[0], dict):
        return None
    value = row[0].get("spread_pct")
    if not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if MIN_SANE_PCT <= value <= MAX_SANE_PCT else None


def round_trip_cost_pct(engine: Engine) -> tuple[float, str]:
    """``(cost_pct, basis)`` where basis is ``observed_spread`` or ``assumed``."""
    live = observed_spread_pct(engine)
    if live is not None:
        return live, "observed_spread"
    return FALLBACK_ROUND_TRIP_COST_PCT, "assumed"
