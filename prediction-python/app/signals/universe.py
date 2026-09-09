"""Which assets can carry a buy/hold/sell reading, and which factors each one
can honestly carry.

This module is the answer to one question the engine could previously avoid:
*what is this score actually made of, for THIS asset?*  While IR_GOLD_18K was
the only symbol scored, the answer was implicit — every factor in
``compute_signal`` was available, so nothing had to be declared.  Scoring
seven assets makes the differences load-bearing, because they are large:

* only two symbols have a trained forecasting model behind them,
* only one has a theoretical parity price to measure a local premium against,
* two of them hold about fifty days of history in total.

The rule this module exists to enforce is that **a factor that does not apply
is omitted with its reason, never defaulted to a neutral value**.  A neutral
default is not neutral.  ``compute_signal`` starts at 50 and every factor
pushes away from it, so a factor silently contributed as 0 does two things at
once: it dilutes every real factor's share of the final number, and it reads
downstream as evidence that was weighed and found balanced.  "We could not
tell" and "the evidence is balanced" are opposite statements and must not
produce the same score.

The engine already had this instinct in two places — the Addendum 15
technical-only branch (no forecasts discounts the score and SAYS so rather
than assuming 0.5 confidence) and the Addendum 21 alignment gate (an
unavailable read contributes nothing rather than a neutral 0).  This
generalises both instead of adding a third mechanism beside them.

One correction to that sentence, since it stood here while being false: the
technical-only branch DECLARED a discount it did not apply.  Its multiply sat
at a point in ``compute_signal`` where the score could only be exactly 50, so
it was arithmetically a no-op and a bullish technical board with no model
behind it published at 74-78 — buy, and at the top of that range strong_buy.
It is now applied after every factor has been counted; see
``app.signals.engine.TECHNICAL_ONLY_WEIGHT``, which also says plainly that the
number is a stated policy rather than a calibration.

Nothing here is a market opinion.  It is a statement about what this system
has measured, and every exclusion below names the measurement that is missing.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --- the eligible universe --------------------------------------------------
#
# Verified against production on 2026-09-09 (rows in `prices`, per symbol):
#   IR_GOLD_18K       18,593  from 2013-07-22   7 active models
#   USD_IRT           17,850  from 2011-11-26   no model
#   XAGUSD            11,813  from 2021-07-26   no model
#   XAUUSD            11,678  from 2021-12-30   7 active models
#   IR_COIN_EMAMI      6,642  from 2010-04-04   no model
#   IR_GOLD_FUND_AYAR    101  from 2026-07-21   no model, ~50 daily bars
#   IR_GOLD_FUND_TALA    101  from 2026-07-21   no model, ~50 daily bars
#
# Every one of these is something a reader can actually hold, and every one
# has real observations behind it. The list is not widened by hope: an asset
# joins it when rows exist, not when a page would look better with it.
#
# ELIGIBLE IS NOT THE SAME AS SCOREABLE, and for the two funds it currently is
# not. At ~50 daily bars neither fund clears FACTOR_TREND_SMA's 100, and the
# SMA20/50 trend is the only directional factor either of them could carry —
# so both are withheld by :func:`withholding_reason` on every pass until they
# have accumulated roughly another fifty sessions. That is the intended
# outcome, not an outage: what they were publishing before was a call on a
# fifty-bar mean with no lookback behind it (see :class:`FactorSpec`). They
# stay in this list because the withholding is itself published, with its
# reason, through app/jobs/signals.py — a reader who wants to know why there
# is no fund call can be told, which is impossible for a symbol that was
# quietly dropped from the universe.
SIGNAL_SYMBOLS: tuple[str, ...] = (
    "IR_GOLD_18K",
    "USD_IRT",
    "IR_COIN_EMAMI",
    "XAUUSD",
    "XAGUSD",
    "IR_GOLD_FUND_AYAR",
    "IR_GOLD_FUND_TALA",
)

# The two symbols a model is trained for; mirrors
# app.models.training.FORECAST_SYMBOLS, which is imported lazily below rather
# than duplicated, so the two cannot drift apart.
FUND_SYMBOLS: frozenset[str] = frozenset({"IR_GOLD_FUND_AYAR", "IR_GOLD_FUND_TALA"})
# The one symbol with a theoretical parity price: 18k gold has a closed-form
# fair value (XAUUSD / troy ounce * USD_IRT * 0.750) and therefore a premium.
PARITY_SYMBOLS: frozenset[str] = frozenset({"IR_GOLD_18K"})
# The Tehran gold-ETF retail/institutional flow ratio that the two fund
# symbols are read against.
FUND_FLOW_SYMBOL = "IR_GOLD_FUND_FLOW"

# Symbols this system collects and serves, which are deliberately NOT given a
# buy/hold/sell reading. Each reason is about the instrument, not about data
# availability — all three have plenty of rows.
EXCLUDED_SYMBOLS: dict[str, str] = {
    "DXY": (
        "The dollar index is macro context, not a holding: it is a basket "
        "level with no price a reader buys or sells, so a buy/sell call on it "
        "would not describe any action available to them."
    ),
    "US10Y": (
        "A Treasury yield in percent, not a price. It moves inversely to the "
        "bond, so 'buy' would be ambiguous about which of the two it means, "
        "and the bond itself is not an instrument this system collects."
    ),
    FUND_FLOW_SYMBOL: (
        "A retail/institutional flow RATIO in percent (migration 0024 files it "
        "as kind='index' for exactly this reason). Nobody holds a ratio, so a "
        "'buy' on it is meaningless. It is used here as an INPUT to the two "
        "fund symbols' readings instead."
    ),
}

# Whole asset classes a reader will look for and not find. Naming them is the
# point: an overview page that lists seven symbols and stops implies that is
# the investable universe, and it is not — it is the part of it this system
# has measurements for.
NOT_COLLECTED: tuple[dict[str, str], ...] = (
    {
        "symbol_or_class": "cars",
        "reason": "Not collected: `prices` holds zero rows for any vehicle. "
                  "No price series exists to score.",
    },
    {
        "symbol_or_class": "housing",
        "reason": "Not collected: `prices` holds zero rows for any housing "
                  "series. No price series exists to score.",
    },
    {
        "symbol_or_class": "tehran_equities",
        "reason": "Not collected: `prices` holds zero rows for any "
                  "Tehran-listed share. The two gold ETFs below are the only "
                  "exchange-traded instruments collected.",
    },
    {
        "symbol_or_class": "IR_SILVER",
        "reason": "No local Iranian silver series exists. XAGUSD is the COMEX "
                  "front-month future in USD, which is not the toman-quoted "
                  "silver a reader in Iran would buy, and presenting it as one "
                  "would be a substitution, not a proxy.",
    },
    {
        "symbol_or_class": "economic_series",
        "reason": "Every kind='economic_series' instrument (CPI, money supply, "
                  "IMF/World Bank aggregates) is reference data with a "
                  "publication lag measured in months. It is not held, not "
                  "traded, and not scoreable as a position.",
    },
)


def unavailable() -> list[dict[str, str]]:
    """The complete, ordered statement of what carries no signal, and why.

    Deliberately one list rather than two: from a reader's point of view "we
    do not score the dollar index" and "we hold no housing data" are the same
    question — *why is this not here?* — and answering them in two different
    places is how one of them ends up unanswered on the page.
    """
    return [
        {"symbol_or_class": symbol, "reason": reason}
        for symbol, reason in EXCLUDED_SYMBOLS.items()
    ] + [dict(entry) for entry in NOT_COLLECTED]


# --- factors ----------------------------------------------------------------


@dataclass(frozen=True)
class FactorSpec:
    """One scoreable factor, the window it reads, and the history it needs.

    THE TWO NUMBERS ARE NOT THE SAME NUMBER, AND CONFLATING THEM PUBLISHED
    DIRECTIONAL CALLS ON NOISE.

    ``window`` is the longest lookback the statistic itself spans.
    ``first_bar`` is the bar at which it first produces a value at all.
    ``min_bars`` — what the engine actually gates on — is ``first_bar +
    window``, and the gap between them is the whole point of this class.

    This module used to gate on ``first_bar`` and call that "deliberately
    literal": an SMA50 on exactly 50 bars is a real SMA50.  It is not.  At
    exactly 50 bars ``series.iloc[-50:].mean()`` is the mean of the ENTIRE
    stored history, so "price is above its SMA50" degenerates into "price is
    above the average of everything we have" — a comparison with zero lookback,
    announced to the reader as an *established uptrend*.  Measured before this
    changed: 60 independent 50-bar random walks at the fixture's own gold-fund
    drift produced 34 buy, 22 hold, 4 sell and nothing withheld.  An SMA50 on
    exactly 50 bars was, in fact, "a different statistic wearing the same
    label" — precisely the failure the old docstring claimed to be guarding
    against, one bar-count off.

    So a window factor must be able to have MOVED before it may be read as a
    trend.  ``first_bar + window`` is the least history that guarantees it: at
    that point the newest window and the oldest window the series could have
    been compared against share no observation, so the average the price is
    held up to was formed before the stretch being judged.  It is a structural
    minimum, not a calibration — no return series was fitted to choose it, and
    it is stated here rather than tuned so that a future reader can argue with
    the rule instead of with seven unexplained integers.

    ``directional`` marks the factors that make a claim about where price sits
    relative to more than a fortnight of its own history.  See
    :func:`withholding_reason`.
    """

    key: str
    label: str
    window: int
    first_bar: int
    directional: bool = False

    @property
    def min_bars(self) -> int:
        """Daily bars required before this factor may be scored at all."""
        return self.first_bar + self.window


# (window, first_bar) sources, so these cannot drift from the code that
# computes them.  ``first_bar`` is where the value first exists; ``min_bars``
# is derived above and is what the engine gates on:
#   momentum_10 = close / close.shift(10)             window 10, first 11  -> 21
#   rsi_14      = diff + rolling(14)                   window 14, first 15  -> 29
#   detect_regime(window=20) 'unknown' below 25        window 20, first 25  -> 45
#   premium_z_30 / the flow z-score: rolling(30)       window 30, first 30  -> 60
#   the SMA trend factor needs BOTH sma20 and sma50    window 50, first 50  -> 100
#   the 1D alignment leg needs a 220-period slow MA    window 220, first 220 -> 440
# The forecast factor reads no price window at all — a trained model's own
# history requirement is enforced by the trainer — so both numbers are 0 and
# min_bars is 0.
FACTOR_FORECAST = FactorSpec(
    "forecast_vs_cost", "forecast edge vs round-trip cost", 0, 0,
    directional=True)
FACTOR_PREMIUM = FactorSpec("local_premium_z", "local premium z-score", 30, 30)
FACTOR_FUND_FLOW = FactorSpec("gold_fund_flow", "gold-fund retail flow", 30, 30)
FACTOR_TREND_SMA = FactorSpec(
    "trend_sma", "price vs SMA20/SMA50", 50, 50, directional=True)
FACTOR_MA_ALIGNMENT = FactorSpec(
    "ma_alignment", "1D/4H/1H moving-average alignment", 220, 220,
    directional=True)
FACTOR_RSI = FactorSpec("rsi14", "RSI(14) zones", 14, 15)
FACTOR_MOMENTUM = FactorSpec("momentum_10", "10-period momentum", 10, 11)
FACTOR_VOLATILITY = FactorSpec("volatility_regime", "volatility regime", 20, 25)

# Available to any symbol with enough history; nothing about them is
# gold-specific.
_UNIVERSAL_FACTORS: tuple[FactorSpec, ...] = (
    FACTOR_TREND_SMA,
    FACTOR_RSI,
    FACTOR_MOMENTUM,
    FACTOR_VOLATILITY,
)

# How many factors must actually produce a number before the engine will
# publish a call at all, and why three plus a directional one.
#
# The four short-window factors are not four independent readings.  RSI(14)
# and momentum(10) are two views of the same fortnight of closes and move
# together most of the time; the volatility regime only ever subtracts (it is
# a caution term, never a reason); the gold-fund flow, likewise, can only
# argue for caution here because this system has not measured a direction for
# it.  A "buy" resting on that set alone would be one fortnight of price
# looked at three ways, published with the same words and the same 0-100 scale
# as gold's seven-factor reading.  The reader cannot see the difference, so the
# engine must refuse to make it.
#
# The directional requirement is therefore the real gate and the count is the
# floor beneath it: at least one of a model forecast, the price-vs-its-own-
# averages trend, or the multi-timeframe stack has to survive.  Those are the
# only three factors that compare the present to a horizon longer than a
# couple of weeks.  Withholding is a normal outcome, not an error: "not enough
# to say anything about this asset" is information, and a hold published in
# its place would be a fabrication wearing a neutral face.
MIN_SCOREABLE_FACTORS = 3


def _forecast_symbols() -> frozenset[str]:
    """The symbols a model is trained for, read from the trainer's own list.

    Imported lazily: app.models.training pulls in scikit-learn and statsmodels,
    and this module is on the import path of the API process.
    """
    from ..models.training import FORECAST_SYMBOLS

    return frozenset(FORECAST_SYMBOLS)


def _alignment_symbols() -> frozenset[str]:
    """Symbols the trend-alignment job actually evaluates.

    Also read from the owning module rather than restated: it publishes its
    own list precisely because only two symbols have the continuous history a
    220-period slow MA on three timeframes needs.
    """
    from ..jobs.trend_alignment import SUPPORTED_SYMBOLS

    return frozenset(SUPPORTED_SYMBOLS)


@dataclass(frozen=True)
class Profile:
    """What one symbol can and cannot carry, before any history is counted."""

    symbol: str
    applicable: tuple[FactorSpec, ...]
    # (factor key, why this asset can never carry it)
    inapplicable: tuple[tuple[str, str], ...]

    def keys(self) -> frozenset[str]:
        return frozenset(spec.key for spec in self.applicable)


def profile_for(symbol: str) -> Profile:
    """The capability profile for one eligible symbol.

    Raises :class:`ValueError` for anything outside :data:`SIGNAL_SYMBOLS`,
    naming the reason when there is a stated one.  Refused, never coerced to
    gold and never silently dropped.
    """
    if symbol not in SIGNAL_SYMBOLS:
        stated = EXCLUDED_SYMBOLS.get(symbol)
        detail = f": {stated}" if stated else ""
        raise ValueError(
            f"{symbol!r} is not an eligible signal symbol{detail}"
            + ("" if stated else
               f" (eligible: {', '.join(SIGNAL_SYMBOLS)})")
        )

    applicable: list[FactorSpec] = list(_UNIVERSAL_FACTORS)
    inapplicable: list[tuple[str, str]] = []

    forecast_symbols = _forecast_symbols()
    if symbol in forecast_symbols:
        applicable.append(FACTOR_FORECAST)
    else:
        inapplicable.append((
            FACTOR_FORECAST.key,
            f"No forecasting model is trained for {symbol}: "
            f"app.models.training.FORECAST_SYMBOLS covers "
            f"{', '.join(sorted(forecast_symbols))}. This reading is "
            f"technical-only and carries no model evidence.",
        ))

    if symbol in PARITY_SYMBOLS:
        applicable.append(FACTOR_PREMIUM)
    else:
        inapplicable.append((
            FACTOR_PREMIUM.key,
            "The local premium is measured against the 18k parity price "
            "(XAUUSD / troy ounce x USD_IRT x 0.750). No such theoretical "
            f"price is defined for {symbol}, so it has no premium to be rich "
            "or cheap against.",
        ))

    if symbol in FUND_SYMBOLS:
        applicable.append(FACTOR_FUND_FLOW)
    else:
        inapplicable.append((
            FACTOR_FUND_FLOW.key,
            f"{FUND_FLOW_SYMBOL} is the retail/institutional flow ratio of the "
            f"Tehran gold ETFs. It describes who is trading those funds and "
            f"says nothing about {symbol}.",
        ))

    alignment_symbols = _alignment_symbols()
    if symbol in alignment_symbols:
        applicable.append(FACTOR_MA_ALIGNMENT)
    else:
        inapplicable.append((
            FACTOR_MA_ALIGNMENT.key,
            "The 1D/4H/1H stack needs a 220-period slow MA on all three "
            "timeframes; app.jobs.trend_alignment.SUPPORTED_SYMBOLS covers "
            f"{', '.join(sorted(alignment_symbols))} because only those have "
            "the continuous history for it.",
        ))

    return Profile(
        symbol=symbol,
        applicable=tuple(applicable),
        inapplicable=tuple(inapplicable),
    )


def history_omissions(
    profile: Profile, bars: int
) -> tuple[frozenset[str], list[dict[str, str]]]:
    """Split a profile against the history actually available.

    Returns ``(factor keys the history supports, omissions with reasons)``.
    The omission text carries every number, because "omitted" without them is
    not something a reader can check or act on — and because ``min_bars`` is
    now larger than the bar count at which the statistic first computes, a
    reason that quoted only the total would look arbitrary.  It says which
    part is the window and which part is the lookback the window needs behind
    it; see :class:`FactorSpec`.
    """
    supported: list[str] = []
    omissions: list[dict[str, str]] = []
    for spec in profile.applicable:
        if bars >= spec.min_bars:
            supported.append(spec.key)
        else:
            omissions.append({
                "factor": spec.key,
                "reason": (
                    f"{spec.label} needs {spec.min_bars} daily bars and "
                    f"{profile.symbol} has {bars}."
                    + (
                        f" ({spec.window}-bar window, first computable at "
                        f"{spec.first_bar} bars, plus a further {spec.window} "
                        f"so the window has moved before it is read as a "
                        f"trend.)"
                        if spec.window
                        else ""
                    )
                ),
            })
    return frozenset(supported), omissions


def withholding_reason(scored: frozenset[str]) -> Optional[str]:
    """``None`` when ``scored`` may be published, else why it may not.

    ``scored`` is the set of factors that actually produced a number — not the
    ones that were applicable in principle.
    """
    directional = {
        FACTOR_FORECAST.key, FACTOR_TREND_SMA.key, FACTOR_MA_ALIGNMENT.key,
    }
    if not scored & directional:
        return (
            "No directional factor could be computed: none of a model "
            "forecast, the SMA20/50 trend or the 1D/4H/1H alignment survived. "
            "The remaining factors all read the same recent fortnight, which "
            "is not enough to publish a buy/hold/sell."
        )
    if len(scored) < MIN_SCOREABLE_FACTORS:
        return (
            f"Only {len(scored)} factor(s) could be computed "
            f"({', '.join(sorted(scored))}); at least "
            f"{MIN_SCOREABLE_FACTORS} are required to publish a "
            f"buy/hold/sell."
        )
    return None


# --- the one market-specific risk sentence per asset ------------------------
#
# ``compute_signal`` has always appended a second risk line naming the market's
# own hazard, and it was hardcoded to Iranian gold. Served beside a silver or
# dollar reading it would simply be false, so it moves here, keyed by symbol.
# The gold string is reproduced BYTE FOR BYTE from the engine's previous
# literal — this table must not be the thing that changes gold's payload.
MARKET_RISK: dict[str, str] = {
    "IR_GOLD_18K":
        "Iranian gold prices are exposed to currency policy shocks and "
        "liquidity gaps.",
    "USD_IRT":
        "The free-market rate is exposed to currency policy, sanctions news "
        "and administrative intervention, and this series is a 24/7 "
        "USDT/toman proxy rather than a cash-dollar quote.",
    "IR_COIN_EMAMI":
        "The Emami coin trades at its own bazaar premium over its gold "
        "content, which moves on domestic demand independently of the metal.",
    "XAUUSD":
        "This is the COMEX front-month future used as a spot proxy: it "
        "carries roll and contango effects and its own settlement calendar, "
        "and it is quoted in USD rather than toman.",
    "XAGUSD":
        "Silver is materially more volatile than gold and carries a large "
        "industrial-demand component; this is the COMEX front-month future "
        "used as a spot proxy, quoted in USD rather than toman.",
    "IR_GOLD_FUND_AYAR":
        "Fund units trade only in the Tehran session and are subject to price "
        "limits and trading halts; the unit price can deviate from the gold "
        "the fund holds.",
    "IR_GOLD_FUND_TALA":
        "Fund units trade only in the Tehran session and are subject to price "
        "limits and trading halts; the unit price can deviate from the gold "
        "the fund holds.",
}
