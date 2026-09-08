"""IMF DataMapper (World Economic Outlook) adapter.

Endpoint, verified live 2026-09-08::

    https://www.imf.org/external/datamapper/api/v1/PCPIPCH/IRN

Response shape::

    {"values": {"PCPIPCH": {"IRN": {"1980": 20.6, ..., "2031": 25}, ...}},
     "api": {"version": "1", "output-method": "json"}}

Three findings from probing it, each of which changes what this adapter must
do:

*The country path segment is ignored.*  ``/PCPIPCH/IRN`` returns all 228
economies (121 KB), and so does ``/PCPIPCH/ZZZ`` — a nonexistent code returns
the same full payload rather than an error.  An adapter that trusted the URL
and took "the first country in the response" would have stored Sudan's
inflation as Iran's, silently, forever.  So the country key is selected here
by name, and a payload that does not contain it is REFUSED rather than
answered from whatever else arrived.

*The series runs to 2031.*  History and projections live in one object with
nothing to tell them apart.  Any period from the current year onward is
flagged ``is_projection``: the current year has not finished, so its figure is
a forecast, not a measurement.  That flag is decided once, at the moment of the
fetch that first stores a value, and the store then freezes it with the row —
so the same 68.9 for 2026, re-fetched on 2027-01-02, does not turn into a
measured 2026 inflation rate because the calendar advanced.  (The IMF publishes
no reported 2026 figure until the April 2027 WEO, and the API would not tell us
if it had.)

*The WEO "estimates start after" flag is not exposed.*  In the published WEO
database each country carries the last year of reported data; the DataMapper
API omits it.  So some values before the current year are staff estimates too,
and this API cannot say which.  The consequence must be stated the right way
round: ``is_projection = False`` on one of these rows means ONLY "the period
was over when we fetched this", never "this is a measurement".  What carries
the rest is the series itself — ``quality_tier = 'estimate'`` and the catalog
note that ships with every number.  This adapter does not guess where reported
data ends (no "the last two years are estimates" rule): an invented boundary
would read as provenance while being fiction, which is worse than a limit
stated plainly.

There is no publication timestamp anywhere in the payload, per observation or
otherwise, so ``published_at`` is None and ``available_at`` (set by the job to
the collection instant) carries the point-in-time claim.

This class subclasses :class:`app.economic.EconomicSeriesProvider`; see that
module's docstring for why the price ``Provider`` ABC was not widened.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

from ..db import utcnow
from ..economic import (
    EconomicSeriesProvider,
    SeriesFetch,
    SeriesPoint,
    SeriesSpec,
    annual_period,
    is_incomplete_annual_period,
)
from .base import ProviderError

log = logging.getLogger(__name__)

BASE_URL = "https://www.imf.org/external/datamapper/api/v1"


def series_url(country: str, indicator: str) -> str:
    return f"{BASE_URL}/{indicator}/{country}"


def parse_series(
    payload: Any, spec: SeriesSpec, now: Optional[datetime] = None
) -> SeriesFetch:
    """Parse ``{"values": {IND: {COUNTRY: {year: value}}}}`` for ``spec``."""
    now = now or utcnow()

    if not isinstance(payload, dict):
        raise ProviderError(
            f"imf_weo: expected a JSON object for {spec.provider_series_id}, "
            f"got {type(payload).__name__}"
        )
    values = payload.get("values")
    if not isinstance(values, dict):
        raise ProviderError(
            f"imf_weo: payload for {spec.provider_series_id} has no 'values' object"
        )
    by_country = values.get(spec.indicator)
    if not isinstance(by_country, dict):
        raise ProviderError(
            f"imf_weo: payload has no '{spec.indicator}' indicator "
            f"(it carries: {', '.join(sorted(map(str, values))) or 'nothing'})"
        )
    country_series = by_country.get(spec.country)
    if not isinstance(country_series, dict) or not country_series:
        # The endpoint ignores its own country filter, so an absent key means
        # the IMF does not publish this economy — not that we should fall back
        # to whatever else the payload contained.
        raise ProviderError(
            f"imf_weo: {spec.indicator} carries no data for {spec.country} "
            f"({len(by_country)} economies were returned)"
        )

    points: list[SeriesPoint] = []
    skipped_null = 0
    projection_from: Optional[int] = None
    for raw_year, value in country_series.items():
        year_text = str(raw_year).strip()
        if not year_text.isdigit() or len(year_text) != 4:
            log.warning(
                "imf_weo: %s: skipping non-annual key %r",
                spec.provider_series_id, raw_year,
            )
            continue
        if value is None:
            skipped_null += 1
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            log.warning(
                "imf_weo: %s: skipping unparseable value %r for %s",
                spec.provider_series_id, value, year_text,
            )
            continue

        year = int(year_text)
        start, end, label = annual_period(year)
        is_projection = is_incomplete_annual_period(year, now)
        if is_projection and (projection_from is None or year < projection_from):
            projection_from = year
        points.append(
            SeriesPoint(
                provider_code="imf_weo",
                code=spec.code,
                ref_period_start=start,
                ref_period_end=end,
                ref_period_label=label,
                value=numeric,
                is_projection=is_projection,
                is_nowcast=False,
                published_at=None,   # the payload states no publication moment
                raw_payload={
                    "indicator": spec.indicator,
                    "country": spec.country,
                    "year": year_text,
                    "value": value,
                },
            )
        )

    if not points:
        raise ProviderError(
            f"imf_weo: {spec.provider_series_id} returned no usable observations"
        )
    points.sort(key=lambda point: point.ref_period_start)
    return SeriesFetch(
        points=points,
        skipped_null=skipped_null,
        meta={
            # How many economies the payload actually held: the endpoint
            # ignores its country filter, and recording 228 here is the
            # evidence that the country key was selected, not assumed.
            "countries_returned": len(by_country),
            "projection_from": projection_from,
        },
    )


class IMFWeoProvider(EconomicSeriesProvider):
    code = "imf_weo"
    category = "global_macro"

    def fetch_detail(
        self, spec: SeriesSpec, now: Optional[datetime] = None
    ) -> SeriesFetch:
        if spec.provider_code != self.code:
            raise ProviderError(
                f"imf_weo: {spec.code} is served by {spec.provider_code!r}, "
                "not by this provider"
            )
        # The country segment is requested even though the API ignores it: it
        # is the documented endpoint, it costs nothing, and if the IMF ever
        # honours it the adapter needs no change (it selects the key anyway).
        payload = self._get_json(series_url(spec.country, spec.indicator))
        return parse_series(payload, spec, now=now)
