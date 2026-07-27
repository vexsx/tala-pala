"""Versioned rule classifier: category, then one hypothesis PER CHANNEL.

WHY rules rather than a model: there is no labelled corpus and no event study on
this system's own data, so anything learned would be fitted to noise and would
arrive with no explanation.  A rule states an economic argument in advance, in a
form a reader can disagree with, and its id and version are stored on every row
it produces — so when an event study finally exists it can refute a NAMED rule
instead of a black box.  Nothing here reaches a model: ``NEWS_ML_ENABLED``
gates that, and every score below is written with ``hypothesis_only=True``.

WHY five channels instead of one sentiment number: a single number cannot say
that a hawkish Fed surprise pushes the dollar gold price DOWN while barely
touching the toman, and that an Iran sanctions escalation does the opposite —
it leaves the dollar gold price alone and pushes the toman and the Tehran
premium UP.  Averaging those into "negative news" destroys exactly the
information this system exists to use.  The channels and their sign
conventions:

* ``xau_usd``          + = upward pressure on the dollar gold price;
* ``usd_irt``          + = the toman weakens (more toman per dollar);
* ``local_premium``    + = Tehran's quote richer than its theoretical value;
* ``liquidity_spread`` + = spreads widen / execution deteriorates.  This is a
  COST channel, not a direction: it is the only one where both a rally and a
  crash score positive;
* ``combined_ir_gold`` + = upward pressure on the toman price of local gold.

WHY ``combined_ir_gold`` is a composition and not an average: the toman price
of local gold is, by identity, the dollar gold price times the exchange rate
times one plus the premium.  In logs those three legs ADD, so the composite is
their weighted sum — a price identity, not a mood summary.  A rule may override
the composition when the identity itself stops holding (a halted venue has no
simultaneous legs to compose).

Rules carry their own version separate from :data:`CLASSIFIER_VERSION` so a
single rule can be revised — and re-examined against the rows it produced —
without invalidating the attribution of every other rule.  The mechanism text
behind a rule lives on the rule object, reachable from any stored row by
``(rule_id, rule_version)``; the persisted evidence lists stay plain term lists.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

from . import entities
from .entities import EntityMatch

# Bumped when the rule SET changes (a rule added, removed, or re-scoped).
CLASSIFIER_VERSION = "rules-1.0.0"
CLASSIFIER_KIND = "deterministic"

CHANNEL_XAU = "xau_usd"
CHANNEL_USD_IRT = "usd_irt"
CHANNEL_PREMIUM = "local_premium"
CHANNEL_LIQUIDITY = "liquidity_spread"
CHANNEL_COMBINED = "combined_ir_gold"
# Order is fixed so a stored set of hypotheses is diffable across runs.
CHANNELS: tuple[str, ...] = (
    CHANNEL_XAU, CHANNEL_USD_IRT, CHANNEL_PREMIUM, CHANNEL_LIQUIDITY,
    CHANNEL_COMBINED,
)
PRICE_LEGS: tuple[str, ...] = (CHANNEL_XAU, CHANNEL_USD_IRT, CHANNEL_PREMIUM)

UNCLASSIFIED = "unclassified"
CATEGORIES: tuple[str, ...] = (
    "gold_market", "federal_reserve", "inflation", "us_labor", "us_yields",
    "dollar_strength", "safe_haven", "sanctions_escalation", "sanctions_relief",
    "iran_negotiations", "iran_fx_policy", "iran_monetary_policy",
    "domestic_gold_regulation", "geopolitical_escalation",
    "geopolitical_deescalation", "oil_energy_shock", "exchange_disruption",
    "iran_political_risk", UNCLASSIFIED,
)

# Where a category's priors were argued before this classifier existed.  The
# taxonomy states its channels for ONE polarity per category, so several rules
# here (a dovish Fed, a de-escalation) are the mirror of the entry they map to;
# the polarity is part of the rule id.  A None means the taxonomy has no entry
# and the rule below is the first written statement of the mechanism.
TAXONOMY_CATEGORY = {
    "federal_reserve": "us_monetary_policy",
    "inflation": "us_inflation",
    "us_labor": "us_labor",
    "us_yields": "yields",
    "dollar_strength": "dollar_strength",
    "safe_haven": "global_risk_off",
    "sanctions_escalation": "sanctions_escalation",
    "sanctions_relief": "sanctions_relief",
    "iran_fx_policy": "iran_fx_policy",
    "iran_monetary_policy": "iran_monetary_policy",
    "domestic_gold_regulation": "domestic_gold_regulation",
    "geopolitical_escalation": "geopolitical_escalation",
    "geopolitical_deescalation": "geopolitical_deescalation",
    "oil_energy_shock": "energy_shock",
    "exchange_disruption": "exchange_disruption",
    "iran_negotiations": "geopolitical_deescalation",
    "gold_market": None,
    "iran_political_risk": None,
}

# Weights of the three legs of the local-gold price identity.  They are not a
# sentiment blend: they are how much of a typical move in the toman gold price
# each leg has accounted for in the Iranian regime this system observes — the
# FX leg dominates, the dollar gold leg is second, and the premium is the
# smallest and the most mean-reverting.  They sum to 1 so a composite of
# in-range legs stays in range.  Liquidity is deliberately absent: a wider
# spread is a cost of transacting, not a level of the price.
COMBINED_WEIGHTS: Mapping[str, float] = {
    CHANNEL_XAU: 0.30,
    CHANNEL_USD_IRT: 0.55,
    CHANNEL_PREMIUM: 0.15,
}
# A composite is never better known than its least certain leg, and composing
# adds a step of its own.
COMBINED_DIRECTNESS_DISCOUNT = 0.85

# Confidence budget.  The ceiling is well below 1 on purpose: a rule that fired
# on a keyword has not verified anything, and a UI that shows 0.95 next to a
# keyword match is lying about what the system knows.
MAX_RULE_CONFIDENCE = 0.85
MIN_RULE_CONFIDENCE = 0.05
# Extra matched terms are weak corroboration — the same story worded twice, not
# a second source — so the bonus is small and capped.
SUPPORT_BONUS_PER_TERM = 0.03
MAX_SUPPORT_BONUS = 0.09
# A "widely expected" style qualifier means the move was already priced.
WEAKEN_PENALTY = 0.12
# Both directions of one mechanism fired: the text argues with itself.
OPPOSED_RULE_PENALTY = 0.20


@dataclass(frozen=True)
class ChannelPrior:
    """A rule's hypothesised effect on one channel.

    ``directness`` scales the rule's confidence for this channel: 1.0 is the
    mechanism the rule is actually about, and a low value marks a channel the
    shock reaches only through a chain of other markets (or, at the bottom, one
    the taxonomy argues it cannot reach at all).
    """

    score: float          # bounded [-1, 1]
    directness: float     # (0, 1]
    mechanism: str


@dataclass(frozen=True)
class Rule:
    """One deterministic rule: what must appear, and what it would imply."""

    rule_id: str
    category: str
    channels: Mapping[str, ChannelPrior]
    base_confidence: float
    horizon: str
    decay_hours: float
    # All groups must match; within a group any one member is enough.
    require_terms: tuple[tuple[str, ...], ...] = ()
    require_entities: tuple[tuple[str, ...], ...] = ()
    # Present-tense qualifiers that make the move less of a surprise.
    weaken_terms: tuple[str, ...] = ()
    # Set when the price identity behind COMBINED_WEIGHTS does not hold.
    combined: Optional[ChannelPrior] = None
    version: str = "1.0.0"


@dataclass(frozen=True)
class Hypothesis:
    """One channel's bounded, evidenced, explicitly-unverified impact claim."""

    channel: str
    score: float
    confidence: float
    rule_id: str
    rule_version: str
    supporting_evidence: tuple[str, ...]
    contradicting_evidence: tuple[str, ...]
    expected_horizon: str
    decay_hours: float
    mechanism: str
    hypothesis_only: bool = True


