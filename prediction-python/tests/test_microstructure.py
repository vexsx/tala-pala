"""Microstructure features: point-in-time correctness, spread math, graceful
degradation when a payload key is absent, and the premium-decomposition
identity on an analytically known case."""
from __future__ import annotations

import math
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.features.microstructure import (
    AGE_COLUMN,
    DECOMPOSITION_FEATURES,
    DISPERSION_KEY,
    FEATURE_REGISTRY,
    FEATURE_SPECS,
    POLICY_FFILL,
    POLICY_NAN_ON_GAP,
    PRIMARY_PROVIDER,
    PRIMARY_SYMBOL,
    STREAM_FEATURES,
    FeatureSpec,
    asof_join,
    build_microstructure_frame,
    decompose_move,
    decompose_premium_move,
    normalize_observations,
    registry_table,
    unavailable_features,
)

START = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)
CADENCE = timedelta(minutes=10)  # the production collect cadence


def _observations(
    n: int = 200,
    start_rial: float = 1_880_000_000.0 / 10,
    spreads: list[float] | None = None,
    dispersion: list | None = None,
    seed: int = 5,
    cadence: timedelta = CADENCE,
) -> pd.DataFrame:
    """Hamrah-Gold-shaped raw_observations rows (rial raw_value + payload)."""
    rng = np.random.default_rng(seed)
    rows = []
    mid = start_rial
    for i in range(n):
        mid *= 1.0 + rng.normal(0.0, 0.0008)
        spread_pct = spreads[i] if spreads is not None else 0.45 + 0.05 * math.sin(i / 7.0)
        half = mid * spread_pct / 200.0
        payload = {
            "sell_rial": mid + half,
            "buy_rial": mid - half,
            "spread_pct": round(spread_pct, 4),
            "sides": 2,
        }
        if dispersion is not None:
            payload[DISPERSION_KEY] = dispersion[i]
        rows.append(
            {
                "provider_code": PRIMARY_PROVIDER,
                "symbol": PRIMARY_SYMBOL,
                "raw_value": mid,
                "unit": "IRR/gram",
                "currency": "IRR",
                "raw_payload": payload,
                "observed_at": START + i * cadence,
            }
        )
    return pd.DataFrame(rows)


def _assert_rows_equal(left: pd.Series, right: pd.Series, label: str) -> None:
    assert list(left.index) == list(right.index), label
    for column in left.index:
        a, b = left[column], right[column]
        if pd.isna(a) and pd.isna(b):
            continue
        assert a == pytest.approx(b, rel=1e-12, abs=1e-12), f"{label}: {column}"


# --- point-in-time correctness (the leakage guard) ---------------------------


def test_rows_unchanged_when_future_observations_are_appended():
    """THE leakage test: a row at time T must be bit-identical whether or not
    observations after T exist in the input."""
    full = _observations(200)
    cutoff = 140
    truncated = full.iloc[:cutoff]

    frame_full = build_microstructure_frame(full)
    frame_trunc = build_microstructure_frame(truncated)

    assert list(frame_trunc.index) == list(frame_full.index[:cutoff])
    for position in (cutoff - 1, cutoff - 2, 120, 100, 61):
        stamp = frame_trunc.index[position]
        _assert_rows_equal(
            frame_full.loc[stamp], frame_trunc.loc[stamp], f"row {stamp}"
        )
    # the deep rows are genuinely populated, not vacuously all-NaN
    assert frame_trunc.iloc[cutoff - 1].notna().sum() >= len(STREAM_FEATURES) - 2


def test_future_rows_are_ignored_when_as_of_is_given():
    """Corrupting every observation after as_of must not move any row."""
    full = _observations(200)
    as_of = START + 139 * CADENCE
    baseline = build_microstructure_frame(full.iloc[:140])

    corrupted = full.copy()
    future = pd.to_datetime(corrupted["observed_at"], utc=True) > pd.Timestamp(as_of)
    assert future.any()
    corrupted.loc[future, "raw_value"] = 1e15
    corrupted.loc[future, "raw_payload"] = corrupted.loc[future, "raw_payload"].map(
        lambda _: {"sell_rial": 2e15, "buy_rial": 1.0, "spread_pct": 99.0, "sides": 2}
    )

    guarded = build_microstructure_frame(corrupted, as_of=as_of)
    pd.testing.assert_frame_equal(guarded, baseline)


