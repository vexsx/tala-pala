"""Leakage guards: features at as_of must never see rows observed after as_of."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.features.engineering import (
    LeakageError,
    assert_no_future,
    build_snapshot,
    compute_feature_frame,
    daily_close,
    gregorian_to_jalali,
)


def _prices_df(n_days: int = 80, start_value: float = 8_000_000.0) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    start = datetime(2026, 4, 1, 12, 0, tzinfo=timezone.utc)
    rows = []
    value = start_value
    usd = 100_000.0
    xau = 3300.0
    for i in range(n_days):
        ts = start + timedelta(days=i)
        value *= 1.0 + rng.normal(0.001, 0.01)
        usd *= 1.0 + rng.normal(0.0005, 0.005)
        xau *= 1.0 + rng.normal(0.0002, 0.006)
        rows.append({"symbol": "IR_GOLD_18K", "observed_at": ts, "value": value})
        rows.append({"symbol": "USD_IRT", "observed_at": ts, "value": usd})
        rows.append({"symbol": "XAUUSD", "observed_at": ts, "value": xau})
    return pd.DataFrame(rows)


def test_assert_no_future_raises():
    df = _prices_df(10)
    early = datetime(2026, 4, 3, tzinfo=timezone.utc)
    with pytest.raises(LeakageError):
        assert_no_future(df, early)
    late = datetime(2027, 1, 1, tzinfo=timezone.utc)
    assert_no_future(df, late)  # no raise


def test_snapshot_ignores_future_rows():
    df = _prices_df(80)
    as_of = datetime(2026, 5, 20, 23, 59, tzinfo=timezone.utc)

    baseline = build_snapshot(df, as_of)
    assert baseline is not None

    # corrupt every row AFTER as_of with absurd values; snapshot must not move
    corrupted = df.copy()
    future_mask = pd.to_datetime(corrupted["observed_at"], utc=True) > pd.Timestamp(as_of)
    assert future_mask.any()
    corrupted.loc[future_mask, "value"] = 1e12
    with_future_noise = build_snapshot(corrupted, as_of)

    assert with_future_noise == baseline


def test_rolling_windows_do_not_peek():
    """Feature values at time t must be identical whether or not later data exists."""
    df = _prices_df(80)
    gold_full = daily_close(df, "IR_GOLD_18K")
    frame_full = compute_feature_frame(gold_full)

    cutoff = 50
    gold_truncated = gold_full.iloc[:cutoff]
    frame_truncated = compute_feature_frame(gold_truncated)

    last_common = gold_truncated.index[-1]
    row_full = frame_full.loc[last_common].dropna()
    row_trunc = frame_truncated.loc[last_common].dropna()
    common_cols = row_full.index.intersection(row_trunc.index)
    assert len(common_cols) > 10
    for col in common_cols:
        assert row_full[col] == pytest.approx(row_trunc[col], rel=1e-12), col


def test_feature_frame_contents():
    df = _prices_df(80)
    gold = daily_close(df, "IR_GOLD_18K")
    usd = daily_close(df, "USD_IRT")
    xau = daily_close(df, "XAUUSD")
    frame = compute_feature_frame(gold, usd, xau)
    for col in ("lag_1", "lag_20", "ret_1", "roll_mean_20", "roll_std_5",
                "momentum_10", "rsi_14", "dow", "jalali_month",
                "usd_ret_1", "xau_ret_1", "premium_pct", "premium_z_30"):
        assert col in frame.columns, col
    last = frame.iloc[-1]
    assert not np.isnan(last["premium_z_30"])
    assert 0.0 <= last["rsi_14"] <= 100.0
    assert 1 <= last["jalali_month"] <= 12


def test_gregorian_to_jalali_known_dates():
    assert gregorian_to_jalali(2026, 3, 21) == (1405, 1, 1)  # Nowruz 1405
    assert gregorian_to_jalali(2026, 7, 19) == (1405, 4, 28)  # matches TGJU fixture


# --- Addendum 2 features -----------------------------------------------------


NEW_BASE_COLS = (
    "adx_14", "stoch_k", "stoch_d", "williams_r_14", "cci_20",
    "donchian_upper_dist_20", "donchian_lower_dist_20", "keltner_pos_20",
    "drawdown_90d_pct",
) + tuple(f"dow_{d}" for d in range(7))


def test_new_feature_columns_present():
    df = _prices_df(120)
    gold = daily_close(df, "IR_GOLD_18K")
    usd = daily_close(df, "USD_IRT")
    xau = daily_close(df, "XAUUSD")
    frame = compute_feature_frame(gold, usd, xau)
    for col in NEW_BASE_COLS + ("corr_xau_20", "premium_mom_5"):
        assert col in frame.columns, col
    last = frame.iloc[-1]
    assert 0.0 <= last["stoch_k"] <= 100.0
    assert 0.0 <= last["stoch_d"] <= 100.0
    assert -100.0 <= last["williams_r_14"] <= 0.0
    assert 0.0 <= last["adx_14"] <= 100.0
    assert -1.0 <= last["corr_xau_20"] <= 1.0
    assert last["drawdown_90d_pct"] <= 0.0
    # aux-derived columns only exist when the aux series are supplied
    plain = compute_feature_frame(gold)
    assert "corr_xau_20" not in plain.columns
    assert "premium_mom_5" not in plain.columns


def test_dow_one_hot_sums_to_one_and_matches_dow():
    frame = compute_feature_frame(daily_close(_prices_df(30), "IR_GOLD_18K"))
    one_hot = frame[[f"dow_{d}" for d in range(7)]]
    assert (one_hot.sum(axis=1) == 1.0).all()
    recovered = one_hot.to_numpy().argmax(axis=1)
    assert (recovered == frame["dow"].to_numpy()).all()


def test_adx_trending_beats_ranging():
    """ADX measures trend strength: a steadily trending series must score
    clearly higher than a mean-reverting/ranging one."""
    n = 150
    rng = np.random.default_rng(11)
    trend = pd.Series(
        1000.0 + 5.0 * np.arange(n) + rng.normal(0, 0.5, n),
        index=pd.date_range(datetime(2026, 1, 1, tzinfo=timezone.utc), periods=n, freq="D"),
    )
    ranging = pd.Series(
        1000.0 + rng.normal(0, 5.0, n),
        index=trend.index,
    )
    adx_trend = compute_feature_frame(trend)["adx_14"].iloc[-1]
    adx_range = compute_feature_frame(ranging)["adx_14"].iloc[-1]
    assert adx_trend > 60.0        # one-sided directional movement
    assert adx_trend > adx_range + 10.0


def test_stochastic_and_donchian_at_new_highs():
    """A monotonically rising series sits at its 14d high: %K = 100,
    Williams %R = 0, zero distance below the Donchian upper band."""
    n = 60
    series = pd.Series(
        np.linspace(100.0, 200.0, n),
        index=pd.date_range(datetime(2026, 1, 1, tzinfo=timezone.utc), periods=n, freq="D"),
    )
    last = compute_feature_frame(series).iloc[-1]
    assert last["stoch_k"] == pytest.approx(100.0)
    assert last["williams_r_14"] == pytest.approx(0.0)
    assert last["donchian_upper_dist_20"] == pytest.approx(0.0)
    assert last["donchian_lower_dist_20"] > 0.0
    assert last["drawdown_90d_pct"] == pytest.approx(0.0)
    assert last["keltner_pos_20"] > 0.0  # above the EMA mid in an uptrend


def test_drawdown_known_value():
    # rise to 100 then fall to 90 -> 10% below the 90d high
    values = list(np.linspace(50.0, 100.0, 60)) + list(np.linspace(99.0, 90.0, 10))
    series = pd.Series(
        values,
        index=pd.date_range(datetime(2026, 1, 1, tzinfo=timezone.utc),
                            periods=len(values), freq="D"),
    )
    last = compute_feature_frame(series).iloc[-1]
    assert last["drawdown_90d_pct"] == pytest.approx(-10.0)


def test_premium_momentum_matches_premium_diff():
    df = _prices_df(90)
    gold = daily_close(df, "IR_GOLD_18K")
    usd = daily_close(df, "USD_IRT")
    xau = daily_close(df, "XAUUSD")
    frame = compute_feature_frame(gold, usd, xau)
    expected = frame["premium_pct"].diff(5)
    pd.testing.assert_series_equal(
        frame["premium_mom_5"], expected, check_names=False
    )


def test_corr_xau_perfectly_coupled_series():
    """When 18k IS a scaled copy of XAU, the rolling log-return correlation
    must be ~1."""
    n = 90
    rng = np.random.default_rng(23)
    index = pd.date_range(datetime(2026, 1, 1, tzinfo=timezone.utc), periods=n, freq="D")
    xau = pd.Series(3300.0 * np.exp(np.cumsum(rng.normal(0, 0.01, n))), index=index)
    gold = xau * 5000.0
    frame = compute_feature_frame(gold, None, xau)
    assert frame["corr_xau_20"].iloc[-1] == pytest.approx(1.0, abs=1e-9)


def test_new_features_do_not_peek():
    """Full-vs-truncated equality specifically for the Addendum 2 columns,
    with the aux series left FULL while gold is truncated — later aux rows
    must not move features at earlier timestamps."""
    df = _prices_df(120)
    gold_full = daily_close(df, "IR_GOLD_18K")
    usd_full = daily_close(df, "USD_IRT")
    xau_full = daily_close(df, "XAUUSD")

    frame_full = compute_feature_frame(gold_full, usd_full, xau_full)
    cutoff = 100
    frame_trunc = compute_feature_frame(gold_full.iloc[:cutoff], usd_full, xau_full)

    last_common = gold_full.index[cutoff - 1]
    row_full = frame_full.loc[last_common]
    row_trunc = frame_trunc.loc[last_common]
    for col in NEW_BASE_COLS + ("corr_xau_20", "premium_mom_5"):
        full_val, trunc_val = row_full[col], row_trunc[col]
        if pd.isna(full_val) and pd.isna(trunc_val):
            continue
        assert full_val == pytest.approx(trunc_val, rel=1e-12), col