@dataclass(frozen=True)
class Classification:
    """A category assignment with the evidence that produced it."""

    category: str
    rule_id: str
    rule_version: str
    confidence: float
    supporting_terms: tuple[str, ...]
    contradicting_terms: tuple[str, ...]


@dataclass(frozen=True)
class EventClassification:
    """Primary category, any secondary categories, and the channel hypotheses.

    Only the primary rule produces hypotheses: ``news_impact_hypotheses`` is
    unique per (event, classifier_version, channel), so a channel gets exactly
    one claim and it must be attributable to one rule.  Secondary categories are
    still recorded — they are how a later study finds the events where two
    mechanisms fired at once.
    """

    classifier_version: str
    primary: Classification
    secondary: tuple[Classification, ...]
    hypotheses: tuple[Hypothesis, ...]
    entity_codes: tuple[str, ...]

    @property
    def category(self) -> str:
        return self.primary.category

    @property
    def is_classified(self) -> bool:
        return self.primary.category != UNCLASSIFIED

    def hypothesis(self, channel: str) -> Optional[Hypothesis]:
        for item in self.hypotheses:
            if item.channel == channel:
                return item
        return None


# --- shared phrase groups ----------------------------------------------------

_HAWKISH = (
    "rate hike", "raise rates", "raising rates", "hike rates", "hiked rates",
    "tightening", "tighter policy", "hawkish", "higher for longer",
    "restrictive stance", "افزایش نرخ بهره", "سیاست انقباضی",
)
_DOVISH = (
    "rate cut", "cut rates", "cutting rates", "lower rates", "rate cuts",
    "dovish", "easing cycle", "easing policy", "accommodative",
    "کاهش نرخ بهره", "سیاست انبساطی",
)
_HOTTER = (
    "hotter than expected", "above expectations", "above forecast",
    "accelerated", "acceleration", "rose more than expected", "beat forecasts",
    "بیش از انتظار",
)
_COOLER = (
    "cooler than expected", "below expectations", "below forecast", "eased",
    "slowed", "decelerated", "missed forecasts", "کمتر از انتظار",
)
_RISING = ("rose", "rise", "rises", "rising", "surge", "surged", "jumped",
           "climbed", "higher", "spike", "spiked", "افزایش", "جهش")
_FALLING = ("fell", "fall", "falls", "falling", "dropped", "slid", "slumped",
            "lower", "plunged", "کاهش", "افت")
_PRICED_IN = (
    "as expected", "widely expected", "in line with expectations",
    "no change", "left unchanged", "kept unchanged", "طبق انتظار",
)
_SANCTIONS = (
    "sanctions", "sanction", "embargo", "asset freeze", "blacklist",
    "designated", "designation", "تحریم", "تحریم‌ها",
)
_SANCTION_TIGHTEN = (
    "new sanctions", "imposed", "impose", "imposes", "tightened", "tighten",
    "expanded", "added", "blacklisted", "snapback", "triggered", "reimposed",
    "اعمال", "تشدید", "مکانیسم ماشه",
)
_SANCTION_EASE = (
    "lifted", "lift", "lifts", "eased", "ease", "waiver", "waivers",
    "suspended sanctions", "unfrozen", "unfreeze", "released funds",
    "delisted", "removed from the list", "رفع تحریم", "لغو تحریم", "آزادسازی",
)
_TALKS = (
    "talks", "negotiations", "negotiation", "diplomacy", "dialogue",
    "nuclear deal", "agreement", "مذاکرات", "مذاکره", "توافق",
)
_TALKS_GOOD = (
    "progress", "breakthrough", "resumed", "resume", "agreed", "deal reached",
    "framework", "constructive", "پیشرفت", "از سرگیری", "توافق شد",
)
_TALKS_BAD = (
    "collapsed", "collapse", "stalled", "suspended", "broke down", "breakdown",
    "walked out", "deadlock", "failed", "شکست", "متوقف", "بن‌بست",
)
_CONFLICT = (
    "airstrike", "air strike", "strikes", "attack", "attacked", "missile",
    "drone attack", "war", "military operation", "mobilisation",
    "mobilization", "assassination", "retaliation", "حمله", "موشک", "جنگ",
    "عملیات نظامی",
)
_CALMING = (
    "ceasefire", "truce", "de escalation", "deescalation", "withdrawal",
    "peace deal", "tensions eased", "stand down", "آتش‌بس", "کاهش تنش",
)
_RISK_OFF = (
    "risk off", "safe haven", "haven demand", "flight to safety", "selloff",
    "sell off", "market rout", "volatility spike", "panic", "فرار سرمایه",
    "تقاضای امن",
)
_FX_RESTRICTIVE = (
    "currency controls", "capital controls", "fx restrictions", "trading limit",
    "trading limits", "rate corridor", "banned", "ban on", "quota",
    "mandatory sale", "ممنوع", "سقف معاملاتی", "محدودیت ارزی", "سهمیه ارزی",
)
_FX_LIBERALISING = (
    "unified exchange rate", "unification", "removed the limit", "lifted the ban",
    "liberalised", "liberalized", "free floating", "آزادسازی ارزی",
    "حذف محدودیت", "یکسان‌سازی نرخ ارز",
)
_MONEY_GROWTH = (
    "money supply", "liquidity growth", "monetisation", "monetization",
    "printing money", "budget deficit", "نقدینگی", "کسری بودجه", "پایه پولی",
)
_GOLD_RULES = (
    "value added tax", "vat", "tax on gold", "transaction limit", "new rules",
    "regulation", "regulations", "levy", "duty", "مالیات", "مقررات",
    "آیین‌نامه", "دستورالعمل",
)
_HALT = (
    "halted", "halt", "suspended trading", "trading suspended", "outage",
    "system failure", "closed the market", "shut down", "disruption",
    "توقف معاملات", "تعطیلی بازار", "اختلال",
)
_UNREST = (
    "protests", "protest", "unrest", "strike action", "crackdown",
    "instability", "succession", "resigned", "impeachment", "اعتراضات",
    "ناآرامی", "استعفا", "بی‌ثباتی",
)
_CB_BUYING = (
    "central bank buying", "central banks bought", "official sector demand",
    "reserves increased", "added to reserves", "خرید طلا توسط بانک‌های مرکزی",
)
_ETF_OUTFLOW = (
    "etf outflows", "outflows", "holdings fell", "liquidation",
    "redemptions", "خروج سرمایه",
)
_ETF_INFLOW = ("etf inflows", "inflows", "holdings rose", "accumulation",
               "ورود سرمایه")

