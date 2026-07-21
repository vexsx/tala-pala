"""Market-hours awareness (docs/CONTRACTS.md Addendum 1).

Pure functions, no I/O.  Two calendars:

* IR_GOLD_18K trades 24h/day on Iranian trading days: its primary source
  (Milli Gold, milli.gold) is an online platform with no intraday session —
  but the Iranian off-days still apply: closed all Thursday and Friday
  (Tehran).  During open days a paused feed goes honestly stale.
* Other Iranian symbols (USD_IRT, IR_COIN_EMAMI): open Sat-Wed between
  ``MARKET_TEHRAN_OPEN`` and ``MARKET_TEHRAN_CLOSE`` (Asia/Tehran local time,
  a fixed UTC+03:30 since Iran abolished DST); closed all Thursday and Friday.
* Global symbols (XAUUSD, XAGUSD, BRENT_OIL, DXY, US10Y): closed from
  Friday 21:00 UTC to Sunday 22:00 UTC, open otherwise.

Freshness rule while a market is CLOSED: an observation from the last session
(observed no earlier than ``closure start - STALE_MINUTES``) still counts as
acceptably fresh; anything older is truly stale.  While OPEN the plain
``STALE_MINUTES`` age rule applies unchanged.

Symbols with no known calendar are treated as always open (plain age rule).
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from ..config import Settings

TEHRAN = ZoneInfo("Asia/Tehran")
THURSDAY = 3  # Python weekday(): Monday=0 .. Sunday=6
FRIDAY = 4

# 24h-traded on Iranian trading days (primary source: Milli Gold, no intraday
# session window; Thursday+Friday off-days still apply).
IRAN_24H_SYMBOLS = frozenset({"IR_GOLD_18K"})
IRANIAN_SYMBOLS = frozenset({"USD_IRT", "IR_COIN_EMAMI"})
GLOBAL_SYMBOLS = frozenset({"XAUUSD", "XAGUSD", "BRENT_OIL", "DXY", "US10Y"})
# Tehran-exchange gold funds trade Sat-Wed between MARKET_TSE_OPEN and
# MARKET_TSE_CLOSE (default 12:00-17:00 Tehran); closed Thursday AND Friday.
TSE_FUND_PREFIX = "IR_GOLD_FUND"

GLOBAL_CLOSE_UTC = time(21, 0)  # Friday
GLOBAL_OPEN_UTC = time(22, 0)   # Sunday


def _parse_hhmm(raw: str, default: time) -> time:
    """'HH:MM' -> datetime.time, falling back to ``default`` on bad input."""
    try:
        hh, mm = str(raw).strip().split(":")
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        return default


def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def is_market_open(symbol: str, at_utc: datetime, settings: Settings) -> bool:
    """True when ``symbol``'s market is open at ``at_utc`` (aware or naive-UTC)."""
    at_utc = _ensure_utc(at_utc)
    if symbol in IRAN_24H_SYMBOLS:
        return at_utc.astimezone(TEHRAN).weekday() not in (THURSDAY, FRIDAY)
    if symbol.startswith(TSE_FUND_PREFIX):
        local = at_utc.astimezone(TEHRAN)
        if local.weekday() in (THURSDAY, FRIDAY):
            return False
        open_t = _parse_hhmm(getattr(settings, "market_tse_open", "12:00"), time(12, 0))
        close_t = _parse_hhmm(getattr(settings, "market_tse_close", "17:00"), time(17, 0))
        return open_t <= local.time() < close_t
    if symbol in IRANIAN_SYMBOLS:
        local = at_utc.astimezone(TEHRAN)
        if local.weekday() in (THURSDAY, FRIDAY):
            return False
        open_t = _parse_hhmm(settings.market_tehran_open, time(12, 0))
        close_t = _parse_hhmm(settings.market_tehran_close, time(20, 0))
        return open_t <= local.time() < close_t
    if symbol in GLOBAL_SYMBOLS:
        wd = at_utc.weekday()
        if wd == FRIDAY and at_utc.time() >= GLOBAL_CLOSE_UTC:
            return False
        if wd == 5:  # Saturday: closed all day
            return False
        if wd == 6 and at_utc.time() < GLOBAL_OPEN_UTC:
            return False
        return True
    return True  # unknown symbol: no calendar, treated as always open


