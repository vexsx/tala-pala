"""Economic (macro) subsystem: catalog, bitemporal store, provider contract.

Why this is a subsystem and not a few more rows in ``prices``: a macro number
has three times that all differ — the period it describes, the moment it was
published, and the revision it belongs to — and ``prices`` (one value, one
instant, never revised) can only hold one of them.  Migration 0024 states the
full argument; this package is the Python half of it.

**The provider contract lives here, not in app/providers/base.py.**
:class:`app.providers.base.Provider` is an adapter for *quotes*: its
``fetch()`` returns :class:`~app.providers.base.Observation` records, each one
value observed at one instant, which is the right shape for a gold price and
the wrong shape for a series of dated periods with revision flags.  Rather
than widen ``Observation`` until it can pretend to be both — which would make
every price adapter carry period and projection fields it can never fill —
:class:`EconomicSeriesProvider` subclasses ``Provider`` for its HTTP
conventions (honest User-Agent, configured timeout, courtesy delay, bounded
retry, never bypassing an auth wall) and declares its own ``fetch_detail``
returning :class:`SeriesFetch` — a list of :class:`SeriesPoint` plus what the
payload said about itself.  ``app/news/__init__.py`` did exactly this for
``NewsProvider``/``ArticleRecord``; this is the same decision for the same
reason.

Tables are mirrored in :mod:`app.db` (Python never creates or alters them).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

from ..db import ensure_utc
from ..providers.base import Observation, Provider, ProviderError
from .catalog import CATALOG, SeriesSpec, enabled_specs, spec_for_code

__all__ = [
    "CATALOG",
    "EconomicSeriesProvider",
    "SeriesFetch",
    "SeriesPoint",
    "SeriesSpec",
    "annual_period",
    "enabled_specs",
    "is_incomplete_annual_period",
    "spec_for_code",
]


@dataclass(frozen=True)
class SeriesPoint:
    """One dated value as a source states it, before any storage decision.

    Deliberately carries no ``available_at``: when THIS system could first
    know a value is a fact about the ingest, not about the payload, and the
    job stamps it once for the whole fetch (see :mod:`app.economic.store`).
    """

    provider_code: str
    code: str                      # canonical series code, e.g. 'WB_CPI_IRN'
    ref_period_start: date
    ref_period_end: date
    ref_period_label: str          # '2025', '2025-Q2', '1405-06'
    value: float
    # A figure for a period that has not finished is not a measurement of it.
    is_projection: bool = False
    is_nowcast: bool = False
    # Only ever set when the SOURCE states a publication moment for THIS
    # observation.  Neither P0 provider does, so it stays None: a dataset-wide
    # "last updated" is not a publication date for a 1960 datapoint.
    published_at: Optional[datetime] = None
    raw_payload: Optional[dict] = field(default=None, hash=False)


@dataclass(frozen=True)
class SeriesFetch:
    """One provider fetch: the points, and what the payload said about itself.

    ``skipped_null`` and ``meta`` exist so the ingest report can state the
    shape of what arrived — how many periods the source has no value for, when
    the source says the dataset was last updated — rather than only how many
    rows were written. A count of stored values with no account of what was
    dropped is not provenance.
    """

    points: list[SeriesPoint]
    skipped_null: int = 0
    meta: dict = field(default_factory=dict)


def annual_period(year: int) -> tuple[date, date, str]:
    """(start, end, label) of a Gregorian calendar year."""
    return date(year, 1, 1), date(year, 12, 31), str(year)


def is_incomplete_annual_period(year: int, now: datetime) -> bool:
    """True when ``year`` has not finished as of ``now`` (UTC).

    The current year counts: on 2026-09-08 the 2026 figure describes a year
    that is 4 months from over, so whatever a source prints for it is a
    forecast or a partial, never an annual observation.  Evaluated in UTC
    because every stored timestamp in this system is UTC.

    **Read what this can and cannot establish.**  It answers exactly one
    question — "had this period finished when we fetched it?" — from the clock,
    because neither P0 source states the answer.  For IMF WEO in particular the
    DataMapper API does not expose the WEO "estimates start after" marker, so a
    resulting ``is_projection = False`` means ONLY *the period was complete when
    we fetched this value*.  It emphatically does not mean *this is a
    measurement*: the IMF publishes staff estimates for years already finished,
    and this API will not say which.  That residue is carried by the series
    itself — ``quality_tier = 'estimate'`` and the catalog note that ships with
    every one of these numbers — and NOT by a heuristic here.  A rule like
    "assume the last two years are estimates" would be a fabricated boundary
    presented as provenance, which is worse than a limit stated plainly.

    Because the answer depends on the clock, it is a fact about the moment of a
    fetch, and :func:`app.economic.store.record_observation` freezes it into the
    row it writes: a period stored as a projection can never become an
    observation just because the year rolled over, and only a genuine change in
    the source's value produces a new row (which then carries this flag as
    computed at that later moment).
    """
    return year >= ensure_utc(now).year


class EconomicSeriesProvider(Provider):
    """Base for adapters that deliver a dated series instead of one quote."""

    category: str = "global_macro"

    @abc.abstractmethod
    def fetch_detail(
        self, spec: SeriesSpec, now: Optional[datetime] = None
    ) -> SeriesFetch:
        """Fetch every period the source publishes for ``spec``.

        Raises :class:`~app.providers.base.ProviderError` when the payload
        cannot be trusted to be the requested series.  ``now`` fixes the
        boundary between finished and unfinished periods so a caller (and a
        test) can pin it; it defaults to the current UTC time.
        """

    def fetch_series(
        self, spec: SeriesSpec, now: Optional[datetime] = None
    ) -> list[SeriesPoint]:
        """Just the points, for callers that need nothing else."""
        return self.fetch_detail(spec, now=now).points

    def fetch(self) -> list[Observation]:
        """Economic providers never produce price observations.

        Raising rather than returning ``[]`` makes an accidental wiring into
        the collect job loud instead of silently contributing nothing — the
        same choice ``NewsProvider`` makes.
        """
        raise ProviderError(f"{self.code}: economic series provider, not a price provider")
