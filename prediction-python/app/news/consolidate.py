"""Group articles into events, and count SOURCES rather than copies.

WHY this module exists: the headline number a news panel shows is "how many
sources reported this", and that number decides how much weight the rest of the
system gives an event.  A wire story republished by twenty sites would make
that number twenty while the world contains exactly one report — an
availability cascade dressed up as corroboration.  Everything below is built
around refusing that inflation.

The grouping signals, strongest first, each a named constant so a stored
``method`` says which one held a group together:

* the source's own item id — a source telling us two rows are the same row;
* the canonical URL — identity, after tracking parameters are stripped;
* the content hash — identical normalized text, whoever published it;
* the normalized title key — the same headline under a different URL;
* near-title similarity within a publication-time window;
* entity overlap plus a weaker title match within a tighter window.

Only the last two need a time window, and they need it badly: "Gold prices
rise" is a recurring headline, so an identical title a week later is a
different story.  Identity keys (item id, URL, hash) carry their own proof and
are matched regardless of time.

All lexical work is delegated to :mod:`app.news.dedupe`; this module adds the
grouping policy, not new string similarity.  Comparison is pairwise over one
ingestion batch, which is a few dozen articles — an index would be complexity
without a problem to solve.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Sequence

from . import classify, dedupe, entities

# Bumped when the grouping policy or any threshold below changes; stored on
# every group and member row so a past grouping can be explained by the rules
# that were in force when it was made.
CONSOLIDATION_VERSION = "consolidate-1.0.0"

MATCH_SEED = "seed"                    # the article that started the group
MATCH_SOURCE_ITEM = "source_item_id"
MATCH_CANONICAL_URL = "canonical_url"
MATCH_CONTENT_HASH = "content_hash"
MATCH_TITLE_KEY = "title_key"
MATCH_NEAR_TITLE = "near_title"
MATCH_ENTITY_TIME = "entity_time"

# How much a match of each kind is trusted.  A group's confidence is the
# WEAKEST link that holds it together, because that link is what a reader would
# have to accept to accept the group.
MATCH_CONFIDENCE: Mapping[str, float] = {
    MATCH_SEED: 1.0,
    # The source asserting its own identity is the only signal here that is not
    # an inference.
    MATCH_SOURCE_ITEM: 1.0,
    MATCH_CANONICAL_URL: 0.98,
    MATCH_CONTENT_HASH: 0.95,
    MATCH_TITLE_KEY: 0.90,
    MATCH_NEAR_TITLE: 0.80,
    # The loosest rule in the set: shared actors plus a partial headline match.
    MATCH_ENTITY_TIME: 0.60,
}
# Strength order used to pick the best available edge and to report a group's
# weakest link.
_MATCH_ORDER: tuple[str, ...] = (
    MATCH_SOURCE_ITEM, MATCH_CANONICAL_URL, MATCH_CONTENT_HASH,
    MATCH_TITLE_KEY, MATCH_NEAR_TITLE, MATCH_ENTITY_TIME,
)

# A wire story reaches its last subscriber within hours, not days; beyond this
# the same headline is a follow-up or a recurrence, which is a different event.
NEAR_TITLE_WINDOW_S = 6 * 3600
# The entity rule is looser, so its window is tighter: shared actors mean
# "same story" only inside one news cycle.
ENTITY_WINDOW_S = 3 * 3600
# An identical normalized headline is strong evidence, but recurring headlines
# exist ("Gold prices rise"), so it still gets a generous day-long window.
TITLE_KEY_WINDOW_S = 24 * 3600

NEAR_TITLE_THRESHOLD = dedupe.NEAR_DUPLICATE_THRESHOLD
# The entity rule may not merge on actors alone: "Iran" and "gold" co-occur in
# unrelated stories every day.  It needs a headline that is already halfway to
# a match, which is what this floor is — deliberately below the near-title
# threshold, since the entity overlap is carrying the rest of the argument.
ENTITY_TITLE_FLOOR = 0.55
ENTITY_OVERLAP_THRESHOLD = 0.60

# Above this, a member's headline is a verbatim republication rather than an
# independent newsroom's own words.  Set high because
# ``dedupe.title_similarity`` takes the MAX of an edit ratio and a token-set
# overlap: two newsrooms describing one event in their own words rarely reach
# 0.95 on either, while a syndicated copy with a house-style tweak still does.
SYNDICATION_TITLE_THRESHOLD = 0.95

# A group spanning more than a day was held together by something other than a
# single news cycle; the penalty keeps its confidence honest without discarding
# it, since slow-moving policy stories legitimately span days.
WIDE_SPAN_S = 24 * 3600
WIDE_SPAN_PENALTY = 0.10


@dataclass(frozen=True)
class ArticleInput:
    """One article as consolidation needs to see it.

    ``published_at`` is Optional on purpose: the schema allows a NULL source
    publication time, and this module must never substitute the fetch time for
    it.  ``available_at`` — when we could first have acted on the item — is
    required, because it is the only clock that is always real.
    """

    article_id: int
    source_code: str
    available_at: datetime
    published_at: Optional[datetime] = None
    external_id: str = ""
    url: str = ""
    canonical_url: str = ""
    title: str = ""
    summary: str = ""
    content_hash: str = ""
    entity_codes: Optional[frozenset[str]] = None


@dataclass(frozen=True)
class GroupMember:
    """An article's membership in a group, and the edge that put it there."""

    article_id: int
    similarity: float
    match_reason: str
    is_primary: bool
    # False when this article's text is a republication of an earlier member's.
    is_independent: bool


