"""Write Tehran equity bars, the actions in them, and the gate's verdict.

**This module is fed files, it never fetches one.**  cdn.tsetmc.com is
unreachable from the production host: DNS resolves to 94.182.113.115,
46.102.143.218 and 212.16.75.245, and TCP 443 fails outright — a harder block
than the SCI one migration 0027 records, where the connection opened and the
payload was dropped.  Every endpoint returns http=000 from there and 200 from
outside that network.  So a server-side cron would fail every tick forever, and
:mod:`scripts.tsetmc_fetch` fetches where TSETMC answers, copies the payloads in
over the existing SSH access, and calls ``POST /internal/equities/bars`` inside
the compose network.  Exactly the shape :mod:`app.economic.sci` established.

WHAT THIS MODULE GUARANTEES
---------------------------
*Per-symbol isolation.*  One malformed payload must not cost the other
nineteen.  Each symbol is parsed, adjusted and written in its OWN transaction,
and a failure is collected into the report rather than raised.

*Raw bars are raw.*  ``equity_bars`` receives ``final_close`` and never
``final_close * factor``.  The adjustment is a factor on ``corporate_actions``,
applied at read time — which is also why re-detecting an action never has to
rewrite the bar table.

*Nothing is invented.*  An insCode the roster does not carry is refused; this
never creates an ``equity_instruments`` row from a payload.  The roster is
data (0028 seeds it), so widening it is an INSERT, not a code change.

*A stored bar is never overwritten.*  TSETMC does not revise a settled session,
so a payload that disagrees with a stored bar is a contradiction, not a
revision, and the symbol FAILS rather than having its history rewritten.  There
is no vintage machinery here on purpose: ``economic_observations`` has it
because macro statistics are restated by their publishers, and a daily bar is
not.  A real restatement is a human decision, and it should arrive as one.

*The gate is enforced by a row.*  ``equity_adjustments`` records the verdict per
symbol per ``adjustment_version``, and the read API serves an adjusted series
only when that row says ``validated``.  A refusal still stores the raw bars and
the detected actions — they are what TSETMC served and what it implied, and
both stay true — because throwing the evidence away with the conclusion leaves
nothing to diagnose the refusal with.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import Connection, Engine

from ..db import (
    corporate_actions,
    ensure_utc,
    equity_adjustments,
    equity_bars,
    equity_instruments,
    insert_ignore,
    utcnow,
)
from .adjust import (
    ADJUSTMENT_VERSION,
    KIND_CLOSE_RESTATED,
    KIND_REFERENCE_RESTATED,
    MAX_REOPENING_RETURN,
    MAX_SESSION_RETURN,
    SESSION_GAP_DAYS,
    STATUS_REFUSED,
    STATUS_VALIDATED,
    AdjustedSeries,
    Bar,
    BarParseError,
    adjust,
    parse_daily_list,
)

log = logging.getLogger(__name__)

PROVIDER_CODE = "tsetmc_cdn"

# A payload big enough to be a real instrument history.  فولاد's is 1.4 MB and
# the smallest in the roster (ذوب, listed 2021) is 361 KB; a WAF page or a
# truncated transfer is orders of magnitude smaller.  Checked here as well as
# in the fetch script because this endpoint can be called by hand.
MIN_PLAUSIBLE_BYTES = 1_000


class EquityIngestFailed(RuntimeError):
    """Every symbol in the pass failed.  Carries the full report."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


# --- roster ------------------------------------------------------------------


def roster(bind: Engine, include_disabled: bool = False) -> list[dict[str, Any]]:
    """The instruments this deployment is willing to ingest and serve.

    ``scripts/tsetmc_fetch.py`` reads this over the same SSH path it already
    uses, which is what makes "widen the roster" an INSERT into
    ``equity_instruments`` rather than an edit to a list in two places that
    then drift.
    """
    columns = equity_instruments
    stmt = select(
        columns.c.ins_code, columns.c.symbol_fa, columns.c.name_fa,
        columns.c.market, columns.c.board, columns.c.sector_code,
        columns.c.sector_fa, columns.c.isin, columns.c.instrument_code,
        columns.c.first_bar, columns.c.last_bar, columns.c.bar_count,
        columns.c.enabled, columns.c.notes,
    ).order_by(columns.c.symbol_fa)
    if not include_disabled:
        stmt = stmt.where(columns.c.enabled.is_(True))
    with bind.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        {
            **dict(row),
            "first_bar": row["first_bar"].isoformat() if row["first_bar"] else None,
            "last_bar": row["last_bar"].isoformat() if row["last_bar"] else None,
        }
        for row in rows
    ]


