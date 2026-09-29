"""Write the market-level TSETMC payloads, each item isolated from the others.

The guarantees are 0028's, carried over:

*Per-item isolation.*  One index, one instrument's money flow, one market's
value series and the snapshot are each parsed and written in their OWN
transaction; a failure is collected into the report, never raised through the
run.  One truncated file costs one item.

*Nothing is invented.*  An index or instrument the registry does not carry is
refused — this never creates a registry row from a payload.

*A stored value is never overwritten.*  TSETMC does not revise a settled
session, so a payload that disagrees with a stored row fails that item and
names the dates.  There is exactly ONE exception, and it is narrow: an index
value restated by an exact power of ten is TSETMC repairing (or re-breaking)
the decimal-shift defect this package corrects for.  It changes no corrected
value, so it is accepted, the stored raw value is kept, and the report counts
it.

*The correction is recomputed, the value is not.*  ``scale_exp`` is derived
from the whole stored series on every ingest, so a new session that shifts the
majority scale re-corrects the history; the raw ``close`` it multiplies is
written once and never touched.
"""
from __future__ import annotations

import json
import logging
import math
import os
from datetime import date, datetime
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import and_, bindparam, func, select, update
from sqlalchemy.engine import Connection, Engine

from ..db import (
    ensure_utc,
    equity_client_flows,
    equity_instruments,
    insert_ignore,
    market_index_checks,
    market_index_values,
    market_indices,
    market_snapshots,
    market_values,
    sector_breadth_snapshots,
    utcnow,
)
from .parse import (
    CLIENT_FIELDS,
    TEHRAN,
    LiveIndex,
    MarketParseError,
    parse_client_types,
    parse_index_history,
    parse_index_live,
    parse_market_overview,
    parse_market_values,
    parse_sector_summary,
)
from .scale import CHECK_VERSION, STATUS_VALIDATED, scale_exponents, verdict

log = logging.getLogger(__name__)

PROVIDER_CODE = "tsetmc_cdn"

# Smaller than any real payload here (the smallest index history, a Farabourse
# sector listed in 2026, is ~20 KB; an overview is ~460 bytes) and larger than
# an empty or error body.
MIN_PLAUSIBLE_BYTES = 200

# Rows per executemany batch.  The first load is ~256,000 index rows; one
# INSERT per row with RETURNING, which is what insert_ignore does, would be a
# quarter of a million round trips.
BULK_BATCH = 2000


# A session is SETTLED once the exchange has closed and published its final
# figures.  Most TSETMC histories only ever carry settled sessions — fetched at
# 11:09 Tehran on 2026-09-29, TEDPIX's newest row was 2026-09-28 — but four
# dormant sector indices (medical instruments, transport equipment, furniture,
# industrial contracting) carried a row dated 2026-09-29 in the middle of that
# session.  Stored, such a row is an unsettled value under a settled contract,
# and the next fetch would have to either accept a revision or fail the index.
# So a row dated on the fetch's own Tehran date is stored only when the fetch
# ran after SETTLED_AFTER_HOUR, well clear of the 12:30 close; anything later
# than that date is never stored.
SETTLED_AFTER_HOUR = 15


def settled_cutoff(now: datetime) -> date:
    """The first trade date that is NOT yet settled at ``now``.  Pure."""
    local = ensure_utc(now).astimezone(TEHRAN)
    if local.hour >= SETTLED_AFTER_HOUR:
        return date.fromordinal(local.date().toordinal() + 1)
    return local.date()


class MarketIngestFailed(RuntimeError):
    """Every item in the pass failed.  Carries the full report."""

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


# --- helpers -----------------------------------------------------------------


def _close_enough(left: float, right: float) -> bool:
    """Equality for a value that has round-tripped through NUMERIC.  Same
    tolerance and reasoning as app.equities.ingest._close_enough."""
    scale = max(abs(left), abs(right), 1.0)
    return abs(left - right) <= 1e-12 * scale