@dataclass(frozen=True)
class ConsolidatedEvent:
    """One story, however many articles carried it."""

    primary_article_id: int
    members: tuple[GroupMember, ...]
    method: str
    method_version: str
    article_count: int
    independent_source_count: int
    syndication_count: int
    source_diversity: float
    confidence: float
    conflicting: bool
    first_published_at: Optional[datetime]
    first_seen_at: datetime
    last_updated_at: datetime
    available_at: datetime
    source_codes: tuple[str, ...]
    entity_codes: tuple[str, ...]
    categories: tuple[str, ...] = ()

    @property
    def article_ids(self) -> tuple[int, ...]:
        return tuple(member.article_id for member in self.members)


def _utc(value: datetime) -> datetime:
    # Naive timestamps come back from SQLite; the schema stores UTC, so that is
    # what a naive value means.
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical(item: ArticleInput) -> str:
    return item.canonical_url or dedupe.canonical_url(item.url)


def _hash(item: ArticleInput) -> str:
    return item.content_hash or dedupe.content_hash(item.title, item.summary)


def _entities(item: ArticleInput, cache: dict[int, frozenset[str]]) -> frozenset[str]:
    if item.entity_codes is not None:
        return item.entity_codes
    cached = cache.get(item.article_id)
    if cached is None:
        cached = entities.codes(entities.extract(item.title, item.summary))
        cache[item.article_id] = cached
    return cached


def _proximity_seconds(left: ArticleInput, right: ArticleInput) -> float:
    """Seconds between two items on a clock they share.

    Publication times are compared to publication times; if either side lacks
    one, both fall back to ``available_at``.  Mixing the two would compare a
    source's claim against our own fetch stamp, which is not a like-for-like
    interval and would silently widen or narrow the window depending on how
    fast the collector happened to run.
    """
    if left.published_at is not None and right.published_at is not None:
        return abs((_utc(left.published_at) - _utc(right.published_at)).total_seconds())
    return abs((_utc(left.available_at) - _utc(right.available_at)).total_seconds())


def _edge(
    candidate: ArticleInput,
    existing: ArticleInput,
    entity_cache: dict[int, frozenset[str]],
) -> Optional[tuple[str, float]]:
    """The strongest match between two articles, or None."""
    if (
        candidate.external_id
        and candidate.source_code == existing.source_code
        and candidate.external_id == existing.external_id
    ):
        return MATCH_SOURCE_ITEM, 1.0

    candidate_url, existing_url = _canonical(candidate), _canonical(existing)
    if candidate_url and candidate_url == existing_url:
        return MATCH_CANONICAL_URL, 1.0

    if _hash(candidate) == _hash(existing):
        return MATCH_CONTENT_HASH, 1.0

    gap = _proximity_seconds(candidate, existing)
    candidate_key = dedupe.normalize_title(candidate.title)
    if (
        candidate_key
        and candidate_key == dedupe.normalize_title(existing.title)
        and gap <= TITLE_KEY_WINDOW_S
    ):
        return MATCH_TITLE_KEY, 1.0

    similarity = dedupe.title_similarity(candidate.title, existing.title)
    if similarity >= NEAR_TITLE_THRESHOLD and gap <= NEAR_TITLE_WINDOW_S:
        return MATCH_NEAR_TITLE, similarity

    if (
        similarity >= ENTITY_TITLE_FLOOR
        and gap <= ENTITY_WINDOW_S
        and entities.overlap(
            _entities(candidate, entity_cache), _entities(existing, entity_cache)
        )
        >= ENTITY_OVERLAP_THRESHOLD
    ):
        return MATCH_ENTITY_TIME, similarity
    return None