def _instrument(conn: Connection, ins_code: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        select(
            equity_instruments.c.ins_code,
            equity_instruments.c.symbol_fa,
            equity_instruments.c.enabled,
        ).where(equity_instruments.c.ins_code == ins_code)
    ).mappings().first()
    return dict(row) if row else None


# --- writes ------------------------------------------------------------------


def _write_bars(
    conn: Connection, ins_code: str, bars: Sequence[Bar], collected_at: datetime
) -> dict[str, int]:
    """Insert the bars that are not stored yet; refuse to change one that is.

    The existing rows are read first — 4,600 of them for the longest history,
    which costs nothing — so the counts are exact and a CONTRADICTION is
    visible.  A payload that disagrees with a stored session raises: TSETMC
    does not revise a settled bar, so the disagreement means either a corrupt
    transfer or a genuine restatement, and both deserve a human rather than an
    UPDATE that erases what was previously served.
    """
    stored = {
        row["trade_date"]: row
        for row in conn.execute(
            select(
                equity_bars.c.trade_date,
                equity_bars.c.final_close,
                equity_bars.c.price_yesterday,
                equity_bars.c.volume,
            ).where(equity_bars.c.ins_code == ins_code)
        ).mappings()
    }

    conflicts: list[str] = []
    pending: list[dict[str, Any]] = []
    for bar in bars:
        existing = stored.get(bar.trade_date)
        if existing is None:
            pending.append(
                {
                    "ins_code": ins_code,
                    "trade_date": bar.trade_date,
                    # RAW.  Never `* factor`: this table is what TSETMC served.
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "final_close": bar.final_close,
                    "price_yesterday": bar.price_yesterday,
                    "volume": bar.volume,
                    "trade_count": bar.trade_count,
                    "value": bar.value,
                    "collected_at": collected_at,
                }
            )
            continue
        if (
            float(existing["final_close"]) != bar.final_close
            or float(existing["price_yesterday"]) != bar.price_yesterday
            or int(existing["volume"]) != bar.volume
        ):
            conflicts.append(
                f"{bar.trade_date.isoformat()}: stored close="
                f"{float(existing['final_close'])} yesterday="
                f"{float(existing['price_yesterday'])} volume={int(existing['volume'])}, "
                f"payload close={bar.final_close} yesterday={bar.price_yesterday} "
                f"volume={bar.volume}"
            )

    if conflicts:
        shown = "; ".join(conflicts[:3])
        more = "" if len(conflicts) <= 3 else f" and {len(conflicts) - 3} more"
        raise BarParseError(
            f"{ins_code}: the payload contradicts {len(conflicts)} stored bar(s): "
            f"{shown}{more}. TSETMC does not revise a settled session, so this is "
            "a corrupt transfer or a genuine restatement — either way a human "
            "decides, and nothing is overwritten here."
        )

    inserted = insert_ignore(conn, equity_bars, pending) if pending else 0
    return {
        "bars_total": len(bars),
        "bars_inserted": inserted,
        "bars_existing": len(bars) - len(pending),
        # A row that was pending and did not insert lost a race with a
        # concurrent ingest of the same symbol.  The unique constraint made
        # that safe; the count says it happened.
        "bars_raced": len(pending) - inserted,
    }


