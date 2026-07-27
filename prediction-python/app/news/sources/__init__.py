"""Source collectors: store the bytes we received, then derive from them.

Why this package exists next to :mod:`app.providers` rather than inside it:
the price providers return parsed observations and nothing else, while an
Addendum 18 collector has two obligations a price provider does not have.

*Evidence before interpretation.*  Every collector writes the exact body it
received into ``news_raw_payloads`` (with its sha256) and commits that BEFORE
deriving anything, so a classification can always be re-derived and audited
against the bytes it came from — and a parser crash cannot destroy the only
copy of a document that will never be served again.

*Timestamp honesty.*  Fetch time is never stored as publication time.  The
0016-generation provider (``app/providers/fedpress.py``) substitutes ingestion
time when a feed omits ``pubDate``; these collectors deliberately do not, so a
reader can tell "the source stated no publication time" (``source_published_at
is None``, ``published_at_is_estimated`` true) apart from "the source published
it at the moment we fetched it".  The substitution still happens at the storage
boundary, because ``news_articles.published_at`` is NOT NULL in 0016 — but it
happens once, in :func:`store_articles`, next to the flag that records it.

``available_at`` is the only clock a historical feature may filter on: the
moment we could first have acted on the item, which is the later of publication
and ingestion.  See ``database/migrations/0017_news_intelligence.up.sql``.

All network access goes through :func:`safe_get`, a thin adapter over
:mod:`app.news.safefetch` that gives the collectors back a status code instead
of an exception and defaults to a single attempt — see its docstring for why
both matter.

Nothing in this package reaches a model.  Collection is gated on
``NEWS_COLLECTION_ENABLED`` (default off) and ``NEWS_ML_ENABLED`` remains false;
these tables are not read by ``app/features`` or ``app/models``.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy import select
from sqlalchemy.engine import Connection

from ...config import Settings
from ...db import ensure_utc, insert_ignore, utcnow
from .. import (
    news_articles,
    news_collection_attempts,
    news_raw_payloads,
    news_sources,
)
from ..dedupe import canonical_url, content_hash, normalize_title
from ..safefetch import (
    DEFAULT_POLICY,
    FetchHTTPError,
    fetch,
    parse_json_safely,
    parse_xml_safely,
)

log = logging.getLogger(__name__)

# Outcomes accepted by news_collection_attempts.outcome (0017 CHECK).
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"
OUTCOME_THROTTLED = "throttled"
OUTCOME_SKIPPED = "skipped"
OUTCOME_EMPTY = "empty"


# --- records handed from a collector to storage ------------------------------


@dataclass(frozen=True)
class CollectedArticle:
    """One normalized item, with its clocks kept apart.

    ``canonical`` is supplied by the collector rather than derived here: for a
    feed or a search API it is the canonical URL, but a list-diff source (OFAC)
    identifies a change by the entry that changed, not by a page that exists.
    """

    source_code: str
    canonical: str                          # per-source dedupe key
    url: str                                # exactly as published, '' when none
    title: str
    summary: str
    # None when the source stated no publication time.  NEVER the fetch time.
    source_published_at: Optional[datetime]
    published_at_is_estimated: bool
    available_at: datetime                  # when we could first act on it
    external_id: str = ""
    language: str = "en"
    query_id: Optional[int] = None
    # Everything the collector wants re-derivable later: raw timestamp strings,
    # rule ids, matched reasons, source-specific metadata.
    provenance: dict = field(default_factory=dict, hash=False)


# --- hashing and timestamp parsing -------------------------------------------


def sha256_text(body: str) -> str:
    """sha256 of a body, computed over the WHOLE text even when storage truncates."""
    return hashlib.sha256((body or "").encode("utf-8")).hexdigest()


def parse_rfc822(raw: str) -> tuple[Optional[datetime], bool]:
    """``(aware UTC datetime | None, is_estimated)`` for an RFC-822 date.

    A date without a zone is read as UTC *and* flagged estimated: the true
    offset could move it by hours, and this timestamp is a leakage boundary.
    """
    raw = (raw or "").strip()
    if not raw:
        return None, True
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None, True
    if parsed is None:
        return None, True
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc), True
    return parsed.astimezone(timezone.utc), False


def parse_iso8601(raw: str) -> tuple[Optional[datetime], bool]:
    """``(aware UTC datetime | None, is_estimated)`` for an ISO-8601 string."""
    raw = (raw or "").strip()
    if not raw:
        return None, True
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None, True
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc), True
    return parsed.astimezone(timezone.utc), False


def parse_compact_utc(raw: str) -> Optional[datetime]:
    """``20260715T141500Z`` (GDELT's stamp format) as an aware UTC datetime."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def available_from(
    source_published_at: Optional[datetime], fetched_at: datetime
) -> datetime:
    """When the item could FIRST have been acted upon.

    The later of publication and ingestion: we cannot act on a headline before
    we hold it, and a source that post-dates its own item does not become
    actionable earlier than it says.
    """
    if source_published_at is None:
        return fetched_at
    return max(source_published_at, fetched_at)