def closure_started_at(
    symbol: str, at_utc: datetime, settings: Settings
) -> Optional[datetime]:
    """UTC start of the closure containing ``at_utc``; None while open.

    For 18k (24h symbol) that is Thursday 00:00 Tehran of the current
    Thu+Fri block; for windowed Iranian symbols the most recent trading-day
    close (``MARKET_TEHRAN_CLOSE`` on the latest Sat-Wed day at or before
    now); for global symbols the most recent Friday 21:00 UTC.
    """
    at_utc = _ensure_utc(at_utc)
    if is_market_open(symbol, at_utc, settings):
        return None
    if symbol in IRAN_24H_SYMBOLS:
        # Closed only during the Thu+Fri block; closure began at
        # Thursday 00:00 Tehran of the current block.
        local = at_utc.astimezone(TEHRAN)
        day = local.date()
        if day.weekday() == FRIDAY:
            day -= timedelta(days=1)
        return datetime.combine(day, time(0, 0), tzinfo=TEHRAN).astimezone(timezone.utc)
    if symbol.startswith(TSE_FUND_PREFIX):
        close_t = _parse_hhmm(getattr(settings, "market_tse_close", "17:00"), time(17, 0))
        local = at_utc.astimezone(TEHRAN)
        for days_back in range(9):
            day = (local - timedelta(days=days_back)).date()
            if day.weekday() in (THURSDAY, FRIDAY):
                continue  # no session, hence no close
            candidate = datetime.combine(day, close_t, tzinfo=TEHRAN)
            if candidate <= local:
                return candidate.astimezone(timezone.utc)
        return at_utc  # unreachable with a sane open<close configuration
    if symbol in IRANIAN_SYMBOLS:
        close_t = _parse_hhmm(settings.market_tehran_close, time(20, 0))
        local = at_utc.astimezone(TEHRAN)
        for days_back in range(9):
            day = (local - timedelta(days=days_back)).date()
            if day.weekday() in (THURSDAY, FRIDAY):
                continue  # off-days never have a session, hence no close
            candidate = datetime.combine(day, close_t, tzinfo=TEHRAN)
            if candidate <= local:
                return candidate.astimezone(timezone.utc)
        return at_utc  # unreachable with a sane open<close configuration
    if symbol in GLOBAL_SYMBOLS:
        days_back = (at_utc.weekday() - FRIDAY) % 7
        candidate = datetime.combine(
            (at_utc - timedelta(days=days_back)).date(),
            GLOBAL_CLOSE_UTC,
            tzinfo=timezone.utc,
        )
        if candidate > at_utc:
            candidate -= timedelta(days=7)
        return candidate
    return None


def is_acceptably_fresh(
    symbol: str,
    observed_at: Optional[datetime],
    at_utc: datetime,
    settings: Settings,
) -> bool:
    """Market-hours-aware staleness check (Addendum 1).

    * market OPEN   -> age <= STALE_MINUTES (unchanged semantics);
    * market CLOSED -> ``observed_at`` must be no older than
      ``closure start - STALE_MINUTES``, i.e. data from the last session
      keeps counting as fresh for the whole closure.
    """
    if observed_at is None:
        return False
    observed_at = _ensure_utc(observed_at)
    at_utc = _ensure_utc(at_utc)
    tolerance = timedelta(minutes=settings.stale_minutes)
    closure_start = closure_started_at(symbol, at_utc, settings)
    if closure_start is None:  # market open: plain age rule
        return (at_utc - observed_at) <= tolerance
    return observed_at >= closure_start - tolerance
