"""On-demand forecast for an arbitrary N-day horizon ("decision horizon").

The scheduled pipeline trains one model per fixed horizon (1d/3d/7d/30d...).
This module answers "what about N days?" for any N the user types in the UI:

* walk-forward validates a *fast* candidate subset at exactly ``N`` daily
  steps (same folds, same naive-baseline gate as the nightly training),
* picks the winner, refits on the full series, produces a point forecast with
  an empirical residual interval (provider-gap widened, like the scheduled
  predictions),
* adds a hedged buy/hold/sell *lean* comparing the expected move against
  realistic round-trip costs.

Results are ephemeral: nothing is persisted, no artifact is written, and the
live-calibration loop is untouched. Costs default to the backtester's fee /
spread / slippage defaults.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.engine import Engine

from ..config import Settings
from .intervals import empirical_interval, relative_residuals
from .predicting import (
    PROVIDER_GAP_WARN_PCT,
    _confidence,
    _direction,
    _drivers,
    provider_gap_pct,
)
from .training import (
    MIN_DAILY_POINTS,
    detect_regime,
    evaluate_candidates,
    load_series,
    select_winner,
)

log = logging.getLogger(__name__)

MIN_DAYS = 1
MAX_DAYS = 90
CUSTOM_MAX_FOLDS = 25  # cheaper than the nightly 40 — this runs interactively

# Fast families only: interactive latency matters and the heavyweight members
# (rf/gbr/quantile_gbr/arima/sarimax) rarely beat these on this data scale.
FAST_CANDIDATES = ("naive", "sma", "ses", "theta", "holt_damped", "linear", "hist_gb")

# Round-trip trading cost defaults, mirroring app/backtest defaults.
DEFAULT_FEE_PCT = 0.5
DEFAULT_SPREAD_PCT = 1.0
DEFAULT_SLIPPAGE_PCT = 0.1


def predict_custom(
    engine: Engine,
    settings: Settings,
    days: int,
    fee_pct: Optional[float] = None,
    spread_pct: Optional[float] = None,
    slippage_pct: Optional[float] = None,
) -> dict:
    """Forecast IR_GOLD_18K ``days`` daily steps ahead; raises ValueError on bad input."""
    if not isinstance(days, int) or not (MIN_DAYS <= days <= MAX_DAYS):
        raise ValueError(f"days must be an integer between {MIN_DAYS} and {MAX_DAYS}")

    fee = DEFAULT_FEE_PCT if fee_pct is None else float(fee_pct)
    spread = DEFAULT_SPREAD_PCT if spread_pct is None else float(spread_pct)
    slippage = DEFAULT_SLIPPAGE_PCT if slippage_pct is None else float(slippage_pct)
    round_trip_cost_pct = 2.0 * fee + spread + slippage

    series = load_series(engine, "IR_GOLD_18K", "daily")
    if len(series) < MIN_DAILY_POINTS + days:
        raise ValueError(
            f"not enough daily history for a {days}-day horizon "
            f"({len(series)} points, need >= {MIN_DAILY_POINTS + days})"
        )

    results = evaluate_candidates(
        series, days, candidates=FAST_CANDIDATES, max_folds=CUSTOM_MAX_FOLDS
    )
    if not results:
        raise ValueError("no candidate model produced walk-forward folds")
    winner = select_winner(results)
    winner_res = results[winner]
    metrics = winner_res["metrics"]

    # Refit the winner on the full series (ensemble never appears here because
    # evaluate_candidates only adds it when >= 2 members beat naive — if it
    # does, fall back to its best member for the ephemeral refit).
    from .training import _build_final_model

    model = _build_final_model(winner, results, series, days)
    point = float(model.predict_point())
    last_price = float(series.iloc[-1])

    residuals = relative_residuals(
        [f.pred for f in winner_res["folds"]], [f.actual for f in winner_res["folds"]]
    )
    native = model.predict_interval()
    if native is not None:
        lower, upper = sorted((float(native[0]), float(native[1])))
        lower, upper = min(lower, point), max(upper, point)
    else:
        lower, upper = empirical_interval(point, residuals)

    warnings: list[str] = [
        "Forecast is an uncertain estimate based on historical patterns, "
        "not a guarantee and not financial advice."
    ]
    gap_pct = provider_gap_pct(engine)
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
    rel_width = (upper - lower) / point if point else 1.0
    dir_acc = float(metrics.get("directional_accuracy", 0.5))
    confidence = _confidence(dir_acc, rel_width)
    regime = detect_regime(series)

    # apply the learned self-assessment gate (see models/metagate.py)
    from .metagate import apply_meta_gate
    from .predicting import load_meta_gate

    gate = load_meta_gate(engine)
    if gate and direction != "flat":
        p_hit = apply_meta_gate(
            gate, point, lower, upper, expected_change_pct,
            confidence, f"{days}d", regime, True,
        )
        if p_hit is not None:
            import numpy as np

            confidence = float(np.clip(0.5 * confidence + 0.5 * p_hit, 0.05, 0.95))
    n_folds = int(metrics.get("n_folds", 0))
    if n_folds and n_folds < 20:
        warnings.append(f"Model validated on only {n_folds} walk-forward folds.")

    # Decision lean vs round-trip costs. Conservative: a "buy" lean requires
    # the expected move to clear costs, a "confident buy" additionally needs
    # the LOWER bound to clear entry costs.
    lower_change_pct = (lower / last_price - 1.0) * 100.0
    if expected_change_pct > round_trip_cost_pct:
        lean = "buy"
        lean_note = (
            f"Expected move ({expected_change_pct:+.2f}%) exceeds round-trip costs "
            f"(~{round_trip_cost_pct:.2f}%) over {days} day(s)."
        )
        if lower_change_pct > 0:
            lean_note += " Even the pessimistic bound is positive."
    elif expected_change_pct < -round_trip_cost_pct:
        lean = "sell"
        lean_note = (
            f"Expected move ({expected_change_pct:+.2f}%) is below "
            f"-{round_trip_cost_pct:.2f}% (round-trip costs) over {days} day(s)."
        )
    else:
        lean = "hold"
        lean_note = (
            f"Expected move ({expected_change_pct:+.2f}%) does not clear round-trip "
            f"costs (~{round_trip_cost_pct:.2f}%) — trading this view would likely "
            "cost more than it gains."
        )

    return {
        "symbol": "IR_GOLD_18K",
        "horizon_days": days,
        "model_name": winner,
        "beats_naive": winner != "naive",
        "point_forecast": round(point, 2),
        "lower_bound": round(lower, 2),
        "upper_bound": round(upper, 2),
        "last_price": round(last_price, 2),
        "expected_change_pct": round(expected_change_pct, 4),
        "direction": direction,
        "confidence": round(confidence, 3),
        "regime": regime,
        "metrics": metrics,
        "drivers": _drivers(model, series, regime),
        "decision_lean": lean,
        "decision_note": lean_note,
        "round_trip_cost_pct": round(round_trip_cost_pct, 3),
        "provider_gap_pct": round(gap_pct, 3) if gap_pct is not None else None,
        "warnings": warnings,
        "ephemeral": True,
    }