def _write_actions(
    conn: Connection, ins_code: str, series: AdjustedSeries, detected_at: datetime
) -> dict[str, int]:
    """Insert the corporate actions for this version; refuse to restate one.

    Keyed ``(ins_code, effective_date, adjustment_version)``, so a v2 detector
    lands beside v1 instead of over it.  A stored action whose ratio disagrees
    with a freshly detected one means the bars underneath it changed, which
    ``_write_bars`` has already refused — reaching here would mean the two
    disagree about the same data, so it raises rather than picking one.
    """
    stored = {
        row["effective_date"]: row
        for row in conn.execute(
            select(
                corporate_actions.c.effective_date,
                corporate_actions.c.ratio,
                corporate_actions.c.cumulative_factor,
            ).where(
                and_(
                    corporate_actions.c.ins_code == ins_code,
                    corporate_actions.c.adjustment_version == series.version,
                )
            )
        ).mappings()
    }

    pending: list[dict[str, Any]] = []
    restated: list[str] = []
    for action in series.actions:
        existing = stored.get(action.effective_date)
        if existing is None:
            pending.append(
                {
                    "ins_code": ins_code,
                    "effective_date": action.effective_date,
                    "prev_trade_date": action.prev_trade_date,
                    "prev_close": action.prev_close,
                    "price_yesterday": action.price_yesterday,
                    "kind": action.kind,
                    "restated_close": action.restated_close,
                    "ratio": action.ratio,
                    "cumulative_factor": action.cumulative_factor,
                    "adjustment_version": series.version,
                    "detected_at": detected_at,
                }
            )
            continue
        if not _close_enough(float(existing["ratio"]), action.ratio):
            restated.append(
                f"{action.effective_date.isoformat()}: stored ratio "
                f"{float(existing['ratio'])!r}, detected {action.ratio!r}"
            )

    if restated:
        raise BarParseError(
            f"{ins_code}: {len(restated)} stored corporate action(s) disagree with "
            f"what this payload implies ({'; '.join(restated[:3])}). The ratio is "
            "derived from two stored bars, so a disagreement means the bars changed "
            "underneath it; refusing rather than restating a factor that every "
            "adjusted price before that date depends on."
        )

    # The cumulative factor of EVERY earlier action changes when a new action
    # lands after it, so extending the history rewrites the chain.  That is the
    # one thing this table does update: the ratio (the evidence) is immutable,
    # the factor (the arithmetic over it) is recomputed.  Both stay auditable
    # because the two numbers the ratio came from are on the row.
    refactored = 0
    for action in series.actions:
        existing = stored.get(action.effective_date)
        if existing is None:
            continue
        if _close_enough(float(existing["cumulative_factor"]), action.cumulative_factor):
            continue
        conn.execute(
            update(corporate_actions)
            .where(
                and_(
                    corporate_actions.c.ins_code == ins_code,
                    corporate_actions.c.effective_date == action.effective_date,
                    corporate_actions.c.adjustment_version == series.version,
                )
            )
            .values(cumulative_factor=action.cumulative_factor)
        )
        refactored += 1

    inserted = insert_ignore(conn, corporate_actions, pending) if pending else 0
    return {
        "actions_total": len(series.actions),
        "actions_reference_restated": sum(
            1 for a in series.actions if a.kind == KIND_REFERENCE_RESTATED
        ),
        # Reported separately because it is the mechanism a priceYesterday-only
        # detector misses, and its absence in a symbol that should have some is
        # the shape of a regression.
        "actions_close_restated": sum(
            1 for a in series.actions if a.kind == KIND_CLOSE_RESTATED
        ),
        "actions_inserted": inserted,
        "actions_existing": len(series.actions) - len(pending),
        "actions_refactored": refactored,
    }


def _close_enough(left: float, right: float) -> bool:
    """Numeric equality for a ratio that has round-tripped through NUMERIC.

    Same reasoning as :func:`app.economic.store.values_equal`: comparing string
    forms would call a formatting difference a restatement.  The tolerance is
    looser here (1e-12 rather than 1e-15) because a cumulative factor is a
    product of up to sixty ratios and accumulates float error across them,
    while the smallest meaningful difference in a corporate-action factor is
    parts in ten thousand.
    """
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) <= 1e-12 * scale