_E_FED = (("federal_reserve", "fed_funds_rate"),)
_E_IRAN = (("iran", "cbi", "irgc", "irr"),)
_E_GOLD = (("gold", "gold_coin"),)

_NO_GLOBAL_REACH = (
    "an Iran-specific measure has no channel into the global dollar gold price"
)
_STICKY_LOCAL = (
    "Tehran quotes lag a global move, so the measured premium moves the other "
    "way until they catch up"
)


def _prior(score: float, directness: float, mechanism: str) -> ChannelPrior:
    return ChannelPrior(score=score, directness=directness, mechanism=mechanism)


# A channel the shock provably cannot reach: scored zero, and marked with a
# directness low enough that the UI shows it as "no mechanism", not "neutral".
def _unreachable(reason: str) -> ChannelPrior:
    return ChannelPrior(0.0, 0.10, reason)


# A channel where a mechanism exists but its SIGN is genuinely unknown.  Scored
# zero with low directness for the same reason: pretending to a sign we do not
# have is worse than admitting the magnitude is all we can claim.
def _sign_unknown(reason: str) -> ChannelPrior:
    return ChannelPrior(0.0, 0.15, reason)


# --- the rules ---------------------------------------------------------------
#
# Each entry states, in its comment, WHY the channel signs are what they are.
# The recurring asymmetry to keep in mind: a US-macro shock lands almost
# entirely on xau_usd and reaches the toman only through the broad dollar,
# whereas an Iran-specific shock lands on usd_irt and the premium and does not
# touch xau_usd at all.

