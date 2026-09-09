"""Buy/hold/sell across every asset that has real data (Addendum 28).

Each test below pins one property that keeps the generalisation honest.  The
theme they share: a factor an asset cannot carry must be OMITTED with its
reason and must never arrive as a neutral value, because ``compute_signal``
starts at 50 and a silent zero both dilutes the real factors and reads
downstream as evidence that was weighed.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.costs import (
    FALLBACK_ROUND_TRIP_COST_PCT,
    resolve_cost,
)
from app.db import app_settings, create_db_engine, metadata, prices, signals, utcnow
from app.jobs.signals import SIGNAL_UNIVERSE_KEY, run_signals
from app.signals import universe
from app.signals.engine import (
    TECHNICAL_ONLY_CONFIDENCE,
    TECHNICAL_ONLY_WEIGHT,
    SignalInputs,
    compute_signal,
    generate_signal_for,
)

from ._legacy_gold_signal import legacy_gold_signal
from ._market_fixtures import (
    seed_dealer_spread,
    seed_forecasts,
    seed_gold_world,
    seed_prices,
    seed_trend_alignment,
)
from .conftest import TEST_TOKEN

AUTH = {"X-Internal-Token": TEST_TOKEN}

# Keys the refactor ADDED to `inputs`. They are provenance — which asset this
# is, what was omitted and why, what the hurdle was based on — and they carry
# no score. Excluded from the byte-for-byte comparison below for exactly that
# reason: the regression is about the numbers, and every one of those is
# compared.
NEW_PROVENANCE_KEYS = frozenset({
    "symbol", "evidence_basis", "omitted_factors", "stale_reason",
    "round_trip_cost_source", "round_trip_cost_reason",
    "round_trip_cost_observed_at", "round_trip_cost_age_hours",
    "fund_flow_z", "fund_flow_points",
    "headline", "top_supporting", "top_conflicting",
    # Confidence provenance and the technical-only discount's own audit line.
    "confidence_basis", "confidence_reason", "model_confidence",
    "technical_only_points",
})

# Gold's published reading on the seeded fixture, written down.
#
# ``tests/_legacy_gold_signal.py`` shares ``compute_signal`` with the code it
# is checking, so a change INSIDE the scorer moves both sides of that
# comparison together and the oracle cannot see it (that file's docstring
# lists the whole shared surface). These two literals are the counterweight:
# they are the scorer's output, so any change to a factor weight, a band edge
# or the technical-only discount fails HERE.
#
# An int score and a string are safe to record where a float would not be —
# the fixture drives numpy's ``default_rng``, whose stream NumPy's own
# compatibility policy pins, and everything after it is plain float
# arithmetic. If a deliberate change moves these, move them in the same commit
# that states why.
GOLD_RECORDED_SCORE = 76
GOLD_RECORDED_SIGNAL = "strong_buy"


# --- the regression that matters most ---------------------------------------


def test_ir_gold_18k_score_is_unchanged_by_the_multi_symbol_refactor(engine, settings):
    """Gold must come out of this change identical, and it is checked against
    the OLD implementation rather than a hardcoded number.

    ``tests/_legacy_gold_signal.py`` is the pre-refactor LOADER, frozen. Both
    read the same database in the same second, so any drift in the price
    query, the SMA50 expression, the ``min_history`` declaration, the
    market-hours freshness call or which factors gold is fed shows up here as
    an inequality — on whatever numpy/pandas build is installed.

    It is a partial oracle and says so: it imports ``compute_signal``,
    ``SignalInputs``, both prediction/alignment loaders and
    ``round_trip_cost_pct`` from the code under test, so a change in the
    SCORER or in COST RESOLUTION moves both sides at once and this comparison
    stays green. The recorded expectation at the end of this test is what
    covers that half; see the oracle module's docstring for the full list of
    what it cannot see.
    """
    seed_gold_world(engine)

    legacy = legacy_gold_signal(engine, settings)
    outcome = generate_signal_for(engine, settings, "IR_GOLD_18K")
    assert outcome.published, outcome.withheld_reason
    fresh = outcome.payload

    # Everything the reader sees, and everything the score is made of.
    for key in ("signal", "score", "confidence", "explanation", "supporting",
                "conflicting", "risks", "invalidation", "data_fresh"):
        assert fresh[key] == legacy[key], key

    for key, legacy_value in legacy["inputs"].items():
        if key in NEW_PROVENANCE_KEYS:
            continue  # provenance, compared on its own below
        if key == "trend_alignment_age_days":
            # Measured from now() in each call, microseconds apart.
            assert fresh["inputs"][key] == pytest.approx(legacy_value, abs=1e-3)
            continue
        assert fresh["inputs"][key] == legacy_value, key

    # The refactor only ever ADDS to `inputs`; nothing was dropped, and every
    # scored key was compared above.
    assert set(legacy["inputs"]) == set(fresh["inputs"])
    assert NEW_PROVENANCE_KEYS < set(fresh["inputs"])
    # The legacy loader names no symbol — it could not, there was only one.
    assert legacy["inputs"]["symbol"] is None
    assert fresh["inputs"]["symbol"] == "IR_GOLD_18K"

    # And the things that depend only on the seeded constants, pinned in the
    # open so a reviewer can see what "unchanged" is worth here: this is a
    # fully-loaded reading, not a degenerate hold.
    #
    # The score and the call are RECORDED values, not the oracle's — see
    # GOLD_RECORDED_SCORE. They are the half of this test that can see inside
    # compute_signal.
    assert fresh["score"] == GOLD_RECORDED_SCORE
    assert fresh["signal"] == GOLD_RECORDED_SIGNAL
    # Gold is model-backed, so the technical-only discount must not touch it.
    assert fresh["inputs"]["technical_only_points"] == 0.0
    assert fresh["inputs"]["confidence_basis"] == "model_mean"
    assert fresh["inputs"]["model_confidence"] == pytest.approx(0.6167, abs=1e-4)
    assert fresh["inputs"]["evidence_basis"] == "model_backed"
    assert fresh["inputs"]["round_trip_cost_basis"] == "observed_spread"
    assert fresh["inputs"]["round_trip_cost_pct"] == pytest.approx(0.49)
    assert any("weighted +1.87% move" in line for line in fresh["supporting"])
    # The headline is a slice of the explanation the engine already produced,
    # not a second wording of the same call.
    assert fresh["explanation"].startswith(fresh["inputs"]["headline"] + " ")
    assert fresh["inputs"]["top_supporting"] == fresh["supporting"][0]
    assert fresh["inputs"]["top_conflicting"] == fresh["conflicting"][0]
    assert fresh["risks"] == [
        "Forecasts are statistical estimates with real uncertainty; markets "
        "can move against any signal.",
        "Iranian gold prices are exposed to currency policy shocks and "
        "liquidity gaps.",
    ]


def test_the_gold_row_is_written_under_its_own_symbol(engine, settings):
    seed_gold_world(engine)
    generate_signal_for(engine, settings, "IR_GOLD_18K")
    with engine.connect() as conn:
        rows = conn.execute(select(signals.c.symbol)).all()
    assert [r[0] for r in rows] == ["IR_GOLD_18K"]


def test_a_signal_row_without_a_symbol_is_refused_by_the_database(engine):
    """Migration 0026's whole point: forgetting the symbol must ERROR.

    The three older symbol columns in this schema default to 'IR_GOLD_18K', so
    the same bug there files a row under gold and serves it to a reader.
    """
    import sqlalchemy.exc

    result = compute_signal(SignalInputs(expected_change_pct={"1d": 1.0},
                                         confidence={"1d": 0.6}))
    assert result["symbol"] is None
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        with engine.begin() as conn:
            conn.execute(signals.insert().values(**result))


# --- technical-only assets ---------------------------------------------------


def test_technical_only_asset_never_claims_model_backing(engine, settings):
    """USD_IRT has 17,850 price rows and no trained model.

    It must publish, must say it is technical-only, and must carry no forecast
    factor at all — not a forecast of zero.
    """
    seed_prices(engine, "USD_IRT", 260, seed=7)
    outcome = generate_signal_for(engine, settings, "USD_IRT")
    assert outcome.published, outcome.withheld_reason
    payload = outcome.payload

    assert payload["inputs"]["evidence_basis"] == "technical_only"
    assert payload["inputs"]["expected_change_pct"] == {}
    assert payload["inputs"]["confidence"] == {}
    assert not any("forecast" in line.lower() and "%" in line
                   for line in payload["supporting"])
    assert any("technical-only" in risk for risk in payload["risks"])

    omitted = {entry["factor"]: entry["reason"]
               for entry in payload["inputs"]["omitted_factors"]}
    assert universe.FACTOR_FORECAST.key in omitted
    assert "no forecasting model is trained for usd_irt" in \
        omitted[universe.FACTOR_FORECAST.key].lower()


def test_evidence_basis_tracks_the_forecast_it_was_actually_scored_with(engine, settings):
    """XAUUSD is model-backed by capability but not on a day the predict job
    did not run — and the two are distinguishable in the payload."""
    seed_prices(engine, "XAUUSD", 260, seed=11)

    silent = generate_signal_for(engine, settings, "XAUUSD")
    assert silent.payload["inputs"]["evidence_basis"] == "technical_only"
    outage = {e["factor"]: e["reason"] for e in silent.omitted_factors}
    assert "no prediction was written" in outage[universe.FACTOR_FORECAST.key]

    seed_forecasts(engine, "XAUUSD", {"1d": (1.5, 0.7)})
    backed = generate_signal_for(engine, settings, "XAUUSD")
    assert backed.payload["inputs"]["evidence_basis"] == "model_backed"
    assert universe.FACTOR_FORECAST.key not in {
        e["factor"] for e in backed.omitted_factors
    }


# --- omission, not a neutral default ----------------------------------------


def test_inapplicable_factors_are_omitted_with_reasons_and_left_unset(engine, settings):
    """The premium and the fund flow do not apply to the dollar.

    Both must be absent from the inputs AND named in the payload with a reason
    a reader can act on. ``None`` and ``0.0`` are not interchangeable here:
    0.0 is a measurement.
    """
    seed_prices(engine, "USD_IRT", 260, seed=7)
    payload = generate_signal_for(engine, settings, "USD_IRT").payload

    assert payload["inputs"]["premium_z"] is None
    assert payload["inputs"]["fund_flow_z"] is None
    assert payload["inputs"]["fund_flow_points"] == 0.0

    omitted = {e["factor"]: e["reason"]
               for e in payload["inputs"]["omitted_factors"]}
    for factor in (universe.FACTOR_PREMIUM.key, universe.FACTOR_FUND_FLOW.key,
                   universe.FACTOR_MA_ALIGNMENT.key):
        assert factor in omitted, factor
        assert len(omitted[factor]) > 40, f"{factor} reason is not a reason"
    assert "parity price" in omitted[universe.FACTOR_PREMIUM.key]
    assert "IR_GOLD_FUND_FLOW" in omitted[universe.FACTOR_FUND_FLOW.key]


def test_a_neutral_default_would_not_have_been_neutral():
    """Why omission is required rather than a tidy default.

    Handed a 'neutral' 0.5 confidence on a zero forecast, the engine scores a
    bullish technical board at 74 and discloses nothing: the reader cannot
    tell a model-backed call from an unmodelled one.  With the forecast
    OMITTED instead, the same board says it is technical-only, carries an
    assumed confidence rather than a measured one, and — the part that used to
    be claimed and not done — is scored lower for it.
    """
    common = dict(last_price=110.0, sma20=105.0, sma50=100.0, rsi14=28.0,
                  momentum_10_pct=2.0, data_fresh=True, symbol="USD_IRT")
    omitted = compute_signal(SignalInputs(**common))
    neutral_default = compute_signal(
        SignalInputs(**common, expected_change_pct={"1d": 0.0},
                     confidence={"1d": 0.5})
    )

    # The raw board is identical — a zero forecast at 50% confidence moves the
    # score by exactly nothing — so every point of difference below is the
    # technical-only discount and nothing else.
    assert neutral_default["score"] == 74
    assert neutral_default["inputs"]["technical_only_points"] == 0.0
    assert omitted["score"] == 62
    assert omitted["inputs"]["technical_only_points"] == -12.0
    # 50 + (74 - 50) * TECHNICAL_ONLY_WEIGHT, derived rather than restated.
    assert omitted["score"] == round(
        50.0 + (neutral_default["score"] - 50.0) * TECHNICAL_ONLY_WEIGHT)

    # And the disclosure, which is the other half of the answer: a number that
    # is lower without a reason attached would be its own kind of fabrication.
    assert omitted["confidence"] < neutral_default["confidence"]
    assert omitted["inputs"]["evidence_basis"] == "technical_only"
    assert neutral_default["inputs"]["evidence_basis"] == "model_backed"
    assert any("technical-only" in r for r in omitted["risks"])
    assert not any("technical-only" in r for r in neutral_default["risks"])


def test_the_technical_only_discount_is_applied_and_not_merely_claimed():
    """The regression this exists to prevent, in the shape it actually had.

    The discount used to live inside the ``else`` of ``if confidences:``, at a
    point where the score could only be exactly 50.0, so ``50 + (score - 50) *
    0.5`` was a no-op and three separate docstrings said otherwise.  A bullish
    technical board with no model published at 74-78 — buy, and at the top of
    the range strong_buy — on the same scale and in the same words as gold's
    seven-factor reading.

    Two properties, and the first alone would not have caught the bug: the
    score must be strictly closer to hold than the undiscounted board, AND the
    engine must report by how much.
    """
    strong = dict(symbol="XAGUSD", last_price=110.0, sma20=105.0, sma50=100.0,
                  rsi14=28.0, momentum_10_pct=4.0, data_fresh=True)
    technical_only = compute_signal(SignalInputs(**strong))
    backed = compute_signal(
        SignalInputs(**strong, expected_change_pct={"1d": 0.0},
                     confidence={"1d": 0.5}))

    # The board that used to reach strong_buy with nothing forward-looking
    # behind it.
    assert backed["score"] == 78
    assert backed["signal"] == "strong_buy"

    assert abs(technical_only["score"] - 50) < abs(backed["score"] - 50)
    assert technical_only["score"] == 64
    assert technical_only["signal"] == "buy"
    assert technical_only["inputs"]["technical_only_points"] == -14.0
    # The discount is auditable per row, exactly like trend_alignment_points.
    assert technical_only["score"] == round(
        backed["score"] + technical_only["inputs"]["technical_only_points"])
    # And it is stated to the reader, not applied silently.
    assert any(f"{TECHNICAL_ONLY_WEIGHT:.0%}" in r for r in technical_only["risks"])


def test_a_bare_confidence_number_can_never_be_read_as_a_measurement():
    """Defect: 0.300 rendered under the label "Confidence" beside gold's 0.62.

    One of those is the mean of what the models reported and the other is a
    constant standing in for the absence of a model.  ``app/core/costs.py``
    solved the identical problem for the round-trip hurdle by shipping a
    basis; confidence now carries one too, so the two numbers cannot be
    compared by a reader who was given no way to tell them apart.
    """
    board = dict(last_price=110.0, sma20=105.0, sma50=100.0, rsi14=50.0,
                 momentum_10_pct=0.0, data_fresh=True, symbol="USD_IRT")
    unmodelled = compute_signal(SignalInputs(**board))
    modelled = compute_signal(
        SignalInputs(**board, expected_change_pct={"1d": 1.0},
                     confidence={"1d": 0.62}))

    assert unmodelled["confidence"] == TECHNICAL_ONLY_CONFIDENCE
    assert unmodelled["inputs"]["confidence_basis"] == "assumed"
    assert unmodelled["inputs"]["model_confidence"] is None
    assert "must not be read as a measurement" in \
        unmodelled["inputs"]["confidence_reason"]

    assert modelled["confidence"] == pytest.approx(0.62)
    assert modelled["inputs"]["confidence_basis"] == "model_mean"
    assert modelled["inputs"]["model_confidence"] == pytest.approx(0.62)
    assert "models reported" in modelled["inputs"]["confidence_reason"]

    # The basis is derived from the same boolean as evidence_basis, so the two
    # fields cannot disagree about whether a model stood behind the row.
    for payload in (unmodelled, modelled):
        assert (payload["inputs"]["confidence_basis"] == "model_mean") == (
            payload["inputs"]["evidence_basis"] == "model_backed")


def test_every_eligible_symbol_carries_its_own_market_risk_sentence():
    """No asset may inherit the Iranian-gold risk line."""
    gold_line = universe.MARKET_RISK["IR_GOLD_18K"]
    assert gold_line == SignalInputs().market_risk  # the scorer's default
    for symbol in universe.SIGNAL_SYMBOLS:
        assert symbol in universe.MARKET_RISK, symbol
        if symbol != "IR_GOLD_18K":
            assert universe.MARKET_RISK[symbol] != gold_line, symbol


# --- thin history ------------------------------------------------------------


def _seed_fund(engine, symbol: str, bars: int) -> None:
    seed_prices(engine, symbol, bars, seed=31)
    seed_prices(engine, universe.FUND_FLOW_SYMBOL, bars, seed=32)


def test_a_fund_at_fifty_bars_publishes_nothing_at_all(engine, settings):
    """The production state of both Tehran gold ETFs, and the defect it was.

    At ~50 daily bars the only directional factor either fund can carry is an
    SMA50 computed as ``series.iloc[-50:].mean()`` — the mean of the ENTIRE
    stored history, a comparison with zero lookback — and it was announced in
    the payload as an "established uptrend".  ``FactorSpec`` now requires the
    window plus a further window before that claim may be made, so the honest
    answer at fifty bars is silence with a stated reason.
    """
    _seed_fund(engine, "IR_GOLD_FUND_AYAR", 50)
    outcome = generate_signal_for(engine, settings, "IR_GOLD_FUND_AYAR")

    assert not outcome.published
    assert "no directional factor" in outcome.withheld_reason.lower()
    omitted = {e["factor"]: e["reason"] for e in outcome.omitted_factors}
    assert universe.FACTOR_TREND_SMA.key in omitted
    # The reason has to be checkable: how many bars, how many it has, and why
    # the requirement is not simply the window.
    reason = omitted[universe.FACTOR_TREND_SMA.key]
    assert "100 daily bars" in reason and "has 50" in reason
    assert "50-bar window" in reason and "read as a trend" in reason

    with engine.connect() as conn:
        assert conn.execute(select(signals)).all() == []


def test_directional_calls_on_pure_noise_are_withheld(engine, settings):
    """The measurement that condemned the old gate, run as a test.

    Fifty-bar random walks at the fixture's own gold-fund drift used to
    produce 34 buy / 22 hold / 4 sell out of 60 and withhold nothing.  Ten
    independent walks are enough to pin the property here — the full 60-trial
    sweep lives in the change's report — and the property is absolute, not
    statistical: at fifty bars there is no directional factor to publish, so
    the withhold rate is 10/10 and not "mostly".
    """
    for trial in range(10):
        eng = create_db_engine("sqlite://")
        metadata.create_all(eng)
        seed_prices(eng, "IR_GOLD_FUND_TALA", 50, seed=9000 + trial)
        outcome = generate_signal_for(eng, settings, "IR_GOLD_FUND_TALA")
        assert not outcome.published, (trial, outcome.payload)
        eng.dispose()


def test_a_fund_with_real_history_publishes_and_still_omits_the_alignment(
    engine, settings
):
    """110 bars: the SMA trend has a lookback behind it at last, the 1D/4H/1H
    stack (220-bar window, 440 required) plainly does not."""
    _seed_fund(engine, "IR_GOLD_FUND_AYAR", 110)
    outcome = generate_signal_for(engine, settings, "IR_GOLD_FUND_AYAR")
    assert outcome.published, outcome.withheld_reason

    omitted = {e["factor"]: e["reason"]
               for e in outcome.payload["inputs"]["omitted_factors"]}
    # Omitted for CAPABILITY, not for history: neither fund is in
    # trend_alignment.SUPPORTED_SYMBOLS, and profile_for reports that reason
    # in preference to the bar count, which would be true as well.
    assert universe.FACTOR_MA_ALIGNMENT.key in omitted
    assert "SUPPORTED_SYMBOLS" in omitted[universe.FACTOR_MA_ALIGNMENT.key]
    assert universe.FACTOR_MA_ALIGNMENT.min_bars == 440
    assert universe.FACTOR_FORECAST.key in omitted
    assert universe.FACTOR_PREMIUM.key in omitted
    # The fund CAN carry the flow factor, so it is not in the omitted list.
    assert universe.FACTOR_FUND_FLOW.key not in omitted
    assert outcome.payload["inputs"]["fund_flow_z"] is not None
    # No model is trained for either fund, so the discount must have bitten.
    assert outcome.payload["inputs"]["evidence_basis"] == "technical_only"
    assert outcome.payload["inputs"]["confidence_basis"] == "assumed"


def test_thin_fund_publishes_no_signal_at_all_when_too_few_factors_survive(
    engine, settings
):
    """40 bars: RSI, momentum, the volatility regime and the fund flow all
    compute — four factors — and the engine still refuses.

    They are four readings of the same recent fortnight and none of them is
    directional. A `hold` published from that would be indistinguishable on
    screen from gold's seven-factor hold, which is the fabrication this gate
    exists to prevent.
    """
    _seed_fund(engine, "IR_GOLD_FUND_TALA", 40)
    outcome = generate_signal_for(engine, settings, "IR_GOLD_FUND_TALA")

    assert not outcome.published
    assert "no directional factor" in outcome.withheld_reason.lower()
    omitted = {e["factor"] for e in outcome.omitted_factors}
    assert universe.FACTOR_TREND_SMA.key in omitted
    assert universe.FACTOR_MA_ALIGNMENT.key in omitted

    with engine.connect() as conn:
        assert conn.execute(select(signals)).all() == []


def test_every_factor_needs_its_window_twice_over():
    """The rule, asserted as a rule rather than as seven integers.

    ``min_bars`` is ``first_bar + window`` for every spec, which is what stops
    a future edit from quietly restoring "the bar at which it first exists" for
    one factor while the class docstring still promises the other thing.
    """
    specs = (
        universe.FACTOR_FORECAST, universe.FACTOR_PREMIUM,
        universe.FACTOR_FUND_FLOW, universe.FACTOR_TREND_SMA,
        universe.FACTOR_MA_ALIGNMENT, universe.FACTOR_RSI,
        universe.FACTOR_MOMENTUM, universe.FACTOR_VOLATILITY,
    )
    for spec in specs:
        assert spec.min_bars == spec.first_bar + spec.window, spec.key
        # A window factor must require strictly more than the bar count at
        # which it first computes — that gap IS the fix.
        if spec.window:
            assert spec.min_bars > spec.first_bar, spec.key

    # The three directional factors, spelled out, because these are the numbers
    # that decide whether an asset may carry a buy/sell call at all.
    assert universe.FACTOR_TREND_SMA.min_bars == 100
    assert universe.FACTOR_MA_ALIGNMENT.min_bars == 440
    assert universe.FACTOR_FORECAST.min_bars == 0  # gated by the trainer, not by bars


def test_history_omission_reasons_carry_both_numbers(engine, settings):
    _seed_fund(engine, "IR_GOLD_FUND_TALA", 40)
    outcome = generate_signal_for(engine, settings, "IR_GOLD_FUND_TALA")
    reason = {e["factor"]: e["reason"] for e in outcome.omitted_factors}[
        universe.FACTOR_TREND_SMA.key
    ]
    assert "100 daily bars" in reason and "has 40" in reason
    # The derived requirement has to explain itself, or 100 for a 50-day
    # average reads as an arbitrary number.
    assert "50-bar window" in reason
    assert "first computable at 50 bars" in reason


# --- per-asset cost ----------------------------------------------------------


def test_coin_and_dollar_do_not_inherit_golds_observed_dealer_spread(engine):
    """The one measured spread in the system belongs to one asset."""
    seed_dealer_spread(engine, 0.49)

    gold = resolve_cost(engine, "IR_GOLD_18K")
    assert gold.basis == "observed_spread"
    assert gold.cost_pct == pytest.approx(0.49)
    assert gold.source == "hamrahgold"

    for symbol in ("IR_COIN_EMAMI", "USD_IRT", "XAUUSD", "XAGUSD",
                   "IR_GOLD_FUND_AYAR", "IR_GOLD_FUND_TALA"):
        res = resolve_cost(engine, symbol)
        assert res.symbol == symbol
        assert res.basis == "assumed", symbol
        assert res.cost_pct == FALLBACK_ROUND_TRIP_COST_PCT, symbol
        assert res.cost_pct != gold.cost_pct
        assert res.source is None
        assert "two-sided quote" in res.reason, symbol

    # And the reason is asset-specific, not one sentence reused.
    assert "minting premium" in resolve_cost(engine, "IR_COIN_EMAMI").reason
    assert "USDT/toman" in resolve_cost(engine, "USD_IRT").reason


def test_the_published_hurdle_travels_with_each_signal(engine, settings):
    """A UI must be able to show that a hurdle is an assumption."""
    seed_dealer_spread(engine, 0.49)
    seed_gold_world(engine)
    seed_prices(engine, "IR_COIN_EMAMI", 260, seed=13)

    gold = generate_signal_for(engine, settings, "IR_GOLD_18K").payload
    coin = generate_signal_for(engine, settings, "IR_COIN_EMAMI").payload

    assert gold["inputs"]["round_trip_cost_basis"] == "observed_spread"
    assert gold["inputs"]["round_trip_cost_pct"] == pytest.approx(0.49)
    assert coin["inputs"]["round_trip_cost_basis"] == "assumed"
    assert coin["inputs"]["round_trip_cost_pct"] == FALLBACK_ROUND_TRIP_COST_PCT
    assert coin["inputs"]["round_trip_cost_pct"] != gold["inputs"]["round_trip_cost_pct"]
    # The coin's reason may NAME hamrahgold — explaining that it quotes 18k
    # gold only is the honest thing to say. What it must never do is claim the
    # measurement, which is what this phrase would mean.
    assert "observed hamrahgold buy/sell spread" in gold["inputs"]["round_trip_cost_reason"]
    assert "observed hamrahgold buy/sell spread" not in coin["inputs"]["round_trip_cost_reason"]
    assert "no dealer publishes a two-sided quote for IR_COIN_EMAMI" in \
        coin["inputs"]["round_trip_cost_reason"]


# --- the job: isolation, eligibility, published universe ---------------------


def _seed_several(engine) -> None:
    seed_gold_world(engine)
    seed_prices(engine, "IR_COIN_EMAMI", 260, seed=13)
    seed_prices(engine, "XAGUSD", 260, seed=17)
    seed_forecasts(engine, "XAUUSD", {"1d": (0.9, 0.6)})
    seed_trend_alignment(engine, "XAUUSD")


def test_one_symbol_raising_does_not_prevent_the_others_being_written(
    engine, settings, monkeypatch
):
    _seed_several(engine)
    import app.jobs.signals as job

    real = job.generate_signal_for

    def exploding(eng, st, symbol):
        if symbol == "IR_COIN_EMAMI":
            raise RuntimeError("synthetic coin failure")
        return real(eng, st, symbol)

    monkeypatch.setattr(job, "generate_signal_for", exploding)
    report = run_signals(engine, settings)

    assert report["symbols"]["IR_COIN_EMAMI"]["status"] == "error"
    assert report["failed"] == 1
    assert any("synthetic coin failure" in e for e in report["errors"])
    assert report["published"] >= 3
    for symbol in ("IR_GOLD_18K", "USD_IRT", "XAUUSD", "XAGUSD"):
        assert report["symbols"][symbol]["status"] == "published", symbol

    with engine.connect() as conn:
        written = {r[0] for r in conn.execute(select(signals.c.symbol))}
    assert "IR_COIN_EMAMI" not in written
    assert {"IR_GOLD_18K", "XAUUSD", "XAGUSD"} <= written


def _job_last_success() -> float | None:
    from prometheus_client import REGISTRY

    return REGISTRY.get_sample_value(
        "talapala_prediction_job_last_success_timestamp_seconds",
        {"job": "signals"},
    )


def test_one_symbol_failing_does_not_freeze_the_freshness_gauge(
    engine, settings, monkeypatch
):
    """JOB_LAST_SUCCESS answers "when did this job last complete a pass?".

    It used to be gated on an empty ``errors`` list, so a single symbol
    raising held the gauge back for all seven: gold could publish normally on
    pass after pass while a staleness alert fired about signals that had just
    been written.  That contradicted this job's own first promise — one
    symbol's failure is its own — because a failure that silences the health
    signal for every other symbol is not its own.

    Which symbol failed is reported per symbol in the report; a single global
    gauge cannot carry that and must not pretend to.
    """
    _seed_several(engine)
    import app.jobs.signals as job

    real = job.generate_signal_for

    def exploding(eng, st, symbol):
        if symbol == "IR_COIN_EMAMI":
            raise RuntimeError("synthetic coin failure")
        return real(eng, st, symbol)

    monkeypatch.setattr(job, "generate_signal_for", exploding)
    before = _job_last_success()
    report = run_signals(engine, settings)
    after = _job_last_success()

    # The pass ran, the coin failed, gold published.
    assert report["failed"] == 1
    assert report["published"] >= 3
    assert report["symbols"]["IR_GOLD_18K"]["status"] == "published"

    assert after is not None
    assert before is None or after >= before
    # The failure is still visible — in the place that can name the symbol.
    assert any("synthetic coin failure" in e for e in report["errors"])
    assert report["symbols"]["IR_COIN_EMAMI"]["status"] == "error"


def test_a_pass_where_every_symbol_fails_is_signalled_as_a_failure(
    engine, settings, monkeypatch
):
    import app.jobs.signals as job

    def exploding(eng, st, symbol):
        raise RuntimeError(f"down: {symbol}")

    monkeypatch.setattr(job, "generate_signal_for", exploding)
    with pytest.raises(job.SignalsGenerationFailed) as excinfo:
        run_signals(engine, settings)
    # The report survives: the failure is signalled, not summarised away.
    assert excinfo.value.report["failed"] == len(universe.SIGNAL_SYMBOLS)
    assert excinfo.value.report["published"] == 0


def test_a_pass_where_everything_is_withheld_is_still_a_success(engine, settings):
    """Nothing seeded: the engine ran and its answer was 'not enough to say'."""
    report = run_signals(engine, settings)
    assert report["failed"] == 0
    assert report["published"] == 0
    assert report["withheld"] == len(universe.SIGNAL_SYMBOLS)
    for entry in report["symbols"].values():
        assert entry["status"] == "withheld"
        assert entry["reason"]


def test_an_ineligible_symbol_is_refused_with_its_stated_reason(engine, settings):
    for symbol, fragment in (("DXY", "macro context"),
                             ("IR_GOLD_FUND_FLOW", "meaningless"),
                             ("IR_HOUSING", "Eligible symbols")):
        with pytest.raises(ValueError) as excinfo:
            run_signals(engine, settings, [symbol])
        assert fragment in str(excinfo.value), symbol
    with pytest.raises(ValueError):
        generate_signal_for(engine, settings, "US10Y")


def test_the_job_publishes_the_universe_it_could_not_score(engine, settings):
    _seed_several(engine)
    run_signals(engine, settings)

    with engine.connect() as conn:
        contract = conn.execute(
            select(app_settings.c.value).where(
                app_settings.c.key == SIGNAL_UNIVERSE_KEY)
        ).scalar_one()

    assert contract["eligible"] == list(universe.SIGNAL_SYMBOLS)
    named = {entry["symbol_or_class"] for entry in contract["unavailable"]}
    # The gap the page has to be able to state.
    assert {"cars", "housing", "tehran_equities", "IR_SILVER"} <= named
    assert {"DXY", "US10Y", "IR_GOLD_FUND_FLOW"} <= named
    for entry in contract["unavailable"]:
        assert len(entry["reason"]) > 40, entry
    # The two funds have no rows in this fixture, so they report why.
    withheld = {e["symbol"]: e["reason"] for e in contract["withheld"]}
    assert "IR_GOLD_FUND_AYAR" in withheld and withheld["IR_GOLD_FUND_AYAR"]


def test_endpoint_generates_every_eligible_symbol_and_narrows_on_request(
    client, engine, settings
):
    _seed_several(engine)

    resp = client.post("/internal/signals/generate",
                       json={"symbols": ["USD_IRT"]}, headers=AUTH)
    assert resp.status_code == 200
    assert list(resp.json()["symbols"]) == ["USD_IRT"]

    resp = client.post("/internal/signals/generate", headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["symbols"]) == set(universe.SIGNAL_SYMBOLS)
    assert body["symbols"]["IR_GOLD_18K"]["status"] == "published"

    resp = client.post("/internal/signals/generate",
                       json={"symbols": ["DXY"]}, headers=AUTH)
    assert resp.status_code == 400
    assert "DXY" in resp.json()["error"]["message"]


def test_stale_data_reports_why_it_is_stale(engine, settings):
    """A forced hold has to say what made it one."""
    newest = utcnow() - timedelta(days=9)
    rows = [
        dict(symbol="USD_IRT", value=90_000.0 + i, currency="IRT", unit="usd",
             source="seed", observed_at=newest - timedelta(days=259 - i),
             collected_at=newest, quality="ok")
        for i in range(260)
    ]
    with engine.begin() as conn:
        conn.execute(prices.insert(), rows)
    payload = generate_signal_for(engine, settings, "USD_IRT").payload
    assert payload["signal"] == "hold"
    assert payload["data_fresh"] is False
    reason = payload["inputs"]["stale_reason"]
    assert "USD_IRT" in reason and "tolerance is 30 minutes" in reason


# --- the fund-flow factor ----------------------------------------------------


def test_crowded_retail_flow_can_only_argue_for_caution():
    """It may withdraw a call; it may never create one.

    This system has not measured which way retail flow predicts, so a
    symmetric factor would be an invented direction.
    """
    base = dict(symbol="IR_GOLD_FUND_AYAR", last_price=110.0, sma20=105.0,
                sma50=100.0, rsi14=50.0, momentum_10_pct=0.0, data_fresh=True,
                expected_change_pct={"1d": 0.0}, confidence={"1d": 0.5})
    flat = compute_signal(SignalInputs(**base))
    crowded = compute_signal(SignalInputs(**base, fund_flow_z=3.0))
    quiet = compute_signal(SignalInputs(**base, fund_flow_z=-3.0))

    assert crowded["score"] < flat["score"]
    assert quiet["score"] == flat["score"]  # never a reason to buy
    assert crowded["inputs"]["fund_flow_points"] == -4.0   # capped
    assert quiet["inputs"]["fund_flow_points"] == 0.0
    assert any("marginal buyer" in c for c in crowded["conflicting"])
    assert any("never counts as a reason to buy" in r for r in crowded["risks"])


def test_a_stale_flow_series_omits_the_flow_factor_rather_than_scoring_it(
    engine, settings
):
    """A month-old crowding reading is not a statement about who is buying now."""
    from datetime import timedelta

    from ._market_fixtures import price_rows

    # 110 bars, so the fund clears FACTOR_TREND_SMA's 100 and the reading is
    # published on its own merits — otherwise this would assert the flow
    # factor's absence on a row that was never going to exist.
    seed_prices(engine, "IR_GOLD_FUND_AYAR", 110, seed=41)
    stale = [
        row | {"observed_at": row["observed_at"] - timedelta(days=40),
               "collected_at": row["collected_at"] - timedelta(days=40)}
        for row in price_rows(universe.FUND_FLOW_SYMBOL, 110, seed=42)
    ]
    with engine.begin() as conn:
        conn.execute(prices.insert(), stale)

    outcome = generate_signal_for(engine, settings, "IR_GOLD_FUND_AYAR")
    assert outcome.published, outcome.withheld_reason
    assert outcome.payload["inputs"]["fund_flow_z"] is None
    omitted = {e["factor"]: e["reason"] for e in outcome.omitted_factors}
    assert "not current" in omitted[universe.FACTOR_FUND_FLOW.key]


def test_the_headline_of_a_forced_hold_says_it_was_the_data(engine, settings):
    from datetime import timedelta

    newest = utcnow() - timedelta(days=9)
    with engine.begin() as conn:
        conn.execute(prices.insert(), [
            dict(symbol="XAGUSD", value=30.0 + i * 0.01, currency="USD",
                 unit="ozt", source="seed",
                 observed_at=newest - timedelta(days=259 - i),
                 collected_at=newest, quality="ok")
            for i in range(260)
        ])
    payload = generate_signal_for(engine, settings, "XAGUSD").payload
    assert payload["signal"] == "hold"
    assert payload["inputs"]["headline"].startswith("Input data is stale")
    assert payload["explanation"].startswith(payload["inputs"]["headline"] + " ")


def test_no_published_asset_drifts_out_of_the_research_register(engine, settings):
    """The hedged wording is a property of the ENGINE, not of the gold fixture.

    Six of the seven assets were never scored before this change, and three of
    them carry no model at all — exactly the situation in which a confident
    sentence would be least defensible.
    """
    import re

    forbidden = re.compile(
        r"guarantee|guaranteed|certainly|will definitely|cannot lose|risk-free|"
        r"sure thing|you should|must buy|must sell",
        re.IGNORECASE,
    )
    _seed_several(engine)
    seed_prices(engine, "IR_GOLD_FUND_AYAR", 60, seed=41)
    seed_prices(engine, universe.FUND_FLOW_SYMBOL, 60, seed=42)
    run_signals(engine, settings)

    with engine.connect() as conn:
        rows = [r._mapping for r in conn.execute(select(signals))]
    assert len(rows) >= 5
    for row in rows:
        blob = " ".join(
            [row["explanation"], row["invalidation"]]
            + list(row["supporting"]) + list(row["conflicting"]) + list(row["risks"])
        )
        assert not forbidden.search(blob), (row["symbol"], blob)
        assert "not financial advice" in row["explanation"], row["symbol"]
        # Every asset states its OWN market hazard, never gold's by default.
        assert universe.MARKET_RISK[row["symbol"]] in row["risks"], row["symbol"]
