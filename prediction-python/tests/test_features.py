"""1D trend-alignment features (Addendum 20 leg) in the causal feature matrix.

Two properties are load-bearing here and each has a test that fails loudly:

* the feature EMA is the SAME EMA ``app/models/trend_alignment`` publishes on
  screen — one market must not have two definitions of "EMA26";
* nothing in these columns looks forward, proven by appending a future spike
  and demanding every pre-existing row come back byte-identical.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.features.engineering import (
    TREND_FAST,
    TREND_MID,
    TREND_MID_MIN_BARS,
    TREND_SLOW,
    TREND_SLOW_MIN_BARS,
    build_snapshot,
    compute_feature_frame,
    daily_close,
    ema_series,
)
from app.models import trend_alignment

STACK_COLS = (
    "close_vs_ema_26",
    "ema26_vs_ema48",
    "ema48_vs_ema220",
    "trend_stack_1d",
    "trend_stack_run",
)
FAST_COLS = STACK_COLS[:2]
SLOW_COLS = STACK_COLS[2:]


def _daily(n, seed=17, start=8_000_000.0, drift=0.0004, vol=0.008):
    """A daily-bucketed IRT-scale gold series, like ``daily_close`` returns."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n)
    values = start * np.exp(np.cumsum(steps))
    index = pd.date_range(
        datetime(2023, 1, 1, tzinfo=timezone.utc), periods=n, freq="D"
    )
    return pd.Series(values, index=index)


def _hourly(n, seed=19):
    rng = np.random.default_rng(seed)
    values = 8_000_000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.002, n)))
    index = pd.date_range(
        datetime(2026, 6, 26, tzinfo=timezone.utc), periods=n, freq="h"
    )
    return pd.Series(values, index=index)


# --- the EMA must be the indicator's EMA -------------------------------------


@pytest.mark.parametrize("period", [TREND_FAST, TREND_MID, TREND_SLOW])
def test_ema_series_equals_the_indicator_at_every_row(period):
    """Row t of the feature EMA == trend_alignment.ema(closes[:t+1]).

    Checked at EVERY row, not just the last: a walk-forward fold fits on a
    prefix, so any row that disagrees with the indicator is a row where the
    model and the card on screen describe different markets.
    """
    series = _daily(400)
    closes = [float(v) for v in series.to_numpy()]
    feature = ema_series(series, period)

    for i in range(len(closes)):
        published = trend_alignment.ema(closes[: i + 1], period)
        if published is None:  # indicator says "does not exist yet"
            assert np.isnan(feature.iloc[i]), i
        else:
            assert abs(float(feature.iloc[i]) - published) <= 1e-9, i


def test_ema_series_is_sma_seeded_not_pandas_ewm():
    """pandas' .ewm(adjust=False) seeds on the FIRST observation; the indicator
    seeds on the SMA of the first ``period`` values. On a series that opens
    with an outlier the two disagree materially — so silently accepting the
    pandas convention would have been a real, invisible divergence."""
    values = [1.0] + [100.0] * 40
    index = pd.date_range(
        datetime(2024, 1, 1, tzinfo=timezone.utc), periods=len(values), freq="D"
    )
    series = pd.Series(values, index=index)

    ours = ema_series(series, 10)
    pandas_way = series.ewm(span=10, adjust=False).mean()

    seed_row = 9
    assert ours.iloc[:seed_row].isna().all()  # no EMA before the seed exists
    assert ours.iloc[seed_row] == pytest.approx(float(np.mean(values[:10])))
    assert abs(ours.iloc[seed_row] - pandas_way.iloc[seed_row]) > 1.0


# --- what each history length can honestly support ---------------------------


def test_long_daily_history_gets_the_whole_stack():
    frame = compute_feature_frame(_daily(1000))
    for col in STACK_COLS:
        assert col in frame.columns, col
    state = frame["trend_stack_1d"].dropna()
    assert set(np.unique(state.to_numpy())) <= {-1.0, 0.0, 1.0}
    assert not state.empty


