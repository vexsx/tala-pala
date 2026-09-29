"""The whole market, share by share (migration 0030): the universe mirror,
every share's money flow, and the exchange's own session rows.

WHAT ARRIVES, AND HOW
---------------------
:mod:`scripts.tsetmc_fetch` downloads, where TSETMC answers::

    market-watch.json          GetMarketWatch — every instrument; the shares are
                               picked out by insID (parse.SHARE_MARKETS)
    static-data.json           GetStaticData — TSETMC's industrial-group names
    flows/flow-<insCode>.json  GetClientTypeHistory, TRIMMED to what is new plus
                               an overlap of what is stored
    days/day-<yyyymmdd>.json   GetInstrmentsHistoryInDay, one per session not
                               yet stored (plus the newest two that are)
    manifest.json              names all of the above

copies the directory in, and posts ``POST /internal/bourse/shares/ingest``
once per PART — ``universe``, then ``flows`` and ``sessions`` in offset/limit
chunks.  Chunks because the first run is ~1,150 flow files and ~305 day
files: neither fits one synchronous call under busybox wget's read timeout,
and ~1,150 paths would not fit the ``--post-data`` argv either (~81 KB in one
``sh -c`` argument against Linux's 128 KiB MAX_ARG_STRLEN).  The body names
the manifest; the manifest names the files; every name is checked against a
fixed pattern and resolved inside the manifest's own directory, so no request
can point this service at a file outside the run it describes.

THE GUARANTEES, CARRIED OVER FROM :mod:`app.bourse.ingest`
-----------------------------------------------------------
*Per-item isolation.*  One share's flows, one session's day file: each is its
own transaction and its own entry in the report.

*The universe is a mirror.*  ``market_shares`` is the one registry an ingest
inserts into, because it mirrors TSETMC's listing rather than curating one.
A share TSETMC stops listing is marked ``listed = FALSE`` and never deleted —
its history is still true, and the foreign keys into it RESTRICT — and a
market watch that would delist more than half of what is listed is refused
as a truncated response rather than believed.

*A stored session is never overwritten.*  A day file that disagrees with a
stored row fails THAT DAY, names the shares, and writes nothing for it.

*The day file's date is checked, not assumed.*  TSETMC's file carries no date
(``dEven`` is 0 on every row), so the date is the one the fetch requested and
put in the file name.  Before believing it, the shares' values are compared
with the flow totals already stored for that date — measured equal on every
instrument compared — and a file most of whose shares disagree is refused as
describing some other session.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional

from sqlalchemy import and_, bindparam, func, select, update
from sqlalchemy.engine import Connection, Engine

from ..db import (
    ensure_utc,
    equity_client_flows,
    equity_instruments,
    market_sectors,
    market_session_files,
    market_share_sessions,
    market_shares,
    utcnow,
)
from ..equities.adjust import BarParseError, parse_deven
from .ingest import (
    PROVIDER_CODE,
    MarketIngestFailed,
    _bulk_insert_ignore,
    _close_enough,
    ingest_client_flows,
    read_payload_file,
    settled_cutoff,
)
from .parse import (
    MarketParseError,
    parse_day_file,
    parse_market_watch,
    parse_session_row,
    parse_static_sectors,
)

log = logging.getLogger(__name__)

# Non-roster shares ship flows and sessions only from 1 Farvardin 1404.  The
# fetch enforces it (it is the thing that would spend the disk); it is served
# by /internal/bourse/shares/state so the fetch and the server agree on it.
# 0030's header has the row-count arithmetic behind the date.
FLOW_FLOOR = date(2025, 3, 21)

# A market watch that would delist more than this fraction of the listed
# shares is a truncated response, not a market-wide delisting.  The guard
# needs at least DELIST_GUARD_MIN delistings to fire, so a small universe (a
# test, a fresh database) is never refused for ordinary churn.
MAX_DELIST_FRACTION = 0.5
DELIST_GUARD_MIN = 50

# The day-file date check.  At least DATE_CHECK_MIN shares must have a flow
# row to compare against before a verdict is reached, and a share "agrees"
# when its session value is within DATE_CHECK_TOLERANCE of its flow total —
# the read side's own bar tolerance.  Measured on 2026-09-28: eight of eight
# instruments equal to the rial.
DATE_CHECK_MIN = 5
DATE_CHECK_TOLERANCE = 0.01

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"
MARKET_WATCH_NAME = "market-watch.json"
STATIC_DATA_NAME = "static-data.json"
FLOW_FILE_RE = re.compile(r"\Aflow-([0-9]{1,20})\.json\Z")
DAY_FILE_RE = re.compile(r"\Aday-([0-9]{8})\.json\Z")
PARTS = ("universe", "flows", "sessions")
MAX_CHUNK = 500

# Rows per IN (...) list when a statement names many shares.
_IN_CHUNK = 500


class SessionContradiction(MarketParseError):
    """A day file disagrees with stored session rows; the whole day fails."""


class WrongSessionFile(MarketParseError):
    """A day file's values do not match the date in its name."""