# --- safefetch adapter --------------------------------------------------------


@dataclass(frozen=True)
class FetchOutcome:
    """What a collector needs back from one request.

    Deliberately narrower than :class:`app.news.safefetch.FetchResult`: the
    collectors branch on the status code and store the body, and everything
    else about the transfer belongs to the fetch layer.
    """

    status_code: Optional[int]
    text: str
    content_type: str = ""
    fetched_at: Optional[datetime] = None


def safe_get(
    url: str,
    *,
    allow_hosts,
    max_bytes: int,
    timeout: float,
    max_attempts: int = 1,
) -> FetchOutcome:
    """Fetch one URL under the source's own caps.

    Two translations of ``safefetch.fetch`` happen here, and both matter:

    *A non-success status is returned, not raised.*  A status is an answer
    about the source and every collector records it on the attempt row; GDELT
    additionally has to SEE a 429 to stop its pass, which an exception that
    unwinds past the loop would prevent.  Failures with no status — blocked
    target, oversized body, timeout — still propagate, because they say nothing
    about what the source published.

    *Retries default to one attempt.*  safefetch retries 429/5xx internally,
    which would put requests on the wire that this package's own throttle never
    sees.  A collector that wants retries asks for them explicitly and keeps
    each one inside its own spacing guard.
    """
    policy = replace(
        DEFAULT_POLICY,
        read_timeout=timeout,
        connect_timeout=min(float(timeout), DEFAULT_POLICY.connect_timeout),
        max_bytes=max_bytes,
        max_decompressed_bytes=max(max_bytes, DEFAULT_POLICY.max_decompressed_bytes),
        max_attempts=max_attempts,
    )
    try:
        result = fetch(url, allowed_hosts=allow_hosts, policy=policy)
    except FetchHTTPError as exc:
        if exc.status_code is None:  # transport failure: no answer to record
            raise
        return FetchOutcome(status_code=exc.status_code, text="")
    return FetchOutcome(
        status_code=result.status_code,
        text=result.text,
        content_type=result.content_type,
        fetched_at=result.fetched_at,
    )


def received_at_of(response: Any) -> datetime:
    """When the body arrived.

    safefetch stamps the moment the response was accepted; falling back to now
    keeps the ingest clock from ever predating the moment we held the text.
    """
    return ensure_utc(getattr(response, "fetched_at", None)) or utcnow()


def parse_xml(text: str, *, source_code: str, max_bytes: int):
    """Parsed XML root, or None when the body is not a usable document.

    A malformed document — however safefetch signals it — means the same thing
    here, and none of those signals may sink a collection pass.  The result is
    compared against None explicitly by callers: an ``Element`` with no children
    is falsy, so truthiness would silently discard an empty-but-valid document.

    ``max_bytes`` is the collector's own cap.  safefetch defaults it to the
    shared decompressed limit, which is well under what a sanctions list
    legitimately weighs.
    """
    try:
        root = parse_xml_safely(text, max_bytes=max_bytes)
    except Exception as exc:  # parser boundary: any failure is "no document"
        log.warning("%s: unparseable XML body: %s", source_code, exc)
        return None
    return root