def test_hourly_frame_gets_no_stack_columns():
    """The 4H/1H legs are not computable — intraday history is ~23 days — so
    they are not offered. An EMA220 over hourly bars would be a 9-day intraday
    average wearing a 1D label."""
    frame = compute_feature_frame(_hourly(23 * 24))
    for col in STACK_COLS:
        assert col not in frame.columns, col


def test_each_leg_appears_only_once_its_warmup_is_affordable():
    """Boundary check on the measured trade: the EMA48 leg costs 47 warm-up
    rows and the EMA220 leg 219, and ml.TabularModel drops any row with a NaN,
    so each leg waits until its warm-up is a quarter of the series or less."""
    just_under_fast = compute_feature_frame(_daily(TREND_MID_MIN_BARS - 1))
    for col in STACK_COLS:
        assert col not in just_under_fast.columns, col

    at_fast = compute_feature_frame(_daily(TREND_MID_MIN_BARS))
    for col in FAST_COLS:
        assert col in at_fast.columns, col
    for col in SLOW_COLS:
        assert col not in at_fast.columns, col

    just_under_slow = compute_feature_frame(_daily(TREND_SLOW_MIN_BARS - 1))
    for col in SLOW_COLS:
        assert col not in just_under_slow.columns, col

    at_slow = compute_feature_frame(_daily(TREND_SLOW_MIN_BARS))
    for col in STACK_COLS:
        assert col in at_slow.columns, col


def test_warmup_is_exactly_the_ema_period_with_no_holes():
    """The columns are NaN for exactly the bars where the EMA does not exist —
    not one row more (which would waste history) and not one row less (which
    would mean an EMA computed from too few points)."""
    frame = compute_feature_frame(_daily(1000))
    expected = {
        "close_vs_ema_26": TREND_FAST - 1,
        "ema26_vs_ema48": TREND_MID - 1,
        "ema48_vs_ema220": TREND_SLOW - 1,
        "trend_stack_1d": TREND_SLOW - 1,
        "trend_stack_run": TREND_SLOW - 1,
    }
    for col, warmup in expected.items():
        values = frame[col]
        assert values.iloc[:warmup].isna().all(), col
        assert values.iloc[warmup:].notna().all(), col  # no interior holes


# --- the stack itself --------------------------------------------------------


def test_stack_state_matches_the_indicator_on_the_same_closes():
    """The feature and the trend card must agree bar for bar, not on average.
    Both are read off the SAME closes through the indicator's own ema() and
    trend_state()."""
    series = _daily(1000, seed=5)
    frame = compute_feature_frame(series)
    closes = [float(v) for v in series.to_numpy()]
    label = {"bullish": 1.0, "bearish": -1.0, "neutral": 0.0}

    for i in range(TREND_SLOW - 1, len(closes), 37):  # sampled: ema() is O(n)
        window = closes[: i + 1]
        published = trend_alignment.trend_state(
            window[-1],
            trend_alignment.ema(window, TREND_FAST),
            trend_alignment.ema(window, TREND_MID),
            trend_alignment.ema(window, TREND_SLOW),
        )
        assert frame["trend_stack_1d"].iloc[i] == label[published], i


def test_trending_and_flat_markets_get_the_states_they_deserve():
    up = compute_feature_frame(_daily(1000, seed=3, drift=0.004, vol=0.001))
    down = compute_feature_frame(_daily(1000, seed=4, drift=-0.004, vol=0.001))
    assert up["trend_stack_1d"].iloc[-1] == 1.0
    assert up["close_vs_ema_26"].iloc[-1] > 0.0
    assert up["ema48_vs_ema220"].iloc[-1] > 0.0
    assert down["trend_stack_1d"].iloc[-1] == -1.0
    assert down["ema26_vs_ema48"].iloc[-1] < 0.0

    # strict comparisons: a flat market has every MA equal, which is a
    # crossover-in-progress at best, and is never reported as a trend
    flat = pd.Series(
        np.full(1000, 8_000_000.0),
        index=pd.date_range(datetime(2023, 1, 1, tzinfo=timezone.utc), periods=1000,
                            freq="D"),
    )
    flat_frame = compute_feature_frame(flat)
    assert (flat_frame["trend_stack_1d"].dropna() == 0.0).all()
    assert (flat_frame["trend_stack_run"].dropna() == 0.0).all()


