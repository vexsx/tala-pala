"""Scheduler-callable pass over the Addendum 18 collectors (fed, OFAC, GDELT).

This is the *orchestration* half of collection and nothing else: every fetch,
parse, timestamp and storage decision already lives in
``app/news/sources/{fed,ofac,gdelt}.py``, and every permission decision lives in
``app/news/registry.py``.  What is added here is the part that only makes sense
once there is more than one source — deciding which collectors may run on this
tick, and containing one collector's failure so the others still run.

Three properties this file exists to guarantee:

*The flag is read off ``Settings``, not the environment.*  ``run_news_ingest``
(the older provider path in ``app/jobs/news.py``) reads ``NEWS_ENABLED`` from
``os.environ`` because ``config.py`` did not carry it yet; it does now, so this
job gates on ``settings.news_collection_enabled``.  A settings field is what a
test can flip and what one process-wide config audit can enumerate.

*Permission comes from the registry, never from this list.*  A module appearing
in :data:`COLLECTORS` means an implementation exists, not that it may fetch:
:func:`app.news.registry.approved_sources` is the gate, and a source it does not
return is skipped with a reason.  GDELT is registered disabled/exploratory on
purpose (its public API rate-limited this host), so it is skipped here until
someone approves it in the database — no code change required either way.

*One broken collector must not sink the pass.*  Each ``collect()`` is wrapped:
the exception is recorded against that source's registry row (so the circuit
breaker sees it and stops re-polling a dead feed every tick) and reported in
``errors``, and the loop continues.  A pass with one failure still returns
counts rather than raising through the endpoint into an alert.

Nothing collected here reaches model input or prediction logic.
``NEWS_ML_ENABLED`` gates that separately and stays false; this job only
accumulates the chronological archive that would have to exist first.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from types import ModuleType
from typing import Any, Optional, Sequence

from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import utcnow
from ..metrics import JOB_LAST_SUCCESS
from ..news import registry
from ..news.safefetch import redact
from ..news.sources import (
    OUTCOME_EMPTY,
    OUTCOME_ERROR,
    OUTCOME_OK,
    OUTCOME_SKIPPED,
    fed,
    gdelt,
    ofac,
)

log = logging.getLogger(__name__)

# Modules, not bound ``collect`` functions: the attribute is looked up per run,
# so the registry row — not an import-time binding — decides what executes.
COLLECTORS: tuple[ModuleType, ...] = (fed, ofac, gdelt)

DISABLED_REASON = "NEWS_COLLECTION_ENABLED is off"

# Outcomes that mean the source was successfully reached.  'empty' counts as
# polled: the fetch worked and the feed had nothing new, which is a normal day
# for a press feed and must not look like a failure in the health counters.
POLLED_OUTCOMES = frozenset({OUTCOME_OK, OUTCOME_EMPTY})


def _new_counts(dry_run: bool) -> dict[str, Any]:
    """The result shape, before anything runs."""
    return {
        "enabled": True,
        "dry_run": dry_run,
        "sources": {},
        "sources_polled": 0,
        "sources_skipped": 0,
        "sources_failed": 0,
        "articles_seen": 0,
        "articles_inserted": 0,
        "articles_duplicate": 0,
        "errors": [],
    }


def _skip(counts: dict[str, Any], code: str, reason: str) -> None:
    """Record a source we chose not to poll.

    Deliberately does NOT call :func:`registry.record_attempt`: a 'skipped'
    attempt stamps ``last_polled_at``, so recording a cadence skip would push
    the due time forward on every tick and a frequently-scheduled job would
    starve the source it was trying to be polite to.
    """
    counts["sources"][code] = {"source": code, "status": OUTCOME_SKIPPED, "reason": reason}
    counts["sources_skipped"] += 1
    log.debug("news collector %s skipped: %s", code, reason)


def _record_crash(
    engine: Engine,
    code: str,
    exc: BaseException,
    *,
    started_at: datetime,
    parser_version: str,
    dry_run: bool,
) -> None:
    """Persist a collector crash against its registry row.

    A dry run writes nothing, matching ``app.news.sources.record_failure``: a
    dry run must leave no trace beyond the request it already made.  The
    bookkeeping write is itself contained — a failure to record a failure must
    not be the thing that stops the remaining collectors.
    """
    if dry_run:
        return
    try:
        registry.record_attempt(
            engine,
            code,
            outcome="error",
            started_at=started_at,
            finished_at=utcnow(),
            error_class=type(exc).__name__,
            error_detail=f"{type(exc).__name__}: {exc}",
            parser_version=parser_version,
        )
    except Exception as record_exc:  # noqa: BLE001 - see docstring
        log.warning("could not record %s collector failure: %s", code, record_exc)


def run_news_collection(
    engine: Engine,
    settings: Settings,
    *,
    sources: Optional[Sequence[str]] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one collection pass over the approved sources; returns counts.

    ``sources`` restricts the pass to those source codes (a code with no
    collector is reported skipped rather than silently ignored, so an operator
    typo is visible).  ``dry_run`` is passed through to each collector, which
    fetches and parses but stores nothing — it does not bypass the courtesy
    interval, because a dry run still costs the source a request.

    ``articles_inserted`` sums the collectors' ``items_new``: rows that did not
    already exist under the same (source, canonical key).  Re-running the pass
    over an unchanged feed therefore reports zero, which is what lets this run
    on a plain cron without a cursor.
    """
    counts = _new_counts(dry_run)
    if not settings.news_collection_enabled:
        counts["enabled"] = False
        counts["reason"] = DISABLED_REASON
        return counts

    requested = {str(code) for code in sources} if sources else None

    # Fails closed: an unreadable registry means nothing is approved, so
    # nothing is polled.  Reported as an error rather than raised, because a
    # scheduler tick should return counts even when the gate cannot be read.
    try:
        approved = {source.code: source for source in registry.approved_sources(engine)}
    except Exception as exc:  # noqa: BLE001 - see comment above
        log.warning("news source registry unavailable: %s", exc)
        approved = {}
        counts["errors"].append(f"registry: {redact(exc)}")

    for collector in COLLECTORS:
        code = str(collector.SOURCE_CODE)
        if requested is not None and code not in requested:
            continue
        source = approved.get(code)
        if source is None:
            _skip(counts, code, "not approved or not enabled")
            continue
        started_at = utcnow()
        may_poll, reason = registry.should_attempt(source, started_at)
        if not may_poll:
            _skip(counts, code, reason)
            continue

        try:
            outcome = collector.collect(engine, settings, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 - one feed must not sink the pass
            log.warning("news collector %s failed: %s", code, exc)
            _record_crash(
                engine,
                code,
                exc,
                started_at=started_at,
                parser_version=str(getattr(collector, "PARSER_VERSION", "")),
                dry_run=dry_run,
            )
            counts["sources"][code] = {
                "source": code,
                "status": OUTCOME_ERROR,
                "reason": type(exc).__name__,
            }
            counts["sources_failed"] += 1
            counts["errors"].append(f"{code}: {redact(exc)}")
            continue

        counts["sources"][code] = outcome
        status = str(outcome.get("status") or "")
        counts["articles_seen"] += int(outcome.get("items_seen") or 0)
        counts["articles_inserted"] += int(outcome.get("items_new") or 0)
        counts["articles_duplicate"] += int(outcome.get("items_duplicate") or 0)
        if status in POLLED_OUTCOMES:
            counts["sources_polled"] += 1
        elif status == OUTCOME_ERROR:
            # The collector already recorded the attempt and the failure on the
            # registry row; here it only has to be counted and surfaced.
            counts["sources_failed"] += 1
            counts["errors"].append(f"{code}: {outcome.get('reason') or status}")
        else:
            # 'skipped' (poll gate) or 'throttled' (the host asked us to stop).
            # Neither is a failure — the registry deliberately leaves the
            # breaker counter alone for both — so neither trips an alert.
            counts["sources_skipped"] += 1

    if requested is not None:
        for code in sorted(requested - set(counts["sources"])):
            _skip(counts, code, "no collector implemented")

    if not dry_run:
        # A dry run stores nothing, so it must not refresh the staleness gauge
        # that says news collection is healthy.
        JOB_LAST_SUCCESS.labels(job="news_collect").set(time.time())
    return counts
