"""Scheduler-callable ingest of the declared economic catalog.

The orchestration half only: what a series IS lives in
``app/economic/catalog.py``, how a payload is parsed lives in the two provider
adapters, and every storage decision — first print, unchanged, revision — lives
in ``app/economic/store.py``.  What is added here is what only matters once
several series are ingested repeatedly on a schedule.

*One series' failure is its own.*  Each series is fetched outside any
transaction and written inside its own, so a provider outage, an unexpected
payload or a constraint violation costs exactly that series.  The others are
still ingested and the pass still returns counts; failures are logged through
the standard logger, which the ``app_issues`` bridge mirrors into the Issues
tab.

*``available_at`` is stamped once per fetch.*  It is the instant the payload
arrived — the first moment this system could have known any value in it.  The
whole payload shares it because that is literally true of every value in it;
see ``app/economic/store.py`` for why collection time is the only defensible
answer for these two sources.

*Provider health is recorded ONCE per provider per pass, not once per series.*
``worldbank`` and ``imf_weo`` are rows in ``data_providers`` — one row each,
backing seven series each — so recording health inside the per-series loop
would let the seventh series overwrite what the first six reported:
``record_success`` zeroes ``consecutive_failures`` and NULLs ``last_error``,
and a provider that failed six times out of seven would end the pass looking
untouched.  So failures are aggregated per ``provider_code`` across the whole
pass and written at the end, and a provider counts as successful only if it had
no failures at all.  Health is a statement about the provider, and "it answered
once" is not one.

*A pass in which EVERY series failed is a failed job.*  Partial failure is
normal (one provider down, the other fine) and stays a success with an errors
list.  But a pass that ingested nothing has told us nothing, and returning 200
for it would have the Go scheduler record a successful run, hiding a total
outage behind a green job history.  So :func:`ingest_economic` raises
:class:`EconomicIngestFailed` — carrying the same report it would have
returned — and the endpoint turns that into a 502.

*No source document is archived, and none is invented.*  0024's
``source_documents`` table exists for the sources that need it — the SCI
publishes its CPI as PDFs whose older paths 404 — but both P0 providers are
JSON APIs at stable URLs, so nothing here writes that table and every
``source_document_id`` stays NULL.  A row claiming an archived artifact that
does not exist would be worse than no row.

Nothing ingested here reaches model input.  These are annual series with 66
observations each; they are reference data for a reader, and the machinery
they exercise (vintages, point-in-time reads) is the machinery the Iranian
monthly series will need when they land.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional, Sequence

from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import utcnow
from ..economic import (
    EconomicSeriesProvider,
    SeriesPoint,
    SeriesSpec,
    enabled_specs,
)
from ..economic.store import (
    STATUS_INSERTED,
    STATUS_RACED,
    STATUS_REVISED,
    STATUS_UNCHANGED,
    ensure_series,
    record_observation,
)
from ..metrics import JOB_LAST_SUCCESS
from ..providers import registry
from ..providers.base import ProviderError

log = logging.getLogger(__name__)

JOB_NAME = "economic"


class EconomicIngestFailed(RuntimeError):
    """Every series in the pass failed; the pass produced nothing.

    Carries the full report as :attr:`report` so the caller can still say which
    series failed and why — the point is to make the failure impossible to
    return as a success, not to lose the detail.
    """

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        failed = report.get("series_failed", 0)
        super().__init__(
            f"economic ingest: all {failed} series failed, nothing was ingested: "
            + "; ".join(report.get("errors", []))[:1000]
        )


def _empty_counts() -> dict[str, int]:
    return {
        "inserted": 0, "unchanged": 0, "revised": 0, "raced": 0,
        "skipped_null": 0, "projections": 0,
    }


def ingest_economic(
    engine: Engine,
    settings: Settings,
    codes: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Ingest the catalog (every enabled series when ``codes`` is empty).

    Returns ``{"series": {code: {inserted, unchanged, revised, ...}},
    "errors": [...]}``.  Re-running it over unchanged sources reports zeros
    for ``inserted``/``revised`` — that is what makes it safe on a plain cron
    with no cursor.

    Raises :class:`ValueError` when a requested code is not in the catalog.
    The endpoint turns that into a 400: an unknown series is refused with a
    message, never quietly dropped from the list the caller asked for.

    Raises :class:`EconomicIngestFailed` when every series that was attempted
    failed, so the caller cannot report a pass that produced nothing as a
    success.  A partial failure returns normally with an ``errors`` list — one
    provider being down is not a failed job while the other still ingested.  A
    pass with nothing to attempt (every named series disabled) is not a failure
    either: nothing was tried, so nothing broke.
    """
    specs = enabled_specs(codes)
    result: dict[str, Any] = {
        "series": {},
        "errors": [],
        "series_ingested": 0,
        "series_failed": 0,
        "started_at": utcnow().isoformat(),
    }
    # Health is per PROVIDER, and a provider backs seven series here, so it is
    # accumulated across the whole pass and written once at the end.
    attempted: dict[str, int] = {}
    provider_failures: dict[str, list[str]] = {}

    for spec in specs:
        if not spec.enabled:
            # Only reachable when a caller names a disabled series explicitly.
            result["series"][spec.code] = _empty_counts() | {
                "status": "disabled",
                "reason": "series is disabled in the catalog",
            }
            continue
        attempted[spec.provider_code] = attempted.get(spec.provider_code, 0) + 1
        try:
            counts = _ingest_one(engine, settings, spec)
        except Exception as exc:  # noqa: BLE001 - one series must not sink the pass
            log.warning("economic ingest failed for %s: %s", spec.code, exc)
            provider_failures.setdefault(spec.provider_code, []).append(
                f"{spec.code}: {exc}"
            )
            result["series"][spec.code] = _empty_counts() | {
                "status": "error",
                "reason": type(exc).__name__,
            }
            result["errors"].append(f"{spec.code}: {exc}")
            result["series_failed"] += 1
            continue
        result["series"][spec.code] = counts
        result["series_ingested"] += 1

    result["providers"] = _record_provider_health(engine, attempted, provider_failures)
    result["finished_at"] = utcnow().isoformat()
    if not result["errors"]:
        JOB_LAST_SUCCESS.labels(job=JOB_NAME).set(time.time())
    if result["series_failed"] and not result["series_ingested"]:
        # Nothing was ingested. The caller must not be able to read this as a
        # successful run; see the module docstring.
        raise EconomicIngestFailed(result)
    return result


