"""Tests for article consolidation and the intelligence snapshot/delta engine.

Both modules exist to resist the same failure: mistaking repetition for
evidence.  Consolidation must count SOURCES, not copies, so twenty
republications of one wire story stay one independent source; the snapshot must
report NULL where it has no evidence rather than a comfortable zero; and the
delta engine must only call something an escalation when it crosses a threshold
that was justified in advance.

These live together because they are one pipeline stage: consolidation decides
how much a story is worth, and the snapshot spends exactly that.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.news import classify, consolidate, intelligence

T0 = datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc)

WIRE_TITLE = "Iran and the US resumed nuclear talks and reported progress"
WIRE_SUMMARY = "Officials said the sides agreed to meet again next week."


def _article(article_id, source_code, title=WIRE_TITLE, summary=WIRE_SUMMARY,
             minutes=0, external_id="", url="", published=True):
    stamp = T0 + timedelta(minutes=minutes)
    return consolidate.ArticleInput(
        article_id=article_id,
        source_code=source_code,
        available_at=stamp,
        published_at=stamp if published else None,
        external_id=external_id,
        url=url or f"https://{source_code}.example/story/{article_id}",
        title=title,
        summary=summary,
    )


# --- syndication --------------------------------------------------------------


def test_twenty_syndicated_copies_are_one_event_with_one_independent_source():
    copies = [
        _article(index, f"wire_site_{index}", minutes=index * 3)
        for index in range(20)
    ]
    events = consolidate.consolidate(copies)

    assert len(events) == 1
    event = events[0]
    assert event.article_count == 20
    # Twenty outlets, one report.
    assert event.independent_source_count == 1
    assert event.syndication_count == 19
    assert event.source_diversity == pytest.approx(1 / 20)
    assert event.method == consolidate.MATCH_CONTENT_HASH
    assert event.primary_article_id == 0
    assert sum(1 for member in event.members if member.is_independent) == 1


def test_independent_newsrooms_each_count_once():
    first = _article(1, "reuters", title="US imposes new sanctions on Iran oil exports",
                     summary="The Treasury blacklisted shipping firms.")
    second = _article(2, "irna", minutes=20,
                      title="Washington imposes fresh sanctions targeting Iranian oil exports",
                      summary="Iran condemned the measure.")
    event = consolidate.consolidate([first, second])[0]

    assert event.article_count == 2
    assert event.independent_source_count == 2
    assert event.syndication_count == 0
    assert all(member.is_independent for member in event.members)


def test_one_source_publishing_updates_does_not_inflate_independence():
    original = _article(1, "reuters", title="Iran nuclear talks resume in Vienna")
    update = _article(2, "reuters", minutes=45,
                      title="Iran nuclear talks resume in Vienna as delegations arrive",
                      summary="Updated with delegation arrivals.")
    event = consolidate.consolidate([original, update])[0]

    assert event.article_count == 2
    assert event.independent_source_count == 1


# --- grouping signals ---------------------------------------------------------


def test_source_item_id_groups_regardless_of_elapsed_time():
    first = _article(1, "fed_press", external_id="guid-1")
    # Same feed item seen a week later with a rewritten headline: identity
    # keys carry their own proof, so no time window applies.
    later = _article(2, "fed_press", external_id="guid-1", minutes=60 * 24 * 7,
                     title="Something else entirely about monetary policy")
    event = consolidate.consolidate([first, later])[0]

    assert event.article_count == 2
    assert event.method == consolidate.MATCH_SOURCE_ITEM


def test_canonical_url_groups_across_tracking_parameters():
    plain = _article(1, "reuters", url="https://reuters.example/a/1")
    tagged = _article(2, "reuters", minutes=5,
                      url="https://www.reuters.example/a/1?utm_source=twitter",
                      title="A completely different headline about something else")
    event = consolidate.consolidate([plain, tagged])[0]

    assert event.article_count == 2
    assert event.method == consolidate.MATCH_CANONICAL_URL


def test_recurring_headline_outside_the_window_is_a_separate_event():
    today = _article(1, "tgju", title="Gold prices rise in the Tehran bazaar",
                     summary="Dealers reported firmer quotes.")
    next_week = _article(
        2, "isna", minutes=60 * 24 * 7,
        title="Gold prices rise in the Tehran bazaar",
        summary="Dealers reported firmer quotes again.",
        url="https://isna.example/a/2",
    )
    events = consolidate.consolidate([today, next_week])

    assert len(events) == 2
    assert all(event.independent_source_count == 1 for event in events)


def test_group_confidence_is_the_weakest_link():
    strong = consolidate.consolidate(
        [_article(1, "a", external_id="g"), _article(2, "a", external_id="g", minutes=5)]
    )[0]
    weak = consolidate.consolidate(
        [
            _article(1, "reuters", title="US imposes new sanctions on Iran oil exports",
                     summary="Treasury blacklists tankers."),
            _article(2, "irna", minutes=30,
                     title="Fresh US sanctions target Iranian oil exports",
                     summary="Tehran rejected the move."),
        ]
    )[0]
    assert strong.confidence == consolidate.MATCH_CONFIDENCE[strong.method]
    assert weak.confidence < strong.confidence


def test_group_of_contradicting_reports_is_flagged_conflicting():
    imposed = _article(1, "reuters", title="US imposes new sanctions on Iran oil exports",
                       summary="Treasury blacklists tankers.")
    lifted = _article(2, "irna", minutes=30,
                      title="US lifted sanctions on Iran oil exports",
                      summary="Funds were released.")
    event = consolidate.consolidate([imposed, lifted])[0]

    assert event.article_count == 2
    assert event.conflicting is True


def test_conflict_detection_can_be_switched_off():
    imposed = _article(1, "reuters", title="US imposes new sanctions on Iran oil exports")
    lifted = _article(2, "irna", minutes=30, title="US lifted sanctions on Iran oil exports")
    event = consolidate.consolidate([imposed, lifted], detect_conflicts=False)[0]
    assert event.conflicting is False


# --- timestamps and persisted shapes -----------------------------------------


def test_missing_publication_time_never_becomes_a_publication_time():
    undated = _article(1, "gdelt", published=False)
    event = consolidate.consolidate([undated])[0]

    # The source gave no publication time, so the group has none — the fetch
    # stamp lives in first_seen_at/available_at and nowhere else.
    assert event.first_published_at is None
    assert event.first_seen_at == T0
    assert event.available_at == T0


def test_available_at_is_the_earliest_moment_we_could_act():
    early = _article(1, "reuters", minutes=0)
    late = _article(2, "wire_site", minutes=90)
    event = consolidate.consolidate([early, late])[0]
    assert event.available_at == T0
    assert event.last_updated_at == T0 + timedelta(minutes=90)


def test_persisted_rows_carry_method_and_version():
    copies = [_article(index, f"site_{index}", minutes=index) for index in range(3)]
    event = consolidate.consolidate(copies)[0]

    group_row = consolidate.duplicate_group_row(event)
    assert group_row["independent_source_count"] == 1
    assert group_row["article_count"] == 3
    assert group_row["method_version"] == consolidate.CONSOLIDATION_VERSION

    member_rows = consolidate.article_duplicate_rows(11, event)
    assert len(member_rows) == 3
    assert sum(1 for row in member_rows if row["is_primary"]) == 1
    assert all(row["group_id"] == 11 for row in member_rows)

    event_fields = consolidate.event_consolidation_fields(event, group_id=11)
    assert event_fields["independent_source_count"] == 1
    assert event_fields["consolidation_version"] == consolidate.CONSOLIDATION_VERSION
    assert event_fields["available_at"] == event.available_at


# --- intelligence snapshots ---------------------------------------------------


def _evidence(event_id, headline, summary="", minutes=0, sources=("reuters",),
              independent=1):
    result = classify.classify(headline, summary)
    return intelligence.EventEvidence(
        event_id=event_id,
        category=result.category,
        available_at=T0 + timedelta(minutes=minutes),
        hypotheses=result.hypotheses,
        independent_source_count=independent,
        source_codes=sources,
    )


def _snapshot(scores, freshness_s=60.0, stale=False, event_ids=(),
              category_counts=None, source_health=None, weights=None):
    """A snapshot with dimension scores set directly, for threshold tests."""
    dimensions = tuple(
        intelligence.DimensionResult(
            dimension=name,
            score=scores.get(name),
            confidence=None if scores.get(name) is None else 0.5,
            total_weight=0.0 if scores.get(name) is None else 1.0,
            event_ids=tuple(event_ids),
            supporting_event_ids=(),
            conflicting_event_ids=(),
        )
        for name in intelligence.DIMENSIONS
    )
    return intelligence.Snapshot(
        captured_at=T0,
        calc_version=intelligence.CALC_VERSION,
        dimensions=dimensions,
        supporting_event_ids=tuple(event_ids),
        conflicting_event_ids=(),
        source_reliability=None,
        data_freshness_s=freshness_s,
        stale=stale,
        limitations="",
        inputs={
            "event_ids": list(event_ids),
            "category_counts": dict(category_counts or {}),
            "source_health": dict(source_health or {}),
        },
        event_weights=dict(weights or {}),
    )


def test_dimensions_without_evidence_are_null_not_zero():
    snapshot = intelligence.build_snapshot(
        [_evidence(1, "Fed signals further rate hikes as FOMC turns hawkish")],
        T0 + timedelta(minutes=5),
    )
    assert snapshot.score("us_macro_pressure") is not None
    # No Iranian policy news in the window: unknown, not calm.
    assert snapshot.score("domestic_policy_pressure") is None
    row = snapshot.as_row()
    assert "domestic_policy_pressure" in row["scores"]
    assert row["scores"]["domestic_policy_pressure"] is None
    assert row["confidence"]["domestic_policy_pressure"] is None
    assert "NULL, not 0" in row["limitations"]


def test_empty_window_is_stale_and_says_so():
    snapshot = intelligence.build_snapshot([], T0)
    assert all(item.score is None for item in snapshot.dimensions)
    assert snapshot.stale is True
    assert snapshot.data_freshness_s is None
    assert "No events in the window" in snapshot.limitations


def test_source_reliability_is_null_when_not_supplied():
    snapshot = intelligence.build_snapshot(
        [_evidence(1, "US imposes new sanctions on Iran oil exports",
                   "Treasury blacklists tankers.")],
        T0 + timedelta(minutes=1),
    )
    assert snapshot.source_reliability is None
    assert "NULL rather than assumed" in snapshot.limitations

    with_reliability = intelligence.build_snapshot(
        [_evidence(1, "US imposes new sanctions on Iran oil exports",
                   "Treasury blacklists tankers.")],
        T0 + timedelta(minutes=1),
        source_reliability={"reuters": 0.9},
    )
    assert with_reliability.source_reliability == pytest.approx(0.9)


def test_scores_stay_bounded_and_confidence_is_capped():
    events = [
        _evidence(index, "US imposes new sanctions on Iran oil exports",
                  "Treasury blacklists tankers.", minutes=index, independent=5)
        for index in range(12)
    ]
    snapshot = intelligence.build_snapshot(events, T0 + timedelta(minutes=15))
    for item in snapshot.dimensions:
        if item.score is not None:
            assert -1.0 <= item.score <= 1.0
            assert item.confidence <= intelligence.MAX_DIMENSION_CONFIDENCE


def test_a_single_source_event_weighs_less_than_a_corroborated_one():
    lonely = intelligence.build_snapshot(
        [_evidence(1, "US imposes new sanctions on Iran oil exports",
                   "Treasury blacklists tankers.", independent=1)],
        T0 + timedelta(minutes=1),
    )
    corroborated = intelligence.build_snapshot(
        [_evidence(1, "US imposes new sanctions on Iran oil exports",
                   "Treasury blacklists tankers.", independent=3)],
        T0 + timedelta(minutes=1),
    )
    assert (
        lonely.dimension("usd_irt").total_weight
        < corroborated.dimension("usd_irt").total_weight
    )
    assert "single independent source" in lonely.limitations


def test_conflicting_events_lower_confidence_without_hiding_either():
    both = intelligence.build_snapshot(
        [
            _evidence(1, "US imposes new sanctions on Iran oil exports",
                      "Treasury blacklists tankers."),
            _evidence(2, "Washington lifted sanctions on Iran and released frozen funds",
                      minutes=5),
        ],
        T0 + timedelta(minutes=10),
    )
    one_sided = intelligence.build_snapshot(
        [_evidence(1, "US imposes new sanctions on Iran oil exports",
                   "Treasury blacklists tankers.")],
        T0 + timedelta(minutes=10),
    )
    assert both.dimension("usd_irt").conflicting_event_ids
    assert both.dimension("usd_irt").confidence < one_sided.dimension("usd_irt").confidence
    assert set(both.supporting_event_ids) | set(both.conflicting_event_ids) == {1, 2}


def test_snapshot_event_rows_label_conflicting_members():
    snapshot = intelligence.build_snapshot(
        [
            _evidence(1, "US imposes new sanctions on Iran oil exports",
                      "Treasury blacklists tankers."),
            _evidence(2, "Washington lifted sanctions on Iran and released frozen funds",
                      minutes=5),
        ],
        T0 + timedelta(minutes=10),
    )
    rows = snapshot.snapshot_event_rows(3)
    assert {row["event_id"] for row in rows} == {1, 2}
    assert {row["role"] for row in rows} == {"supporting", "conflicting"}
    assert all(row["snapshot_id"] == 3 for row in rows)


# --- deltas -------------------------------------------------------------------


def test_no_baseline_emits_no_deltas():
    assert intelligence.compute_delta(None, _snapshot({"usd_irt": 0.5})) == []


def test_escalation_only_above_the_documented_threshold():
    before = _snapshot({"usd_irt": 0.10})
    just_under = _snapshot({"usd_irt": 0.10 + intelligence.ESCALATION_THRESHOLD - 0.01})
    just_over = _snapshot({"usd_irt": 0.10 + intelligence.ESCALATION_THRESHOLD + 0.01})

    assert [
        delta for delta in intelligence.compute_delta(before, just_under)
        if delta.kind == intelligence.DELTA_ESCALATION
    ] == []

    escalations = [
        delta for delta in intelligence.compute_delta(before, just_over)
        if delta.kind == intelligence.DELTA_ESCALATION
    ]
    assert len(escalations) == 1
    assert escalations[0].detail["dimension"] == "usd_irt"
    assert escalations[0].detail["threshold"] == intelligence.ESCALATION_THRESHOLD
    assert escalations[0].magnitude == pytest.approx(
        intelligence.ESCALATION_THRESHOLD + 0.01
    )


def test_deescalation_uses_the_same_bar_as_escalation():
    before = _snapshot({"usd_irt": 0.60})
    after = _snapshot({"usd_irt": 0.60 - intelligence.DEESCALATION_THRESHOLD - 0.01})
    kinds = {delta.kind for delta in intelligence.compute_delta(before, after)}
    assert intelligence.DELTA_DEESCALATION in kinds
    assert intelligence.DEESCALATION_THRESHOLD == intelligence.ESCALATION_THRESHOLD


def test_a_dimension_filling_in_from_null_is_not_an_escalation():
    before = _snapshot({"usd_irt": None})
    after = _snapshot({"usd_irt": 0.9})
    kinds = {delta.kind for delta in intelligence.compute_delta(before, after)}
    assert intelligence.DELTA_ESCALATION not in kinds


def test_new_events_are_reported_with_their_weight():
    before = _snapshot({}, event_ids=(1,))
    after = _snapshot({}, event_ids=(1, 2), weights={2: 0.4})
    new = [
        delta for delta in intelligence.compute_delta(before, after)
        if delta.kind == intelligence.DELTA_NEW_EVENT
    ]
    assert [delta.event_id for delta in new] == [2]
    assert new[0].magnitude == pytest.approx(0.4)


def test_category_intensity_needs_a_cluster_not_a_single_article():
    before = _snapshot({}, category_counts={"sanctions_escalation": 1})
    small = _snapshot({}, category_counts={"sanctions_escalation": 2})
    cluster = _snapshot(
        {},
        category_counts={
            "sanctions_escalation": 1 + intelligence.CATEGORY_INTENSITY_STEP
        },
    )
    assert not [
        delta for delta in intelligence.compute_delta(before, small)
        if delta.kind == intelligence.DELTA_CATEGORY_INTENSITY
    ]
    intensity = [
        delta for delta in intelligence.compute_delta(before, cluster)
        if delta.kind == intelligence.DELTA_CATEGORY_INTENSITY
    ]
    assert len(intensity) == 1
    assert intensity[0].detail["category"] == "sanctions_escalation"


def test_source_failure_and_recovery_need_a_before_and_an_after():
    healthy = _snapshot({}, source_health={"fed_press": True})
    broken = _snapshot({}, source_health={"fed_press": False})
    assert [delta.kind for delta in intelligence.compute_delta(healthy, broken)] == [
        intelligence.DELTA_SOURCE_FAILURE
    ]
    assert [delta.kind for delta in intelligence.compute_delta(broken, healthy)] == [
        intelligence.DELTA_SOURCE_RECOVERY
    ]
    # A source that was never observed before is a configuration change, not a
    # failure or a recovery.
    assert intelligence.compute_delta(_snapshot({}), broken) == []


def test_freshness_change_reports_two_missed_cycles_or_a_stale_flip():
    before = _snapshot({}, freshness_s=300.0)
    jitter = _snapshot({}, freshness_s=300.0 + intelligence.FRESHNESS_STEP_S - 60)
    lagging = _snapshot({}, freshness_s=300.0 + intelligence.FRESHNESS_STEP_S + 60)

    assert not [
        delta for delta in intelligence.compute_delta(before, jitter)
        if delta.kind == intelligence.DELTA_FRESHNESS_CHANGE
    ]
    changes = [
        delta for delta in intelligence.compute_delta(before, lagging)
        if delta.kind == intelligence.DELTA_FRESHNESS_CHANGE
    ]
    assert len(changes) == 1
    assert changes[0].detail["threshold_s"] == intelligence.FRESHNESS_STEP_S

    flipped = _snapshot({}, freshness_s=400.0, stale=True)
    assert [
        delta.kind for delta in intelligence.compute_delta(before, flipped)
    ] == [intelligence.DELTA_FRESHNESS_CHANGE]


def test_snapshots_from_different_calc_versions_are_refused():
    before = _snapshot({"usd_irt": 0.1})
    after = _snapshot({"usd_irt": 0.9})
    stale_version = intelligence.Snapshot(
        captured_at=before.captured_at,
        calc_version="intel-0.9.0",
        dimensions=before.dimensions,
        supporting_event_ids=(),
        conflicting_event_ids=(),
        source_reliability=None,
        data_freshness_s=None,
        stale=True,
        limitations="",
        inputs=before.inputs,
    )
    with pytest.raises(ValueError):
        intelligence.compute_delta(stale_version, after)


def test_delta_rows_carry_both_snapshot_ids():
    deltas = intelligence.compute_delta(
        _snapshot({}, event_ids=(1,)), _snapshot({}, event_ids=(1, 2))
    )
    rows = [delta.as_row(to_snapshot=9, from_snapshot=8) for delta in deltas]
    assert rows and all(row["to_snapshot"] == 9 and row["from_snapshot"] == 8 for row in rows)
    assert all(row["kind"] for row in rows)
