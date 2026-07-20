"""Backtest engine: cost drag, benchmark math, and the no-future-info guarantee."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.backtest.engine import BacktestParams, default_forecast, run_backtest


def _series(values) -> pd.Series:
    index = pd.date_range(
        datetime(2025, 1, 1, tzinfo=timezone.utc), periods=len(values), freq="D"
    )
    return pd.Series(list(values), index=index, dtype=float)


def _rising(n=100, daily=0.005):
    return _series(100.0 * (1.0 + daily) ** np.arange(n))


ALWAYS_BULLISH = lambda history, steps: 5.0  # noqa: E731
ALWAYS_FLAT = lambda history, steps: 0.0  # noqa: E731


def test_fees_reduce_returns():
    series = _rising(120)
    base = dict(horizon_steps=1, min_holding_days=1, warmup=60)
    free = run_backtest(series, BacktestParams(fee_pct=0.0, spread_pct=0.0,
                                              slippage_pct=0.0, **base),
                        forecast_fn=ALWAYS_BULLISH)
    costly = run_backtest(series, BacktestParams(fee_pct=0.5, spread_pct=1.0,
                                                 slippage_pct=0.1, **base),
                          forecast_fn=ALWAYS_BULLISH)
    assert free["strategy"]["total_return_pct"] > costly["strategy"]["total_return_pct"]
    # net never exceeds gross when trades occurred
    assert (costly["strategy"]["total_return_pct"]
            <= costly["strategy"]["gross_total_return_pct"])


def test_buy_and_hold_benchmark_math():
    series = _rising(120)
    params = BacktestParams(fee_pct=0.5, spread_pct=1.0, slippage_pct=0.1,
                            horizon_steps=1, warmup=60)
    result = run_backtest(series, params, forecast_fn=ALWAYS_FLAT)
    window = series.iloc[params.warmup:].to_numpy()
    cost_side = params.cost_per_side_pct / 100.0
    expected_net = (window[-1] / window[0]) * (1.0 - cost_side) ** 2 - 1.0
    # engine rounds metrics to 4 decimals
    assert result["benchmarks"]["buy_and_hold"]["total_return_pct"] == pytest.approx(
        expected_net * 100.0, abs=1e-3
    )
    expected_gross = (window[-1] / window[0] - 1.0) * 100.0
    assert result["benchmarks"]["buy_and_hold"]["gross_total_return_pct"] == pytest.approx(
        expected_gross, abs=1e-3
    )


def test_no_action_benchmark_is_zero():
    series = _rising(100)
    result = run_backtest(series, BacktestParams(warmup=60), forecast_fn=ALWAYS_FLAT)
    assert result["benchmarks"]["no_action"]["total_return_pct"] == 0.0
    assert result["benchmarks"]["no_action"]["n_trades"] == 0


def test_flat_forecast_never_trades():
    series = _rising(100)
    result = run_backtest(series, BacktestParams(warmup=60), forecast_fn=ALWAYS_FLAT)
    assert result["strategy"]["n_trades"] == 0
    assert result["strategy"]["total_return_pct"] == 0.0


def test_no_future_information():
    """The forecast function must only ever see the prefix of the series."""
    rng = np.random.default_rng(9)
    values = 100.0 * np.exp(np.cumsum(rng.normal(0.001, 0.01, 110)))
    series = _series(values)
    full = series.to_numpy()
    seen_lengths: list[int] = []

    def spy_forecast(history: np.ndarray, steps: int) -> float:
        # exact prefix of the full series, nothing from the future
        assert np.array_equal(history, full[: len(history)])
        seen_lengths.append(len(history))
        return default_forecast(history, steps)

    run_backtest(series, BacktestParams(warmup=60, horizon_steps=1),
                 forecast_fn=spy_forecast)
    assert seen_lengths == sorted(seen_lengths)
    assert max(seen_lengths) <= len(full) - 1  # never handed the final point early


def test_min_holding_period_respected():
    """With min_holding_days=5, a sell urge on day 1 cannot close the trade."""
    series = _rising(100)
    calls = {"n": 0}

    def enter_then_exit(history, steps):
        calls["n"] += 1
        return 10.0 if calls["n"] == 1 else -10.0  # enter once, then beg to exit

    result = run_backtest(
        series,
        BacktestParams(warmup=60, min_holding_days=5,
                       fee_pct=0.0, spread_pct=0.0, slippage_pct=0.0),
        forecast_fn=enter_then_exit,
    )
    assert result["strategy"]["n_trades"] == 1
    # held through 5 rising days minimum => positive trade
    assert result["strategy"]["avg_trade_return_pct"] > 0


def test_series_too_short_raises():
    with pytest.raises(ValueError):
        run_backtest(_rising(30), BacktestParams(warmup=60))


def test_results_include_per_regime_and_params():
    series = _rising(140)
    result = run_backtest(series, BacktestParams(warmup=60), forecast_fn=ALWAYS_BULLISH)
    assert "per_regime" in result
    assert result["params"]["cost_per_side_pct"] == pytest.approx(0.5 + 0.5 + 0.1)
    for key in ("total_return_pct", "annualized_return_pct", "win_rate",
                "profit_factor", "max_drawdown_pct", "n_trades",
                "avg_trade_return_pct", "sharpe_like", "directional_accuracy"):
        assert key in result["strategy"], key