def _write_verdict(
    conn: Connection, ins_code: str, series: AdjustedSeries, computed_at: datetime
) -> str:
    """Record (or refresh) the gate's verdict for this symbol and version.

    This row is the enforcement point for REDESIGN's P3 gate — "adjustment must
    be validated before any return, ratio or score is computed from it" — so it
    is written LAST, after the bars and actions it describes are in place.  It
    is also the one row here that is UPDATED rather than appended: a verdict is
    a statement about the series as it stands now, and a stale one saying
    "validated" while a later ingest found a violation would be worse than none.
    """
    validation = series.validation
    traded = series.traded_bars
    values = {
        "ins_code": ins_code,
        "adjustment_version": series.version,
        "status": validation.status,
        "actions_applied": len(series.actions),
        "bars_total": len(series.bars),
        "pre_listing_bars": series.pre_listing_bars,
        "sessions_checked": validation.sessions_checked,
        "worst_return": validation.worst_return,
        "worst_return_date": validation.worst_return_date,
        "reopenings": validation.reopenings,
        "max_session_return": validation.max_session_return,
        "max_reopening_return": validation.max_reopening_return,
        "session_gap_days": validation.session_gap_days,
        "first_bar": traded[0].trade_date if traded else None,
        "last_bar": traded[-1].trade_date if traded else None,
        "refusal_reason": validation.refusal_reason,
        "computed_at": computed_at,
    }
    existing = conn.execute(
        select(equity_adjustments.c.id).where(
            and_(
                equity_adjustments.c.ins_code == ins_code,
                equity_adjustments.c.adjustment_version == series.version,
            )
        )
    ).scalar()
    if existing is None:
        insert_ignore(conn, equity_adjustments, [values])
        return "inserted"
    conn.execute(
        update(equity_adjustments)
        .where(equity_adjustments.c.id == existing)
        .values(**{k: v for k, v in values.items() if k != "ins_code"})
    )
    return "updated"


def _write_coverage(conn: Connection, ins_code: str, now: datetime) -> None:
    """Refresh the roster row's coverage from what is actually stored.

    Aggregated in the DATABASE, and from the TABLE rather than from the payload:
    the coverage columns answer "what does this database hold?", and deriving
    them from the file just ingested would make them a claim about the last
    transfer instead — which would be wrong the moment a partial payload is
    posted for a symbol that already has history.
    """
    row = conn.execute(
        select(
            func.min(equity_bars.c.trade_date),
            func.max(equity_bars.c.trade_date),
            func.count(),
        ).where(equity_bars.c.ins_code == ins_code)
    ).one()
    conn.execute(
        update(equity_instruments)
        .where(equity_instruments.c.ins_code == ins_code)
        .values(
            first_bar=row[0],
            last_bar=row[1],
            bar_count=int(row[2] or 0),
            updated_at=now,
        )
    )


# --- the ingest --------------------------------------------------------------