def _chunks(items: list, size: int = _IN_CHUNK) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --- state, for the fetch ----------------------------------------------------


def share_state(bind: Engine) -> dict[str, Any]:
    """What the fetch needs to ship only what is new.

    Per share: its stored flow coverage and whether it is on the roster (an
    enabled ``equity_instruments`` row), because the roster keeps its full
    history while every other share is floored at FLOW_FLOOR.  Plus the
    sessions whose day file is already ingested, so the fetch asks only for
    the others.
    """
    s = market_shares
    with bind.connect() as conn:
        roster = set(
            conn.execute(
                select(equity_instruments.c.ins_code).where(equity_instruments.c.enabled.is_(True))
            ).scalars()
        )
        rows = conn.execute(
            select(
                s.c.ins_code, s.c.symbol_fa, s.c.listed, s.c.flow_first_date,
                s.c.flow_last_date, s.c.flow_count, s.c.session_last_date, s.c.session_count,
            ).order_by(s.c.ins_code)
        ).mappings().all()
        sessions = conn.execute(
            select(market_session_files.c.trade_date).order_by(market_session_files.c.trade_date)
        ).scalars().all()

    def iso(value: Optional[date]) -> Optional[str]:
        return value.isoformat() if value else None

    items = [
        {
            "ins_code": r["ins_code"],
            "symbol_fa": r["symbol_fa"],
            "listed": bool(r["listed"]),
            "roster": r["ins_code"] in roster,
            "flow_first_date": iso(r["flow_first_date"]),
            "flow_last_date": iso(r["flow_last_date"]),
            "flow_count": int(r["flow_count"]),
            "session_last_date": iso(r["session_last_date"]),
            "session_count": int(r["session_count"]),
        }
        for r in rows
    ]
    return {
        "floor": FLOW_FLOOR.isoformat(),
        "items": items,
        "count": len(items),
        "session_dates": [d.isoformat() for d in sessions],
    }


# --- the universe --------------------------------------------------------------


def _refresh_sector_names(
    conn: Connection, names: dict[str, str], at: datetime
) -> dict[str, Any]:
    """TSETMC's current names onto the seeded vocabulary.  A code TSETMC lists
    and 0030 did not is ADDED with an empty English name — the translation is
    this system's to write, and an ingest does not invent one."""
    stored = {
        r.sector_code: r.name_fa
        for r in conn.execute(select(market_sectors.c.sector_code, market_sectors.c.name_fa))
    }
    added: list[str] = []
    renamed: list[str] = []
    for code, name in sorted(names.items()):
        if not name:
            continue  # never blank a stored name with an empty one
        if code not in stored:
            conn.execute(
                market_sectors.insert().values(
                    sector_code=code, name_fa=name, name_en="", updated_at=at
                )
            )
            added.append(code)
        elif stored[code] != name:
            conn.execute(
                update(market_sectors)
                .where(market_sectors.c.sector_code == code)
                .values(name_fa=name, updated_at=at)
            )
            renamed.append(code)
    return {"listed": len(names), "added": added, "renamed": renamed}


