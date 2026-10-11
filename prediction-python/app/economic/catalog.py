"""The declared catalog of economic series this system ingests.

One entry per series, and a series is not in this file unless its endpoint was
probed and its shape verified — every entry below was fetched live on
2026-09-08.  The catalog is the source of truth for the ``instruments`` and
``economic_series`` rows :func:`app.economic.store.ensure_series` writes; the
database rows are a projection of it, never the other way round.

Four families.  The first two are annual, revisable, and were chosen because
they are the cheapest honest test of the vintage machinery migration 0024
introduced; the third is the series that machinery was built for, and the
fourth is the second Iranian publisher to use it.

* ``WB_CPI_*``   — World Bank ``FP.CPI.TOTL``, a CPI *index* (2010 = 100).
* ``IMF_PCPIPCH_*`` — IMF WEO ``PCPIPCH``, annual average inflation in percent.
* ``SCI_CPI_*``  — the Statistical Centre of Iran's urban CPI (1400 = 100),
  monthly, on Jalali reference periods.
* ``CBI_*``      — the Central Bank of Iran's monetary aggregates (base money,
  M2, M1, quasi-money): end-of-month STOCKS in thousand billion rials, monthly
  on Jalali reference periods (migration 0034).

The first two are NOT two views of one series and must never be spliced
together: one is a level, the other a rate, and they disagree about Iran by a
wide margin because they are measuring different things from different sources.
Neither is comparable level-for-level with the third, which is on its own base.

``measure`` is part of series identity, not a display option (0024 spells out
why), which is why the same country appears twice with two codes.

**What used to be absent, and why it is here now.**  This file used to record
that nothing from the Statistical Centre of Iran was in the catalog because
that "needs the ``source_documents`` archive path to work first, and P0 does
not have it".  :mod:`app.economic.sci` is that path: it hashes the workbook,
archives it in ``source_documents`` — the first real write to that table — and
links every observation it stores to the document it was read from.  So the
SCI entries are here, and they are this catalog's first
``quality_tier='official'`` series (SCI is the legally designated statistical
authority; everything else here is a mirror or an estimate), its first
``calendar='jalali'`` series, and its first ``splice_policy='chain_growth'``
series.

The Central Bank of Iran's HOUSING report is still absent, and for a
different reason: it ran Farvardin 1396 to Mordad 1403 and then stopped when
the CBI lost access to the transaction registry, so there is no current series
to ingest.  Its MONETARY statistics are current and are here: see the CBI
section below and :mod:`app.economic.cbi`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence


@dataclass(frozen=True)
class SeriesSpec:
    """A declared series: its identity, its provenance, and its caveats.

    The field set is exactly what ``instruments`` + ``economic_series`` need
    plus the two the adapter needs (:attr:`country`, :attr:`indicator`), so a
    new series is one entry here and no code change anywhere else.
    """

    code: str                    # canonical instrument code, PK of instruments
    name_en: str
    name_fa: str
    domain: str                  # instruments.domain
    provider_code: str           # must exist in data_providers
    provider_series_id: str      # the source's own key for this series
    country: str                 # ISO3 the adapter asks the source for
    indicator: str               # the source's indicator id
    frequency: str               # D | W | M | Q | A
    calendar: str                # gregorian | jalali
    measure: str                 # index | level | yoy_pct | mom_pct | ratio | rate
    seasonal_adjustment: str     # nsa | sa | unknown
    base_period: str             # '2010=100'; '' when the measure has no base
    quality_tier: str            # official | official_mirror | ... | experimental
    quote_currency: str          # INDEX | PCT | ...
    unit: str                    # index | pct | ...
    decimals: int
    revisable: bool
    splice_policy: str           # none | chain_growth
    notes: str                   # travels with the number, into the UI
    # NULL in the schema means "not characterised".  One measured example is
    # not a characterisation, so these stay None and the measurement lives in
    # ``notes`` where it cannot be mistaken for a modelled lag.
    publication_lag_days: Optional[int] = None
    enabled: bool = True
    # An economic series is never open or closed, so the market-hours class
    # that governs price freshness does not apply to it.
    calendar_class: str = "none"
    kind: str = "economic_series"


# The seven economies P0 covers: Iran plus the six neighbours and trade
# partners whose inflation is the comparison a Tehran reader actually makes.
# (ISO3, English name, Persian name)
_COUNTRIES: tuple[tuple[str, str, str], ...] = (
    ("IRN", "Iran", "ایران"),
    ("TUR", "Türkiye", "ترکیه"),
    ("SAU", "Saudi Arabia", "عربستان سعودی"),
    ("ARE", "United Arab Emirates", "امارات متحده عربی"),
    ("PAK", "Pakistan", "پاکستان"),
    ("EGY", "Egypt", "مصر"),
    ("RUS", "Russia", "روسیه"),
)

# --- World Bank FP.CPI.TOTL --------------------------------------------------

WORLDBANK_INDICATOR = "FP.CPI.TOTL"

# Verified 2026-09-08 against
# https://api.worldbank.org/v2/country/IRN/indicator/FP.CPI.TOTL?format=json&per_page=200
# The indicator's own name states the base and the 2010 observation is exactly
# 100.0, so the base period is the World Bank's, not the country's.
WORLDBANK_BASE_PERIOD = "2010=100"

_WB_NOTE = (
    "World Bank Indicators API mirror of the national statistics office's CPI, "
    "rebased by the World Bank to 2010=100 (verified 2026-09-08: the 2010 "
    "observation is exactly 100). Annual only. The API states a dataset-wide "
    "'lastupdated' (2026-07-13 when this entry was written) but no "
    "per-observation publication date, so published_at is NULL and available_at "
    "carries the whole point-in-time claim. Coverage is genuinely incomplete for "
    "some countries — the UAE series has 47 missing years and Russia 32 — and a "
    "missing year is skipped, never zero-filled. The base is verified on every "
    "ingest against the base the payload's own indicator name states; if the "
    "World Bank rebases, the series is refused rather than restated levels being "
    "stored under this label."
)

_WB_IRAN_NOTE = (
    " For Iran this is NOT the Statistical Centre of Iran's own 1400=100 index: "
    "the two are different levels on different bases and cannot be compared "
    "level-for-level or spliced."
)

# --- IMF WEO PCPIPCH ---------------------------------------------------------

IMF_INDICATOR = "PCPIPCH"

_IMF_NOTE = (
    "IMF World Economic Outlook annual average inflation (PCPIPCH) via the "
    "DataMapper API, verified 2026-09-08. Three limits travel with every number "
    "here. (1) The IMF has had no Article IV consultation with Iran since 2018, "
    "so the Iranian figures in this family stand on staff estimation with no "
    "current mission behind them. (2) The DataMapper API does not expose the WEO "
    "'estimates start after' flag, so the API cannot say which year reported data "
    "ends and staff estimates begin — recent values are estimates and this system "
    "cannot tell you exactly where that starts. Read is_projection=false "
    "accordingly: it means only that the period was already over when the value "
    "was collected, NEVER that the number is a measurement. (3) The series runs "
    "to 2031: any period not yet finished when its value was stored is flagged "
    "is_projection and must never be scored or displayed as an observation. That "
    "flag is frozen with the row — a stored projection stays one until the IMF "
    "actually restates the number, so no figure here turns into an observation "
    "just because a year ended. quality_tier is 'estimate' rather than "
    "'official_mirror' for reasons (1) and (2)."
)


def _worldbank_spec(iso3: str, name_en: str, name_fa: str) -> SeriesSpec:
    return SeriesSpec(
        code=f"WB_CPI_{iso3}",
        name_en=f"{name_en} consumer price index (World Bank, 2010=100)",
        name_fa=f"شاخص قیمت مصرف‌کننده {name_fa} (بانک جهانی)",
        domain="macro",
        provider_code="worldbank",
        provider_series_id=f"{WORLDBANK_INDICATOR}/{iso3}",
        country=iso3,
        indicator=WORLDBANK_INDICATOR,
        frequency="A",
        calendar="gregorian",
        measure="index",
        # An annual average has no seasonal pattern left to adjust; 'nsa' is
        # the honest label, not a placeholder.
        seasonal_adjustment="nsa",
        base_period=WORLDBANK_BASE_PERIOD,
        quality_tier="official_mirror",
        quote_currency="INDEX",
        unit="index",
        decimals=2,
        revisable=True,
        # A World Bank rebase restates the level history in place; chaining is
        # not something this adapter is equipped to do, so it declares 'none'
        # rather than implying a splice it does not perform.
        splice_policy="none",
        notes=_WB_NOTE + (_WB_IRAN_NOTE if iso3 == "IRN" else ""),
    )


def _imf_spec(iso3: str, name_en: str, name_fa: str) -> SeriesSpec:
    return SeriesSpec(
        code=f"IMF_{IMF_INDICATOR}_{iso3}",
        name_en=f"{name_en} annual inflation, average consumer prices (IMF WEO)",
        name_fa=f"نرخ تورم سالانه {name_fa} (صندوق بین‌المللی پول)",
        domain="macro",
        provider_code="imf_weo",
        provider_series_id=f"{IMF_INDICATOR}/{iso3}",
        country=iso3,
        indicator=IMF_INDICATOR,
        frequency="A",
        calendar="gregorian",
        measure="yoy_pct",
        seasonal_adjustment="nsa",
        base_period="",  # a percent change has no base period
        quality_tier="estimate",
        quote_currency="PCT",
        unit="pct",
        decimals=1,  # the API publishes one decimal
        revisable=True,
        splice_policy="none",
        notes=_IMF_NOTE,
    )


# --- Statistical Centre of Iran, urban CPI ----------------------------------
#
# Verified 2026-09-09 against the workbook
#   ts_urban_140505-14050618165804.xlsx
#   sha256 b28e3375b772f37ef5e440ca6ba3378b33173226b0388507953f0c102d259114
# 293 monthly observations per series, Farvardin 1381 -> Mordad 1405, and all
# four series average EXACTLY 100.000000 across the twelve months of 1400,
# which is the stated base and is re-checked from the data itself on every
# ingest (app/economic/sci.py).

SCI_PROVIDER = "sci"
SCI_WORKBOOK = "ts_urban"
SCI_SHEET = "جدول 1"
SCI_BASE_PERIOD = "1400=100"

_SCI_NOTE = (
    "Statistical Centre of Iran, urban-household CPI, base 1400=100 (verified on "
    "every ingest: the twelve months of 1400 must average exactly 100). Monthly, "
    "on JALALI reference periods — each observation covers one Jalali month, and "
    "ref_period_start/end are that month's Gregorian span. Published around day "
    "10 of the following Jalali month; the publication timestamp is taken from "
    "SCI's own filename, so published_at is a real date here rather than NULL. "
    "NOT seasonally adjusted: SCI publishes no SA variant, and Nowruz and Ramadan "
    "shift against the Gregorian calendar, so standard seasonal factors do not "
    "apply. splice_policy is chain_growth because SCI restated the whole level "
    "history at the 1400 rebase and publishes no concordance — growth rates chain "
    "across a rebase, levels do not."
)

_SCI_HOUSING_CAVEAT = (
    " IMPORTANT: this is the housing component of a CONSUMPTION price index, and "
    "it is dominated by rent — over the full series it moves almost identically "
    "to the standalone rent index (95.6x vs 94.5x since 1381). It is NOT a house "
    "price index and must never be read as one. No public source publishes a "
    "Tehran or national house price series today: the Central Bank's monthly "
    "Tehran housing report ran Farvardin 1396 to Mordad 1403 and then stopped "
    "when the CBI lost access to the transaction registry."
)

_SCI_VEHICLES_CAVEAT = (
    " This is the CPI basket for vehicle purchase, which is quality-adjusted by "
    "construction and therefore smooths away exactly the factory-versus-market "
    "divergence an Iranian car buyer cares about. It is the only long-run "
    "official vehicle price index that exists; it is not a market quote. No "
    "source carries more than about five weeks of retrievable Iranian market car "
    "prices."
)

# (code, row label as printed by SCI, domain, name_en, name_fa, extra note)
_SCI_SERIES: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("SCI_CPI_URBAN", "شاخص كل", "macro",
     "Iran urban CPI, all items (SCI, 1400=100)",
     "شاخص قیمت مصرف‌کننده شهری، کل", ""),
    ("SCI_CPI_HOUSING", "مسكن", "housing",
     "Iran urban CPI, housing (SCI, 1400=100)",
     "شاخص قیمت مصرف‌کننده شهری، مسکن", _SCI_HOUSING_CAVEAT),
    ("SCI_CPI_RENT", "اجاره", "housing",
     "Iran urban CPI, rent (SCI, 1400=100)",
     "شاخص قیمت مصرف‌کننده شهری، اجاره", ""),
    ("SCI_CPI_VEHICLES", "071 - خريد وسايل نقليه", "auto",
     "Iran urban CPI, vehicle purchase (SCI, 1400=100)",
     "شاخص قیمت مصرف‌کننده شهری، خرید وسایل نقلیه", _SCI_VEHICLES_CAVEAT),
)


def _sci_spec(
    code: str, row_label: str, domain: str, name_en: str, name_fa: str, extra: str
) -> SeriesSpec:
    return SeriesSpec(
        code=code,
        name_en=name_en,
        name_fa=name_fa,
        domain=domain,
        provider_code=SCI_PROVIDER,
        # The source's own key for this row: workbook, sheet, and the label as
        # SCI prints it. Kept in the SOURCE's orthography (Arabic kaf/yeh) so it
        # can be grepped against a downloaded file; app/economic/sci.py
        # normalises both sides rather than assuming either form.
        provider_series_id=f"{SCI_WORKBOOK}:{SCI_SHEET}:{row_label}",
        country="IRN",
        indicator=row_label,
        frequency="M",
        calendar="jalali",
        measure="index",
        seasonal_adjustment="nsa",
        base_period=SCI_BASE_PERIOD,
        quality_tier="official",
        quote_currency="INDEX",
        unit="index",
        decimals=2,
        revisable=True,
        splice_policy="chain_growth",
        notes=_SCI_NOTE + extra,
        # ~40 days observed once; one measurement is not a characterisation.
        publication_lag_days=None,
    )


def sci_specs() -> list[SeriesSpec]:
    """The four SCI urban-CPI rows this system extracts."""
    return [_sci_spec(*row) for row in _SCI_SERIES]


# --- Central Bank of Iran, monetary aggregates -------------------------------
#
# Verified 2026-10-11 against every Excel edition CBI publishes from Tir 1399
# to Esfand 1404 that was downloaded (48 of 69, plus the Esfand 1400 and 1402
# editions), e.g. 140412.xls, sha256 648af2335b07d1b8...: base money 21,959.3
# and M2 155,812.2 thousand billion rials at the end of Esfand 1404, M2 = M1 +
# quasi-money and base money (sources) = base money (uses) in every edition.
# app/economic/cbi.py re-checks all of that on every ingest and refuses the
# whole workbook when any of it fails.

CBI_PROVIDER = "cbi"
# The publication the monthly workbooks belong to: گزیده آمارهای اقتصادی (Selected
# Economic Indicators), its monetary and banking part.
CBI_PUBLICATION = "sei_monetary"
CBI_UNIT = "thousand_billion_rial"      # هزار میلیارد ریال = 1e12 rial
BASE_MONEY_CODE = "CBI_BASE_MONEY"

_CBI_NOTE = (
    "Central Bank of Iran, Selected Economic Indicators (گزیده آمارهای اقتصادی), "
    "monetary part. An END-OF-MONTH STOCK, not a monthly average: each value is "
    "the balance at the end of the Jalali month in ref_period_label, and "
    "ref_period_start/end are that month's Gregorian span. Stored EXACTLY as "
    "printed, in thousand billion rials (هزار میلیارد ریال = 1e12 rial); divide by "
    "10 for همت (1e12 toman) — the conversion is for display only. Sources: "
    "the monthly Excel editions (Tir 1399 onwards; each prints the same month "
    "last year, last Esfand and the current month, so months from Tir 1398 plus "
    "Esfand 1397 are covered) and, for the quarter-ends no Excel edition carries "
    "(1352 to Khordad 1398, Esfand 1397 excepted), the CBI time-series viewer's "
    "values (four decimals rather than one; published_at NULL, available_at the "
    "day they were ingested). CBI states no publication "
    "date: published_at is the workbook's own last-saved timestamp, a lower "
    "bound. Editions arrive three weeks to five and a half months after the "
    "month ends (21 to 164 days measured), often in batches. A value restated "
    "in a later edition is a new vintage beside the print it revises. NOT "
    "seasonally adjusted."
)

_CBI_M2_CAVEAT = (
    " CBI states that 2.7 points of 1400's M2 growth come only from Mehr Eqtesad "
    "bank's balance sheet being added to Sepah's (a coverage change with no "
    "monetary effect); the level is continuous as printed and no adjustment is "
    "made here."
)

_CBI_BASE_MONEY_CAVEAT = (
    " BREAK: CBI redefined base money — its foreign assets revalued in 1402 and "
    "the components reclassified to the IMF's MFSMCG 2016 — for months from "
    "Esfand 1399 / 1400 on, and restated those months in later editions (Esfand "
    "1400: 6,039.7 -> 5,981.1; Esfand 1401: 8,599.9 -> 8,548.0). Every row "
    "carries method 'old' or 'revised' and growth is never computed across the "
    "two; levels on different methods are not comparable, hence chain_growth. "
    "Read as the SOURCES side (پایه پولی از سمت منابع), checked against the uses "
    "side on every ingest. The newest editions label it «کاملا مقدماتی» (fully "
    "preliminary)."
)

# (code, row label as printed by CBI, name_en, name_fa, splice_policy, extra note)
_CBI_SERIES: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("CBI_LIQUIDITY", "نقدينگي برحسب عوامل موثر بر عرضه",
     "Liquidity (M2)", "نقدینگی", "none", _CBI_M2_CAVEAT),
    (BASE_MONEY_CODE, "پايه پولي از سمت منابع",
     "Base money", "پایه پولی", "chain_growth", _CBI_BASE_MONEY_CAVEAT),
    ("CBI_MONEY_M1", "پول",
     "Money (M1)", "پول", "none", " M1 = currency with the public + demand deposits."),
    ("CBI_QUASI_MONEY", "شبه پول",
     "Quasi-money", "شبه پول", "none",
     " Quasi-money = M2 - M1: savings, term and other non-demand deposits."),
)


def _cbi_spec(
    code: str, row_label: str, name_en: str, name_fa: str, splice: str, extra: str
) -> SeriesSpec:
    return SeriesSpec(
        code=code,
        name_en=name_en,
        name_fa=name_fa,
        domain="macro",
        provider_code=CBI_PROVIDER,
        # The publication and the row label as CBI prints it, kept in the
        # SOURCE's orthography (Arabic yeh) so it can be grepped against a
        # downloaded workbook; app/economic/cbi.py normalises both sides. Not
        # the sheet name: CBI has called that sheet «Sheet1», «متغیرهای عمده»
        # and «متغیرهای پولی».
        provider_series_id=f"{CBI_PUBLICATION}:{row_label}",
        country="IRN",
        indicator=row_label,
        frequency="M",
        calendar="jalali",
        measure="level",
        seasonal_adjustment="nsa",
        base_period="",          # a stock in currency units has no base
        quality_tier="official",
        quote_currency="IRR",
        unit=CBI_UNIT,
        decimals=1,              # CBI prints one decimal
        revisable=True,
        splice_policy=splice,
        notes=_CBI_NOTE + extra,
        # 21 to 164 days measured (median 61; 53 to 96 by year); not a
        # characterisation.
        publication_lag_days=None,
    )


def cbi_specs() -> list[SeriesSpec]:
    """The four CBI monetary aggregates this system extracts."""
    return [_cbi_spec(*row) for row in _CBI_SERIES]


# Providers whose series arrive as an archived DOCUMENT rather than a poll of
# an API, with where each one is ingested. The scheduled economic job reads
# this to report them as document-ingested instead of attempting a fetch it
# has no adapter for and recording a provider failure every tick.
#
# * sci: amar.org.ir is unreachable from the production host (migration 0027
#   records the diagnosis), so the workbook is fetched elsewhere and posted.
# * cbi: www.cbi.ir IS reachable, but its numbers come in a workbook that is
#   parsed, cross-checked and archived as a whole (migration 0034), by its own
#   scheduled job; the quarterly history comes from a viewer the host cannot
#   reach, fetched off-server and posted.
DOCUMENT_INGEST_ROUTES: dict[str, str] = {
    SCI_PROVIDER: (
        "POST /internal/economic/sci-cpi with a workbook fetched by "
        "scripts/sci_fetch.py"
    ),
    CBI_PROVIDER: (
        "POST /internal/economic/cbi-monetary (the Go job cbi-monetary, "
        "SCHEDULE_CBI_MONETARY_CRON) for the monthly workbooks, and "
        "POST /internal/economic/cbi-history fed by scripts/cbi_history_fetch.py "
        "for the quarterly history"
    ),
}
DOCUMENT_INGEST_PROVIDERS = frozenset(DOCUMENT_INGEST_ROUTES)


CATALOG: tuple[SeriesSpec, ...] = tuple(
    [_worldbank_spec(*country) for country in _COUNTRIES]
    + [_imf_spec(*country) for country in _COUNTRIES]
    + sci_specs()
    + cbi_specs()
)

_BY_CODE: dict[str, SeriesSpec] = {spec.code: spec for spec in CATALOG}


def spec_for_code(code: str) -> SeriesSpec:
    """The entry for ``code``.

    Raises :class:`KeyError` for an unknown code — an unrecognised series is
    refused, never silently substituted or skipped.
    """
    return _BY_CODE[code]


def enabled_specs(codes: Optional[Sequence[str]] = None) -> list[SeriesSpec]:
    """Catalog entries to ingest.

    ``codes`` empty/None selects every enabled entry.  When codes ARE given
    every one of them must exist in the catalog; an unknown code raises
    :class:`ValueError` so a caller's typo is refused rather than quietly
    ingesting a shorter list than they asked for.  A named-but-disabled entry
    is returned so the caller can report it as skipped and say why.
    """
    if not codes:
        return [spec for spec in CATALOG if spec.enabled]
    unknown = [code for code in codes if code not in _BY_CODE]
    if unknown:
        raise ValueError(
            "This series is not in the economic catalog: " + ", ".join(sorted(unknown))
        )
    seen: set[str] = set()
    out: list[SeriesSpec] = []
    for code in codes:
        if code in seen:
            continue
        seen.add(code)
        out.append(_BY_CODE[code])
    return out


def codes_for_provider(provider_code: str, specs: Optional[Iterable[SeriesSpec]] = None) -> list[str]:
    """Codes served by one provider (used by tests and operational tooling)."""
    return [
        spec.code
        for spec in (specs if specs is not None else CATALOG)
        if spec.provider_code == provider_code
    ]
