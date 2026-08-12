"""Scheduler-callable evaluation of multi-timeframe trend alignment (Addendum 20).

The rules live in ``app/models/trend_alignment.py`` and are pure. What is added
here is everything that only exists because the evaluation runs repeatedly, on
a schedule, against a database that outlives the process:

*Candles must mean the same thing they mean in Go.* ``backend-go/internal/
prices/candles.go`` synthesizes OHLC buckets with ``date_trunc`` over
``prices`` where ``quality='ok'``, open = first value by ``observed_at``,
close = last. :func:`_load_candles` is the same aggregation spelled portably
(window functions instead of ``array_agg(... ORDER BY ...)``, which SQLite has
no equivalent of), so the chart a user reads and the trend this job publishes
are derived from identical buckets. If the two ever disagreed, the indicator
would be contradicting the chart it is drawn on.

*Being aligned is not an event; becoming aligned is.* A transition is recorded
only when the STORED alignment differs from the new one and the new one is a
full alignment. That comparison is against the database, never against a
process variable: this job restarts on every deploy, and an in-memory
"previous" would re-fire an alert the user already acknowledged.

*The duplicate guard is the unique index, not the comparison above.* The
comparison decides whether to attempt an insert; ``uq_trend_alignment_event_
identity`` decides whether that insert becomes a row. Two evaluators racing
after a deploy, a restored backup, or a state row rolled back all end at the
same place — one event per (symbol, direction, closed-candle triple).

*One symbol's failure is its own.* Each symbol is evaluated inside its own
try/except and its own transaction, so a gap in one series cannot stop the
other symbol from being evaluated or leave a half-written state behind.

Nothing here touches model input, model selection, prediction confidence,
intervals or the buy/sell decision policy. It reads ``prices`` and writes its
own two tables plus (when a user has subscribed) one ``alert_events`` row.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Sequence

from prometheus_client import Counter
from sqlalchemy import JSON, DateTime, bindparam, select, text, update
from sqlalchemy.engine import Connection, Engine

from ..config import Settings
from ..db import (
    ensure_utc,
    trend_alignment_events,
    trend_alignment_states,
    utcnow,
)
from ..metrics import JOB_LAST_SUCCESS
from ..models.trend_alignment import (
    TIMEFRAME_SECONDS,
    TIMEFRAMES,
    WARMUP_FACTOR,
    AlignmentResult,
    Candle,
    TrendConfig,
    evaluate,
)

log = logging.getLogger(__name__)

# The only symbols with enough continuous price history for a 220-period slow
# MA on three timeframes. Everything else is reported skipped rather than
# evaluated against a series that cannot support the conclusion.
SUPPORTED_SYMBOLS: tuple[str, ...] = ("IR_GOLD_18K", "XAUUSD")

DISABLED_REASON = "TREND_ALIGNMENT_ENABLED is off"

FULL_ALIGNMENTS = frozenset({"full_bullish", "full_bearish"})

# alerts.alert_type of the opt-in subscription this job fans out to. An
# alert_events row cannot exist without an owning alerts row (alert_id and
# user_id are NOT NULL foreign keys, and the API joins the two), so
# "notify everyone" is not available without inventing subscriptions nobody
# made. Users who want this alert create it the same way they create every
# other alert.
TREND_ALERT_TYPE = "trend_alignment"

# Every message ends with this. The alignment is a technical observation about
# three moving averages, not a recommendation, and the text a user sees has to
# say so in the same breath.
DISCLAIMER = "Technical indicator only — not financial advice."

# How many buckets to pull per timeframe: enough for the slow MA to be fully
# warmed up, plus a couple for the still-forming candle the engine drops. The
# hourly pull also feeds the resampled 4H series, so it needs four hourly
# buckets per 4H bucket.
BUCKET_SLACK = 4
HOURLY_PER_4H = TIMEFRAME_SECONDS["4h"] // TIMEFRAME_SECONDS["1h"]

# Multiplier on the scanned time window. The bucket LIMIT already bounds the
# result, but an unbounded scan of `prices` would grow with the table forever,
# so the query is floored at this many bucket-widths back. Wide enough that
# ordinary collection gaps (a provider outage, a closed market) still leave
# enough buckets to warm the slow MA; narrow enough that the index range scan
# stays small.
SCAN_SLACK = 3.0

# --- metrics -----------------------------------------------------------------
# Defined here rather than in app/metrics.py because these are NEW series: the
# _Dual wrapper in that module exists to keep the deprecated ``goldpred_*``
# twins alive for dashboards that predate the rename, and a metric that never
# had an old name must not acquire one. Naming follows the same convention —
# ``talapala_prediction_*``, one counter per outcome, and the shared
# JOB_LAST_SUCCESS gauge for staleness.
TREND_EVALUATIONS = Counter(
    "talapala_prediction_trend_alignment_evaluations_total",
    "Trend-alignment evaluations completed, by symbol and resulting alignment",
    ["symbol", "alignment"],
)
TREND_FAILURES = Counter(
    "talapala_prediction_trend_alignment_failures_total",
    "Trend-alignment evaluations that raised, by symbol",
    ["symbol"],
)
TREND_ALERTS = Counter(
    "talapala_prediction_trend_alignment_alerts_total",
    "In-app alert_events rows written for a trend-alignment entry",
    ["symbol", "alignment"],
)

# Raw-SQL binds carry their type explicitly. Timestamps because an untyped
# datetime reaches the DBAPI unprocessed (sqlite3's implicit datetime adapter
# is deprecated and going away), JSON because the target column is JSONB and
# the value must be adapted, not stringified by accident.
_TS_PARAM = DateTime(timezone=True)
_JSON_PARAM = JSON()


def trend_config(settings: Settings) -> TrendConfig:
    """Validated :class:`TrendConfig` from settings (single source of truth)."""
    config = TrendConfig(
        enabled=settings.trend_alignment_enabled,
        ma_type=settings.trend_alignment_ma_type,  # type: ignore[arg-type]
        fast=settings.trend_alignment_fast_period,
        mid=settings.trend_alignment_mid_period,
        slow=settings.trend_alignment_slow_period,
    )
    config.validate()
    return config


# --- candles ----------------------------------------------------------------


def _bucket_expr(dialect: str, unit: str) -> str:
    """The bucket key, per dialect.

    PostgreSQL gets exactly what ``candles.go`` uses. ``date_trunc`` on a
    ``timestamptz`` truncates in the session time zone, which is UTC in the
    deployed container — the same session setting the Go query runs under, so
    both services floor onto the same boundaries. SQLite (tests) has no
    ``date_trunc``; ``strftime`` on the UTC-normalized values stored by
    SQLAlchemy produces the identical buckets.
    """
    if dialect == "postgresql":
        return f"date_trunc('{unit}', observed_at)"
    if dialect == "sqlite":
        fmt = "%Y-%m-%d %H:00:00" if unit == "hour" else "%Y-%m-%d 00:00:00"
        return f"strftime('{fmt}', observed_at)"
    raise RuntimeError(f"unsupported dialect for candle aggregation: {dialect}")


def _parse_bucket(value: Any) -> datetime:
    """Bucket start as an aware UTC datetime (SQLite hands back a string)."""
    if isinstance(value, datetime):
        return ensure_utc(value)  # type: ignore[return-value]
    return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)


def _load_candles(
    conn: Connection, symbol: str, unit: str, limit: int, now: datetime
) -> list[Candle]:
    """The last ``limit`` OHLC buckets for ``symbol``, oldest first — ONE query.

    Same shape as ``candles.go``: first value in the bucket is the open, last
    is the close, max/min are the high/low, and only ``quality='ok'`` rows take
    part (a suspicious tick must not become a candle extreme). ``row_number``
    replaces ``array_agg(value ORDER BY observed_at)`` because the ordered
    aggregate is PostgreSQL-only; the selection is identical, with ``id`` as a
    deterministic tie-break for two sources quoting the same instant.

    Buckets are taken from the newest end (``ORDER BY bucket DESC LIMIT``)
    rather than by a fixed date range, so a series with gaps still yields as
    much warm-up history as it actually has instead of silently short-changing
    the slow MA.

    The window is closed at ``now`` on BOTH ends. The upper bound is not
    redundant with the engine's completeness filter: that filter drops
    candles, but the ``LIMIT`` runs first, so ticks stamped after ``now`` (a
    provider with a skewed clock, or any replay at a historical instant) would
    otherwise consume the newest slots and starve the evaluation of the
    history it is supposed to read.
    """
    bucket = _bucket_expr(conn.dialect.name, unit)
    seconds = TIMEFRAME_SECONDS["1h" if unit == "hour" else "1d"]
    since = now - timedelta(seconds=seconds * limit * SCAN_SLACK)
    sql = text(
        f"""
        SELECT bucket, open, high, low, close FROM (
            SELECT bucket,
                   max(CASE WHEN rn_first = 1 THEN value END) AS open,
                   max(value) AS high,
                   min(value) AS low,
                   max(CASE WHEN rn_last = 1 THEN value END) AS close
            FROM (
                SELECT {bucket} AS bucket, value,
                       row_number() OVER (PARTITION BY {bucket}
                                          ORDER BY observed_at ASC, id ASC) AS rn_first,
                       row_number() OVER (PARTITION BY {bucket}
                                          ORDER BY observed_at DESC, id DESC) AS rn_last
                FROM prices
                WHERE symbol = :symbol AND quality = 'ok'
                  AND observed_at >= :since AND observed_at <= :now
            ) ticks
            GROUP BY bucket
            ORDER BY bucket DESC
            LIMIT :limit
        ) recent
        ORDER BY bucket ASC
        """  # noqa: S608 - `bucket` is a dialect constant, never user input
    ).bindparams(
        bindparam("since", type_=_TS_PARAM), bindparam("now", type_=_TS_PARAM)
    )
    rows = conn.execute(
        sql, {"symbol": symbol, "since": since, "now": now, "limit": limit}
    ).all()
    return [
        Candle(
            start=_parse_bucket(row.bucket),
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
        )
        for row in rows
    ]


def _bucket_limits(config: TrendConfig) -> tuple[int, int]:
    """(hourly, daily) bucket counts to load for this configuration."""
    warm = int(config.slow * WARMUP_FACTOR) + BUCKET_SLACK
    return warm * HOURLY_PER_4H, warm


# --- persistence -------------------------------------------------------------


def _dialect_insert(conn: Connection):
    """``INSERT`` construct with ``ON CONFLICT`` support for this dialect."""
    if conn.dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif conn.dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - not used in this project
        raise RuntimeError(f"unsupported dialect: {conn.dialect.name}")
    return dialect_insert


def _stored_state(conn: Connection, symbol: str) -> Optional[dict]:
    row = conn.execute(
        select(trend_alignment_states).where(trend_alignment_states.c.symbol == symbol)
    ).mappings().first()
    return dict(row) if row else None


def _upsert_state(
    conn: Connection,
    result: AlignmentResult,
    config: TrendConfig,
    stored: Optional[dict],
    identity: dict[str, Optional[datetime]],
    *,
    alerted_at: Optional[datetime],
) -> None:
    """Write the current conclusion for this symbol (one row, upserted).

    ``previous_alignment`` only moves when the alignment itself moves: a run
    that re-confirms the same alignment must not overwrite the record of what
    the symbol was before it got here. ``state_version`` counts real changes
    for the same reason.
    """
    changed = stored is None or stored["alignment"] != result.alignment
    previous = (
        stored["alignment"]
        if (stored is not None and changed)
        else (stored or {}).get("previous_alignment")
    )
    values: dict[str, Any] = {
        "symbol": result.symbol,
        "alignment": result.alignment,
        "previous_alignment": previous,
        "timeframes": {tf: r.as_dict() for tf, r in result.timeframes.items()},
        "ma_type": config.ma_type,
        "fast_period": config.fast,
        "mid_period": config.mid,
        "slow_period": config.slow,
        "data_fresh": result.data_fresh,
        "latest_1h_candle_close": identity["1h"],
        "latest_4h_candle_close": identity["4h"],
        "latest_1d_candle_close": identity["1d"],
        "last_bullish_alert_at": (stored or {}).get("last_bullish_alert_at"),
        "last_bearish_alert_at": (stored or {}).get("last_bearish_alert_at"),
        "state_version": int((stored or {}).get("state_version") or 0) + (1 if changed else 0),
        "calculated_at": result.calculated_at,
        "updated_at": utcnow(),
    }
    if values["state_version"] < 1:
        values["state_version"] = 1
    if alerted_at is not None:
        key = (
            "last_bullish_alert_at"
            if result.alignment == "full_bullish"
            else "last_bearish_alert_at"
        )
        values[key] = alerted_at

    insert = _dialect_insert(conn)
    stmt = insert(trend_alignment_states).values(**values)
    conn.execute(
        stmt.on_conflict_do_update(
            index_elements=[trend_alignment_states.c.symbol],
            set_={k: v for k, v in values.items() if k != "symbol"},
        )
    )


def _insert_event(
    conn: Connection,
    result: AlignmentResult,
    config: TrendConfig,
    previous: Optional[str],
    identity: dict[str, Optional[datetime]],
) -> Optional[int]:
    """Record an ENTRY into a full alignment; None when it already existed.

    ``ON CONFLICT DO NOTHING`` against ``uq_trend_alignment_event_identity``
    is what makes a re-run over unchanged candles a no-op. The insert reports
    through ``RETURNING id`` rather than ``rowcount`` — psycopg reports -1 for
    a conflicting ``DO NOTHING``, so rowcount cannot distinguish "skipped"
    from "written" (the same reason ``db.insert_ignore`` uses RETURNING), and
    the id is needed to link the alert row back.
    """
    insert = _dialect_insert(conn)
    stmt = (
        insert(trend_alignment_events)
        .values(
            symbol=result.symbol,
            alignment=result.alignment,
            previous_alignment=previous,
            occurred_at=result.calculated_at,
            latest_1h_candle_close=identity["1h"],
            latest_4h_candle_close=identity["4h"],
            latest_1d_candle_close=identity["1d"],
            timeframes={tf: r.as_dict() for tf, r in result.timeframes.items()},
            ma_type=config.ma_type,
        )
        .on_conflict_do_nothing(
            index_elements=[
                trend_alignment_events.c.symbol,
                trend_alignment_events.c.alignment,
                trend_alignment_events.c.latest_1d_candle_close,
                trend_alignment_events.c.latest_4h_candle_close,
                trend_alignment_events.c.latest_1h_candle_close,
            ]
        )
        .returning(trend_alignment_events.c.id)
    )
    row = conn.execute(stmt).first()
    return int(row[0]) if row is not None else None


# --- alerting ----------------------------------------------------------------


def _fmt(value: Optional[float]) -> str:
    return "n/a" if value is None else f"{value:,.2f}"


def alert_message(result: AlignmentResult, config: TrendConfig) -> str:
    """The text a user reads, carrying the evidence rather than a verdict.

    Every number the conclusion was drawn from is in the message: a user who
    disagrees with the alert can check it against the chart without opening
    the API, and one who reads it a week later still knows which candles it
    was about.
    """
    direction = "BULLISH" if result.alignment == "full_bullish" else "BEARISH"
    ma = config.ma_type.upper()
    parts = []
    for tf in TIMEFRAMES:
        res = result.timeframes.get(tf)
        if res is None:
            continue
        parts.append(
            f"{tf.upper()} {res.trend}: price {_fmt(res.price)} vs "
            f"{ma}{config.fast} {_fmt(res.ma26)}, "
            f"{ma}{config.mid} {_fmt(res.ma48)}, "
            f"{ma}{config.slow} {_fmt(res.ma220)}"
        )
    return (
        f"{result.symbol}: full {direction} alignment — 1D, 4H and 1H all agree "
        f"on closed candles ({ma} {config.fast}/{config.mid}/{config.slow}). "
        + "; ".join(parts)
        + f". {DISCLAIMER}"
    )


def _write_alert_events(
    conn: Connection,
    result: AlignmentResult,
    config: TrendConfig,
    event_id: int,
    now: datetime,
) -> Optional[int]:
    """Fan the entry out to subscribed users; returns the first alert_events id.

    Subscription is the ``alerts`` row, because the schema leaves no other
    honest option: ``alert_events.alert_id``/``user_id`` are NOT NULL foreign
    keys and ``GET /api/v1/alerts/events`` joins ``alerts`` for the type. No
    subscriber therefore means no alert row — the transition is still recorded
    in ``trend_alignment_events``, which is what the UI card reads.

    ``alerts.cooldown_minutes`` is deliberately NOT applied. The cooldown
    exists to stop a threshold alert re-firing while a price hovers on the
    line; entries here are already unique per closed-candle triple, so a
    cooldown could only suppress a genuine entry that will never come round
    again.
    """
    subscribers = conn.execute(
        text(
            "SELECT id, user_id FROM alerts "
            "WHERE enabled AND alert_type = :alert_type ORDER BY id"
        ),
        {"alert_type": TREND_ALERT_TYPE},
    ).all()
    if not subscribers:
        return None

    message = alert_message(result, config)
    payload = {
        "source": "trend_alignment",
        "symbol": result.symbol,
        "alignment": result.alignment,
        "ma_type": config.ma_type,
        "periods": {"fast": config.fast, "mid": config.mid, "slow": config.slow},
        "timeframes": {tf: r.as_dict() for tf, r in result.timeframes.items()},
        "trend_alignment_event_id": event_id,
        "disclaimer": DISCLAIMER,
    }

    insert_event = text(
        "INSERT INTO alert_events (alert_id, user_id, triggered_at, message, payload) "
        "VALUES (:alert_id, :user_id, :triggered_at, :message, :payload) "
        "RETURNING id"
    ).bindparams(
        bindparam("triggered_at", type_=_TS_PARAM), bindparam("payload", type_=_JSON_PARAM)
    )
    touch_alert = text(
        "UPDATE alerts SET last_triggered_at = :now, updated_at = :now WHERE id = :id"
    ).bindparams(bindparam("now", type_=_TS_PARAM))

    first_id: Optional[int] = None
    for alert_id, user_id in subscribers:
        row = conn.execute(
            insert_event,
            {
                "alert_id": alert_id,
                "user_id": user_id,
                "triggered_at": now,
                "message": message,
                "payload": payload,
            },
        ).first()
        if row is not None and first_id is None:
            first_id = int(row[0])
        # Mirrors backend-go/internal/alerts/runner.go: the subscription row
        # records when it last fired, whichever service fired it.
        conn.execute(touch_alert, {"now": now, "id": alert_id})
        TREND_ALERTS.labels(symbol=result.symbol, alignment=result.alignment).inc()
    return first_id


# --- the pass ----------------------------------------------------------------


def _evaluate_symbol(
    engine: Engine,
    symbol: str,
    config: TrendConfig,
    now: datetime,
) -> dict[str, Any]:
    """Evaluate, persist, and record a transition for one symbol."""
    hourly_limit, daily_limit = _bucket_limits(config)
    with engine.connect() as conn:
        hourly = _load_candles(conn, symbol, "hour", hourly_limit, now)
        daily = _load_candles(conn, symbol, "day", daily_limit, now)

    result = evaluate(symbol, hourly, daily, now, config)
    identity_iso = result.candle_identity()
    identity = {tf: result.timeframes[tf].candle_close_time for tf in TIMEFRAMES}

    outcome: dict[str, Any] = {
        "symbol": symbol,
        "alignment": result.alignment,
        "data_fresh": result.data_fresh,
        "hourly_candles": len(hourly),
        "daily_candles": len(daily),
        "candle_identity": identity_iso,
        "event_created": False,
        "alerted": False,
    }

    with engine.begin() as conn:
        stored = _stored_state(conn, symbol)
        previous = stored["alignment"] if stored else None
        outcome["previous_alignment"] = previous

        event_id: Optional[int] = None
        alert_event_id: Optional[int] = None
        # A transition is an ENTRY into a full alignment. Leaving one is a
        # state change (persisted below) but not an event: there is nothing to
        # act on in "the stack stopped agreeing".
        if result.alignment in FULL_ALIGNMENTS and previous != result.alignment:
            if all(identity[tf] is not None for tf in TIMEFRAMES):
                event_id = _insert_event(conn, result, config, previous, identity)
            else:  # pragma: no cover - a full alignment always has three closed candles
                log.warning(
                    "trend alignment %s is %s with an incomplete candle identity %s; "
                    "no event recorded",
                    symbol, result.alignment, identity_iso,
                )

        if event_id is not None:
            outcome["event_created"] = True
            outcome["event_id"] = event_id
            # An alert that cannot be written must not roll back the event:
            # the event is the record, the alert is the notification, and a
            # missing alerts table (or a deleted user) is not a reason to
            # re-decide the transition on the next run.
            try:
                alert_event_id = _write_alert_events(conn, result, config, event_id, now)
            except Exception as exc:  # noqa: BLE001 - see comment above
                log.warning("trend alignment alert write failed for %s: %s", symbol, exc)
            if alert_event_id is not None:
                conn.execute(
                    update(trend_alignment_events)
                    .where(trend_alignment_events.c.id == event_id)
                    .values(alert_event_id=alert_event_id)
                )
                outcome["alerted"] = True
                outcome["alert_event_id"] = alert_event_id

        _upsert_state(
            conn, result, config, stored, identity,
            alerted_at=now if alert_event_id is not None else None,
        )

    TREND_EVALUATIONS.labels(symbol=symbol, alignment=result.alignment).inc()
    return outcome


def run_trend_alignment(
    engine: Engine,
    settings: Settings,
    *,
    symbols: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Run one evaluation pass; returns per-symbol outcomes and counts.

    ``symbols`` narrows the pass (an unsupported symbol is reported skipped,
    so an operator typo is visible instead of silently doing nothing).
    ``now`` is injectable for tests and replays; it decides which candles
    count as closed, so it must be an instant, never "whatever the row says".
    """
    counts: dict[str, Any] = {
        "enabled": True,
        "symbols": {},
        "evaluated": 0,
        "skipped": 0,
        "failed": 0,
        "events": 0,
        "alerts": 0,
        "errors": [],
    }
    if not settings.trend_alignment_enabled:
        counts["enabled"] = False
        counts["reason"] = DISABLED_REASON
        return counts

    config = trend_config(settings)
    now = ensure_utc(now) or utcnow()
    counts["as_of"] = now.isoformat()
    counts["ma_type"] = config.ma_type
    counts["periods"] = {"fast": config.fast, "mid": config.mid, "slow": config.slow}

    requested = [str(s) for s in symbols] if symbols else list(SUPPORTED_SYMBOLS)
    for symbol in requested:
        if symbol not in SUPPORTED_SYMBOLS:
            counts["symbols"][symbol] = {
                "symbol": symbol, "status": "skipped", "reason": "unsupported symbol",
            }
            counts["skipped"] += 1
            continue
        try:
            outcome = _evaluate_symbol(engine, symbol, config, now)
        except Exception as exc:  # noqa: BLE001 - one symbol must not sink the pass
            log.warning("trend alignment failed for %s: %s", symbol, exc)
            TREND_FAILURES.labels(symbol=symbol).inc()
            counts["symbols"][symbol] = {
                "symbol": symbol, "status": "error", "reason": type(exc).__name__,
            }
            counts["failed"] += 1
            counts["errors"].append(f"{symbol}: {exc}")
            continue
        outcome["status"] = "ok"
        counts["symbols"][symbol] = outcome
        counts["evaluated"] += 1
        counts["events"] += 1 if outcome["event_created"] else 0
        counts["alerts"] += 1 if outcome["alerted"] else 0

    if counts["evaluated"]:
        # Only a pass that actually evaluated something may refresh the
        # staleness gauge; otherwise a pass where every symbol failed would
        # look healthy to the alerting rules.
        JOB_LAST_SUCCESS.labels(job="trend_alignment").set(time.time())
    return counts
