"""News source registry: which feeds may be fetched, and whether to fetch now.

Two tables answer two different questions and are deliberately not merged:

* ``news_sources`` (migration 0016) is the *operational* row — URL, cadence,
  and the health counters that a failed poll updates;
* ``news_source_policies`` (migration 0017) is the *policy* row — the approval
  decision, who made it, when, and what it permits (backfill, full-body
  storage, attribution).  It has its own history so an approval is auditable
  and cannot be quietly flipped by an ingestion bug touching the same row.

Only the policy row grants permission.  :func:`approved_sources` is the only
function ingestion code should use to decide what to poll, and it fails closed:
if the policy table is missing or a source has no policy row, that source is
simply not returned — an unreviewed feed is never fetched by default.

The circuit breaker is derived from ``consecutive_failures``, not from an
in-memory counter, for the reason the TSE-funds quota guard already taught this
codebase: a process restart must not reset a source's failure history and
resume hammering a feed that is down or rate-limiting us.

Nothing here reads or writes model input.  A source row's health has no path
into a forecast; ``NEWS_ML_ENABLED`` gates that separately and stays false.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.engine import Engine

from ..db import ensure_utc, utcnow
from .safefetch import MAX_ERROR_DETAIL, redact

log = logging.getLogger(__name__)

# Outcomes accepted by news_collection_attempts.outcome (migration 0017).
ATTEMPT_OUTCOMES = frozenset({"ok", "error", "throttled", "skipped", "empty"})
SUCCESS_OUTCOMES = frozenset({"ok", "empty"})  # 'empty' means the fetch worked

APPROVED = "approved"

# Failures before the breaker opens.  Three is the threshold used for price
# providers; news is less urgent and feeds flap more, so five avoids opening on
# a transient DNS blip while still stopping a genuinely dead feed quickly.
CIRCUIT_TRIP_FAILURES = 5
CIRCUIT_BASE_COOLDOWN_S = 300
CIRCUIT_MAX_COOLDOWN_S = 6 * 3600

DEFAULT_CACHE_TTL_S = 300.0

_SOURCE_COLUMNS = """
    s.code, s.name, s.feed_url, s.homepage_url, s.kind, s.jurisdiction,
    s.language, s.enabled, s.policy_status, s.quota_per_day,
    s.min_interval_seconds, s.last_polled_at, s.last_success_at,
    s.last_error_at, s.last_error, s.consecutive_failures,
    p.access_method, p.auth_type, p.approval_state, p.user_agent_policy,
    p.backfill_allowed, p.store_full_body, p.attribution_required,
    p.rate_limit_per_day, p.min_interval_seconds AS policy_min_interval,
    p.policy_note, p.reviewed_at, p.reviewed_by
