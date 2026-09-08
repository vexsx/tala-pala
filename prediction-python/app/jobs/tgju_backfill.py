"""Deep-history gap-fill of the Iranian daily series from TGJU (P1).

Why this exists
---------------
The flagship P1 analytics — purchasing power, inflation catch-up, long-run
real returns — are statistically empty on the history this database holds.
Measured on production 2026-09-09, ``prices`` starts at 2022-04 for
``IR_GOLD_18K`` and ``USD_IRT`` and at 2026-04 for ``IR_COIN_EMAMI``: too
short to say anything honest about a decade of inflation.  TGJU publishes far
deeper daily history for exactly these three instruments (same run:
``geram18`` 3498 rows from 2013-07-22, ``price_dollar_rl`` 3946 rows from
2011-11-26, ``sekee`` 4284 rows from 2010-04-04), and this job splices that
history in FRONT of the live era.

Those oldest production rows are themselves TGJU (``source = 'tgju'``, written
by the legacy seeder), verified against TGJU's published table to the digit
(2022-04-20 ``geram18`` = 1,293,840 toman on both sides; 2022-04-04
``price_dollar_rl`` = 27,717 toman on both sides).  History stops in 2022 only
because ``fetch_history``'s ``max_rows`` defaulted to 1200 and truncated
silently.  So this job EXTENDS a segment that already exists; it does not
introduce a new source.

The splice is real, and it is recorded rather than hidden
---------------------------------------------------------
Extending it does not make the join harmless.  The SERIES DEFINITION still
changes where the backfilled era meets the live one:

* ``USD_IRT`` is the free-market **cash** dollar's daily close (TGJU
  ``price_dollar_rl``) before the join and the 24/7 **USDT/toman** market
  (BitMax) after it — a different instrument carrying its own stablecoin
  premium;
* ``IR_GOLD_18K`` is TGJU's bazaar **aggregate** before and a single live
  dealer feed (Hamrah Gold) after;
* ``IR_COIN_EMAMI`` is TGJU's published coin close before and the live dealer
  quotes after.

P1 makes that far more consequential, because returns are now measured over
fifteen years ACROSS the join.  A backfill that refused on those grounds would
leave the analytics empty; a backfill that hid it would let every long-run
number quietly mix two conventions.  So the splice is made VISIBLE, at three
levels, by :func:`_splice_record`:

1. every backfilled row carries ``source = 'tgju_history'`` (:data:`SOURCE`),
   so the two eras are separable with a ``WHERE`` clause;
2. the symbol's ``instruments.notes`` row says in words that the definition
   changes, on what date, from what to what — that text is what the UI shows
   beside the instrument;
3. ``app_settings['series_splices']`` holds the machine-readable register
   (splice instant, both definitions, both sources, when it was recorded), so
   an analytics reader can find the boundary without parsing prose.

What it will not do, and why
----------------------------
**It never writes into the live era.**  For each symbol the cutoff is
``min(observed_at)`` in ``prices``, read from the database at run time and
never hardcoded; only bars dated strictly before that observation's UTC day
are written (see :func:`_rows_to_write` for why the boundary is the whole day
rather than the instant).  This is not tidiness.  ``prices`` is unique on
``(symbol, observed_at, source)``, so a daily TGJU close landing inside a
period already covered by 5-minute Hamrah Gold data does not collide — it is
accepted as a SECOND same-day observation from another source, which silently
changes candle aggregation (``daily_close`` takes the last value in the UTC
day) and injects a fake disagreement into the provider-gap dispersion
statistics.  The dense era is left exactly as collected.

**It does not backfill XAUUSD.**  Our XAUUSD is collected from Yahoo ``GC=F``
(see ``app/providers/yahoo.py``), a COMEX **futures** proxy; TGJU's ``ons`` is
the **spot** ounce.  The two are different instruments that differ by the cost
of carry, so splicing spot history onto a futures series manufactures a level
discontinuity precisely at the join and silently corrupts every long-run gold
statistic computed across it — the exact failure the join check below exists
to catch, deliberately introduced.  If a spot series is ever wanted it needs
its OWN symbol (e.g. ``XAUUSD_SPOT``) collected from a spot source, never a
splice into the futures-proxy series.  Asking this endpoint for ``XAUUSD`` is
refused with that explanation rather than quietly ignored.  ``app/seed/
seed_history.py`` no longer writes XAUUSD for the same reason.

Timestamp convention
--------------------
Each daily close is stamped at 23:00 UTC (:data:`CLOSE_HOUR_UTC`, imported
from ``app/jobs/backfill.py`` so there is one definition) on the bar's own
Gregorian date.  For an Iranian instrument that is 02:30 Tehran the following
morning — after the bazaar has closed for the day, so the claim "this close
was available at this instant" is never overstated — while still sitting
inside the bar's own UTC day, so ``features.daily_close`` (which floors to the
UTC day) keeps the bar on its correct date.

The legacy ``app/seed/seed_history.py`` stamps 12:00 UTC, which for these
symbols is 15:30 Tehran, BEFORE the Tehran close: that timestamp asserts a
close was knowable hours before it existed, which is look-ahead bias written
straight into the training set.  This job does not reuse it.

Units and audit trail
---------------------
TGJU quotes Iranian instruments in **rials**.  Normalization to toman goes
through :func:`providers.tgju.normalize_history_value` — the same function the
live path uses — so the factor is never re-derived here.  Every normalized
``prices`` row is written together with a ``raw_observations`` row carrying
the provider's RAW rial value and its raw unit (``IRR/gram``), the way
collection does.  That raw/normalized split is what let migration 0022
diagnose a tenfold scale error years after the fact; a backfill that stored
only the normalized number would destroy that property for a decade of
history in one call.

The three guards, and what each one can prove
---------------------------------------------
*Dates* (:func:`_bar_date_error`).  A TGJU history row carries the Gregorian
date in column 6 and the JALALI date in column 7, and a Jalali date parses
cleanly as a Gregorian one — ``1405/01/19`` is a perfectly valid ``date`` in
the year 1405.  Such a bar is dated centuries before the cutoff, so it is
"eligible", gets written, and becomes the oldest row in ``prices`` — after
which every real bar is at-or-after the new cutoff, nothing is ever eligible
again, and the job reports the success-looking ``up_to_date`` forever.  So bar
dates are bounded to a plausible window and a payload with even one row
outside it is refused whole: one shifted column means the whole payload's
column layout is suspect, not just that row.

*Internal coherence* (:func:`span_breaks`).  Checking only the bar adjacent to
the live cutoff proves nothing about the other 4000.  A scale or convention
break INSIDE the backfilled decade — TGJU switching a table from rial to
toman, or gram to mesghal, mid-series — would be written verbatim, with
status ``ok`` and no note.  So every adjacent pair in the span is checked
against a tolerance that compounds with the real gap between the two bars, and
any break refuses the symbol.

*The join* (:func:`_check_join`).  The last backfilled value against the first
trustworthy live value, which is the only test that can catch a mismatch
between our era and TGJU's.  Beyond a gap-scaled threshold
(:func:`join_threshold_pct`) the symbol is REFUSED outright — nothing written,
both values and the implied jump reported.

Note on what is NOT used as a plausibility test:
``validation.SANITY_RANGES`` is calibrated on today's levels and would reject
the very history this job recovers — the Emami coin traded around 3e5 IRT in
2010, an order of magnitude below the table's 1e6 floor for
``IR_COIN_EMAMI``.  Continuity, not an absolute band, is the test that works
across a decade of Iranian inflation.

Idempotency
-----------
A re-run finds the cutoff already moved back to the first backfilled row, so
no bar is eligible and nothing is written.  Belt and braces: the rows carry
their own ``source`` (:data:`SOURCE`) and deterministic dedupe keys, so even a
cutoff that somehow moved would conflict rather than duplicate.  ``dry_run``
runs every step above, including all three guards, and skips only the writes.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from typing import NamedTuple, Optional, Sequence

from sqlalchemy import func, select, update
from sqlalchemy.engine import Connection, Engine

from ..config import Settings
from ..core.normalize import SYMBOL_META
from ..db import (
    app_settings,
    ensure_utc,
    insert_ignore,
    instruments,
    prices,
    raw_observations,
    utcnow,
)
from ..metrics import JOB_LAST_SUCCESS
from ..providers.tgju import (
    SLUG_MAP,
    HistoryPage,
    TGJUProvider,
    normalize_history_value,
)
from .backfill import CLOSE_HOUR_UTC  # one definition of the 23:00 UTC stamp

log = logging.getLogger(__name__)

# Canonical symbol -> TGJU slug.  Deliberately NOT 'ons'/XAUUSD (module
# docstring): our XAUUSD is a COMEX futures proxy and TGJU's is spot.
BACKFILL_SLUGS: dict[str, str] = {
    "IR_GOLD_18K": "geram18",
    "USD_IRT": "price_dollar_rl",
    "IR_COIN_EMAMI": "sekee",
}

# Symbols this job refuses on purpose, with the reason the caller gets back.
REFUSED_SYMBOLS: dict[str, str] = {
    "XAUUSD": (
        "XAUUSD is collected from Yahoo GC=F, a COMEX FUTURES proxy, while "
        "TGJU 'ons' is the spot ounce; splicing them would manufacture a "
        "level discontinuity at the join and corrupt every long-run gold "
        "statistic. A spot series needs its own symbol and its own source."
    ),
}

PROVIDER_CODE = "tgju"
# Distinct from the live 'tgju' source so a backfilled row is identifiable —
# and deletable — without guessing from its timestamp.
SOURCE = "tgju_history"

# Rows to request.  The provider default (1200) silently truncates every one
# of these series; sekee alone was 4284 rows on 2026-09-09.  This cap is far
# above the longest table and, crucially, the response is CHECKED against the
# server's own ``recordsTotal`` — a truncated payload refuses the symbol
# loudly instead of storing a fragment (see :func:`_truncation_error`).
MAX_HISTORY_ROWS = 20_000

# --- plausible bar dates ----------------------------------------------------
#
# The failure this bounds is not a typo, it is a COLUMN SHIFT: TGJU's history
# row is [open, low, high, close, change, change_pct, gregorian, jalali], and
# the Jalali date one column over parses as a valid Gregorian date in the year
# 1300-1500 range.  Anything from that range is centuries below the floor
# here, so the two can never be confused.
#
# The floor itself: the deepest table TGJU serves for these instruments starts
# 2010-04-04 (sekee, verified 2026-09-09), and daily Iranian bazaar quotes do
# not exist on this endpoint before that.  1990 leaves two decades of slack
# for a table that turns out to reach further back, while still sitting ~500
# years above any Jalali year.
MIN_BAR_DATE = date(1990, 1, 1)
# A close cannot be published for a day that has not happened.  One day of
# slack because Tehran (UTC+3:30) is already on the next calendar date for
# three and a half hours of every UTC day.
MAX_BAR_DATE_SLACK = timedelta(days=1)
# How many offending dates to name in a refusal before saying "and N more".
MAX_REPORTED_DATES = 5

# --- join sanity check thresholds ------------------------------------------
#
# What must be caught: a convention or scale mismatch between the backfilled
# history and the live series.  Every such failure is an order-of-magnitude
# one — rials stored as toman is exactly 10x (+900% reading up, -90% reading
# down), a gram/coin or gram/ounce unit mixup is larger still.  So the
# smallest signature the check must never miss is 90%.
#
# What must NOT be flagged: a real market move across the join.  Iranian gold
# and USD/IRT are capable of double-digit percentage moves in a single day
# during a currency crisis, and the join can land on a multi-day bazaar
# closure (Nowruz alone shuts the market for the better part of two weeks),
# so a flat few-percent tolerance would refuse honest data.
#
# Hence a threshold scaled by the REAL gap between the two dates, which the
# job measures rather than assumes: 15% for the join itself plus 5% for each
# day of separation, capped at 50%.  The cap is what keeps the check useful —
# at 50% there is still a 1.8x margin below the 90% signature of a decimal
# error, so no scale mismatch can hide inside the tolerance no matter how
# wide the gap.
JOIN_BASE_TOLERANCE_PCT = 15.0
JOIN_PER_DAY_TOLERANCE_PCT = 5.0
JOIN_MAX_TOLERANCE_PCT = 50.0
# The smallest jump a scale/convention error can produce (10x read downward).
# Stated so the relationship above is checkable, not just asserted in prose.
SCALE_ERROR_MIN_JUMP_PCT = 90.0

# --- internal coherence of the backfilled span ------------------------------
#
# The join check looks at ONE pair of bars.  This one looks at every adjacent
# pair inside the span, because a rial/toman switch in 2014 is invisible at
# the 2026 join and corrupts every return computed across it.
#
# Measured as a RATIO (max(a/b, b/a)), not as a percentage.  A 10x error read
# upward is +900% and read downward is -90%: the percentage scale compresses
# one direction against a -100% floor while the other runs away, so a single
# percentage threshold cannot be symmetric.  The ratio is 10 either way.
#
# Why a naive 20% bar-to-bar limit would be wrong: the rial genuinely moves
# that fast.  During the October 2012 collapse the free-market dollar went
# from roughly 24,000-25,000 to about 35,000 rial in a handful of sessions,
# with individual sessions well into double digits; the 2018 sanctions
# snapback took USD/IRT from about 4,200 to about 19,000 toman inside a year;
# June 2025 produced several more double-digit sessions.  A 20% rule would
# refuse the most informative years in the series.
#
# So the tolerance is "one violent session, compounded by a plausible maximum
# drift for however many days actually separate the two bars":
#
#   tolerance(gap) = SPAN_SESSION_RATIO * SPAN_DAILY_DRIFT ** gap
#
# * 1.5 for a single session is roughly three times the worst session the
#   sources above describe, so honest crisis data is never refused;
# * 0.6%/day compounds to ~8.9x a year, about twice the worst YEAR in this
#   history (2018, ~4.5x) and five times the realized long-run drift of the
#   fixture series itself (the Emami coin ran 258,000 -> 190,600,000 toman
#   between 2010 and 2026, which is 0.11%/day).
#
# The gap term is what makes multi-day bazaar closures — Nowruz, Ashura,
# a suspended market — legal without weakening the one-session case.
SPAN_SESSION_RATIO = 1.5
SPAN_DAILY_DRIFT = 1.006
# The smallest factor a real convention break could introduce.  Not 10 (rial
# vs toman) — TGJU also publishes gold per MESGHAL, and one mesghal is
# 4.6083 grams, so a gram/mesghal switch is the tightest thing this check has
# to catch.
MIN_CONVENTION_FACTOR = 4.6083
# Beyond this many days of separation the compounding tolerance grows past
# MIN_CONVENTION_FACTOR and the check can no longer PROVE a convention break
# did not hide inside the gap.  Such pairs are still checked, but they are
# reported as unverifiable rather than silently counted as passes — a daily
# series should not contain gaps that long in the first place.
SPAN_GUARANTEE_DAYS = int(
    math.log(MIN_CONVENTION_FACTOR / SPAN_SESSION_RATIO) / math.log(SPAN_DAILY_DRIFT)
)
# How many breaks / unverifiable gaps to carry in the report before truncating.
MAX_REPORTED_BREAKS = 5

# --- the live anchor --------------------------------------------------------
#
# Sources in ``prices`` that must never become the value the backfill is
# checked against.  ``prices`` has FOUR independent writers today —
# ``jobs/collect.py`` (live providers), ``jobs/backfill.py`` (Yahoo history),
# this job, and ``app/seed/seed_history.py`` (operator CSVs and bundled sample
# files) — and the last of those writes ``quality = 'ok'`` on whatever the
# file contained.  So neither "there is only one writer" nor "every row is an
# accepted observation" is true, and the join reference has to be chosen
# rather than assumed.
UNTRUSTED_ANCHOR_SOURCES = frozenset({
    "seed_sample",  # synthetic bundled demo CSV (data_samples/)
    "csv_import",   # whatever an operator handed --from-csv
    SOURCE,         # this job's own output: a self-confirming join proves nothing
})

SPLICE_SETTINGS_KEY = "series_splices"
# Everything from this marker to the end of an instrument note is owned by
# this job, so a re-run replaces its own sentence instead of appending a
# second copy beside the human-written part.
SPLICE_NOTE_MARKER = "[series splice]"

# symbol -> (what the backfilled era measures, what the live era measures).
# Written into the instrument note in words; see the module docstring.
SPLICE_DEFINITIONS: dict[str, tuple[str, str]] = {
    "IR_GOLD_18K": (
        "one daily close of TGJU's geram18 aggregate of Tehran bazaar 18k "
        "gold-per-gram quotes, published in rials and stored as toman",
        "a single live dealer feed quoting continuously, 24/7, many "
        "observations a day",
    ),
    "USD_IRT": (
        "one daily close of TGJU's price_dollar_rl — the free-market CASH "
        "dollar in Tehran, published in rials and stored as toman",
        "the 24/7 USDT/toman market used as the free-market proxy, which "
        "carries its own stablecoin premium over cash dollars",
    ),
    "IR_COIN_EMAMI": (
        "one daily close of TGJU's sekee — the Emami coin as the bazaar "
        "aggregator published it, in rials and stored as toman",
        "the live dealer quotes for the coin, collected through the day",
    ),
}


class BackfillRefused(Exception):
    """A symbol failed a guard; nothing is written for it."""


class LiveAnchor(NamedTuple):
    """What ``prices`` already holds for a symbol, as two separate facts.

    The two questions this job asks of the existing rows are different, so
    they are answered by two different rows:

    ``cutoff``
        "Is there data on this day already?"  Answered by the oldest row of
        ANY source and ANY quality, because the hazard being avoided — a
        second same-day observation from another source changing candle
        aggregation — is caused by the row existing, not by it being good.
        Making this one selective would also break idempotency: this job's own
        rows have to count, or a re-run would rewrite them.

    ``reference_*``
        "What value should the backfill be checked against?"  Answered by the
        oldest row that is actually an accepted market observation of this
        symbol (``quality = 'ok'``, positive, and not from
        :data:`UNTRUSTED_ANCHOR_SOURCES`).  A synthetic sample row that
        happens to be the oldest must not silently become the yardstick for a
        decade of history.

    ``reference_*`` is ``None`` when no such row exists; the caller refuses
    with that stated, rather than joining against something it does not trust.
    """

    cutoff: datetime
    cutoff_source: str
    cutoff_quality: str
    rows: int
    reference_at: Optional[datetime]
    reference_value: Optional[float]
    reference_source: Optional[str]


def join_threshold_pct(gap_days: int) -> float:
    """Tolerated percentage jump across a join separated by ``gap_days``.

    See the constants above for the reasoning; the cap is what guarantees a
    scale error can never sit inside the tolerance.
    """
    gap = max(int(gap_days), 0)
    return min(
        JOIN_BASE_TOLERANCE_PCT + JOIN_PER_DAY_TOLERANCE_PCT * gap,
        JOIN_MAX_TOLERANCE_PCT,
    )


def span_tolerance_ratio(gap_days: int) -> float:
    """Tolerated bar-to-bar RATIO for two bars ``gap_days`` apart.

    One violent session compounded by a plausible maximum drift for the days
    actually separating the bars — see the constants above for where both
    numbers come from.
    """
    gap = max(int(gap_days), 1)
    return SPAN_SESSION_RATIO * (SPAN_DAILY_DRIFT ** gap)


def close_stamp(day: date) -> datetime:
    """Availability instant for a daily bar: 23:00 UTC on the bar's own date.

    02:30 Tehran the next morning — after the bazaar close, so availability is
    never overstated — and still inside the bar's own UTC day, so the daily
    resample keeps the bar on its correct date.  See the module docstring for
    why ``seed_history``'s 12:00 UTC (15:30 Tehran, mid-session) is not used.
    """
    return datetime.combine(day, dt_time(CLOSE_HOUR_UTC, 0), tzinfo=timezone.utc)


def bar_date_window(now: Optional[datetime] = None) -> tuple[date, date]:
    """The window a published daily bar's date must fall inside."""
    today = (now or utcnow()).astimezone(timezone.utc).date()
    return MIN_BAR_DATE, today + MAX_BAR_DATE_SLACK


