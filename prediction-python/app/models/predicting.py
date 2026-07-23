"""Prediction pass: load the active model per horizon, forecast, persist.

Every written prediction carries empirical interval bounds, a flat-band
direction (±0.15%), a confidence heuristic (validation directional accuracy
blended with interval tightness), the detected regime, drivers, and data
freshness / warnings.  All wording is hedged — forecasts are uncertain
estimates, never guarantees.

Live calibration (jobs/evaluate.py writes ``app_settings['live_calibration']``
from matured predictions) feeds back into every new prediction:

* confidence is blended toward the live directional hit rate as evidence
  accumulates (:func:`blended_confidence`);
* intervals that recently under-covered are widened and flagged
  (:func:`coverage_widening`);
* an active ensemble is re-weighted by live per-member sMAPE once every
  member has enough matured predictions (models/ensemble.py).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

import joblib
import numpy as np
from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..config import Settings
from ..core.freshness import is_acceptably_fresh, is_fresh, staleness_minutes
from ..core.market_hours import TEHRAN
from ..db import app_settings, ensure_utc, model_versions, predictions, prices, utcnow
from ..metrics import PREDICTION_DURATION
from .base import ForecastModel
from .ensemble import EnsembleModel, inverse_smape_weights, live_member_smapes
from .intervals import DEFAULT_ALPHA, adaptive_alpha, empirical_interval
from .metagate import META_GATE_KEY, apply_meta_gate
from .training import CONTEXT_SYMBOLS, HORIZON_SPECS, detect_regime, load_series

log = logging.getLogger(__name__)

FLAT_BAND_PCT = 0.15  # |expected change| below this => 'flat'

# --- live calibration (see jobs/evaluate.py, key 'live_calibration') --------
LIVE_CAL_KEY = "live_calibration"
LIVE_CAL_WINDOW = 60        # denominator of the blend weight shrinkage
MIN_COVERAGE_N = 20         # matured predictions before coverage is trusted
COVERAGE_FLOOR = 0.75       # below this the interval is widened + flagged
NOMINAL_COVERAGE = 0.90     # the intervals' nominal coverage (alpha=0.1)
MAX_WIDEN_FACTOR = 1.5      # cap on the interval widening multiplier
UNDER_COVERAGE_WARNING = (
    "prediction intervals recently under-covered; widen expectations"
)


# --- provider gap (quote uncertainty across Iranian sources) ----------------
PROVIDER_GAP_WINDOW_MIN = 120  # look-back for "current" quotes per provider
PROVIDER_GAP_WARN_PCT = 1.0    # gaps above this widen the interval + warn


def provider_gap_pct(
    engine: Engine, symbol: str = "IR_GOLD_18K", window_minutes: int = PROVIDER_GAP_WINDOW_MIN
) -> Optional[float]:
    """Percent spread between providers' latest quotes for ``symbol``.

    Latest good observation per source within the window; gap = (max - min) /
    median * 100. Returns None with fewer than two quoting providers. This is
    *quote* uncertainty (bid/ask handling, update cadence, retail vs wholesale
    pricing differ across Iranian sources) — orthogonal to model uncertainty,
    so predictions add it to their interval half-width.
    """
    since = utcnow() - timedelta(minutes=window_minutes)
    stmt = (
        select(prices.c.source, prices.c.value, prices.c.observed_at)
        .where(
            prices.c.symbol == symbol,
            prices.c.quality == "ok",
            prices.c.observed_at >= since,
        )
        .order_by(prices.c.observed_at.desc())
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    latest_per_source: dict[str, float] = {}
    for source, value, _observed in rows:  # newest first -> first hit wins
        if source not in latest_per_source:
            latest_per_source[source] = float(value)
    values = sorted(latest_per_source.values())
    if len(values) < 2:
        return None
    mid = float(np.median(values))
    if mid <= 0:
        return None
    return (values[-1] - values[0]) / mid * 100.0


def _target_time(horizon: str, now: datetime) -> datetime:
    if horizon == "1h":
        return now + timedelta(hours=1)
    if horizon == "4h":
        return now + timedelta(hours=4)
    if horizon == "eod":
        # End of the TEHRAN day (this is an Iranian-market forecast); the old
        # UTC midnight target was 03:29 the next Tehran morning.
        local = now.astimezone(TEHRAN)
        end = local.replace(hour=23, minute=59, second=59, microsecond=0)
        if end <= local:
            end += timedelta(days=1)
        return end.astimezone(timezone.utc)
    days = {"1d": 1, "3d": 3, "7d": 7, "30d": 30}[horizon]
    return now + timedelta(days=days)


def _direction(expected_change_pct: float) -> str:
    if abs(expected_change_pct) <= FLAT_BAND_PCT:
        return "flat"
    return "up" if expected_change_pct > 0 else "down"


def _confidence(dir_acc: float, rel_width: float) -> float:
    """Blend validation directional accuracy with interval tightness (0..1)."""
    tightness = 1.0 / (1.0 + 10.0 * max(rel_width, 0.0))
    return float(np.clip(0.55 * dir_acc + 0.45 * tightness, 0.05, 0.95))


def load_live_calibration(engine: Engine) -> dict:
    """Read ``app_settings['live_calibration']`` (written by jobs/evaluate.py).

    Returns ``{horizon: {"n", "dir_hit_rate", "coverage", "updated_at"}}``,
    or ``{}`` when the row is missing/malformed (fail open: no live evidence
    means validation-time behaviour).
    """
    with engine.connect() as conn:
        row = conn.execute(
            select(app_settings.c.value).where(app_settings.c.key == LIVE_CAL_KEY)
        ).first()
    return dict(row[0]) if row is not None and isinstance(row[0], dict) else {}


MIN_REGIME_N = 10  # matured predictions in a regime before its stats are used


def blended_confidence(
    validation_confidence: float, live: Optional[dict], regime: Optional[str] = None
) -> float:
    """Calibrate confidence against live outcomes, preferring same-regime stats.

    ``final = w * validation_confidence + (1 - w) * live_dir_hit_rate`` with
    ``w = max(0.3, 1 - n/60)`` where ``n`` is the number of matured live
    predictions behind ``live_dir_hit_rate``.  With ``n = 0`` (or no live
    stats) ``w = 1`` and the result equals today's validation-only
    confidence; as evidence accumulates the weight shifts toward reality,
    floored at ``w = 0.3`` so validation always keeps a vote.

    When per-regime stats exist for the CURRENT regime with at least
    ``MIN_REGIME_N`` matured predictions they replace the overall hit rate —
    the market's behaviour (and the models' skill) differ by regime, so the
    system leans on what it has learned about conditions like today's.
    The result is clamped to ``[0.05, 0.95]``.
    """
    blended = float(validation_confidence)
    if live:
        n = int(live.get("n") or 0)
        hit_rate = live.get("dir_hit_rate")
        regime_stats = (live.get("by_regime") or {}).get(regime) if regime else None
        if regime_stats and int(regime_stats.get("n") or 0) >= MIN_REGIME_N:
            n = int(regime_stats["n"])
            hit_rate = regime_stats.get("dir_hit_rate")
        if n > 0 and hit_rate is not None:
            w = max(0.3, 1.0 - n / LIVE_CAL_WINDOW)
            blended = w * blended + (1.0 - w) * float(hit_rate)
    return float(np.clip(blended, 0.05, 0.95))


def load_meta_gate(engine: Engine) -> Optional[dict]:
    """Read ``app_settings['meta_gate']`` (written by jobs/evaluate.py)."""
    with engine.connect() as conn:
        row = conn.execute(
            select(app_settings.c.value).where(app_settings.c.key == META_GATE_KEY)
        ).first()
    return dict(row[0]) if row is not None and isinstance(row[0], dict) else None


def coverage_widening(live: Optional[dict]) -> float:
    """Interval half-width multiplier from live coverage.

    If the live coverage of the nominal 90% interval dropped below
    ``COVERAGE_FLOOR`` (0.75) with at least ``MIN_COVERAGE_N`` (20) matured
    predictions, the half-width is multiplied by ``0.90 / coverage``, capped
    at ``MAX_WIDEN_FACTOR`` (1.5).  Returns 1.0 (no widening) otherwise.
    """
    if not live:
        return 1.0
    n = int(live.get("n") or 0)
    cov = live.get("coverage")
    if cov is None or n < MIN_COVERAGE_N:
        return 1.0
    cov = float(cov)
    if cov >= COVERAGE_FLOOR:
        return 1.0
    if cov <= 0.0:
        return MAX_WIDEN_FACTOR
    return float(min(NOMINAL_COVERAGE / cov, MAX_WIDEN_FACTOR))


def _drivers(model: ForecastModel, series, regime: str) -> list[dict]:
    importances = model.feature_importances()
    if importances:
        return [
            {"factor": name, "importance": round(weight, 4)}
            for name, weight in importances[:5]
        ]
    # heuristic drivers for series models
    values = series.astype(float)
    drivers: list[dict] = []
    if len(values) >= 11:
        momentum = float(values.iloc[-1] / values.iloc[-11] - 1.0) * 100.0
        drivers.append(
            {"factor": "momentum_10", "note": f"{momentum:+.2f}% over 10 steps"}
        )
    if len(values) >= 20:
        sma20 = float(values.iloc[-20:].mean())
        rel = (float(values.iloc[-1]) / sma20 - 1.0) * 100.0
        drivers.append({"factor": "price_vs_sma20", "note": f"{rel:+.2f}% vs SMA20"})
    drivers.append({"factor": "regime", "note": regime})
    return drivers


def _load_active(engine: Engine, symbol: str, horizon: str) -> Optional[dict]:
    stmt = (
        select(model_versions)
        .where(
            model_versions.c.symbol == symbol,
            model_versions.c.horizon == horizon,
            model_versions.c.is_active.is_(True),
        )
        .order_by(model_versions.c.trained_at.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    return dict(row._mapping) if row else None


def _latest_observation(engine: Engine, symbol: str) -> Optional[dict]:
    stmt = (
        select(prices.c.observed_at, prices.c.value)
        .where(prices.c.symbol == symbol, prices.c.quality == "ok")
        .order_by(prices.c.observed_at.desc())
        .limit(1)
    )
    with engine.connect() as conn:
        row = conn.execute(stmt).first()
    if row is None:
        return None
    return {"observed_at": ensure_utc(row[0]), "value": float(row[1])}


def predict_all(
    engine: Engine,
    settings: Settings,
    horizons: Optional[Sequence[str]] = None,
    symbols: Optional[Sequence[str]] = None,
) -> dict:
    """Run predictions for the requested symbols x horizons; rows + errors."""
    from .training import FORECAST_SYMBOLS

    requested = [h for h in (horizons or list(HORIZON_SPECS)) if h in HORIZON_SPECS]
    req_symbols = [s for s in (symbols or FORECAST_SYMBOLS) if s in FORECAST_SYMBOLS]
    out: list[dict] = []
    errors: list[str] = []
    with PREDICTION_DURATION.time():
        for symbol in req_symbols:
            series_cache: dict = {}
            for horizon in requested:
                try:
                    row = _predict_one(engine, settings, symbol, horizon, series_cache)
                    if isinstance(row, dict):
                        out.append(row)
                    else:
                        errors.append(f"{symbol}/{horizon}: {row}")
                except Exception as exc:
                    log.exception("predict %s/%s failed", symbol, horizon)
                    errors.append(f"{symbol}/{horizon}: {type(exc).__name__}: {exc}")
    return {"predictions": out, "errors": errors}


def _predict_one(
    engine: Engine, settings: Settings, symbol: str, horizon: str, series_cache: dict
):
    freq, steps = HORIZON_SPECS[horizon]
    active = _load_active(engine, symbol, horizon)
    if active is None:
        return "no active model (run /internal/train first)"
    artifact_path = active.get("artifact_path")
    if not artifact_path or not os.path.exists(artifact_path):
        return f"artifact missing at {artifact_path!r}"
    artifact = joblib.load(artifact_path)
    model: ForecastModel = artifact["model"]
    residuals: list[float] = list(artifact.get("residual_pcts", []))

    if freq not in series_cache:
        series_cache[freq] = load_series(engine, symbol, freq)
    series = series_cache[freq]
    if series.empty:
        return "no price series available"

    warnings: list[str] = [
        "Forecast is an uncertain estimate based on historical patterns, "
        "not a guarantee and not financial advice."
    ]
    latest = _latest_observation(engine, symbol)
    now = utcnow()
    # market-hours aware (Addendum 1): during a closure last-session data
    # still counts as fresh and only carries an informational note
    data_fresh = bool(
        latest
        and is_acceptably_fresh(symbol, latest["observed_at"], now, settings)
    )
    if not data_fresh:
        age = staleness_minutes(latest["observed_at"], now) if latest else None
        warnings.append(
            "Input data is stale"
            + (f" (last observation {age:.0f} minutes old)" if age is not None else "")
            + "; treat this forecast with extra caution."
        )
    elif latest and not is_fresh(latest["observed_at"], settings.stale_minutes, now):
        warnings.append("prices from last session (market closed)")

    # adaptive ensemble: once every member has enough matured live
    # predictions, re-weight by inverse live sMAPE instead of validation sMAPE
    if isinstance(model, EnsembleModel):
        live_smapes = live_member_smapes(engine, horizon, list(model.members), symbol=symbol)
        if live_smapes:
            model.weights = inverse_smape_weights(live_smapes)
            log.info("%s: ensemble re-weighted from live sMAPE %s", horizon, live_smapes)

    # refit on the freshest series (cheap for all model families used here);
    # exog-aware models get the auxiliary series via set_context
    from .training import SYMBOL_CONTEXTS

    aux_key = f"aux_{freq}"
    if aux_key not in series_cache:
        series_cache[aux_key] = {
            key: load_series(engine, ctx_sym, freq)
            for key, ctx_sym in SYMBOL_CONTEXTS.get(symbol, {}).items()
        }
    model.set_context(series_cache[aux_key])
    model.fit(series, steps)
    point = float(model.predict_point())
    last_price = float(series.iloc[-1])

    cal_all = load_live_calibration(engine)
    live_cal = (cal_all.get(symbol) or {}).get(horizon)
    if live_cal is None and symbol == "IR_GOLD_18K":
        legacy = cal_all.get(horizon)  # pre-multi-symbol flat layout
        live_cal = legacy if isinstance(legacy, dict) and "n" in legacy else None

    # a model exposing its own interval (e.g. quantile_gbr) takes precedence
    # over the empirical residual-quantile interval
    native = model.predict_interval()
    if native is not None:
        lower, upper = float(native[0]), float(native[1])
        if lower > upper:
            lower, upper = upper, lower
        lower, upper = min(lower, point), max(upper, point)
        # native intervals have no residual quantiles to re-level, so live
        # under-coverage is corrected multiplicatively
        widen = coverage_widening(live_cal)
        if widen > 1.0:
            lower = point - (point - lower) * widen
            upper = point + (upper - point) * widen
            warnings.append(UNDER_COVERAGE_WARNING)
    else:
        # adaptive conformal (ACI-style): live coverage feedback re-levels the
        # residual quantiles instead of scaling widths — self-correcting and
        # better calibrated than a fixed multiplier
        alpha_eff = adaptive_alpha(
            (live_cal or {}).get("coverage"), int((live_cal or {}).get("n") or 0)
        )
        lower, upper = empirical_interval(point, residuals, alpha_eff)
        if alpha_eff < DEFAULT_ALPHA:
            warnings.append(UNDER_COVERAGE_WARNING)

    # provider gap: when Iranian sources disagree materially on the current
    # price, that quote uncertainty is added to the interval half-width
    gap_pct = provider_gap_pct(engine) if symbol == "IR_GOLD_18K" else None
    if gap_pct is not None and gap_pct >= PROVIDER_GAP_WARN_PCT:
        half_gap = gap_pct / 2.0 / 100.0 * point
        lower -= half_gap
        upper += half_gap
        warnings.append(
            f"Iranian data providers currently disagree by {gap_pct:.1f}% on the "
            "18k price; the interval was widened to reflect this quote uncertainty."
        )

    expected_change_pct = (point / last_price - 1.0) * 100.0
    direction = _direction(expected_change_pct)
    regime = detect_regime(series)
    metrics = active.get("metrics") or {}
    dir_acc = float(metrics.get("directional_accuracy", 0.5))
    rel_width = (upper - lower) / point if point else 1.0
    confidence = blended_confidence(_confidence(dir_acc, rel_width), live_cal, regime)
    n_folds = int(metrics.get("n_folds", 0))
    if n_folds and n_folds < 20:
        warnings.append(f"Model validated on only {n_folds} walk-forward folds.")

    predicted_at = now
    target_time = _target_time(horizon, now)
    drivers = _drivers(model, series, regime)

    # meta-gate (meta-labeling): the system's learned self-assessment — a
    # secondary model trained on this app's own matured predictions estimates
    # P(this direction call is right) and pulls confidence toward it
    gate = load_meta_gate(engine)
    if gate and direction != "flat":
        p_hit = apply_meta_gate(
            gate, point, lower, upper, expected_change_pct,
            confidence, horizon, regime, data_fresh,
        )
        if p_hit is not None:
            confidence = float(np.clip(0.5 * confidence + 0.5 * p_hit, 0.05, 0.95))
            drivers.append({
                "factor": "self_assessment",
                "note": (
                    f"learned P(direction hit)={p_hit:.2f} from "
                    f"{int(gate.get('n', 0))} of this system's own past predictions"
                ),
            })
            if p_hit < 0.45:
                warnings.append(
                    "The system's self-assessment (learned from its own past "
                    "predictions) rates this call below coin-flip reliability."
                )

    with engine.begin() as conn:
        row_id = conn.execute(
            predictions.insert().values(
                symbol=symbol,
                horizon=horizon,
                model_version_id=active["id"],
                model_name=active["model_name"],
                predicted_at=predicted_at,
                target_time=target_time,
                point_forecast=point,
                lower_bound=lower,
                upper_bound=upper,
                expected_change_pct=expected_change_pct,
                direction=direction,
                confidence=confidence,
                regime=regime,
                drivers=drivers,
                data_fresh=data_fresh,
                warnings=warnings,
                created_at=now,
            )
        ).inserted_primary_key[0]

    return {
        "id": int(row_id),
        "symbol": symbol,
        "horizon": horizon,
        "model_name": active["model_name"],
        "predicted_at": predicted_at.isoformat(),
        "target_time": target_time.isoformat(),
        "point_forecast": round(point, 2),
        "lower_bound": round(lower, 2),
        "upper_bound": round(upper, 2),
        "expected_change_pct": round(expected_change_pct, 4),
        "direction": direction,
        "confidence": round(confidence, 3),
        "regime": regime,
        "drivers": drivers,
        "data_fresh": data_fresh,
        "warnings": warnings,
    }
