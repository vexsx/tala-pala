"""One feature space per run — proven on the real fold structure.

Two defects in the 1D trend-alignment feature made the matrix' SHAPE depend on
the frame handed to it, and walk-forward hands it a different frame every fold:

* the "1D" stack was grown on the HOURLY series, because the spacing was
  inferred from the median gap over the whole index and production's hourly
  series is one bucket per day for its long pre-2026-07-19 era;
* the EMA220 leg appeared only once a fold's prefix was long enough, which at
  production's ~1224 daily bars is fold 28 of 39 — i.e. in the holdout and the
  final refit, and in none of the selection folds that chose the winner.

The invariant enforced here: the feature column set is identical in every
selection fold, every holdout fold and the final refit. A candidate must be
scored in the feature space it is later shipped in.
"""
from __future__ import annotations

from datetime import timezone

import numpy as np
import pandas as pd
import pytest

from app.features.engineering import (
    FRAME_SPACING_KEY,
    MIN_HISTORY_KEY,
    SPACING_DAILY,
    SPACING_INTRADAY,
    TREND_MID,
    TREND_SLOW,
    WALK_FORWARD_MIN_BARS,
    daily_close,
    hourly_close,
    infer_bar_spacing,
)
from app.models.ml import _feature_matrix
from app.models.training import (
    FRAME_SPACING,
    HORIZON_SPECS,
    MAX_FOLDS,
    MIN_TRAIN_POINTS,
    Fold,
    _fold_step,
    split_folds,
)

STACK_COLS = (
    "close_vs_ema_26",
    "ema26_vs_ema48",
    "ema48_vs_ema220",
    "trend_stack_1d",
    "trend_stack_run",
)


def _series(n, seed=17, start=8_000_000.0, drift=0.0004, vol=0.008, freq="D"):
    rng = np.random.default_rng(seed)
    values = start * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    index = pd.date_range("2023-01-01", periods=n, freq=freq, tz="UTC")
    return pd.Series(values, index=index)


def _training_context(series: pd.Series, freq: str = "daily") -> dict:
    """The context train_all builds: exog series + the run's declarations."""
    return {
        "usd_irt": _series(len(series), seed=31, start=90_000.0, vol=0.004),
        "xau_usd": _series(len(series), seed=37, start=3_000.0, vol=0.006),
        FRAME_SPACING_KEY: FRAME_SPACING[freq],
        MIN_HISTORY_KEY: MIN_TRAIN_POINTS,
    }


# --- the declarations the trainer makes --------------------------------------


def test_walk_forward_min_bars_mirrors_the_trainer():
    """engineering.WALK_FORWARD_MIN_BARS is a copy of training.MIN_TRAIN_POINTS
    (the feature layer cannot import the trainer, which imports it). If the
    trainer's first fold moves and this constant does not, the admission rule
    starts describing a fold that no longer exists."""
    assert WALK_FORWARD_MIN_BARS == MIN_TRAIN_POINTS


def test_every_horizon_declares_its_spacing():
    for horizon, (freq, _steps) in HORIZON_SPECS.items():
        assert freq in FRAME_SPACING, horizon
    assert FRAME_SPACING["daily"] == SPACING_DAILY
    # 1h/4h train on hourly buckets: never a 1D frame, whatever their gaps
    # happen to look like today
    assert FRAME_SPACING["hourly"] == SPACING_INTRADAY


# --- defect 1: the production-shaped hourly series ---------------------------


def _production_shaped_prices() -> pd.DataFrame:
    """The documented production history: one observation per day until
    2026-07-19, 5-minute ticks after it."""
    rng = np.random.default_rng(7)
    switch = pd.Timestamp("2026-07-19", tz="UTC")
    daily_idx = pd.date_range(end=switch, periods=1205, freq="D")
    tick_idx = pd.date_range(
        start=switch + pd.Timedelta(minutes=5),
        end=pd.Timestamp("2026-08-12 12:00", tz="UTC"),
        freq="5min",
    )
    index = daily_idx.append(tick_idx)
    values = 8_000_000.0 * np.exp(np.cumsum(rng.normal(0.0002, 0.004, len(index))))
    return pd.DataFrame(
        {"symbol": "IR_GOLD_18K", "observed_at": index, "value": values}
    )


def test_hourly_series_of_the_production_shape_gets_no_stack_columns():
    """The 1h/4h horizons train on hourly_close over the WHOLE history, so
    their series is a long daily-spaced era followed by an hourly one: most of
    its gaps are 86400s. Measured on this shape, a median-over-the-whole-index
    test calls it daily and grows an "EMA220" spanning ~9 days of hourly
    closes. It must get no 1D columns — by the trainer's declaration AND by
    inference, since the predict-time refit declares nothing."""
    prices = _production_shaped_prices()
    hourly = hourly_close(prices, "IR_GOLD_18K")

    gaps = pd.Series(pd.DatetimeIndex(hourly.index)).diff().dt.total_seconds().dropna()
    assert (gaps == 86400).sum() > (gaps == 3600).sum()  # the trap: median is daily
    assert float(gaps.median()) == 86400.0

    assert infer_bar_spacing(hourly.index) == SPACING_INTRADAY
    for context in (_training_context(hourly, "hourly"), None):
        frame = _feature_matrix(hourly, context)
        for col in STACK_COLS:
            assert col not in frame.columns, col

    # the DAILY series off the same prices is still a daily frame
    daily = daily_close(prices, "IR_GOLD_18K")
    assert infer_bar_spacing(daily.index) == SPACING_DAILY