def live_anchor(
    engine: Engine,
    symbol: str,
    ignore_sources: Sequence[str] = (),
) -> Optional[LiveAnchor]:
    """Read the write boundary and the join reference for ``symbol``.

    ``None`` when the symbol has no rows at all.  See :class:`LiveAnchor` for
    why the two are read as separate rows.

    ``ignore_sources`` is the operator override: sources named here are
    excluded from the join REFERENCE on top of
    :data:`UNTRUSTED_ANCHOR_SOURCES`, which is what unblocks a symbol whose
    oldest accepted-looking row has been diagnosed as junk.  It deliberately
    cannot move ``cutoff``: no argument to this job may make it write into a
    day that already holds an observation.
    """
    excluded = set(UNTRUSTED_ANCHOR_SOURCES)
    excluded.update(s.strip() for s in ignore_sources if s and s.strip())
    with engine.connect() as conn:
        oldest = conn.execute(
            select(prices.c.observed_at, prices.c.source, prices.c.quality)
            .where(prices.c.symbol == symbol)
            .order_by(prices.c.observed_at.asc())
            .limit(1)
        ).first()
        if oldest is None or oldest[0] is None:
            return None
        total = conn.execute(
            select(func.count()).select_from(prices).where(prices.c.symbol == symbol)
        ).scalar_one()
        reference = conn.execute(
            select(prices.c.observed_at, prices.c.value, prices.c.source)
            .where(
                prices.c.symbol == symbol,
                prices.c.quality == "ok",
                prices.c.value > 0,  # also guards the ratio in _check_join
                prices.c.source.notin_(sorted(excluded)),
            )
            .order_by(prices.c.observed_at.asc())
            .limit(1)
        ).first()
    return LiveAnchor(
        cutoff=ensure_utc(oldest[0]),
        cutoff_source=str(oldest[1] or ""),
        cutoff_quality=str(oldest[2] or ""),
        rows=int(total),
        reference_at=ensure_utc(reference[0]) if reference is not None else None,
        reference_value=float(reference[1]) if reference is not None else None,
        reference_source=str(reference[2] or "") if reference is not None else None,
    )