RULES: tuple[Rule, ...] = (
    # Higher expected real rates raise the carrying cost of a zero-yield asset,
    # so the dollar gold price falls.  The toman is driven by sanctions and
    # domestic liquidity, not by the FOMC, so usd_irt is left near zero rather
    # than given a token positive sign — that near-zero IS the claim.  The
    # premium rises because Tehran quotes lag the global leg down.
    Rule(
        rule_id="fed_hawkish",
        category="federal_reserve",
        require_entities=_E_FED,
        require_terms=(_HAWKISH,),
        weaken_terms=_PRICED_IN,
        base_confidence=0.65,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(-0.60, 0.90, "higher expected real rates raise the carrying cost of gold"),
            CHANNEL_USD_IRT: _prior(0.05, 0.35, "the free-market toman responds to sanctions and domestic liquidity, not to the FOMC; only the broad dollar carries any of this across"),
            CHANNEL_PREMIUM: _prior(0.20, 0.40, _STICKY_LOCAL),
            CHANNEL_LIQUIDITY: _prior(0.10, 0.30, "a repricing session widens dealer spreads while quotes are stale"),
        },
    ),
    # The mirror of fed_hawkish, and deliberately not its exact negative: an
    # easing surprise lifts gold roughly as much as a tightening surprise sinks
    # it, but the local premium unwinds a little less sharply than it builds.
    Rule(
        rule_id="fed_dovish",
        category="federal_reserve",
        require_entities=_E_FED,
        require_terms=(_DOVISH,),
        weaken_terms=_PRICED_IN,
        base_confidence=0.65,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(0.55, 0.90, "lower expected real rates cut the carrying cost of gold"),
            CHANNEL_USD_IRT: _prior(-0.05, 0.35, "same asymmetry as the hawkish case: the toman barely hears the FOMC"),
            CHANNEL_PREMIUM: _prior(-0.15, 0.40, "Tehran lags the global leg up, so the measured premium compresses"),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.25, "any repricing session widens spreads, whichever way it goes"),
        },
    ),
    # Inflation is the one US category whose gold sign has genuinely flipped by
    # regime: hedge demand pushes up, the implied tightening pushes real yields
    # up and gold down.  Small magnitude and low directness encode that, rather
    # than a confident number nobody can defend.
    Rule(
        rule_id="us_inflation_hot",
        category="inflation",
        require_entities=(("cpi", "pce", "ppi"),),
        require_terms=(_HOTTER,),
        weaken_terms=_PRICED_IN,
        base_confidence=0.50,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(-0.15, 0.35, "the implied policy tightening usually outweighs hedge demand, but the sign has flipped by regime"),
            CHANNEL_USD_IRT: _prior(0.0, 0.20, "no direct channel; reaches the toman only via the broad dollar"),
            CHANNEL_PREMIUM: _prior(0.10, 0.30, _STICKY_LOCAL),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "release-window spread widening"),
        },
    ),
    Rule(
        rule_id="us_inflation_cool",
        category="inflation",
        require_entities=(("cpi", "pce", "ppi"),),
        require_terms=(_COOLER,),
        weaken_terms=_PRICED_IN,
        base_confidence=0.50,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(0.15, 0.35, "a softer print pulls expected policy easier; hedge demand falls at the same time"),
            CHANNEL_USD_IRT: _prior(0.0, 0.20, "no direct channel"),
            CHANNEL_PREMIUM: _prior(-0.08, 0.30, "mirror of the sticky-quote lag"),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "release-window spread widening"),
        },
    ),
    # A strong labour market supports tighter policy and higher real yields.
    # The taxonomy states NO channel into the toman for this one, so usd_irt is
    # marked unreachable rather than nudged.
    Rule(
        rule_id="us_labor_strong",
        category="us_labor",
        require_entities=(("nonfarm_payrolls", "unemployment_rate"),),
        require_terms=(_HOTTER,),
        weaken_terms=_PRICED_IN,
        base_confidence=0.55,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(-0.35, 0.60, "a strong labour market supports tighter policy and higher real yields"),
            CHANNEL_USD_IRT: _unreachable("no plausible channel from US payrolls into the free-market toman rate"),
            CHANNEL_PREMIUM: _prior(0.15, 0.35, _STICKY_LOCAL),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "release-window spread widening"),
        },
    ),
    Rule(
        rule_id="us_labor_weak",
        category="us_labor",
        require_entities=(("nonfarm_payrolls", "unemployment_rate"),),
        require_terms=(_COOLER,),
        weaken_terms=_PRICED_IN,
        base_confidence=0.55,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(0.35, 0.60, "a weakening labour market pulls expected policy easier"),
            CHANNEL_USD_IRT: _unreachable("no plausible channel into the free-market toman rate"),
            CHANNEL_PREMIUM: _prior(-0.12, 0.35, "mirror of the sticky-quote lag"),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "release-window spread widening"),
        },
    ),
    # The opportunity cost of holding gold IS the real yield it forgoes, which
    # makes this the cleanest US-macro mechanism in the set — hence the highest
    # directness on xau_usd of any macro rule.
    Rule(
        rule_id="us_yields_up",
        category="us_yields",
        require_entities=(("treasury_yield_10y",),),
        require_terms=(_RISING,),
        base_confidence=0.60,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(-0.50, 0.80, "the opportunity cost of holding gold is the real yield it forgoes"),
            CHANNEL_USD_IRT: _prior(0.05, 0.25, "reaches the toman only through the broad dollar"),
            CHANNEL_PREMIUM: _prior(0.15, 0.35, _STICKY_LOCAL),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "repricing widens spreads"),
        },
    ),
    Rule(
        rule_id="us_yields_down",
        category="us_yields",
        require_entities=(("treasury_yield_10y",),),
        require_terms=(_FALLING,),
        base_confidence=0.60,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(0.45, 0.80, "a lower real yield lowers the cost of carrying a zero-yield asset"),
            CHANNEL_USD_IRT: _prior(-0.05, 0.25, "reaches the toman only through the broad dollar"),
            CHANNEL_PREMIUM: _prior(-0.12, 0.35, "mirror of the sticky-quote lag"),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "repricing widens spreads"),
        },
    ),
    # Gold is quoted in dollars, so this one is close to an accounting effect.
    # It is also the ONLY US-macro rule with a defensible positive usd_irt sign:
    # imported-goods pricing passes some broad dollar strength into the toman.
    Rule(
        rule_id="dollar_strength_up",
        category="dollar_strength",
        require_entities=(("dxy", "usd"),),
        require_terms=(("strengthened", "strengthens", "rally", "rallied",
                        "firmer", "stronger dollar", "dollar index rose",
                        "تقویت دلار"),),
        base_confidence=0.60,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(-0.45, 0.80, "gold is quoted in dollars: a stronger dollar lowers the dollar price at unchanged value elsewhere"),
            CHANNEL_USD_IRT: _prior(0.15, 0.40, "imported-goods pricing and expectations pass part of broad dollar strength into the toman, though domestic factors dominate"),
            CHANNEL_PREMIUM: _prior(0.15, 0.35, _STICKY_LOCAL),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "repricing widens spreads"),
        },
    ),
    Rule(
        rule_id="dollar_weakness",
        category="dollar_strength",
        require_entities=(("dxy", "usd"),),
        require_terms=(("weakened", "weakens", "slumped", "weaker dollar",
                        "dollar index fell", "تضعیف دلار"),),
        base_confidence=0.60,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(0.40, 0.80, "the quotation effect in reverse"),
            CHANNEL_USD_IRT: _prior(-0.10, 0.40, "partial pass-through, damped by domestic factors"),
            CHANNEL_PREMIUM: _prior(-0.12, 0.35, "mirror of the sticky-quote lag"),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "repricing widens spreads"),
        },
    ),
    # Risk-off is the one global category that reaches every channel at once:
    # the haven bid lifts gold, dollarization lifts the toman rate, and Iranian
    # physical demand lifts the premium above theory.
    Rule(
        rule_id="safe_haven_bid",
        category="safe_haven",
        require_terms=(_RISK_OFF,),
        base_confidence=0.55,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(0.50, 0.70, "safe-haven bid"),
            CHANNEL_USD_IRT: _prior(0.20, 0.40, "hard-currency demand rises everywhere; in Iran it shows up as dollarization"),
            CHANNEL_PREMIUM: _prior(0.35, 0.50, "domestic demand for physical gold outruns the theoretical price"),
            CHANNEL_LIQUIDITY: _prior(0.30, 0.45, "dealers widen quotes into a one-way flow"),
        },
    ),
    # The canonical asymmetry: this rule must NOT move xau_usd (Washington
    # sanctioning Tehran does not change the global gold price) while pushing
    # usd_irt and the premium up hard — restricted hard-currency supply and
    # bullion import friction respectively.
    Rule(
        rule_id="sanctions_escalation",
        category="sanctions_escalation",
        require_entities=_E_IRAN,
        require_terms=(_SANCTIONS, _SANCTION_TIGHTEN),
        base_confidence=0.70,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _unreachable(_NO_GLOBAL_REACH),
            CHANNEL_USD_IRT: _prior(0.65, 0.90, "restricted hard-currency supply and blocked repatriation"),
            CHANNEL_PREMIUM: _prior(0.50, 0.80, "bullion import friction raises the local scarcity premium above theory"),
            CHANNEL_LIQUIDITY: _prior(0.40, 0.60, "dealers widen quotes when replacement cost becomes uncertain"),
        },
    ),
    Rule(
        rule_id="sanctions_relief",
        category="sanctions_relief",
        require_entities=_E_IRAN,
        require_terms=(_SANCTIONS, _SANCTION_EASE),
        base_confidence=0.65,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _unreachable(_NO_GLOBAL_REACH),
            CHANNEL_USD_IRT: _prior(-0.55, 0.80, "hard-currency supply and expectations both improve"),
            CHANNEL_PREMIUM: _prior(-0.40, 0.70, "import friction eases, so the scarcity premium compresses"),
            CHANNEL_LIQUIDITY: _prior(-0.25, 0.50, "replacement cost becomes calculable again and quotes tighten"),
        },
    ),
    # Negotiations are a category, not a direction: the same category holds
    # both of the rules below, which is exactly why direction is stored per
    # rule and not per category.
    Rule(
        rule_id="iran_negotiations_progress",
        category="iran_negotiations",
        require_entities=_E_IRAN,
        require_terms=(_TALKS, _TALKS_GOOD),
        base_confidence=0.55,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _prior(-0.10, 0.30, "a marginal unwind of the regional risk premium in global gold"),
            CHANNEL_USD_IRT: _prior(-0.35, 0.60, "the free rate has historically retraced on credible diplomatic progress"),
            CHANNEL_PREMIUM: _prior(-0.30, 0.55, "the panic premium unwinds faster than it built"),
            CHANNEL_LIQUIDITY: _prior(-0.15, 0.40, "two-way flow returns and quotes tighten"),
        },
    ),
    Rule(
        rule_id="iran_negotiations_breakdown",
        category="iran_negotiations",
        require_entities=_E_IRAN,
        require_terms=(_TALKS, _TALKS_BAD),
        base_confidence=0.55,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _prior(0.10, 0.30, "a marginal regional risk premium in global gold"),
            CHANNEL_USD_IRT: _prior(0.45, 0.65, "the expectation channel reverses: no relief path means no supply improvement"),
            CHANNEL_PREMIUM: _prior(0.35, 0.60, "hedging demand returns to physical gold"),
            CHANNEL_LIQUIDITY: _prior(0.25, 0.45, "one-way flow widens quotes"),
        },
    ),
    # Administrative suppression lowers the REPORTED rate while usually widening
    # the free-market gap it is measured against, so usd_irt gets a small
    # positive with low directness rather than a confident sign.
    Rule(
        rule_id="iran_fx_policy_restrictive",
        category="iran_fx_policy",
        require_entities=(("cbi", "iran_free_fx_market", "gold_center", "irr"),),
        require_terms=(_FX_RESTRICTIVE,),
        base_confidence=0.55,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _unreachable(_NO_GLOBAL_REACH),
            CHANNEL_USD_IRT: _prior(0.15, 0.30, "the administered rate is suppressed while the free-market gap widens; which of the two a quote source reports decides the observed sign"),
            CHANNEL_PREMIUM: _prior(0.30, 0.50, "when the FX channel is restricted, gold becomes the substitute hedge"),
            CHANNEL_LIQUIDITY: _prior(0.35, 0.55, "rationed access thins the market and widens dealer spreads"),
        },
    ),
    Rule(
        rule_id="iran_fx_policy_liberalising",
        category="iran_fx_policy",
        require_entities=(("cbi", "iran_free_fx_market", "gold_center", "irr"),),
        require_terms=(_FX_LIBERALISING,),
        base_confidence=0.50,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _unreachable(_NO_GLOBAL_REACH),
            CHANNEL_USD_IRT: _prior(-0.10, 0.25, "unification can raise the administered rate while lowering the free one; the observed sign depends on which is quoted"),
            CHANNEL_PREMIUM: _prior(-0.20, 0.45, "a working FX channel removes gold's role as the substitute hedge"),
            CHANNEL_LIQUIDITY: _prior(-0.25, 0.50, "restored access deepens the market"),
        },
    ),
    # Money growth is the classic driver of the toman's depreciation, and it is
    # a slow burn — hence the weekly horizon and the long decay.
    Rule(
        rule_id="iran_monetary_expansion",
        category="iran_monetary_policy",
        require_entities=(("cbi", "iran_liquidity", "irr"),),
        require_terms=(_MONEY_GROWTH, _RISING),
        base_confidence=0.55,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _unreachable(_NO_GLOBAL_REACH),
            CHANNEL_USD_IRT: _prior(0.45, 0.60, "money growth is the classic driver of the toman's depreciation"),
            CHANNEL_PREMIUM: _prior(0.25, 0.45, "real-asset demand rises with expected inflation"),
            CHANNEL_LIQUIDITY: _prior(0.10, 0.30, "more nominal turnover, only mildly wider spreads"),
        },
    ),
    Rule(
        rule_id="iran_monetary_tightening",
        category="iran_monetary_policy",
        require_entities=(("cbi", "iran_liquidity", "irr"),),
        require_terms=(_MONEY_GROWTH, _FALLING),
        base_confidence=0.50,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _unreachable(_NO_GLOBAL_REACH),
            CHANNEL_USD_IRT: _prior(-0.25, 0.50, "slower money growth slows the depreciation trend, but does not reverse the level"),
            CHANNEL_PREMIUM: _prior(-0.15, 0.40, "expected-inflation demand for real assets cools"),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.25, "tighter money thins turnover"),
        },
    ),
    # The honest case where the MAGNITUDE is knowable and the SIGN is not: a
    # transaction tax raises retail prices while an anti-speculation rule
    # suppresses them, and the premium is measured against a theory that models
    # neither.  Only the liquidity channel gets a confident sign.
    Rule(
        rule_id="domestic_gold_regulation_restrictive",
        category="domestic_gold_regulation",
        require_entities=_E_GOLD,
        require_terms=(_GOLD_RULES,),
        base_confidence=0.50,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _unreachable(_NO_GLOBAL_REACH),
            CHANNEL_USD_IRT: _prior(0.10, 0.25, "restricting gold can push hedging demand into dollars instead"),
            CHANNEL_PREMIUM: _sign_unknown("a transaction tax raises retail prices while an anti-speculation rule suppresses them; the premium's theory models neither"),
            CHANNEL_LIQUIDITY: _prior(0.35, 0.55, "compliance friction and reporting duties thin the quoted market"),
        },
    ),
    # When the venue stops trading, the three legs of the price identity stop
    # being observed at the same instant, so composing them is meaningless —
    # this is the one rule that overrides the composite instead of deriving it.
    Rule(
        rule_id="exchange_disruption",
        category="exchange_disruption",
        require_entities=(("tehran_gold_bazaar", "ime", "tse", "gold_center",
                           "iran_free_fx_market"),),
        require_terms=(_HALT,),
        base_confidence=0.60,
        horizon="intraday",
        decay_hours=12.0,
        combined=ChannelPrior(
            0.0, 0.15,
            "with the venue halted the legs are no longer observed together, so "
            "a composed level is not meaningful; only the liquidity channel says "
            "anything",
        ),
        channels={
            CHANNEL_XAU: _unreachable(_NO_GLOBAL_REACH),
            CHANNEL_USD_IRT: _sign_unknown("quotes thin out; the mid can drift either way"),
            CHANNEL_PREMIUM: _sign_unknown("arbitrage is impaired, so the observed price detaches from theory: the MAGNITUDE of the premium should rise, its sign is genuinely uncertain"),
            CHANNEL_LIQUIDITY: _prior(0.60, 0.70, "a halted or degraded venue is the definition of impaired execution"),
        },
    ),
    # Iran is an oil exporter, so an energy spike improves export receipts —
    # but under sanctions only part of those receipts is realisable, which is
    # why usd_irt stays near zero here instead of turning negative.
    Rule(
        rule_id="oil_energy_shock_up",
        category="oil_energy_shock",
        require_entities=(("crude_oil", "natural_gas", "opec"),),
        require_terms=(_RISING,),
        base_confidence=0.50,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(0.20, 0.40, "inflation-hedge demand"),
            CHANNEL_USD_IRT: _prior(0.05, 0.20, "higher export receipts help, but sanctions make only part of them realisable"),
            CHANNEL_PREMIUM: _prior(0.05, 0.20, "inherits the ambiguity of both legs"),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "commodity-wide repricing"),
        },
    ),
    Rule(
        rule_id="oil_energy_shock_down",
        category="oil_energy_shock",
        require_entities=(("crude_oil", "natural_gas", "opec"),),
        require_terms=(_FALLING,),
        base_confidence=0.50,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(-0.15, 0.35, "the inflation-hedge bid fades"),
            CHANNEL_USD_IRT: _prior(0.10, 0.25, "export receipts fall, and the sanctioned share of them was already the binding constraint"),
            CHANNEL_PREMIUM: _prior(0.05, 0.20, "inherits both legs"),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "commodity-wide repricing"),
        },
    ),
    # Escalation is the one Iran-specific category that DOES reach xau_usd:
    # a Middle East conflict is a global haven event, not only a local one.
    Rule(
        rule_id="geopolitical_escalation",
        category="geopolitical_escalation",
        require_terms=(_CONFLICT,),
        base_confidence=0.60,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(0.45, 0.70, "global haven bid"),
            CHANNEL_USD_IRT: _prior(0.45, 0.70, "capital flight into hard currency"),
            CHANNEL_PREMIUM: _prior(0.50, 0.70, "physical demand spikes and dealers widen quotes, so Tehran overshoots theory"),
            CHANNEL_LIQUIDITY: _prior(0.40, 0.60, "one-way panic flow"),
        },
    ),
    Rule(
        rule_id="geopolitical_deescalation",
        category="geopolitical_deescalation",
        require_terms=(_CALMING,),
        base_confidence=0.55,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(-0.30, 0.60, "the haven bid unwinds"),
            CHANNEL_USD_IRT: _prior(-0.35, 0.60, "the free rate has historically retraced on easing"),
            CHANNEL_PREMIUM: _prior(-0.40, 0.60, "the panic premium unwinds faster than it built"),
            CHANNEL_LIQUIDITY: _prior(-0.20, 0.45, "two-way flow returns"),
        },
    ),
    # Domestic instability is a toman and premium story with no global leg:
    # world gold does not reprice on Iranian street protests, but Iranian
    # households move into hard assets immediately.
    Rule(
        rule_id="iran_political_risk",
        category="iran_political_risk",
        require_entities=_E_IRAN,
        require_terms=(_UNREST,),
        base_confidence=0.50,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _prior(0.05, 0.15, "at most a marginal regional risk premium in global gold"),
            CHANNEL_USD_IRT: _prior(0.40, 0.60, "households dollarize when domestic stability is in question"),
            CHANNEL_PREMIUM: _prior(0.35, 0.55, "physical gold is the accessible hedge, and dealers widen into the demand"),
            CHANNEL_LIQUIDITY: _prior(0.30, 0.50, "one-way retail flow"),
        },
    ),
    # Official-sector buying is a durable, price-insensitive bid: a level
    # effect on the global leg with almost nothing local attached.
    Rule(
        rule_id="gold_official_demand",
        category="gold_market",
        require_entities=_E_GOLD,
        require_terms=(_CB_BUYING,),
        base_confidence=0.45,
        horizon="1-2w",
        decay_hours=168.0,
        channels={
            CHANNEL_XAU: _prior(0.30, 0.50, "official-sector demand is price-insensitive and persistent"),
            CHANNEL_USD_IRT: _unreachable("reserve buying abroad has no channel into the toman rate"),
            CHANNEL_PREMIUM: _prior(0.05, 0.20, "a firmer global leg that Tehran quotes will lag"),
            CHANNEL_LIQUIDITY: _prior(0.0, 0.15, "no local execution effect"),
        },
    ),
    Rule(
        rule_id="gold_investor_outflow",
        category="gold_market",
        require_entities=_E_GOLD,
        require_terms=(_ETF_OUTFLOW,),
        base_confidence=0.45,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(-0.25, 0.45, "fund liquidation adds supply to the global market"),
            CHANNEL_USD_IRT: _unreachable("no channel into the toman rate"),
            CHANNEL_PREMIUM: _prior(0.05, 0.20, _STICKY_LOCAL),
            CHANNEL_LIQUIDITY: _prior(0.05, 0.20, "no local execution effect worth claiming"),
        },
    ),
    Rule(
        rule_id="gold_investor_inflow",
        category="gold_market",
        require_entities=_E_GOLD,
        require_terms=(_ETF_INFLOW,),
        base_confidence=0.45,
        horizon="1-3d",
        decay_hours=48.0,
        channels={
            CHANNEL_XAU: _prior(0.25, 0.45, "fund accumulation removes supply from the global market"),
            CHANNEL_USD_IRT: _unreachable("no channel into the toman rate"),
            CHANNEL_PREMIUM: _prior(-0.05, 0.20, "mirror of the sticky-quote lag"),
            CHANNEL_LIQUIDITY: _prior(0.0, 0.15, "no local execution effect"),
        },
    ),
)