def parse_json(text: str, *, source_code: str, max_bytes: int) -> Optional[Any]:
    """Parsed JSON document, or None when the body is not usable JSON."""
    try:
        doc = parse_json_safely(text, max_bytes=max_bytes)
    except Exception as exc:  # parser boundary: any failure is "no document"
        log.warning("%s: unparseable JSON body: %s", source_code, exc)
        return None
    return doc


# --- source registry ----------------------------------------------------------


def load_source(conn: Connection, source_code: str) -> Optional[Mapping[str, Any]]:
    """The ``news_sources`` row for a collector, or None when unregistered."""
    row = conn.execute(
        select(news_sources).where(news_sources.c.code == source_code)
    ).first()
    return dict(row._mapping) if row is not None else None


def poll_gate(
    source_row: Optional[Mapping[str, Any]],
    settings: Settings,
    *,
    now: datetime,
) -> Optional[str]:
    """Reason this collector must not fetch right now, or None to proceed.

    The courtesy interval is enforced here rather than inside each collector so
    every source obeys the cadence stored on its own registry row, and so a
    dry run cannot be used to bypass it — a dry run still costs the source a
    request.
    """
    if not settings.news_collection_enabled:
        return "collection_disabled"
    if source_row is None:
        return "source_not_registered"
    if not source_row.get("enabled"):
        return "source_disabled"
    if source_row.get("policy_status") != "approved":
        return "policy_not_approved"
    last_polled = ensure_utc(source_row.get("last_polled_at"))
    interval = int(source_row.get("min_interval_seconds") or 0)
    if last_polled is not None and interval > 0:
        if now < last_polled + timedelta(seconds=interval):
            return "too_soon"
    return None


def mark_polled(
    conn: Connection,
    source_code: str,
    *,
    now: datetime,
    ok: bool,
    error: str = "",
) -> None:
    """Stamp the attempt marker on the registry row, success or failure alike.

    Stamped on failure too: a broken feed must not re-poll on every tick.
    """
    values: dict[str, Any] = {"last_polled_at": now, "updated_at": now}
    if ok:
        values.update(last_success_at=now, consecutive_failures=0, last_error=None)
    else:
        current = conn.execute(
            select(news_sources.c.consecutive_failures).where(
                news_sources.c.code == source_code
            )
        ).scalar()
        values.update(
            last_error_at=now,
            last_error=error[:2000],
            consecutive_failures=int(current or 0) + 1,
        )
    conn.execute(
        news_sources.update().where(news_sources.c.code == source_code).values(**values)
    )


def record_attempt(
    conn: Connection,
    *,
    source_code: str,
    started_at: datetime,
    finished_at: datetime,
    outcome: str,
    parser_version: str,
    http_status: Optional[int] = None,
    bytes_received: Optional[int] = None,
    items_seen: int = 0,
    items_new: int = 0,
    items_updated: int = 0,
    items_duplicate: int = 0,
    error_class: Optional[str] = None,
    error_detail: Optional[str] = None,
    query_id: Optional[int] = None,
) -> None:
    """Append the fetch attempt — this is what makes a quota claim checkable."""
    conn.execute(
        news_collection_attempts.insert().values(
            source_code=source_code,
            started_at=started_at,
            finished_at=finished_at,
            outcome=outcome,
            http_status=http_status,
            bytes_received=bytes_received,
            items_seen=items_seen,
            items_new=items_new,
            items_updated=items_updated,
            items_duplicate=items_duplicate,
            error_class=error_class,
            error_detail=(error_detail or "")[:4000] or None,
            query_id=query_id,
            parser_version=parser_version,
        )
    )


# --- raw payloads -------------------------------------------------------------


