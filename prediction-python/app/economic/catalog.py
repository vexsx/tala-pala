"""The declared catalog of economic series this system ingests.

One entry per series, and a series is not in this file unless its endpoint was
probed and its shape verified — every entry below was fetched live on
2026-09-08.  The catalog is the source of truth for the ``instruments`` and
``economic_series`` rows :func:`app.economic.store.ensure_series` writes; the
database rows are a projection of it, never the other way round.

Two families, both annual, both revisable, and both chosen because they are
the cheapest honest test of the vintage machinery migration 0024 introduced:

* ``WB_CPI_*``   — World Bank ``FP.CPI.TOTL``, a CPI *index* (2010 = 100).
* ``IMF_PCPIPCH_*`` — IMF WEO ``PCPIPCH``, annual average inflation in percent.

They are NOT two views of one series and must never be spliced together: one
is a level, the other a rate, and they disagree about Iran by a wide margin
because they are measuring different things from different sources.

``measure`` is part of series identity, not a display option (0024 spells out
why), which is why the same country appears twice with two codes.

A note on what is deliberately absent: nothing from the Statistical Centre of
Iran or the Central Bank of Iran is here yet.  Those are the authoritative
Iranian series, they are Jalali-dated, and SCI publishes them as PDFs whose
older paths 404 — that needs the ``source_documents`` archive path to work
first, and P0 does not have it.  What ships here is a mirror and an estimate,
and both entries say so in their own notes.
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


CATALOG: tuple[SeriesSpec, ...] = tuple(
    [_worldbank_spec(*country) for country in _COUNTRIES]
    + [_imf_spec(*country) for country in _COUNTRIES]
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
