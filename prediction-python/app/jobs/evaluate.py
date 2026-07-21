"""Evaluation job: fill ``predictions.actual_value`` for matured predictions.

For each prediction whose ``target_time`` has passed and whose actual is not
yet recorded, the nearest good observation within a horizon-dependent
tolerance is used.  Returns a live-accuracy summary.

After the fill pass the job also refreshes **live calibration**: per-horizon
rolling stats over the last up-to-:data:`LIVE_CAL_WINDOW` matured predictions
(directional hit rate + coverage of the nominal 90% interval), persisted to
``app_settings`` under key ``'live_calibration'`` as::

    {"<horizon>": {"n": int, "dir_hit_rate": float, "coverage": float,
                   "updated_at": "<iso UTC>"}, ...}

``models/predicting.py`` blends these live stats into each new prediction's
confidence and widens under-covering intervals.
"""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Optional

import numpy as np
from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import app_settings, ensure_utc, predictions, prices, utcnow
from ..metrics import JOB_LAST_SUCCESS

log = logging.getLogger(__name__)

LIVE_CAL_KEY = "live_calibration"
LIVE_CAL_WINDOW = 60  # most recent matured predictions considered per horizon

# horizon -> tolerance for matching the actual observation to target_time
TOLERANCES: dict[str, timedelta] = {
    "1h": timedelta(minutes=45),
    "4h": timedelta(hours=2),
    "eod": timedelta(hours=36),
    "1d": timedelta(hours=36),
    "3d": timedelta(hours=36),
    "7d": timedelta(hours=48),
    "30d": timedelta(hours=72),
}
DEFAULT_TOLERANCE = timedelta(hours=36)


