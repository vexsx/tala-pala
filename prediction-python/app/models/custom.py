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
    blended_confidence,
    live_calibration_for,
    load_live_calibration,
    provider_gap_pct,
)
from .training import (
    report_metrics,
    MIN_DAILY_POINTS,
    _worker_count,
    detect_regime,
    evaluate_candidates,
    load_series,
    select_winner,
)

log = logging.getLogger(__name__)

MIN_DAYS = 1
MAX_DAYS = 90

# Matches the nightly MAX_FOLDS, and it has to.
#
# At 25 folds this path could NEVER publish a coverage rate. ``report_metrics``
# hands back a block whose coverage was walked over the full fold set, and
# ``walk_forward_coverage`` spends its first ``min_history=10`` folds building
# the residual pool — so 25 folds scores at most 15 against
# ``MIN_SCORED_FOR_COVERAGE`` = 20, for every horizon and every candidate,
# forever. ``interval_coverage_walk_forward.rate`` was structurally null.
#
# Measured cost of the fix (9 FAST_CANDIDATES, this machine, warm process):
#
#   daily points   days   25 folds   40 folds   scored 25 -> 40
#   300            7d      8.7s      11.6s      14 -> 29
#   300            30d     6.4s       9.4s      14 -> 26
#   700            7d      9.3s      14.9s      15 -> 30
#   700            30d     8.9s      14.0s      15 -> 29
#
# +3 to +6 seconds on a request that already takes 6-9, against a Go client
# timeout of 5 minutes — modest, and it buys a real measurement in place of a
# permanently null field. It is still not a guarantee: a short history with a
# long horizon produces fewer folds than the budget asks for and the rate stays
# null. That case is now stated in the response instead of being silent.
CUSTOM_MAX_FOLDS = 40

# Where the wait actually went, measured on the production shape (1224 daily
# points, 9 FAST_CANDIDATES, 40 folds, this machine, warm process):
#
#   candidate        walk-forward s   share
#   hist_gb                  13.18   78.7%
#   huber                     1.32    7.9%
#   ses / linear / holt / theta / knn / sma / naive   3.56 combined
#
# So "360 fits" was the wrong unit: ONE candidate is four fifths of the
# request, and 1.59s of the remainder (8.4%) was the three tabular candidates
# rebuilding the same causal feature frame once per fold. Neither is fixable by
# doing less work — the fold count buys the coverage measurement and the model
# set decides the forecast — so the fix is to stop repeating work and to stop
# doing it one core at a time:
#
#   * ``walk_forward`` builds the feature frame once per candidate and slices
#     it (folds are prefixes of one series and every feature is causal);
#   * the folds, which are independent by construction, are spread over worker
#     processes for this path only. The nightly run stays sequential: nobody is
#     waiting on it and it already has two symbols x seven horizons to fill a
#     machine with.
#
# Measured end to end on evaluate_candidates, same input, 7 workers:
#   7d   19.0s -> 17.4s (frame reuse) -> 2.7s warm pool / 5.3s cold
#   30d  18.0s ->                        2.8s
# Fold-for-fold identical predictions (max |diff| 0.0), asserted by
# ``test_parallel_folds_are_the_same_folds``. The first request after a restart
# pays the ~2.6s pool spin-up; loky keeps the workers for later ones.
#
# Fast families only: interactive latency matters and the heavyweight members
# (rf/gbr/quantile_gbr/arima/sarimax) rarely beat these on this data scale.
FAST_CANDIDATES = (
    "naive", "sma", "ses", "theta", "holt_damped", "linear", "hist_gb",
    "lorentzian_knn", "huber",
)

# Round-trip trading cost defaults, mirroring app/backtest defaults.
DEFAULT_FEE_PCT = 0.5
DEFAULT_SPREAD_PCT = 1.0
DEFAULT_SLIPPAGE_PCT = 0.1