def test_asof_join_never_reads_a_future_feature_row():
    features = build_microstructure_frame(_observations(120))
    stamp = features.index[80]
    joined = asof_join(features, [stamp - timedelta(seconds=1), stamp])

    # one second early: the row at `stamp` does not exist yet, so the join
    # falls BACK to the previous observation and never forward to `stamp`
    assert joined.iloc[0]["dealer_spread_pct"] == pytest.approx(
        features.iloc[79]["dealer_spread_pct"]
    )
    assert joined.iloc[0]["dealer_spread_pct"] != features.loc[stamp, "dealer_spread_pct"]
    assert joined.iloc[0][AGE_COLUMN] == pytest.approx(CADENCE.total_seconds() - 1)
    # exactly on the observation: the value observed then, zero age
    assert joined.iloc[1]["dealer_spread_pct"] == pytest.approx(
        features.loc[stamp, "dealer_spread_pct"]
    )
    assert joined.iloc[1][AGE_COLUMN] == pytest.approx(0.0)
    # before the stream starts there is nothing to know
    before = asof_join(features, [features.index[0] - timedelta(hours=1)])
    assert before[list(STREAM_FEATURES)].isna().all(axis=None)


def test_asof_join_carries_forward_only_within_the_staleness_threshold():
    features = build_microstructure_frame(_observations(120))
    last = features.index[-1]
    spec = FEATURE_REGISTRY["dealer_spread_pct"]
    fresh = last + timedelta(seconds=spec.staleness_seconds - 60)
    stale = last + timedelta(seconds=spec.staleness_seconds + 60)

    joined = asof_join(features, [fresh, stale])
    assert joined.iloc[0]["dealer_spread_pct"] == pytest.approx(
        features.iloc[-1]["dealer_spread_pct"]
    )
    assert pd.isna(joined.iloc[1]["dealer_spread_pct"])
    assert joined.iloc[1][AGE_COLUMN] == pytest.approx(spec.staleness_seconds + 60)


def test_asof_join_never_carries_forward_nan_on_gap_features():
    """A jump flag is only true of the instant it was computed for."""
    features = build_microstructure_frame(_observations(120))
    stamp = features.index[-1]
    joined = asof_join(features, [stamp, stamp + timedelta(seconds=1)])
    assert FEATURE_REGISTRY["jump_flag"].missing_policy == POLICY_NAN_ON_GAP
    assert joined.iloc[0]["jump_flag"] == features.iloc[-1]["jump_flag"]
    assert pd.isna(joined.iloc[1]["jump_flag"])


def test_asof_join_preserves_caller_row_order():
    features = build_microstructure_frame(_observations(120))
    stamps = [features.index[90], features.index[30], features.index[60]]
    joined = asof_join(features, stamps)
    assert list(joined.index) == stamps
    for position, stamp in enumerate(stamps):
        assert joined.iloc[position]["dealer_spread_pct"] == pytest.approx(
            features.loc[stamp, "dealer_spread_pct"]
        )


# --- dealer spread math ------------------------------------------------------


def test_spread_z_score_and_percentile_on_a_known_series():
    """Trailing z-score = (x - mean)/std over the last 60 spreads, ddof=1."""
    window = 60
    rng = np.random.default_rng(3)
    spreads = list(np.round(rng.uniform(0.30, 0.70, 200), 4))
    frame = build_microstructure_frame(_observations(200, spreads=spreads))

    position = 150
    history = pd.Series(spreads[position - window + 1: position + 1])
    expected_z = (spreads[position] - history.mean()) / history.std(ddof=1)
    row = frame.iloc[position]
    assert row[f"dealer_spread_z_{window}"] == pytest.approx(expected_z, rel=1e-9)
    # percentile = rank of the current value inside the same window, in [0, 1],
    # ties averaged (pandas' default ranking method)
    below = (history < spreads[position]).sum()
    at_or_below = (history <= spreads[position]).sum()
    expected_pctile = (below + at_or_below + 1) / 2.0 / window
    assert row[f"dealer_spread_pctile_{window}"] == pytest.approx(expected_pctile)
    # z-scores need the full window: earlier rows stay NaN rather than guess
    assert pd.isna(frame.iloc[window - 2][f"dealer_spread_z_{window}"])