def _record_provider_health(
    engine: Engine,
    attempted: dict[str, int],
    failures: dict[str, list[str]],
) -> dict[str, dict[str, Any]]:
    """Write one health verdict per provider for the whole pass.

    A provider is successful for the pass ONLY if none of its series failed.
    Anything weaker lets a lucky seventh series erase six real failures:
    ``record_success`` sets ``consecutive_failures = 0`` and ``last_error =
    NULL``, so /internal/providers/health would show a provider that answered
    once out of seven times as perfectly healthy.

    The recorded error names every series that failed (truncated by
    ``record_failure``), because "worldbank failed" without the list is not
    enough to act on.
    """
    verdicts: dict[str, dict[str, Any]] = {}
    for provider_code, tried in attempted.items():
        failed = failures.get(provider_code, [])
        if failed:
            registry.record_failure(
                engine,
                provider_code,
                f"{len(failed)} of {tried} series failed: " + "; ".join(failed),
            )
        else:
            registry.record_success(engine, provider_code)
        verdicts[provider_code] = {
            "attempted": tried,
            "failed": len(failed),
            "healthy": not failed,
        }
    return verdicts


def _ingest_one(engine: Engine, settings: Settings, spec: SeriesSpec) -> dict[str, Any]:
    """Fetch one series and write it in a single transaction of its own."""
    provider = registry.build_provider(spec.provider_code, settings)
    if provider is None:
        raise ProviderError(
            f"no provider is registered for code {spec.provider_code!r}"
        )
    if not isinstance(provider, EconomicSeriesProvider):
        # A price adapter cannot answer for a dated series, and letting it try
        # would end in an AttributeError three frames away from the cause.
        raise ProviderError(
            f"provider {spec.provider_code!r} is a {type(provider).__name__}, "
            "which does not serve economic series"
        )
    fetch_started = utcnow()
    fetch = provider.fetch_detail(spec, now=fetch_started)
    points: list[SeriesPoint] = fetch.points
    # The instant the payload was in hand: the first moment this system could
    # have known ANY value in it, and therefore the availability stamp for all
    # of them.
    available_at = utcnow()

    counts = _empty_counts()
    counts["fetched"] = len(points)
    # Periods the source itself has no value for. Reported, never zero-filled.
    counts["skipped_null"] = fetch.skipped_null
    # How many periods were unfinished AT THIS FETCH — a property of the
    # payload and the clock, not of the table. Stored flags are frozen at write
    # time, so after a New Year this can be one lower than the number of stored
    # rows carrying the flag, and that is correct: the store did not rewrite
    # them because the source did not restate them.
    counts["projections"] = sum(1 for point in points if point.is_projection)
    with engine.begin() as conn:
        series_id = ensure_series(conn, spec)
        for point in points:
            written = record_observation(
                conn,
                series_id,
                ref_period_start=point.ref_period_start,
                ref_period_end=point.ref_period_end,
                ref_period_label=point.ref_period_label,
                value=point.value,
                available_at=available_at,
                published_at=point.published_at,
                is_nowcast=point.is_nowcast,
                is_projection=point.is_projection,
                collected_at=available_at,
            )
            if written.status == STATUS_INSERTED:
                counts["inserted"] += 1
            elif written.status == STATUS_REVISED:
                counts["revised"] += 1
                log.info(
                    "economic revision: %s %s %s -> %s (vintage %d)",
                    spec.code, point.ref_period_label, written.previous_value,
                    point.value, written.vintage,
                )
            elif written.status == STATUS_UNCHANGED:
                counts["unchanged"] += 1
            elif written.status == STATUS_RACED:
                # Another pass wrote this exact vintage first. Not an error:
                # the row is there, written from the same payload, and the next
                # pass re-compares. Counted so a run that hits many of these
                # says so instead of looking like a quiet no-op.
                counts["raced"] += 1
    counts["status"] = "ok"
    counts["series_id"] = series_id
    counts["available_at"] = available_at.isoformat()
    # What the payload said about itself, so the report can state provenance
    # rather than only row counts.
    counts["source_meta"] = fetch.meta
    return counts
