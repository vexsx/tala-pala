"""Historical backfill of exogenous market history (Addendum 15).

Why this exists
---------------
The forecast symbols (``IR_GOLD_18K``, ``XAUUSD``, ``USD_IRT``) carry years of
seeded daily history, but every *macro* symbol the models want as exogenous
context — ``DXY``, ``BRENT_OIL``, ``XAGUSD``, ``US10Y`` — only started
accumulating when live collection began. A feature computed from five days of
history cannot be trained on, cannot be ablated, and cannot be honestly
evaluated. This job pulls multi-year daily history for those symbols from the
same public Yahoo chart endpoint the live provider already uses.

Point-in-time correctness (the load-bearing detail)
---------------------------------------------------
A daily bar's CLOSE is not known at the bar's start. Yahoo stamps each daily
bar with its session start; storing that timestamp as ``observed_at`` would
claim the close was available hours before it existed — look-ahead bias
injected straight into the training set.

This job therefore stamps each close at :data:`CLOSE_HOUR_UTC` (23:00 UTC) on
the bar's own UTC date:

* it is at or after the real session close for every instrument handled here
  (COMEX gold ~21:00, ICE Brent ~21:30, DXY ~22:00, CBOT/CBOE yields ~21:00),
  so availability is never overstated;
* it stays inside the bar's own UTC day, so the daily resample
  (``daily_close`` floors to the UTC day) keeps the bar on its correct date.

The legacy ``app/seed/seed_history.py`` used 12:00 UTC, which overstates
availability by ~9 hours. Daily models bucket both to the same day so the
existing series are unaffected; new backfills use the honest convention.

The same rule applies to the NEWEST bar.  During a session Yahoo's chart
serves today's bar with its price so far, and stamped 23:00 UTC today that
row claims a close that does not exist yet — a row from the future.
Measured on the local rehearsal (2026-09-29): a run at 12:18 UTC stored DXY
and BRENT_OIL at 2026-09-29 23:00, and ``/prices/current`` then served
Brent's forming 96.36 as the current price, not stale, with a -7.63% change
computed from it.  So a bar whose close stamp is still ahead of the run is
not stored (``unsettled_skipped``); the next run stores its close.

A run BEFORE that guard may already have stored such a row, and skipping
every day that has a row kept it for good — a forming price read ever after
as that day's close.  It is detectable: a backfill row is collected after its
own 23:00 UTC stamp unless it was written from the future.  So a
``yahoo_backfill`` row with ``observed_at > collected_at`` is not a close:
the first run after its stamp REPLACES it with the settled close
(``forming_replaced``), and migration 0033 flags every such row ``suspect``
so no reader serves it until then.

Idempotency
-----------
Days that already have a price row for the symbol are skipped, so the job can
be re-run safely and never double-writes across timestamp conventions.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, time as dt_time, timezone
from typing import Optional, Sequence

from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import Engine

from ..config import Settings
from ..core.normalize import SYMBOL_META
from ..db import ensure_utc, insert_ignore, prices, raw_observations, utcnow
from ..metrics import JOB_LAST_SUCCESS
from ..providers.base import ProviderError
from ..providers.yahoo import TICKER_MAP, YahooProvider

log = logging.getLogger(__name__)

# Conservative availability stamp for a daily close (see module docstring).
CLOSE_HOUR_UTC = 23

# Symbols this job can backfill, mapped to their Yahoo ticker. Only exogenous
# context symbols: the three forecast symbols have their own seeded history and
# their own live providers, and rewriting them is out of scope here.
BACKFILL_TICKERS: dict[str, str] = {
    symbol: ticker for ticker, symbol in TICKER_MAP.items()
}

# Default set: the macro context that the models want but the DB lacks.
DEFAULT_SYMBOLS: tuple[str, ...] = ("DXY", "BRENT_OIL", "XAGUSD", "US10Y")

DEFAULT_RANGE = "5y"


SOURCE = "yahoo_backfill"


def _existing_days(engine: Engine, symbol: str) -> tuple[set, set]:
    """UTC dates that already have a price row for ``symbol``, and those whose
    only claim is a forming bar a run before the fix stored: a backfill row
    collected before its own close stamp (module docstring)."""
    stmt = select(prices.c.observed_at, prices.c.collected_at, prices.c.source).where(
        prices.c.symbol == symbol
    )
    have: set = set()
    forming: set = set()
    with engine.connect() as conn:
        for observed_at, collected_at, source in conn.execute(stmt):
            if observed_at is None:
                continue
            observed_at = ensure_utc(observed_at)
            if source == SOURCE and collected_at is not None and observed_at > ensure_utc(collected_at):
                forming.add(observed_at.date())
            else:
                have.add(observed_at.date())
    return have, forming - have


def _close_stamp(bar_ts: datetime) -> datetime:
    """Availability instant for a daily bar: 23:00 UTC on the bar's own date."""
    day = bar_ts.astimezone(timezone.utc).date()
    return datetime.combine(day, dt_time(CLOSE_HOUR_UTC, 0), tzinfo=timezone.utc)


