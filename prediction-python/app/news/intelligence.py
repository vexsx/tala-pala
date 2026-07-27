"""Intelligence snapshots: bounded pressure scores, and what changed since.

WHY a snapshot table instead of a live computation: a number shown to a user
has to be reproducible after the fact.  Every score below is written with the
event ids that produced it, the calculation version, how fresh the inputs were
and what the calculation could NOT see — so a snapshot can be re-derived and
argued with, and so a change between two snapshots has a cause more specific
than "the page reloaded".

WHY NULL and not 0: a dimension with too little evidence stores NULL, and the
UI must render that as unknown.  Zero means "the evidence points nowhere",
which is a finding; NULL means "there is no evidence", which is not.  Collapsing
the two is how a dashboard ends up showing a confident calm during an outage.

WHY every dimension points the same way: all scores are oriented so that
POSITIVE = upward pressure on the Tehran gold complex.  That is why the delta
engine below needs no per-dimension sign table, and why "escalation" can mean
one thing across a heterogeneous set of dimensions.

Nothing here is an input to a model.  These are aggregates of rule-derived
hypotheses (``app.news.classify``), and ``NEWS_ML_ENABLED`` gates the only path
that could change a forecast; the limitations text says so on every row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Sequence

from . import classify

# Bumped when a dimension, a weight or a threshold below changes.  Two
# snapshots may only be diffed when they share this version — see
# :func:`compute_delta`.
CALC_VERSION = "intel-1.0.0"

# The five channel dimensions are the classifier's channels, aggregated across
# events.  Their sign conventions are the classifier's (see that module).
DIMENSION_CHANNELS: tuple[str, ...] = classify.CHANNELS

# Family dimensions answer "how much pressure is coming from THIS kind of
# news", which the channel view cannot: a quiet net xau_usd can hide a large
# escalation offset by a large de-escalation.  Each names the categories it
# reads and the single channel it reads from them, so no family dimension is a
# blend of incomparable things.
FAMILY_DIMENSIONS: Mapping[str, tuple[frozenset[str], str]] = {
    # Conflict and haven demand reach every leg, so the composite is the honest
    # summary of them.
    "geopolitical_risk": (
        frozenset({"geopolitical_escalation", "geopolitical_deescalation",
                   "iran_political_risk", "safe_haven"}),
        classify.CHANNEL_COMBINED,
    ),
    # Sanctions and the diplomacy around them act on hard-currency supply
    # first; usd_irt is where that lands.
    "sanctions_pressure": (
        frozenset({"sanctions_escalation", "sanctions_relief",
                   "iran_negotiations"}),
        classify.CHANNEL_USD_IRT,
    ),
    # US macro reaches this market through the dollar gold price and almost
    # nowhere else, so reading any other channel here would be noise.
    "us_macro_pressure": (
        frozenset({"federal_reserve", "inflation", "us_labor", "us_yields",
                   "dollar_strength", "gold_market"}),
        classify.CHANNEL_XAU,
    ),
    "domestic_policy_pressure": (
        frozenset({"iran_fx_policy", "iran_monetary_policy",
                   "domestic_gold_regulation", "exchange_disruption"}),
        classify.CHANNEL_COMBINED,
    ),
}

DIMENSIONS: tuple[str, ...] = DIMENSION_CHANNELS + tuple(FAMILY_DIMENSIONS)

# --- weighting -------------------------------------------------------------
# A hypothesis's weight is confidence x recency x independence.  Each factor
# answers a different question: how much did the rule claim, is the claim still
# live, and did more than one newsroom say it.

# A single-source claim is halved, never zeroed: real news does break in one
# outlet first, and zeroing it would make the system blind to exactly the
# events that matter most early.
INDEPENDENCE_FLOOR = 0.5
# Corroboration saturates fast.  The third independent source adds much less
# than the second, and past three the marginal source is almost always another
# subscriber to the same wire.
FULL_INDEPENDENCE_AT = 3

# Below this much total weight a dimension is reported as NULL.  0.10 is about
# one stale, low-directness, single-source hypothesis — evidence too thin to
# put a number on, and precisely the case where a 0 would be read as calm.
MIN_EVIDENCE_WEIGHT = 0.10
# The total weight at which the count term stops adding confidence: roughly
# three fresh, direct, corroborated hypotheses.
FULL_CONFIDENCE_WEIGHT = 2.0
# The classifier caps a single rule at 0.85 because a keyword match verifies
# nothing.  Aggregation cannot manufacture certainty its inputs never had, so
# the ceiling here is lower still.
MAX_DIMENSION_CONFIDENCE = 0.75

# The shortest horizon any rule claims is intraday.  Once the newest input is
# older than that, every score describes a window that has already closed —
# which is a different failure from "the market is quiet" and must be labelled.
STALE_AFTER_S = 6 * 3600

# --- delta thresholds ------------------------------------------------------
# Each is justified against something measurable, so no threshold here is a
# taste-based red/yellow/green boundary.

# Recency decay alone moves a 0.5-scored dimension by ~0.015 per hour at the
# 24-hour half-life the medium-horizon rules use.  0.25 is more than an order
# of magnitude above that drift, and about 40% of the range a single rule can
# actually produce (|score| <= 0.65), so an escalation row means new or
# reversed evidence rather than the clock ticking.
ESCALATION_THRESHOLD = 0.25
# The SAME magnitude in the other direction, deliberately: a delta engine with
# a lower bar for alarm than for relief reports a world that is always getting
# worse.
DEESCALATION_THRESHOLD = ESCALATION_THRESHOLD
# One extra event in a category is ordinary feed traffic. Three distinct new
# events in one category between two snapshots is the smallest count that a
# quiet baseline does not produce by chance at this poll cadence.
CATEGORY_INTENSITY_STEP = 3
# Sources are polled on a 900-second courtesy cadence, so freshness normally
# sags by at most one cycle between snapshots.  Half an hour is two missed
# cycles: no longer explicable as polling jitter.
FRESHNESS_STEP_S = 1800

DELTA_NEW_EVENT = "new_event"
DELTA_ESCALATION = "escalation"
DELTA_DEESCALATION = "deescalation"
DELTA_CATEGORY_INTENSITY = "category_intensity"
DELTA_SOURCE_FAILURE = "source_failure"
DELTA_SOURCE_RECOVERY = "source_recovery"
DELTA_FRESHNESS_CHANGE = "freshness_change"

_HYPOTHESIS_DISCLAIMER = (
    "All scores are rule-derived hypotheses (classifier "
    f"{classify.CLASSIFIER_VERSION}), not measured effects; no event study "
    "supports them and no news feature reaches a model."
)


@dataclass(frozen=True)
class EventEvidence:
    """One consolidated event as the snapshot needs to see it."""

    event_id: int
    category: str
    available_at: datetime
    hypotheses: tuple[classify.Hypothesis, ...]
    independent_source_count: int = 1
    source_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class DimensionResult:
    """One pressure dimension: score, confidence, and who said so."""

    dimension: str
    score: Optional[float]          # NULL when the evidence is insufficient
    confidence: Optional[float]
    total_weight: float
    event_ids: tuple[int, ...]
    supporting_event_ids: tuple[int, ...]
    conflicting_event_ids: tuple[int, ...]


@dataclass(frozen=True)
class Snapshot:
    """A persisted row of ``intelligence_snapshots`` plus its member weights."""

    captured_at: datetime
    calc_version: str
    dimensions: tuple[DimensionResult, ...]
    supporting_event_ids: tuple[int, ...]
    conflicting_event_ids: tuple[int, ...]
    source_reliability: Optional[float]
    data_freshness_s: Optional[float]
    stale: bool
    limitations: str
    inputs: dict = field(default_factory=dict)
    event_weights: Mapping[int, float] = field(default_factory=dict)

    def dimension(self, name: str) -> Optional[DimensionResult]:
        for item in self.dimensions:
            if item.dimension == name:
                return item
        return None

    def score(self, name: str) -> Optional[float]:
        result = self.dimension(name)
        return None if result is None else result.score

    def as_row(self) -> dict:
        """Row for ``intelligence_snapshots``.

        ``scores`` and ``confidence`` keep NULL entries rather than dropping
        them: a dimension that exists but could not be measured is information,
        and a missing key would let a consumer silently default it to zero.
        """
        return {
            "captured_at": self.captured_at,
            "calc_version": self.calc_version,
            "scores": {item.dimension: item.score for item in self.dimensions},
            "confidence": {item.dimension: item.confidence for item in self.dimensions},
            "inputs": self.inputs,
            "supporting_event_ids": list(self.supporting_event_ids),
            "conflicting_event_ids": list(self.conflicting_event_ids),
            "source_reliability": self.source_reliability,
            "data_freshness_s": self.data_freshness_s,
            "stale": self.stale,
            "limitations": self.limitations,
        }

    def snapshot_event_rows(self, snapshot_id: int) -> list[dict]:
        """Rows for ``intelligence_snapshot_events``."""
        conflicting = set(self.conflicting_event_ids)
        return [
            {
                "snapshot_id": snapshot_id,
                "event_id": event_id,
                "role": "conflicting" if event_id in conflicting else "supporting",
                "weight": round(weight, 4),
            }
            for event_id, weight in sorted(self.event_weights.items())
        ]


@dataclass(frozen=True)
class Delta:
    """One reportable change between two snapshots."""

    kind: str
    detail: dict
    magnitude: Optional[float] = None
    event_id: Optional[int] = None

    def as_row(self, to_snapshot: int, from_snapshot: Optional[int] = None) -> dict:
        return {
            "from_snapshot": from_snapshot,
            "to_snapshot": to_snapshot,
            "kind": self.kind,
            "detail": self.detail,
            "magnitude": self.magnitude,
            "event_id": self.event_id,
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _recency(age_seconds: float, decay_hours: Optional[float]) -> float:
    """Half-life decay on the rule's own stated decay horizon.

    A rule that claims an intraday effect stops counting within the day; one
    that claims a fortnight keeps counting for a fortnight.  Using the rule's
    own horizon rather than a global constant is what keeps a week-old sanctions
    headline relevant while a week-old payrolls print is not.
    """
    if decay_hours is None or decay_hours <= 0:
        return 1.0
    if age_seconds <= 0:
        return 1.0
    return 0.5 ** ((age_seconds / 3600.0) / decay_hours)


def _independence(independent_source_count: int) -> float:
    corroboration = max(1, int(independent_source_count))
    if corroboration >= FULL_INDEPENDENCE_AT:
        return 1.0
    span = 1.0 - INDEPENDENCE_FLOOR
    return INDEPENDENCE_FLOOR + span * (corroboration - 1) / (FULL_INDEPENDENCE_AT - 1)


def _contributions(
    events: Sequence[EventEvidence], dimension: str, now: datetime
) -> list[tuple[EventEvidence, float, float]]:
    """``(event, score, weight)`` for every event that speaks to ``dimension``."""
    if dimension in FAMILY_DIMENSIONS:
        categories, channel = FAMILY_DIMENSIONS[dimension]
    else:
        categories, channel = None, dimension
    rows: list[tuple[EventEvidence, float, float]] = []
    for event in events:
        if categories is not None and event.category not in categories:
            continue
        hypothesis = next(
            (item for item in event.hypotheses if item.channel == channel), None
        )
        if hypothesis is None:
            continue
        age = (_utc(now) - _utc(event.available_at)).total_seconds()
        weight = (
            hypothesis.confidence
            * _recency(age, hypothesis.decay_hours)
            * _independence(event.independent_source_count)
        )
        if weight <= 0:
            continue
        rows.append((event, _clamp(hypothesis.score), weight))
    return rows


def _score_dimension(
    dimension: str, contributions: Sequence[tuple[EventEvidence, float, float]]
) -> DimensionResult:
    total_weight = sum(weight for _event, _score, weight in contributions)
    event_ids = tuple(event.event_id for event, _score, _weight in contributions)
    if not contributions or total_weight < MIN_EVIDENCE_WEIGHT:
        return DimensionResult(
            dimension=dimension,
            score=None,
            confidence=None,
            total_weight=round(total_weight, 4),
            event_ids=event_ids,
            supporting_event_ids=(),
            conflicting_event_ids=(),
        )
    weighted = sum(score * weight for _event, score, weight in contributions)
    score = _clamp(weighted / total_weight)

    # Agreement is how much the contributions point the same way: 1.0 when they
    # all share a sign, near 0 when they cancel.  A cancelled-out dimension is
    # NOT a confident zero — it is a contested one, and the confidence has to
    # say that.
    magnitude = sum(abs(score) * weight for _event, score, weight in contributions)
    agreement = 1.0 if magnitude == 0 else abs(weighted) / magnitude
    confidence = min(
        MAX_DIMENSION_CONFIDENCE,
        agreement * min(1.0, total_weight / FULL_CONFIDENCE_WEIGHT),
    )
    supporting = tuple(
        event.event_id for event, item_score, _weight in contributions
        if item_score * score > 0 or (item_score == 0 and score == 0)
    )
    conflicting = tuple(
        event.event_id for event, item_score, _weight in contributions
        if item_score * score < 0
    )
    return DimensionResult(
        dimension=dimension,
        score=round(score, 4),
        confidence=round(confidence, 4),
        total_weight=round(total_weight, 4),
        event_ids=event_ids,
        supporting_event_ids=supporting,
        conflicting_event_ids=conflicting,
    )


def _limitations(
    events: Sequence[EventEvidence],
    dimensions: Sequence[DimensionResult],
    freshness_s: Optional[float],
    stale: bool,
    source_reliability: Optional[float],
) -> str:
    notes = [_HYPOTHESIS_DISCLAIMER]
    if not events:
        notes.append("No events in the window: every dimension is unknown, not neutral.")
    unmeasured = [item.dimension for item in dimensions if item.score is None]
    if unmeasured:
        notes.append(
            "Insufficient evidence (reported NULL, not 0): " + ", ".join(unmeasured) + "."
        )
    single_source = [
        str(event.event_id) for event in events if event.independent_source_count <= 1
    ]
    if single_source:
        notes.append(
            f"{len(single_source)} of {len(events)} events rest on a single "
            "independent source and are weighted down accordingly."
        )
    if stale:
        age = "unknown" if freshness_s is None else f"{freshness_s / 3600.0:.1f}h"
        notes.append(
            f"Newest input is {age} old, past the {STALE_AFTER_S / 3600.0:.0f}h "
            "staleness bound: these scores describe a window that has closed."
        )
    if source_reliability is None:
        notes.append(
            "No source-reliability input was supplied, so reliability is NULL "
            "rather than assumed."
        )
    return " ".join(notes)


def build_snapshot(
    events: Iterable[EventEvidence],
    now: datetime,
    source_reliability: Optional[Mapping[str, float]] = None,
    source_health: Optional[Mapping[str, bool]] = None,
) -> Snapshot:
    """Aggregate classified events into one bounded, auditable snapshot.

    ``source_reliability`` maps a source code to a score in [0, 1] (health
    snapshots are the natural producer).  When it is absent — or none of the
    contributing sources appear in it — the snapshot stores NULL rather than a
    made-up default, because a fabricated reliability would silently reweight
    everything downstream.
    """
    event_list = list(events)
    captured_at = _utc(now)

    results: list[DimensionResult] = []
    contributions_by_dimension: dict[str, list[tuple[EventEvidence, float, float]]] = {}
    for dimension in DIMENSIONS:
        contributions = _contributions(event_list, dimension, captured_at)
        contributions_by_dimension[dimension] = contributions
        results.append(_score_dimension(dimension, contributions))

    # An event's weight in the snapshot is the largest weight it carried in any
    # dimension: that is the strongest claim it made anywhere.
    event_weights: dict[int, float] = {}
    for contributions in contributions_by_dimension.values():
        for event, _score, weight in contributions:
            event_weights[event.event_id] = max(
                event_weights.get(event.event_id, 0.0), weight
            )

    conflicting_ids = sorted(
        {
            event_id
            for item in results
            for event_id in item.conflicting_event_ids
        }
    )
    supporting_ids = sorted(
        {
            event_id
            for item in results
            for event_id in item.supporting_event_ids
        }
        - set(conflicting_ids)
    )

    freshness_s: Optional[float] = None
    if event_list:
        newest = max(_utc(event.available_at) for event in event_list)
        freshness_s = max(0.0, (captured_at - newest).total_seconds())
    # No events at all is the stalest state there is; reporting it as fresh
    # would make an outage look like calm.
    stale = freshness_s is None or freshness_s > STALE_AFTER_S

    reliability: Optional[float] = None
    if source_reliability:
        scored = [
            (source_reliability[code], event_weights.get(event.event_id, 0.0))
            for event in event_list
            for code in event.source_codes
            if code in source_reliability
        ]
        weight_sum = sum(weight for _value, weight in scored)
        if scored and weight_sum > 0:
            reliability = round(
                sum(value * weight for value, weight in scored) / weight_sum, 4
            )
        elif scored:
            reliability = round(sum(value for value, _weight in scored) / len(scored), 4)

    category_counts: dict[str, int] = {}
    for event in event_list:
        category_counts[event.category] = category_counts.get(event.category, 0) + 1

    inputs = {
        "event_ids": sorted(event.event_id for event in event_list),
        "event_count": len(event_list),
        "category_counts": category_counts,
        "source_codes": sorted({code for event in event_list for code in event.source_codes}),
        "source_health": dict(source_health) if source_health else {},
        "classifier_version": classify.CLASSIFIER_VERSION,
        "dimensions": {
            item.dimension: {
                "n_events": len(item.event_ids),
                "total_weight": item.total_weight,
                "event_ids": list(item.event_ids),
                "supporting_event_ids": list(item.supporting_event_ids),
                "conflicting_event_ids": list(item.conflicting_event_ids),
            }
            for item in results
        },
    }

    return Snapshot(
        captured_at=captured_at,
        calc_version=CALC_VERSION,
        dimensions=tuple(results),
        supporting_event_ids=tuple(supporting_ids),
        conflicting_event_ids=tuple(conflicting_ids),
        source_reliability=reliability,
        data_freshness_s=None if freshness_s is None else round(freshness_s, 3),
        stale=stale,
        limitations=_limitations(event_list, results, freshness_s, stale, reliability),
        inputs=inputs,
        event_weights=event_weights,
    )


def compute_delta(prev: Optional[Snapshot], cur: Snapshot) -> list[Delta]:
    """What changed between two snapshots, above the documented thresholds.

    Returns an empty list when there is no baseline: a first snapshot differs
    from nothing, and emitting a "new event" for every item on a cold start
    would make a restart look like a crisis.  Snapshots computed by different
    calculation versions are not comparable and are refused for the same
    reason — the difference would measure the code change, not the world.
    """
    if prev is None:
        return []
    if prev.calc_version != cur.calc_version:
        raise ValueError(
            "intelligence: refusing to diff snapshots across calc versions "
            f"({prev.calc_version} -> {cur.calc_version})"
        )

    deltas: list[Delta] = []

    previous_ids = set(prev.inputs.get("event_ids", []))
    for event_id in sorted(set(cur.inputs.get("event_ids", [])) - previous_ids):
        deltas.append(
            Delta(
                kind=DELTA_NEW_EVENT,
                detail={"event_id": event_id},
                magnitude=round(cur.event_weights.get(event_id, 0.0), 4),
                event_id=event_id,
            )
        )

    for dimension in DIMENSIONS:
        before, after = prev.score(dimension), cur.score(dimension)
        if before is None or after is None:
            # A dimension that was unknown and is now scored (or vice versa) has
            # no comparable baseline; that arrival is already reported as
            # new_event evidence, and calling it an escalation would turn every
            # cold dimension into an alarm the first time it fills in.
            continue
        change = after - before
        if change >= ESCALATION_THRESHOLD:
            kind = DELTA_ESCALATION
        elif -change >= DEESCALATION_THRESHOLD:
            kind = DELTA_DEESCALATION
        else:
            continue
        deltas.append(
            Delta(
                kind=kind,
                detail={
                    "dimension": dimension,
                    "from": before,
                    "to": after,
                    "threshold": ESCALATION_THRESHOLD,
                },
                magnitude=round(change, 4),
            )
        )

    previous_counts = prev.inputs.get("category_counts", {})
    for category, count in sorted(cur.inputs.get("category_counts", {}).items()):
        rise = count - previous_counts.get(category, 0)
        if rise >= CATEGORY_INTENSITY_STEP:
            deltas.append(
                Delta(
                    kind=DELTA_CATEGORY_INTENSITY,
                    detail={
                        "category": category,
                        "from": previous_counts.get(category, 0),
                        "to": count,
                        "threshold": CATEGORY_INTENSITY_STEP,
                    },
                    magnitude=float(rise),
                )
            )

    previous_health = prev.inputs.get("source_health", {})
    current_health = cur.inputs.get("source_health", {})
    for source_code in sorted(set(previous_health) | set(current_health)):
        was = previous_health.get(source_code)
        now_healthy = current_health.get(source_code)
        if was is None or now_healthy is None:
            # A source that appeared or disappeared from the health input has
            # not been observed failing or recovering; claiming either would be
            # inventing an event out of a configuration change.
            continue
        if was and not now_healthy:
            deltas.append(
                Delta(kind=DELTA_SOURCE_FAILURE, detail={"source_code": source_code})
            )
        elif now_healthy and not was:
            deltas.append(
                Delta(kind=DELTA_SOURCE_RECOVERY, detail={"source_code": source_code})
            )

    before_fresh, after_fresh = prev.data_freshness_s, cur.data_freshness_s
    if before_fresh is not None and after_fresh is not None:
        change = after_fresh - before_fresh
        if abs(change) >= FRESHNESS_STEP_S or prev.stale != cur.stale:
            deltas.append(
                Delta(
                    kind=DELTA_FRESHNESS_CHANGE,
                    detail={
                        "from_s": before_fresh,
                        "to_s": after_fresh,
                        "was_stale": prev.stale,
                        "is_stale": cur.stale,
                        "threshold_s": FRESHNESS_STEP_S,
                    },
                    magnitude=round(change, 3),
                )
            )
    elif prev.stale != cur.stale:
        deltas.append(
            Delta(
                kind=DELTA_FRESHNESS_CHANGE,
                detail={
                    "from_s": before_fresh,
                    "to_s": after_fresh,
                    "was_stale": prev.stale,
                    "is_stale": cur.stale,
                    "threshold_s": FRESHNESS_STEP_S,
                },
            )
        )
    return deltas


def _table(name: str):
    """Resolve a mirrored table by name from the shared metadata.

    Looked up rather than imported so this module does not have to know which
    module declares the 0017 mirrors, and so a missing mirror fails with a
    sentence instead of an ImportError.
    """
    from ..db import metadata

    table = metadata.tables.get(name)
    if table is None:
        raise RuntimeError(
            f"intelligence: table {name!r} is not mirrored on app.db.metadata"
        )
    return table


def persist_snapshot(conn, snapshot: Snapshot) -> int:
    """Insert a snapshot and its event rows; returns the snapshot id."""
    snapshots = _table("intelligence_snapshots")
    result = conn.execute(snapshots.insert().values(**snapshot.as_row()))
    snapshot_id = int(result.inserted_primary_key[0])
    rows = snapshot.snapshot_event_rows(snapshot_id)
    if rows:
        conn.execute(_table("intelligence_snapshot_events").insert(), rows)
    return snapshot_id


def persist_deltas(
    conn,
    deltas: Sequence[Delta],
    to_snapshot: int,
    from_snapshot: Optional[int] = None,
) -> int:
    """Insert delta rows for a snapshot pair; returns the number written."""
    if not deltas:
        return 0
    rows = [delta.as_row(to_snapshot, from_snapshot) for delta in deltas]
    conn.execute(_table("intelligence_deltas").insert(), rows)
    return len(rows)
