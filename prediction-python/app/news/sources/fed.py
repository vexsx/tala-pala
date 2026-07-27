"""Federal Reserve Board press releases (RSS) — evidence-first collector.

    GET https://www.federalreserve.gov/feeds/press_all.xml

An official public syndication feed of a government body: no account, no key,
no CAPTCHA, not robots-disallowed.  It is the one source this project fetches
without reservation, and 0016 already registers it ``approved``.

Two things this collector does that ``app/providers/fedpress.py`` does not:

*It stores the feed body first.*  A press release page is edited in place and
the feed window rolls; the stored payload plus its sha256 is the only copy of
what the source actually said at the moment we read it.

*It never substitutes the fetch time for a publication time.*  The feed's own
``pubDate`` (RFC-822) is the publication claim; ``dc:date`` (ISO-8601) is the
fallback.  When neither parses, ``source_published_at`` stays None and
``published_at_is_estimated`` is set — the difference between "we do not know
when this was published" and "it was published at 14:02:11 UTC" is exactly the
difference a leakage-safe cutoff depends on.

Release kind is derived from the URL and title by ordered, versioned rules and
stored with the rule id that fired.  Kind is a property of the DOCUMENT, not a
market judgement: a routine speech is low importance however hawkish it reads,
because reading it is a classifier's job and this file only records facts.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from ...config import Settings
from ...db import utcnow
from ..safefetch import safe_get
from . import (
    OUTCOME_EMPTY,
    OUTCOME_OK,
    CollectedArticle,
    available_from,
    base_result,
    canonical_url,
    content_type_of,
    load_source,
    mark_polled,
    parse_iso8601,
    parse_rfc822,
    parse_xml,
    poll_gate,
    record_attempt,
    record_failure,
    store_articles,
    store_raw_payload,
)

SOURCE_CODE = "fed_press"
PARSER_VERSION = "fed_rss_v1"
KIND_RULES_VERSION = "fed_kind_v1"

DEFAULT_FEED_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
ALLOW_HOSTS = ("www.federalreserve.gov", "federalreserve.gov")

# The full press feed is a few hundred KB; anything near the cap is a challenge
# page or a broken response, not a feed.
MAX_FETCH_BYTES = 8_000_000
MAX_STORED_BODY_CHARS = 4_000_000
HARD_TIMEOUT_SECONDS = 30.0

DC_NS = "{http://purl.org/dc/elements/1.1/}"

# Document kinds.  Deliberately coarse: these are the shapes the feed actually
# carries, and a finer taxonomy would be a classification, not a parse.
KIND_FOMC_STATEMENT = "fomc_statement"
KIND_FOMC_MINUTES = "fomc_minutes"
KIND_SPEECH = "speech"
KIND_PRESS_RELEASE = "press_release"
KIND_OTHER = "other"

# Importance of the DOCUMENT TYPE, not of its content.  Only the two scheduled
# FOMC publications are high: their release times are announced in advance and
# rates markets are positioned for them.  Everything else — including every
# speech, however senior the speaker — is routine until something downstream
# measures otherwise, and nothing has measured anything yet.
IMPORTANCE_BY_KIND = {
    KIND_FOMC_STATEMENT: "high",
    KIND_FOMC_MINUTES: "high",
    KIND_SPEECH: "low",
    KIND_PRESS_RELEASE: "low",
    KIND_OTHER: "low",
}


class ReleaseKind:
    """Kind of a press item plus the rule that decided it."""

    __slots__ = ("kind", "importance", "rule_id")

    def __init__(self, kind: str, rule_id: str) -> None:
        self.kind = kind
        self.importance = IMPORTANCE_BY_KIND[kind]
        self.rule_id = f"{KIND_RULES_VERSION}.{rule_id}"

    def as_dict(self) -> dict[str, str]:
        return {
            "release_kind": self.kind,
            "importance": self.importance,
            "kind_rule_id": self.rule_id,
            "kind_rules_version": KIND_RULES_VERSION,
        }


def classify_release(title: str, url: str) -> ReleaseKind:
    """Kind of one press item, by URL first and title second.

    Order is load-bearing.  The speech test runs before the FOMC tests because
    a speech ABOUT the FOMC statement is still a speech, and the minutes test
    runs before the statement test because a minutes release names the
    committee too.  URL evidence beats title wording throughout: the Board's
    own path segments are stable, headlines are prose.
    """
    path = (url or "").lower()
    text = (title or "").lower()

    if "/newsevents/speech/" in path or "/speeches/" in path:
        return ReleaseKind(KIND_SPEECH, "speech_url")
    if "/newsevents/testimony/" in path:
        # Testimony is prepared remarks to Congress: not a speech in the feed's
        # own taxonomy and not a press release either.
        return ReleaseKind(KIND_OTHER, "testimony_url")
    if "fomcminutes" in path.replace("_", "").replace("-", ""):
        return ReleaseKind(KIND_FOMC_MINUTES, "minutes_url")
    if "minutes" in text and ("fomc" in text or "federal open market committee" in text):
        return ReleaseKind(KIND_FOMC_MINUTES, "minutes_title")
    if "fomc statement" in text or (
        "federal open market committee" in text and "statement" in text
    ):
        return ReleaseKind(KIND_FOMC_STATEMENT, "statement_title")
    if "/newsevents/pressreleases/" in path:
        return ReleaseKind(KIND_PRESS_RELEASE, "press_release_url")
    return ReleaseKind(KIND_OTHER, "unmatched")


def _text(node, tag: str) -> str:
    if node is None:
        return ""
    found = node.find(tag)
    return (found.text or "").strip() if found is not None and found.text else ""


def _publication_time(item) -> tuple[Optional[datetime], bool, dict[str, str]]:
    """``(source_published_at | None, is_estimated, raw strings)`` for one item."""
    pub_date = _text(item, "pubDate")
    dc_date = _text(item, f"{DC_NS}date")
    raw = {"pub_date": pub_date, "dc_date": dc_date}
    for value, parser in ((pub_date, parse_rfc822), (dc_date, parse_iso8601)):
        parsed, estimated = parser(value)
        if parsed is not None:
            return parsed, estimated, raw
    # No parseable claim.  Do NOT fall back to the fetch time here: the caller
    # needs to be able to see that the source said nothing.
    return None, True, raw


def parse_releases(
    xml_text: str, *, fetched_at: datetime, source_code: str = SOURCE_CODE
) -> list[CollectedArticle]:
    """Parse an RSS/Atom press feed into collected articles.

    Never raises: an unparseable or truncated body yields an empty list so the
    caller records it as a source problem rather than an exception that sinks
    the pass.
    """
    root = parse_xml(xml_text, source_code=source_code)
    if root is None:
        return []

    channel = root.find("channel")
    channel_language = _text(channel, "language") if channel is not None else ""

    articles: list[CollectedArticle] = []
    # RSS <item> and Atom <entry>: the Board publishes RSS, but the feed URL has
    # changed format before and an Atom body must not silently parse to zero.
    items = list(root.iter("item")) or [
        node for node in root.iter() if node.tag.endswith("}entry")
    ]
    for item in items:
        title = _text(item, "title")
        link = _text(item, "link")
        if not link:
            link_node = item.find("{http://www.w3.org/2005/Atom}link")
            if link_node is not None:
                link = (link_node.get("href") or "").strip()
        if not title and not link:
            continue  # an item that identifies nothing cannot be deduped
        published_at, estimated, raw_dates = _publication_time(item)
        kind = classify_release(title, link)
        categories = [
            (node.text or "").strip()
            for node in item.findall("category")
            if node.text and node.text.strip()
        ]
        provenance: dict[str, Any] = {
            "guid": _text(item, "guid"),
            "categories": categories,
            "feed_language": channel_language,
        }
        provenance.update(raw_dates)
        provenance.update(kind.as_dict())
        articles.append(
            CollectedArticle(
                source_code=source_code,
                canonical=canonical_url(link),
                url=link,
                title=title,
                summary=_text(item, "description") or _text(item, "summary"),
                source_published_at=published_at,
                published_at_is_estimated=estimated,
                available_at=available_from(published_at, fetched_at),
                external_id=_text(item, "guid") or link,
                language=(
                    _text(item, f"{DC_NS}language") or channel_language or "en"
                ).split("-")[0].lower(),
                provenance=provenance,
            )
        )
    return articles


def collect(engine, settings: Settings, *, dry_run: bool = False) -> dict[str, Any]:
    """Fetch the press feed, store the body, then store what parsed out of it.

    ``dry_run`` fetches and parses but writes nothing, so an operator can
    verify the source without accumulating rows.  It does not bypass the
    courtesy interval: a dry run still costs the source a request.
    """
    result = base_result(SOURCE_CODE, PARSER_VERSION, dry_run)
    with engine.begin() as conn:
        source_row = load_source(conn, SOURCE_CODE)

    started_at = utcnow()
    reason = poll_gate(source_row, settings, now=started_at)
    if reason is not None:
        result["reason"] = reason
        return result

    feed_url = (source_row or {}).get("feed_url") or DEFAULT_FEED_URL
    timeout = min(float(settings.http_timeout_seconds), HARD_TIMEOUT_SECONDS)
    try:
        response = safe_get(
            feed_url,
            allow_hosts=ALLOW_HOSTS,
            max_bytes=MAX_FETCH_BYTES,
            timeout=timeout,
        )
        status = getattr(response, "status_code", None)
        body = getattr(response, "text", "") or ""
    except Exception as exc:
        return record_failure(
            engine,
            result,
            started_at=started_at,
            error_class="fetch_error",
            detail=f"{type(exc).__name__}: {exc}",
            dry_run=dry_run,
        )

    result["http_status"] = status
    if status is not None and int(status) != 200:
        return record_failure(
            engine,
            result,
            started_at=started_at,
            error_class="http_status",
            detail=f"HTTP {status}",
            dry_run=dry_run,
            http_status=status,
            bytes_received=len(body.encode("utf-8")),
        )

    if dry_run:
        articles = parse_releases(body, fetched_at=started_at)
        result["status"] = OUTCOME_OK if articles else OUTCOME_EMPTY
        result["items_seen"] = len(articles)
        result["reason"] = "dry_run"
        return result

    # The payload is committed on its own so a parser failure cannot destroy
    # the only copy of a feed window that has already rolled.
    with engine.begin() as conn:
        raw_payload_id = store_raw_payload(
            conn,
            source_code=SOURCE_CODE,
            request_url=feed_url,
            body=body,
            fetched_at=started_at,
            parser_version=PARSER_VERSION,
            http_status=status,
            content_type=content_type_of(response),
            max_body_chars=MAX_STORED_BODY_CHARS,
        )
    result["raw_payload_id"] = raw_payload_id

    articles = parse_releases(body, fetched_at=started_at)
    finished_at = utcnow()
    with engine.begin() as conn:
        counts = store_articles(
            conn,
            articles,
            raw_payload_id=raw_payload_id,
            parser_version=PARSER_VERSION,
            fetched_at=started_at,
        )
        result.update(counts)
        empty = not articles
        result["status"] = OUTCOME_EMPTY if empty else OUTCOME_OK
        if empty:
            # An always-populated feed returning nothing is a source problem
            # (challenge page, truncated body), not a quiet news day.
            result["reason"] = "no_parseable_items"
        record_attempt(
            conn,
            source_code=SOURCE_CODE,
            started_at=started_at,
            finished_at=finished_at,
            outcome=result["status"],
            parser_version=PARSER_VERSION,
            http_status=status,
            bytes_received=len(body.encode("utf-8")),
            items_seen=counts["items_seen"],
            items_new=counts["items_new"],
            items_duplicate=counts["items_duplicate"],
            error_class="empty_feed" if empty else None,
        )
        mark_polled(
            conn,
            SOURCE_CODE,
            now=finished_at,
            ok=not empty,
            error="feed contained no parseable items" if empty else "",
        )
    return result
