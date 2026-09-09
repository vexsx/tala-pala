"""Deterministic market fixtures shared by the multi-symbol signal tests.

Not a test module (the leading underscore keeps pytest from collecting it):
it seeds a small but complete world — prices, model forecasts, a dealer
spread quote and a live trend-alignment state — so that the same history can
be scored by the gold path and by the generalised per-symbol path and the two
compared.

Timestamps are anchored on ``utcnow()`` and walk BACKWARDS one UTC day per
bar, so the newest observation is always "now".  That matters: seeding
forwards from a fixed start (as the e2e smoke test does) leaves the last bar a
day old, the freshness gate fires and every signal collapses to the same
forced hold — which would make a regression test that compares scores prove
nothing at all.
"""
from __future__ import annotations

import itertools
from datetime import timedelta
from typing import Optional, Sequence

import numpy as np

from app.db import (
    predictions,
    prices,
    raw_observations,
    trend_alignment_events,
    trend_alignment_states,
    utcnow,
)

# Long enough for every history gate the engine declares, the binding one
# being FACTOR_MA_ALIGNMENT: a 220-bar window plus a further 220 before it may
# be read as a trend (see app/signals/universe.py::FactorSpec) = 440.  The
# next longest is the SMA trend factor's 100, then the 60-bar premium
# z-score, then detect_regime's 45.
#
# This is a fixture for the symbols that HAVE deep history, and it has to look
# like them: production holds 18,593 IR_GOLD_18K rows from 2013 and 11,678
# XAUUSD rows from 2021, which is thousands of daily bars each.  A 260-bar
# fixture used to be enough only because the alignment factor was gated at the
# bar count where a 220-period MA first computes rather than where it means
# anything, and a fixture that quietly depended on that would have hidden the
# change instead of pinning it.
GOLD_DAYS = 480

_SERIES_SPEC = {
    # symbol: (start value, drift, sigma, currency, unit)
    "IR_GOLD_18K": (6_000_000.0, 0.0020, 0.0080, "IRT", "gram"),
    "USD_IRT": (90_000.0, 0.0010, 0.0040, "IRT", "usd"),
    "XAUUSD": (3_000.0, 0.0005, 0.0060, "USD", "ozt"),
    "IR_COIN_EMAMI": (700_000_000.0, 0.0018, 0.0090, "IRT", "coin"),
    "XAGUSD": (32.0, 0.0004, 0.0110, "USD", "ozt"),
    "IR_GOLD_FUND_AYAR": (42_000.0, 0.0015, 0.0070, "IRT", "unit"),
    "IR_GOLD_FUND_TALA": (39_000.0, 0.0014, 0.0072, "IRT", "unit"),
    "IR_GOLD_FUND_FLOW": (12.0, 0.0, 0.0, "PCT", "pct"),
}


def price_rows(symbol: str, days: int, *, seed: int) -> list[dict]:
    """``days`` daily rows for ``symbol``, newest at ``utcnow()``."""
    start_value, drift, sigma, currency, unit = _SERIES_SPEC[symbol]
    rng = np.random.default_rng(seed)
    now = utcnow()
    rows: list[dict] = []
    value = start_value
    for i in range(days):
        # i = days-1 is the OLDEST bar; the loop below walks forward in time.
        observed_at = now - timedelta(days=days - 1 - i)
        if symbol == "IR_GOLD_FUND_FLOW":
            # A ratio in percent that swings through zero, not a price.
            value = float(np.sin(i / 3.0) * 18.0 + rng.normal(0.0, 2.0))
        else:
            value *= 1.0 + rng.normal(drift, sigma)
        rows.append(
            dict(symbol=symbol, value=value, currency=currency, unit=unit,
                 source="seed", observed_at=observed_at, collected_at=observed_at,
                 quality="ok")
        )
    return rows


def seed_prices(engine, symbol: str, days: int, *, seed: int) -> None:
    rows = price_rows(symbol, days, seed=seed)
    with engine.begin() as conn:
        conn.execute(prices.insert(), rows)


def seed_forecasts(engine, symbol: str, moves: dict[str, float]) -> None:
    """One recent prediction per horizon, so the forecast factor can fire."""
    now = utcnow()
    with engine.begin() as conn:
        for horizon, (change_pct, confidence) in moves.items():
            conn.execute(predictions.insert().values(
                symbol=symbol, horizon=horizon, model_name="seed",
                predicted_at=now - timedelta(minutes=5),
                target_time=now + timedelta(days=1),
                point_forecast=1.0, lower_bound=0.9, upper_bound=1.1,
                expected_change_pct=change_pct, direction="up",
                confidence=confidence, regime="ranging", drivers=[],
                data_fresh=True, warnings=[],
            ))


_spread_seq = itertools.count()


def seed_dealer_spread(engine, spread_pct: float = 0.49) -> None:
    """The observed Hamrah Gold buy/sell spread — IR_GOLD_18K's real hurdle.

    ``dedupe_key`` is unique per call: raw_observations enforces uniqueness on
    it, and a test that seeds the world and then adds a spread of its own must
    not collide.
    """
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(raw_observations.insert().values(
            provider_code="hamrahgold", symbol="IR_GOLD_18K", raw_value=1.0,
            unit="gram", currency="IRT", raw_payload={"spread_pct": spread_pct},
            observed_at=now, collected_at=now, quality="ok",
            dedupe_key=f"fixture-spread-{next(_spread_seq)}",
        ))


def seed_trend_alignment(
    engine, symbol: str, alignment: str = "full_bullish", *, age_days: float = 3.0
) -> None:
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(trend_alignment_states.insert().values(
            symbol=symbol, alignment=alignment, previous_alignment="not_aligned",
            timeframes={tf: {"trend": "bullish" if alignment == "full_bullish"
                             else "bearish"} for tf in ("1d", "4h", "1h")},
            ma_type="ema", fast_period=26, mid_period=48, slow_period=220,
            data_fresh=True, calculated_at=now, updated_at=now,
        ))
        conn.execute(trend_alignment_events.insert().values(
            symbol=symbol, alignment=alignment, previous_alignment="not_aligned",
            occurred_at=now - timedelta(days=age_days),
            latest_1h_candle_close=now, latest_4h_candle_close=now,
            latest_1d_candle_close=now,
        ))


def seed_gold_world(engine, symbols: Optional[Sequence[str]] = None) -> None:
    """The full IR_GOLD_18K decision context, plus its parity context.

    This is the fixture the "gold is unchanged" regression pins: every factor
    the gold profile declares is computable from it.
    """
    for i, symbol in enumerate(symbols or ("IR_GOLD_18K", "USD_IRT", "XAUUSD")):
        seed_prices(engine, symbol, GOLD_DAYS, seed=100 + i)
    seed_forecasts(engine, "IR_GOLD_18K",
                   {"1d": (1.4, 0.66), "3d": (1.9, 0.61), "7d": (2.6, 0.58)})
    seed_dealer_spread(engine)
    seed_trend_alignment(engine, "IR_GOLD_18K")
