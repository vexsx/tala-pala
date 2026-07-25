"""News ingestion job — feature-flagged, default OFF.

Idempotent by construction: an article is keyed by (source, canonical URL), an
unchanged article only refreshes ``last_seen_at``, and an edited one APPENDS a
version row instead of overwriting.  Re-running the job over the same feed
window therefore inserts nothing, which is what lets it run on a plain cron
without a cursor.

Degradation: a source that fails is logged as a warning, recorded on its
registry row, and skipped.  Every other source is still polled and the job
still returns counts — one broken feed must never sink the pass.

Feature flag: the whole subsystem is gated on ``NEWS_ENABLED`` (default off),
read from the environment HERE rather than from :class:`app.config.Settings`.
``config.py`` should later expose it as ``news_enabled: bool`` following the
existing ``_env`` conventions; when it does, :func:`news_enabled` should be
deleted and the flag read off ``settings`` instead.

Nothing downstream consumes what this job stores.  There is no historical news
archive — accumulation starts at the first successful ingest — so news features
cannot improve forecasts yet, and no feature frame or model reads these tables.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import timedelta
from typing import Optional, Sequence

from sqlalchemy import select, update
from sqlalchemy.engine import Connection, Engine

from ..config import Settings
from ..db import ensure_utc, utcnow
from ..metrics import JOB_LAST_SUCCESS
from ..news import (
    ArticleRecord,
    NewsProvider,
    news_article_versions,
    news_articles,
    news_sources,
)
from ..news.dedupe import canonical_url, content_hash, find_near_duplicate, normalize_title
from ..providers.fedpress import FedPressProvider

log = logging.getLogger(__name__)

NEWS_ENABLED_ENV = "NEWS_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Near-duplicate candidates come from the same source within this window.  A
# story re-issued weeks later is a new story, and an unbounded scan would grow
# linearly with the archive.
NEAR_DUP_LOOKBACK_DAYS = 7


def news_enabled(env: Optional[dict] = None) -> bool:
    """True when ``NEWS_ENABLED`` is set to a truthy value (default off)."""
    raw = (env or os.environ).get(NEWS_ENABLED_ENV, "")
    return str(raw).strip().lower() in _TRUTHY


def build_news_provider(code: str, settings: Settings) -> Optional[NewsProvider]:
    """Instantiate a news provider by ``news_sources.code``.

    Returns None for sources with no implementation — ``gdelt`` is registered
    exploratory and disabled on purpose (its public API rate-limited this host
    with HTTP 429 and no fetcher exists for it).
    """
    if code == "fed_press":
        return FedPressProvider(
            timeout=settings.http_timeout_seconds,
            courtesy_delay=settings.provider_courtesy_delay,
            backoff_base=settings.provider_backoff_base,
        )
    return None


# --- source registry bookkeeping --------------------------------------------


def _load_sources(engine: Engine, codes: Optional[Sequence[str]]) -> list[dict]:
    """Enabled, policy-approved source rows (optionally filtered by code)."""
    stmt = select(news_sources).where(
        news_sources.c.enabled.is_(True),
        news_sources.c.policy_status == "approved",
    )
    if codes:
        stmt = stmt.where(news_sources.c.code.in_(list(codes)))
    stmt = stmt.order_by(news_sources.c.code)
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(stmt)]


def _mark_polled(engine: Engine, code: str, at) -> None:
    """Stamp the attempt marker BEFORE fetching.

    Success or failure alike consumes the courtesy interval: a broken feed must
    not be re-polled on every scheduler tick (the lesson the TSE-funds quota
    guard learned the expensive way).
    """
    with engine.begin() as conn:
        conn.execute(
            update(news_sources)
            .where(news_sources.c.code == code)
            .values(last_polled_at=at, updated_at=at)
        )


def _record_success(engine: Engine, code: str, at) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(news_sources)
            .where(news_sources.c.code == code)
            .values(
                last_success_at=at,
                consecutive_failures=0,
                last_error=None,
                updated_at=at,
            )
        )


def _record_failure(engine: Engine, code: str, error: str, at) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(news_sources)
            .where(news_sources.c.code == code)
            .values(
                last_error_at=at,
                last_error=error[:2000],
                consecutive_failures=news_sources.c.consecutive_failures + 1,
                updated_at=at,
            )
        )


def poll_due(row: dict, at) -> bool:
    """True when the source's courtesy interval has elapsed."""
    last = ensure_utc(row.get("last_polled_at"))
    if last is None:
        return True
    interval = int(row.get("min_interval_seconds") or 0)
    return (at - last).total_seconds() >= interval


# --- article upsert ----------------------------------------------------------


def _find_duplicate_id(
    conn: Connection, source_code: str, record: ArticleRecord, title_key: str
) -> Optional[int]:
    """Id of an existing distinct story this record merely restates, if any."""
    cutoff = record.ingested_at - timedelta(days=NEAR_DUP_LOOKBACK_DAYS)
    base = (
        select(news_articles.c.id, news_articles.c.title)
        .where(
            news_articles.c.source_code == source_code,
            news_articles.c.duplicate_of.is_(None),
            news_articles.c.ingested_at >= cutoff,
        )
        .order_by(news_articles.c.ingested_at)
    )
    if title_key:
        # Exact normalized-title hit: the indexed fast path in front of the
        # linear similarity scan below.
        exact = conn.execute(
            base.where(news_articles.c.title_key == title_key).limit(1)
        ).first()
        if exact is not None:
            return int(exact[0])
    candidates = [(int(row[0]), str(row[1])) for row in conn.execute(base)]
    return find_near_duplicate(record.title, candidates)