def _power_of_ten_apart(stored: float, served: float) -> bool:
    """True when two values differ by exactly 10^k, k in 1..3 either way."""
    if stored <= 0 or served <= 0:
        return False
    k = round(math.log10(served / stored))
    if k == 0 or abs(k) > 3:
        return False
    return _close_enough(served, stored * 10 ** k)


def _bulk_insert_ignore(conn: Connection, table, rows: Sequence[dict]) -> None:
    """executemany INSERT ... ON CONFLICT DO NOTHING, in batches.

    The inserted count is taken by the caller from a COUNT before and after,
    which is exact and costs one indexed query, rather than from rowcount —
    psycopg reports -1 for ON CONFLICT DO NOTHING under executemany.
    """
    if not rows:
        return
    dialect = conn.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    stmt = dialect_insert(table).on_conflict_do_nothing()
    for start in range(0, len(rows), BULK_BATCH):
        conn.execute(stmt, list(rows[start : start + BULK_BATCH]))


def read_payload_file(path: str) -> Any:
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no payload at {path}")
    size = os.path.getsize(path)
    if size < MIN_PLAUSIBLE_BYTES:
        raise MarketParseError(
            f"{os.path.basename(path)} is {size} bytes — an error page or a truncated "
            "transfer, not a TSETMC payload. Refusing to parse it."
        )
    with open(path, "r", encoding="utf-8") as handle:
        try:
            return json.load(handle)
        except json.JSONDecodeError as exc:
            raise MarketParseError(f"{os.path.basename(path)} is not valid JSON: {exc}") from exc


# --- rosters -----------------------------------------------------------------


def index_roster(bind: Engine, include_disabled: bool = False) -> list[dict[str, Any]]:
    """The indices this deployment ingests, for scripts/tsetmc_fetch.py."""
    t = market_indices
    stmt = select(
        t.c.ins_code, t.c.name_fa, t.c.name_en, t.c.market, t.c.kind,
        t.c.sector_code, t.c.first_date, t.c.last_date, t.c.value_count, t.c.enabled,
    ).order_by(t.c.display_order, t.c.ins_code)
    if not include_disabled:
        stmt = stmt.where(t.c.enabled.is_(True))
    with bind.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        {
            **dict(r),
            "first_date": r["first_date"].isoformat() if r["first_date"] else None,
            "last_date": r["last_date"].isoformat() if r["last_date"] else None,
        }
        for r in rows
    ]


# --- one index ---------------------------------------------------------------


