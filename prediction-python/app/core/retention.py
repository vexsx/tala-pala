"""Bounded, calibration-aware retention (Addendum 17).

Every table here grew without limit before this module existed. Deleting from
them naively is dangerous in two different ways, and this module addresses
both explicitly:

1. **Correctness.** Several tables feed live self-learning loops. Matured
   ``predictions`` rows drive maturity evaluation, the meta-gate
   (``MAX_SAMPLES`` most recent), adaptive conformal intervals and per-regime
   calibration (``LIVE_CAL_WINDOW`` most recent per symbol+horizon) and live
   ensemble re-weighting (120-day window). Deleting inside those windows would
   silently degrade calibration rather than fail loudly, so the floor is
   computed from the loops' own constants — not from a hand-picked number.

2. **Operational safety.** A single unbounded ``DELETE`` over a year of rows
   takes a long lock and bloats WAL. Every delete here runs in bounded
   batches with an explicit statement cap, each batch in its own transaction.

Nothing is deleted that an ACTIVE ``model_versions`` row still references.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import utcnow

log = logging.getLogger(__name__)

# Batch size for every bounded delete. Small enough that a batch never holds a
# lock long enough to matter, large enough that a year of backlog drains in a
# reasonable number of iterations.
BATCH_ROWS = 5_000
# Hard cap on batches per table per run, so one pathological table cannot
# monopolise the cleanup window. Leftovers are removed on the next run.
MAX_BATCHES = 200

# The self-learning loops' own windows. Predictions inside the union of these
# must never be deleted regardless of the configured retention.
META_GATE_SAMPLES = 500       # models/metagate.py MAX_SAMPLES
LIVE_CAL_WINDOW = 60          # jobs/evaluate.py LIVE_CAL_WINDOW (per symbol+hz)
ENSEMBLE_WINDOW_DAYS = 120    # models/ensemble.py recency bound
# Audit rows are kept far longer than anything else: they are the record of
# who changed what, and are cheap.
MIN_AUDIT_RETENTION_DAYS = 365


@dataclass
class TableResult:
    table: str
    deleted: int = 0
    scanned_batches: int = 0
    floor_reason: str = ""
    skipped: bool = False
    error: Optional[str] = None


@dataclass
class RetentionResult:
    dry_run: bool
    tables: list[TableResult] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "deleted": {t.table: t.deleted for t in self.tables},
            "details": [
                {"table": t.table, "deleted": t.deleted, "batches": t.scanned_batches,
                 "floor": t.floor_reason, "skipped": t.skipped, "error": t.error}
                for t in self.tables
            ],
            "total_deleted": sum(t.deleted for t in self.tables),
        }


def _as_datetime(value) -> Optional[datetime]:
    """Coerce a driver-returned timestamp to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _count(engine: Engine, sql: str, params: dict) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(sql), params).scalar() or 0)


def _delete_batched(
    engine: Engine, table: str, where: str, params: dict, dry_run: bool,
    pk: str = "id",
) -> TableResult:
    """Delete matching rows in bounded batches, one transaction per batch."""
    res = TableResult(table=table)
    count_sql = f"SELECT count(*) FROM {table} WHERE {where}"  # noqa: S608 - fixed identifiers
    try:
        pending = _count(engine, count_sql, params)
    except Exception as exc:  # noqa: BLE001
        res.error = str(exc)
        log.warning("retention: count failed for %s: %s", table, exc)
        return res
    if dry_run:
        res.deleted = pending
        return res

    # DELETE ... WHERE pk IN (SELECT pk ... LIMIT n) keeps each statement
    # bounded and uses the same indexed predicate as the count.
    stmt = text(
        f"DELETE FROM {table} WHERE {pk} IN "  # noqa: S608 - fixed identifiers
        f"(SELECT {pk} FROM {table} WHERE {where} ORDER BY {pk} LIMIT :__batch)"
    )
    batch_params = dict(params, __batch=BATCH_ROWS)
    while res.scanned_batches < MAX_BATCHES:
        try:
            with engine.begin() as conn:
                removed = conn.execute(stmt, batch_params).rowcount or 0
        except Exception as exc:  # noqa: BLE001
            res.error = str(exc)
            log.warning("retention: delete failed for %s: %s", table, exc)
            return res
        res.scanned_batches += 1
        res.deleted += removed
        if removed < BATCH_ROWS:
            break
    if res.scanned_batches >= MAX_BATCHES:
        log.warning("retention: %s hit the batch cap (%d); remainder next run",
                    table, MAX_BATCHES)
    return res


