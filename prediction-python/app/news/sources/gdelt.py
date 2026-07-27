"""GDELT DOC 2.0 — narrow configured queries behind a hard request throttle.

GDELT's public API rate-limited this host with an HTTP 429 once already, which
is why 0016 registered the source disabled with no fetcher.  This collector is
the fetcher, written so that the failure cannot repeat by accident:

*Spacing is enforced at the request boundary, not in the query loop.*  The
guard is module state consulted by :func:`_request`, so a retry — which is a
request too — cannot slip through a gap the loop never sees.  It uses a
monotonic clock: a wall-clock jump backwards during an NTP correction must not
release the throttle early.

*A 429 ends the pass.*  Not "retry with a longer delay": a host that has told
us to stop gets no further requests until the next scheduled run.

*Queries are configuration, not code.*  They come from
``news_source_queries`` — enabled rows only, each with its own record cap — and
the exact query text is stored with every result, so a result set can be
reproduced and a query change is visible in the data rather than in a diff.  A
run with no enabled query is a no-op: which topics are worth asking a global
news index about is a policy decision, not a constant.

What GDELT returns is metadata about an article, not the article.  ``seendate``
is when GDELT's crawler saw the item and is kept under that name; it is NOT the
article's publication time, and since the DOC API states no publication time,
``source_published_at`` is None with the estimated flag set.  Tone, when a
configured mode returns it, is stored as unverified source metadata — it is
GDELT's number about a document we never read, and nothing may treat it as a
sentiment feature.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Any, Mapping, Optional
from urllib.parse import urlencode

from sqlalchemy import select

from ...config import Settings
from ...db import utcnow
from .. import news_source_queries
from . import (
    OUTCOME_EMPTY,
    OUTCOME_OK,
    OUTCOME_THROTTLED,
    CollectedArticle,
    base_result,
    canonical_url,
    content_type_of,
    load_source,
    mark_polled,
    parse_compact_utc,
    parse_json,
    poll_gate,
    received_at_of,
    record_attempt,
    safe_get,
    store_articles,
    store_raw_payload,
)

SOURCE_CODE = "gdelt"
PARSER_VERSION = "gdelt_doc2_artlist_v1"

DEFAULT_API_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
ALLOW_HOSTS = ("api.gdeltproject.org",)

# Floor for the spacing between requests.  Configurable upwards through
# GDELT_MIN_INTERVAL_SECONDS; never downwards.
MIN_REQUEST_INTERVAL_SECONDS = 5.0
# Ceiling on one response and on the copy we keep.  An artlist page of 75
# records is tens of KB; anything near the cap is an error page.
MAX_FETCH_BYTES = 8_000_000
MAX_STORED_BODY_CHARS = 4_000_000
HARD_TIMEOUT_SECONDS = 20.0
# Per-query cap, over the top of the per-query `max_records` column.
MAX_RECORDS_HARD_CAP = 250
# One retry only, and only for a transport error or a 5xx.  A rate-limited host
# is not retried at all.
MAX_ATTEMPTS_PER_QUERY = 2
RETRY_STATUSES = frozenset({500, 502, 503, 504})
THROTTLE_STATUSES = frozenset({429})
DEFAULT_TIMESPAN = "1d"

# GDELT reports a language name, not a code.  Anything unmapped is recorded as
# ISO 639-2 "und" (undetermined) rather than guessed at.
_LANGUAGE_CODES = {
    "english": "en",
    "persian": "fa",
    "farsi": "fa",
    "arabic": "ar",
    "french": "fr",
    "german": "de",
    "russian": "ru",
    "chinese": "zh",
    "turkish": "tr",
    "spanish": "es",
}
UNKNOWN_LANGUAGE = "und"


class _RequestThrottle:
    """Process-wide minimum spacing between GDELT requests.

    Module state on purpose: two queries in one pass, a retry inside one query,
    and a second collector instance in the same process all share one budget,
    because the host sees one client.  ``monotonic`` and ``sleep`` are
    attributes so a test can drive the clock without waiting in real time.
    """

    def __init__(self) -> None:
        self.monotonic = time.monotonic
        self.sleep = time.sleep
        self._lock = threading.Lock()
        self._last_request_at: Optional[float] = None

    def wait(self, min_interval: float) -> float:
        """Block until the next request is allowed; return the seconds waited."""
        with self._lock:
            now = self.monotonic()
            waited = 0.0
            if self._last_request_at is not None:
                remaining = min_interval - (now - self._last_request_at)
                if remaining > 0:
                    self.sleep(remaining)
                    waited = remaining
                    now = self.monotonic()
            # Stamped before the request is made, and stamped whatever its
            # outcome: a failed request cost the host just as much as a good
            # one, so the next one waits the full interval either way.
            self._last_request_at = now
            return waited

    def reset(self) -> None:
        with self._lock:
            self._last_request_at = None


THROTTLE = _RequestThrottle()


def min_interval_seconds(settings: Settings) -> float:
    """Configured spacing, floored at the module minimum."""
    return max(float(settings.gdelt_min_interval_seconds), MIN_REQUEST_INTERVAL_SECONDS)


def build_query_url(query_text: str, *, max_records: int, timespan: str, api_url: str) -> str:
    """The exact request URL for one configured query."""
    params = {
        "query": query_text,
        "mode": "artlist",
        "format": "json",
        "maxrecords": str(max(1, min(int(max_records), MAX_RECORDS_HARD_CAP))),
        "timespan": timespan,
        "sort": "datedesc",
    }
    return f"{api_url}?{urlencode(params)}"


def load_queries(conn) -> list[Mapping[str, Any]]:
    """Enabled query rows for this source, in a stable order."""
    rows = conn.execute(
        select(news_source_queries)
        .where(
            news_source_queries.c.source_code == SOURCE_CODE,
            news_source_queries.c.enabled.is_(True),
        )
        .order_by(news_source_queries.c.code)
    ).fetchall()
    return [dict(row._mapping) for row in rows]


def _request(url: str, *, timeout: float, min_interval: float):
    """Every outbound GDELT request goes through here, retries included."""
    THROTTLE.wait(min_interval)
    # max_attempts=1: retrying inside the fetch layer would put a request on
    # the wire that this throttle never saw.  Retries belong to _run_query.
    return safe_get(
        url,
        allow_hosts=ALLOW_HOSTS,
        max_bytes=MAX_FETCH_BYTES,
        timeout=timeout,
        max_attempts=1,
    )


def parse_articles(
    document: Any,
    *,
    query_text: str,
    query_id: Optional[int],
    fetched_at: datetime,
) -> list[CollectedArticle]:
    """Normalize one artlist response.

    Never raises.  An item with no URL or no title is dropped: it cannot be
    deduped or displayed, and inventing either would be fabrication.
    """
    if not isinstance(document, dict):
        return []
    raw_articles = document.get("articles")
    if not isinstance(raw_articles, list):
        return []

    articles: list[CollectedArticle] = []
    for item in raw_articles:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        seendate_raw = str(item.get("seendate") or "").strip()
        seen_at = parse_compact_utc(seendate_raw)
        language_raw = str(item.get("language") or "").strip()
        provenance: dict[str, Any] = {
            "query_text": query_text,
            "gdelt_seendate": seendate_raw,
            # Crawler observation time, deliberately under its own key: it is
            # not the article's publication time and must never be read as one.
            "gdelt_seen_at": seen_at.isoformat() if seen_at is not None else None,
            "gdelt_domain": str(item.get("domain") or ""),
            "gdelt_language": language_raw,
            "gdelt_source_country": str(item.get("sourcecountry") or ""),
            "gdelt_url_mobile": str(item.get("url_mobile") or ""),
            "publication_time_available": False,
        }
        # artlist does not carry tone today; a mode that does must not turn it
        # into a score by arriving.
        if "tone" in item:
            provenance["unverified"] = {
                "tone": item.get("tone"),
                "note": (
                    "GDELT's own tone number for a document this system never "
                    "read; unverified metadata, not a sentiment feature"
                ),
            }
        articles.append(
            CollectedArticle(
                source_code=SOURCE_CODE,
                canonical=canonical_url(url),
                url=url,
                title=title,
                summary="",
                # The DOC API states no publication time for the article.
                source_published_at=None,
                published_at_is_estimated=True,
                available_at=fetched_at,
                external_id=url,
                language=_LANGUAGE_CODES.get(language_raw.lower(), UNKNOWN_LANGUAGE),
                query_id=query_id,
                provenance=provenance,
            )
        )
    return articles


def collect(engine, settings: Settings, *, dry_run: bool = False) -> dict[str, Any]:
    """Run every enabled query once, throttled, deduped across queries."""
    result = base_result(SOURCE_CODE, PARSER_VERSION, dry_run)
    result.update(
        queries_run=0, queries_failed=0, duplicates_dropped=0, cross_query_duplicates=0
    )
    with engine.begin() as conn:
        source_row = load_source(conn, SOURCE_CODE)
        queries = load_queries(conn)

    started_at = utcnow()
    reason = poll_gate(source_row, settings, now=started_at)
    if reason is not None:
        result["reason"] = reason
        return result
    if not queries:
        result["reason"] = "no_queries_configured"
        return result

    api_url = (source_row or {}).get("feed_url") or DEFAULT_API_URL
    timeout = min(float(settings.http_timeout_seconds), HARD_TIMEOUT_SECONDS)
    interval = min_interval_seconds(settings)

    # Dedupe across queries within the run: two narrow queries about the same
    # topic return the same wire story, and counting it twice would make one
    # article look like two independent sources.  Keyed by canonical URL to the
    # query that first produced it, so "the same story came back twice" and
    # "two queries overlap" stay separable.
    seen_canonical: dict[str, int] = {}
    totals = {"items_seen": 0, "items_new": 0, "items_duplicate": 0}

    for query in queries:
        query_id = int(query["id"])
        query_text = str(query["query_text"])
        request_url = build_query_url(
            query_text,
            max_records=int(query.get("max_records") or 75),
            timespan=DEFAULT_TIMESPAN,
            api_url=api_url,
        )
        outcome = _run_query(
            engine,
            result=result,
            query_id=query_id,
            query_text=query_text,
            request_url=request_url,
            timeout=timeout,
            interval=interval,
            seen_canonical=seen_canonical,
            totals=totals,
            dry_run=dry_run,
        )
        result["queries_run"] += 1
        if outcome == OUTCOME_THROTTLED:
            # The host asked us to stop.  Stop for the whole pass.
            result["status"] = OUTCOME_THROTTLED
            result["reason"] = "rate_limited"
            result.update(totals)
            _finish(engine, result, ok=False, dry_run=dry_run)
            return result
        if outcome != OUTCOME_OK:
            result["queries_failed"] += 1

    result.update(totals)
    any_items = totals["items_seen"] > 0
    all_failed = result["queries_failed"] >= result["queries_run"]
    result["status"] = OUTCOME_OK if any_items else OUTCOME_EMPTY
    if not any_items:
        result["reason"] = result["reason"] or "no_articles_returned"
    _finish(engine, result, ok=not all_failed, dry_run=dry_run)
    return result


def _run_query(
    engine,
    *,
    result: dict[str, Any],
    query_id: int,
    query_text: str,
    request_url: str,
    timeout: float,
    interval: float,
    seen_canonical: dict[str, int],
    totals: dict[str, int],
    dry_run: bool,
) -> str:
    """Fetch and store one query.  Returns the outcome for that query alone."""
    attempt_started = utcnow()
    response = None
    body = ""
    status: Optional[int] = None
    last_error = ""
    # Per query, not per pass: queries are 5s apart by construction, and an
    # available_at borrowed from the start of the pass would claim we held a
    # later query's results before we did.
    received_at = attempt_started

    for attempt in range(1, MAX_ATTEMPTS_PER_QUERY + 1):
        try:
            response = _request(request_url, timeout=timeout, min_interval=interval)
            status = getattr(response, "status_code", None)
            body = getattr(response, "text", "") or ""
            received_at = received_at_of(response)
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            response = None
            status = None
            if attempt >= MAX_ATTEMPTS_PER_QUERY:
                break
            continue
        if status is not None and int(status) in THROTTLE_STATUSES:
            _record_query_failure(
                engine,
                result,
                started_at=attempt_started,
                error_class="rate_limited",
                detail=f"HTTP {status}",
                dry_run=dry_run,
                outcome=OUTCOME_THROTTLED,
                http_status=status,
                query_id=query_id,
            )
            return OUTCOME_THROTTLED
        if status is not None and int(status) in RETRY_STATUSES:
            last_error = f"HTTP {status}"
            if attempt >= MAX_ATTEMPTS_PER_QUERY:
                break
            continue
        break

    if response is None or (status is not None and int(status) != 200):
        _record_query_failure(
            engine,
            result,
            started_at=attempt_started,
            error_class="fetch_error",
            detail=last_error or f"HTTP {status}",
            dry_run=dry_run,
            http_status=status,
            query_id=query_id,
        )
        return "error"

    result["http_status"] = status
    document = parse_json(body, source_code=SOURCE_CODE, max_bytes=MAX_FETCH_BYTES)
    articles = parse_articles(
        document, query_text=query_text, query_id=query_id, fetched_at=received_at
    )

    fresh: list[CollectedArticle] = []
    for article in articles:
        totals["items_seen"] += 1
        first_query = seen_canonical.get(article.canonical)
        if first_query is not None:
            totals["items_duplicate"] += 1
            result["duplicates_dropped"] += 1
            if first_query != query_id:
                result["cross_query_duplicates"] += 1
            continue
        seen_canonical[article.canonical] = query_id
        fresh.append(article)

    if dry_run:
        return OUTCOME_OK

    with engine.begin() as conn:
        raw_payload_id = store_raw_payload(
            conn,
            source_code=SOURCE_CODE,
            request_url=request_url,
            body=body,
            fetched_at=received_at,
            parser_version=PARSER_VERSION,
            http_status=status,
            content_type=content_type_of(response),
            query_id=query_id,
            max_body_chars=MAX_STORED_BODY_CHARS,
        )
    result["raw_payload_id"] = raw_payload_id

    with engine.begin() as conn:
        counts = store_articles(
            conn,
            fresh,
            raw_payload_id=raw_payload_id,
            parser_version=PARSER_VERSION,
            fetched_at=received_at,
        )
        totals["items_new"] += counts["items_new"]
        totals["items_duplicate"] += counts["items_duplicate"]
        record_attempt(
            conn,
            source_code=SOURCE_CODE,
            started_at=attempt_started,
            finished_at=utcnow(),
            outcome=OUTCOME_OK if articles else OUTCOME_EMPTY,
            parser_version=PARSER_VERSION,
            http_status=status,
            bytes_received=len(body.encode("utf-8")),
            items_seen=len(articles),
            items_new=counts["items_new"],
            items_duplicate=counts["items_duplicate"] + len(articles) - len(fresh),
            query_id=query_id,
        )
    return OUTCOME_OK


def _record_query_failure(
    engine,
    result: dict[str, Any],
    *,
    started_at: datetime,
    error_class: str,
    detail: str,
    dry_run: bool,
    http_status: Optional[int],
    query_id: int,
    outcome: str = "error",
) -> None:
    """One query failed; the pass continues unless the host rate-limited us."""
    if dry_run:
        return
    finished_at = utcnow()
    with engine.begin() as conn:
        record_attempt(
            conn,
            source_code=SOURCE_CODE,
            started_at=started_at,
            finished_at=finished_at,
            outcome=outcome,
            parser_version=PARSER_VERSION,
            http_status=http_status,
            error_class=error_class,
            error_detail=detail,
            query_id=query_id,
        )
    result["reason"] = result["reason"] or error_class


def _finish(engine, result: dict[str, Any], *, ok: bool, dry_run: bool) -> None:
    """Stamp the registry row once for the whole pass."""
    if dry_run:
        return
    with engine.begin() as conn:
        mark_polled(
            conn,
            SOURCE_CODE,
            now=utcnow(),
            ok=ok,
            error="" if ok else str(result.get("reason") or "collection failed"),
        )