def store_raw_payload(
    conn: Connection,
    *,
    source_code: str,
    request_url: str,
    body: str,
    fetched_at: datetime,
    parser_version: str,
    http_status: Optional[int] = None,
    content_type: str = "",
    query_id: Optional[int] = None,
    max_body_chars: int,
) -> int:
    """Insert the received body and return its ``news_raw_payloads.id``.

    ``body_sha256`` covers the full body even when the stored copy is cut at
    ``max_body_chars``, so the hash still identifies what we actually received
    and ``truncated`` says whether the stored copy is complete.
    """
    body = body or ""
    stored = body[:max_body_chars]
    result = conn.execute(
        news_raw_payloads.insert().values(
            source_code=source_code,
            query_id=query_id,
            fetched_at=fetched_at,
            request_url=request_url,
            http_status=http_status,
            content_type=content_type or "",
            body_sha256=sha256_text(body),
            body_bytes=len(body.encode("utf-8")),
            body=stored,
            truncated=len(stored) < len(body),
            parser_version=parser_version,
        )
    )
    return int(result.inserted_primary_key[0])


def latest_raw_payload(
    conn: Connection, source_code: str
) -> Optional[Mapping[str, Any]]:
    """Most recently stored payload for a source (the snapshot-diff baseline)."""
    row = conn.execute(
        select(news_raw_payloads)
        .where(news_raw_payloads.c.source_code == source_code)
        .order_by(news_raw_payloads.c.fetched_at.desc(), news_raw_payloads.c.id.desc())
        .limit(1)
    ).first()
    return dict(row._mapping) if row is not None else None


# --- normalized rows ----------------------------------------------------------

# available_at, raw_payload_id, parser_version and query_id are columns 0017
# ALTERs onto the table 0016 created.  The SQLAlchemy mirror in
# app/news/__init__.py is hand-maintained and can lag a migration, so a value
# whose column is absent is dropped instead of crashing the pass — every one of
# them is also written into ``raw_payload``, which always exists, so a lagging
# mirror costs the indexed projection of the provenance, never the provenance.
_WARNED_MISSING: set[str] = set()


def _mirror_supported(values: dict[str, Any]) -> dict[str, Any]:
    missing = [name for name in values if name not in news_articles.c]
    if not missing:
        return values
    unseen = sorted(set(missing) - _WARNED_MISSING)
    if unseen:
        _WARNED_MISSING.update(unseen)
        log.warning(
            "news_articles mirror is missing %s (migration 0017 adds them); "
            "the values stay in raw_payload but are not indexed",
            ", ".join(unseen),
        )
    return {key: value for key, value in values.items() if key not in missing}