def _ingest_record(
    conn: Connection, source_code: str, record: ArticleRecord, counts: dict
) -> None:
    """Upsert one article, appending a version when its text changed."""
    canonical = canonical_url(record.url)
    if not canonical and record.external_id:
        # No usable link: the feed's guid is the only stable identity left.
        canonical = f"urn:{source_code}:{record.external_id}"
    if not canonical:
        counts["articles_skipped"] += 1
        return

    digest = content_hash(record.title, record.summary)
    title_key = normalize_title(record.title)

    existing = conn.execute(
        select(
            news_articles.c.id,
            news_articles.c.content_hash,
            news_articles.c.n_versions,
            news_articles.c.published_at,
        ).where(
            news_articles.c.source_code == source_code,
            news_articles.c.canonical_url == canonical,
        )
    ).first()

    if existing is not None:
        article_id, stored_hash, n_versions, first_published = existing
        if stored_hash == digest:
            conn.execute(
                update(news_articles)
                .where(news_articles.c.id == article_id)
                .values(last_seen_at=record.ingested_at)
            )
            counts["unchanged"] += 1
            return
        # The source moving its own timestamp forward is its claim about when
        # the revision happened; otherwise all we honestly know is when we saw
        # it.  Either way the FIRST publication time on the article is left
        # alone — each version row keeps the time stated for its own text.
        first_published = ensure_utc(first_published)
        revised_at = (
            record.published_at
            if first_published is not None and record.published_at > first_published
            else record.ingested_at
        )
        version = int(n_versions) + 1
        conn.execute(
            news_article_versions.insert().values(
                article_id=article_id,
                version=version,
                title=record.title,
                summary=record.summary,
                content_hash=digest,
                published_at=record.published_at,
                ingested_at=record.ingested_at,
                raw_payload=record.raw_payload,
            )
        )
        conn.execute(
            update(news_articles)
            .where(news_articles.c.id == article_id)
            .values(
                title=record.title,
                title_key=title_key,
                summary=record.summary,
                content_hash=digest,
                revised_at=revised_at,
                last_seen_at=record.ingested_at,
                n_versions=version,
            )
        )
        counts["versions_inserted"] += 1
        return

    duplicate_of = _find_duplicate_id(conn, source_code, record, title_key)
    result = conn.execute(
        news_articles.insert().values(
            source_code=source_code,
            external_id=record.external_id,
            canonical_url=canonical,
            url=record.url,
            title=record.title,
            title_key=title_key,
            summary=record.summary,
            language=record.language,
            content_hash=digest,
            published_at=record.published_at,
            published_at_estimated=record.published_at_estimated,
            ingested_at=record.ingested_at,
            last_seen_at=record.ingested_at,
            n_versions=1,
            duplicate_of=duplicate_of,
            raw_payload=record.raw_payload,
        )
    )
    article_id = result.inserted_primary_key[0]
    # Version 1 is written at first ingest so the original wording survives
    # every later edit.
    conn.execute(
        news_article_versions.insert().values(
            article_id=article_id,
            version=1,
            title=record.title,
            summary=record.summary,
            content_hash=digest,
            published_at=record.published_at,
            ingested_at=record.ingested_at,
            raw_payload=record.raw_payload,
        )
    )
    if duplicate_of is None:
        counts["articles_inserted"] += 1
    else:
        counts["near_duplicates"] += 1


def run_news_ingest(
    engine: Engine,
    settings: Settings,
    sources: Optional[Sequence[str]] = None,
    now: Optional[object] = None,
) -> dict:
    """Poll every approved source and upsert its articles; returns counts.

    ``articles_inserted`` counts DISTINCT stories; ``near_duplicates`` counts
    rows stored but linked to a story already held (``duplicate_of``), so the
    two never double-count the same news.
    """
    at = ensure_utc(now) or utcnow()
    counts = {
        "enabled": True,
        "sources_polled": 0,
        "sources_skipped": 0,
        "sources_failed": 0,
        "articles_fetched": 0,
        "articles_inserted": 0,
        "near_duplicates": 0,
        "versions_inserted": 0,
        "unchanged": 0,
        "articles_skipped": 0,
        "errors": [],
    }
    if not news_enabled():
        counts["enabled"] = False
        counts["reason"] = f"{NEWS_ENABLED_ENV} is off"
        return counts

    for row in _load_sources(engine, sources):
        code = str(row["code"])
        provider = build_news_provider(code, settings)
        if provider is None or not poll_due(row, at):
            counts["sources_skipped"] += 1
            continue
        _mark_polled(engine, code, at)
        try:
            records = provider.fetch_articles(ingested_at=at)
        except Exception as exc:  # noqa: BLE001 - one feed must not sink the job
            log.warning("news source %s failed: %s", code, exc)
            _record_failure(engine, code, str(exc), at)
            counts["sources_failed"] += 1
            counts["errors"].append(f"{code}: {exc}")
            continue
        _record_success(engine, code, at)
        counts["sources_polled"] += 1
        counts["articles_fetched"] += len(records)
        # One transaction per source: records inside a batch can dedupe against
        # each other, and a mid-source error leaves no half-written article.
        try:
            with engine.begin() as conn:
                for record in records:
                    _ingest_record(conn, code, record, counts)
        except Exception as exc:  # noqa: BLE001 - same containment as fetching
            log.warning("news ingest for %s failed: %s", code, exc)
            counts["sources_failed"] += 1
            counts["errors"].append(f"{code}: {exc}")

    JOB_LAST_SUCCESS.labels(job="news").set(time.time())
    return counts
