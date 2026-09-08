"""World Bank Indicators API adapter (annual macro series).

Endpoint, verified live 2026-09-08::

    https://api.worldbank.org/v2/country/IRN/indicator/FP.CPI.TOTL
        ?format=json&per_page=200

The response is a TWO-ELEMENT envelope — ``[metadata, rows]`` — not a list of
rows, and the metadata half is the only place a page count or the dataset's
``lastupdated`` appears::

    [{"page":1,"pages":1,"per_page":200,"total":66,"lastupdated":"2026-07-13"},
     [{"indicator":{"id":"FP.CPI.TOTL",...},"countryiso3code":"IRN",
       "date":"2025","value":4030.3356899712,...}, ...]]

An error is shaped differently again: a ONE-element list whose single object
carries ``message``.  Treating that as the envelope would index past the end
of the list and report a network-shaped failure for what is really "you asked
for an indicator that does not exist", so it is detected and reported as
itself.

Two behaviours worth stating because they are decisions, not accidents:

*A null value is skipped, never zero-filled.*  Coverage gaps are real and
large — the UAE CPI series is missing 47 of its 66 years and Russia 32
(measured 2026-09-08) — and a zero in a price index is not a gap, it is a
catastrophic deflation that never happened.

*``published_at`` stays NULL.*  The API states one ``lastupdated`` for the
whole indicator dataset.  That is not the publication date of the 1960
datapoint, and stamping every observation with it would manufacture a
provenance claim the source never made.  It is carried in the raw payload and
in the fetch report instead.

*The base period is verified against the payload, never assumed.*  Every row
states it in the indicator name — ``"Consumer price index (2010 = 100)"`` — and
the catalog declares the same string as ``base_period``.  A rebase restates the
whole level history against a new year, so if those two ever disagree the
series is REFUSED (see :func:`base_period_in_name`): storing 2021-based levels
under a ``2010=100`` label would leave no as-of read able to tell the two bases
apart, and adopting the new base automatically would silently splice two
incomparable level series — a decision that belongs to a human, along with
``splice_policy``.

This class subclasses :class:`app.economic.EconomicSeriesProvider` — a series
of dated periods is not the shape ``Provider.fetch()`` describes; see that
module's docstring for why the price ABC was not widened to cover it.
"""
from __future__ import annotations

import logging
import re
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

BASE_URL = "https://api.worldbank.org/v2"
# 66 annual rows per country today (1960..2025); 200 leaves room to grow and
# still returns a single page, so no paging loop is needed. ``pages`` is
# checked anyway and a second page is reported rather than silently dropped.
PER_PAGE = 200


def series_url(country: str, indicator: str) -> str:
    return f"{BASE_URL}/country/{country}/indicator/{indicator}"


def _envelope_error(payload: Any) -> Optional[str]:
    """The API's own error message, when the payload is an error envelope."""
    if isinstance(payload, dict):
        messages = payload.get("message")
    elif isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        messages = payload[0].get("message")
    else:
        return None
    if not messages:
        return None
    if isinstance(messages, list):
        parts = [
            "; ".join(
                str(item.get(key, "")).strip()
                for key in ("key", "value")
                if str(item.get(key, "")).strip()
            )
            for item in messages
            if isinstance(item, dict)
        ]
        return " | ".join(part for part in parts if part) or "unspecified error"
    return str(messages)


# "Consumer price index (2010 = 100)" -> "2010=100".  Also matches a base
# stated without parentheses or as a span ("2014-2016 = 100"), because the
# thing that must be caught is a CHANGED base year, not a changed sentence.
_BASE_PERIOD_RE = re.compile(r"(\d{4}(?:\s*-\s*\d{2,4})?)\s*=\s*(\d+(?:\.\d+)?)")


def base_period_in_name(indicator_name: str) -> str:
    """The base the indicator name states, normalised ('2010=100'), or ''.

    Empty means the name does not state a base at all — which is NOT evidence
    of a rebase, so the caller warns and proceeds rather than refusing.  A
    parsed base that DISAGREES with the catalog is a different matter entirely.
    """
    match = _BASE_PERIOD_RE.search(indicator_name or "")
    if match is None:
        return ""
    period = re.sub(r"\s+", "", match.group(1))
    return f"{period}={match.group(2)}"


