"""Single source of truth for the round-trip trading cost (Addendum 15).

Before this module the system carried FOUR different hurdles at once: the
signal engine's hardcoded 2.2%, the custom forecast's 2.2%, the backtest's
1.65% entry rule, and the UI's live dealer spread (~0.49%). The headline
signal could therefore say "below the ~2.2% cost threshold" for the very move
the action planner on the same page called "favors buying".

The honest cost is what a real round trip actually pays: buy at the dealer's
sell price, later sell at their buy price. Hamrah Gold publishes both sides,
so the observed spread IS that cost, and it is stored on every observation.

Resolution order, per asset:
  1. the most recent observed dealer spread for THAT asset (<= MAX_AGE_HOURS old);
  2. otherwise the conservative fixed assumption, clearly flagged as such,
     carrying the reason no observed cost exists for that asset.

WHY THE ASSUMPTION IS THE SAME NUMBER FOR EVERY ASSET THAT LACKS A QUOTE

Only IR_GOLD_18K has an observed round-trip cost, because Hamrah Gold is the
only source here that publishes both sides of a quote.  The obvious-looking
fix for the other six symbols -- a plausible per-market figure each -- would
be worse than useless: a coin spread, a USDT/toman exchange fee or a TSE
brokerage commission written down from memory is a number nobody measured,
and once it is in the payload it is indistinguishable from the 0.49% that WAS
measured.  Precision that was invented is the specific failure this module
was created to end (see the four-competing-hurdles history above).

So an asset without a quote gets the same conservative structural assumption
gold falls back to, and says per asset WHY nothing better is available.  The
consequence is deliberate and visible: an asset whose hurdle is assumed at
2.2% needs a much larger forecast edge to earn a directional call than gold
does at its observed 0.49%.  That asymmetry is honest -- it is the difference
between a cost we measured and a cost we did not -- and ``basis`` says which
of the two the reader is looking at.

What is NOT done here, under any circumstance: reuse gold's OBSERVED dealer
spread for another asset.  The coin carries a minting premium and a wider
bazaar margin, the dollar trades on an exchange with its own fee schedule,
and the funds pay brokerage commission -- different markets with different
spreads.  Borrowing gold's measurement for them would label a fabrication
``observed_spread``, which is the worst outcome available.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..db import ensure_utc, raw_observations, utcnow

log = logging.getLogger(__name__)

# Conservative fallback: fee both sides + dealer spread + slippage both sides.
DEFAULT_FEE_PCT = 0.5
DEFAULT_SPREAD_PCT = 1.0
DEFAULT_SLIPPAGE_PCT = 0.1
FALLBACK_ROUND_TRIP_COST_PCT = (
    2 * DEFAULT_FEE_PCT + DEFAULT_SPREAD_PCT + 2 * DEFAULT_SLIPPAGE_PCT
)

SPREAD_PROVIDER = "hamrahgold"

# Which provider publishes a two-sided quote for which symbol. One entry,
# today. It is a map rather than a constant so that the day a dealer feed for
# the coin or a fund lands, the asset starts resolving to an OBSERVED cost by
# appearing here -- and until that day, its absence is what makes every other
# asset's hurdle correctly say "assumed".
SPREAD_SOURCES: dict[str, str] = {"IR_GOLD_18K": SPREAD_PROVIDER}

DEFAULT_COST_SYMBOL = "IR_GOLD_18K"

# Why each asset has no observed round-trip cost. Not boilerplate: it is what
# the UI shows beside an assumed hurdle, and it is what tells a future reader
# which measurement would have to exist for the number to stop being a guess.
NO_OBSERVED_COST_REASON: dict[str, str] = {
    "USD_IRT":
        "collected as a single mid price from the 24/7 USDT/toman market; no "
        "order book and no dealer buy/sell pair is stored, so the real round "
        "trip (exchange fee both sides plus the book's spread) has never been "
        "measured here",
    "IR_COIN_EMAMI":
        "no source here publishes a two-sided Emami-coin quote; hamrahgold "
        "quotes 18k gold only, and the coin is a different market -- it "
        "carries a minting premium and its own, wider, bazaar margin",
    "XAUUSD":
        "collected from the Yahoo GC=F front-month settlement, a single "
        "settlement price with no bid/ask; broker commission and futures roll "
        "cost are not observed by this system",
    "XAGUSD":
        "collected from the Yahoo SI=F front-month settlement, a single "
        "settlement price with no bid/ask; broker commission and futures roll "
        "cost are not observed by this system",
    "IR_GOLD_FUND_AYAR":
        "TSE fund units are collected as a single traded price; brokerage "
        "commission and the unit's own bid/ask are not collected",
    "IR_GOLD_FUND_TALA":
        "TSE fund units are collected as a single traded price; brokerage "
        "commission and the unit's own bid/ask are not collected",
}

MAX_AGE_HOURS = 72
# Sanity band: a spread outside this range is a parsing accident, not a quote.
MIN_SANE_PCT = 0.1
MAX_SANE_PCT = 10.0


# How many recent rows to scan for a usable quote. The newest row can be a
# suspect/outlier or a malformed payload; an older GOOD quote is far better
# evidence than the blanket 2.2% assumption, so we walk back a bounded window
# instead of giving up on the first bad row.
SCAN_LIMIT = 25


@dataclass(frozen=True)
class CostResolution:
    """Full provenance of the round-trip cost hurdle used by the decision layer.

    ``symbol`` leads and has no default, deliberately.  A cost hurdle that
    does not say which asset it was resolved for is exactly how gold's
    measured 0.49% would end up gating a decision about the coin.
    """

    symbol: str
    cost_pct: float
    basis: str                      # "observed_spread" | "assumed"
    source: Optional[str]           # provider code when observed
    observed_at: Optional[object]   # UTC datetime of the quote used
    age_hours: Optional[float]
    reason: str                     # why this basis was chosen (audit trail)
    rejected: int = 0               # rows skipped before an acceptable one

    def as_dict(self) -> dict:
        """Serializable provenance, for the payload the UI reads.

        The point of shipping this whole record rather than just the number is
        that the page can say "assumed, because ..." instead of printing a
        hurdle that looks measured.
        """
        return {
            "symbol": self.symbol,
            "cost_pct": round(self.cost_pct, 4),
            "cost_basis": self.basis,
            "cost_source": self.source,
            "cost_observed_at": (
                self.observed_at.isoformat() if self.observed_at else None
            ),
            "cost_age_hours": self.age_hours,
            "cost_reason": self.reason,
        }


def _assumed(symbol: str, reason: str, rejected: int = 0) -> CostResolution:
    """The conservative structural assumption, flagged as one.

    Every asset without an observed quote lands here with the SAME number and
    a different reason; see the module docstring for why the number is not
    made asset-specific.
    """
    return CostResolution(
        symbol, FALLBACK_ROUND_TRIP_COST_PCT, "assumed", None, None, None,
        reason, rejected,
    )


def resolve_cost(engine: Engine, symbol: str = DEFAULT_COST_SYMBOL) -> CostResolution:
    """Resolve ``symbol``'s round-trip cost with full provenance.

    An asset with no entry in :data:`SPREAD_SOURCES` never touches the
    database: there is nothing to look up, and the assumption it gets says so
    in its own words rather than gold's.  This is the whole per-asset point —
    the previous single-asset version would happily have returned the Hamrah
    Gold spread to a caller asking about the Emami coin.

    For the one asset that HAS a source, only ``quality='ok'`` raw
    observations are eligible.  This is load-bearing: the classifier marks
    MAD-failing rows ``suspect``/``outlier`` and they are deliberately kept out
    of ``prices``, but the spread lives on the RAW row — so without this filter
    a rejected quote could still set the hurdle that gates every buy/sell
    recommendation.
    """
    provider = SPREAD_SOURCES.get(symbol)
    if provider is None:
        detail = NO_OBSERVED_COST_REASON.get(symbol)
        return _assumed(
            symbol,
            f"no dealer publishes a two-sided quote for {symbol} into this "
            f"system" + (f": {detail}" if detail else "")
            + "; using the conservative assumption",
        )

    now = utcnow()
    cutoff = now - timedelta(hours=MAX_AGE_HOURS)
    stmt = (
        select(raw_observations.c.raw_payload, raw_observations.c.observed_at)
        .where(
            raw_observations.c.provider_code == provider,
            raw_observations.c.symbol == symbol,
            raw_observations.c.quality == "ok",
            raw_observations.c.observed_at >= cutoff,
        )
        .order_by(raw_observations.c.observed_at.desc())
        .limit(SCAN_LIMIT)
    )
    try:
        with engine.connect() as conn:
            rows = conn.execute(stmt).all()
    except Exception as exc:  # noqa: BLE001 — cost lookup must never sink a job
        log.warning("spread lookup failed for %s: %s", symbol, exc)
        return _assumed(symbol, "spread lookup failed; using the conservative assumption")

    rejected = 0
    for payload, observed_at in rows:
        if not isinstance(payload, dict):
            rejected += 1
            continue
        value = payload.get("spread_pct")
        if not isinstance(value, (int, float)):
            rejected += 1
            continue
        value = float(value)
        if not (MIN_SANE_PCT <= value <= MAX_SANE_PCT):
            rejected += 1  # parsing accident, not a quote
            continue
        observed_at = ensure_utc(observed_at)
        age = (now - observed_at).total_seconds() / 3600.0
        return CostResolution(
            symbol, value, "observed_spread", provider, observed_at, round(age, 2),
            f"observed {provider} buy/sell spread, {age:.1f}h old"
            + (f" ({rejected} newer row(s) skipped as unusable)" if rejected else ""),
            rejected,
        )

    reason = (
        f"no usable {provider} spread in the last {MAX_AGE_HOURS}h"
        if not rows
        else f"all {len(rows)} recent {provider} rows were unusable"
    )
    return _assumed(symbol, reason, rejected)


def observed_spread_pct(engine: Engine, symbol: str = DEFAULT_COST_SYMBOL) -> Optional[float]:
    """Most recent USABLE observed dealer buy/sell spread in percent, or None."""
    res = resolve_cost(engine, symbol)
    return res.cost_pct if res.basis == "observed_spread" else None


def round_trip_cost_pct(
    engine: Engine, symbol: str = DEFAULT_COST_SYMBOL
) -> tuple[float, str]:
    """``(cost_pct, basis)`` where basis is ``observed_spread`` or ``assumed``."""
    res = resolve_cost(engine, symbol)
    return res.cost_pct, res.basis


# --- Decision policy: ONE backend-owned contract (Addendum 16) ---------------
# The frontend previously implemented its own rules and diverged from Python on
# three axes: fallback cost (1.5% vs 2.2%), sell threshold (half cost vs full
# cost) and the confidence gate (55% hard requirement vs none). The UI must
# FORMAT decisions, never invent them, so the thresholds live here and are
# published to the API.
DECISION_POLICY_KEY = "decision_policy"

# A directional call needs a move that clears the round-trip cost AND enough
# model confidence to be worth acting on.
MIN_CONFIDENCE_PCT = 55.0
# Selling is judged on HALF the round-trip cost, deliberately: a holder who
# sells pays only the exit leg, whereas buy-then-sell pays both. This
# asymmetry is real, not an oversight — it is preserved here so that moving
# the rule server-side changes WHERE it is defined, not WHAT it decides.
SELL_THRESHOLD_MULTIPLE = 0.5


def decision_policy(engine: Engine) -> dict:
    """The complete, serializable decision contract used by every surface.

    IR_GOLD_18K's, and it now says so.  This contract drives the on-page action
    planner, which is a gold surface; the per-asset hurdles the multi-symbol
    signals carry travel inside each signal's own ``inputs`` instead, so no
    reader has to assume that one published policy applies to seven markets.
    """
    res = resolve_cost(engine, DEFAULT_COST_SYMBOL)
    return {
        "symbol": res.symbol,
        "cost_pct": round(res.cost_pct, 4),
        "cost_basis": res.basis,
        "cost_source": res.source,
        "cost_observed_at": res.observed_at.isoformat() if res.observed_at else None,
        "cost_age_hours": res.age_hours,
        "cost_reason": res.reason,
        "buy_threshold_pct": round(res.cost_pct, 4),
        "sell_threshold_pct": round(res.cost_pct * SELL_THRESHOLD_MULTIPLE, 4),
        "min_confidence_pct": MIN_CONFIDENCE_PCT,
        "fallback_cost_pct": FALLBACK_ROUND_TRIP_COST_PCT,
        "policy_version": 1,
    }
