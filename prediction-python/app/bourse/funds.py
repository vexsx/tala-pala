"""The IME commodity funds' settled closes, into ``prices`` (migration 0030).

``instruments`` carries the gold and silver commodity funds as prices, and
until 0030 their only rows were BrsApi's intraday mirror of TSETMC's last
trade, from 2026-07-21.  ``GetClosingPriceDailyList/{insCode}/0`` gives each
fund's whole settled history, and this module turns it into what every
``prices`` reader already understands: one toman close per session.

WHAT A ROW HERE IS
------------------
* **The official closing price** (pClosing, TSE's قیمت پایانی) of one fund
  UNIT, divided by ten — TSETMC quotes rials and ``prices`` stores toman.  It
  is the price units changed hands at, NOT the fund's NAV; the registry note
  says so on every fund.
* **Stamped 23:00 UTC on the session's own date** — the convention of
  :func:`app.jobs.tgju_backfill.close_stamp`, 02:30 Tehran the next morning.
  Funds trade until 18:00 Tehran, so the stamp is after the close, and a close
  whose stamp is still in the future at ingest is NOT stored: no row ever
  claims an availability before the number existed.  That is this module's
  settled-session rule, and it is stricter than the shares' 15:00 one because
  the funds close five and a half hours later.
* **Traded sessions only.**  A zero-trade row is TSETMC carrying the previous
  close forward (pClosing = priceYesterday on all 190 measured), a price of
  nothing; it is skipped and counted, never stored as a 0% day.
* **Unadjusted, and checked to be safe that way.**  Across the five funds'
  5,846 consecutive session pairs TSETMC's reference price equals the previous
  close on all but two, and those two differ by exactly one rial.  A fund
  whose reference ever restates beyond rounding (a unit split, a restated
  close on a halt) is REFUSED rather than stored with a step in it that no
  price made.
* **Never overwritten.**  A close that disagrees with the stored one for the
  same session fails that fund and writes nothing for it.

Each close also gets a ``raw_observations`` row holding the rial number TSETMC
served, the audit trail for the division, exactly as the TGJU backfill keeps.
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Optional, Sequence

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import Engine

from ..core.normalize import rial_to_toman
from ..db import commodity_funds, ensure_utc, prices, raw_observations, utcnow
from ..equities.adjust import Bar, parse_daily_list
from ..jobs.tgju_backfill import close_stamp
from .ingest import (
    PROVIDER_CODE,
    MarketIngestFailed,
    _bulk_insert_ignore,
    _close_enough,
    read_payload_file,
)
from .parse import MarketParseError

log = logging.getLogger(__name__)

# The source label in ``prices``: the provider row 0028 created.  No other
# writer uses it in ``prices`` (the equity bars live in their own table), which
# is what lets 0030's down migration remove exactly these rows.
SOURCE = PROVIDER_CODE
CURRENCY = "IRT"
UNIT = "unit"
RAW_CURRENCY = "IRR"
RAW_UNIT = "IRR/unit"

# A reference price that differs from the previous close by no more than this
# is rounding, not a restatement: the only two measured differences are one
# rial each.  A unit split would be a factor of two or more.
ROUNDING_RIALS = 1.0
ROUNDING_RELATIVE = 1e-4


class FundRestated(MarketParseError):
    """TSETMC restated a fund's reference price: unadjusted closes across that
    date would not be comparable, so the fund is refused."""


class FundContradiction(MarketParseError):
    """A fund's served close disagrees with the stored one for a session."""


def _rounding_apart(left: float, right: float) -> bool:
    return abs(left - right) <= max(ROUNDING_RIALS, ROUNDING_RELATIVE * max(abs(left), abs(right)))