def test_spread_widening_and_compression_have_the_expected_sign():
    widening = [0.30 + 0.01 * i for i in range(120)]
    compressing = [1.50 - 0.01 * i for i in range(120)]
    wide = build_microstructure_frame(_observations(120, spreads=widening)).iloc[-1]
    tight = build_microstructure_frame(_observations(120, spreads=compressing)).iloc[-1]

    assert wide["dealer_spread_chg_5"] == pytest.approx(0.05)
    assert wide["dealer_spread_ratio_20"] > 1.0
    assert wide["dealer_spread_pctile_60"] == pytest.approx(1.0)
    assert tight["dealer_spread_chg_5"] == pytest.approx(-0.05)
    assert tight["dealer_spread_ratio_20"] < 1.0


def test_spread_recomputed_from_sides_when_stored_pct_is_missing():
    """Provider algebra: (sell - buy) / midpoint * 100, rial cancels out."""
    observations = _observations(3)
    observations["raw_payload"] = [
        {"sell_rial": 1_000_000.0, "buy_rial": 990_000.0, "sides": 2},
        {"sell_rial": 1_000_000.0, "buy_rial": None, "sides": 1},  # one-sided
        {"spread_pct": None, "sides": 1},
    ]
    spread = build_microstructure_frame(observations)["dealer_spread_pct"]
    assert spread.iloc[0] == pytest.approx(10_000.0 / 995_000.0 * 100.0)
    assert pd.isna(spread.iloc[1])
    assert pd.isna(spread.iloc[2])


# --- provider dispersion: absent key must degrade, never crash ---------------


def test_dispersion_absent_degrades_to_nan_and_is_reported_unavailable():
    observations = _observations(120)  # payloads without the peer_dispersion key
    frame = build_microstructure_frame(observations)

    assert frame["peer_dispersion_pct"].isna().all()
    assert frame["peer_dispersion_z_60"].isna().all()
    assert set(unavailable_features(observations)) == {
        "peer_dispersion_pct",
        "peer_dispersion_z_60",
    }
    # the rest of the frame is unaffected by the missing key
    assert frame["dealer_spread_pct"].notna().all()


def test_dispersion_present_is_read_and_reported_available():
    values = list(np.linspace(0.2, 1.4, 120))
    observations = _observations(120, dispersion=values)
    frame = build_microstructure_frame(observations)

    assert frame["peer_dispersion_pct"].to_numpy() == pytest.approx(values)
    assert frame["peer_dispersion_z_60"].iloc[-1] > 0.0  # rising dispersion
    assert unavailable_features(observations) == ()


def test_dispersion_malformed_payload_shapes_do_not_crash():
    shapes = ["wide", None, {"unrelated": 1}, float("nan"), True, {"pct": 0.8}, 0.5]
    observations = _observations(len(shapes), dispersion=shapes)
    values = build_microstructure_frame(observations)["peer_dispersion_pct"]

    assert values.iloc[:5].isna().all()      # unusable shapes -> NaN
    assert values.iloc[5] == pytest.approx(0.8)  # mapping with a percent field
    assert values.iloc[6] == pytest.approx(0.5)  # bare number
    assert unavailable_features(observations) == ()


# --- cadence, staleness, volatility, jumps ----------------------------------


def test_update_frequency_and_staleness_track_the_observation_series():
    frame = build_microstructure_frame(_observations(200))
    row = frame.iloc[-1]
    assert row["obs_gap_seconds"] == pytest.approx(CADENCE.total_seconds())
    assert row["obs_gap_ratio_20"] == pytest.approx(1.0)
    assert row["obs_per_hour_6h"] == pytest.approx(6.0)   # 10-minute cadence
    assert row["obs_per_hour_24h"] == pytest.approx(6.0)
    assert pd.isna(frame.iloc[0]["obs_gap_seconds"])      # nothing before it

    # a stalled source: the last gap is six times the trailing median
    stalled = _observations(60)
    stalled.loc[stalled.index[-1], "observed_at"] = START + 59 * CADENCE + timedelta(minutes=50)
    last = build_microstructure_frame(stalled).iloc[-1]
    assert last["obs_gap_seconds"] == pytest.approx(3600.0)
    assert last["obs_gap_ratio_20"] == pytest.approx(6.0)


def test_realized_volatility_matches_the_manual_standard_deviation():
    frame = build_microstructure_frame(_observations(200))
    prices = _observations(200)["raw_value"].to_numpy()
    log_returns = pd.Series(np.diff(np.log(prices)))

    expected_short = log_returns.iloc[-12:].std(ddof=1)
    expected_long = log_returns.iloc[-48:].std(ddof=1)
    row = frame.iloc[-1]
    assert row["realized_vol_12"] == pytest.approx(expected_short, rel=1e-9)
    assert row["realized_vol_48"] == pytest.approx(expected_long, rel=1e-9)
    assert row["realized_vol_ratio_12_48"] == pytest.approx(
        expected_short / expected_long, rel=1e-9
    )