def _truncation_error(page: HistoryPage, requested: int) -> Optional[str]:
    """Describe a truncated history payload, or None when it is complete.

    Truncation is the one failure that looks exactly like success: a short
    answer and a complete answer are the same list of rows.  Two independent
    ways of noticing it, because either alone can be defeated —

    * the server states ``recordsTotal``; fewer rows than that is proof of a
      cut, and the count is compared against what the payload CARRIED, not
      against the parsed rows (the parser legitimately drops junk rows);
    * if the server states nothing, a payload that exactly fills the
      requested cap cannot be shown to be complete, so it is refused too.
    """
    total = page.records_total
    if total is not None and page.returned_rows < total:
        return (
            f"history truncated: server reports {total} rows, payload carried "
            f"{page.returned_rows} (requested length={requested})"
        )
    if total is None and page.returned_rows >= requested:
        return (
            f"history may be truncated: payload filled the requested cap "
            f"({page.returned_rows} rows, length={requested}) and the server "
            f"reported no recordsTotal to check it against"
        )
    return None


def _bar_date_error(
    pairs: Sequence[tuple[date, float]], window: tuple[date, date]
) -> Optional[str]:
    """Describe out-of-window bar dates, or None when every date is plausible.

    Refuses the WHOLE payload rather than dropping the offending rows: the
    realistic cause is TGJU shifting the history table's columns, which puts
    the Jalali date where the Gregorian one belongs (see the constants above).
    That is a statement about the payload's layout, so the remaining rows are
    no more trustworthy than the ones that gave it away — and their prices
    would be read out of shifted columns too.
    """
    low, high = window
    bad = [day for day, _ in pairs if day < low or day > high]
    if not bad:
        return None
    shown = ", ".join(day.isoformat() for day in sorted(set(bad))[:MAX_REPORTED_DATES])
    extra = ""
    if len(set(bad)) > MAX_REPORTED_DATES:
        extra = f" (and {len(set(bad)) - MAX_REPORTED_DATES} more)"
    return (
        f"{len(bad)} of {len(pairs)} bar date(s) fall outside the plausible "
        f"window {low.isoformat()}..{high.isoformat()}: {shown}{extra}. A "
        f"Jalali date parses as a valid Gregorian one, so this reads as a "
        f"shifted history column rather than one bad row; refusing the whole "
        f"payload because every other row's columns are then suspect too"
    )


