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
  whose reference restates beyond rounding (a unit split, a restated close on
  a halt) is REFUSED rather than stored with a step in it that no price made.
  Only the pairs a run would WRITE are checked — a new close against its
  neighbours — and a pair the served list could not have chained is not a
  restatement: one of TSETMC's two CDN copies omits the whole 2023-03-27
  session, so its 2023-03-28 reference (77,041, AYAR) is the missing
  session's close, not the 2023-03-26 one it follows (75,820).  A pair that
  skips a session TEDPIX records is therefore reported as a gap in the
  served copy and not judged.  (Measured 2026-09-29: the copy with the
  session and the copy without it were served five minutes apart, and the
  second refused all three gold funds.)  That copy is also inconsistent with
  itself once: Tala's 2021-12-15 close is 93,433 there and the next session
  opens on 93,443, the close the other copy has.  A first ingest from that
  copy refuses Tala for it — ten rials is not the exchange's rounding — and
  the other copy stores it on a later run, after which the difference is a
  reported restatement like any other.
* **Not written into the live era.**  AYAR and TALA have BrsApi's intraday
  mirror (provider ``tse_funds``) from 2026-07-21.  A daily close written into
  a day that holds those observations is a second, different-source row for
  the same day — TSETMC's official close differs from the last trade by a
  median 0.32% and up to 7.2% on عیار — which changes what the day's close
  reads as and fakes a provider disagreement.  So, exactly as
  :mod:`app.jobs.tgju_backfill` does, only sessions strictly before the UTC
  day of the symbol's first observation from ANY other source are written.