def test_jump_indicator_fires_only_on_the_jump():
    """The robust scale comes from the PRIOR window, so a single large move
    cannot hide inside the threshold it sets."""
    n = 120
    prices = [1_000_000.0 * (1.0 + 0.0005 * (-1) ** i) for i in range(n)]
    jump_at = 100
    for i in range(jump_at, n):
        prices[i] *= 1.10  # a one-off 10% level shift
    observations = _observations(n)
    observations["raw_value"] = prices

    frame = build_microstructure_frame(observations)
    flags = frame["jump_flag"].dropna()
    assert flags.sum() == 1.0
    assert frame["jump_flag"].iloc[jump_at] == 1.0
    assert frame["jump_z"].iloc[jump_at] > 4.0
    assert frame["jump_flag"].iloc[jump_at + 1] == 0.0


# --- premium decomposition ---------------------------------------------------


def test_decompose_move_pure_fx_move_leaves_no_residual():
    """18k rises exactly with the dollar: all of it is FX, nothing local."""
    result = decompose_move(
        k18_start=10_000_000.0, k18_end=11_000_000.0,
        usd_start=100_000.0, usd_end=110_000.0,
        xau_start=3_300.0, xau_end=3_300.0,
    )
    assert result.dlog_total == pytest.approx(math.log(1.1))
    assert result.dlog_fx == pytest.approx(math.log(1.1))
    assert result.dlog_gold == pytest.approx(0.0)
    assert result.dlog_premium == pytest.approx(0.0, abs=1e-12)  # float noise only
    assert result.pct_total == pytest.approx(10.0)


def test_decompose_move_known_three_way_split():
    """Analytic case: USD +10%, XAU +5%, local premium factor +2%.

    From the parity identity P = xau * usd * m * const, the 18k price must
    move by exactly 1.10 * 1.05 * 1.02, and the decomposition must return
    ln(1.10), ln(1.05), ln(1.02) — no regression, no estimation error.
    """
    result = decompose_move(
        k18_start=10_000_000.0,
        k18_end=10_000_000.0 * 1.10 * 1.05 * 1.02,
        usd_start=100_000.0, usd_end=110_000.0,
        xau_start=3_300.0, xau_end=3_300.0 * 1.05,
    )
    assert result.dlog_fx == pytest.approx(math.log(1.10), rel=1e-12)
    assert result.dlog_gold == pytest.approx(math.log(1.05), rel=1e-12)
    assert result.dlog_premium == pytest.approx(math.log(1.02), rel=1e-9)
    # the parts re-sum to the whole, exactly
    assert result.dlog_fx + result.dlog_gold + result.dlog_premium == pytest.approx(
        result.dlog_total, rel=1e-15
    )


def test_decompose_move_handles_missing_and_non_positive_inputs():
    result = decompose_move(10_000_000.0, 11_000_000.0, 0.0, 110_000.0, 3_300.0, None)
    assert math.isnan(result.dlog_fx)
    assert math.isnan(result.dlog_gold)
    assert math.isnan(result.dlog_premium)
    assert result.dlog_total == pytest.approx(math.log(1.1))


def test_decompose_premium_move_series_matches_the_scalar_helper():
    index = pd.date_range(START, periods=40, freq="D", tz="UTC")
    rng = np.random.default_rng(17)
    usd = pd.Series(100_000.0 * np.exp(np.cumsum(rng.normal(0, 0.004, 40))), index=index)
    xau = pd.Series(3_300.0 * np.exp(np.cumsum(rng.normal(0, 0.006, 40))), index=index)
    premium = pd.Series(1.0 + 0.02 * np.sin(np.arange(40) / 5.0), index=index)
    k18 = xau / 31.1034768 * usd * 0.750 * premium

    frame = decompose_premium_move(k18, usd, xau)
    scalar = decompose_move(
        k18.iloc[-2], k18.iloc[-1], usd.iloc[-2], usd.iloc[-1], xau.iloc[-2], xau.iloc[-1]
    )
    row = frame.iloc[-1]
    assert row["dlog_18k"] == pytest.approx(scalar.dlog_total, rel=1e-12)
    assert row["fx_contrib_log"] == pytest.approx(scalar.dlog_fx, rel=1e-12)
    assert row["gold_contrib_log"] == pytest.approx(scalar.dlog_gold, rel=1e-12)
    # the residual is exactly the change in the local premium factor
    assert row["premium_contrib_log"] == pytest.approx(
        math.log(premium.iloc[-1] / premium.iloc[-2]), rel=1e-9
    )
    assert row["fx_share"] + row["gold_share"] + row["premium_share"] == pytest.approx(1.0)
    assert pd.isna(frame.iloc[0]["dlog_18k"])  # no prior row to difference against