def span_breaks(kept: Sequence[tuple[date, float, float]]) -> dict:
    """Check every adjacent pair in the backfilled span for a discontinuity.

    Returns the full picture either way — a coherent span is evidence too, and
    the caller reports it — with ``ok`` saying whether the symbol may be
    written.  ``unverifiable_gaps`` is the honest counterpart of ``ok``: pairs
    separated by more than :data:`SPAN_GUARANTEE_DAYS`, where the compounding
    tolerance has grown past the smallest real convention factor and a pass
    therefore proves nothing.
    """
    breaks: list[dict] = []
    unverifiable: list[dict] = []
    max_ratio = 1.0
    max_ratio_at: Optional[str] = None
    max_gap = 0
    for (prev_day, _, prev_value), (day, _, value) in zip(kept, kept[1:]):
        if prev_value <= 0 or value <= 0:  # unreachable: the parser drops these
            continue
        gap = (day - prev_day).days
        ratio = max(value / prev_value, prev_value / value)
        tolerance = span_tolerance_ratio(gap)
        max_gap = max(max_gap, gap)
        if ratio > max_ratio:
            max_ratio = ratio
            max_ratio_at = day.isoformat()
        entry = {
            "from": prev_day.isoformat(),
            "to": day.isoformat(),
            "gap_days": gap,
            "from_value": round(prev_value, 4),
            "to_value": round(value, 4),
            "ratio": round(ratio, 4),
            "tolerance_ratio": round(tolerance, 4),
        }
        if ratio > tolerance:
            breaks.append(entry)
        elif gap > SPAN_GUARANTEE_DAYS:
            unverifiable.append(entry)
    return {
        "bars": len(kept),
        "compared_pairs": max(len(kept) - 1, 0),
        "max_ratio": round(max_ratio, 4),
        "max_ratio_at": max_ratio_at,
        "max_gap_days": max_gap,
        "guarantee_days": SPAN_GUARANTEE_DAYS,
        "break_count": len(breaks),
        "breaks": breaks[:MAX_REPORTED_BREAKS],
        "unverifiable_gap_count": len(unverifiable),
        "unverifiable_gaps": unverifiable[:MAX_REPORTED_BREAKS],
        "ok": not breaks,
    }


