"""Federal Reserve Board press releases — the one approved news source.

    GET https://www.federalreserve.gov/feeds/press_all.xml

An official public syndication feed of a government body: no account, no key,
no CAPTCHA, not robots-disallowed, and reachable from the collector host.  It
is fetched with the project's honest User-Agent at the courtesy cadence stored
on its ``news_sources`` row (docs/CONTRACTS.md source policy).

Parsing uses stdlib ``xml.etree`` only — one RSS 2.0 document does not justify
a feed dependency on a single small host.

Timestamps.  ``<pubDate>`` (RFC 822, e.g. ``Wed, 15 Jul 2026 14:00:00 EST``) is
the PUBLICATION time; the ingestion time is stamped separately by the caller
and the two are never substituted for each other.  When no timestamp parses,
the ingestion time is used and flagged ``published_at_estimated`` — deliberately
the conservative direction, because an over-late publication time can only make
a future feature ignore an item, never use it before it was public.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional
from xml.etree import ElementTree

from ..db import utcnow
from ..news import ArticleRecord, NewsProvider
from .base import ProviderError

log = logging.getLogger(__name__)

FEED_URL = "https://www.federalreserve.gov/feeds/press_all.xml"
DC_NS = "{http://purl.org/dc/elements/1.1/}"

# The full press feed is a few hundred KB.  ElementTree has no entity-expansion
# guard and the project may not add defusedxml, so an implausibly large body is
# refused before parsing rather than handed to the parser.
MAX_FEED_CHARS = 8_000_000


def _text(item: ElementTree.Element, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None and node.text else ""


def parse_pub_date(raw: str) -> tuple[Optional[datetime], bool]:
    """``(aware UTC datetime, is_estimated)`` for one RFC-822 date string.

    A date without a timezone is read as UTC *and* flagged estimated: the true
    offset could move it by hours, which matters when the timestamp is a
    leakage boundary.
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


def parse_iso_date(raw: str) -> tuple[Optional[datetime], bool]:
    """``(aware UTC datetime, is_estimated)`` for a ``dc:date`` ISO-8601 string."""
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


def publication_time(
    item: ElementTree.Element, ingested_at: datetime
) -> tuple[datetime, bool]:
    """Publication time of one item, falling back to ingestion time."""
    for raw, parser in (
        (_text(item, "pubDate"), parse_pub_date),
        (_text(item, f"{DC_NS}date"), parse_iso_date),
    ):
        published, estimated = parser(raw)
        if published is not None:
            return published, estimated
    return ingested_at, True


def parse_feed(
    xml_text: str, ingested_at: datetime, source_code: str = "fed_press"
) -> list[ArticleRecord]:
    """Parse an RSS 2.0 document into article records.

    Never raises: a malformed or truncated feed yields an empty list, because
    the ingestion job must treat "nothing parseable" as a source problem to log
    rather than an exception that sinks the pass.
    """
    if not xml_text or not xml_text.strip():
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        log.warning("%s: unparseable feed XML: %s", source_code, exc)
        return []

    channel_language = ""
    channel = root.find("channel")
    if channel is not None:
        channel_language = _text(channel, "language")

    records: list[ArticleRecord] = []
    for item in root.iter("item"):
        title = _text(item, "title")
        link = _text(item, "link")
        if not title and not link:
            continue  # an item that identifies nothing cannot be deduped
        published_at, estimated = publication_time(item, ingested_at)
        categories = [
            (node.text or "").strip()
            for node in item.findall("category")
            if node.text and node.text.strip()
        ]
        records.append(
            ArticleRecord(
                source_code=source_code,
                external_id=_text(item, "guid"),
                url=link,
                title=title,
                summary=_text(item, "description"),
                published_at=published_at,
                ingested_at=ingested_at,
                published_at_estimated=estimated,
                language=(
                    _text(item, f"{DC_NS}language") or channel_language or "en"
                ).split("-")[0].lower(),
                raw_payload={
                    "pub_date": _text(item, "pubDate"),
                    "dc_date": _text(item, f"{DC_NS}date"),
                    "categories": categories,
                },
            )
        )
    return records


class FedPressProvider(NewsProvider):
    """Public RSS feed of Federal Reserve Board press releases (keyless)."""

    code = "fed_press"
    category = "news"

    def fetch_articles(
        self, ingested_at: Optional[datetime] = None
    ) -> list[ArticleRecord]:
        at = ingested_at or utcnow()
        body = self._get_text(FEED_URL)
        if len(body) > MAX_FEED_CHARS:
            raise ProviderError(
                f"{self.code}: feed body of {len(body)} chars exceeds the "
                f"{MAX_FEED_CHARS} limit; refusing to parse"
            )
        records = parse_feed(body, at, source_code=self.code)
        if not records:
            # An always-populated feed returning nothing is a source problem
            # (challenge page, truncated body); surface it as a health failure.
            raise ProviderError(f"{self.code}: feed contained no parseable items")
        return records