def _sort_key(item: ArticleInput) -> tuple[float, float, int]:
    published = _utc(item.published_at) if item.published_at is not None else None
    available = _utc(item.available_at)
    # Chronological by the earliest evidence we have, with the id as the
    # tie-break so identical timestamps cannot reorder between runs.
    return ((published or available).timestamp(), available.timestamp(), item.article_id)


def _independence(
    ordered: Sequence[ArticleInput],
) -> tuple[dict[int, bool], int, int]:
    """Which members contribute an independent report, and the counts.

    A member is a syndicated copy when its normalized text (or near-verbatim
    headline) was already contributed by an EARLIER member — that is what makes
    twenty republications of one wire story count as one source rather than
    twenty.  Independent sources are then the DISTINCT source codes among the
    members that did contribute their own words; a source publishing three
    updates of its own story still counts once.
    """
    independent_flags: dict[int, bool] = {}
    seen_hashes: set[str] = set()
    originals: list[ArticleInput] = []
    independent_sources: set[str] = set()
    for item in ordered:
        digest = _hash(item)
        verbatim = digest in seen_hashes or any(
            dedupe.title_similarity(item.title, earlier.title)
            >= SYNDICATION_TITLE_THRESHOLD
            for earlier in originals
        )
        independent_flags[item.article_id] = not verbatim
        seen_hashes.add(digest)
        if not verbatim:
            originals.append(item)
            independent_sources.add(item.source_code)
    syndication_count = sum(1 for flag in independent_flags.values() if not flag)
    return independent_flags, len(independent_sources), syndication_count


def _conflicting(originals: Sequence[ArticleInput]) -> tuple[bool, tuple[str, ...]]:
    """True when two independent members argue opposite directions.

    Opposition is read off the classifier's own map of mirrored rules, so
    "sanctions imposed" versus "sanctions lifted" in the same group is flagged
    rather than averaged away.  Verbatim copies are excluded: they cannot
    disagree with each other by construction.
    """
    rule_ids: list[str] = []
    categories: list[str] = []
    for item in originals:
        result = classify.classify(item.title, item.summary)
        if result.is_classified:
            rule_ids.append(result.primary.rule_id)
            categories.append(result.category)
    conflict = any(
        other in classify.OPPOSED_RULES.get(rule_id, frozenset())
        for index, rule_id in enumerate(rule_ids)
        for other in rule_ids[index + 1:]
    )
    return conflict, tuple(sorted(set(categories)))


def consolidate(
    articles: Iterable[ArticleInput], detect_conflicts: bool = True
) -> tuple[ConsolidatedEvent, ...]:
    """Group a batch of articles into events.

    Articles are processed oldest-first so a group's primary is the first
    article we could have acted on, and every later member records the edge
    that attached it.  Groups are returned in that same chronological order.
    """
    ordered = sorted(articles, key=_sort_key)
    entity_cache: dict[int, frozenset[str]] = {}

    groups: list[list[ArticleInput]] = []
    edges: list[dict[int, tuple[str, float]]] = []
    for item in ordered:
        best_group: Optional[int] = None
        best_edge: Optional[tuple[str, float]] = None
        for index, members in enumerate(groups):
            for existing in members:
                edge = _edge(item, existing, entity_cache)
                if edge is None:
                    continue
                if best_edge is None or _MATCH_ORDER.index(edge[0]) < _MATCH_ORDER.index(
                    best_edge[0]
                ):
                    best_group, best_edge = index, edge
            if best_edge is not None and best_edge[0] == MATCH_SOURCE_ITEM:
                # Nothing can beat the source's own identity claim.
                break
        if best_group is None or best_edge is None:
            groups.append([item])
            edges.append({item.article_id: (MATCH_SEED, 1.0)})
        else:
            groups[best_group].append(item)
            edges[best_group][item.article_id] = best_edge

    return tuple(
        _build_event(members, edges[index], detect_conflicts)
        for index, members in enumerate(groups)
    )