def ingest_payload(
    engine: Engine,
    payload: Any,
    ins_code: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ingest one instrument's daily-bar payload.  One transaction.

    Everything is parsed, adjusted and validated BEFORE the transaction opens,
    so a payload that fails any guard leaves the database exactly as it was.
    """
    collected_at = ensure_utc(now) or utcnow()
    series = adjust(parse_daily_list(payload, ins_code=ins_code))

    with engine.begin() as conn:
        instrument = _instrument(conn, series.ins_code)
        if instrument is None:
            # Never invents a roster row from a payload.  The roster is the
            # deployment's decision about what it stores and serves, and a file
            # arriving on disk is not that decision.
            raise BarParseError(
                f"insCode {series.ins_code} is not in equity_instruments. The roster "
                "is data (migration 0028 seeds it): add the instrument with an "
                "INSERT, then ingest. Nothing here creates a registry row from a "
                "payload."
            )
        report: dict[str, Any] = {
            "ins_code": series.ins_code,
            "symbol": instrument["symbol_fa"],
            "enabled": bool(instrument["enabled"]),
            "adjustment_version": series.version,
        }
        report.update(_write_bars(conn, series.ins_code, series.bars, collected_at))
        report.update(_write_actions(conn, series.ins_code, series, collected_at))
        report["verdict"] = _write_verdict(conn, series.ins_code, series, collected_at)
        _write_coverage(conn, series.ins_code, collected_at)

    validation = series.validation
    report.update(
        {
            "status": validation.status,
            "pre_listing_bars": series.pre_listing_bars,
            "first_bar": series.traded_bars[0].trade_date.isoformat(),
            "last_bar": series.traded_bars[-1].trade_date.isoformat(),
            "raw_ratio": round(series.raw_ratio, 6),
            "adjusted_ratio": round(series.adjusted_ratio, 6),
            "sessions_checked": validation.sessions_checked,
            "reopenings": validation.reopenings,
            "worst_session_return": validation.worst_return,
            "worst_session_date": (
                validation.worst_return_date.isoformat()
                if validation.worst_return_date
                else None
            ),
        }
    )
    if validation.refusal_reason:
        report["refusal_reason"] = validation.refusal_reason
    return report


def read_payload_file(path: str) -> Any:
    """Load one payload from disk, with the refusals a transfer can need."""
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no payload at {path}")
    size = os.path.getsize(path)
    if size < MIN_PLAUSIBLE_BYTES:
        raise BarParseError(
            f"{os.path.basename(path)} is {size} bytes — an error page or a "
            "truncated transfer, not an instrument history (the smallest in the "
            "roster is 361 KB). Refusing to parse it."
        )
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError as exc:
            raise BarParseError(
                f"{os.path.basename(path)} is not valid JSON: {exc}"
            ) from exc


def ingest_bar_files(
    engine: Engine,
    paths: Sequence[str],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ingest several instruments' payloads, each isolated from the others.

    One symbol failing must not abort the rest — a single truncated transfer in
    a twenty-symbol run would otherwise cost the whole run — so each path gets
    its own try/except and its own transaction, and failures are collected into
    ``errors`` rather than raised.

    Raises :class:`EquityIngestFailed` only when EVERY path failed.  The
    endpoint turns that into a 502, matching ``/internal/economic/ingest``: the
    Go scheduler reads job success from the status code, and a pass that
    ingested nothing must not enter that history as a success.  A partial
    failure stays a success with an ``errors`` list — one bad file is not a
    failed job.
    """
    started = ensure_utc(now) or utcnow()
    report: dict[str, Any] = {
        "adjustment_version": ADJUSTMENT_VERSION,
        "max_session_return": MAX_SESSION_RETURN,
        "max_reopening_return": MAX_REOPENING_RETURN,
        "session_gap_days": SESSION_GAP_DAYS,
        "provider": PROVIDER_CODE,
        "started_at": started.isoformat(),
        "symbols": {},
        "errors": [],
        "validated": 0,
        "refused": 0,
        "failed": 0,
        "bars_inserted": 0,
        "actions_inserted": 0,
    }
    if not paths:
        raise ValueError("no payload paths given: nothing to ingest")

    for path in paths:
        try:
            result = ingest_payload(engine, read_payload_file(path), now=started)
        except Exception as exc:  # noqa: BLE001 - isolation is the whole point
            # Deliberately broad.  The nineteen other symbols in a run must not
            # depend on this module having anticipated every way one file can
            # be wrong; the exception type is reported so it stays diagnosable.
            log.warning("equity ingest failed for %s: %s", path, exc)
            report["errors"].append(
                {
                    "path": path,
                    "error": type(exc).__name__,
                    "message": str(exc),
                }
            )
            report["failed"] += 1
            continue
        result["path"] = path
        report["symbols"][result["symbol"]] = result
        report["bars_inserted"] += result["bars_inserted"]
        report["actions_inserted"] += result["actions_inserted"]
        report["validated" if result["status"] == STATUS_VALIDATED else "refused"] += 1

    report["finished_at"] = utcnow().isoformat()
    if report["failed"] == len(paths):
        raise EquityIngestFailed(
            f"all {len(paths)} payload(s) failed; nothing was ingested", report
        )
    log.info(
        "equity ingest: %d validated, %d refused, %d failed, %d bars, %d actions",
        report["validated"], report["refused"], report["failed"],
        report["bars_inserted"], report["actions_inserted"],
    )
    return report


__all__ = [
    "ADJUSTMENT_VERSION",
    "STATUS_REFUSED",
    "STATUS_VALIDATED",
    "EquityIngestFailed",
    "ingest_bar_files",
    "ingest_payload",
    "read_payload_file",
    "roster",
]
