"""Event taxonomy — HYPOTHESES about direction, not measured effects.

Every prior below is an economic argument written down *before* any event study
exists, so a later study can confirm or refute it instead of being fitted to
whatever the accumulated data happens to show.  Nothing here was estimated:
the system has no historical news archive, no news feature reaches a model, and
``news_events`` is not yet populated by anything.

Priors are stated for ONE polarity per category (:attr:`Category.prior_polarity`
— e.g. "hawkish surprise").  An event carrying the opposite sign flips every
channel; see :func:`opposite` and the ``polarity`` column on ``news_events``.

The three channels are the three places a shock can land in this system:

* ``xauusd`` — the global dollar gold price;
* ``usd_irt`` — the free-market toman price of the dollar;
* ``local_premium`` — the observed Tehran 18k price over its theoretical value
  (``premium_pct`` in docs/CONTRACTS.md).  Not redundant with the other two:
  it is where local frictions live — sticky bazaar quotes, physical scarcity,
  panic demand — and it is the only channel a purely global shock can reach
  through a *lag* rather than a level.

A recurring hypothesis worth stating once: when XAUUSD moves sharply, Tehran
quotes adjust with a lag, so the measured premium moves the OTHER way for a
while.  Several categories below inherit that mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Direction of the hypothesised effect on a channel.
UP = "up"
DOWN = "down"
NONE = "none"            # no plausible mechanism at all
AMBIGUOUS = "ambiguous"  # a mechanism exists but its sign is genuinely unknown
DIRECTIONS = (UP, DOWN, NONE, AMBIGUOUS)

# How much weight the prior deserves a priori.  Never "strong": nothing here
# has been measured on this system's own data.
NO_PRIOR = "none"
WEAK = "weak"
MODERATE = "moderate"
PRIOR_STRENGTHS = (NO_PRIOR, WEAK, MODERATE)

# Marks every entry in this module as unverified.  A future event study should
# flip individual categories to a measured status — and must be allowed to
# refute them.
EVIDENCE_HYPOTHESIS = "hypothesis"


@dataclass(frozen=True)
class Channel:
    """Hypothesised effect on one channel."""

    direction: str        # one of DIRECTIONS
    prior_strength: str   # one of PRIOR_STRENGTHS
    rationale: str        # the mechanism, in one line


@dataclass(frozen=True)
class Category:
    """One taxonomy entry: a label plus its three channel hypotheses."""

    code: str
    label: str
    prior_polarity: str   # the event sign the channels below are stated FOR
    xauusd: Channel
    usd_irt: Channel
    local_premium: Channel
    evidence: str = EVIDENCE_HYPOTHESIS


def _no_channel(reason: str) -> Channel:
    return Channel(NONE, NO_PRIOR, reason)


_NO_GLOBAL_REACH = "an Iran-specific measure has no channel into the global dollar gold price"
_STICKY_LOCAL = (
    "Tehran quotes adjust to a global move with a lag, so the measured premium "
    "moves the other way until they catch up"
)


CATEGORIES: dict[str, Category] = {
    "us_monetary_policy": Category(
        code="us_monetary_policy",
        label="US monetary policy",
        prior_polarity="hawkish surprise (policy tighter than expected)",
        xauusd=Channel(
            DOWN, MODERATE,
            "higher expected real rates raise the carrying cost of a zero-yield asset",
        ),
        usd_irt=Channel(
            AMBIGUOUS, WEAK,
            "the free-market toman rate is driven by sanctions and domestic liquidity; "
            "global dollar strength reaches it only indirectly",
        ),
        local_premium=Channel(UP, WEAK, _STICKY_LOCAL),
    ),
    "us_inflation": Category(
        code="us_inflation",
        label="US inflation data",
        prior_polarity="upside surprise (prices hotter than expected)",
        xauusd=Channel(
            AMBIGUOUS, WEAK,
            "inflation-hedge demand pushes up while the implied policy tightening pushes "
            "real yields up and gold down; the net sign has historically flipped by regime",
        ),
        usd_irt=Channel(AMBIGUOUS, WEAK, "no direct channel; only via global dollar strength"),
        local_premium=Channel(
            AMBIGUOUS, WEAK,
            "inherits the ambiguity of the global leg it would lag",
        ),
    ),
    "us_labor": Category(
        code="us_labor",
        label="US labor data",
        prior_polarity="upside surprise (labor market stronger than expected)",
        xauusd=Channel(
            DOWN, WEAK,
            "a strong labor market supports tighter policy and higher real yields",
        ),
        usd_irt=_no_channel("no plausible channel into the toman rate"),
        local_premium=Channel(UP, WEAK, _STICKY_LOCAL),
    ),
    "yields": Category(
        code="yields",
        label="Sovereign yields (US nominal/real)",
        prior_polarity="yields rising",
        xauusd=Channel(
            DOWN, MODERATE,
            "the opportunity cost of holding gold is the real yield it forgoes",
        ),
        usd_irt=Channel(AMBIGUOUS, WEAK, "reaches the toman only via the global dollar"),
        local_premium=Channel(UP, WEAK, _STICKY_LOCAL),
    ),
    "dollar_strength": Category(
        code="dollar_strength",
        label="Broad dollar strength",
        prior_polarity="dollar appreciating (DXY up)",
        xauusd=Channel(
            DOWN, MODERATE,
            "gold is quoted in dollars: a stronger dollar lowers the dollar price at "
            "unchanged value in other currencies",
        ),
        usd_irt=Channel(
            UP, WEAK,
            "imported-goods pricing and expectations pass some global dollar strength "
            "into the toman rate, but domestic factors dominate",
        ),
        local_premium=Channel(UP, WEAK, _STICKY_LOCAL),
    ),
    "global_risk_off": Category(
        code="global_risk_off",
        label="Global risk-off",
        prior_polarity="risk aversion intensifying",
        xauusd=Channel(UP, MODERATE, "safe-haven bid"),
        usd_irt=Channel(
            UP, WEAK,
            "hard-currency demand rises everywhere; in Iran that shows up as dollarization",
        ),
        local_premium=Channel(
            UP, MODERATE,
            "domestic demand for physical gold as a store of value outruns the theoretical price",
        ),
    ),
    "geopolitical_escalation": Category(
        code="geopolitical_escalation",
        label="Geopolitical escalation",
        prior_polarity="escalation (conflict, strikes, military mobilisation)",
        xauusd=Channel(UP, MODERATE, "global haven bid"),
        usd_irt=Channel(UP, MODERATE, "capital flight into hard currency"),
        local_premium=Channel(
            UP, MODERATE,
            "physical demand spikes and dealers widen quotes, so Tehran overshoots theory",
        ),
    ),
    "geopolitical_deescalation": Category(
        code="geopolitical_deescalation",
        label="Geopolitical de-escalation",
        prior_polarity="de-escalation (ceasefire, talks, tension easing)",
        xauusd=Channel(DOWN, WEAK, "the haven bid unwinds"),
        usd_irt=Channel(DOWN, MODERATE, "the free rate has historically retraced on easing"),
        local_premium=Channel(DOWN, MODERATE, "the panic premium unwinds faster than it built"),
    ),
    "sanctions_escalation": Category(
        code="sanctions_escalation",
        label="Sanctions escalation",
        prior_polarity="new or tightened sanctions on Iran",
        xauusd=_no_channel(_NO_GLOBAL_REACH),
        usd_irt=Channel(UP, MODERATE, "restricted hard-currency supply and repatriation"),
        local_premium=Channel(
            UP, MODERATE,
            "bullion import friction raises the local scarcity premium above theory",
        ),
    ),
    "sanctions_relief": Category(
        code="sanctions_relief",
        label="Sanctions relief",
        prior_polarity="sanctions eased, waivers granted, funds released",
        xauusd=_no_channel(_NO_GLOBAL_REACH),
        usd_irt=Channel(DOWN, MODERATE, "hard-currency supply and expectations both improve"),
        local_premium=Channel(DOWN, MODERATE, "import friction eases, so the scarcity premium compresses"),
    ),
    "iran_fx_policy": Category(
        code="iran_fx_policy",
        label="Iranian FX policy",
        prior_polarity="restrictive (new rate corridors, trading limits, allocation rules)",
        xauusd=_no_channel(_NO_GLOBAL_REACH),
        usd_irt=Channel(
            AMBIGUOUS, MODERATE,
            "administrative suppression lowers the reported rate while usually widening "
            "the free-market gap it is measured against",
        ),
        local_premium=Channel(
            UP, WEAK,
            "when the FX channel is restricted, gold becomes the substitute hedge",
        ),
    ),
    "iran_monetary_policy": Category(
        code="iran_monetary_policy",
        label="Iranian monetary policy",
        prior_polarity="expansionary (rate cuts, liquidity growth, deficit monetisation)",
        xauusd=_no_channel(_NO_GLOBAL_REACH),
        usd_irt=Channel(
            UP, MODERATE,
            "money growth is the classic driver of the toman's depreciation",
        ),
        local_premium=Channel(UP, WEAK, "real-asset demand rises with expected inflation"),
    ),
    "domestic_gold_regulation": Category(
        code="domestic_gold_regulation",
        label="Domestic gold-market regulation",
        prior_polarity="restrictive (taxes, transaction limits, exchange rules, VAT changes)",
        xauusd=_no_channel(_NO_GLOBAL_REACH),
        usd_irt=Channel(
            AMBIGUOUS, WEAK,
            "restricting gold can push hedging demand into dollars instead",
        ),
        local_premium=Channel(
            AMBIGUOUS, MODERATE,
            "a transaction tax raises retail prices while an anti-speculation rule "
            "suppresses them; the sign depends on the instrument, and the premium is "
            "measured against a theory that ignores both",
        ),
    ),
    "exchange_disruption": Category(
        code="exchange_disruption",
        label="Exchange or platform disruption",
        prior_polarity="trading halted or degraded (gold exchange, dealer platform, FX market)",
        xauusd=_no_channel(_NO_GLOBAL_REACH),
        usd_irt=Channel(AMBIGUOUS, WEAK, "quotes thin out; the mid can drift either way"),
        local_premium=Channel(
            AMBIGUOUS, MODERATE,
            "arbitrage is impaired so the observed price detaches from theory — the "
            "MAGNITUDE of the premium should rise, its sign is genuinely uncertain",
        ),
    ),
    "energy_shock": Category(
        code="energy_shock",
        label="Energy shock",
        prior_polarity="energy prices spiking",
        xauusd=Channel(UP, WEAK, "inflation-hedge demand"),
        usd_irt=Channel(
            AMBIGUOUS, WEAK,
            "Iran is an oil exporter, so a spike improves export receipts, yet under "
            "sanctions those receipts are only partly realisable",
        ),
        local_premium=Channel(AMBIGUOUS, WEAK, "inherits the ambiguity of both legs"),
    ),
    "data_outage": Category(
        code="data_outage",
        label="Data outage (operational marker)",
        prior_polarity="a source or feed stopped publishing",
        xauusd=_no_channel("not a market event"),
        usd_irt=_no_channel("not a market event"),
        local_premium=_no_channel("not a market event"),
    ),
}

CATEGORY_CODES: tuple[str, ...] = tuple(CATEGORIES)
CHANNEL_NAMES: tuple[str, ...] = ("xauusd", "usd_irt", "local_premium")


def get(code: str) -> Optional[Category]:
    """Taxonomy entry for ``code``, or None when unknown."""
    return CATEGORIES.get(code)


def is_known(code: str) -> bool:
    """True when ``code`` is a taxonomy category (validates stored events)."""
    return code in CATEGORIES


def channels(code: str) -> dict[str, Channel]:
    """``{channel_name: Channel}`` for ``code`` (empty dict when unknown)."""
    category = CATEGORIES.get(code)
    if category is None:
        return {}
    return {name: getattr(category, name) for name in CHANNEL_NAMES}


def opposite(direction: str) -> str:
    """Mirror a direction for an event of the polarity opposite the prior's.

    ``none`` and ``ambiguous`` have no mirror — a missing mechanism stays
    missing, and an unknown sign stays unknown.
    """
    if direction == UP:
        return DOWN
    if direction == DOWN:
        return UP
    return direction