def _calibration_driver(days: int, live_cal: Optional[dict]) -> dict:
    """Say whether the shipped confidence was calibrated against live outcomes.

    Two states, and only two, because the lookup is exact-horizon (see
    :func:`~app.models.predicting.live_calibration_for`): either this day count
    has matured predictions of its own and the confidence is blended toward
    their hit rate, or it does not and the confidence is the validation
    heuristic, unblended and labelled as such.

    There used to be a third state — "calibrated against the NEAREST horizon
    with live outcomes" — and it was unbounded: a 90-day forecast could be, and
    was, calibrated on the 1-hour hit rate. It is gone. Saying which horizon
    was substituted did not make the substituted number a measurement of this
    one.
    """
    if not live_cal:
        return {
            "factor": "confidence_calibration",
            "note": (
                f"not calibrated against live outcomes: no {days}-day "
                "predictions of this system's own have matured yet, so this "
                "confidence is the validation heuristic alone (validation "
                "directional accuracy blended with interval tightness) and no "
                "other horizon's outcomes were substituted for it"
            ),
        }
    n = int(live_cal.get("n") or 0)
    hit = live_cal.get("dir_hit_rate")
    rate = f", live directional hit rate {float(hit):.0%}" if hit is not None else ""
    return {
        "factor": "confidence_calibration",
        "note": f"calibrated against {n} matured {days}d prediction(s){rate}",
    }