# Rules that argue the opposite direction of the SAME mechanism.  When both
# fire, neither is trusted at face value: each records the other's matched terms
# as contradicting evidence and pays OPPOSED_RULE_PENALTY.  Consolidation also
# reads this map to flag a duplicate group whose members disagree.
_OPPOSED_PAIRS: tuple[tuple[str, str], ...] = (
    ("fed_hawkish", "fed_dovish"),
    ("us_inflation_hot", "us_inflation_cool"),
    ("us_labor_strong", "us_labor_weak"),
    ("us_yields_up", "us_yields_down"),
    ("dollar_strength_up", "dollar_weakness"),
    ("sanctions_escalation", "sanctions_relief"),
    ("iran_negotiations_progress", "iran_negotiations_breakdown"),
    ("iran_fx_policy_restrictive", "iran_fx_policy_liberalising"),
    ("iran_monetary_expansion", "iran_monetary_tightening"),
    ("oil_energy_shock_up", "oil_energy_shock_down"),
    ("geopolitical_escalation", "geopolitical_deescalation"),
    ("gold_official_demand", "gold_investor_outflow"),
    ("gold_investor_inflow", "gold_investor_outflow"),
)


def _build_opposed() -> dict[str, frozenset[str]]:
    opposed: dict[str, set[str]] = {}
    for left, right in _OPPOSED_PAIRS:
        opposed.setdefault(left, set()).add(right)
        opposed.setdefault(right, set()).add(left)
    return {key: frozenset(value) for key, value in opposed.items()}