def _check_base_period(spec: SeriesSpec, indicator_name: str) -> str:
    """Refuse the series when the payload's base is not the catalog's.

    The World Bank rebases this indicator periodically and restates every
    level when it does.  The catalog constant is a claim about what the stored
    numbers mean; a payload that states a different base means the claim is now
    false, and the honest response is to stop rather than to write restated
    levels under the old label or to adopt the new one behind the operator's
    back (a rebase needs a decision about ``splice_policy``, and this adapter
    performs no splice).
    """
    if not spec.base_period:
        return ""
    stated = base_period_in_name(indicator_name)
    if not stated:
        # Nothing to compare against — reported once by the caller, after the
        # loop, and not refused: the API dropping the base from a display
        # string is not the same event as a rebase.
        return ""
    if stated.replace(" ", "") != spec.base_period.replace(" ", ""):
        raise ProviderError(
            f"worldbank: {spec.provider_series_id} is published on base "
            f"{stated!r} but the catalog declares {spec.base_period!r} "
            f"(indicator name: {indicator_name!r}). This looks like a World "
            "Bank rebase: the level history has been restated against a new "
            "year, so these values are not comparable with the ones already "
            "stored. Refusing the series. Fixing it is a human decision — "
            "update base_period and choose a splice_policy for the break."
        )
    return stated