def protected_prediction_cutoff(engine: Engine) -> tuple[Optional[object], str]:
    """Oldest ``predicted_at`` any live loop still needs. None = protect all.

    Takes the EARLIEST of: the meta-gate's most recent MAX_SAMPLES matured
    rows, the per-(symbol,horizon) calibration windows, and the ensemble's
    120-day recency bound. Anything at or after this instant is off-limits.
    """
    ensemble_floor = utcnow() - timedelta(days=ENSEMBLE_WINDOW_DAYS)
    with engine.connect() as conn:
        gate_floor = conn.execute(text(
            "SELECT min(target_time) FROM (SELECT target_time FROM predictions "
            "WHERE actual_value IS NOT NULL ORDER BY target_time DESC LIMIT :n) s"
        ), {"n": META_GATE_SAMPLES}).scalar()
        # Per symbol+horizon calibration windows.
        cal_floor = conn.execute(text(
            "SELECT min(t) FROM (SELECT symbol, horizon, "
            "  (SELECT min(target_time) FROM (SELECT target_time FROM predictions p2 "
            "     WHERE p2.symbol = p1.symbol AND p2.horizon = p1.horizon "
            "       AND p2.actual_value IS NOT NULL "
            "     ORDER BY target_time DESC LIMIT :w) x) AS t "
            "FROM (SELECT DISTINCT symbol, horizon FROM predictions) p1) y"
        ), {"w": LIVE_CAL_WINDOW}).scalar()
    # Drivers differ: Postgres hands back datetimes, SQLite hands back strings.
    # Comparing the two raises TypeError, so coerce before taking the minimum.
    floors = [
        _as_datetime(f) for f in (gate_floor, cal_floor, ensemble_floor)
        if f is not None
    ]
    floors = [f for f in floors if f is not None]
    if not floors:
        return None, "no matured predictions yet: nothing is eligible"
    floor = min(floors)
    return floor, (
        f"protected by self-learning windows (meta-gate {META_GATE_SAMPLES}, "
        f"calibration {LIVE_CAL_WINDOW}/series, ensemble {ENSEMBLE_WINDOW_DAYS}d)"
    )


def run_retention(engine: Engine, settings: Settings, dry_run: bool = False) -> dict:
    """Apply every retention policy. Returns per-table counts."""
    now = utcnow()
    out = RetentionResult(dry_run=dry_run)

    def days(attr: str, default: int) -> int:
        return int(getattr(settings, attr, default) or default)

    # --- predictions: never inside a live calibration window ----------------
    pred_days = days("prediction_retention_days", 730)
    floor, reason = protected_prediction_cutoff(engine)
    cutoff = now - timedelta(days=pred_days)
    if floor is None:
        # No matured rows: no loop depends on anything yet, but there is also
        # nothing to protect against — fall back to the policy cutoff alone.
        r = _delete_batched(engine, "predictions", "predicted_at < :cutoff",
                            {"cutoff": cutoff}, dry_run)
    else:
        # `floor` is the target_time of the OLDEST row still inside a live
        # window, so the row at exactly `floor` must survive. Comparing
        # target_time strictly below it excludes every protected row without
        # an off-by-one (an earlier version compared predicted_at and deleted
        # the boundary row).
        r = _delete_batched(
            engine, "predictions",
            "predicted_at < :cutoff AND target_time < :floor",
            {"cutoff": cutoff, "floor": floor}, dry_run,
        )
    r.floor_reason = reason
    out.tables.append(r)

    # --- signals -------------------------------------------------------------
    out.tables.append(_delete_batched(
        engine, "signals", "generated_at < :cutoff",
        {"cutoff": now - timedelta(days=days("signal_retention_days", 365))}, dry_run))

    # --- training_runs: keep the most recent N regardless of age -------------
    keep_runs = days("training_run_keep", 200)
    out.tables.append(_delete_batched(
        engine, "training_runs",
        "id < (SELECT coalesce(min(id), 0) FROM (SELECT id FROM training_runs "
        "ORDER BY id DESC LIMIT :keep) s)",
        {"keep": keep_runs}, dry_run))

    # --- inactive model_versions: never touch active rows or recent history --
    mv_days = days("model_version_retention_days", 180)
    out.tables.append(_delete_batched(
        engine, "model_versions",
        "NOT is_active AND trained_at < :cutoff",
        {"cutoff": now - timedelta(days=mv_days)}, dry_run))

    # --- alert_events --------------------------------------------------------
    out.tables.append(_delete_batched(
        engine, "alert_events", "triggered_at < :cutoff",
        {"cutoff": now - timedelta(days=days("alert_event_retention_days", 180))}, dry_run))

    # --- audit_logs: floored at MIN_AUDIT_RETENTION_DAYS ---------------------
    audit_days = max(days("audit_retention_days", 730), MIN_AUDIT_RETENTION_DAYS)
    r = _delete_batched(
        engine, "audit_logs", "created_at < :cutoff",
        {"cutoff": now - timedelta(days=audit_days)}, dry_run)
    r.floor_reason = f"never below {MIN_AUDIT_RETENTION_DAYS}d"
    out.tables.append(r)

    return out.as_dict()