def store_articles(
    conn: Connection,
    articles: Iterable[CollectedArticle],
    *,
    raw_payload_id: Optional[int],
    parser_version: str,
    fetched_at: datetime,
) -> dict[str, int]:
    """Insert new articles; count the ones already held.

    An article already stored under the same (source, canonical key) is left
    untouched apart from ``last_seen_at``: revision tracking is the ingestion
    job's contract (``app/jobs/news.py`` appends version rows), and a collector
    that also rewrote article text would produce two writers for one row.
    """
    counts = {"items_seen": 0, "items_new": 0, "items_duplicate": 0}
    seen_in_batch: set[str] = set()
    for article in articles:
        counts["items_seen"] += 1
        key = article.canonical
        if not key:
            counts["items_duplicate"] += 1
            continue
        if key in seen_in_batch:
            counts["items_duplicate"] += 1
            continue
        seen_in_batch.add(key)

        # published_at is NOT NULL in 0016, so it holds the only defensible
        # substitute — the moment we could first act — with the estimated flag
        # set.  The provenance still records source_published_at as null, so
        # "not stated" stays distinguishable from "stated, and it was then".
        published_at = article.source_published_at or article.available_at
        provenance = dict(article.provenance)
        provenance.update(
            source_published_at=(
                article.source_published_at.isoformat()
                if article.source_published_at is not None
                else None
            ),
            published_at_is_estimated=article.published_at_is_estimated,
            available_at=article.available_at.isoformat(),
            raw_payload_id=raw_payload_id,
            parser_version=parser_version,
            query_id=article.query_id,
        )
        values = _mirror_supported(
            {
                "source_code": article.source_code,
                "external_id": article.external_id,
                "canonical_url": key,
                "url": article.url,
                "title": article.title,
                "title_key": normalize_title(article.title),
                "summary": article.summary,
                "language": article.language,
                "content_hash": content_hash(article.title, article.summary),
                "published_at": published_at,
                "published_at_estimated": article.published_at_is_estimated,
                "ingested_at": fetched_at,
                "last_seen_at": fetched_at,
                "raw_payload": provenance,
                "available_at": article.available_at,
                "raw_payload_id": raw_payload_id,
                "parser_version": parser_version,
                "query_id": article.query_id,
            }
        )
        if insert_ignore(conn, news_articles, [values]) == 1:
            counts["items_new"] += 1
        else:
            counts["items_duplicate"] += 1
            conn.execute(
                news_articles.update()
                .where(
                    news_articles.c.source_code == article.source_code,
                    news_articles.c.canonical_url == key,
                )
                .values(last_seen_at=fetched_at)
            )
    return counts


# --- result shape shared by every collector ----------------------------------


def base_result(source_code: str, parser_version: str, dry_run: bool) -> dict[str, Any]:
    """The counts dict every ``collect()`` returns, before it runs."""
    return {
        "source": source_code,
        "parser_version": parser_version,
        "status": OUTCOME_SKIPPED,
        "reason": "",
        "dry_run": dry_run,
        "http_status": None,
        "raw_payload_id": None,
        "items_seen": 0,
        "items_new": 0,
        "items_duplicate": 0,
    }


def content_type_of(response: Any) -> str:
    """Media type of a fetch outcome, '' when it carries none."""
    return str(getattr(response, "content_type", "") or "")


def record_failure(
    engine,
    result: dict[str, Any],
    *,
    started_at: datetime,
    error_class: str,
    detail: str,
    dry_run: bool,
    outcome: str = OUTCOME_ERROR,
    http_status: Optional[int] = None,
    bytes_received: Optional[int] = None,
    query_id: Optional[int] = None,
) -> dict[str, Any]:
    """Log a failed pass to the attempts table and the registry row.

    A dry run writes nothing, including this: it must leave no trace beyond the
    request it already made.
    """
    source_code = result["source"]
    result["status"] = outcome
    result["reason"] = error_class
    log.warning("%s: collection failed (%s): %s", source_code, error_class, detail)
    if dry_run:
        return result
    finished_at = utcnow()
    with engine.begin() as conn:
        record_attempt(
            conn,
            source_code=source_code,
            started_at=started_at,
            finished_at=finished_at,
            outcome=outcome,
            parser_version=result["parser_version"],
            http_status=http_status,
            bytes_received=bytes_received,
            error_class=error_class,
            error_detail=detail,
            query_id=query_id,
        )
        mark_polled(conn, source_code, now=finished_at, ok=False, error=detail)
    return result


__all__ = [
    "OUTCOME_EMPTY",
    "OUTCOME_ERROR",
    "OUTCOME_OK",
    "OUTCOME_SKIPPED",
    "OUTCOME_THROTTLED",
    "CollectedArticle",
    "FetchOutcome",
    "available_from",
    "base_result",
    "canonical_url",
    "content_type_of",
    "latest_raw_payload",
    "load_source",
    "mark_polled",
    "parse_compact_utc",
    "parse_iso8601",
    "parse_json",
    "parse_rfc822",
    "parse_xml",
    "poll_gate",
    "received_at_of",
    "record_attempt",
    "record_failure",
    "safe_get",
    "sha256_text",
    "store_articles",
    "store_raw_payload",
]