def parse_series(
    payload: Any, spec: SeriesSpec, now: Optional[datetime] = None
) -> SeriesFetch:
    """Parse the ``[metadata, rows]`` envelope into points for ``spec``.

    Raises :class:`ProviderError` when the payload is not the requested
    series — a value that cannot state its provenance does not ship, so a
    payload for another country or another indicator is refused rather than
    stored under this series' code.
    """
    now = now or utcnow()

    message = _envelope_error(payload)
    if message is not None:
        raise ProviderError(f"worldbank: API error for {spec.provider_series_id}: {message}")
    if not isinstance(payload, list) or len(payload) < 2:
        raise ProviderError(
            "worldbank: expected the [metadata, rows] envelope for "
            f"{spec.provider_series_id}, got {type(payload).__name__} "
            f"of length {len(payload) if isinstance(payload, list) else 'n/a'}"
        )

    meta = payload[0] if isinstance(payload[0], dict) else {}
    rows = payload[1]
    if rows is None:
        raise ProviderError(
            f"worldbank: no rows for {spec.provider_series_id} "
            f"(the API reports total={meta.get('total')})"
        )
    if not isinstance(rows, list):
        raise ProviderError(
            f"worldbank: rows for {spec.provider_series_id} are "
            f"{type(rows).__name__}, not a list"
        )
    if not rows:
        # Well-formed and empty: a page past the end, or an indicator the
        # country has no coverage for at all.
        raise ProviderError(
            f"worldbank: {spec.provider_series_id} returned an empty row set"
        )

    points: list[SeriesPoint] = []
    skipped_null = 0
    stated_base = ""
    for row in rows:
        if not isinstance(row, dict):
            continue
        iso3 = str(row.get("countryiso3code") or "").strip().upper()
        indicator = row.get("indicator") or {}
        indicator_id = str(indicator.get("id") or "").strip()
        indicator_name = str(indicator.get("value") or "").strip()
        if iso3 and iso3 != spec.country.upper():
            raise ProviderError(
                f"worldbank: payload for {spec.provider_series_id} contains a row "
                f"for {iso3}; refusing to store another country's value"
            )
        if indicator_id and indicator_id != spec.indicator:
            raise ProviderError(
                f"worldbank: payload for {spec.provider_series_id} contains "
                f"indicator {indicator_id}; refusing to store another series' value"
            )
        # Identity first, then what the numbers MEAN: every row repeats the
        # indicator name and the base is stated inside it. Checked per row
        # rather than once, because a payload that changed base halfway down is
        # exactly as unstorable as one that changed at the top.
        stated_base = _check_base_period(spec, indicator_name) or stated_base

        raw_date = str(row.get("date") or "").strip()
        if not raw_date.isdigit() or len(raw_date) != 4:
            # Monthly/quarterly WB series use 2025M07 / 2025Q2. This adapter
            # only claims annual periods; anything else is reported, not
            # guessed at.
            log.warning(
                "worldbank: %s: skipping non-annual period %r",
                spec.provider_series_id, raw_date,
            )
            continue
        value = row.get("value")
        if value is None:
            # A gap in the source is a gap here. Never zero-filled.
            skipped_null += 1
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            log.warning(
                "worldbank: %s: skipping unparseable value %r for %s",
                spec.provider_series_id, value, raw_date,
            )
            continue

        year = int(raw_date)
        start, end, label = annual_period(year)
        points.append(
            SeriesPoint(
                provider_code="worldbank",
                code=spec.code,
                ref_period_start=start,
                ref_period_end=end,
                ref_period_label=label,
                value=numeric,
                # The World Bank publishes finished years only (latest is 2025
                # as of 2026-09-08), so this never fires today. If it ever
                # returns the running year, that figure cannot be a complete
                # annual measurement, and flagging it is the only treatment
                # that does not present an unfinished year as an observation.
                is_projection=is_incomplete_annual_period(year, now),
                is_nowcast=False,
                # See the module docstring: the dataset-wide 'lastupdated' is
                # not this observation's publication moment.
                published_at=None,
                raw_payload={
                    "date": raw_date,
                    "value": value,
                    "countryiso3code": iso3,
                    "indicator": indicator_id,
                    "obs_status": row.get("obs_status", ""),
                    "decimal": row.get("decimal"),
                    "lastupdated": str(meta.get("lastupdated") or ""),
                },
            )
        )

    if spec.base_period and not stated_base:
        # Said once for the series, not once per row. It reaches the Issues tab
        # through the logging bridge, where an operator can decide whether the
        # World Bank changed a label or changed a base.
        log.warning(
            "worldbank: %s: no row states a base period, so the catalog's %r "
            "could not be verified against this payload",
            spec.provider_series_id, spec.base_period,
        )
    if not points:
        raise ProviderError(
            f"worldbank: {spec.provider_series_id} returned no usable "
            f"observations ({skipped_null} of {len(rows)} rows were null)"
        )
    points.sort(key=lambda point: point.ref_period_start)
    return SeriesFetch(
        points=points,
        skipped_null=skipped_null,
        meta={
            # Dataset-wide, NOT a per-observation publication timestamp — it is
            # reported so the ingest can say when the source last touched the
            # dataset, and it never reaches published_at.
            "last_updated": str(meta.get("lastupdated") or ""),
            "total": int(meta.get("total") or len(rows)),
            "pages": int(meta.get("pages") or 1),
            "source_id": str(meta.get("sourceid") or ""),
            # The base the PAYLOAD stated, checked against the catalog above.
            # '' means the name stated none (a warning was logged); it is never
            # the catalog's value echoed back, which would prove nothing.
            "base_period": stated_base,
        },
    )


class WorldBankProvider(EconomicSeriesProvider):
    code = "worldbank"
    category = "global_macro"

    def fetch_detail(
        self, spec: SeriesSpec, now: Optional[datetime] = None
    ) -> SeriesFetch:
        """Fetch + parse, keeping the envelope's own metadata."""
        if spec.provider_code != self.code:
            raise ProviderError(
                f"worldbank: {spec.code} is served by {spec.provider_code!r}, "
                "not by this provider"
            )
        payload = self._get_json(
            series_url(spec.country, spec.indicator),
            params={"format": "json", "per_page": PER_PAGE},
        )
        result = parse_series(payload, spec, now=now)
        if int(result.meta.get("pages") or 1) > 1:
            # Not fatal (page 1 holds the recent history that matters) but it
            # means this adapter is no longer seeing the whole series.
            log.warning(
                "worldbank: %s returned %s pages at per_page=%d; only page 1 was read",
                spec.provider_series_id, result.meta.get("pages"), PER_PAGE,
            )
        return result
