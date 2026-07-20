"""Buy/hold/sell signal engine.

Composes a 0–100 bullishness score from weighted factors:

* forecast expected return vs the round-trip transaction cost threshold,
* model confidence and cross-horizon agreement,
* trend (price vs SMA20/SMA50), RSI zones, momentum,
* premium z-score (a rich premium argues caution when buying),
* volatility regime,
* and a hard data-freshness gate — stale inputs force ``hold``.

Score mapping (docs/CONTRACTS.md): >=75 strong_buy, >=60 buy, 40–60 hold,
<=40 sell, <=25 strong_sell.

Wording is deliberately hedged ("conditions currently favor ..."); the engine
never promises outcomes and always attaches risks + an invalidation
condition.  ``review_at`` is six hours out.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

# Fee assumptions for the cost gate (percent, per docs: fee both sides +
# dealer spread + slippage on a round trip).
DEFAULT_FEE_PCT = 0.5
DEFAULT_SPREAD_PCT = 1.0
DEFAULT_SLIPPAGE_PCT = 0.1
DEFAULT_ROUND_TRIP_COST_PCT = 2 * DEFAULT_FEE_PCT + DEFAULT_SPREAD_PCT + 2 * DEFAULT_SLIPPAGE_PCT

REVIEW_AFTER = timedelta(hours=6)

# horizon -> weight in the forecast factor (nearer horizons matter more)
FORECAST_WEIGHTS = {"1h": 0.5, "4h": 0.75, "eod": 1.0, "1d": 1.0, "3d": 0.8, "7d": 0.6, "30d": 0.4}


@dataclass
class SignalInputs:
    """Everything the scorer needs; assembled by the signals job."""

    expected_change_pct: dict[str, float] = field(default_factory=dict)  # per horizon
    confidence: dict[str, float] = field(default_factory=dict)           # per horizon
    last_price: Optional[float] = None
    sma20: Optional[float] = None
    sma50: Optional[float] = None
    rsi14: Optional[float] = None
    momentum_10_pct: Optional[float] = None   # percent over 10 steps
    premium_z: Optional[float] = None
    regime: str = "unknown"
    data_fresh: bool = True
    # Addendum 1: True while the Tehran market is closed.  With market_closed
    # and data_fresh both set (= last-session data), scoring proceeds normally
    # and the signal just carries an informational note; only truly stale data
    # (older than the last session) forces hold.
    market_closed: bool = False
    round_trip_cost_pct: float = DEFAULT_ROUND_TRIP_COST_PCT


def _score_to_signal(score: int) -> str:
    if score >= 75:
        return "strong_buy"
    if score >= 60:
        return "buy"
    if score <= 25:
        return "strong_sell"
    if score <= 40:
        return "sell"
    return "hold"


def compute_signal(inputs: SignalInputs, now: Optional[datetime] = None) -> dict:
    """Pure scoring function; returns a dict shaped like a ``signals`` row."""
    now = now or datetime.now(timezone.utc)
    supporting: list[str] = []
    conflicting: list[str] = []
    risks: list[str] = [
        "Forecasts are statistical estimates with real uncertainty; markets can move against any signal.",
        "Iranian gold prices are exposed to currency policy shocks and liquidity gaps.",
    ]

    score = 50.0

    # --- forecast expected return vs cost threshold ------------------------
    horizon_items = [
        (h, chg) for h, chg in inputs.expected_change_pct.items() if chg is not None
    ]
    weighted_exp = None
    if horizon_items:
        weights = np.array([FORECAST_WEIGHTS.get(h, 0.5) for h, _ in horizon_items])
        changes = np.array([chg for _, chg in horizon_items])
        weighted_exp = float(np.average(changes, weights=weights))
        cost = inputs.round_trip_cost_pct
        if weighted_exp > cost:
            # clears the cost hurdle: bullish in proportion to the excess
            contribution = float(np.clip((weighted_exp - cost / 2.0) * 8.0, 0.0, 25.0))
        elif weighted_exp > 0:
            # positive but under the cost hurdle: mild hold bias, not a sell
            contribution = -3.0
        else:
            contribution = float(np.clip(weighted_exp * 8.0, -25.0, 0.0))
        score += contribution
        if weighted_exp > inputs.round_trip_cost_pct:
            supporting.append(
                f"Model forecasts a weighted {weighted_exp:+.2f}% move, above the "
                f"~{inputs.round_trip_cost_pct:.1f}% round-trip cost threshold."
            )
        elif weighted_exp > 0:
            conflicting.append(
                f"Forecast move ({weighted_exp:+.2f}%) is positive but below the "
                f"~{inputs.round_trip_cost_pct:.1f}% round-trip cost threshold."
            )
        else:
            conflicting.append(f"Models forecast a {weighted_exp:+.2f}% move.")

    # --- model confidence ---------------------------------------------------
    confidences = [c for c in inputs.confidence.values() if c is not None]
    mean_conf = float(np.mean(confidences)) if confidences else 0.5
    score += (mean_conf - 0.5) * 20.0
    if mean_conf >= 0.6:
        supporting.append(f"Average model confidence is {mean_conf:.0%}.")
    elif mean_conf < 0.45:
        conflicting.append(f"Model confidence is low ({mean_conf:.0%}).")

    # --- agreement across horizons -----------------------------------------
    if len(horizon_items) >= 2:
        signs = [np.sign(chg) for _, chg in horizon_items if chg != 0]
        if signs:
            agreement = abs(sum(signs)) / len(signs)
            score += (agreement - 0.5) * 12.0
            if agreement >= 0.75:
                supporting.append("Forecast horizons largely agree on direction.")
            elif agreement < 0.5:
                conflicting.append("Forecast horizons disagree on direction.")

    # --- trend: price vs SMA20/50 ------------------------------------------
    if inputs.last_price and inputs.sma20 and inputs.sma50:
        if inputs.last_price > inputs.sma20 > inputs.sma50:
            score += 12.0
            supporting.append("Price is above SMA20 and SMA50 (established uptrend).")
        elif inputs.last_price > inputs.sma20:
            score += 6.0
            supporting.append("Price is above SMA20.")
        elif inputs.last_price < inputs.sma20 < inputs.sma50:
            score -= 12.0
            conflicting.append("Price is below SMA20 and SMA50 (established downtrend).")
        elif inputs.last_price < inputs.sma20:
            score -= 6.0
            conflicting.append("Price is below SMA20.")
        # price sitting exactly on its averages is trend-neutral

    # --- RSI zones ----------------------------------------------------------
    if inputs.rsi14 is not None:
        if inputs.rsi14 <= 30:
            score += 8.0
            supporting.append(f"RSI14 at {inputs.rsi14:.0f} is oversold.")
        elif inputs.rsi14 >= 70:
            score -= 8.0
            conflicting.append(f"RSI14 at {inputs.rsi14:.0f} is overbought.")

    # --- momentum -----------------------------------------------------------
    if inputs.momentum_10_pct is not None:
        contribution = float(np.clip(inputs.momentum_10_pct * 2.0, -8.0, 8.0))
        score += contribution
        if inputs.momentum_10_pct > 1.0:
            supporting.append(f"Positive 10-period momentum ({inputs.momentum_10_pct:+.1f}%).")
        elif inputs.momentum_10_pct < -1.0:
            conflicting.append(f"Negative 10-period momentum ({inputs.momentum_10_pct:+.1f}%).")

    # --- premium z-score (rich premium => caution buying) -------------------
    if inputs.premium_z is not None:
        if inputs.premium_z > 0:
            score -= float(min(10.0, inputs.premium_z * 4.0))
            if inputs.premium_z > 1.0:
                conflicting.append(
                    f"Local premium over the theoretical price is rich "
                    f"(z={inputs.premium_z:+.1f}); buying now pays extra premium."
                )
                risks.append("A rich local premium can mean-revert independently of global gold.")
        else:
            score += float(min(6.0, -inputs.premium_z * 3.0))
            if inputs.premium_z < -1.0:
                supporting.append(
                    f"Local premium is cheap vs its 30-day norm (z={inputs.premium_z:+.1f})."
                )

    # --- volatility regime --------------------------------------------------
    if inputs.regime == "high_volatility":
        score -= 5.0
        risks.append("Volatility is in its top decile; price swings can overwhelm the signal.")
        conflicting.append("Market is in a high-volatility regime.")

    # --- freshness gate (hard; market-hours aware upstream) -----------------
    # data_fresh is computed with is_acceptably_fresh, so last-session data
    # during a closure arrives here as fresh and does NOT force hold.
    forced_hold = False
    if not inputs.data_fresh:
        forced_hold = True
        risks.insert(0, "Input data is STALE; the signal was forced to hold until fresh data arrives.")
    elif inputs.market_closed:
        risks.append("prices from last session (market closed)")

    final_score = int(np.clip(round(score), 0, 100))
    if forced_hold:
        final_score = 50
        signal = "hold"
    else:
        signal = _score_to_signal(final_score)

    confidence = float(
        np.clip(mean_conf * (1.0 if inputs.data_fresh else 0.3), 0.05, 0.95)
    )

    direction_word = {
        "strong_buy": "accumulating", "buy": "buying", "hold": "waiting",
        "sell": "reducing exposure", "strong_sell": "exiting positions",
    }[signal]
    explanation = (
        f"Conditions currently favor {direction_word} "
        f"(score {final_score}/100). "
        + (f"Weighted forecast move: {weighted_exp:+.2f}%. " if weighted_exp is not None else "")
        + f"{len(supporting)} supporting vs {len(conflicting)} conflicting factors. "
        "This is an uncertain, model-based assessment of current conditions — "
        "not financial advice, and actual outcomes can differ."
    )
    if forced_hold:
        explanation = (
            "Input data is stale, so the engine holds regardless of model output. "
            "Conditions will be reassessed when fresh data arrives. "
            "This is an uncertain, model-based assessment — not financial advice."
        )

    invalidation = "Reassess if input data goes stale."
    if inputs.sma20 and inputs.last_price:
        if final_score >= 60:
            invalidation = (
                f"Signal is invalidated if price closes below SMA20 "
                f"(~{inputs.sma20:,.0f} IRT) or data goes stale."
            )
        elif final_score <= 40:
            invalidation = (
                f"Signal is invalidated if price closes above SMA20 "
                f"(~{inputs.sma20:,.0f} IRT) or data goes stale."
            )

    return {
        "generated_at": now,
        "signal": signal,
        "score": final_score,
        "confidence": round(confidence, 3),
        "explanation": explanation,
        "supporting": supporting,
        "conflicting": conflicting,
        "risks": risks,
        "invalidation": invalidation,
        "review_at": now + REVIEW_AFTER,
        "data_fresh": bool(inputs.data_fresh),
        "inputs": {
            "expected_change_pct": inputs.expected_change_pct,
            "confidence": inputs.confidence,
            "last_price": inputs.last_price,
            "sma20": inputs.sma20,
            "sma50": inputs.sma50,
            "rsi14": inputs.rsi14,
            "momentum_10_pct": inputs.momentum_10_pct,
            "premium_z": inputs.premium_z,
            "regime": inputs.regime,
            "market_closed": inputs.market_closed,
            "round_trip_cost_pct": inputs.round_trip_cost_pct,
        },
    }


# ---------------------------------------------------------------------------
# DB assembly job (called by POST /internal/signals/generate)
# ---------------------------------------------------------------------------


def _load_latest_predictions(engine) -> dict:
    """Latest prediction per horizon from the last 24h."""
    from sqlalchemy import select

    from ..db import ensure_utc, predictions, utcnow

    cutoff = utcnow() - timedelta(hours=24)
    stmt = (
        select(
            predictions.c.horizon,
            predictions.c.expected_change_pct,
            predictions.c.confidence,
            predictions.c.predicted_at,
        )
        .where(predictions.c.predicted_at >= cutoff)
        .order_by(predictions.c.predicted_at)
    )
    latest: dict[str, dict] = {}
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            latest[str(row[0])] = {
                "expected_change_pct": float(row[1]),
                "confidence": float(row[2]),
                "predicted_at": ensure_utc(row[3]),
            }
    return latest


def generate_signal(engine, settings) -> dict:
    """Assemble inputs from the DB, score, persist one signals row, return it."""
    import pandas as pd
    from sqlalchemy import select

    from ..core.freshness import is_acceptably_fresh, is_market_open
    from ..db import prices as prices_t
    from ..db import signals as signals_t
    from ..db import utcnow
    from ..features.engineering import compute_feature_frame, daily_close
    from ..metrics import JOB_LAST_SUCCESS
    from ..models.training import detect_regime

    now = utcnow()
    stmt = (
        select(prices_t.c.symbol, prices_t.c.observed_at, prices_t.c.value)
        .where(
            prices_t.c.symbol.in_(("IR_GOLD_18K", "USD_IRT", "XAUUSD")),
            prices_t.c.quality == "ok",
        )
        .order_by(prices_t.c.observed_at)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    df = pd.DataFrame(rows, columns=["symbol", "observed_at", "value"])

    inputs = SignalInputs()
    latest_preds = _load_latest_predictions(engine)
    inputs.expected_change_pct = {
        h: p["expected_change_pct"] for h, p in latest_preds.items()
    }
    inputs.confidence = {h: p["confidence"] for h, p in latest_preds.items()}

    if not df.empty:
        gold = daily_close(df, "IR_GOLD_18K")
        if not gold.empty:
            usd = daily_close(df, "USD_IRT")
            xau = daily_close(df, "XAUUSD")
            frame = compute_feature_frame(
                gold,
                usd if not usd.empty else None,
                xau if not xau.empty else None,
            )
            last = frame.iloc[-1]

            def _f(col):
                val = last.get(col)
                return None if val is None or pd.isna(val) else float(val)

            inputs.last_price = float(gold.iloc[-1])
            inputs.sma20 = _f("roll_mean_20")
            inputs.sma50 = (
                float(gold.iloc[-50:].mean()) if len(gold) >= 50 else None
            )
            inputs.rsi14 = _f("rsi_14")
            mom = _f("momentum_10")
            inputs.momentum_10_pct = mom * 100.0 if mom is not None else None
            inputs.premium_z = _f("premium_z_30")
            inputs.regime = detect_regime(gold)

        gold_rows = df[df["symbol"] == "IR_GOLD_18K"]
        inputs.market_closed = not is_market_open("IR_GOLD_18K", now, settings)
        if not gold_rows.empty:
            last_obs = pd.to_datetime(gold_rows["observed_at"].max())
            if last_obs.tzinfo is None:
                last_obs = last_obs.tz_localize("UTC")
            # market-hours aware (Addendum 1): last-session data during a
            # closure stays fresh and only adds an informational note
            inputs.data_fresh = is_acceptably_fresh(
                "IR_GOLD_18K", last_obs.to_pydatetime(), now, settings
            )
        else:
            inputs.data_fresh = False
    else:
        inputs.data_fresh = False
        inputs.market_closed = not is_market_open("IR_GOLD_18K", now, settings)

    result = compute_signal(inputs, now=now)

    with engine.begin() as conn:
        row_id = conn.execute(
            signals_t.insert().values(**result)
        ).inserted_primary_key[0]
    JOB_LAST_SUCCESS.labels(job="signals").set(time.time())

    payload = dict(result)
    payload["id"] = int(row_id)
    payload["generated_at"] = result["generated_at"].isoformat()
    payload["review_at"] = result["review_at"].isoformat()
    return payload