def _nearest_price(
    engine: Engine, symbol: str, target, tolerance: timedelta
) -> Optional[tuple[float, object]]:
    stmt = (
        select(prices.c.value, prices.c.observed_at)
        .where(
            prices.c.symbol == symbol,
            prices.c.quality == "ok",
            prices.c.observed_at >= target - tolerance,
            prices.c.observed_at <= target + tolerance,
        )
        .order_by(prices.c.observed_at)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    if not rows:
        return None
    best = min(rows, key=lambda r: abs((ensure_utc(r[1]) - target).total_seconds()))
    return float(best[0]), ensure_utc(best[1])


def _recover_base(point: float, expected_change_pct: float) -> Optional[float]:
    """Recover the base price a forecast was made from (see fill loop)."""
    if expected_change_pct <= -100:
        return None
    base = point / (1.0 + expected_change_pct / 100.0)
    return base if base else None


def compute_live_calibration(engine: Engine, window: int = LIVE_CAL_WINDOW) -> dict:
    """Per-horizon rolling live stats over the last up-to-``window`` matured
    predictions.

    * ``dir_hit_rate`` — fraction where the predicted direction (sign of
      ``point - base``) matched the realized direction (sign of
      ``actual - base``);
    * ``coverage`` — fraction where ``lower_bound <= actual <= upper_bound``
      (empirical coverage of the nominal 90% interval).

    Layout (Addendum 8): nested ``{symbol: {horizon: stats}}`` — one stats
    block per forecast symbol so IR_GOLD_18K and XAUUSD never mix.
    """
    out: dict[str, dict] = {}
    now_iso = utcnow().isoformat()
    with engine.connect() as conn:
        pairs = [
            (s, h)
            for (s, h) in conn.execute(
                select(predictions.c.symbol, predictions.c.horizon)
                .where(predictions.c.actual_value.is_not(None))
                .distinct()
            )
        ]
        for symbol, horizon in sorted(pairs):
            rows = conn.execute(
                select(
                    predictions.c.point_forecast,
                    predictions.c.lower_bound,
                    predictions.c.upper_bound,
                    predictions.c.expected_change_pct,
                    predictions.c.actual_value,
                    predictions.c.regime,
                )
                .where(
                    predictions.c.symbol == symbol,
                    predictions.c.horizon == horizon,
                    predictions.c.actual_value.is_not(None),
                )
                .order_by(predictions.c.target_time.desc())
                .limit(window)
            ).all()
            if not rows:
                continue
            dir_hits: list[bool] = []
            covered: list[bool] = []
            regime_hits: dict[str, list[bool]] = {}
            for point, lower, upper, expected_pct, actual, regime in rows:
                point, actual = float(point), float(actual)
                covered.append(float(lower) <= actual <= float(upper))
                base = _recover_base(point, float(expected_pct))
                if base:
                    hit = bool(np.sign(point - base) == np.sign(actual - base))
                    dir_hits.append(hit)
                    regime_hits.setdefault(str(regime), []).append(hit)
            # per-regime hit rates: the market behaves differently by regime,
            # so new predictions prefer the stats of THEIR regime when enough
            # evidence exists (see predicting.blended_confidence)
            by_regime = {
                regime: {
                    "n": len(hits),
                    "dir_hit_rate": round(float(np.mean(hits)), 4),
                }
                for regime, hits in regime_hits.items()
                if hits
            }
            out.setdefault(symbol, {})[horizon] = {
                "n": len(rows),
                "dir_hit_rate": round(float(np.mean(dir_hits)), 4) if dir_hits else None,
                "coverage": round(float(np.mean(covered)), 4),
                "by_regime": by_regime,
                "updated_at": now_iso,
            }
    return out


def upsert_setting(engine: Engine, key: str, value: dict) -> None:
    """Upsert one ``app_settings`` row (portable update-then-insert)."""
    now = utcnow()
    with engine.begin() as conn:
        result = conn.execute(
            update(app_settings)
            .where(app_settings.c.key == key)
            .values(value=value, updated_at=now)
        )
        if result.rowcount == 0:
            conn.execute(
                app_settings.insert().values(key=key, value=value, updated_at=now)
            )


def run_evaluate(engine: Engine, settings: Settings) -> dict:
    now = utcnow()
    stmt = (
        select(predictions)
        .where(predictions.c.actual_value.is_(None), predictions.c.target_time <= now)
        .order_by(predictions.c.target_time)
        .limit(500)
    )
    with engine.connect() as conn:
        pending = [dict(r._mapping) for r in conn.execute(stmt)]

    evaluated = 0
    unmatched = 0
    abs_pct_errors: list[float] = []
    direction_hits: list[bool] = []

    for row in pending:
        target = ensure_utc(row["target_time"])
        tolerance = TOLERANCES.get(str(row["horizon"]), DEFAULT_TOLERANCE)
        match = _nearest_price(engine, str(row["symbol"]), target, tolerance)
        if match is None:
            unmatched += 1
            continue
        actual, _observed_at = match
        with engine.begin() as conn:
            conn.execute(
                update(predictions)
                .where(predictions.c.id == row["id"])
                .values(actual_value=actual, actual_recorded_at=now)
            )
        evaluated += 1
        point = float(row["point_forecast"])
        if actual != 0:
            abs_pct_errors.append(abs(actual - point) / abs(actual) * 100.0)
        # recover the base price the forecast was made from
        base = _recover_base(point, float(row["expected_change_pct"]))
        if base:
            direction_hits.append(
                np.sign(point - base) == np.sign(actual - base)
            )

    calibration = compute_live_calibration(engine)
    if calibration:
        upsert_setting(engine, LIVE_CAL_KEY, calibration)

    # refit the meta-gate (the system's learned self-assessment) whenever
    # enough matured outcomes exist; stored as plain coefficients
    from ..models.metagate import META_GATE_KEY, fit_meta_gate

    gate = fit_meta_gate(engine)
    if gate:
        upsert_setting(engine, META_GATE_KEY, gate)
        log.info("meta_gate refit on %d matured predictions (base rate %.2f)",
                 gate["n"], gate["base_rate"])

    summary = {
        "evaluated": evaluated,
        "unmatched": unmatched,
        "pending_remaining": len(pending) - evaluated - unmatched,
        "live_mape_pct": round(float(np.mean(abs_pct_errors)), 4) if abs_pct_errors else None,
        "live_directional_accuracy": (
            round(float(np.mean(direction_hits)), 4) if direction_hits else None
        ),
        "live_calibration": calibration,
        "meta_gate": {"n": gate["n"], "base_rate": gate["base_rate"]} if gate else None,
    }
    JOB_LAST_SUCCESS.labels(job="evaluate").set(time.time())
    return summary