OPPOSED_RULES: Mapping[str, frozenset[str]] = _build_opposed()


def _build_rule_index() -> dict[str, Rule]:
    index: dict[str, Rule] = {}
    for rule in RULES:
        if rule.rule_id in index:
            raise ValueError(f"classifier: duplicate rule id {rule.rule_id!r}")
        if rule.category not in CATEGORIES:
            raise ValueError(f"classifier: rule {rule.rule_id!r} has unknown category")
        for channel in rule.channels:
            if channel not in CHANNELS:
                raise ValueError(f"classifier: rule {rule.rule_id!r} has unknown channel {channel!r}")
        index[rule.rule_id] = rule
    for rule_id in OPPOSED_RULES:
        if rule_id not in index:
            raise ValueError(f"classifier: opposed pair names unknown rule {rule_id!r}")
    return index


RULES_BY_ID: Mapping[str, Rule] = _build_rule_index()

# Folded phrase forms, built once.  Reusing the gazetteer's folding is what
# lets a rule list a Persian cue next to an English one and have both matched
# by the same code path.
_FOLDED_TERMS: dict[str, str] = {}


def _folded(term: str) -> str:
    folded = _FOLDED_TERMS.get(term)
    if folded is None:
        folded = entities.fold(term)
        _FOLDED_TERMS[term] = folded
    return folded


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _matched_terms(padded: str, group: Sequence[str]) -> tuple[str, ...]:
    """Terms of ``group`` present in ``padded`` as whole tokens, in order."""
    return tuple(
        term for term in group
        if _folded(term) and f" {_folded(term)} " in padded
    )


