"""FROZEN COPY of the gold-only signal LOADER, kept as a regression oracle.

This is ``app.signals.engine.generate_signal`` (since deleted — it had no
callers) exactly as it stood before the multi-symbol refactor (Addendum 28),
with three changes and no others:

  * it is a module-level function taking the engine, not a method;
  * it does not write to ``signals`` and does not publish the decision policy,
    because the oracle must not have side effects;
  * it ends by returning ``compute_signal``'s dict instead of the persisted
    payload.

Every line of the LOADER that decides a NUMBER is untouched — the literal
('IR_GOLD_18K','USD_IRT','XAUUSD') price query, the SMA50-over-the-last-50-bars
expression, the ``min_history=len(gold)`` declaration, the market-hours
freshness call and the trend-alignment lookup.

WHAT THIS ORACLE CAN AND CANNOT CATCH
-------------------------------------

It is not an independent implementation, and a green result must not be read
as one.  It IMPORTS five things from the code under test, so any change to
them moves both sides of the comparison identically and the test stays green
while the behaviour changes:

  * ``compute_signal`` — the whole scorer.  Every factor weight, every band
    edge, the technical-only discount, the confidence basis.
  * ``SignalInputs`` — the field set, and what any field means.
  * ``_load_latest_predictions`` and ``_load_trend_alignment`` — two of the
    loaders, deliberately shared from the start.
  * ``app.core.costs.round_trip_cost_pct`` — the cost hurdle, including how a
    dealer spread is resolved and when it falls back to the assumption.

The scorer is therefore pinned separately, by a recorded expectation in
``test_ir_gold_18k_score_is_unchanged_by_the_multi_symbol_refactor``: gold's
published score and signal on this fixture are written down as literals, so a
change inside ``compute_signal`` fails that assertion even though the oracle
comparison cannot see it.  The two mechanisms are complementary and neither is
sufficient alone.

What the oracle DOES catch is everything the multi-symbol refactor actually
rewrote, which is why it was written: the price query and its symbol tuple,
the ``daily_close`` / ``compute_feature_frame`` call and its ``min_history``
declaration, the SMA50 expression, the market-hours freshness call, and — the
failure mode the profile machinery newly makes possible — WHICH of gold's
factors get populated at all.  That last one is not hypothetical: raising
``FACTOR_MA_ALIGNMENT``'s history requirement dropped gold's alignment factor
on the old 260-bar fixture, and this oracle is what reported it.

DO NOT "fix", modernise or reformat this file.  If a future change to the gold
pipeline is deliberate, the test that uses this oracle should be deleted along
with the file, in the same commit that states the intent — not quietly edited
until it agrees again.
"""
from __future__ import annotations

from app.signals.engine import (
    SignalInputs,
    _load_latest_predictions,
    _load_trend_alignment,
    compute_signal,
)


def legacy_gold_signal(engine, settings) -> dict:
    """The pre-Addendum-28 gold scoring path, verbatim, without side effects."""
    import pandas as pd
    from sqlalchemy import select

    from app.core.costs import round_trip_cost_pct
    from app.core.freshness import is_acceptably_fresh, is_market_open
    from app.db import prices as prices_t
    from app.db import utcnow
    from app.features.engineering import (
        SPACING_DAILY,
        compute_feature_frame,
        daily_close,
    )
    from app.models.training import detect_regime

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
    cost_pct, cost_basis = round_trip_cost_pct(engine)
    inputs.round_trip_cost_pct = cost_pct
    inputs.round_trip_cost_basis = cost_basis
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
                bar_spacing=SPACING_DAILY,
                min_history=len(gold),
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
            inputs.data_fresh = is_acceptably_fresh(
                "IR_GOLD_18K", last_obs.to_pydatetime(), now, settings
            )
        else:
            inputs.data_fresh = False
    else:
        inputs.data_fresh = False
        inputs.market_closed = not is_market_open("IR_GOLD_18K", now, settings)

    trend = _load_trend_alignment(engine, settings, "IR_GOLD_18K", now)
    if trend:
        inputs.trend_alignment = trend["alignment"]
        inputs.trend_states = trend["states"]
        inputs.trend_alignment_age_days = trend["age_days"]
        inputs.trend_weight = trend["weight"]

    return compute_signal(inputs, now=now)