"""

# INNER JOIN, not LEFT: a source without a policy row is unreviewed, and
# unreviewed means not fetched.
_SELECT_SOURCES = text(
    f"SELECT {_SOURCE_COLUMNS} "
    "FROM news_sources s "
    "JOIN news_source_policies p ON p.source_code = s.code "
    "ORDER BY s.code"
)

_SELECT_APPROVED = text(
    f"SELECT {_SOURCE_COLUMNS} "
    "FROM news_sources s "
    "JOIN news_source_policies p ON p.source_code = s.code "
    "WHERE p.approval_state = :approved AND s.enabled = :enabled "
    "ORDER BY s.code"
)

_INSERT_ATTEMPT = text(
    "INSERT INTO news_collection_attempts "
    "(source_code, started_at, finished_at, outcome, http_status, "
    " bytes_received, items_seen, items_new, items_updated, items_duplicate, "
    " error_class, error_detail, query_id, parser_version) "
    "VALUES (:source_code, :started_at, :finished_at, :outcome, :http_status, "
    " :bytes_received, :items_seen, :items_new, :items_updated, "
    " :items_duplicate, :error_class, :error_detail, :query_id, :parser_version)"
)

_MARK_SUCCESS = text(
    "UPDATE news_sources SET last_polled_at = :now, last_success_at = :now, "
    "consecutive_failures = 0, last_error = NULL, updated_at = :now "
    "WHERE code = :code"
)

_MARK_FAILURE = text(
    "UPDATE news_sources SET last_polled_at = :now, last_error_at = :now, "
    "last_error = :error, consecutive_failures = consecutive_failures + 1, "
    "updated_at = :now WHERE code = :code"
)

# A throttled/skipped attempt still stamps the poll marker: a source we chose
# not to fetch must not be reconsidered on every tick.
_MARK_POLLED = text(
    "UPDATE news_sources SET last_polled_at = :now, updated_at = :now "
    "WHERE code = :code"
)


def _host(url: str) -> str:
    parts = urlsplit((url or "").strip())
    return (parts.hostname or "").lower()


@dataclass(frozen=True)
class SourcePolicy:
    """The approval decision for one source (``news_source_policies`` row)."""

    access_method: str
    auth_type: str
    approval_state: str
    user_agent_policy: str
    backfill_allowed: bool
    store_full_body: bool
    attribution_required: bool
    rate_limit_per_day: Optional[int]
    min_interval_seconds: int
    policy_note: str
    reviewed_at: Optional[datetime]
    reviewed_by: str

    @property
    def is_approved(self) -> bool:
        return self.approval_state == APPROVED


@dataclass(frozen=True)
class NewsSource:
    """An operational source row joined to its policy row."""

    code: str
    name: str
    feed_url: str
    homepage_url: str
    kind: str
    jurisdiction: str
    language: str
    enabled: bool
    policy_status: str          # 0016's field; advisory, superseded by policy
    quota_per_day: Optional[int]
    min_interval_seconds: int
    last_polled_at: Optional[datetime]
    last_success_at: Optional[datetime]
    last_error_at: Optional[datetime]
    last_error: Optional[str]
    consecutive_failures: int
    policy: SourcePolicy

    @property
    def is_fetchable(self) -> bool:
        return self.enabled and self.policy.is_approved

    @property
    def effective_min_interval_seconds(self) -> int:
        """The more courteous of the operational and policy cadences."""
        return max(self.min_interval_seconds, self.policy.min_interval_seconds)

    @property
    def allowed_hosts(self) -> frozenset[str]:
        """Host allowlist for :func:`app.news.safefetch.fetch`.

        Only hosts that appear in the reviewed row itself: the feed host and
        the homepage host (a feed commonly redirects between the two).  A
        redirect anywhere else is refused, which is what keeps a URL taken from
        a database row from reaching an arbitrary destination.
        """
        return frozenset(h for h in (_host(self.feed_url), _host(self.homepage_url)) if h)


def _row_to_source(row) -> NewsSource:
    data = dict(row._mapping)
    policy = SourcePolicy(
        access_method=data["access_method"],
        auth_type=data["auth_type"],
        approval_state=data["approval_state"],
        user_agent_policy=data["user_agent_policy"],
        backfill_allowed=bool(data["backfill_allowed"]),
        store_full_body=bool(data["store_full_body"]),
        attribution_required=bool(data["attribution_required"]),
        rate_limit_per_day=data["rate_limit_per_day"],
        min_interval_seconds=int(data["policy_min_interval"] or 0),
        policy_note=data["policy_note"] or "",
        reviewed_at=ensure_utc(data["reviewed_at"]),
        reviewed_by=data["reviewed_by"] or "",
    )
    return NewsSource(
        code=data["code"],
        name=data["name"],
        feed_url=data["feed_url"] or "",
        homepage_url=data["homepage_url"] or "",
        kind=data["kind"],
        jurisdiction=data["jurisdiction"],
        language=data["language"],
        enabled=bool(data["enabled"]),
        policy_status=data["policy_status"],
        quota_per_day=data["quota_per_day"],
        min_interval_seconds=int(data["min_interval_seconds"] or 0),
        last_polled_at=ensure_utc(data["last_polled_at"]),
        last_success_at=ensure_utc(data["last_success_at"]),
        last_error_at=ensure_utc(data["last_error_at"]),
        last_error=data["last_error"],
        consecutive_failures=int(data["consecutive_failures"] or 0),
        policy=policy,
    )


def load_sources(engine: Engine) -> list[NewsSource]:
    """Every source that has a policy row, approved or not (for health/UI)."""
    with engine.connect() as conn:
        return [_row_to_source(row) for row in conn.execute(_SELECT_SOURCES)]


def approved_sources(engine: Engine) -> list[NewsSource]:
    """Sources that policy approves AND that are operationally enabled.

    The only permission gate in the news subsystem.  Both conditions are
    required: approval says we *may* fetch, ``enabled`` says we *want* to.
    """
    with engine.connect() as conn:
        rows = conn.execute(_SELECT_APPROVED, {"approved": APPROVED, "enabled": True})
        return [_row_to_source(row) for row in rows]


# --- circuit breaker ---------------------------------------------------------


@dataclass(frozen=True)
class CircuitState:
    """Whether a source may be polled right now, and why not when it may not."""

    open: bool
    reason: str
    retry_at: Optional[datetime] = None

    @property
    def closed(self) -> bool:
        return not self.open


def cooldown_seconds(consecutive_failures: int) -> float:
    """Backoff after the breaker trips: doubles per extra failure, capped.

    Capped because an uncapped exponential turns a week-long outage into a
    source that never retries again without a manual poke.
    """
    over = max(0, consecutive_failures - CIRCUIT_TRIP_FAILURES)
    return float(min(CIRCUIT_BASE_COOLDOWN_S * (2**over), CIRCUIT_MAX_COOLDOWN_S))


def circuit_state(source: NewsSource, now: Optional[datetime] = None) -> CircuitState:
    """Breaker state derived from the persisted failure counter."""
    now = now or utcnow()
    if not source.policy.is_approved:
        return CircuitState(True, f"policy approval_state={source.policy.approval_state}")
    if not source.enabled:
        return CircuitState(True, "source disabled")
    if source.consecutive_failures < CIRCUIT_TRIP_FAILURES:
        return CircuitState(False, "")
    cooldown = cooldown_seconds(source.consecutive_failures)
    last_error = source.last_error_at or source.last_polled_at
    if last_error is None:
        # Counter without a timestamp: treat as open until a poll updates one,
        # rather than inventing a retry time we cannot justify.
        return CircuitState(True, f"{source.consecutive_failures} consecutive failures")
    retry_at = last_error + timedelta(seconds=cooldown)
    if now < retry_at:
        return CircuitState(
            True,
            f"{source.consecutive_failures} consecutive failures; "
            f"cooling down for {cooldown:.0f}s",
            retry_at,
        )
    return CircuitState(False, "", retry_at)


def should_attempt(
    source: NewsSource, now: Optional[datetime] = None
) -> tuple[bool, str]:
    """(may poll now, reason when not) — breaker first, then courtesy cadence."""
    now = now or utcnow()
    state = circuit_state(source, now)
    if state.open:
        return False, state.reason
    interval = source.effective_min_interval_seconds
    if source.last_polled_at is not None and interval > 0:
        due_at = source.last_polled_at + timedelta(seconds=interval)
        if now < due_at:
            age = int((now - source.last_polled_at).total_seconds())
            return False, f"polled {age}s ago; min interval {interval}s"
    return True, ""


# --- attempt recording -------------------------------------------------------


def record_attempt(
    engine: Engine,
    source_code: str,
    *,
    outcome: str,
    started_at: datetime,
    finished_at: Optional[datetime] = None,
    http_status: Optional[int] = None,
    bytes_received: Optional[int] = None,
    items_seen: int = 0,
    items_new: int = 0,
    items_updated: int = 0,
    items_duplicate: int = 0,
    error_class: Optional[str] = None,
    error_detail: Optional[str] = None,
    query_id: Optional[int] = None,
    parser_version: str = "",
) -> None:
    """Append one row to ``news_collection_attempts`` and update source health.

    Every attempt is recorded, success or failure: that is what makes a quota
    claim checkable and the circuit breaker honest — a breaker driven by a
    counter that only some code paths increment is worse than none.

    ``error_detail`` is redacted (a feed URL may carry an API key) and truncated
    before it is stored.  Both timestamps are the caller's own clock and are
    never used as a publication time.
    """
    if outcome not in ATTEMPT_OUTCOMES:
        raise ValueError(f"unknown attempt outcome {outcome!r}")
    now = utcnow()
    detail = redact(error_detail)[:MAX_ERROR_DETAIL] if error_detail else None

    with engine.begin() as conn:
        conn.execute(
            _INSERT_ATTEMPT,
            {
                "source_code": source_code,
                "started_at": started_at,
                "finished_at": finished_at or now,
                "outcome": outcome,
                "http_status": http_status,
                "bytes_received": bytes_received,
                "items_seen": items_seen,
                "items_new": items_new,
                "items_updated": items_updated,
                "items_duplicate": items_duplicate,
                "error_class": error_class,
                "error_detail": detail,
                "query_id": query_id,
                "parser_version": parser_version,
            },
        )
        if outcome in SUCCESS_OUTCOMES:
            conn.execute(_MARK_SUCCESS, {"now": now, "code": source_code})
        elif outcome == "error":
            conn.execute(
                _MARK_FAILURE,
                {"now": now, "code": source_code, "error": (detail or error_class or "")[:2000]},
            )
        else:
            # throttled / skipped: not a failure, so the breaker counter is
            # left alone; only the poll marker moves.
            conn.execute(_MARK_POLLED, {"now": now, "code": source_code})


# --- cached view -------------------------------------------------------------


class SourceRegistry:
    """Process-local cache of the source rows with a TTL refresh.

    The ingestion loop asks "what should I poll" often and the rows change
    rarely, but the cache is deliberately short-lived and always re-read on
    :meth:`refresh`, so disabling a source in the database takes effect within
    one TTL without a restart.
    """

    def __init__(self, engine: Engine, ttl_seconds: float = DEFAULT_CACHE_TTL_S) -> None:
        self._engine = engine
        self._ttl = ttl_seconds
        self._sources: list[NewsSource] = []
        self._loaded_at: Optional[float] = None

    def refresh(self) -> list[NewsSource]:
        self._sources = load_sources(self._engine)
        self._loaded_at = time.monotonic()
        return self._sources

    def sources(self, *, force: bool = False) -> list[NewsSource]:
        if (
            force
            or self._loaded_at is None
            or time.monotonic() - self._loaded_at > self._ttl
        ):
            return self.refresh()
        return self._sources

    def get(self, code: str, *, force: bool = False) -> Optional[NewsSource]:
        for source in self.sources(force=force):
            if source.code == code:
                return source
        return None

    def approved(self, *, force: bool = False) -> list[NewsSource]:
        return [s for s in self.sources(force=force) if s.is_fetchable]

    def due(
        self, now: Optional[datetime] = None, *, force: bool = False
    ) -> list[NewsSource]:
        """Approved sources whose breaker is closed and whose cadence elapsed."""
        now = now or datetime.now(timezone.utc)
        due: list[NewsSource] = []
        for source in self.approved(force=force):
            ok, reason = should_attempt(source, now)
            if ok:
                due.append(source)
            else:
                log.debug("news source %s not due: %s", source.code, reason)
        return due