def backfill_symbol(
    engine: Engine,
    settings: Settings,
    symbol: str,
    range_: str = DEFAULT_RANGE,
) -> dict:
    """Backfill one symbol's daily history; returns a per-symbol summary."""
    ticker = BACKFILL_TICKERS.get(symbol)
    if ticker is None:
        return {"symbol": symbol, "error": "no yahoo ticker mapping", "inserted": 0}
    if symbol not in SYMBOL_META:
        return {"symbol": symbol, "error": "unknown symbol", "inserted": 0}

    provider = YahooProvider(
        courtesy_delay=settings.provider_courtesy_delay,
        backoff_base=settings.provider_backoff_base,
        timeout=settings.http_timeout_seconds,
    )
    try:
        pairs = provider.fetch_history(ticker, range_=range_)
    except (ProviderError, Exception) as exc:  # noqa: BLE001 — one symbol must not sink the job
        log.warning("backfill fetch failed for %s (%s): %s", symbol, ticker, exc)
        return {"symbol": symbol, "error": str(exc), "inserted": 0}

    if not pairs:
        return {"symbol": symbol, "error": "empty history payload", "inserted": 0}

    currency, unit = SYMBOL_META[symbol]
    have, forming = _existing_days(engine, symbol)
    now = utcnow()
    price_rows: list[dict] = []
    raw_rows: list[dict] = []
    replacements: list[tuple] = []
    skipped = 0
    unsettled = 0
    for bar_ts, value in pairs:
        if not (isinstance(value, (int, float)) and value > 0):
            continue
        day = bar_ts.astimezone(timezone.utc).date()
        stamp = _close_stamp(bar_ts)
        if stamp > now:
            # Today's bar, still forming: its close does not exist yet
            # (module docstring). Checked before `have`, so the day stays
            # open for the run that stores its close.
            unsettled += 1
            continue
        if day in forming:
            # A forming bar a run before the fix stored under this stamp: the
            # close exists now, and replaces it (module docstring).
            forming.discard(day)
            have.add(day)
            replacements.append((day, stamp, float(value)))
            continue
        if day in have:
            skipped += 1
            continue
        have.add(day)  # guard against duplicate bars inside one payload
        price_rows.append({
            "symbol": symbol, "value": float(value), "currency": currency,
            "unit": unit, "source": SOURCE, "observed_at": stamp,
            "collected_at": now, "quality": "ok",
        })
        raw_rows.append({
            "provider_code": "yahoo", "symbol": symbol, "raw_value": float(value),
            "unit": unit, "currency": currency,
            "raw_payload": {
                "ticker": ticker, "interval": "1d", "kind": "backfill",
                # event time = the bar itself; availability = our close stamp.
                "bar_start_utc": bar_ts.astimezone(timezone.utc).isoformat(),
                "availability_utc": stamp.isoformat(),
            },
            "observed_at": stamp, "collected_at": now, "quality": "ok",
            "dedupe_key": f"yahoo|{symbol}|backfill|{day.isoformat()}",
        })

    inserted = 0
    replaced = 0
    if price_rows or replacements:
        with engine.begin() as conn:
            if price_rows:
                inserted = insert_ignore(conn, prices, price_rows)
                insert_ignore(conn, raw_observations, raw_rows)
            for day, stamp, value in replacements:
                replaced += _replace_forming(conn, symbol, ticker, day, stamp, value, now)
    if replaced:
        log.warning("backfill %s: %d forming bar(s) a run before the fix stored as closes were "
                    "replaced by their settled closes", symbol, replaced)
    log.info("backfill %s: %d inserted, %d already present", symbol, inserted, skipped)
    return {
        "symbol": symbol, "inserted": int(inserted), "skipped_existing": skipped,
        "unsettled_skipped": unsettled, "forming_replaced": replaced,
        "fetched": len(pairs), "range": range_,
        "first": price_rows[0]["observed_at"].date().isoformat() if price_rows else None,
        "last": price_rows[-1]["observed_at"].date().isoformat() if price_rows else None,
    }


