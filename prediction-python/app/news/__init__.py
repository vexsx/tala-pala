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
    BigInteger,
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
    Column("duplicate_of", ForeignKey("news_articles.id", ondelete="SET NULL")),
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
    Column("article_id", ForeignKey("news_articles.id", ondelete="SET NULL")),
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


# --- Addendum 18 tables (migration 0017) -------------------------------------
# Mirrors for the tables the collectors, consolidator and intelligence engine
# write. Kept alongside the 0016 mirrors so the whole news schema is described
# in one module; app.db.metadata is shared, so tests get these for free.

news_source_policies = Table(
    "news_source_policies", metadata,
    Column("id", Integer, primary_key=True),
    Column("source_code", Text, nullable=False),
    Column("access_method", Text, nullable=False, server_default="rss"),
    Column("auth_type", Text, nullable=False, server_default="none"),
    Column("approval_state", Text, nullable=False,
           server_default="policy_review_required"),
    Column("user_agent_policy", Text, nullable=False, server_default="honest"),
    Column("backfill_allowed", Boolean, nullable=False, server_default=text("FALSE")),
    Column("store_full_body", Boolean, nullable=False, server_default=text("FALSE")),
    Column("attribution_required", Boolean, nullable=False, server_default=text("TRUE")),
    Column("rate_limit_per_day", Integer),
    Column("min_interval_seconds", Integer, nullable=False, server_default="900"),
    Column("policy_note", Text, nullable=False, server_default=""),
    Column("reviewed_at", _TS),
    Column("reviewed_by", Text, nullable=False, server_default=""),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
)

news_collection_attempts = Table(
    "news_collection_attempts", metadata,
    _big_pk(),
    Column("source_code", Text, nullable=False),
    Column("started_at", _TS, nullable=False, server_default=func.now()),
    Column("finished_at", _TS),
    Column("outcome", Text, nullable=False, server_default="error"),
    Column("http_status", Integer),
    Column("bytes_received", BigInteger),
    Column("items_seen", Integer, nullable=False, server_default="0"),
    Column("items_new", Integer, nullable=False, server_default="0"),
    Column("items_updated", Integer, nullable=False, server_default="0"),
    Column("items_duplicate", Integer, nullable=False, server_default="0"),
    Column("error_class", Text),
    Column("error_detail", Text),
    Column("query_id", Integer),
    Column("parser_version", Text, nullable=False, server_default=""),
)

news_source_health_snapshots = Table(
    "news_source_health_snapshots", metadata,
    _big_pk(),
    Column("source_code", Text, nullable=False),
    Column("captured_at", _TS, nullable=False, server_default=func.now()),
    Column("health", Text, nullable=False, server_default="unknown"),
    Column("last_success_at", _TS),
    Column("last_publication_at", _TS),
    Column("publication_latency_s", Float),
    Column("consecutive_failures", Integer, nullable=False, server_default="0"),
    Column("duplicate_ratio", Float),
    Column("circuit_open", Boolean, nullable=False, server_default=text("FALSE")),
    Column("note", Text, nullable=False, server_default=""),
)