def ingest_universe(
    engine: Engine,
    market_watch: Any,
    static_data: Any = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Mirror the market watch's shares into ``market_shares``.

    New shares are inserted; a listed share takes TSETMC's latest symbol,
    name, sector, board and insID; a share absent from this market watch is
    marked ``listed = FALSE``.  Nothing is deleted.  ``static_data``, when
    given, refreshes ``market_sectors.name_fa`` first, so the unknown-sector
    count is taken against the vocabulary as it now stands.
    """
    at = ensure_utc(now) or utcnow()
    watch = parse_market_watch(market_watch)
    names = parse_static_sectors(static_data) if static_data is not None else None
    s = market_shares
    fields = ("ins_id", "symbol_fa", "name_fa", "market", "board", "company_code", "sector_code")

    with engine.begin() as conn:
        sectors = _refresh_sector_names(conn, names, at) if names is not None else None
        existing = {
            r.ins_code: r
            for r in conn.execute(select(s.c.ins_code, s.c.listed, *[s.c[f] for f in fields]))
        }
        served = {share.ins_code for share in watch.shares}
        listed_before = [code for code, r in existing.items() if r.listed]
        delisted = sorted(code for code in listed_before if code not in served)
        if (
            len(delisted) >= DELIST_GUARD_MIN
            and len(delisted) > MAX_DELIST_FRACTION * len(listed_before)
        ):
            raise MarketParseError(
                f"this market watch would delist {len(delisted)} of the {len(listed_before)} "
                f"listed shares (it lists {len(watch.shares)} shares in {watch.rows_total} "
                "rows). That is a truncated or partial response, not the market; nothing "
                "was changed."
            )

        inserts: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        seen_only: list[str] = []
        renamed: list[dict[str, str]] = []
        resectored: list[dict[str, str]] = []
        relisted = 0
        for share in watch.shares:
            values = {f: getattr(share, f) for f in fields}
            old = existing.get(share.ins_code)
            if old is None:
                inserts.append(
                    {
                        "ins_code": share.ins_code,
                        **values,
                        "listed": True,
                        "first_seen_at": at,
                        "last_seen_at": at,
                        "updated_at": at,
                    }
                )
                continue
            if old.symbol_fa != share.symbol_fa or old.name_fa != share.name_fa:
                renamed.append(
                    {"ins_code": share.ins_code, "from": old.symbol_fa, "to": share.symbol_fa}
                )
            if old.sector_code != share.sector_code:
                resectored.append(
                    {"ins_code": share.ins_code, "from": old.sector_code, "to": share.sector_code}
                )
            if not old.listed:
                relisted += 1
            if old.listed and all(getattr(old, f) == values[f] for f in fields):
                seen_only.append(share.ins_code)
            else:
                changes.append({"k": share.ins_code, **{f"v_{f}": values[f] for f in fields}})

        if inserts:
            _bulk_insert_ignore(conn, s, inserts)
        if changes:
            conn.execute(
                update(s)
                .where(s.c.ins_code == bindparam("k"))
                .values(
                    **{f: bindparam(f"v_{f}") for f in fields},
                    listed=True,
                    last_seen_at=at,
                    updated_at=at,
                ),
                changes,
            )
        for chunk in _chunks(seen_only):
            conn.execute(update(s).where(s.c.ins_code.in_(chunk)).values(last_seen_at=at))
        for chunk in _chunks(delisted):
            conn.execute(
                update(s).where(s.c.ins_code.in_(chunk)).values(listed=False, updated_at=at)
            )

        known = set(conn.execute(select(market_sectors.c.sector_code)).scalars())
    unknown = Counter(
        share.sector_code or "(none)"
        for share in watch.shares
        if share.sector_code not in known
    )
    return {
        "rows_total": watch.rows_total,
        "shares": len(watch.shares),
        "markets": dict(sorted(Counter(sh.market for sh in watch.shares).items())),
        "boards": dict(sorted(Counter(sh.board for sh in watch.shares).items())),
        "inserted": len(inserts),
        "renamed": len(renamed),
        "resectored": len(resectored),
        "relisted": relisted,
        "delisted": len(delisted),
        # Examples, not the whole list: a first run inserts ~1,150 rows (and
        # "relists" the twenty roster rows 0030 seeded unlisted), and the
        # counts above are what a report needs.
        "renamed_examples": renamed[:10],
        "resectored_examples": resectored[:10],
        "delisted_codes": delisted[:50],
        "unknown_sector": {"count": sum(unknown.values()), "codes": dict(sorted(unknown.items()))},
        "sectors": sectors,
    }


# --- one session ---------------------------------------------------------------


def _date_check(
    conn: Connection, trade_date: date, sessions: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Compare each share's session value with its stored flow total that day."""
    f = equity_client_flows
    totals: dict[str, float] = {}
    for chunk in _chunks(sorted(sessions)):
        for code, buy_i, buy_n in conn.execute(
            select(f.c.ins_code, f.c.buy_i_value, f.c.buy_n_value).where(
                and_(f.c.trade_date == trade_date, f.c.ins_code.in_(chunk))
            )
        ):
            totals[code] = float(buy_i) + float(buy_n)
    agreed = 0
    for code, total in totals.items():
        value = sessions[code]["value"]
        if abs(value - total) <= DATE_CHECK_TOLERANCE * max(abs(value), abs(total)):
            agreed += 1
    compared = len(totals)
    status = "unchecked" if compared < DATE_CHECK_MIN else "agreed"
    if compared >= DATE_CHECK_MIN and agreed * 2 < compared:
        raise WrongSessionFile(
            f"day file for {trade_date.isoformat()}: only {agreed} of the {compared} shares "
            "with stored money flow that day agree with it on traded value. TSETMC's file "
            "carries no date of its own, so a file that disagrees with the session named "
            "in it describes some other session; nothing was written for this date."
        )
    return {
        "against": "equity_client_flows",
        "compared": compared,
        "agreed": agreed,
        "disagreed": compared - agreed,
        "status": status,
    }


def ingest_day_file(
    engine: Engine,
    payload: Any,
    trade_date: date,
    now: Optional[datetime] = None,
    universe: Optional[set[str]] = None,
) -> tuple[dict[str, Any], set[str]]:
    """Ingest one session's day file: the rows of shares ``market_shares``
    carries, compared with what is stored and never overwritten.

    Returns the report and the insCodes whose rows were inserted (their
    coverage is refreshed once per call, not once per day).
    """
    at = ensure_utc(now) or utcnow()
    if trade_date >= settled_cutoff(at):
        raise MarketParseError(
            f"{trade_date.isoformat()} is not a settled session at {at.isoformat()}; a day "
            "file for it would be a snapshot of an open market"
        )
    rows = parse_day_file(payload)
    t = market_share_sessions
    columns = ("close", "last_trade", "price_yesterday", "value", "volume", "trades")

    with engine.begin() as conn:
        if universe is None:
            universe = set(conn.execute(select(market_shares.c.ins_code)).scalars())
        sessions: dict[str, dict[str, Any]] = {}
        invalid: list[dict[str, str]] = []
        for code, raw in rows.items():
            if code not in universe:
                continue
            try:
                sessions[code] = parse_session_row(raw)
            except MarketParseError as exc:
                invalid.append({"ins_code": code, "reason": str(exc)})
        date_check = _date_check(conn, trade_date, sessions)

        stored = {
            r["ins_code"]: r
            for r in conn.execute(
                select(t.c.ins_code, *[t.c[c] for c in columns]).where(t.c.trade_date == trade_date)
            ).mappings()
        }
        conflicts: list[str] = []
        pending: list[dict[str, Any]] = []
        for code, row in sessions.items():
            old = stored.get(code)
            if old is None:
                pending.append(
                    {"ins_code": code, "trade_date": trade_date, **row, "collected_at": at}
                )
                continue
            differing = [
                c
                for c in columns
                if (old[c] is None) != (row[c] is None)
                or (row[c] is not None and not _close_enough(float(old[c]), float(row[c])))
            ]
            if differing:
                conflicts.append(f"{code} ({', '.join(differing[:3])})")
        if conflicts:
            raise SessionContradiction(
                f"day file for {trade_date.isoformat()} contradicts {len(conflicts)} stored "
                f"session row(s): {'; '.join(conflicts[:3])}. A settled session is not "
                "revised; nothing was written for this date. Delete the rows deliberately "
                "if TSETMC genuinely restated them."
            )
        before = len(stored)
        _bulk_insert_ignore(conn, t, pending)
        after = conn.execute(
            select(func.count()).select_from(t).where(t.c.trade_date == trade_date)
        ).scalar_one()
        inserted = int(after) - before

        record = {"rows_total": len(rows), "rows_shares": len(sessions)}
        old_file = conn.execute(
            select(market_session_files).where(market_session_files.c.trade_date == trade_date)
        ).mappings().first()
        if old_file is None:
            conn.execute(
                market_session_files.insert().values(trade_date=trade_date, ingested_at=at, **record)
            )
            file_state = "inserted"
        elif inserted or any(old_file[k] != v for k, v in record.items()):
            conn.execute(
                update(market_session_files)
                .where(market_session_files.c.trade_date == trade_date)
                .values(ingested_at=at, **record)
            )
            file_state = "updated"
        else:
            file_state = "unchanged"

    report = {
        "trade_date": trade_date.isoformat(),
        "rows_total": len(rows),
        "rows_shares": len(sessions),
        "rows_inserted": inserted,
        "rows_existing": len(sessions) - len(pending),
        "rows_invalid": len(invalid),
        "invalid_examples": invalid[:5],
        "date_check": date_check,
        "file_record": file_state,
    }
    return report, {p["ins_code"] for p in pending}


def refresh_session_coverage(
    engine: Engine, codes: Iterable[str], now: Optional[datetime] = None
) -> int:
    """Recompute session_last_date/session_count from the table for ``codes``."""
    at = ensure_utc(now) or utcnow()
    t = market_share_sessions
    s = market_shares
    last = select(func.max(t.c.trade_date)).where(t.c.ins_code == s.c.ins_code).scalar_subquery()
    count = (
        select(func.count()).select_from(t).where(t.c.ins_code == s.c.ins_code).scalar_subquery()
    )
    ordered = sorted(set(codes))
    with engine.begin() as conn:
        for chunk in _chunks(ordered):
            conn.execute(
                update(s)
                .where(s.c.ins_code.in_(chunk))
                .values(session_last_date=last, session_count=count, updated_at=at)
            )
    return len(ordered)


# --- the manifest ------------------------------------------------------------


@dataclass(frozen=True)
class ShareManifest:
    base: str
    market_watch: Optional[str]
    static_data: Optional[str]
    flows: list[tuple[str, str]]   # (insCode, absolute path)
    days: list[tuple[date, str]]   # (session, absolute path)


def load_manifest(path: str) -> ShareManifest:
    """Read a fetch run's manifest, refusing any name that could leave its
    directory.

    Every name must match a fixed pattern — ``flow-<digits>.json``,
    ``day-<8 digits>.json`` or one of two fixed file names — so no separator,
    ``..`` or absolute path can appear in one, and each resolved path is then
    checked to be inside the manifest's own directory, which also refuses a
    symlink pointing out of it.  Raises ValueError: a bad manifest is a
    statement about the call, not about TSETMC.
    """
    if os.path.basename(path) != MANIFEST_NAME:
        raise ValueError(f"the manifest must be named {MANIFEST_NAME}, got {path!r}")
    if not os.path.isfile(path):
        raise ValueError(f"no manifest at {path}")
    base = os.path.realpath(os.path.dirname(path))
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not a readable JSON manifest: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
        raise ValueError(f"{path} is not a version-{MANIFEST_VERSION} share manifest")

    def inside(*parts: str) -> str:
        resolved = os.path.realpath(os.path.join(base, *parts))
        if not resolved.startswith(base + os.sep):
            raise ValueError(f"{os.path.join(*parts)!r} resolves outside the manifest directory")
        return resolved

    def fixed(key: str, name: str) -> Optional[str]:
        value = data.get(key)
        if value is None:
            return None
        if value != name:
            raise ValueError(f"manifest {key} must be {name!r}, got {value!r}")
        return inside(name)

    def names(key: str) -> list:
        value = data.get(key) or []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"manifest {key} must be a list of file names")
        if len(set(value)) != len(value):
            raise ValueError(f"manifest {key} names a file twice")
        return value

    flows: list[tuple[str, str]] = []
    for name in names("flows"):
        match = FLOW_FILE_RE.match(name)
        if not match:
            raise ValueError(f"manifest flow entry {name!r} is not flow-<insCode>.json")
        flows.append((match.group(1), inside("flows", name)))
    days: list[tuple[date, str]] = []
    for name in names("days"):
        match = DAY_FILE_RE.match(name)
        if not match:
            raise ValueError(f"manifest day entry {name!r} is not day-<yyyymmdd>.json")
        try:
            session = parse_deven(match.group(1))
        except BarParseError as exc:
            raise ValueError(f"manifest day entry {name!r}: {exc}") from exc
        days.append((session, inside("days", name)))
    return ShareManifest(
        base=base,
        market_watch=fixed("market_watch", MARKET_WATCH_NAME),
        static_data=fixed("static_data", STATIC_DATA_NAME),
        flows=flows,
        days=days,
    )