def _check_join(
    last_day: date,
    last_value: float,
    reference_at: datetime,
    reference_value: float,
) -> dict:
    """Compare the last backfilled value with the first trusted live value.

    Returns the full comparison either way — a passing join is evidence too,
    and the caller reports it — with ``ok`` saying whether the symbol may be
    written.  Both values are positive: the parser drops non-positive closes,
    :func:`live_anchor` selects on ``value > 0``, and the caller re-checks (a
    zero denominator here would put a non-finite number into a JSON response).
    """
    gap_days = (reference_at.date() - last_day).days
    threshold = join_threshold_pct(gap_days)
    jump = abs(reference_value - last_value) / last_value * 100.0
    return {
        "last_backfill_date": last_day.isoformat(),
        "last_backfill_value": round(last_value, 4),
        "first_live_at": reference_at.isoformat(),
        "first_live_value": round(reference_value, 4),
        "gap_days": gap_days,
        "jump_pct": round(jump, 4),
        "threshold_pct": threshold,
        "ok": jump <= threshold,
    }


class Skips(NamedTuple):
    """Why parsed bars did not make it into the write set."""

    at_or_after_cutoff: int
    non_positive: int
    duplicate_day: int


def _rows_to_write(
    slug: str,
    pairs: Sequence[tuple[date, float]],
    cutoff: datetime,
) -> tuple[list[tuple[date, float, float]], Skips]:
    """Normalize and keep only bars that fall wholly before the live era.

    Returns ``([(day, raw_rial, normalized_toman)], skips)``, where ``skips``
    counts each reason separately so the caller can tell "everything is
    already covered" (the idempotent success) from "everything was thrown
    away" (a failure that must not be reported as success).

    The rule is by DAY, not by timestamp: a bar is kept only when its own date
    is strictly earlier than the UTC date of the first live observation.  That
    is deliberately stricter than "stamped before ``min(observed_at)``", and
    the extra strictness is the whole point of the guard.  A first live
    observation late in its UTC day (say 23:30) would leave 23:00 on that same
    day "strictly before the cutoff", so the timestamp rule alone would write
    a daily close INTO a day the live collector already covers — precisely the
    second-same-day-observation hazard from another source that changes candle
    aggregation and manufactures provider-gap dispersion.  Excluding the
    boundary day entirely costs one day of history and cannot do that.
    """
    kept: list[tuple[date, float, float]] = []
    seen: set[date] = set()
    at_or_after = 0
    non_positive = 0
    duplicate = 0
    for day, raw_close in pairs:
        if not (isinstance(raw_close, (int, float)) and raw_close > 0):
            non_positive += 1
            continue
        if day >= cutoff.date():
            at_or_after += 1
            continue
        if day in seen:  # a duplicated bar inside one payload
            duplicate += 1
            continue
        seen.add(day)
        kept.append((day, float(raw_close), normalize_history_value(slug, raw_close)))
    kept.sort(key=lambda item: item[0])
    return kept, Skips(at_or_after, non_positive, duplicate)