@dataclass(frozen=True)
class _Fired:
    rule: Rule
    confidence: float
    supporting: tuple[str, ...]
    weakened: tuple[str, ...]


def _fire(rule: Rule, padded: str, present: frozenset[str]) -> Optional[_Fired]:
    supporting: list[str] = []
    for group in rule.require_entities:
        hit = [code for code in group if code in present]
        if not hit:
            return None
        supporting.extend(f"entity:{code}" for code in hit)
    for group in rule.require_terms:
        hit = _matched_terms(padded, group)
        if not hit:
            return None
        supporting.extend(hit)
    weakened = _matched_terms(padded, rule.weaken_terms)

    # One required term per group is the baseline; anything beyond that is the
    # same claim restated, so it is worth a small capped bonus only.
    extra = max(0, len(supporting) - len(rule.require_terms) - len(rule.require_entities))
    confidence = (
        rule.base_confidence
        + min(MAX_SUPPORT_BONUS, SUPPORT_BONUS_PER_TERM * extra)
        - WEAKEN_PENALTY * len(weakened)
    )
    return _Fired(
        rule=rule,
        confidence=_clamp(confidence, MIN_RULE_CONFIDENCE, MAX_RULE_CONFIDENCE),
        supporting=tuple(supporting),
        weakened=weakened,
    )


def _compose_combined(
    rule: Rule, priors: Mapping[str, ChannelPrior]
) -> ChannelPrior:
    """The composite leg of the local-gold price identity (see module doc)."""
    if rule.combined is not None:
        return rule.combined
    score = sum(
        weight * priors[channel].score
        for channel, weight in COMBINED_WEIGHTS.items()
        if channel in priors
    )
    # A composite is only as well known as the weakest leg that actually moves
    # it; when no leg moves, the weakest leg overall sets the bar.
    contributing = [
        priors[channel].directness
        for channel in COMBINED_WEIGHTS
        if channel in priors and priors[channel].score != 0.0
    ] or [priors[channel].directness for channel in COMBINED_WEIGHTS if channel in priors]
    directness = COMBINED_DIRECTNESS_DISCOUNT * min(contributing or [0.10])
    legs = ", ".join(
        f"{weight:.2f}x {channel}" for channel, weight in COMBINED_WEIGHTS.items()
    )
    return ChannelPrior(
        _clamp(score), directness,
        "price identity, not a sentiment average: local toman gold = " + legs,
    )


