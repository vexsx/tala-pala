"""News/event subsystem: shared vocabulary and storage mirror.

The subsystem exists to keep four clocks apart — event time, publication time,
ingestion time and revision time — because a feature built on the wrong one
leaks.  Only ``ingested_at`` bounds what this system could actually have known:
a dateline can claim anything, but we cannot have acted on a headline before we
held it.

**No news feature feeds any model today.**  There is no historical news
archive; accumulation starts at the first successful ingest, so nothing here
can improve forecasts yet and nothing in ``app/features`` or ``app/models``
reads these tables.

The table definitions below mirror ``database/migrations/0016_news_events``
exactly as :mod:`app.db` mirrors the rest of the schema (Postgres is created
and migrated by the Go service; Python only reads and writes).  They live with
their subsystem while it is behind ``NEWS_ENABLED`` — they belong in
:mod:`app.db` once the subsystem is permanent — and they register on
``app.db.metadata``, so importing this package is what makes the tables appear
in the tests' SQLite schema.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)

# Reusing db.py's column helpers (BIGSERIAL/NUMERIC/TIMESTAMPTZ with their
# SQLite variants) so this mirror cannot drift from the rest of the schema.
from ..db import _NUM, _TS, _big_pk, metadata
from ..providers.base import Observation, Provider, ProviderError

# --- records exchanged between providers and the ingestion job ---------------


@dataclass(frozen=True)
class ArticleRecord:
    """One article as a source published it, plus our own ingestion stamp.

    ``published_at`` and ``ingested_at`` are separate fields and no code path
    may substitute one for the other: the first is a claim by the source, the
    second is evidence about this system.
    """

    source_code: str
    external_id: str           # feed <guid>, '' when absent
    url: str                   # exactly as published
    title: str
    summary: str
    published_at: datetime     # aware UTC, as stated by the source
    ingested_at: datetime      # aware UTC, when THIS system saw it
    # True when the source gave no parseable timestamp and ingestion time was
    # substituted — the conservative direction (never earlier than reality).
    published_at_estimated: bool = False
    language: str = "en"
    raw_payload: Optional[dict] = field(default=None, hash=False)


class NewsProvider(Provider):
    """Base for providers that deliver articles instead of price quotes.

    Inherits the HTTP conventions of :class:`app.providers.base.Provider`:
    honest User-Agent, courtesy delay, bounded retry, and never retrying or
    bypassing an auth wall / bot challenge.
    """

    category: str = "news"

    @abc.abstractmethod
    def fetch_articles(
        self, ingested_at: Optional[datetime] = None
    ) -> list[ArticleRecord]:
        """Fetch the source's current window.  Raises ProviderError on failure."""

    def fetch(self) -> list[Observation]:
        """News providers never produce price observations.

        Raising (rather than returning ``[]``) makes an accidental wiring into
        the collect job loud instead of silently contributing nothing.
        """
        raise ProviderError(f"{self.code}: news provider, not a price provider")


# --- tables mirroring database/migrations/0016_news_events.up.sql ------------

news_sources = Table(
    "news_sources",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("feed_url", Text, nullable=False, server_default=""),
    Column("homepage_url", Text, nullable=False, server_default=""),
    Column("kind", Text, nullable=False, server_default="rss"),
    Column("jurisdiction", Text, nullable=False, server_default="global"),
    Column("language", Text, nullable=False, server_default="en"),
    Column("enabled", Boolean, nullable=False, server_default=text("FALSE")),
    Column("policy_status", Text, nullable=False, server_default="exploratory"),
    Column("policy_note", Text, nullable=False, server_default=""),
    Column("policy_checked_at", _TS),
    Column("quota_per_day", Integer),
    Column("min_interval_seconds", Integer, nullable=False, server_default=text("900")),
    Column("last_polled_at", _TS),
    Column("last_success_at", _TS),
    Column("last_error_at", _TS),
    Column("last_error", Text),
    Column("consecutive_failures", Integer, nullable=False, server_default=text("0")),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
)

news_articles = Table(
    "news_articles",
    metadata,
    _big_pk(),
    Column("source_code", Text, ForeignKey("news_sources.code"), nullable=False),
    Column("external_id", Text, nullable=False, server_default=""),
    Column("canonical_url", Text, nullable=False),
    Column("url", Text, nullable=False, server_default=""),
    Column("title", Text, nullable=False),
    Column("title_key", Text, nullable=False, server_default=""),
    Column("summary", Text, nullable=False, server_default=""),
    Column("language", Text, nullable=False, server_default="en"),
    Column("content_hash", Text, nullable=False),
    Column("published_at", _TS, nullable=False),
    Column(
        "published_at_estimated", Boolean, nullable=False, server_default=text("FALSE")
    ),
    Column("ingested_at", _TS, nullable=False, server_default=func.now()),
    Column("revised_at", _TS),
    Column("last_seen_at", _TS, nullable=False, server_default=func.now()),
    Column("n_versions", Integer, nullable=False, server_default=text("1")),
    Column("duplicate_of", ForeignKey("news_articles.id")),
    Column("raw_payload", JSON, nullable=True),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    UniqueConstraint("source_code", "canonical_url", name="news_articles_unique"),
    Index("idx_news_articles_published", "published_at"),
    Index("idx_news_articles_source_published", "source_code", "published_at"),
    Index("idx_news_articles_ingested", "ingested_at"),
    Index("idx_news_articles_content_hash", "content_hash"),
    Index("idx_news_articles_title_key", "source_code", "title_key"),
)

news_article_versions = Table(
    "news_article_versions",
    metadata,
    _big_pk(),
    Column(
        "article_id",
        ForeignKey("news_articles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("version", Integer, nullable=False),
    Column("title", Text, nullable=False),
    Column("summary", Text, nullable=False, server_default=""),
    Column("content_hash", Text, nullable=False),
    Column("published_at", _TS, nullable=False),
    Column("ingested_at", _TS, nullable=False, server_default=func.now()),
    Column("raw_payload", JSON, nullable=True),
    UniqueConstraint("article_id", "version", name="news_article_versions_unique"),
    Index("idx_news_article_versions_article", "article_id", "ingested_at"),
)

news_events = Table(
    "news_events",
    metadata,
    _big_pk(),
    Column("article_id", ForeignKey("news_articles.id")),
    Column("source_code", Text, nullable=False, server_default=""),
    Column("category", Text, nullable=False),
    Column("polarity", Text, nullable=False, server_default="unknown"),
    Column("event_time", _TS, nullable=False),
    Column("event_time_precision", Text, nullable=False, server_default="unknown"),
    Column("published_at", _TS, nullable=False),
    Column("ingested_at", _TS, nullable=False, server_default=func.now()),
    Column("revised_at", _TS),
    Column("severity", Text, nullable=False, server_default="unknown"),
    Column("surprise", _NUM),
    Column("classifier", Text, nullable=False, server_default=""),
    Column("classifier_confidence", Float),
    Column("details", JSON, nullable=False, default=dict),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    UniqueConstraint("article_id", "category", name="news_events_unique"),
    Index("idx_news_events_category_ingested", "category", "ingested_at"),
    Index("idx_news_events_event_time", "event_time"),
    Index("idx_news_events_published", "published_at"),
)