def test_stack_run_counts_consecutive_bars_and_resets_on_a_flip():
    frame = compute_feature_frame(_daily(1000, seed=11))
    state = frame["trend_stack_1d"]
    run = frame["trend_stack_run"]

    expected, count, previous = [], 0, None
    for value in state.to_numpy():
        if np.isnan(value):
            expected.append(np.nan)
            count, previous = 0, None
            continue
        count = count + 1 if value == previous else 1
        previous = value
        expected.append(value * count)
    pd.testing.assert_series_equal(
        run, pd.Series(expected, index=state.index), check_names=False
    )

    live = run.dropna().to_numpy()
    signs = np.sign(state.dropna().to_numpy())
    assert (np.sign(live) == signs).all()          # signed, as the UI shows it
    assert np.abs(live).max() > 5.0                # runs really do accumulate
    flips = np.flatnonzero(np.diff(state.dropna().to_numpy()) != 0)
    assert len(flips) > 0                          # the test is not vacuous
    assert np.abs(live[flips[0] + 1]) == 1.0       # a new state starts at 1


# --- causality ---------------------------------------------------------------


def test_future_spike_cannot_move_a_single_earlier_row():
    """The strong form: recomputing with one huge future bar appended must
    leave every pre-existing row byte-identical, for every column."""
    series = _daily(1000, seed=29)
    usd = _daily(1000, seed=31, start=90_000.0)
    xau = _daily(1000, seed=37, start=3_000.0, vol=0.006)

    before = compute_feature_frame(series, usd, xau)

    spiked = pd.concat([
        series,
        pd.Series([float(series.iloc[-1]) * 10.0],
                  index=[series.index[-1] + pd.Timedelta(days=1)]),
    ])
    after = compute_feature_frame(spiked, usd, xau)

    assert list(before.columns) == list(after.columns)
    pd.testing.assert_frame_equal(
        after.iloc[: len(before)], before, check_exact=True
    )


def test_snapshot_stack_ignores_rows_observed_after_as_of():
    """Snapshot path (jobs/features.py) end to end: the published stack at
    as_of must not move when later rows are corrupted."""
    series = _daily(1000, seed=41)
    rows = [
        {"symbol": "IR_GOLD_18K", "observed_at": ts, "value": float(v)}
        for ts, v in series.items()
    ]
    prices = pd.DataFrame(rows)
    as_of = series.index[900].to_pydatetime()

    baseline = build_snapshot(prices, as_of)
    assert baseline is not None
    assert "trend_stack_1d" in baseline
    assert "trend_stack_run" in baseline
    assert baseline["ema48_vs_ema220"] == pytest.approx(
        float(compute_feature_frame(series.iloc[:901])["ema48_vs_ema220"].iloc[-1])
    )

    corrupted = prices.copy()
    future = pd.to_datetime(corrupted["observed_at"], utc=True) > pd.Timestamp(as_of)
    assert future.any()
    corrupted.loc[future, "value"] = 1e12
    assert build_snapshot(corrupted, as_of) == baseline


def test_stack_columns_survive_the_daily_close_bucketing():
    """The features are built on the same daily buckets the indicator's 1D
    candles use, so a frame built through daily_close carries them."""
    series = _daily(1000, seed=43)
    prices = pd.DataFrame([
        # two ticks a day: the bucketing must still produce daily bars
        {"symbol": "IR_GOLD_18K", "observed_at": ts + pd.Timedelta(hours=h),
         "value": float(v)}
        for ts, v in series.items() for h in (3, 9)
    ])
    gold = daily_close(prices, "IR_GOLD_18K")
    frame = compute_feature_frame(gold)
    for col in STACK_COLS:
        assert col in frame.columns, col