def splice_note(
    symbol: str,
    last_backfilled: date,
    anchor: LiveAnchor,
) -> str:
    """The sentence an instrument's note carries about its own splice.

    Written for a person reading the instrument in the UI, not for a parser:
    where the definition changes, in which direction, and what that costs a
    statistic measured across it.
    """
    before, after = SPLICE_DEFINITIONS[symbol]
    live_source = anchor.cutoff_source or "the live collector"
    return (
        f"{SPLICE_NOTE_MARKER} The definition of this series CHANGES on "
        f"{anchor.cutoff.date().isoformat()}. Up to {last_backfilled.isoformat()} "
        f"it is deep history backfilled from TGJU (source '{SOURCE}'): {before}. "
        f"From {anchor.cutoff.isoformat()} it is the live era (source "
        f"'{live_source}'): {after}. Both measure the same underlying thing, "
        f"but they are not the same measurement, so any return, volatility or "
        f"real-value statistic computed ACROSS that date mixes two conventions "
        f"and should say so."
    )


def _splice_record(
    symbol: str,
    first_backfilled: date,
    last_backfilled: date,
    anchor: LiveAnchor,
    rows: int,
    now: datetime,
) -> dict:
    """The machine-readable register entry for one symbol's splice."""
    before, after = SPLICE_DEFINITIONS[symbol]
    return {
        "symbol": symbol,
        "splice_at": anchor.cutoff.isoformat(),
        "backfill_first_bar": first_backfilled.isoformat(),
        "backfill_last_bar": last_backfilled.isoformat(),
        "backfill_rows": int(rows),
        "backfill_source": SOURCE,
        "backfill_definition": before,
        "live_source": anchor.cutoff_source,
        "live_definition": after,
        "join_reference_source": anchor.reference_source,
        "join_reference_at": (
            anchor.reference_at.isoformat() if anchor.reference_at else None
        ),
        "recorded_by": "tgju_backfill",
        "recorded_at": now.isoformat(),
        "note": splice_note(symbol, last_backfilled, anchor),
    }


def _write_splice_record(conn: Connection, record: dict) -> bool:
    """Persist one splice: the register row, then the instrument note.

    Returns whether the ``instruments`` row was updated.  A missing instrument
    row is not fatal — the register still holds the splice — but it IS
    reported, because the note is the half a person actually sees.
    """
    symbol = record["symbol"]
    now = utcnow()
    existing = conn.execute(
        select(app_settings.c.value).where(app_settings.c.key == SPLICE_SETTINGS_KEY)
    ).scalar()
    register = dict(existing) if isinstance(existing, dict) else {}
    register[symbol] = record
    updated = conn.execute(
        update(app_settings)
        .where(app_settings.c.key == SPLICE_SETTINGS_KEY)
        .values(value=register, updated_at=now)
    )
    if updated.rowcount == 0:
        conn.execute(
            app_settings.insert().values(
                key=SPLICE_SETTINGS_KEY, value=register, updated_at=now
            )
        )

    # .first() rather than .scalar(): a missing instruments row and a row with
    # an empty note are different facts, and only the first one is reportable.
    row = conn.execute(
        select(instruments.c.notes).where(instruments.c.code == symbol)
    ).first()
    if row is None:
        return False
    # Everything from the marker on belongs to this job; keep whatever a human
    # (or migration 0024) wrote before it.
    kept = str(row[0] or "").split(SPLICE_NOTE_MARKER, 1)[0].strip()
    notes = f"{kept} {record['note']}".strip() if kept else record["note"]
    result = conn.execute(
        update(instruments)
        .where(instruments.c.code == symbol)
        .values(notes=notes, updated_at=now)
    )
    return bool(result.rowcount)