def test_one_intraday_bucket_is_enough_to_stop_calling_a_frame_daily():
    """No knife-edge: the switch does not wait for the intraday era to
    outnumber the daily one (~50 days of collection at production's rates)."""
    daily = _series(1205, freq="D")
    hourly_tail = pd.Series(
        [float(daily.iloc[-1])] * 3,
        index=pd.date_range(
            daily.index[-1] + pd.Timedelta(hours=1), periods=3, freq="h"
        ),
    )
    mixed = pd.concat([daily, hourly_tail])
    assert infer_bar_spacing(daily.index) == SPACING_DAILY
    assert infer_bar_spacing(mixed.index) == SPACING_INTRADAY


# --- defect 2: one column set for the whole run ------------------------------


@pytest.mark.parametrize(
    "n,horizon_steps",
    [
        (1224, 1),    # production today: the slow leg used to arrive at fold 28/39
        (1224, 30),
        (1000, 7),
        (1300, 1),    # past the band where the old code self-corrected
    ],
)
def test_column_set_is_identical_in_selection_holdout_and_final_refit(n, horizon_steps):
    """Walks the REAL fold geometry: walk_forward's fold spacing, split_folds'
    embargoed selection/holdout split, and _build_final_model's refit on the
    full series."""
    series = _series(n)
    context = _training_context(series)

    step = _fold_step(series, horizon_steps, MAX_FOLDS)
    nows = list(range(MIN_TRAIN_POINTS - 1, n - horizon_steps, step))[:MAX_FOLDS + 1]
    folds = [
        Fold(t_index=i, t_time=series.index[i].to_pydatetime(),
             base=1.0, pred=1.0, actual=1.0)
        for i in nows
    ]
    selection, holdout = split_folds(folds, horizon_steps, step)
    assert selection and holdout, "the split must actually happen for this to test it"

    def columns_at(prefix_len):
        return frozenset(_feature_matrix(series.iloc[:prefix_len], context).columns)

    selection_sets = {columns_at(f.t_index + 1) for f in selection}
    holdout_sets = {columns_at(f.t_index + 1) for f in holdout}
    final_set = columns_at(n)  # _build_final_model refits on the whole series

    assert len(selection_sets) == 1, "selection folds disagree about the columns"
    assert selection_sets == holdout_sets, "holdout is scored on other columns"
    assert next(iter(selection_sets)) == final_set, (
        "the artifact ships in a feature space it was never selected in"
    )


def test_the_run_wide_set_is_the_fast_pair_at_production_history():
    """The honest consequence, pinned with its numbers rather than smoothed
    over: walk-forward's first fold trains on 60 bars, which can compute an
    EMA48 (48 <= 60) and cannot compute an EMA220 (220 > 60). So at 1224 daily
    bars the models get the two fast distances, and the EMA220 leg plus the
    categorical stack are not offered to ANY stage."""
    series = _series(1224)
    columns = _feature_matrix(series, _training_context(series)).columns
    assert TREND_MID <= MIN_TRAIN_POINTS < TREND_SLOW
    assert "close_vs_ema_26" in columns
    assert "ema26_vs_ema48" in columns
    for col in ("ema48_vs_ema220", "trend_stack_1d", "trend_stack_run"):
        assert col not in columns, col


def test_predict_time_refit_sees_the_training_column_set():
    """predicting.py refits the loaded artifact on the freshest series with a
    context that carries only the exog series — no declarations. The defaults
    must land it in the same feature space the artifact was certified in,
    otherwise train/serve alignment breaks at the last step."""
    series = _series(1224)
    trained = _feature_matrix(series, _training_context(series))
    serve_context = {
        k: v for k, v in _training_context(series).items()
        if k not in (FRAME_SPACING_KEY, MIN_HISTORY_KEY)
    }
    served = _feature_matrix(series, serve_context)
    assert list(trained.columns) == list(served.columns)


def test_horizon_may_change_the_set_between_runs_but_never_within_one():
    """The invariant is per run. Two runs at different horizons are allowed to
    differ; what is forbidden is a single run changing its mind."""
    series = _series(1224)
    context = _training_context(series)
    sets = []
    for horizon_steps in (1, 7, 30):
        step = _fold_step(series, horizon_steps, MAX_FOLDS)
        nows = list(range(MIN_TRAIN_POINTS - 1, len(series) - horizon_steps, step))
        per_run = {
            frozenset(_feature_matrix(series.iloc[: i + 1], context).columns)
            for i in (nows[0], nows[len(nows) // 2], nows[-1])
        }
        assert len(per_run) == 1
        sets.append(next(iter(per_run)))
    assert all(s == sets[0] for s in sets)  # true today; not required by the rule


def test_index_timezone_is_not_what_decides_the_spacing():
    """Naive timestamps are still daily bars (build_snapshot localizes, other
    callers may not)."""
    naive = _series(300)
    naive.index = naive.index.tz_convert(timezone.utc).tz_localize(None)
    assert infer_bar_spacing(naive.index) == SPACING_DAILY