def _build_event(
    members: Sequence[ArticleInput],
    edges: Mapping[int, tuple[str, float]],
    detect_conflicts: bool,
) -> ConsolidatedEvent:
    ordered = sorted(members, key=_sort_key)
    primary = ordered[0]
    independent_flags, independent_sources, syndication_count = _independence(ordered)

    reasons = [edges[item.article_id][0] for item in ordered
               if edges[item.article_id][0] != MATCH_SEED]
    weakest = max(reasons, key=_MATCH_ORDER.index) if reasons else MATCH_SEED

    published = [_utc(item.published_at) for item in ordered if item.published_at is not None]
    available = [_utc(item.available_at) for item in ordered]
    span = max(available) - min(available)
    confidence = MATCH_CONFIDENCE[weakest]
    if span.total_seconds() > WIDE_SPAN_S:
        confidence -= WIDE_SPAN_PENALTY

    conflicting, categories = (False, ())
    if detect_conflicts:
        conflicting, categories = _conflicting(
            [item for item in ordered if independent_flags[item.article_id]]
        )

    entity_codes: set[str] = set()
    for item in ordered:
        if item.entity_codes is not None:
            entity_codes |= item.entity_codes
        else:
            entity_codes |= entities.codes(entities.extract(item.title, item.summary))

    return ConsolidatedEvent(
        primary_article_id=primary.article_id,
        members=tuple(
            GroupMember(
                article_id=item.article_id,
                similarity=round(edges[item.article_id][1], 4),
                match_reason=edges[item.article_id][0],
                is_primary=item.article_id == primary.article_id,
                is_independent=independent_flags[item.article_id],
            )
            for item in ordered
        ),
        method=weakest,
        method_version=CONSOLIDATION_VERSION,
        article_count=len(ordered),
        independent_source_count=independent_sources,
        syndication_count=syndication_count,
        # The share of the group that is original reporting rather than copies.
        source_diversity=round(independent_sources / len(ordered), 4),
        confidence=round(max(0.0, min(1.0, confidence)), 4),
        conflicting=conflicting,
        # NULL when no member stated a publication time: a group of items whose
        # sources gave no timestamp has no first publication time, and the
        # first time WE saw it is a different fact, kept in first_seen_at.
        first_published_at=min(published) if published else None,
        first_seen_at=min(available),
        last_updated_at=max(available),
        # When we could first have acted on this story.
        available_at=min(available),
        source_codes=tuple(sorted({item.source_code for item in ordered})),
        entity_codes=tuple(sorted(entity_codes)),
        categories=categories,
    )


def duplicate_group_row(event: ConsolidatedEvent) -> dict:
    """Row for ``news_duplicate_groups``."""
    return {
        "primary_article_id": event.primary_article_id,
        "method": event.method,
        "method_version": event.method_version,
        "article_count": event.article_count,
        "independent_source_count": event.independent_source_count,
        "syndication_count": event.syndication_count,
        "source_diversity": event.source_diversity,
        "first_published_at": event.first_published_at,
        "first_seen_at": event.first_seen_at,
        "last_updated_at": event.last_updated_at,
        "conflicting": event.conflicting,
        "confidence": event.confidence,
    }


def article_duplicate_rows(group_id: int, event: ConsolidatedEvent) -> list[dict]:
    """Rows for ``news_article_duplicates``."""
    return [
        {
            "group_id": group_id,
            "article_id": member.article_id,
            "similarity": member.similarity,
            "match_reason": member.match_reason,
            "method_version": event.method_version,
            "is_primary": member.is_primary,
        }
        for member in event.members
    ]


def event_consolidation_fields(event: ConsolidatedEvent, group_id: Optional[int] = None) -> dict:
    """The ``news_events`` columns consolidation owns.

    Deliberately partial: category, event time and polarity belong to the
    classifier and the ingestion job, and this module has no business guessing
    them.
    """
    return {
        "duplicate_group_id": group_id,
        "available_at": event.available_at,
        "independent_source_count": event.independent_source_count,
        "consolidation_method": event.method,
        "consolidation_version": event.method_version,
        "conflicting": event.conflicting,
    }