def backfill_symbol(
    engine: Engine,
    settings: Settings,
    symbol: str,
    dry_run: bool = False,
    ignore_sources: Sequence[str] = (),
) -> dict:
    """Gap-fill one symbol's deep history; returns a per-symbol report.

    Never raises for an expected failure: a refusal is a reported outcome, so
    one bad symbol cannot sink the others in a multi-symbol call.  Every
    non-writing outcome reports ``refused`` or ``error``; ``up_to_date`` is
    reserved for the one case that genuinely is one.
    """
    report: dict = {
        "symbol": symbol,
        "slug": BACKFILL_SLUGS.get(symbol),
        "status": "refused",
        "dry_run": bool(dry_run),
        "fetched": 0,
        "eligible": 0,
        "skipped_at_or_after_cutoff": 0,
        "inserted": 0,
        "raw_inserted": 0,
        "would_insert": 0,
        "join": None,
        "coherence": None,
        "splice": None,
    }
    try:
        slug = BACKFILL_SLUGS.get(symbol)
        if slug is None or symbol not in SYMBOL_META:
            raise BackfillRefused(f"{symbol} is not a TGJU backfill symbol")

        anchor = live_anchor(engine, symbol, ignore_sources)
        if anchor is None:
            # No live era means no anchor, and without an anchor the join
            # check — the only test that can catch a scale or convention
            # mismatch between our era and TGJU's — cannot run at all.
            # Refusing is the conservative branch: these three symbols all
            # HAVE live history (that is the premise of the job), so this is a
            # sign something is wrong, not a case to write blind through.
            raise BackfillRefused(
                "no live history for this symbol, so the join sanity check "
                "has nothing to verify the backfill against"
            )
        report["live_cutoff"] = anchor.cutoff.isoformat()
        report["live_cutoff_source"] = anchor.cutoff_source
        report["live_cutoff_quality"] = anchor.cutoff_quality
        if anchor.reference_at is None or anchor.reference_value is None:
            excluded = sorted(UNTRUSTED_ANCHOR_SOURCES.union(ignore_sources))
            raise BackfillRefused(
                f"prices holds {anchor.rows} row(s) for this symbol but none "
                f"is a trusted live observation: they are non-ok, non-positive, "
                f"or from a source this job will not join against "
                f"({', '.join(excluded)}). The join sanity check has nothing to "
                f"verify the backfill against"
            )
        report["first_live_value"] = round(anchor.reference_value, 4)
        report["first_live_at"] = anchor.reference_at.isoformat()
        report["first_live_source"] = anchor.reference_source

        window = bar_date_window()
        if not (window[0] <= anchor.cutoff.date() <= window[1]):
            # A row dated outside the plausible window is exactly what an
            # unbounded backfill writes (module docstring).  Left unsaid, it
            # makes every later run report 'up_to_date' while nothing can ever
            # be written again.
            raise BackfillRefused(
                f"the oldest row in prices for this symbol is dated "
                f"{anchor.cutoff.date().isoformat()} (source "
                f"{anchor.cutoff_source!r}, quality {anchor.cutoff_quality!r}), "
                f"outside the plausible window {window[0].isoformat()}.."
                f"{window[1].isoformat()}. That row is blocking every eligible "
                f"bar; delete or repair it rather than reading this job's "
                f"empty result as 'up to date'"
            )

        provider = TGJUProvider(
            courtesy_delay=settings.provider_courtesy_delay,
            backoff_base=settings.provider_backoff_base,
            timeout=settings.http_timeout_seconds,
        )
        try:
            page = provider.fetch_history_page(slug, max_rows=MAX_HISTORY_ROWS)
        except Exception as exc:  # noqa: BLE001 — one symbol must not sink the job
            log.warning("tgju history fetch failed for %s (%s): %s", symbol, slug, exc)
            report["status"] = "error"
            report["reason"] = str(exc)
            return report

        report["fetched"] = len(page.rows)
        report["records_total"] = page.records_total
        truncated = _truncation_error(page, MAX_HISTORY_ROWS)
        if truncated:
            raise BackfillRefused(truncated)

        bad_dates = _bar_date_error(page.rows, window)
        if bad_dates:
            raise BackfillRefused(bad_dates)

        kept, skips = _rows_to_write(slug, page.rows, anchor.cutoff)
        report["eligible"] = len(kept)
        report["skipped_at_or_after_cutoff"] = skips.at_or_after_cutoff
        report["skipped_duplicate_day"] = skips.duplicate_day
        report["skipped_non_positive"] = skips.non_positive
        if not kept:
            if page.rows and skips.at_or_after_cutoff == len(page.rows):
                # The idempotent path, and the only one that earns the name:
                # every bar TGJU has is already covered by the live era or by
                # a previous run of this job.
                report["status"] = "up_to_date"
                return report
            raise BackfillRefused(
                f"no bar was eligible and this is not an up-to-date series: "
                f"{len(page.rows)} parsed bar(s), {skips.at_or_after_cutoff} at "
                f"or after the {anchor.cutoff.date().isoformat()} cutoff, "
                f"{skips.non_positive} non-positive, {skips.duplicate_day} "
                f"duplicated. Refusing rather than reporting an empty write as "
                f"success"
            )

        first_day, _, _ = kept[0]
        last_day, _, last_value = kept[-1]
        if last_value <= 0:  # unreachable via the parser; guards the ratio below
            raise BackfillRefused(
                f"last backfilled close for {last_day.isoformat()} is not "
                f"positive ({last_value}); refusing to join on it"
            )
        report["first"] = first_day.isoformat()
        report["last"] = last_day.isoformat()

        coherence = span_breaks(kept)
        report["coherence"] = coherence
        if not coherence["ok"]:
            worst = coherence["breaks"][0]
            raise BackfillRefused(
                f"the backfilled span is not internally coherent: "
                f"{coherence['break_count']} bar-to-bar discontinuit(ies) over "
                f"{coherence['compared_pairs']} pair(s), the first from "
                f"{worst['from']} ({worst['from_value']}) to {worst['to']} "
                f"({worst['to_value']}) — a {worst['ratio']}x move across "
                f"{worst['gap_days']} day(s), over the "
                f"{worst['tolerance_ratio']}x tolerance. A scale or convention "
                f"break inside the history is invisible at the live join and "
                f"corrupts every return computed across it; nothing written "
                f"for this symbol"
            )

        join = _check_join(
            last_day, last_value, anchor.reference_at, anchor.reference_value
        )
        report["join"] = join
        if not join["ok"]:
            raise BackfillRefused(
                f"join sanity check failed: last backfilled close "
                f"{join['last_backfill_value']} on {join['last_backfill_date']} vs "
                f"first live value {join['first_live_value']} at "
                f"{join['first_live_at']} is a {join['jump_pct']}% jump across "
                f"{join['gap_days']} day(s), over the {join['threshold_pct']}% "
                f"threshold; nothing written for this symbol"
            )

        report["would_insert"] = len(kept)
        now = utcnow()
        splice = _splice_record(symbol, first_day, last_day, anchor, len(kept), now)
        report["splice"] = dict(splice, recorded=False, instrument_note_updated=False)
        if dry_run:
            report["status"] = "dry_run"
            return report

        currency, unit = SYMBOL_META[symbol]
        _, raw_unit, raw_currency = SLUG_MAP[slug]
        price_rows: list[dict] = []
        raw_rows: list[dict] = []
        for day, raw_close, value in kept:
            stamp = close_stamp(day)
            price_rows.append({
                "symbol": symbol, "value": value, "currency": currency,
                "unit": unit, "source": SOURCE, "observed_at": stamp,
                "collected_at": now, "quality": "ok",
            })
            raw_rows.append({
                "provider_code": PROVIDER_CODE, "symbol": symbol,
                # The provider's own number, in the provider's own unit: this
                # row is the audit trail for the ÷10 above, and it is only an
                # audit trail if it holds what TGJU actually published.
                "raw_value": raw_close, "unit": raw_unit, "currency": raw_currency,
                "raw_payload": {
                    "slug": slug, "kind": "history_backfill",
                    "bar_date": day.isoformat(),
                    "availability_utc": stamp.isoformat(),
                    "normalization": "rial_to_toman",
                    "normalized_value": value,
                    "normalized_currency": currency,
                },
                "observed_at": stamp, "collected_at": now, "quality": "ok",
                "dedupe_key": f"{PROVIDER_CODE}|{symbol}|history|{day.isoformat()}",
            })

        with engine.begin() as conn:
            inserted = insert_ignore(conn, prices, price_rows)
            raw_inserted = insert_ignore(conn, raw_observations, raw_rows)
            # Same transaction as the rows themselves: a splice that exists in
            # `prices` but not in the register is exactly the invisible join
            # this job is supposed to prevent.
            note_updated = _write_splice_record(conn, splice)
        report["inserted"] = int(inserted)
        report["raw_inserted"] = int(raw_inserted)
        report["splice"] = dict(
            splice, recorded=True, instrument_note_updated=bool(note_updated)
        )
        report["status"] = "ok"
        if not note_updated:
            log.warning(
                "tgju backfill %s: splice recorded in app_settings but no "
                "instruments row exists to carry the note", symbol,
            )
        log.info(
            "tgju backfill %s: %d rows %s..%s (join %.2f%% <= %.1f%%, span max "
            "%.2fx over %d pairs)",
            symbol, inserted, report["first"], report["last"],
            join["jump_pct"], join["threshold_pct"],
            coherence["max_ratio"], coherence["compared_pairs"],
        )
        return report
    except BackfillRefused as exc:
        # WARNING and above is mirrored into app_issues by core.issues: a
        # refusal must be visible to an operator, not just to the caller who
        # happened to read the response body.
        log.warning("tgju backfill refused for %s: %s", symbol, exc)
        report["status"] = "refused"
        report["reason"] = str(exc)
        return report