# --- one call ----------------------------------------------------------------


def ingest_share_manifest(
    engine: Engine,
    manifest_path: str,
    part: str,
    offset: int = 0,
    limit: int = 200,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ingest one PART of a fetch run, or one chunk of it.

    ``universe`` ingests the market watch (and the sector names); ``flows``
    and ``sessions`` ingest ``manifest[part][offset:offset + limit]``, each
    file isolated, and answer ``next_offset`` until the part is done.  Raises
    :class:`MarketIngestFailed` only when every item in the call failed.
    """
    if part not in PARTS:
        raise ValueError(f"part must be one of {', '.join(PARTS)}; got {part!r}")
    if offset < 0 or not 1 <= limit <= MAX_CHUNK:
        raise ValueError(f"offset must be >= 0 and limit 1..{MAX_CHUNK}")
    manifest = load_manifest(manifest_path)
    started = ensure_utc(now) or utcnow()
    report: dict[str, Any] = {
        "part": part,
        "provider": PROVIDER_CODE,
        "manifest": manifest_path,
        "started_at": started.isoformat(),
        "offset": offset,
        "limit": limit,
        "items": {},
        "errors": [],
        "succeeded": 0,
        "failed": 0,
    }

    def failed(kind: str, path: str, exc: Exception, **key: str) -> None:
        log.warning("share ingest failed for %s %s: %s", kind, path, exc)
        report["errors"].append(
            {
                "kind": kind,
                "name": os.path.basename(path),
                **key,
                "error": type(exc).__name__,
                "message": str(exc),
            }
        )
        report["failed"] += 1

    if part == "universe":
        if manifest.market_watch is None:
            raise ValueError("the manifest names no market watch")
        static = None
        if manifest.static_data is not None:
            try:
                static = read_payload_file(manifest.static_data)
                parse_static_sectors(static)  # a bad names file must not cost the universe
            except Exception as exc:  # noqa: BLE001 - names are optional, the watch is not
                failed("static_data", manifest.static_data, exc)
                static = None
        try:
            report["universe"] = ingest_universe(
                engine, read_payload_file(manifest.market_watch), static, now=started
            )
            report["succeeded"] += 1
        except Exception as exc:  # noqa: BLE001
            failed("universe", manifest.market_watch, exc)
        report["total"] = 1
        report["next_offset"] = None
    else:
        entries = manifest.flows if part == "flows" else manifest.days
        total = len(entries)
        if offset >= total:
            raise ValueError(
                f"the manifest names {total} {part} file(s); offset {offset} is past the end"
            )
        chunk = entries[offset : offset + limit]
        if part == "flows":
            for code, path in chunk:
                try:
                    result = ingest_client_flows(
                        engine, read_payload_file(path), now=started, ins_code=code
                    )
                except Exception as exc:  # noqa: BLE001 - isolation is the point
                    failed("flows", path, exc, ins_code=code)
                    continue
                report["items"][code] = result
                report["succeeded"] += 1
        else:
            with engine.connect() as conn:
                universe = set(conn.execute(select(market_shares.c.ins_code)).scalars())
            affected: set[str] = set()
            for session, path in chunk:
                try:
                    result, inserted = ingest_day_file(
                        engine, read_payload_file(path), session, now=started, universe=universe
                    )
                except Exception as exc:  # noqa: BLE001
                    failed("sessions", path, exc, trade_date=session.isoformat())
                    continue
                report["items"][session.isoformat()] = result
                report["succeeded"] += 1
                affected |= inserted
            report["coverage_refreshed"] = refresh_session_coverage(engine, affected, now=started)
        report["total"] = total
        end = offset + len(chunk)
        report["next_offset"] = end if end < total else None

    report["finished_at"] = utcnow().isoformat()
    if report["succeeded"] == 0:
        raise MarketIngestFailed(
            f"all {report['failed']} {part} item(s) failed; nothing was ingested", report
        )
    log.info(
        "share ingest %s [%d:%d]: %d ok, %d failed",
        part, offset, offset + limit, report["succeeded"], report["failed"],
    )
    return report


__all__ = [
    "FLOW_FLOOR",
    "SessionContradiction",
    "ShareManifest",
    "WrongSessionFile",
    "ingest_day_file",
    "ingest_share_manifest",
    "ingest_universe",
    "load_manifest",
    "refresh_session_coverage",
    "share_state",
]