def _coverage_warning(metrics: dict, days: int) -> Optional[str]:
    """Explain a withheld interval-coverage rate, rather than shipping a null.

    ``metrics['interval_coverage_walk_forward']['rate']`` is ``None`` whenever
    the walk-forward validation could not score ``min_scored_folds`` of them
    (:data:`~app.models.intervals.MIN_SCORED_FOR_COVERAGE`, read back off the
    block so the warning quotes the bar that measurement actually used). Raising
    ``CUSTOM_MAX_FOLDS`` to the nightly budget makes that reachable for ordinary
    requests, but a short history with a long horizon still cannot get there —
    and a null field with nothing next to it is indistinguishable from a bug
    (it WAS one: at 25 folds the field was null for every request that ever
    existed).
    """
    cov = metrics.get("interval_coverage_walk_forward")
    if not isinstance(cov, dict) or cov.get("rate") is not None:
        return None
    scored = int(cov.get("scored_folds") or 0)
    total = int(cov.get("total_folds") or 0)
    warmup = int(cov.get("residual_warmup_folds") or 0)
    need = int(cov.get("min_scored_folds") or 0)
    return (
        f"This {days}-day forecast does not carry a measured interval-coverage "
        f"rate: walk-forward validation produced {total} fold(s), the first "
        f"{warmup} of which only build the residual pool, leaving {scored} "
        f"scored against the {need} needed before a coverage rate says anything "
        "about a 90% band. The interval is built exactly as the scheduled "
        "forecasts' are; what is missing is a measurement of how often it "
        "contained the truth."
    )


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

    # Cost basis: explicit caller overrides win; otherwise the observed dealer
    # spread (same number the signal and the UI use), else the assumption.
    if fee_pct is None and spread_pct is None and slippage_pct is None:
        from ..core.costs import round_trip_cost_pct as _resolve_cost

        round_trip_cost_pct, cost_basis = _resolve_cost(engine)
    else:
        fee = DEFAULT_FEE_PCT if fee_pct is None else float(fee_pct)
        spread = DEFAULT_SPREAD_PCT if spread_pct is None else float(spread_pct)
        slippage = DEFAULT_SLIPPAGE_PCT if slippage_pct is None else float(slippage_pct)
        # fee and slippage are paid on BOTH sides; the spread once.
        round_trip_cost_pct = 2.0 * fee + spread + 2.0 * slippage
        cost_basis = "caller"

    series = load_series(engine, "IR_GOLD_18K", "daily")
    if len(series) < MIN_DAILY_POINTS + days:
        raise ValueError(
            f"not enough daily history for a {days}-day horizon "
            f"({len(series)} points, need >= {MIN_DAILY_POINTS + days})"
        )

    results = evaluate_candidates(
        series, days, candidates=FAST_CANDIDATES, max_folds=CUSTOM_MAX_FOLDS,
        n_jobs=_worker_count(None),
    )
    if not results:
        raise ValueError("no candidate model produced walk-forward folds")
    winner = select_winner(results)
    winner_res = results[winner]
    metrics = report_metrics(winner_res)

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
    regime = detect_regime(series)

    # Confidence has to MEAN the same thing here as on the scheduled path.
    # It did not: _predict_one blends the validation heuristic toward live
    # outcomes and stores THAT as raw_confidence, which is the feature the
    # meta-gate trains on, while this path shipped the unblended heuristic
    # straight into score_meta_gate. The gate then z-scored a raw value against
    # a blended distribution and refused every custom forecast — measured at
    # |z| = 6.1 on horizons backed by 309 training rows — while telling the
    # user their confidence was abnormal. The scale difference was the
    # application's, not the forecast's.
    #
    # Calibration is EXACT-horizon (``live_calibration_for``), so on a day count
    # the scheduler never runs there is no block and no blend. That is also what
    # keeps this path self-consistent: the gate's confidence column is fitted on
    # blended values, so an unblended confidence is off the trained scale — and
    # the same horizons that have no live calibration are the ones the gate has
    # no rows for, so it declines and leaves the number alone rather than
    # scoring a scale it never saw.
    cal_all = load_live_calibration(engine)
    live_cal = live_calibration_for(cal_all, "IR_GOLD_18K", f"{days}d")
    confidence = blended_confidence(_confidence(dir_acc, rel_width), live_cal, regime)

    # apply the learned self-assessment gate (see models/metagate.py)
    from .metagate import score_meta_gate
    from .predicting import _gate_driver, load_meta_gate

    from ..core.market_hours import is_acceptably_fresh
    from ..db import utcnow as _utcnow

    last_obs = series.index[-1].to_pydatetime() if len(series) else None
    fresh = bool(
        last_obs is not None
        and is_acceptably_fresh("IR_GOLD_18K", last_obs, _utcnow(), settings)
    )
    drivers = _drivers(model, series, regime)
    drivers.append(_calibration_driver(days, live_cal))
    gate = load_meta_gate(engine)
    if direction != "flat":
        # This path is the one most likely to be OUTSIDE the gate's support:
        # the user can ask for any horizon in 1..90 days while the gate only
        # ever trains on the seven the scheduler emits. So the verdict is
        # published as a driver whatever it says — a gate that silently
        # declined and a gate that never existed cannot both look like
        # "nothing here" while a scored one quietly moves the number.
        verdict = score_meta_gate(
            gate, point, lower, upper, expected_change_pct,
            confidence, f"{days}d", regime, fresh, "IR_GOLD_18K",
        )
        drivers.append(_gate_driver(verdict, gate))
        if verdict.p_hit is not None:
            import numpy as np

            confidence = float(np.clip(0.5 * confidence + 0.5 * verdict.p_hit, 0.05, 0.95))
            if verdict.p_hit < 0.45:
                warnings.append(
                    "The system's self-assessment (learned from its own past "
                    "predictions) rates this call below coin-flip reliability."
                )
    n_folds = int(metrics.get("n_folds", 0))
    if n_folds and n_folds < 20:
        warnings.append(f"Model validated on only {n_folds} walk-forward folds.")
    cov_warning = _coverage_warning(metrics, days)
    if cov_warning:
        warnings.append(cov_warning)

    # Monte Carlo outcome probabilities (bootstrap over historical returns):
    # the honest answer to "how often would a move like this clear costs?"
    from .tvinspired import mc_probabilities

    monte_carlo = mc_probabilities(
        series, days, round_trip_cost_pct, expected_change_pct=expected_change_pct
    )

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
    if monte_carlo is not None:
        lean_note += (
            f" Historically-bootstrapped odds over {days} day(s): "
            f"{monte_carlo['p_gain_over_cost']:.0%} of simulated paths gain more "
            f"than the {round_trip_cost_pct:.1f}% cost."
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
        "drivers": drivers,
        "decision_lean": lean,
        "decision_note": lean_note,
        "monte_carlo": monte_carlo,
        "round_trip_cost_pct": round(round_trip_cost_pct, 3),
        "provider_gap_pct": round(gap_pct, 3) if gap_pct is not None else None,
        "warnings": warnings,
        "ephemeral": True,
    }