def run_tgju_backfill(
    engine: Engine,
    settings: Settings,
    symbols: Optional[Sequence[str]] = None,
    dry_run: bool = False,
    ignore_sources: Optional[Sequence[str]] = None,
) -> dict:
    """Gap-fill deep TGJU history for the requested symbols (empty = all three).

    Raises ``ValueError`` for a symbol this job will not handle, so the caller
    gets a 400 naming it: a symbol silently dropped from the requested list is
    how an operator comes to believe a backfill happened that never did.

    ``ignore_sources`` is passed through to :func:`live_anchor` as the
    operator override for a junk join reference; it can never move the write
    boundary.
    """
    requested = list(symbols) if symbols else list(BACKFILL_SLUGS)
    unknown = [s for s in requested if s not in BACKFILL_SLUGS]
    if unknown:
        details = "; ".join(
            f"{s}: {REFUSED_SYMBOLS[s]}" if s in REFUSED_SYMBOLS
            else f"{s}: not a TGJU-backed Iranian symbol"
            for s in unknown
        )
        raise ValueError(
            f"unsupported symbol(s) for tgju history backfill: {details}. "
            f"Supported: {', '.join(sorted(BACKFILL_SLUGS))}"
        )

    ignored = list(ignore_sources or ())
    # De-duplicate while keeping the caller's order.
    ordered = list(dict.fromkeys(requested))
    results = [
        backfill_symbol(engine, settings, s, dry_run=dry_run, ignore_sources=ignored)
        for s in ordered
    ]
    total = sum(int(r.get("inserted", 0)) for r in results)
    if total and not dry_run:
        JOB_LAST_SUCCESS.labels(job="tgju_backfill").set(time.time())
    return {
        "dry_run": bool(dry_run),
        "ignore_sources": ignored,
        "symbols": results,
        "total_inserted": total,
        "total_would_insert": sum(int(r.get("would_insert", 0)) for r in results),
        "refused": [
            {"symbol": r["symbol"], "reason": r.get("reason", "")}
            for r in results
            if r.get("status") in ("refused", "error")
        ],
    }
