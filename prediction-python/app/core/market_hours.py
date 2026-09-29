"""Market-hours awareness (docs/CONTRACTS.md Addendum 1).

Pure functions, no I/O.  Two calendars:

* IR_GOLD_18K and USD_IRT are ALWAYS open: their primary sources quote
  24/7 every day of the week (Hamrah Gold for 18k, the USDT market for the
  free-market dollar). Update frequency drops on Iranian off-days, but the
  plain STALE_MINUTES age rule handles that honestly.
* IR_COIN_EMAMI and the TGJU daily-close instruments of migration 0031
  (silver 999, the Bahar Azadi, half, quarter and gram coins, 24k gold, melted
  gold per mesghal): open Sat-Wed between ``MARKET_TEHRAN_OPEN`` and
  ``MARKET_TEHRAN_CLOSE`` (Asia/Tehran local time, a fixed UTC+03:30 since
  Iran abolished DST); closed all Thursday and Friday.
* Tehran-exchange commodity funds — gold, silver and saffron alike — trade
  Sat-Wed between ``MARKET_TSE_OPEN`` and ``MARKET_TSE_CLOSE``.
* Global symbols (XAUUSD, XAGUSD, BRENT_OIL, DXY, US10Y): closed from
  Friday 21:00 UTC to Sunday 22:00 UTC, open otherwise.

Freshness rule while a market is CLOSED: an observation from the last session
(observed no earlier than ``closure start - STALE_MINUTES``) still counts as
acceptably fresh; anything older is truly stale.  While OPEN the plain
``STALE_MINUTES`` age rule applies unchanged.

Series that only ever receive ONE settled close per session
(:data:`DAILY_CLOSE_ONLY`) have their own rule instead, mirrored from
backend-go/internal/markethours: they are stale only when that close is more
than :data:`DAILY_CLOSE_STALE_DAYS` calendar days old, because under the
minutes rule yesterday's close — the newest that can exist — is always stale.

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

# Always-open symbols: primary sources quote 24/7 every day (Hamrah Gold
# for 18k, the USDT market for USD). Only the plain age rule applies.
ALWAYS_OPEN_SYMBOLS = frozenset({"IR_GOLD_18K", "USD_IRT"})
# The Tehran bazaar calendar.  The seven migration 0031 codes are one settled
# close per day from TGJU, but the market they close is this one, so a close
# from the last session stays acceptably fresh through the Thu+Fri closure.
IRANIAN_SYMBOLS = frozenset({
    "IR_COIN_EMAMI",
    "IR_SILVER_999",
    "IR_COIN_BAHAR",
    "IR_COIN_HALF",
    "IR_COIN_QUARTER",
    "IR_COIN_GERAMI",
    "IR_GOLD_24K",
    "IR_GOLD_MESGHAL",
})
GLOBAL_SYMBOLS = frozenset({"XAUUSD", "XAGUSD", "BRENT_OIL", "DXY", "US10Y"})

# One settled close per session, stamped 23:00 UTC on its own date: the seven
# TGJU daily-history series of migration 0031 and the commodity funds with no
# live source (their only rows are migration 0030's settled closes).  AYAR and
# TALA have BrsApi's intraday mirror and keep the session rule.
DAILY_CLOSE_ONLY = frozenset({
    "IR_SILVER_999",
    "IR_COIN_BAHAR",
    "IR_COIN_HALF",
    "IR_COIN_QUARTER",
    "IR_COIN_GERAMI",
    "IR_GOLD_24K",
    "IR_GOLD_MESGHAL",
    "IR_SILVER_FUND_SILVER",
    "IR_SILVER_FUND_SIMIN",
    "IR_GOLD_FUND_KAHRABA",
})
# Thursday and Friday shut the bazaar and the exchange, so Saturday's newest
# close is Wednesday's, three days back; a holiday against the weekend makes
# four.  A fifth day means a close is missing (a long Nowruz closure does read
# as stale — and the series is that old).
DAILY_CLOSE_STALE_DAYS = 4

GLOBAL_CLOSE_UTC = time(21, 0)  # Friday
GLOBAL_OPEN_UTC = time(22, 0)   # Sunday


def is_tse_fund(symbol: str) -> bool:
    """Whether ``symbol`` is a Tehran-exchange commodity fund: ``IR_<X>_FUND*``.

    Such funds trade Sat-Wed between MARKET_TSE_OPEN and MARKET_TSE_CLOSE
    (default 12:00-18:00 Tehran) and are closed Thursday AND Friday.  Written
    as a rule over the code rather than the old ``IR_GOLD_FUND`` prefix so the
    silver and saffron funds (``IR_SILVER_FUND_*``, ``IR_SAFFRON_FUND_*``) get
    the session the gold funds always had.  ``IR_GOLD_FUND_FLOW`` stays on it,
    as it always was: the flow ratio is computed from the funds' own sessions
    (migration 0024 files it under calendar_class 'tse_session').
    ``IR_SILVER_999`` is not a fund.
    """
    parts = symbol.split("_")
    return len(parts) >= 3 and parts[0] == "IR" and parts[2] == "FUND"


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
    if symbol in ALWAYS_OPEN_SYMBOLS:
        return True
    if is_tse_fund(symbol):
        local = at_utc.astimezone(TEHRAN)
        if local.weekday() in (THURSDAY, FRIDAY):
            return False
        open_t = _parse_hhmm(getattr(settings, "market_tse_open", "12:00"), time(12, 0))
        close_t = _parse_hhmm(getattr(settings, "market_tse_close", "18:00"), time(18, 0))
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

    Always-open symbols (18k, USD) never enter a closure. Windowed Iranian
    symbols close at ``MARKET_TEHRAN_CLOSE`` on the latest Sat-Wed day at or
    before now; global symbols at the most recent Friday 21:00 UTC.
    """
    at_utc = _ensure_utc(at_utc)
    if is_market_open(symbol, at_utc, settings):
        return None
    if is_tse_fund(symbol):
        close_t = _parse_hhmm(getattr(settings, "market_tse_close", "18:00"), time(18, 0))
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
      keeps counting as fresh for the whole closure;
    * a :data:`DAILY_CLOSE_ONLY` series -> the close's own date is at most
      :data:`DAILY_CLOSE_STALE_DAYS` days before today's Tehran date.
    """
    if observed_at is None:
        return False
    observed_at = _ensure_utc(observed_at)
    at_utc = _ensure_utc(at_utc)
    if symbol in DAILY_CLOSE_ONLY:
        # A close is stamped 23:00 UTC on its trade date: its UTC date is it.
        age_days = (at_utc.astimezone(TEHRAN).date() - observed_at.date()).days
        return age_days <= DAILY_CLOSE_STALE_DAYS
    tolerance = timedelta(minutes=settings.stale_minutes)
    closure_start = closure_started_at(symbol, at_utc, settings)
    if closure_start is None:  # market open: plain age rule
        return (at_utc - observed_at) <= tolerance
    return observed_at >= closure_start - tolerance
