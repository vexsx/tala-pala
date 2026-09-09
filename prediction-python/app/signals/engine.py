"""Buy/hold/sell signal engine.

Composes a 0–100 bullishness score from weighted factors:

* forecast expected return vs the round-trip transaction cost threshold,
* model confidence and cross-horizon agreement,
* trend (price vs SMA20/SMA50), RSI zones, momentum,
* multi-timeframe MA alignment (Addendum 21),
* premium z-score (a rich premium argues caution when buying),
* volatility regime,
* and a hard data-freshness gate — stale inputs force ``hold``.

Score mapping (docs/CONTRACTS.md): >=75 strong_buy, >=60 buy, 40–60 hold,
<=40 sell, <=25 strong_sell.

A reading with no model behind it keeps only ``TECHNICAL_ONLY_WEIGHT`` of its
distance from hold before that mapping is applied, because those bands were
drawn for the full factor set.  The discount is applied after every factor has
been counted and is reported per row as ``inputs.technical_only_points``.

Wording is deliberately hedged ("conditions currently favor ..."); the engine
never promises outcomes and always attaches risks + an invalidation
condition.  ``review_at`` is six hours out.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Fee assumptions for the cost gate (percent, per docs: fee both sides +
# dealer spread + slippage on a round trip).
DEFAULT_FEE_PCT = 0.5
DEFAULT_SPREAD_PCT = 1.0
DEFAULT_SLIPPAGE_PCT = 0.1
DEFAULT_ROUND_TRIP_COST_PCT = 2 * DEFAULT_FEE_PCT + DEFAULT_SPREAD_PCT + 2 * DEFAULT_SLIPPAGE_PCT

REVIEW_AFTER = timedelta(hours=6)

# --- three numbers that are NOT measurements --------------------------------
#
# Named here rather than written inline mid-function, and each one published
# with a basis beside it, for the reason app/core/costs.py exists: a bare
# number in a payload is indistinguishable from a measured one, and the
# reader has no way to tell which they are looking at.

# What ``confidence`` carries when NO model ran for the asset.  It is a
# structural assumption — "this reading has nothing forward-looking behind
# it", written on the 0–1 scale that field uses — and every payload built
# from it is stamped ``confidence_basis: 'assumed'`` so it can never be read
# beside gold's model-derived 0.62 as though the two were the same kind of
# number.  It is deliberately low and deliberately constant; it is not an
# estimate of anything.
TECHNICAL_ONLY_CONFIDENCE = 0.3

# Stale inputs do not make a model wrong, they make it out of date, so the
# published confidence is discounted rather than the score rewritten.
STALE_CONFIDENCE_MULTIPLIER = 0.3

# How much of its distance from hold a technical-only reading keeps.
#
# HISTORY, because this constant was a lie for as long as it existed: the
# multiply used to sit inside the ``else`` of ``if confidences:``, at a point
# in the function where ``score`` could only ever be exactly 50.0 — the
# forecast block above it is gated on ``if horizon_items:``, and the loader
# populates ``confidence`` and ``expected_change_pct`` together, so no
# confidences meant no forecasts meant no contribution yet.  ``50 + (50-50) *
# 0.5`` is 50.  Three places claimed the score was pulled toward hold and none
# of them was true: bullish technicals with no model scored 74–78 and were
# published as buy/strong_buy on the same scale and in the same words as
# gold's full board.
#
# It is applied below, after every factor has been counted, which is the only
# place it can mean anything.  The number itself is a STATED POLICY, not a
# fitted coefficient — no return series was regressed to choose it.  What
# justifies its direction: a technical-only board is missing every factor that
# looks forward at all (the forecast edge, worth up to ±25 points, the
# confidence term ±10, and the cross-horizon agreement term ±6), while the
# bands it is mapped through — 60 = buy, 75 = strong_buy — were drawn for the
# full board.  Halving the remaining distance is round and conservative rather
# than precise, and ``technical_only_points`` in the payload says exactly what
# it did to this row.
TECHNICAL_ONLY_WEIGHT = 0.5

# horizon -> weight in the forecast factor (nearer horizons matter more)
FORECAST_WEIGHTS = {"1h": 0.5, "4h": 0.75, "eod": 1.0, "1d": 1.0, "3d": 0.8, "7d": 0.6, "30d": 0.4}


@dataclass
class SignalInputs:
    """Everything the scorer needs; assembled by the signals job.

    Multi-asset note (Addendum 28): a field left at ``None`` here means "this
    factor was not computed", and :func:`compute_signal` skips it entirely
    rather than substituting a neutral value.  That was already true of every
    optional field below and is now the contract the per-symbol loader relies
    on — see :mod:`app.signals.universe` for why a neutral default would be a
    fabrication rather than a conservative choice.
    """

    # The asset this reading is about.  No default, deliberately: it becomes
    # the ``signals.symbol`` column, which migration 0026 made NOT NULL with no
    # default so that forgetting it fails the INSERT instead of publishing a
    # silver call labelled as gold.  ``None`` reaches the database as NULL and
    # is refused there.
    symbol: Optional[str] = None
    expected_change_pct: dict[str, float] = field(default_factory=dict)  # per horizon
    round_trip_cost_basis: str = "assumed"  # 'observed_spread' when live
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
    # Addendum 21 — multi-timeframe MA alignment as a scored factor.
    # ``trend_alignment`` is 'full_bullish' | 'full_bearish' | 'not_aligned' |
    # None (never evaluated, disabled, or too old to use).  ``trend_states``
    # maps '1d'/'4h'/'1h' -> that timeframe's own trend, which is what lets the
    # scorer distinguish "no trend anywhere" from "the timeframes contradict
    # each other" — two very different pieces of information that both arrive
    # as 'not_aligned'.  ``trend_alignment_age_days`` is how long the current
    # alignment has held.
    trend_alignment: Optional[str] = None
    trend_states: dict[str, str] = field(default_factory=dict)
    trend_alignment_age_days: Optional[float] = None
    trend_weight: float = 0.0
    # Retail net buying of the Tehran gold ETFs, as a z-score against its own
    # 30-day norm.  Only the two fund symbols can carry it; see the scoring
    # block for why it is caution-only.
    fund_flow_z: Optional[float] = None
    # The one market-specific risk sentence.  It defaults to the Iranian 18k
    # gold line the engine has carried since it was single-asset, because
    # compute_signal() is called directly by tests and ad-hoc scorers and
    # changing what those callers get would be a silent change to the gold
    # payload.  Every per-symbol loader sets it explicitly from
    # universe.MARKET_RISK, so no PUBLISHED row can inherit another asset's
    # risk sentence.
    market_risk: str = (
        "Iranian gold prices are exposed to currency policy shocks and liquidity gaps."
    )
    # Provenance carried through to the payload; none of it is scored.
    # ``omitted_factors`` is the list of {factor, reason} entries for
    # everything this asset could not contribute, which is the whole point of
    # the multi-asset design: an absent factor must be visible and explained,
    # not merely absent.
    omitted_factors: list[dict] = field(default_factory=list)
    stale_reason: Optional[str] = None
    # Full provenance of the hurdle above, flat and under the same
    # ``round_trip_cost_`` prefix as the two fields that predate it, so the UI
    # can show "assumed, because ..." rather than a number that looks measured.
    round_trip_cost_source: Optional[str] = None      # provider code when observed
    round_trip_cost_reason: str = ""
    round_trip_cost_observed_at: Optional[str] = None  # ISO-8601 UTC
    round_trip_cost_age_hours: Optional[float] = None


# Score band edges of _score_to_signal, as (class, lowest, highest). Used to
# stop a single factor promoting the score into a stronger call on its own.
_SIGNAL_BANDS = (
    ("strong_sell", 0, 25),
    ("sell", 26, 40),
    ("hold", 41, 59),
    ("buy", 60, 74),
    ("strong_buy", 75, 100),
)


def _hold_within_band(before: float, after: float) -> float:
    """Cap ``after`` at the edge of the band ``before`` already sat in.

    Only ever used for a move that makes the call MORE committal, so the cap
    is on the far side in the direction of travel.
    """
    band = _score_to_signal(int(np.clip(round(before), 0, 100)))
    for name, low, high in _SIGNAL_BANDS:
        if name == band:
            return float(min(after, high)) if after > before else float(max(after, low))
    return after


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
        # Market-specific, and therefore not hardcoded any more: served beside
        # a silver or dollar reading the gold sentence would simply be false.
        inputs.market_risk,
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
        # Contribution is the cost-ADJUSTED edge, scored monotonically and
        # continuously in `weighted_exp`. The previous piecewise form was
        # non-monotonic (a +0.30% forecast scored WORSE than -0.30%) and had a
        # ~12-point cliff at the cost threshold, so a 0.02pp drift could flip
        # hold -> near-buy. Deadband: moves inside +/- cost earn nothing,
        # because they cannot pay for the round trip in either direction.
        if weighted_exp > cost:
            edge = weighted_exp - cost
        elif weighted_exp < -cost:
            edge = weighted_exp + cost
        else:
            edge = 0.0
        contribution = float(np.clip(edge * 8.0, -25.0, 25.0))
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
    # ``model_backed`` is computed once and every downstream branch reads it:
    # the confidence term, the technical-only discount below, the published
    # ``confidence_basis`` and ``evidence_basis``. They cannot disagree about
    # whether a model stood behind this row, because they are the same boolean.
    confidences = [c for c in inputs.confidence.values() if c is not None]
    model_backed = bool(confidences)
    measured_conf: Optional[float] = None
    if model_backed:
        measured_conf = float(np.mean(confidences))
        mean_conf = measured_conf
        score += (mean_conf - 0.5) * 20.0
        if mean_conf >= 0.6:
            supporting.append(f"Average model confidence is {mean_conf:.0%}.")
        elif mean_conf < 0.45:
            conflicting.append(f"Model confidence is low ({mean_conf:.0%}).")
    else:
        # No forecasts at all (predict job down / no active models). Before
        # this branch existed the engine silently defaulted to 0.5 and the UI
        # rendered a technical-only score as if models backed it. It says so
        # instead — and the score is now genuinely discounted for it, further
        # down, where every factor has been counted and the discount can
        # actually bite.
        mean_conf = TECHNICAL_ONLY_CONFIDENCE
        risks.append(
            "No model forecasts were available: this reading is technical-only "
            "(trend, momentum, premium) and carries no model evidence, so its "
            f"distance from hold is cut to {TECHNICAL_ONLY_WEIGHT:.0%} and the "
            "confidence shown beside it is an assumption, not a measurement."
        )
        conflicting.append("No model forecast available for any horizon.")

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
    sma_factor = 0.0
    if inputs.last_price and inputs.sma20 and inputs.sma50:
        if inputs.last_price > inputs.sma20 > inputs.sma50:
            sma_factor = 12.0
            supporting.append("Price is above SMA20 and SMA50 (established uptrend).")
        elif inputs.last_price > inputs.sma20:
            sma_factor = 6.0
            supporting.append("Price is above SMA20.")
        elif inputs.last_price < inputs.sma20 < inputs.sma50:
            sma_factor = -12.0
            conflicting.append("Price is below SMA20 and SMA50 (established downtrend).")
        elif inputs.last_price < inputs.sma20:
            sma_factor = -6.0
            conflicting.append("Price is below SMA20.")
        # price sitting exactly on its averages is trend-neutral
        score += sma_factor

    # --- multi-timeframe MA alignment (Addendum 21) -------------------------
    # The strongest purely technical read the app has: 1D, 4H and 1H must ALL
    # show a strict price > ma26 > ma48 > ma220 stack (or the mirror) on their
    # own CLOSED candles.  Two design decisions are load-bearing:
    #
    # It is deliberately correlation-aware.  This factor and the SMA20/50 one
    # above answer overlapping questions — "is price above its own averages" —
    # so paying both in full when they agree would count one piece of evidence
    # twice and let a single trending tape push the score around by 22 points.
    # Agreement therefore pays half; the full weight is reserved for the case
    # where the SMA factor is absent, flat, or pointing the other way, which is
    # exactly when the multi-timeframe read carries information the rest of the
    # scorer does not already have.
    #
    # It never scores an unavailable read.  A timeframe short of warm-up, or a
    # state older than the configured age gate, contributes NOTHING rather than
    # a neutral 0 — "we could not tell" is not evidence of balance, and letting
    # it read as balance would quietly dilute every other factor.
    trend_contribution = 0.0
    if inputs.trend_alignment in ("full_bullish", "full_bearish") and inputs.trend_weight > 0:
        direction = 1.0 if inputs.trend_alignment == "full_bullish" else -1.0
        confirms_sma = sma_factor * direction > 0
        trend_contribution = direction * inputs.trend_weight * (0.5 if confirms_sma else 1.0)
        # It can talk the engine OUT of a call, never INTO one.  That asymmetry
        # is how multi-timeframe alignment is actually used: agreement across
        # timeframes is permission to act on a case the rest of the evidence
        # already made, while disagreement is a genuine reason to stand down.
        # So a contribution that moves the score toward caution applies in full
        # and may change the call, while one that makes the call MORE committal
        # is capped at the edge of the band the other eight factors had already
        # reached.  Without this an otherwise perfectly neutral board plus a
        # bullish stack scored exactly 60 — the alignment inventing a "buy" out
        # of no other evidence, which is the one thing it must not do.
        proposed = score + trend_contribution
        if abs(proposed - 50.0) > abs(score - 50.0):
            proposed = _hold_within_band(score, proposed)
        trend_contribution = proposed - score
        score = proposed
        held = (
            f" (held {inputs.trend_alignment_age_days:.1f} days)"
            if inputs.trend_alignment_age_days is not None
            else ""
        )
        word = "bullish" if direction > 0 else "bearish"
        line = (
            f"1D, 4H and 1H moving averages are all stacked {word} on closed "
            f"candles{held}."
        )
        if confirms_sma:
            line += " Counted at half weight: it confirms the SMA trend factor rather than adding to it."
        (supporting if direction > 0 else conflicting).append(line)
    elif inputs.trend_alignment == "not_aligned" and inputs.trend_states:
        # 'not_aligned' covers two different worlds.  Timeframes that actively
        # contradict each other are a genuine caution — a position taken on one
        # timeframe is fighting another — and that is worth telling the reader
        # even though it moves no score.  Merely quiet timeframes are not.
        directions = {s for s in inputs.trend_states.values() if s in ("bullish", "bearish")}
        if len(directions) > 1:
            conflicting.append(
                "Timeframes contradict each other: "
                + ", ".join(f"{tf.upper()} {inputs.trend_states[tf]}" for tf in ("1d", "4h", "1h")
                            if tf in inputs.trend_states)
                + "."
            )
            risks.append(
                "Short- and long-timeframe trends disagree; moves against the "
                "slower timeframe tend to reverse."
            )

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

    # --- gold-fund retail flow (the two Tehran ETFs only) -------------------
    # IR_GOLD_FUND_FLOW is retail net buying as a percentage of volume:
    # positive means individuals are net buyers from institutions.  This system
    # has NOT measured which way that predicts, and inventing a direction for
    # it would be exactly the fabrication the rest of this module refuses.  So
    # it is scored in one direction only — as crowding risk.
    #
    # What can be said without measuring a return: when retail net buying is
    # far above its own recent norm, the marginal buyer of a thin
    # exchange-traded unit is the participant least able to hold it through a
    # drawdown.  That is a statement about who is on the other side, not a
    # forecast, so it may argue for caution and may never argue for buying.
    # Same asymmetry as the multi-timeframe alignment above, for the same
    # reason: a factor that cannot justify a call must not be able to create
    # one.  Nothing below +1 sd fires at all, and the whole factor is capped
    # at 4 points — a quarter of the SMA trend factor.
    fund_flow_points = 0.0
    if inputs.fund_flow_z is not None and inputs.fund_flow_z > 1.0:
        fund_flow_points = -float(min(4.0, (inputs.fund_flow_z - 1.0) * 4.0))
        score += fund_flow_points
        conflicting.append(
            f"Retail investors are net buyers of the gold funds well above "
            f"their 30-day norm (z={inputs.fund_flow_z:+.1f}), so the marginal "
            f"buyer is the retail side."
        )
        risks.append(
            "Crowded retail flow is scored only as caution here: this system "
            "has not measured which way retail flow predicts, so it never "
            "counts as a reason to buy."
        )

    # --- technical-only discount (applied, at last) -------------------------
    # Every factor above has now been counted, which is the only point at which
    # scaling the distance from hold can mean anything. See
    # TECHNICAL_ONLY_WEIGHT for what this constant is and what it is not.
    technical_only_points = 0.0
    if not model_backed:
        discounted = 50.0 + (score - 50.0) * TECHNICAL_ONLY_WEIGHT
        technical_only_points = discounted - score
        score = discounted

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
        np.clip(
            mean_conf * (1.0 if inputs.data_fresh else STALE_CONFIDENCE_MULTIPLIER),
            0.05, 0.95,
        )
    )
    # The same treatment app/core/costs.py gives the round-trip hurdle, for the
    # same reason: 0.300 rendered under a bare "Confidence" label beside gold's
    # measured 0.62 invites a comparison that is not available. One of those
    # two numbers is the mean of what the models reported and the other is a
    # constant standing in for the absence of a model, and the payload now says
    # which is which. ``model_confidence`` is the measured figure before any
    # staleness discount — None when nothing measured it.
    if model_backed:
        confidence_basis = "model_mean"
        confidence_reason = (
            f"mean of the {len(confidences)} per-horizon confidence(s) the "
            f"models reported for this asset"
            + (
                ""
                if inputs.data_fresh
                else f", discounted to {STALE_CONFIDENCE_MULTIPLIER:.0%} because "
                     f"the inputs are stale"
            )
        )
    else:
        confidence_basis = "assumed"
        confidence_reason = (
            f"no model ran for this asset, so no confidence was measured; "
            f"{TECHNICAL_ONLY_CONFIDENCE} is a fixed structural assumption "
            f"standing in for one and must not be read as a measurement or "
            f"compared with a model-derived figure"
        )

    direction_word = {
        "strong_buy": "accumulating", "buy": "buying", "hold": "waiting",
        "sell": "reducing exposure", "strong_sell": "exiting positions",
    }[signal]
    # The headline is the first sentence, extracted rather than duplicated:
    # the overview endpoint needs one line per asset, and Go must not be the
    # place that decides how a buy/sell call is worded. Keeping `explanation`
    # built FROM the headline is what stops the two from ever disagreeing.
    headline = f"Conditions currently favor {direction_word} (score {final_score}/100)."
    explanation = (
        headline + " "
        + (f"Weighted forecast move: {weighted_exp:+.2f}%. " if weighted_exp is not None else "")
        + f"{len(supporting)} supporting vs {len(conflicting)} conflicting factors. "
        "This is an uncertain, model-based assessment of current conditions — "
        "not financial advice, and actual outcomes can differ."
    )
    if forced_hold:
        headline = "Input data is stale, so the engine holds regardless of model output."
        explanation = (
            headline + " "
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

    # Whether a model stood behind this number, as one machine-readable word.
    # It is derived from the same condition the scorer branched on above, not
    # asserted separately, so the payload cannot claim model backing that the
    # score was not actually built with.
    evidence_basis = "model_backed" if model_backed else "technical_only"

    return {
        # Migration 0026's NOT NULL column. Emitted here rather than stamped on
        # by the caller so that a row is complete by construction: a scorer run
        # without a symbol produces None, and the INSERT refuses it.
        "symbol": inputs.symbol,
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
            # --- provenance the overview projects (Addendum 28) -------------
            # These live in `inputs` and not in new columns because they
            # describe HOW this row was assembled, which is exactly what
            # `inputs` is for; the Go API projects them, it does not compute
            # them.
            "symbol": inputs.symbol,
            "evidence_basis": evidence_basis,
            # Confidence provenance, mirroring round_trip_cost_basis: which
            # kind of number the `confidence` column is carrying, what it was
            # derived from, and the measured figure itself when one exists.
            "confidence_basis": confidence_basis,
            "confidence_reason": confidence_reason,
            "model_confidence": (
                round(measured_conf, 4) if measured_conf is not None else None
            ),
            # What the technical-only discount actually removed from this row,
            # so a factor that moves the buy/sell call stays auditable — the
            # same contract trend_alignment_points has.
            "technical_only_points": round(technical_only_points, 2),
            # One line, and the single strongest factor on each side. The
            # overview renders these directly; slicing them in Go would put
            # the wording of a buy/sell call outside the engine that made it.
            "headline": headline,
            "top_supporting": supporting[0] if supporting else None,
            "top_conflicting": conflicting[0] if conflicting else None,
            # Everything this asset could not contribute, each with its reason.
            # An empty list means every factor its profile declares was
            # computed — it does NOT mean the asset has every factor.
            "omitted_factors": list(inputs.omitted_factors),
            "stale_reason": inputs.stale_reason,
            "round_trip_cost_source": inputs.round_trip_cost_source,
            "round_trip_cost_reason": inputs.round_trip_cost_reason,
            "round_trip_cost_observed_at": inputs.round_trip_cost_observed_at,
            "round_trip_cost_age_hours": inputs.round_trip_cost_age_hours,
            "fund_flow_z": inputs.fund_flow_z,
            "fund_flow_points": round(fund_flow_points, 2),
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
            "round_trip_cost_basis": getattr(inputs, "round_trip_cost_basis", "assumed"),
            # Recorded so a reader can reconstruct exactly what the alignment
            # did to this score, including the half-weight case. A factor that
            # moves the buy/sell call has to be auditable after the fact.
            "trend_alignment": inputs.trend_alignment,
            "trend_states": inputs.trend_states,
            "trend_alignment_age_days": inputs.trend_alignment_age_days,
            "trend_alignment_points": round(trend_contribution, 2),
        },
    }


# ---------------------------------------------------------------------------
# DB assembly job (called by POST /internal/signals/generate)
# ---------------------------------------------------------------------------


def _load_latest_predictions(engine, symbol: str = "IR_GOLD_18K") -> dict:
    """Latest prediction per horizon for ONE symbol from the last 24h.

    The symbol filter is load-bearing: without it, XAUUSD rows (written after
    the gold rows every predict cycle) silently overwrote the gold entries and
    the Tehran buy/hold/sell signal was scored from global-gold forecasts.
    """
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
        .where(predictions.c.predicted_at >= cutoff, predictions.c.symbol == symbol)
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


def _load_trend_alignment(engine, settings, symbol: str, now: datetime) -> dict:
    """Current alignment for ``symbol``, or an empty dict when unusable.

    Three gates, all of which mean "contribute nothing" rather than "contribute
    neutrally":  the feature is switched off, the stored read is not fresh, or
    it was calculated too long ago to describe the present.  Returning an empty
    dict (not a 'not_aligned') keeps that distinction — the scorer must be able
    to tell "the timeframes do not agree" from "we do not know".

    ``age_days`` is measured from the ENTRY into the current alignment, which
    lives in ``trend_alignment_events``, not from the last evaluation — the
    question a reader asks is how long this has held, not when we last looked.
    """
    if not getattr(settings, "trend_alignment_enabled", False):
        return {}
    weight = float(getattr(settings, "trend_alignment_signal_weight", 0.0) or 0.0)
    if weight <= 0:
        return {}

    from sqlalchemy import select

    from ..db import ensure_utc, trend_alignment_events, trend_alignment_states

    with engine.connect() as conn:
        row = conn.execute(
            select(trend_alignment_states).where(
                trend_alignment_states.c.symbol == symbol
            )
        ).mappings().first()
        if row is None:
            return {}

        calculated_at = ensure_utc(row["calculated_at"])
        max_age = timedelta(minutes=int(getattr(settings, "trend_alignment_max_age_minutes", 180)))
        if not row["data_fresh"] or calculated_at is None or now - calculated_at > max_age:
            return {}

        alignment = str(row["alignment"])
        timeframes = row["timeframes"] or {}
        states = {
            tf: str(leg.get("trend"))
            for tf, leg in timeframes.items()
            if isinstance(leg, dict) and leg.get("trend")
        }

        age_days = None
        if alignment in ("full_bullish", "full_bearish"):
            entered = conn.execute(
                select(trend_alignment_events.c.occurred_at)
                .where(
                    trend_alignment_events.c.symbol == symbol,
                    trend_alignment_events.c.alignment == alignment,
                )
                .order_by(trend_alignment_events.c.occurred_at.desc())
                .limit(1)
            ).scalar()
            entered = ensure_utc(entered)
            if entered is not None:
                age_days = max(0.0, (now - entered).total_seconds() / 86400.0)

    return {
        "alignment": alignment,
        "states": states,
        "age_days": age_days,
        "weight": weight,
    }


@dataclass(frozen=True)
class SignalOutcome:
    """One symbol's pass: a published row, or a stated refusal to publish.

    A refusal is a normal, expected result — the two Tehran gold ETFs hold
    about fifty days of history between them — and it is NOT an error.  The
    distinction matters to the job above: an error means the pass failed for
    that symbol and should be reported as a failure, while a withholding means
    the pass succeeded and the honest answer was "not enough to say".
    """

    symbol: str
    bars: int
    payload: Optional[dict] = None
    withheld_reason: Optional[str] = None
    omitted_factors: tuple[dict, ...] = ()

    @property
    def published(self) -> bool:
        return self.payload is not None

    def summary(self) -> dict:
        """Compact report entry; the full row is in the database either way."""
        if self.payload is None:
            return {
                "status": "withheld",
                "reason": self.withheld_reason,
                "bars": self.bars,
                "omitted_factors": list(self.omitted_factors),
            }
        return {
            "status": "published",
            "id": self.payload.get("id"),
            "signal": self.payload["signal"],
            "score": self.payload["score"],
            "confidence": self.payload["confidence"],
            "evidence_basis": self.payload["inputs"]["evidence_basis"],
            "data_fresh": self.payload["data_fresh"],
            "bars": self.bars,
            "omitted_factors": list(self.omitted_factors),
        }


def _load_daily_closes(engine, symbols: tuple[str, ...]):
    """``(raw frame, {symbol: daily close series})`` for ``symbols``.

    One query for the target and whatever context its profile actually needs,
    so a symbol with no parity and no flow factor never pays for reading
    USD_IRT and XAUUSD.  For IR_GOLD_18K the requested set is exactly the
    ('IR_GOLD_18K','USD_IRT','XAUUSD') tuple the gold-only loader used, which
    is what keeps its numbers identical.
    """
    import pandas as pd
    from sqlalchemy import select

    from ..db import prices as prices_t
    from ..features.engineering import daily_close

    stmt = (
        select(prices_t.c.symbol, prices_t.c.observed_at, prices_t.c.value)
        .where(prices_t.c.symbol.in_(symbols), prices_t.c.quality == "ok")
        .order_by(prices_t.c.observed_at)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    df = pd.DataFrame(rows, columns=["symbol", "observed_at", "value"])
    closes = {sym: daily_close(df, sym) for sym in symbols} if not df.empty else {}
    return df, closes


def _freshness(df, symbol: str, now: datetime, settings) -> tuple[bool, bool, Optional[str]]:
    """``(data_fresh, market_closed, stale_reason)`` for one symbol.

    Market-hours aware (Addendum 1): last-session data during a closure is
    still acceptably fresh and only earns an informational note.  When it is
    NOT fresh, the reason travels with it — "stale" on its own is not
    something a reader can check.
    """
    import pandas as pd

    from ..core.freshness import is_acceptably_fresh, is_market_open

    market_closed = not is_market_open(symbol, now, settings)
    rows = df[df["symbol"] == symbol] if not df.empty else df
    if rows.empty:
        return False, market_closed, (
            f"no usable {symbol} observations: nothing with quality='ok' is "
            f"stored for it"
        )
    last_obs = pd.to_datetime(rows["observed_at"].max())
    if last_obs.tzinfo is None:
        last_obs = last_obs.tz_localize("UTC")
    last_dt = last_obs.to_pydatetime()
    fresh = is_acceptably_fresh(symbol, last_dt, now, settings)
    if fresh:
        return True, market_closed, None
    age_hours = (now - last_dt).total_seconds() / 3600.0
    return False, market_closed, (
        f"the newest {symbol} observation is {age_hours:.1f}h old "
        f"({last_dt.isoformat()}); the tolerance is "
        f"{settings.stale_minutes} minutes"
        + (" and the market is closed, so last-session data would have counted "
           "as fresh — this is older than that" if market_closed else "")
    )


def _fund_flow_z(df, closes, now: datetime, settings) -> tuple[Optional[float], Optional[str]]:
    """Retail net buying against its own 30-day norm, or why it is unavailable.

    ``IR_GOLD_FUND_FLOW`` is a percentage that swings through zero, so its
    LEVEL is not comparable across regimes and only its position within its own
    recent distribution is.  A z-score is therefore the only form in which this
    series can be read at all.

    Its own freshness is checked separately from the fund's, through the same
    market-hours machinery: a crowding reading from a month ago says nothing
    about who is buying today, and scoring it would be worse than omitting it.
    """
    from .universe import FACTOR_FUND_FLOW, FUND_FLOW_SYMBOL

    fresh, _closed, stale_reason = _freshness(df, FUND_FLOW_SYMBOL, now, settings)
    if not fresh:
        return None, f"the flow series is not current: {stale_reason}"

    series = closes.get(FUND_FLOW_SYMBOL)
    # The z-score's own window, NOT the history gate. Those were the same
    # integer until FactorSpec split them, and reading min_bars here would
    # have silently turned the "30-day norm" this function and the scorer both
    # promise the reader into a 60-day one.
    window = FACTOR_FUND_FLOW.window
    if series is None or len(series) < FACTOR_FUND_FLOW.min_bars:
        have = 0 if series is None else len(series)
        return None, (
            f"{FUND_FLOW_SYMBOL} needs {FACTOR_FUND_FLOW.min_bars} daily bars "
            f"for a {window}-bar z-score against its own norm — the window "
            f"plus a further {window} so it has moved — and has {have}"
        )
    recent = series.iloc[-window:]
    std = float(recent.std())
    if not std > 0:
        return None, (
            f"{FUND_FLOW_SYMBOL} has not moved over the last {window} bars, so "
            f"a z-score against its own norm is undefined"
        )
    return float((float(series.iloc[-1]) - float(recent.mean())) / std), None


def assemble_inputs(engine, settings, symbol: str, now: datetime):
    """Build one symbol's :class:`SignalInputs` from its capability profile.

    Returns ``(inputs, scored_factor_keys, bars)``.  Every factor the profile
    declares is either computed and recorded in ``scored``, or omitted with a
    reason in ``inputs.omitted_factors``.  Nothing in between: there is no path
    through this function that leaves a factor silently at a neutral value.

    Raises :class:`ValueError` for a symbol outside the eligible universe.
    """
    import pandas as pd

    from ..core.costs import resolve_cost
    from ..features.engineering import SPACING_DAILY, compute_feature_frame
    from ..models.training import detect_regime
    from . import universe

    profile = universe.profile_for(symbol)
    applicable = profile.keys()

    wanted = {symbol}
    if universe.FACTOR_PREMIUM.key in applicable:
        # The parity price needs both legs; nothing else does.
        wanted |= {"USD_IRT", "XAUUSD"}
    if universe.FACTOR_FUND_FLOW.key in applicable:
        wanted |= {universe.FUND_FLOW_SYMBOL}
    df, closes = _load_daily_closes(engine, tuple(sorted(wanted)))

    series = closes.get(symbol)
    bars = 0 if series is None else len(series)

    supported, omitted = universe.history_omissions(profile, bars)
    omitted = [dict(entry) for entry in omitted]
    omitted.extend(
        {"factor": key, "reason": reason} for key, reason in profile.inapplicable
    )
    scored: set[str] = set()

    inputs = SignalInputs(symbol=symbol)
    inputs.market_risk = universe.MARKET_RISK[symbol]

    # --- the cost hurdle, resolved for THIS asset --------------------------
    cost = resolve_cost(engine, symbol)
    inputs.round_trip_cost_pct = cost.cost_pct
    inputs.round_trip_cost_basis = cost.basis
    inputs.round_trip_cost_source = cost.source
    inputs.round_trip_cost_reason = cost.reason
    inputs.round_trip_cost_observed_at = (
        cost.observed_at.isoformat() if cost.observed_at else None
    )
    inputs.round_trip_cost_age_hours = cost.age_hours

    # --- forecast edge vs that hurdle --------------------------------------
    if universe.FACTOR_FORECAST.key in supported:
        latest = _load_latest_predictions(engine, symbol)
        if latest:
            inputs.expected_change_pct = {
                h: p["expected_change_pct"] for h, p in latest.items()
            }
            inputs.confidence = {h: p["confidence"] for h, p in latest.items()}
            scored.add(universe.FACTOR_FORECAST.key)
        else:
            # A trained symbol whose predict job has not run in 24h. Distinct
            # from a symbol that has no model at all, and the reason says so —
            # one is an outage, the other is the design.
            omitted.append({
                "factor": universe.FACTOR_FORECAST.key,
                "reason": (
                    f"{symbol} has trained models, but no prediction was "
                    f"written for any horizon in the last 24 hours."
                ),
            })

    # --- price-derived factors ---------------------------------------------
    if bars:
        frame = compute_feature_frame(
            series,
            closes.get("USD_IRT") if universe.FACTOR_PREMIUM.key in applicable else None,
            closes.get("XAUUSD") if universe.FACTOR_PREMIUM.key in applicable else None,
            bar_spacing=SPACING_DAILY,
            min_history=bars,
        )
        last = frame.iloc[-1]

        def _f(col):
            val = last.get(col)
            return None if val is None or pd.isna(val) else float(val)

        inputs.last_price = float(series.iloc[-1])

        if universe.FACTOR_TREND_SMA.key in supported:
            # Window from the spec that gates it, so the statistic and the
            # history requirement cannot drift apart.
            window = universe.FACTOR_TREND_SMA.window
            sma20 = _f("roll_mean_20")
            sma50 = (
                float(series.iloc[-window:].mean()) if bars >= window else None
            )
            if sma20 is not None and sma50 is not None:
                inputs.sma20, inputs.sma50 = sma20, sma50
                scored.add(universe.FACTOR_TREND_SMA.key)
            else:
                omitted.append({
                    "factor": universe.FACTOR_TREND_SMA.key,
                    "reason": (
                        "SMA20/SMA50 could not be computed from the stored "
                        "history even though the bar count allows it (gaps in "
                        "the series)."
                    ),
                })

        if universe.FACTOR_RSI.key in supported:
            inputs.rsi14 = _f("rsi_14")
            if inputs.rsi14 is not None:
                scored.add(universe.FACTOR_RSI.key)
            else:
                omitted.append({
                    "factor": universe.FACTOR_RSI.key,
                    "reason": "RSI(14) is undefined over the stored history.",
                })

        if universe.FACTOR_MOMENTUM.key in supported:
            mom = _f("momentum_10")
            inputs.momentum_10_pct = mom * 100.0 if mom is not None else None
            if inputs.momentum_10_pct is not None:
                scored.add(universe.FACTOR_MOMENTUM.key)
            else:
                omitted.append({
                    "factor": universe.FACTOR_MOMENTUM.key,
                    "reason": "10-period momentum is undefined over the stored history.",
                })

        if universe.FACTOR_PREMIUM.key in supported:
            inputs.premium_z = _f("premium_z_30")
            if inputs.premium_z is not None:
                scored.add(universe.FACTOR_PREMIUM.key)
            else:
                omitted.append({
                    "factor": universe.FACTOR_PREMIUM.key,
                    "reason": (
                        "The 18k parity price needs overlapping USD_IRT and "
                        "XAUUSD history; the stored series do not provide 30 "
                        "aligned bars."
                    ),
                })

        if universe.FACTOR_VOLATILITY.key in supported:
            inputs.regime = detect_regime(series)
            if inputs.regime != "unknown":
                scored.add(universe.FACTOR_VOLATILITY.key)
            else:
                omitted.append({
                    "factor": universe.FACTOR_VOLATILITY.key,
                    "reason": "The volatility regime is undetermined over the stored history.",
                })

    if universe.FACTOR_FUND_FLOW.key in supported:
        flow_z, flow_reason = _fund_flow_z(df, closes, now, settings)
        if flow_z is None:
            omitted.append({
                "factor": universe.FACTOR_FUND_FLOW.key, "reason": flow_reason,
            })
        else:
            inputs.fund_flow_z = flow_z
            scored.add(universe.FACTOR_FUND_FLOW.key)

    # --- multi-timeframe alignment -----------------------------------------
    # Never let the indicator sink signal generation: it is one factor among
    # several, and a signal scored without it is far better than no signal.
    if universe.FACTOR_MA_ALIGNMENT.key in supported:
        try:
            trend = _load_trend_alignment(engine, settings, symbol, now)
        except Exception as exc:  # noqa: BLE001 — see comment above
            log.warning("trend alignment unavailable for %s: %s", symbol, exc)
            trend = {}
        if trend:
            inputs.trend_alignment = trend["alignment"]
            inputs.trend_states = trend["states"]
            inputs.trend_alignment_age_days = trend["age_days"]
            inputs.trend_weight = trend["weight"]
            scored.add(universe.FACTOR_MA_ALIGNMENT.key)
        else:
            omitted.append({
                "factor": universe.FACTOR_MA_ALIGNMENT.key,
                "reason": (
                    "No usable 1D/4H/1H read: the indicator is disabled, "
                    "weighted at zero, has no stored state for this symbol, or "
                    "its state is older than the configured age gate."
                ),
            })

    # --- freshness ----------------------------------------------------------
    fresh, closed, stale_reason = _freshness(df, symbol, now, settings)
    inputs.data_fresh = fresh
    inputs.market_closed = closed
    inputs.stale_reason = stale_reason

    inputs.omitted_factors = omitted
    return inputs, frozenset(scored), bars


def generate_signal_for(engine, settings, symbol: str) -> SignalOutcome:
    """Score ONE symbol and persist its row, or refuse with a stated reason.

    Refuses (writes nothing) when too few factors survived to say anything —
    see :func:`app.signals.universe.withholding_reason`.  Publishing a `hold`
    in that case would be the worst available outcome: it looks exactly like a
    considered neutral verdict and is in fact the absence of one.

    Raises :class:`ValueError` for a symbol outside the eligible universe.
    """
    from ..db import signals as signals_t
    from ..db import utcnow
    from . import universe

    now = utcnow()
    inputs, scored, bars = assemble_inputs(engine, settings, symbol, now)
    omitted = tuple(inputs.omitted_factors)

    refusal = universe.withholding_reason(scored)
    if refusal is not None:
        log.info("no signal published for %s: %s", symbol, refusal)
        return SignalOutcome(
            symbol=symbol, bars=bars, withheld_reason=refusal, omitted_factors=omitted
        )

    result = compute_signal(inputs, now=now)
    with engine.begin() as conn:
        row_id = conn.execute(signals_t.insert().values(**result)).inserted_primary_key[0]

    payload = dict(result)
    payload["id"] = int(row_id)
    payload["generated_at"] = result["generated_at"].isoformat()
    payload["review_at"] = result["review_at"].isoformat()
    return SignalOutcome(
        symbol=symbol, bars=bars, payload=payload, omitted_factors=omitted
    )


def publish_decision_policy(engine) -> dict:
    """Publish IR_GOLD_18K's decision contract so Go/UI read Python's numbers.

    Once per pass, not once per symbol: it is a single gold-only contract (the
    on-page action planner's), and writing it seven times would only give the
    last symbol's pass the chance to be the one that failed.
    """
    from ..core.costs import DECISION_POLICY_KEY, decision_policy
    from ..jobs.evaluate import upsert_setting

    policy = decision_policy(engine)
    try:
        upsert_setting(engine, DECISION_POLICY_KEY, policy)
    except Exception as exc:  # noqa: BLE001 — never sink signal generation
        log.warning("decision policy persist failed: %s", exc)
    return policy