def test_decompose_premium_move_does_not_peek_at_later_aux_rows():
    index = pd.date_range(START, periods=40, freq="D", tz="UTC")
    rng = np.random.default_rng(19)
    usd = pd.Series(100_000.0 * np.exp(np.cumsum(rng.normal(0, 0.004, 40))), index=index)
    xau = pd.Series(3_300.0 * np.exp(np.cumsum(rng.normal(0, 0.006, 40))), index=index)
    k18 = xau / 31.1034768 * usd * 0.750

    full = decompose_premium_move(k18, usd, xau)
    # gold truncated, FX and global gold left FULL: later aux rows must not
    # change any earlier row
    truncated = decompose_premium_move(k18.iloc[:25], usd, xau)
    _assert_rows_equal(full.iloc[24], truncated.iloc[24], "row 24")


# --- registry ----------------------------------------------------------------


def test_registry_describes_exactly_the_produced_columns():
    produced = set(build_microstructure_frame(_observations(80)).columns) | set(
        decompose_premium_move(
            pd.Series([1.0, 2.0], index=pd.date_range(START, periods=2, tz="UTC"))
        ).columns
    )
    assert produced == set(FEATURE_REGISTRY)
    assert set(STREAM_FEATURES) | set(DECOMPOSITION_FEATURES) == set(FEATURE_REGISTRY)
    assert len(FEATURE_SPECS) == len(FEATURE_REGISTRY)  # no duplicate names


def test_every_spec_carries_complete_metadata():
    for spec in FEATURE_SPECS:
        assert spec.name and spec.version and spec.description, spec.name
        assert spec.provider and spec.symbol and spec.lookback, spec.name
        assert spec.missing_policy in (POLICY_FFILL, POLICY_NAN_ON_GAP), spec.name
        assert spec.staleness_seconds >= 0, spec.name
        # a value that is never carried forward has no staleness budget
        if spec.missing_policy == POLICY_NAN_ON_GAP:
            assert spec.staleness_seconds == 0, spec.name
        else:
            assert spec.staleness_seconds > 0, spec.name


def test_registry_is_immutable():
    with pytest.raises(TypeError):
        FEATURE_REGISTRY["dealer_spread_pct"] = FEATURE_SPECS[0]
    with pytest.raises(FrozenInstanceError):
        FEATURE_SPECS[0].name = "renamed"


def test_registry_table_documents_every_feature():
    table = registry_table()
    assert len(table) == len(FEATURE_SPECS)
    assert set(table["name"]) == set(FEATURE_REGISTRY)
    assert {f.name for f in FeatureSpec.__dataclass_fields__.values()} <= set(table.columns)


# --- observation normalization ----------------------------------------------


def test_rial_raw_values_are_converted_to_toman_exactly_once():
    observations = _observations(3)
    normalized = normalize_observations(observations)
    assert normalized["value"].to_numpy() == pytest.approx(
        observations["raw_value"].to_numpy() / 10.0
    )
    # an already-normalized frame (prices-style) is taken as-is
    prices_style = observations.drop(columns=["raw_value", "currency"]).assign(
        value=[1.0, 2.0, 3.0]
    )
    assert normalize_observations(prices_style)["value"].to_numpy() == pytest.approx(
        [1.0, 2.0, 3.0]
    )


def test_other_providers_and_symbols_are_not_mixed_into_the_stream():
    observations = _observations(60)
    foreign = observations.copy()
    foreign["provider_code"] = "bitmax"
    foreign["symbol"] = "USD_IRT"
    frame = build_microstructure_frame(pd.concat([observations, foreign]))
    assert len(frame) == 60


def test_empty_input_returns_the_registered_columns():
    frame = build_microstructure_frame(pd.DataFrame())
    assert list(frame.columns) == list(STREAM_FEATURES)
    assert frame.empty
    joined = asof_join(frame, [START])
    assert list(joined.index) == [pd.Timestamp(START)]
    assert joined[AGE_COLUMN].isna().all()