def _replace_forming(conn, symbol: str, ticker: str, day, stamp: datetime, value: float,
                     now: datetime) -> int:
    """Put the settled close in place of a forming bar stored under its stamp.

    An UPDATE, because the row's key (symbol, stamp, source) is the close's
    own: the forming price was never a close of anything, so this corrects a
    row that claimed an availability it did not have — and the audit row
    keeps the value it replaced.  Only a row still collected before its stamp
    is touched, so a second run finds nothing to replace.
    """
    result = conn.execute(
        update(prices)
        .where(and_(
            prices.c.symbol == symbol, prices.c.source == SOURCE,
            prices.c.observed_at == stamp, prices.c.collected_at < stamp,
        ))
        .values(value=value, collected_at=now, quality="ok")
    )
    if not result.rowcount:
        return 0
    key = f"yahoo|{symbol}|backfill|{day.isoformat()}"
    old = conn.execute(
        select(raw_observations.c.raw_value).where(raw_observations.c.dedupe_key == key)
    ).scalar()
    conn.execute(
        update(raw_observations)
        .where(raw_observations.c.dedupe_key == key)
        .values(
            raw_value=value, collected_at=now, quality="ok",
            raw_payload={
                "ticker": ticker, "interval": "1d", "kind": "backfill",
                "availability_utc": stamp.isoformat(),
                "replaced_forming_value": float(old) if old is not None else None,
            },
        )
    )
    return 1


def run_backfill(
    engine: Engine,
    settings: Settings,
    symbols: Optional[Sequence[str]] = None,
    range_: str = DEFAULT_RANGE,
) -> dict:
    """Backfill daily history for the requested symbols (default: macro set)."""
    requested = [s for s in (symbols or DEFAULT_SYMBOLS) if s in BACKFILL_TICKERS]
    results = [backfill_symbol(engine, settings, s, range_) for s in requested]
    total = sum(r.get("inserted", 0) for r in results)
    if total:
        JOB_LAST_SUCCESS.labels(job="backfill").set(time.time())
    return {
        "symbols": results,
        "total_inserted": total,
        "coverage": coverage_report(engine),
    }


def coverage_report(engine: Engine) -> list[dict]:
    """Per-symbol daily coverage — the honest picture of what models can use."""
    stmt = (
        select(
            prices.c.symbol,
            func.count().label("n"),
            func.min(prices.c.observed_at).label("first"),
            func.max(prices.c.observed_at).label("last"),
        )
        .where(prices.c.quality == "ok")
        .group_by(prices.c.symbol)
        .order_by(prices.c.symbol)
    )
    out: list[dict] = []
    with engine.connect() as conn:
        for symbol, n, first, last in conn.execute(stmt):
            out.append({
                "symbol": symbol,
                "n": int(n),
                "first": first.date().isoformat() if first else None,
                "last": last.date().isoformat() if last else None,
            })
    return out