* **Never overwritten.**  A close that disagrees with the stored one for the
  same session keeps the stored value and is reported as restated (TSETMC's
  two copies differ on Tala's 2021-12-15 close by ten rials); only a list that
  disagrees with most of what is stored fails the fund
  (app.bourse.ingest.restatement_is_systematic).

Each close also gets a ``raw_observations`` row holding the rial number TSETMC
served, the audit trail for the division, exactly as the TGJU backfill keeps.
"""
from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from datetime import date, datetime
from typing import Any, Iterable, Optional, Sequence

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import Engine

from ..core.normalize import rial_to_toman
from ..db import commodity_funds, ensure_utc, prices, raw_observations, utcnow
from ..equities.adjust import Bar, parse_daily_list
from ..jobs.tgju_backfill import close_stamp
from ..db import market_index_values
from .ingest import (
    PROVIDER_CODE,
    RESTATED_EXAMPLES,
    MarketIngestFailed,
    _bulk_insert_ignore,
    read_payload_file,
    restatement_is_systematic,
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

# The exchange's session calendar a served list is checked against: TEDPIX,
# the index every market-level fetch ingests first (backend-go
# internal/bourse/bourse.go names the same code).
TEDPIX = "32097828799138957"


class FundRestated(MarketParseError):
    """TSETMC restated a fund's reference price: unadjusted closes across that
    date would not be comparable, so the fund is refused."""


class FundContradiction(MarketParseError):
    """A fund's served list disagrees with most of the stored closes it
    overlaps: not a restatement of that fund's history."""


def _rounding_apart(left: float, right: float) -> bool:
    return abs(left - right) <= max(ROUNDING_RIALS, ROUNDING_RELATIVE * max(abs(left), abs(right)))


def _between(sessions: Sequence[date], after: date, before: date) -> list[date]:
    """Market sessions strictly between two dates; ``sessions`` ascending."""
    lo = bisect_right(sessions, after)
    hi = bisect_left(sessions, before)
    return list(sessions[lo:hi])


def reference_restatements(
    bars: Sequence[Bar],
    only: Optional[Iterable[date]] = None,
    sessions: Optional[Sequence[date]] = None,
) -> list[tuple[date, float, float]]:
    """Every session whose reference TSETMC restated, as (date, before, after).

    Pure.  Both of TSETMC's signals (see app.equities.adjust): a reference that
    differs from the previous close, and a zero-trade row whose own close
    differs from its reference.

    ``only``, when given, limits the check to pairs with a session in it on
    either side — the sessions a run would write.  ``sessions``, when given, is
    the exchange's calendar (ascending): a pair that skips one of its sessions
    was not served consecutively, and its reference is the skipped session's
    close, so it is not judged here (:func:`served_gaps` names it).
    """
    wanted = set(only) if only is not None else None
    out: list[tuple[date, float, float]] = []
    for prev, bar in zip(bars, bars[1:]):
        if wanted is not None and bar.trade_date not in wanted and prev.trade_date not in wanted:
            continue
        if sessions and _between(sessions, prev.trade_date, bar.trade_date):
            continue
        if not _rounding_apart(bar.price_yesterday, prev.final_close):
            out.append((bar.trade_date, prev.final_close, bar.price_yesterday))
    for bar in bars:
        if wanted is not None and bar.trade_date not in wanted:
            continue
        if not bar.traded and not _rounding_apart(bar.final_close, bar.price_yesterday):
            out.append((bar.trade_date, bar.price_yesterday, bar.final_close))
    return sorted(out)


def served_gaps(
    bars: Sequence[Bar], sessions: Sequence[date], only: Optional[Iterable[date]] = None
) -> list[date]:
    """The exchange sessions a served daily list skips between its first and
    last row, ascending.  Pure.  A fund's list carries a row for every session
    it was listed through (a zero-trade carry row when nothing traded), so a
    skipped session is a gap in the copy served — or one of the few dates
    TEDPIX carries that no daily list does (2024-08-07 and 2025-02-08 are
    absent from all nineteen roster shares' lists too).  Reported, not
    judged.  ``only`` limits it to the pairs :func:`reference_restatements`
    would have judged, so a weekly run names the gaps next to what it writes
    and not the same old ones every time."""
    wanted = set(only) if only is not None else None
    missing: list[date] = []
    for prev, bar in zip(bars, bars[1:]):
        if wanted is not None and bar.trade_date not in wanted and prev.trade_date not in wanted:
            continue
        missing.extend(_between(sessions, prev.trade_date, bar.trade_date))
    return missing


def _market_sessions(conn, first: date, last: date) -> list[date]:
    """TEDPIX's stored sessions in [first, last], ascending; empty when no
    index is stored, in which case every pair is judged (the strict rule)."""
    t = market_index_values
    return list(
        conn.execute(
            select(t.c.trade_date)
            .where(and_(t.c.ins_code == TEDPIX, t.c.trade_date >= first, t.c.trade_date <= last))
            .order_by(t.c.trade_date)
        ).scalars()
    )


def _live_era_start(conn, symbol: str) -> Optional[datetime]:
    """The first observation of ``symbol`` from any source but this one, of
    any quality — the rule and the reasoning of tgju_backfill.LiveAnchor.cutoff:
    the hazard is caused by the row existing, not by it being good."""
    first = conn.execute(
        select(func.min(prices.c.observed_at)).where(
            and_(prices.c.symbol == symbol, prices.c.source != SOURCE)
        )
    ).scalar()
    return ensure_utc(first) if first is not None else None


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

        traded = [b for b in bars if b.traded]
        carries = len(bars) - len(traded)
        settled = [b for b in traded if close_stamp(b.trade_date) <= at]
        unsettled = len(traded) - len(settled)
        live_from = _live_era_start(conn, symbol)
        live_day = live_from.date() if live_from is not None else None
        in_live_era = [b for b in settled if live_day is not None and b.trade_date >= live_day]
        settled = [b for b in settled if live_day is None or b.trade_date < live_day]

        stored = {
            ensure_utc(r.observed_at): float(r.value)
            for r in conn.execute(
                select(prices.c.observed_at, prices.c.value).where(
                    and_(prices.c.symbol == symbol, prices.c.source == SOURCE)
                )
            )
        }
        restated: list[dict[str, Any]] = []
        new_bars: list[Bar] = []
        price_rows: list[dict[str, Any]] = []
        raw_rows: list[dict[str, Any]] = []
        for bar in settled:
            stamp = close_stamp(bar.trade_date)
            value = rial_to_toman(bar.final_close)
            old = stored.get(stamp)
            if old is not None:
                # One rial of the exchange's rounding is the same close in
                # TSETMC's two copies (33,216 against 33,215 on عیار).
                if not _rounding_apart(old * 10, bar.final_close):
                    restated.append({
                        "date": bar.trade_date.isoformat(), "stored": old, "served": value,
                    })
                continue
            new_bars.append(bar)
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
        compared = len(settled) - len(new_bars)
        if restatement_is_systematic(len(restated), compared):
            shown = "; ".join(
                f"{r['date']}: stored {r['stored']!r}, served {r['served']!r}" for r in restated[:3]
            )
            raise FundContradiction(
                f"{fund.symbol_fa} ({code}): the daily list contradicts {len(restated)} of the "
                f"{compared} stored close(s) it overlaps ({shown}). That is not a restatement "
                "of this fund's history but a different one; nothing was written. Delete the "
                "rows deliberately if TSETMC genuinely changed them all."
            )

        # Restatements are judged only where this run WRITES: a new close
        # against its neighbours, and the carry rows since the newest stored
        # close. Re-judging the whole history on every run refused a fund for
        # good the first time a copy of it was served with a session missing.
        newest_stored = max(stored).date() if stored else None
        judged = {b.trade_date for b in new_bars} | {
            b.trade_date for b in bars
            if not b.traded and (newest_stored is None or b.trade_date > newest_stored)
        }
        sessions = _market_sessions(conn, bars[0].trade_date, bars[-1].trade_date)
        restatements = reference_restatements(bars, only=judged, sessions=sessions)
        if restatements:
            shown = "; ".join(
                f"{d.isoformat()}: {a:,.0f} -> {b:,.0f}" for d, a, b in restatements[:3]
            )
            raise FundRestated(
                f"{fund.symbol_fa} ({code}): TSETMC restated the reference price on "
                f"{len(restatements)} session(s) this run would write ({shown}). Closes on "
                "either side of a restatement are not comparable unadjusted, and this series "
                "stores them unadjusted; nothing was written for this fund."
            )
        gaps = served_gaps(bars, sessions, only=judged)
        if restated:
            log.warning(
                "fund %s: %d stored close(s) differ from TSETMC's current copy and were "
                "kept; first %s", fund.symbol_fa, len(restated), restated[0],
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
        "restated": len(restated),
        "restated_examples": restated[:RESTATED_EXAMPLES],
        "carry_forward_skipped": carries,
        "unsettled_skipped": unsettled,
        # Sessions on or after the first observation of another source: left
        # to that source (module docstring).
        "live_era_skipped": len(in_live_era),
        "live_era_from": live_from.isoformat() if live_from is not None else None,
        # Exchange sessions the served list skips: a gap in TSETMC's copy.
        "served_gaps": len(gaps),
        "served_gap_examples": [d.isoformat() for d in gaps[:RESTATED_EXAMPLES]],
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
    "served_gaps",
]
