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
