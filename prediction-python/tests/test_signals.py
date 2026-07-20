"""Signal engine: freshness gate, bullish/bearish mapping, careful wording."""
from __future__ import annotations

import re

from app.signals.engine import SignalInputs, compute_signal

FORBIDDEN_WORDING = re.compile(
    r"guarantee|guaranteed|certainly|will definitely|cannot lose|risk-free|sure thing",
    re.IGNORECASE,
)


def _bullish_inputs(**overrides) -> SignalInputs:
    base = dict(
        expected_change_pct={"1d": 3.5, "3d": 4.2, "7d": 5.0},
        confidence={"1d": 0.8, "3d": 0.75, "7d": 0.7},
        last_price=8_500_000.0,
        sma20=8_200_000.0,
        sma50=8_000_000.0,
        rsi14=55.0,
        momentum_10_pct=3.0,
        premium_z=-1.2,
        regime="trending_up",
        data_fresh=True,
    )
    base.update(overrides)
    return SignalInputs(**base)


def test_stale_data_forces_hold():
    result = compute_signal(_bullish_inputs(data_fresh=False))
    assert result["signal"] == "hold"
    assert result["score"] == 50
    assert result["data_fresh"] is False
    assert any("stale" in r.lower() for r in result["risks"])
    assert result["confidence"] < 0.5  # confidence collapses on stale data


def test_strong_bullish_inputs_give_buy_or_strong_buy():
    result = compute_signal(_bullish_inputs())
    assert result["signal"] in ("buy", "strong_buy")
    assert result["score"] >= 60
    assert result["supporting"]
    assert result["data_fresh"] is True


def test_strong_bearish_inputs_give_sell_side():
    result = compute_signal(
        _bullish_inputs(
            expected_change_pct={"1d": -3.5, "3d": -4.0, "7d": -5.0},
            last_price=7_500_000.0,
            sma20=7_900_000.0,
            sma50=8_100_000.0,
            rsi14=75.0,
            momentum_10_pct=-3.5,
            premium_z=2.0,
            regime="trending_down",
        )
    )
    assert result["signal"] in ("sell", "strong_sell")
    assert result["score"] <= 40
    assert result["conflicting"]


def test_neutral_inputs_hold():
    result = compute_signal(
        SignalInputs(
            expected_change_pct={"1d": 0.1},
            confidence={"1d": 0.5},
            last_price=8_000_000.0,
            sma20=8_000_000.0,
            sma50=8_000_000.0,
            rsi14=50.0,
            momentum_10_pct=0.0,
            premium_z=0.0,
            data_fresh=True,
        )
    )
    assert result["signal"] == "hold"


def test_wording_contains_no_guarantees():
    for inputs in (
        _bullish_inputs(),
        _bullish_inputs(data_fresh=False),
        _bullish_inputs(expected_change_pct={"1d": -4.0}, momentum_10_pct=-3.0),
    ):
        result = compute_signal(inputs)
        blob = " ".join(
            [result["explanation"], result["invalidation"]]
            + result["supporting"] + result["conflicting"] + result["risks"]
        )
        assert not FORBIDDEN_WORDING.search(blob), blob
        # hedged framing + explicit uncertainty disclaimer present
        assert "not financial advice" in result["explanation"]


def test_row_shape_and_review_at():
    result = compute_signal(_bullish_inputs())
    for key in ("generated_at", "signal", "score", "confidence", "explanation",
                "supporting", "conflicting", "risks", "invalidation", "review_at",
                "data_fresh", "inputs"):
        assert key in result, key
    delta = result["review_at"] - result["generated_at"]
    assert delta.total_seconds() == 6 * 3600
    assert 0 <= result["score"] <= 100
    assert isinstance(result["inputs"], dict)


def test_high_premium_penalizes_buying():
    rich = compute_signal(_bullish_inputs(premium_z=2.5))
    cheap = compute_signal(_bullish_inputs(premium_z=-1.2))
    assert rich["score"] < cheap["score"]