def ingest_index_history(
    engine: Engine,
    payload: Any,
    live: Mapping[str, LiveIndex],
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ingest one index's history, recompute its correction, record its verdict."""
    collected_at = ensure_utc(now) or utcnow()
    history = parse_index_history(payload)
    code = history.ins_code
    t = market_index_values
    cutoff = settled_cutoff(collected_at)
    settled = [v for v in history.values if v.trade_date < cutoff]
    unsettled = len(history.values) - len(settled)
    if not settled:
        raise MarketParseError(f"index {code}: no settled session in the payload")

    with engine.begin() as conn:
        registry = conn.execute(
            select(market_indices.c.name_fa, market_indices.c.enabled).where(
                market_indices.c.ins_code == code
            )
        ).first()
        if registry is None:
            raise MarketParseError(
                f"index {code} is not in market_indices. The registry is data "
                "(migration 0029 seeds it): add the index with an INSERT, then ingest."
            )

        stored = {
            r.trade_date: r
            for r in conn.execute(
                select(t.c.trade_date, t.c.close, t.c.scale_exp).where(t.c.ins_code == code)
            )
        }
        conflicts: list[str] = []
        restated_scale = 0
        pending: list[dict[str, Any]] = []
        for v in settled:
            existing = stored.get(v.trade_date)
            if existing is None:
                pending.append(
                    {
                        "ins_code": code,
                        "trade_date": v.trade_date,
                        "close": v.close,
                        "low": v.low,
                        "high": v.high,
                        "scale_exp": 0,
                        "collected_at": collected_at,
                    }
                )
                continue
            old = float(existing.close)
            if _close_enough(old, v.close):
                continue
            if _power_of_ten_apart(old, v.close):
                restated_scale += 1
                continue
            conflicts.append(
                f"{v.trade_date.isoformat()}: stored {old!r}, payload {v.close!r}"
            )
        if conflicts:
            more = "" if len(conflicts) <= 3 else f" and {len(conflicts) - 3} more"
            raise MarketParseError(
                f"index {code}: the payload contradicts {len(conflicts)} stored value(s) "
                f"({'; '.join(conflicts[:3])}{more}). A settled index value is not "
                "revised, and the only restatement accepted is an exact power of ten. "
                "If TSETMC genuinely restated this index, delete its rows from "
                "market_index_values and re-run the fetch; nothing is overwritten here."
            )

        # The merged series — every stored value plus every new one — is what
        # the correction is computed over.  Stored raw closes are used as
        # stored, including any the payload restated by a power of ten.
        merged: dict[date, float] = {d: float(r.close) for d, r in stored.items()}
        for row in pending:
            merged[row["trade_date"]] = row["close"]
        dates = sorted(merged)
        closes = [merged[d] for d in dates]
        exps, breaks = scale_exponents(closes)
        exp_of = dict(zip(dates, exps))

        for row in pending:
            row["scale_exp"] = exp_of[row["trade_date"]]
        before = conn.execute(
            select(func.count()).select_from(t).where(t.c.ins_code == code)
        ).scalar_one()
        _bulk_insert_ignore(conn, t, pending)
        after = conn.execute(
            select(func.count()).select_from(t).where(t.c.ins_code == code)
        ).scalar_one()

        rescaled_updates = [
            {"k": code, "d": d, "e": exp_of[d]}
            for d, r in stored.items()
            if int(r.scale_exp) != exp_of[d]
        ]
        if rescaled_updates:
            conn.execute(
                update(t)
                .where(and_(t.c.ins_code == bindparam("k"), t.c.trade_date == bindparam("d")))
                .values(scale_exp=bindparam("e")),
                rescaled_updates,
            )

        live_index = live.get(code)
        result = verdict(
            dates,
            closes,
            exps,
            breaks,
            history.dropped_nonpositive,
            live_index.value if live_index else None,
        )
        _write_index_verdict(conn, code, result, live_index is not None, collected_at)
        conn.execute(
            update(market_indices)
            .where(market_indices.c.ins_code == code)
            .values(
                first_date=dates[0],
                last_date=dates[-1],
                value_count=int(after),
                updated_at=collected_at,
            )
        )

    report: dict[str, Any] = {
        "ins_code": code,
        "name_fa": registry.name_fa,
        "status": result.status,
        "values_total": len(history.values),
        "values_inserted": int(after) - int(before),
        "values_existing": len(settled) - len(pending),
        "unsettled_skipped": unsettled,
        "restated_by_power_of_ten": restated_scale,
        "dropped_nonpositive": history.dropped_nonpositive,
        "scale_breaks": result.scale_breaks,
        "rows_rescaled": result.rows_rescaled,
        "rescaled_existing_rows": len(rescaled_updates),
        "first_date": dates[0].isoformat(),
        "last_date": dates[-1].isoformat(),
        "largest_move": round(result.largest_move, 6) if result.largest_move is not None else None,
        "largest_move_date": (
            result.largest_move_date.isoformat() if result.largest_move_date else None
        ),
        "live_ratio": round(result.live_ratio, 6) if result.live_ratio is not None else None,
    }
    if result.refusal_reason:
        report["refusal_reason"] = result.refusal_reason
    return report


def _write_index_verdict(
    conn: Connection, code: str, result, live_checked: bool, computed_at: datetime
) -> None:
    values = {
        "ins_code": code,
        "check_version": CHECK_VERSION,
        "status": result.status,
        "values_total": result.values_total,
        "dropped_nonpositive": result.dropped_nonpositive,
        "scale_breaks": result.scale_breaks,
        "rows_rescaled": result.rows_rescaled,
        "first_break": result.first_break,
        "last_break": result.last_break,
        "largest_move": result.largest_move,
        "largest_move_date": result.largest_move_date,
        "live_value": result.live_value,
        "live_ratio": result.live_ratio,
        "live_checked_at": computed_at if live_checked else None,
        "refusal_reason": result.refusal_reason,
        "computed_at": computed_at,
    }
    existing = conn.execute(
        select(market_index_checks.c.id).where(
            and_(
                market_index_checks.c.ins_code == code,
                market_index_checks.c.check_version == CHECK_VERSION,
            )
        )
    ).scalar()
    if existing is None:
        insert_ignore(conn, market_index_checks, [values])
        return
    # A verdict describes the series as it stands NOW, so it is the one row
    # that is updated rather than appended — a stale 'validated' beside a
    # fresh refusal would be worse than none.
    conn.execute(
        update(market_index_checks)
        .where(market_index_checks.c.id == existing)
        .values(**{k: v for k, v in values.items() if k != "ins_code"})
    )


# --- market value ------------------------------------------------------------


def ingest_market_values(
    engine: Engine, payload: Any, market: str, now: Optional[datetime] = None
) -> dict[str, Any]:
    collected_at = ensure_utc(now) or utcnow()
    parsed = parse_market_values(payload, market)
    t = market_values
    cutoff = settled_cutoff(collected_at)
    values = [(d, v) for d, v in parsed.values if d < cutoff]
    unsettled = len(parsed.values) - len(values)
    if not values:
        raise MarketParseError(f"{market} market value: no settled session in the payload")

    # The decimal-shift defect is corrected for INDICES because it was measured
    # there.  It was not measured here (1,615 bourse and 1,610 Farabourse rows,
    # largest step -10.0% and +28.2%), so there is no correction machinery for
    # it — and if it ever appears, the series is refused rather than stored
    # with a tenfold cliff in it.
    for (d0, v0), (d1, v1) in zip(values, values[1:]):
        r = v1 / v0
        if r >= 10 ** 0.9 or r <= 10 ** -0.9:
            raise MarketParseError(
                f"{market} market value moves x{r:.3f} from {d0} to {d1}; a total market "
                "value cannot change tenfold in a session, and this series has no "
                "decimal-shift correction. Refusing the whole series."
            )

    with engine.begin() as conn:
        stored = {
            r.trade_date: float(r.market_cap)
            for r in conn.execute(
                select(t.c.trade_date, t.c.market_cap).where(t.c.market == market)
            )
        }
        conflicts = [
            f"{d.isoformat()}: stored {stored[d]!r}, payload {v!r}"
            for d, v in values
            if d in stored and not _close_enough(stored[d], v)
        ]
        if conflicts:
            raise MarketParseError(
                f"{market} market value: the payload contradicts {len(conflicts)} stored "
                f"session(s) ({'; '.join(conflicts[:3])}). Nothing is overwritten; delete "
                "the rows deliberately if TSETMC genuinely restated them."
            )
        pending = [
            {"market": market, "trade_date": d, "market_cap": v, "collected_at": collected_at}
            for d, v in values
            if d not in stored
        ]
        before = len(stored)
        _bulk_insert_ignore(conn, t, pending)
        after = conn.execute(
            select(func.count()).select_from(t).where(t.c.market == market)
        ).scalar_one()
    return {
        "market": market,
        "values_total": len(parsed.values),
        "values_inserted": int(after) - before,
        "unsettled_skipped": unsettled,
        "dropped_nonpositive": parsed.dropped_nonpositive,
        "first_date": values[0][0].isoformat(),
        "last_date": values[-1][0].isoformat(),
    }


# --- money flow --------------------------------------------------------------


def ingest_client_flows(
    engine: Engine, payload: Any, now: Optional[datetime] = None
) -> dict[str, Any]:
    collected_at = ensure_utc(now) or utcnow()
    parsed = parse_client_types(payload)
    code = parsed.ins_code
    t = equity_client_flows
    columns = [c for c, _, _ in CLIENT_FIELDS]
    cutoff = settled_cutoff(collected_at)
    rows = [r for r in parsed.rows if r["trade_date"] < cutoff]
    unsettled = len(parsed.rows) - len(rows)
    if not rows:
        raise MarketParseError(f"client types of {code}: no settled session in the payload")

    with engine.begin() as conn:
        instrument = conn.execute(
            select(equity_instruments.c.symbol_fa).where(
                equity_instruments.c.ins_code == code
            )
        ).first()
        if instrument is None:
            raise MarketParseError(
                f"insCode {code} is not in equity_instruments; money flow is stored only "
                "for the roster. Add the instrument with an INSERT, then ingest."
            )
        stored = {
            r["trade_date"]: r
            for r in conn.execute(
                select(t.c.trade_date, *[t.c[c] for c in columns]).where(t.c.ins_code == code)
            ).mappings()
        }
        conflicts: list[str] = []
        pending: list[dict[str, Any]] = []
        for row in rows:
            existing = stored.get(row["trade_date"])
            if existing is None:
                pending.append({"ins_code": code, **row, "collected_at": collected_at})
                continue
            differing = [
                c for c in columns if not _close_enough(float(existing[c]), float(row[c]))
            ]
            if differing:
                conflicts.append(f"{row['trade_date'].isoformat()} ({', '.join(differing[:3])})")
        if conflicts:
            raise MarketParseError(
                f"{instrument.symbol_fa} ({code}): the client-type payload contradicts "
                f"{len(conflicts)} stored session(s): {'; '.join(conflicts[:3])}. Nothing is "
                "overwritten; delete the rows deliberately if TSETMC genuinely restated them."
            )
        before = len(stored)
        _bulk_insert_ignore(conn, t, pending)
        after = conn.execute(
            select(func.count()).select_from(t).where(t.c.ins_code == code)
        ).scalar_one()
    return {
        "ins_code": code,
        "symbol": instrument.symbol_fa,
        "sessions_total": len(parsed.rows),
        "sessions_inserted": int(after) - before,
        "unsettled_skipped": unsettled,
        "first_date": rows[0]["trade_date"].isoformat(),
        "last_date": rows[-1]["trade_date"].isoformat(),
    }


# --- snapshot ----------------------------------------------------------------


def ingest_snapshot(
    engine: Engine,
    overviews: Mapping[str, Any],
    sector_summary: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """The market overview(s) and the sector breadth, as they stood at fetch time."""
    collected_at = ensure_utc(now) or utcnow()
    parsed = {m: parse_market_overview(p, m) for m, p in overviews.items()}
    sectors = parse_sector_summary(sector_summary) if sector_summary is not None else None
    if sectors is not None and "bourse" not in parsed:
        raise MarketParseError(
            "the sector summary carries no time of its own and is stamped with the "
            "bourse overview from the same run; none was posted, so it has no time at all"
        )
    report: dict[str, Any] = {}
    with engine.begin() as conn:
        for market, row in parsed.items():
            inserted = insert_ignore(
                conn, market_snapshots, [{**row, "collected_at": collected_at}]
            )
            report[market] = {
                "activity_at": row["activity_at"].isoformat(),
                "stored": "inserted" if inserted else "existing",
            }
        if sectors is not None:
            stamp = parsed["bourse"]["activity_at"]
            inserted = insert_ignore(
                conn,
                sector_breadth_snapshots,
                [{**s, "activity_at": stamp, "collected_at": collected_at} for s in sectors],
            )
            report["sectors"] = {"rows": len(sectors), "inserted": inserted}
    return report


# --- the run -----------------------------------------------------------------


def ingest_market_files(
    engine: Engine, request: Mapping[str, Any], now: Optional[datetime] = None
) -> dict[str, Any]:
    """Ingest one fetch run's market-level payloads, every item isolated.

    ``request`` names files already inside this container::

        {"index_live":      [path, ...],        # GetIndexB1LastAll, flows 1 and 2
         "index_histories": [path, ...],        # GetIndexB2History, one per index
         "market_values":   {"bourse": path, "farabourse": path},
         "client_flows":    [path, ...],        # GetClientTypeHistory, one per symbol
         "overviews":       {"bourse": path, "farabourse": path},
         "sector_summary":  path}

    Raises :class:`MarketIngestFailed` only when EVERY item failed; a partial
    failure is a success with an ``errors`` list, matching the bars ingest.
    """
    started = ensure_utc(now) or utcnow()
    report: dict[str, Any] = {
        "check_version": CHECK_VERSION,
        "provider": PROVIDER_CODE,
        "started_at": started.isoformat(),
        "indices": {},
        "market_values": {},
        "client_flows": {},
        "snapshot": {},
        "errors": [],
        "succeeded": 0,
        "failed": 0,
        "validated": 0,
        "refused": 0,
    }

    def failed(kind: str, path: str, exc: Exception) -> None:
        log.warning("market ingest failed for %s %s: %s", kind, path, exc)
        report["errors"].append(
            {"kind": kind, "path": path, "error": type(exc).__name__, "message": str(exc)}
        )
        report["failed"] += 1

    live: dict[str, LiveIndex] = {}
    for path in request.get("index_live") or []:
        try:
            live.update(parse_index_live(read_payload_file(path)))
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            # Losing the live figures costs the cross-check, not the ingest: the
            # verdict records that no live value was compared.
            failed("index_live", path, exc)

    for path in request.get("index_histories") or []:
        try:
            result = ingest_index_history(engine, read_payload_file(path), live, now=started)
        except Exception as exc:  # noqa: BLE001
            failed("index_history", path, exc)
            continue
        report["indices"][result["ins_code"]] = result
        report["succeeded"] += 1
        report["validated" if result["status"] == STATUS_VALIDATED else "refused"] += 1

    for market, path in (request.get("market_values") or {}).items():
        try:
            result = ingest_market_values(engine, read_payload_file(path), market, now=started)
        except Exception as exc:  # noqa: BLE001
            failed("market_values", path, exc)
            continue
        report["market_values"][market] = result
        report["succeeded"] += 1

    for path in request.get("client_flows") or []:
        try:
            result = ingest_client_flows(engine, read_payload_file(path), now=started)
        except Exception as exc:  # noqa: BLE001
            failed("client_flows", path, exc)
            continue
        report["client_flows"][result["symbol"]] = result
        report["succeeded"] += 1

    overview_paths = request.get("overviews") or {}
    sector_path = request.get("sector_summary")
    if overview_paths or sector_path:
        try:
            report["snapshot"] = ingest_snapshot(
                engine,
                {m: read_payload_file(p) for m, p in overview_paths.items()},
                read_payload_file(sector_path) if sector_path else None,
                now=started,
            )
            report["succeeded"] += 1
        except Exception as exc:  # noqa: BLE001
            failed("snapshot", ",".join([*overview_paths.values(), sector_path or ""]), exc)

    report["finished_at"] = utcnow().isoformat()
    if report["succeeded"] == 0:
        if report["failed"] == 0:
            raise ValueError("the request named no payloads: nothing to ingest")
        raise MarketIngestFailed(
            f"all {report['failed']} item(s) failed; nothing was ingested", report
        )
    log.info(
        "market ingest: %d ok (%d indices validated, %d refused), %d failed",
        report["succeeded"], report["validated"], report["refused"], report["failed"],
    )
    return report


__all__ = [
    "SETTLED_AFTER_HOUR",
    "MarketIngestFailed",
    "settled_cutoff",
    "index_roster",
    "ingest_client_flows",
    "ingest_index_history",
    "ingest_market_files",
    "ingest_market_values",
    "ingest_snapshot",
    "read_payload_file",
]