def classify(
    title: str,
    summary: str = "",
    body: str = "",
    entity_matches: Optional[Iterable[EntityMatch]] = None,
) -> EventClassification:
    """Classify one article/event and derive its per-channel hypotheses.

    ``entity_matches`` may be supplied by a caller that already extracted them
    (consolidation does) to avoid a second pass over the same text.
    """
    matches = tuple(entity_matches) if entity_matches is not None else entities.extract(
        title, summary, body
    )
    present = entities.codes(matches)
    haystack = " ".join(
        part for part in (entities.fold(title), entities.fold(summary), entities.fold(body))
        if part
    )
    padded = f" {haystack} "

    fired = [result for result in (_fire(rule, padded, present) for rule in RULES)
             if result is not None]
    if not fired:
        return EventClassification(
            classifier_version=CLASSIFIER_VERSION,
            primary=Classification(
                category=UNCLASSIFIED,
                rule_id="",
                rule_version="",
                confidence=0.0,
                supporting_terms=(),
                contradicting_terms=(),
            ),
            secondary=(),
            # No rule, no hypothesis.  An unclassified event asserts nothing
            # about any channel, which is different from asserting zero.
            hypotheses=(),
            entity_codes=tuple(sorted(present)),
        )

    fired_ids = {result.rule.rule_id for result in fired}
    contradictions: dict[str, tuple[str, ...]] = {}
    adjusted: list[_Fired] = []
    for result in fired:
        opposing = sorted(OPPOSED_RULES.get(result.rule.rule_id, frozenset()) & fired_ids)
        against: list[str] = list(result.weakened)
        for other_id in opposing:
            other = next(item for item in fired if item.rule.rule_id == other_id)
            against.extend(other.supporting)
        contradictions[result.rule.rule_id] = tuple(against)
        penalty = OPPOSED_RULE_PENALTY if opposing else 0.0
        adjusted.append(
            _Fired(
                rule=result.rule,
                confidence=_clamp(result.confidence - penalty, MIN_RULE_CONFIDENCE,
                                  MAX_RULE_CONFIDENCE),
                supporting=result.supporting,
                weakened=result.weakened,
            )
        )

    # Deterministic ranking: confidence, then how much text backed it, then the
    # rule id so the same input can never produce two different primaries.
    adjusted.sort(key=lambda item: (-item.confidence, -len(item.supporting), item.rule.rule_id))
    winner = adjusted[0]

    classifications = [
        Classification(
            category=item.rule.category,
            rule_id=item.rule.rule_id,
            rule_version=item.rule.version,
            confidence=round(item.confidence, 4),
            supporting_terms=item.supporting,
            contradicting_terms=contradictions[item.rule.rule_id],
        )
        for item in adjusted
    ]

    priors = dict(winner.rule.channels)
    priors[CHANNEL_COMBINED] = _compose_combined(winner.rule, priors)
    hypotheses = tuple(
        Hypothesis(
            channel=channel,
            score=round(_clamp(priors[channel].score), 4),
            confidence=round(_clamp(winner.confidence * priors[channel].directness, 0.0, 1.0), 4),
            rule_id=winner.rule.rule_id,
            rule_version=winner.rule.version,
            supporting_evidence=winner.supporting,
            contradicting_evidence=contradictions[winner.rule.rule_id],
            expected_horizon=winner.rule.horizon,
            decay_hours=winner.rule.decay_hours,
            mechanism=priors[channel].mechanism,
        )
        for channel in CHANNELS
        if channel in priors
    )
    return EventClassification(
        classifier_version=CLASSIFIER_VERSION,
        primary=classifications[0],
        secondary=tuple(classifications[1:]),
        hypotheses=hypotheses,
        entity_codes=tuple(sorted(present)),
    )


def classification_rows(event_id: int, result: EventClassification) -> list[dict]:
    """Rows for ``news_event_classifications`` (primary first).

    Secondary rules that repeat the primary's category are dropped: the table
    is unique per (event, classifier_version, category), and a second rule of
    the same category adds no category information.
    """
    rows = []
    seen: set[str] = set()
    for item in (result.primary,) + result.secondary:
        if item.category in seen or item.category == UNCLASSIFIED:
            continue
        seen.add(item.category)
        rows.append(
            {
                "event_id": event_id,
                "classifier_version": result.classifier_version,
                "category": item.category,
                "confidence": item.confidence,
                "rule_id": item.rule_id,
                "supporting_terms": list(item.supporting_terms),
                "contradicting_terms": list(item.contradicting_terms),
            }
        )
    return rows


def hypothesis_rows(event_id: int, result: EventClassification) -> list[dict]:
    """Rows for ``news_impact_hypotheses``.

    ``hypothesis_only`` is written explicitly rather than left to the column
    default, so the flag is visible in the insert that a reader audits.
    """
    return [
        {
            "event_id": event_id,
            "classifier_version": result.classifier_version,
            "channel": item.channel,
            "score": item.score,
            "confidence": item.confidence,
            "rule_id": item.rule_id,
            "rule_version": item.rule_version,
            "supporting_evidence": list(item.supporting_evidence),
            "contradicting_evidence": list(item.contradicting_evidence),
            # Left NULL: no event study has measured how many past cases back
            # this rule, and 0 would read as "measured, and found nothing".
            "sample_support": None,
            "expected_horizon": item.expected_horizon,
            "decay_hours": item.decay_hours,
            "hypothesis_only": True,
        }
        for item in result.hypotheses
    ]


def classifier_version_row() -> dict:
    """Row for ``news_classifier_versions`` describing this rule set."""
    return {
        "version": CLASSIFIER_VERSION,
        "kind": CLASSIFIER_KIND,
        "description": (
            "Deterministic keyword/entity rules producing one bounded, "
            "hypothesis-only impact claim per channel. No measured effects."
        ),
        "rule_count": len(RULES),
    }