def reference_restatements(bars: Sequence[Bar]) -> list[tuple[date, float, float]]:
    """Every session whose reference TSETMC restated, as (date, before, after).

    Pure.  Both of TSETMC's signals (see app.equities.adjust): a reference that
    differs from the previous close, and a zero-trade row whose own close
    differs from its reference.
    """
    out: list[tuple[date, float, float]] = []
    for prev, bar in zip(bars, bars[1:]):
        if not _rounding_apart(bar.price_yesterday, prev.final_close):
            out.append((bar.trade_date, prev.final_close, bar.price_yesterday))
    for bar in bars:
        if not bar.traded and not _rounding_apart(bar.final_close, bar.price_yesterday):
            out.append((bar.trade_date, bar.price_yesterday, bar.final_close))
    return sorted(out)


def fund_roster(bind: Engine, include_disabled: bool = False) -> list[dict[str, Any]]:
    """The funds this deployment ingests, for scripts/tsetmc_fetch.py."""
    t = commodity_funds
    stmt = select(
        t.c.ins_code, t.c.instrument_code, t.c.symbol_fa, t.c.name_fa, t.c.underlying,
        t.c.enabled, t.c.first_close, t.c.last_close, t.c.close_count,
    ).order_by(t.c.instrument_code)
    if not include_disabled:
        stmt = stmt.where(t.c.enabled.is_(True))
    with bind.connect() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        {
            **dict(r),
            "first_close": r["first_close"].isoformat() if r["first_close"] else None,
            "last_close": r["last_close"].isoformat() if r["last_close"] else None,
        }
        for r in rows
    ]


def ingest_fund_closes(
    engine: Engine, payload: Any, now: Optional[datetime] = None
) -> dict[str, Any]:
    """Store one fund's settled closes; see the module docstring for the rules."""
    at = ensure_utc(now) or utcnow()
    bars = parse_daily_list(payload)
    code = bars[0].ins_code

    with engine.begin() as conn:
        fund = conn.execute(
            select(
                commodity_funds.c.instrument_code, commodity_funds.c.symbol_fa,
                commodity_funds.c.enabled, commodity_funds.c.first_close,
                commodity_funds.c.last_close, commodity_funds.c.close_count,
            ).where(commodity_funds.c.ins_code == code)
        ).first()
        if fund is None:
            raise MarketParseError(
                f"insCode {code} is not in commodity_funds. The registry is data (migration "
                "0030 seeds it): add the fund and its instruments row, then ingest."
            )
        if not fund.enabled:
            raise MarketParseError(f"{fund.symbol_fa} ({code}) is disabled in commodity_funds")
        symbol = fund.instrument_code

        restated = reference_restatements(bars)
        if restated:
            shown = "; ".join(f"{d.isoformat()}: {a:,.0f} -> {b:,.0f}" for d, a, b in restated[:3])
            raise FundRestated(
                f"{fund.symbol_fa} ({code}): TSETMC restated the reference price on "
                f"{len(restated)} session(s) ({shown}). Closes on either side of a "
                "restatement are not comparable unadjusted, and this series stores them "
                "unadjusted; nothing was written for this fund."
            )

        traded = [b for b in bars if b.traded]
        carries = len(bars) - len(traded)
        settled = [b for b in traded if close_stamp(b.trade_date) <= at]
        unsettled = len(traded) - len(settled)

        stored = {
            ensure_utc(r.observed_at): float(r.value)
            for r in conn.execute(
                select(prices.c.observed_at, prices.c.value).where(
                    and_(prices.c.symbol == symbol, prices.c.source == SOURCE)
                )
            )
        }
        conflicts: list[str] = []
        price_rows: list[dict[str, Any]] = []
        raw_rows: list[dict[str, Any]] = []
        for bar in settled:
            stamp = close_stamp(bar.trade_date)
            value = rial_to_toman(bar.final_close)
            old = stored.get(stamp)
            if old is not None:
                if not _close_enough(old, value):
                    conflicts.append(f"{bar.trade_date.isoformat()}: stored {old!r}, served {value!r}")
                continue
            price_rows.append({
                "symbol": symbol, "value": value, "currency": CURRENCY, "unit": UNIT,
                "source": SOURCE, "observed_at": stamp, "collected_at": at, "quality": "ok",
            })
            raw_rows.append({
                "provider_code": PROVIDER_CODE, "symbol": symbol,
                # TSETMC's own number in TSETMC's own unit: the audit trail for
                # the division above.
                "raw_value": bar.final_close, "unit": RAW_UNIT, "currency": RAW_CURRENCY,
                "raw_payload": {
                    "ins_code": code, "kind": "settled_close",
                    "trade_date": bar.trade_date.isoformat(),
                    "availability_utc": stamp.isoformat(),
                    "last_trade": bar.close, "price_yesterday": bar.price_yesterday,
                    "volume": bar.volume, "trades": bar.trade_count, "value": bar.value,
                    "normalization": "rial_to_toman", "normalized_value": value,
                    "normalized_currency": CURRENCY,
                },
                "observed_at": stamp, "collected_at": at, "quality": "ok",
                "dedupe_key": f"{PROVIDER_CODE}|{symbol}|close|{bar.trade_date.isoformat()}",
            })
        if conflicts:
            raise FundContradiction(
                f"{fund.symbol_fa} ({code}): the daily list contradicts {len(conflicts)} stored "
                f"close(s) ({'; '.join(conflicts[:3])}). Nothing is overwritten; delete the "
                "rows deliberately if TSETMC genuinely restated them."
            )
        before = len(stored)
        _bulk_insert_ignore(conn, prices, price_rows)
        _bulk_insert_ignore(conn, raw_observations, raw_rows)
        first, last, count = conn.execute(
            select(func.min(prices.c.observed_at), func.max(prices.c.observed_at), func.count())
            .select_from(prices)
            .where(and_(prices.c.symbol == symbol, prices.c.source == SOURCE))
        ).one()
        first_day = ensure_utc(first).date() if first else None
        last_day = ensure_utc(last).date() if last else None
        if (first_day, last_day, count) != (fund.first_close, fund.last_close, fund.close_count):
            conn.execute(
                update(commodity_funds)
                .where(commodity_funds.c.ins_code == code)
                .values(
                    first_close=first_day, last_close=last_day, close_count=int(count),
                    updated_at=at,
                )
            )

    newest = settled[-1] if settled else None
    return {
        "ins_code": code,
        "instrument_code": symbol,
        "symbol": fund.symbol_fa,
        "sessions_total": len(bars),
        "closes_inserted": int(count) - before,
        "closes_existing": len(settled) - len(price_rows),
        "carry_forward_skipped": carries,
        "unsettled_skipped": unsettled,
        "first_date": first_day.isoformat() if first_day else None,
        "last_date": last_day.isoformat() if last_day else None,
        "last_close": rial_to_toman(newest.final_close) if newest else None,
    }


