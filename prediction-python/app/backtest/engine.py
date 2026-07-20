"""Walk-forward signal backtest (long-only gold vs cash-toman).

No-future-information guarantee: at each step ``t`` the forecast function
receives ONLY ``values[: t + 1]`` (a copy), and the position decided at ``t``
earns the ``t -> t+1`` return.  Fees, spread and slippage are charged on each
side of every trade; a minimum holding period is enforced.

Benchmarks: buy_and_hold, no_action, sma20/50 crossover.  Metrics include
gross-vs-net and a per-regime breakdown.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Sequence

import numpy as np
import pandas as pd

from ..models.training import detect_regime

ForecastFn = Callable[[np.ndarray, int], float]
"""(history_values, horizon_steps) -> expected percent change over the horizon."""


@dataclass
class BacktestParams:
    horizon_steps: int = 1
    fee_pct: float = 0.5
    spread_pct: float = 1.0
    slippage_pct: float = 0.1
    min_holding_days: int = 1
    warmup: int = 60

    @property
    def cost_per_side_pct(self) -> float:
        """Fee + half the spread + slippage, paid on each side of a trade."""
        return self.fee_pct + self.spread_pct / 2.0 + self.slippage_pct

    @property
    def round_trip_cost_pct(self) -> float:
        return 2.0 * self.cost_per_side_pct


def default_forecast(history: np.ndarray, steps: int) -> float:
    """Simple, deterministic momentum/trend blend (no future information).

    Mirrors what the cheap production models see: short-vs-long SMA gap plus
    recent drift, scaled by the horizon.
    """
    if len(history) < 21:
        return 0.0
    sma5 = float(np.mean(history[-5:]))
    sma20 = float(np.mean(history[-20:]))
    trend_gap = (sma5 / sma20 - 1.0) * 100.0
    drift = (history[-1] / history[-11] - 1.0) * 100.0 / 10.0  # per-step drift
    return trend_gap * 0.5 + drift * steps


def _max_drawdown_pct(equity: np.ndarray) -> float:
    peaks = np.maximum.accumulate(equity)
    drawdowns = equity / peaks - 1.0
    return float(-drawdowns.min() * 100.0) if len(drawdowns) else 0.0


def _metrics_from_equity(
    equity: np.ndarray,
    trades: Sequence[float],
    n_days: int,
    gross_equity: Optional[np.ndarray] = None,
    directional_hits: Optional[Sequence[bool]] = None,
) -> dict:
    total_return = (float(equity[-1]) - 1.0) * 100.0 if len(equity) else 0.0
    years = max(n_days / 365.0, 1e-9)
    annualized = ((float(equity[-1])) ** (1.0 / years) - 1.0) * 100.0 if len(equity) and equity[-1] > 0 else -100.0
    daily_ret = np.diff(equity) / equity[:-1] if len(equity) > 1 else np.array([])
    sharpe_like = (
        float(np.mean(daily_ret) / np.std(daily_ret) * np.sqrt(365.0))
        if daily_ret.size and np.std(daily_ret) > 0
        else 0.0
    )
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    out = {
        "total_return_pct": round(total_return, 4),
        "annualized_return_pct": round(annualized, 4),
        "win_rate": round(len(wins) / len(trades), 4) if trades else 0.0,
        "profit_factor": round(gross_win / gross_loss, 4) if gross_loss > 0 else (
            float("inf") if gross_win > 0 else 0.0
        ),
        "max_drawdown_pct": round(_max_drawdown_pct(equity), 4),
        "n_trades": len(trades),
        "avg_trade_return_pct": round(float(np.mean(trades)), 4) if trades else 0.0,
        "sharpe_like": round(sharpe_like, 4),
    }
    if gross_equity is not None and len(gross_equity):
        out["gross_total_return_pct"] = round((float(gross_equity[-1]) - 1.0) * 100.0, 4)
    if directional_hits:
        out["directional_accuracy"] = round(
            sum(directional_hits) / len(directional_hits), 4
        )
    if out["profit_factor"] == float("inf"):
        out["profit_factor"] = None  # JSON-safe
    return out


def run_backtest(
    series: pd.Series,
    params: BacktestParams,
    forecast_fn: Optional[ForecastFn] = None,
) -> dict:
    """Run the strategy + benchmarks over a daily price series."""
    forecast_fn = forecast_fn or default_forecast
    values = series.astype(float).to_numpy()
    n = len(values)
    if n < params.warmup + params.horizon_steps + 2:
        raise ValueError(
            f"series too short for backtest: {n} points, "
            f"need > {params.warmup + params.horizon_steps + 2}"
        )
    cost_side = params.cost_per_side_pct / 100.0
    entry_threshold = params.round_trip_cost_pct * 0.75  # expected move must roughly clear costs

    # causal regime labels (computed on history up to and including t)
    regimes = [detect_regime(series.iloc[: t + 1]) for t in range(n)]

    equity = [1.0]
    gross = [1.0]
    position = 0
    entry_equity = 0.0
    held_days = 0
    trades: list[float] = []
    directional_hits: list[bool] = []
    regime_returns: dict[str, list[float]] = {}

    for t in range(params.warmup, n - 1):
        history = values[: t + 1].copy()  # strictly no future data
        expected = float(forecast_fn(history, params.horizon_steps))

        # record forecast direction vs realized move over the horizon
        realized_idx = min(t + params.horizon_steps, n - 1)
        realized = (values[realized_idx] / values[t] - 1.0) * 100.0
        if expected != 0.0 and realized != 0.0:
            directional_hits.append(np.sign(expected) == np.sign(realized))

        eq = equity[-1]
        if position == 0 and expected > entry_threshold:
            eq *= 1.0 - cost_side
            position = 1
            entry_equity = eq
            held_days = 0
        elif position == 1 and held_days >= params.min_holding_days and expected < 0.0:
            eq *= 1.0 - cost_side
            trades.append((eq / entry_equity - 1.0) * 100.0)
            position = 0

        step_ret = values[t + 1] / values[t] - 1.0
        if position == 1:
            eq *= 1.0 + step_ret
            held_days += 1
            regime_returns.setdefault(regimes[t], []).append(step_ret)
            gross.append(gross[-1] * (1.0 + step_ret))
        else:
            gross.append(gross[-1])
        equity.append(eq)

    if position == 1:  # close the final open position
        eq = equity[-1] * (1.0 - cost_side)
        trades.append((eq / entry_equity - 1.0) * 100.0)
        equity[-1] = eq

    n_days = n - 1 - params.warmup
    equity_arr = np.array(equity)
    strategy = _metrics_from_equity(
        equity_arr, trades, n_days, gross_equity=np.array(gross),
        directional_hits=directional_hits,
    )

    per_regime = {
        regime: {
            "days": len(rets),
            "return_pct": round((float(np.prod([1.0 + r for r in rets])) - 1.0) * 100.0, 4),
        }
        for regime, rets in sorted(regime_returns.items())
    }

    # --- benchmarks ---------------------------------------------------------
    window = values[params.warmup :]
    bh_gross = window[-1] / window[0]
    bh_net = bh_gross * (1.0 - cost_side) ** 2
    bh_equity = np.concatenate(([1.0 * (1.0 - cost_side)], window[1:] / window[0] * (1.0 - cost_side)))
    bh_equity[-1] *= 1.0 - cost_side
    buy_and_hold = _metrics_from_equity(
        bh_equity, [(bh_net - 1.0) * 100.0], n_days,
        gross_equity=np.array([1.0, bh_gross]),
    )

    no_action = _metrics_from_equity(np.ones(2), [], n_days, gross_equity=np.ones(2))

    sma_equity = [1.0]
    sma_gross = [1.0]
    sma_pos = 0
    sma_entry = 0.0
    sma_trades: list[float] = []
    for t in range(params.warmup, n - 1):
        if t >= 50:
            sma20 = float(np.mean(values[t - 19 : t + 1]))
            sma50 = float(np.mean(values[t - 49 : t + 1]))
            want = 1 if sma20 > sma50 else 0
        else:
            want = sma_pos
        eq = sma_equity[-1]
        if want != sma_pos:
            eq *= 1.0 - cost_side
            if want == 1:
                sma_entry = eq
            else:
                sma_trades.append((eq / sma_entry - 1.0) * 100.0)
            sma_pos = want
        step_ret = values[t + 1] / values[t] - 1.0
        if sma_pos == 1:
            eq *= 1.0 + step_ret
            sma_gross.append(sma_gross[-1] * (1.0 + step_ret))
        else:
            sma_gross.append(sma_gross[-1])
        sma_equity.append(eq)
    if sma_pos == 1:
        eq = sma_equity[-1] * (1.0 - cost_side)
        sma_trades.append((eq / sma_entry - 1.0) * 100.0)
        sma_equity[-1] = eq
    sma_crossover = _metrics_from_equity(
        np.array(sma_equity), sma_trades, n_days, gross_equity=np.array(sma_gross)
    )

    return {
        "strategy": strategy,
        "benchmarks": {
            "buy_and_hold": buy_and_hold,
            "no_action": no_action,
            "sma_crossover": sma_crossover,
        },
        "per_regime": per_regime,
        "params": {
            "horizon_steps": params.horizon_steps,
            "fee_pct": params.fee_pct,
            "spread_pct": params.spread_pct,
            "slippage_pct": params.slippage_pct,
            "min_holding_days": params.min_holding_days,
            "warmup": params.warmup,
            "cost_per_side_pct": round(params.cost_per_side_pct, 4),
        },
        "period": {
            "start": series.index[0].isoformat(),
            "end": series.index[-1].isoformat(),
            "n_points": n,
        },
    }


# ---------------------------------------------------------------------------
# DB wrapper (called by POST /internal/backtest)
# ---------------------------------------------------------------------------


def run_and_store(engine, settings, payload: dict) -> dict:
    """Load the daily series, run the backtest, persist a backtest_runs row."""
    from datetime import datetime, timezone

    from ..db import backtest_runs, utcnow
    from ..models.training import HORIZON_SPECS, load_series

    horizon = str(payload.get("horizon") or "1d")
    if horizon not in HORIZON_SPECS or HORIZON_SPECS[horizon][0] != "daily":
        raise ValueError(f"backtest supports daily horizons only, got {horizon!r}")
    steps = HORIZON_SPECS[horizon][1]
    params = BacktestParams(
        horizon_steps=steps,
        fee_pct=float(payload.get("fee_pct", 0.5)),
        spread_pct=float(payload.get("spread_pct", 1.0)),
        slippage_pct=float(payload.get("slippage_pct", 0.1)),
        min_holding_days=int(payload.get("min_holding_days", 1)),
    )

    series = load_series(engine, "IR_GOLD_18K", "daily")

    def _parse_bound(raw):
        if not raw:
            return None
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    start = _parse_bound(payload.get("start"))
    end = _parse_bound(payload.get("end"))
    if start is not None:
        series = series[series.index >= start]
    if end is not None:
        series = series[series.index <= end]

    now = utcnow()
    try:
        results = run_backtest(series, params)
        status, error = "succeeded", None
    except ValueError as exc:
        results, status, error = {}, "failed", str(exc)

    with engine.begin() as conn:
        run_id = conn.execute(
            backtest_runs.insert().values(
                created_at=now,
                horizon=horizon,
                params={
                    "fee_pct": params.fee_pct,
                    "spread_pct": params.spread_pct,
                    "slippage_pct": params.slippage_pct,
                    "min_holding_days": params.min_holding_days,
                    "horizon_steps": params.horizon_steps,
                },
                period_start=series.index.min().to_pydatetime() if len(series) else None,
                period_end=series.index.max().to_pydatetime() if len(series) else None,
                results=results,
                status=status,
                error=error,
            )
        ).inserted_primary_key[0]

    if status == "failed":
        return {"id": int(run_id), "status": status, "error": error}
    out = dict(results)
    out["id"] = int(run_id)
    out["status"] = status
    return out
