"""Deterministic gazetteer entity extraction for news articles.

WHY a gazetteer and not a model: entity extraction here feeds dedupe decisions
(does the same story from two outlets mention the same actors?) and the audit
trail behind every impact hypothesis.  Both uses demand that the SAME text
always yields the SAME entities, today and after a redeploy, and that a human
can read the reason a match happened.  A statistical NER would give neither,
and would drag a model dependency onto an ingestion path whose job is string
matching.

WHY names and never offices: the gazetteer stores an actor's name and its
aliases only.  It deliberately does not record "Fed chair" or "CBI governor"
against a person, because office-holders change and a stale title stored as
fact is a fabricated claim about the world.  Roles that matter are modelled as
their own institutional entities (``federal_reserve``, ``cbi``).

WHY no coordinates: ``news_entities`` has ``latitude``/``longitude`` columns,
and this module never populates them.  A coordinate that was inferred from a
country name — rather than supplied and verified by the source — is invented
precision, and a map drawn from invented precision reads as evidence.  The
row builder below simply omits both columns so they stay NULL and
``location_verified`` stays FALSE.

Persian and English aliases live side by side: Iranian sources write the same
actor as ``بانک مرکزی`` and English wires write "Central Bank of Iran", and a
group of articles about one event must resolve to one entity set regardless of
which language reported it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

from . import textnorm
from .dedupe import normalize_text

# Bumped whenever the gazetteer or the matching rule changes, and stored on
# every ``news_article_entities`` row so an extraction can be attributed to the
# exact vocabulary that produced it.
EXTRACTOR_VERSION = "gazetteer-1.0.0"

KIND_COUNTRY = "country"
KIND_CENTRAL_BANK = "central_bank"
KIND_PERSON = "person"
KIND_ORGANIZATION = "organization"
KIND_COMMODITY = "commodity"
KIND_CURRENCY = "currency"
KIND_INDICATOR = "economic_indicator"
KIND_SANCTIONS_PROGRAM = "sanctions_program"
KIND_MARKET = "market"

KINDS: tuple[str, ...] = (
    KIND_COUNTRY, KIND_CENTRAL_BANK, KIND_PERSON, KIND_ORGANIZATION,
    KIND_COMMODITY, KIND_CURRENCY, KIND_INDICATOR, KIND_SANCTIONS_PROGRAM,
    KIND_MARKET,
)


@dataclass(frozen=True)
class Entity:
    """One gazetteer entry.  No location fields exist on purpose (see module doc)."""

    kind: str
    code: str                    # stable slug, globally unique across kinds
    display_name: str
    display_fa: str
    aliases: tuple[str, ...]     # surface forms, English and Persian


@dataclass(frozen=True)
class EntityMatch:
    """An entity found in a text, with the surface form that produced it."""

    kind: str
    code: str
    display_name: str
    matched_term: str            # the alias as written in the gazetteer
    extractor_version: str = EXTRACTOR_VERSION


def _e(kind: str, code: str, name: str, fa: str, *aliases: str) -> Entity:
    # The display name is always a valid alias; listing it again in every entry
    # would be noise.
    forms = (name,) + aliases if name not in aliases else aliases
    if fa and fa not in forms:
        forms = forms + (fa,)
    return Entity(kind=kind, code=code, display_name=name, display_fa=fa,
                  aliases=forms)


# "us" is deliberately absent as an alias of the United States: the English
# pronoun collides with it on every second headline.  "u s" (from "U.S." after
# punctuation stripping) carries the same information without the collision.
GAZETTEER: tuple[Entity, ...] = (
    # --- countries -----------------------------------------------------------
    _e(KIND_COUNTRY, "iran", "Iran", "ایران", "iranian", "islamic republic",
       "tehran", "تهران", "جمهوری اسلامی"),
    _e(KIND_COUNTRY, "united_states", "United States", "آمریکا", "u s", "usa",
       "america", "american", "washington", "ایالات متحده", "امریکا"),
    _e(KIND_COUNTRY, "israel", "Israel", "اسرائیل", "israeli", "رژیم صهیونیستی"),
    _e(KIND_COUNTRY, "russia", "Russia", "روسیه", "russian", "moscow", "مسکو"),
    _e(KIND_COUNTRY, "china", "China", "چین", "chinese", "beijing", "پکن"),
    _e(KIND_COUNTRY, "saudi_arabia", "Saudi Arabia", "عربستان", "saudi",
       "riyadh", "عربستان سعودی"),
    _e(KIND_COUNTRY, "iraq", "Iraq", "عراق", "iraqi", "baghdad"),
    _e(KIND_COUNTRY, "turkey", "Turkey", "ترکیه", "turkish", "ankara"),
    _e(KIND_COUNTRY, "uae", "United Arab Emirates", "امارات", "u a e", "dubai",
       "emirati", "دبی"),
    _e(KIND_COUNTRY, "venezuela", "Venezuela", "ونزوئلا", "venezuelan"),
    _e(KIND_COUNTRY, "lebanon", "Lebanon", "لبنان", "lebanese", "beirut"),
    _e(KIND_COUNTRY, "yemen", "Yemen", "یمن", "yemeni"),

    # --- central banks -------------------------------------------------------
    _e(KIND_CENTRAL_BANK, "federal_reserve", "Federal Reserve", "فدرال رزرو",
       "the fed", "fed", "fomc", "federal open market committee",
       "فدرال‌رزرو", "بانک مرکزی آمریکا"),
    _e(KIND_CENTRAL_BANK, "cbi", "Central Bank of Iran", "بانک مرکزی",
       "central bank of iran", "cbi", "بانک مرکزی ایران", "بانک مرکزی جمهوری اسلامی"),
    _e(KIND_CENTRAL_BANK, "ecb", "European Central Bank", "بانک مرکزی اروپا",
       "ecb"),
    _e(KIND_CENTRAL_BANK, "boe", "Bank of England", "بانک مرکزی انگلیس", "boe"),
    _e(KIND_CENTRAL_BANK, "boj", "Bank of Japan", "بانک مرکزی ژاپن", "boj"),
    _e(KIND_CENTRAL_BANK, "pboc", "People's Bank of China", "بانک مرکزی چین",
       "pboc", "people s bank of china"),

    # --- persons (names only; offices are never asserted here) ---------------
    _e(KIND_PERSON, "jerome_powell", "Jerome Powell", "جروم پاول", "powell",
       "پاول"),
    _e(KIND_PERSON, "ali_khamenei", "Ali Khamenei", "علی خامنه‌ای", "khamenei",
       "خامنه‌ای"),
    _e(KIND_PERSON, "masoud_pezeshkian", "Masoud Pezeshkian", "مسعود پزشکیان",
       "pezeshkian", "پزشکیان"),
    _e(KIND_PERSON, "donald_trump", "Donald Trump", "دونالد ترامپ", "trump",
       "ترامپ"),
    _e(KIND_PERSON, "benjamin_netanyahu", "Benjamin Netanyahu", "بنیامین نتانیاهو",
       "netanyahu", "نتانیاهو"),
    _e(KIND_PERSON, "abbas_araghchi", "Abbas Araghchi", "عباس عراقچی",
       "araghchi", "عراقچی"),

    # --- organizations -------------------------------------------------------
    _e(KIND_ORGANIZATION, "imf", "International Monetary Fund", "صندوق بین‌المللی پول",
       "imf"),
    _e(KIND_ORGANIZATION, "world_bank", "World Bank", "بانک جهانی"),
    _e(KIND_ORGANIZATION, "opec", "OPEC", "اوپک", "opec plus", "opec+"),
    _e(KIND_ORGANIZATION, "iaea", "IAEA", "آژانس بین‌المللی انرژی اتمی", "iaea",
       "international atomic energy agency", "آژانس اتمی"),
    _e(KIND_ORGANIZATION, "un_security_council", "UN Security Council",
       "شورای امنیت", "security council", "unsc", "شورای امنیت سازمان ملل"),
    _e(KIND_ORGANIZATION, "ofac", "OFAC", "اوفک",
       "office of foreign assets control"),
    _e(KIND_ORGANIZATION, "us_treasury", "US Treasury", "وزارت خزانه‌داری آمریکا",
       "treasury department", "خزانه‌داری آمریکا"),
    _e(KIND_ORGANIZATION, "european_union", "European Union", "اتحادیه اروپا",
       "eu", "brussels"),
    _e(KIND_ORGANIZATION, "swift", "SWIFT", "سوئیفت"),
    _e(KIND_ORGANIZATION, "fatf", "FATF", "گروه ویژه اقدام مالی", "fatf"),
    _e(KIND_ORGANIZATION, "irgc", "IRGC", "سپاه پاسداران",
       "revolutionary guard", "revolutionary guards", "سپاه"),
    _e(KIND_ORGANIZATION, "gold_jewellers_union", "Gold and Jewellery Union",
       "اتحادیه طلا و جواهر", "اتحادیه طلا"),

    # --- commodities ---------------------------------------------------------
    _e(KIND_COMMODITY, "gold", "Gold", "طلا", "bullion", "xau", "شمش طلا",
       "طلای آب‌شده", "gold price", "قیمت طلا"),
    _e(KIND_COMMODITY, "silver", "Silver", "نقره", "xag"),
    _e(KIND_COMMODITY, "crude_oil", "Crude oil", "نفت", "oil", "crude", "brent",
       "wti", "oil price", "نفت خام", "قیمت نفت"),
    _e(KIND_COMMODITY, "natural_gas", "Natural gas", "گاز طبیعی", "natural gas",
       "lng"),
    _e(KIND_COMMODITY, "gold_coin", "Gold coin (Bahar Azadi)", "سکه",
       "bahar azadi", "سکه بهار آزادی", "سکه امامی", "نیم سکه", "ربع سکه"),

    # --- currencies ----------------------------------------------------------
    _e(KIND_CURRENCY, "usd", "US dollar", "دلار", "dollar", "greenback",
       "دلار آمریکا"),
    _e(KIND_CURRENCY, "irr", "Iranian rial", "ریال", "rial", "toman", "تومان",
       "irr", "irt"),
    _e(KIND_CURRENCY, "eur", "Euro", "یورو", "euro"),
    _e(KIND_CURRENCY, "cny", "Chinese yuan", "یوان", "yuan", "renminbi"),

    # --- economic indicators -------------------------------------------------
    _e(KIND_INDICATOR, "cpi", "Consumer price index", "شاخص قیمت مصرف‌کننده",
       "cpi", "consumer price index", "consumer prices"),
    _e(KIND_INDICATOR, "pce", "PCE price index", "شاخص قیمت مخارج مصرفی", "pce"),
    _e(KIND_INDICATOR, "ppi", "Producer price index", "شاخص قیمت تولیدکننده",
       "ppi", "producer price index"),
    _e(KIND_INDICATOR, "nonfarm_payrolls", "Nonfarm payrolls", "اشتغال بخش غیرکشاورزی",
       "nonfarm payrolls", "non farm payrolls", "payrolls", "jobs report"),
    _e(KIND_INDICATOR, "unemployment_rate", "Unemployment rate", "نرخ بیکاری",
       "unemployment rate", "jobless claims", "jobless rate"),
    _e(KIND_INDICATOR, "gdp", "Gross domestic product", "تولید ناخالص داخلی",
       "gdp"),
    _e(KIND_INDICATOR, "fed_funds_rate", "Federal funds rate", "نرخ بهره فدرال",
       "federal funds rate", "fed funds", "policy rate", "interest rate decision"),
    _e(KIND_INDICATOR, "treasury_yield_10y", "10-year Treasury yield",
       "بازدهی اوراق خزانه", "treasury yield", "treasury yields",
       "10 year yield", "bond yields"),
    _e(KIND_INDICATOR, "dxy", "Dollar index", "شاخص دلار", "dxy",
       "dollar index"),
    _e(KIND_INDICATOR, "iran_liquidity", "Iranian money supply", "نقدینگی",
       "money supply", "حجم نقدینگی"),
    _e(KIND_INDICATOR, "iran_inflation", "Iranian inflation", "تورم",
       "نرخ تورم"),

    # --- sanctions programs --------------------------------------------------
    _e(KIND_SANCTIONS_PROGRAM, "jcpoa", "JCPOA", "برجام", "jcpoa",
       "nuclear deal", "nuclear agreement", "توافق هسته‌ای"),
    _e(KIND_SANCTIONS_PROGRAM, "snapback", "Snapback mechanism", "مکانیسم ماشه",
       "snapback", "ماشه"),
    _e(KIND_SANCTIONS_PROGRAM, "ofac_sdn", "OFAC SDN designations",
       "فهرست تحریم اوفک", "sdn list", "specially designated nationals",
       "designations"),
    _e(KIND_SANCTIONS_PROGRAM, "maximum_pressure", "Maximum pressure campaign",
       "فشار حداکثری", "maximum pressure"),
    _e(KIND_SANCTIONS_PROGRAM, "oil_export_sanctions", "Oil export sanctions",
       "تحریم نفتی", "oil sanctions", "petroleum sanctions", "تحریم صادرات نفت"),
    _e(KIND_SANCTIONS_PROGRAM, "banking_sanctions", "Banking sanctions",
       "تحریم بانکی", "banking sanctions", "financial sanctions"),

    # --- markets/venues ------------------------------------------------------
    _e(KIND_MARKET, "comex", "COMEX", "کامکس", "comex"),
    _e(KIND_MARKET, "lbma", "LBMA", "ال‌بی‌ام‌ای", "lbma", "london bullion market"),
    _e(KIND_MARKET, "tehran_gold_bazaar", "Tehran gold bazaar", "بازار طلا",
       "gold bazaar", "بازار طلای تهران", "بازار سکه"),
    _e(KIND_MARKET, "iran_free_fx_market", "Iranian free FX market", "بازار آزاد ارز",
       "free market rate", "بازار آزاد", "بازار ارز"),
    _e(KIND_MARKET, "ime", "Iran Mercantile Exchange", "بورس کالای ایران",
       "mercantile exchange", "بورس کالا"),
    _e(KIND_MARKET, "tse", "Tehran Stock Exchange", "بورس تهران",
       "tehran stock exchange", "بورس اوراق بهادار تهران"),
    _e(KIND_MARKET, "gold_center", "Gold Exchange Center", "مرکز مبادله طلا",
       "مرکز مبادله ارز و طلا", "exchange centre"),
)


def fold(value: str) -> str:
    """Orthography-independent, punctuation-free, casefolded text.

    Both existing normalizers, composed rather than reimplemented:
    :func:`app.news.textnorm.normalize` unifies Arabic letter forms, digits,
    diacritics and the half-space, and :func:`app.news.dedupe.normalize_text`
    casefolds and strips punctuation.  Aliases and article text go through the
    same function, which is what makes a match orthography-independent — and
    reusing these two means entity matching can never disagree with hashing or
    title comparison about what a string says.
    """
    if not value:
        return ""
    return normalize_text(textnorm.normalize(value))


def _build_index() -> tuple[dict[str, Entity], dict[str, tuple[Entity, str]]]:
    by_code: dict[str, Entity] = {}
    by_alias: dict[str, tuple[Entity, str]] = {}
    for entity in GAZETTEER:
        if entity.kind not in KINDS:
            raise ValueError(f"gazetteer: unknown kind {entity.kind!r}")
        if entity.code in by_code:
            # Codes are the join key used by consolidation and by the
            # ``news_entities`` seed; a silent collision would merge two actors.
            raise ValueError(f"gazetteer: duplicate code {entity.code!r}")
        by_code[entity.code] = entity
        for alias in entity.aliases:
            folded = fold(alias)
            if not folded:
                continue
            existing = by_alias.get(folded)
            if existing is not None and existing[0].code != entity.code:
                raise ValueError(
                    f"gazetteer: alias {alias!r} claimed by "
                    f"{existing[0].code!r} and {entity.code!r}"
                )
            # Keep the longest original spelling for an alias that folds to the
            # same string, so the stored matched_term is the readable one.
            if existing is None or len(alias) > len(existing[1]):
                by_alias[folded] = (entity, alias)
    return by_code, by_alias


_BY_CODE, _BY_ALIAS = _build_index()


def get(code: str) -> Optional[Entity]:
    """Gazetteer entry for ``code``, or None when unknown."""
    return _BY_CODE.get(code)


def extract(title: str, summary: str = "", body: str = "") -> tuple[EntityMatch, ...]:
    """Entities mentioned in an article, deterministically ordered.

    Matching is whole-token: the folded alias must appear between token
    boundaries, so "iran" does not fire on "irangate" while the explicit
    "iranian" alias still does.  At most one match per entity is returned, and
    the longest matching alias wins, because the row it becomes
    (``news_article_entities``) is unique per (article, entity) anyway.

    Same-kind containment is then resolved in favour of the longer name: the
    Persian for "US Federal Reserve" (``بانک مرکزی آمریکا``) contains the
    Persian for "Central Bank of Iran" (``بانک مرکزی``), and two central banks
    are alternatives, not complements.  Containment ACROSS kinds is kept —
    "Central Bank of Iran" genuinely mentions Iran — because there the shorter
    entity is implied rather than displaced.
    """
    haystack = " ".join(part for part in (fold(title), fold(summary), fold(body)) if part)
    if not haystack:
        return ()
    padded = f" {haystack} "
    best: dict[str, tuple[Entity, str, str]] = {}
    for folded_alias, (entity, original) in _BY_ALIAS.items():
        if f" {folded_alias} " not in padded:
            continue
        current = best.get(entity.code)
        if current is None or len(folded_alias) > len(current[2]):
            best[entity.code] = (entity, original, folded_alias)

    kept = [hit for hit in best.values() if not _is_covered(padded, hit, best.values())]
    return tuple(
        EntityMatch(
            kind=entity.kind,
            code=entity.code,
            display_name=entity.display_name,
            matched_term=term,
        )
        for entity, term, _folded in sorted(
            kept, key=lambda hit: (hit[0].kind, hit[0].code)
        )
    )


def _is_covered(
    padded: str,
    hit: tuple[Entity, str, str],
    others: Iterable[tuple[Entity, str, str]],
) -> bool:
    """True when every occurrence of ``hit`` sits inside a longer same-kind name."""
    entity, _term, folded = hit
    covering = [
        other_folded
        for other_entity, _other_term, other_folded in others
        if other_entity.kind == entity.kind
        and other_entity.code != entity.code
        and f" {folded} " in f" {other_folded} "
    ]
    if not covering:
        return False
    residual = padded
    for other_folded in covering:
        # A sentinel rather than an empty string: collapsing the surrounding
        # spaces would glue neighbouring tokens together and invent matches.
        residual = residual.replace(f" {other_folded} ", " \x00 ")
    return f" {folded} " not in residual


def codes(matches: Iterable[EntityMatch]) -> frozenset[str]:
    """Entity codes of a match list — the form consolidation compares."""
    return frozenset(match.code for match in matches)


def overlap(left: Iterable[str], right: Iterable[str]) -> float:
    """Jaccard overlap of two entity-code sets; 0.0 when either is empty.

    Empty-vs-empty scores 0, not 1: two articles that mention no known actor
    have not been shown to be about the same thing.
    """
    a, b = frozenset(left), frozenset(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def gazetteer_rows() -> list[dict]:
    """Seed rows for ``news_entities``.

    ``latitude``/``longitude`` are omitted rather than set: an absent key
    leaves the columns NULL, which is the only honest value for a location this
    system never observed (see the module docstring).
    """
    return [
        {
            "kind": entity.kind,
            "code": entity.code,
            "display_name": entity.display_name,
            "display_fa": entity.display_fa,
            "aliases": list(entity.aliases),
            "location_verified": False,
        }
        for entity in GAZETTEER
    ]


def article_entity_rows(
    article_id: int,
    matches: Iterable[EntityMatch],
    entity_ids: Mapping[tuple[str, str], int],
) -> list[dict]:
    """Rows for ``news_article_entities``.

    ``entity_ids`` maps ``(kind, code)`` — the table's natural key — to the
    surrogate id the seed assigned.  A match whose entity is not in the map is
    dropped rather than inserted against a guessed id.
    """
    rows = []
    for match in matches:
        entity_id = entity_ids.get((match.kind, match.code))
        if entity_id is None:
            continue
        rows.append(
            {
                "article_id": article_id,
                "entity_id": entity_id,
                "matched_term": match.matched_term,
                "extractor_version": match.extractor_version,
            }
        )
    return rows
