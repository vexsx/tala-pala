"""Data staleness checks (STALE_MINUTES contract).

:func:`is_fresh` is the plain age rule.  The market-hours-aware variants
(Addendum 1) live in :mod:`app.core.market_hours` and are re-exported here so
callers keep a single import point: during a market closure, data from the
last session still counts as acceptably fresh.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .market_hours import (  # noqa: F401  (re-exported per module docstring)
    closure_started_at,
    is_acceptably_fresh,
    is_market_open,
)


def is_fresh(
    observed_at: Optional[datetime],
    stale_minutes: int,
    now: Optional[datetime] = None,
) -> bool:
    """True when the observation is younger than ``stale_minutes``."""
    if observed_at is None:
        return False
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - observed_at) <= timedelta(minutes=stale_minutes)


def staleness_minutes(
    observed_at: Optional[datetime], now: Optional[datetime] = None
) -> Optional[float]:
    """Age of an observation in minutes (None when unknown)."""
    if observed_at is None:
        return None
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return (now - observed_at).total_seconds() / 60.0