news_source_queries = Table(
    "news_source_queries", metadata,
    Column("id", Integer, primary_key=True),
    Column("source_code", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("query_text", Text, nullable=False),
    Column("description", Text, nullable=False, server_default=""),
    Column("enabled", Boolean, nullable=False, server_default=text("TRUE")),
    Column("max_records", Integer, nullable=False, server_default="75"),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

news_raw_payloads = Table(
    "news_raw_payloads", metadata,
    _big_pk(),
    Column("source_code", Text, nullable=False),
    Column("query_id", Integer),
    Column("fetched_at", _TS, nullable=False, server_default=func.now()),
    Column("request_url", Text, nullable=False),
    Column("http_status", Integer),
    Column("content_type", Text, nullable=False, server_default=""),
    Column("body_sha256", Text, nullable=False),
    Column("body_bytes", BigInteger, nullable=False, server_default="0"),
    Column("body", Text),
    Column("truncated", Boolean, nullable=False, server_default=text("FALSE")),
    Column("parser_version", Text, nullable=False, server_default=""),
)

news_duplicate_groups = Table(
    "news_duplicate_groups", metadata,
    _big_pk(),
    Column("primary_article_id", BigInteger),
    Column("method", Text, nullable=False, server_default=""),
    Column("method_version", Text, nullable=False, server_default=""),
    Column("article_count", Integer, nullable=False, server_default="1"),
    Column("independent_source_count", Integer, nullable=False, server_default="1"),
    Column("syndication_count", Integer, nullable=False, server_default="0"),
    Column("source_diversity", Float, nullable=False, server_default="0"),
    Column("first_published_at", _TS),
    Column("first_seen_at", _TS),
    Column("last_updated_at", _TS),
    Column("conflicting", Boolean, nullable=False, server_default=text("FALSE")),
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

news_article_duplicates = Table(
    "news_article_duplicates", metadata,
    Column("group_id", BigInteger, primary_key=True),
    Column("article_id", BigInteger, primary_key=True),
    Column("similarity", Float, nullable=False, server_default="1.0"),
    Column("match_reason", Text, nullable=False, server_default=""),
    Column("method_version", Text, nullable=False, server_default=""),
    Column("is_primary", Boolean, nullable=False, server_default=text("FALSE")),
)

news_entities = Table(
    "news_entities", metadata,
    Column("id", Integer, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("code", Text, nullable=False),
    Column("display_name", Text, nullable=False),
    Column("display_fa", Text, nullable=False, server_default=""),
    Column("aliases", JSON, nullable=False, default=list),
    Column("latitude", Float),
    Column("longitude", Float),
    Column("location_verified", Boolean, nullable=False, server_default=text("FALSE")),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

news_article_entities = Table(
    "news_article_entities", metadata,
    Column("article_id", BigInteger, primary_key=True),
    Column("entity_id", Integer, primary_key=True),
    Column("matched_term", Text, nullable=False, server_default=""),
    Column("extractor_version", Text, nullable=False, server_default=""),
)

news_event_articles = Table(
    "news_event_articles", metadata,
    Column("event_id", BigInteger, primary_key=True),
    Column("article_id", BigInteger, primary_key=True),
    Column("role", Text, nullable=False, server_default="supporting"),
)

news_event_entities = Table(
    "news_event_entities", metadata,
    Column("event_id", BigInteger, primary_key=True),
    Column("entity_id", Integer, primary_key=True),
)

news_classifier_versions = Table(
    "news_classifier_versions", metadata,
    Column("id", Integer, primary_key=True),
    Column("version", Text, nullable=False),
    Column("kind", Text, nullable=False, server_default="deterministic"),
    Column("description", Text, nullable=False, server_default=""),
    Column("rule_count", Integer, nullable=False, server_default="0"),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

news_event_classifications = Table(
    "news_event_classifications", metadata,
    _big_pk(),
    Column("event_id", BigInteger, nullable=False),
    Column("classifier_version", Text, nullable=False),
    Column("category", Text, nullable=False),
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("rule_id", Text, nullable=False, server_default=""),
    Column("supporting_terms", JSON, nullable=False, default=list),
    Column("contradicting_terms", JSON, nullable=False, default=list),
    Column("classified_at", _TS, nullable=False, server_default=func.now()),
)

news_impact_hypotheses = Table(
    "news_impact_hypotheses", metadata,
    _big_pk(),
    Column("event_id", BigInteger, nullable=False),
    Column("classifier_version", Text, nullable=False),
    Column("channel", Text, nullable=False),
    Column("score", Float, nullable=False),
    Column("confidence", Float, nullable=False, server_default="0"),
    Column("rule_id", Text, nullable=False, server_default=""),
    Column("rule_version", Text, nullable=False, server_default=""),
    Column("supporting_evidence", JSON, nullable=False, default=list),
    Column("contradicting_evidence", JSON, nullable=False, default=list),
    Column("sample_support", Integer),
    Column("expected_horizon", Text, nullable=False, server_default=""),
    Column("decay_hours", Float),
    Column("hypothesis_only", Boolean, nullable=False, server_default=text("TRUE")),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

scheduled_macro_events = Table(
    "scheduled_macro_events", metadata,
    Column("id", Integer, primary_key=True),
    Column("code", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("region", Text, nullable=False, server_default="us"),
    Column("importance", Text, nullable=False, server_default="medium"),
    Column("scheduled_at", _TS, nullable=False),
    Column("scheduled_precision", Text, nullable=False, server_default="exact"),
    Column("source_code", Text),
    Column("source_url", Text, nullable=False, server_default=""),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

macro_event_releases = Table(
    "macro_event_releases", metadata,
    _big_pk(),
    Column("scheduled_event_id", Integer, nullable=False),
    Column("released_at", _TS, nullable=False),
    Column("available_at", _TS, nullable=False),
    Column("previous_value", Float),
    Column("consensus_value", Float),
    Column("first_value", Float),
    Column("unit", Text, nullable=False, server_default=""),
    Column("surprise", Float),
    Column("source_code", Text),
    Column("raw_payload_id", BigInteger),
)

macro_event_revisions = Table(
    "macro_event_revisions", metadata,
    _big_pk(),
    Column("release_id", BigInteger, nullable=False),
    Column("revised_value", Float, nullable=False),
    Column("revised_at", _TS, nullable=False),
    Column("available_at", _TS, nullable=False),
    Column("note", Text, nullable=False, server_default=""),
)

intelligence_snapshots = Table(
    "intelligence_snapshots", metadata,
    _big_pk(),
    Column("captured_at", _TS, nullable=False, server_default=func.now()),
    Column("calc_version", Text, nullable=False),
    Column("scores", JSON, nullable=False, default=dict),
    Column("confidence", JSON, nullable=False, default=dict),
    Column("inputs", JSON, nullable=False, default=dict),
    Column("supporting_event_ids", JSON, nullable=False, default=list),
    Column("conflicting_event_ids", JSON, nullable=False, default=list),
    Column("source_reliability", Float),
    Column("data_freshness_s", Float),
    Column("stale", Boolean, nullable=False, server_default=text("FALSE")),
    Column("limitations", Text, nullable=False, server_default=""),
)

intelligence_snapshot_events = Table(
    "intelligence_snapshot_events", metadata,
    Column("snapshot_id", BigInteger, primary_key=True),
    Column("event_id", BigInteger, primary_key=True),
    Column("role", Text, nullable=False, server_default="supporting"),
    Column("weight", Float, nullable=False, server_default="0"),
)

intelligence_deltas = Table(
    "intelligence_deltas", metadata,
    _big_pk(),
    Column("from_snapshot", BigInteger),
    Column("to_snapshot", BigInteger, nullable=False),
    Column("computed_at", _TS, nullable=False, server_default=func.now()),
    Column("kind", Text, nullable=False),
    Column("detail", JSON, nullable=False, default=dict),
    Column("magnitude", Float),
    Column("event_id", BigInteger),
)

event_impact_stats = Table(
    "event_impact_stats", metadata,
    _big_pk(),
    Column("computed_at", _TS, nullable=False, server_default=func.now()),
    Column("category", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("window_label", Text, nullable=False),
    Column("n_events", Integer, nullable=False),
    Column("n_independent", Integer, nullable=False, server_default="0"),
    Column("mean_move_pct", Float),
    Column("median_move_pct", Float),
    Column("hit_rate", Float),
    Column("vol_change_pct", Float),
    Column("spread_change_pct", Float),
    Column("premium_change_pct", Float),
    Column("ci_low", Float),
    Column("ci_high", Float),
    Column("regime", Text, nullable=False, server_default="all"),
    Column("sufficient_support", Boolean, nullable=False, server_default=text("FALSE")),
    Column("method_version", Text, nullable=False, server_default=""),
)

news_feature_snapshots = Table(
    "news_feature_snapshots", metadata,
    _big_pk(),
    Column("symbol", Text, nullable=False),
    Column("as_of", _TS, nullable=False),
    Column("builder_version", Text, nullable=False),
    Column("features", JSON, nullable=False, default=dict),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

news_research_runs = Table(
    "news_research_runs", metadata,
    _big_pk(),
    Column("started_at", _TS, nullable=False, server_default=func.now()),
    Column("finished_at", _TS),
    Column("kind", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="running"),
    Column("feature_sets", JSON, nullable=False, default=list),
    Column("results", JSON, nullable=False, default=dict),
    Column("decision", Text, nullable=False, server_default="shadow"),
    Column("notes", Text, nullable=False, server_default=""),
)