def ingest_fund_files(
    engine: Engine, paths: Sequence[str], now: Optional[datetime] = None
) -> dict[str, Any]:
    """Ingest one run's fund daily lists, each fund in its own transaction.

    Raises :class:`MarketIngestFailed` only when every fund failed, and
    ValueError when the request named none — the conventions of
    /internal/bourse/ingest.
    """
    if not paths:
        raise ValueError("the request named no fund payloads: nothing to ingest")
    started = ensure_utc(now) or utcnow()
    report: dict[str, Any] = {
        "provider": PROVIDER_CODE,
        "started_at": started.isoformat(),
        "funds": {},
        "errors": [],
        "succeeded": 0,
        "failed": 0,
    }
    for path in paths:
        try:
            result = ingest_fund_closes(engine, read_payload_file(path), now=started)
        except Exception as exc:  # noqa: BLE001 - isolation is the point
            log.warning("fund ingest failed for %s: %s", path, exc)
            report["errors"].append(
                {"path": path, "error": type(exc).__name__, "message": str(exc)}
            )
            report["failed"] += 1
            continue
        report["funds"][result["ins_code"]] = result
        report["succeeded"] += 1
    report["finished_at"] = utcnow().isoformat()
    if report["succeeded"] == 0:
        raise MarketIngestFailed(
            f"all {report['failed']} fund payload(s) failed; nothing was ingested", report
        )
    return report


__all__ = [
    "FundContradiction",
    "FundRestated",
    "fund_roster",
    "ingest_fund_closes",
    "ingest_fund_files",
    "reference_restatements",
]
