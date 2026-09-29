"""TGJU daily settled closes for the Iranian instruments nothing collects live.

Why this exists
---------------
Migration 0031 registers seven Iranian instruments — silver 999 per gram, the
Bahar Azadi, half, quarter and gram coins, 24k gold per gram and melted gold
per mesghal — whose only source in this system is TGJU's daily history table.
No live provider in the roster publishes silver at all (BrsApi has none, the
pricedb mirror stopped on 2026-04-26 and TGJU's own ``silver`` slug hangs), and
for the rest one settled close per day is exactly what Relative value and
Purchasing power compare.  This job keeps those seven series current from
``summary-table-data/{slug}``, the endpoint ``jobs/tgju_backfill.py`` already
reads, and it is scheduled by the Go API twice a day.

It also fills ONE hole in an existing series.  ``IR_COIN_EMAMI`` has no row of
any source on 83 UTC days, 2026-04-28 → 2026-07-20 (measured on production
2026-09-29), between the pricedb mirror going stale and live collection
starting.  TGJU's ``sekee`` close is written on exactly those days and on no
other: a day that already holds an observation keeps it.  ``USD_IRT`` and
``IR_GOLD_18K`` are refused rather than filled — production's USD_IRT is the
USDT/toman market, a different instrument from TGJU's cash dollar, and 18k has
no hole to fill.

What a stored row claims, and why it can claim it
-------------------------------------------------
* **Units.** TGJU publishes rials.  The close is divided by 10 through
  :func:`providers.tgju.normalize_history_value` — the one function the live
  path and the deep backfill use — and stored as toman.  A coin is priced per
  coin, silver and 24k per gram, melted gold per MESGHAL; nothing here
  converts between them.
* **Time.** Each close is stamped 23:00 UTC on its own Gregorian date
  (:func:`jobs.tgju_backfill.close_stamp`): 02:30 Tehran the next morning,
  after the bazaar has closed, and still inside the bar's own UTC day.  Only
  days strictly before the current TEHRAN date are written, and never a bar
  whose stamp is still in the future — between 20:30 and 24:00 UTC Tehran is
  already on the next date while yesterday's 23:00 stamp has not arrived yet.
  A day's value is therefore never available here before that day settled.
* **Audit.** Every stored close is written with a ``raw_observations`` row
  holding TGJU's own rial number, the cell exactly as printed ("5,065,700")
  and its Jalali date, under the same dedupe key the deep backfill uses for
  the same fact (``tgju|<symbol>|history|<date>``).

Junk bars are held, not stored
------------------------------
TGJU's silver table contains closes no market printed: 8,000 rial on
2021-08-10 and 8,200 on 2021-08-29 against ~200,000 on either side, and
2,008,800 on 2022-09-30 — ten times its neighbours.  A close is SUSPECT when it
is more than :data:`SUSPECT_RATIO` away, in either direction, from the median
of the :data:`NEIGHBOURS_EACH_SIDE` closes on each side of it in the same
table.  Measured 2026-09-29 over the WHOLE of all ten TGJU tables this module
and the backfill read, that rule flags exactly those three silver closes and
nothing else.  Why these two numbers:

* the median of up to fourteen neighbours ignores a single junk bar among
  them, where a "previous close" test would flag the honest bar AFTER the junk
  one as well;
* x1.6 sits above the worst honest session the backfill's coherence check
  allows for (x1.5, three times the worst crisis session on record) and far
  below the smallest junk signature measured (x10 and x25).

A suspect close goes to ``raw_observations`` as quality ``'suspect'`` with the
median it was judged against, and never to ``prices`` — the collect job's own
rule for an uncorroborated value, and the one every ``prices`` reader relies
on (not all of them filter on quality).  It is re-judged on every later run
while it is still inside the fetched window, so an honest level shift — which
looks like junk only while nothing after it has been published — is written
once its later neighbours exist.  A bar with fewer than
:data:`MIN_JUDGED_NEIGHBOURS` neighbours cannot be judged at all and is held
the same way.

Never overwrite
---------------
TGJU can restate a past close.  A refetched close that differs from the
``tgju_history`` row already stored for that day is COUNTED as a restatement,
reported with both values, logged, and left alone: a stored series that
changes under a reader without a trace is worse than one that is known to
disagree with its source.  Re-running changes nothing, and ``dry_run`` runs
every step above and writes nothing.

How much is fetched
-------------------
The whole table (``length`` far above the longest one, then checked against
TGJU's own ``recordsTotal``, refusing a truncated answer) when a symbol has no
``tgju_history`` row yet or this job has never completed a full pass for it —
the Emami coin's first pass is what reaches its 83-day hole.  Otherwise the
:data:`RECENT_ROWS` newest rows, which is about six weeks; and if even those do
not reach back to the newest bar the previous pass saw, rows in between were
never seen (the job was down for weeks), so the whole table is read again.
Never ``length=-1``: measured, it silently drops the oldest row.
"""
from __future__ import annotations

import logging
import math
import statistics
import time
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple, Optional, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
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
    HistoryPage,
    HistoryRow,
    TGJUProvider,
    normalize_history_value,
    slug_meta,
)
# The deep backfill's guards and conventions, reused rather than restated: the
# same length cap and recordsTotal check, the same Jalali-in-the-Gregorian-
# column refusal, the same source label, audit key scheme and 23:00 UTC stamp.
from .tgju_backfill import (
    MAX_HISTORY_ROWS,
    PROVIDER_CODE,
    SOURCE,
    SPLICE_NOTE_MARKER,
    _bar_date_error,
    _truncation_error,
    bar_date_window,
    close_stamp,
)

log = logging.getLogger(__name__)

TEHRAN = ZoneInfo("Asia/Tehran")

# The seven series migration 0031 registers: symbol -> TGJU slug.  Order is the
# order they are fetched and reported in.
SERIES_SLUGS: dict[str, str] = {
    "IR_SILVER_999": "silver_999",
    "IR_COIN_BAHAR": "sekeb",
    "IR_COIN_HALF": "nim",
    "IR_COIN_QUARTER": "rob",
    "IR_COIN_GERAMI": "gerami",
    "IR_GOLD_24K": "geram24",
    "IR_GOLD_MESGHAL": "mesghal",
}

# Existing series whose EMPTY UTC days are filled from TGJU, and only those.
GAP_FILL_SLUGS: dict[str, str] = {
    "IR_COIN_EMAMI": "sekee",
}

# Symbols a caller might reasonably ask for and this job will not touch, with
# the reason the 400 carries.
REFUSED_SYMBOLS: dict[str, str] = {
    "USD_IRT": (
        "USD_IRT in this deployment is the 24/7 USDT/toman market, a different "
        "instrument from TGJU's price_dollar_rl cash dollar; filling its days "
        "with TGJU closes would splice two instruments without saying so."
    ),
    "IR_GOLD_18K": (
        "IR_GOLD_18K is collected live around the clock and has no empty day "
        "to fill; its deep history is jobs/tgju_backfill.py's, which records "
        "its own splice."
    ),
}

# Rows requested on an incremental pass: about six weeks of bars, far more
# than the two runs a day ever need, so one missed run costs nothing.
RECENT_ROWS = 40

# --- the junk-bar rule (module docstring) ------------------------------------
NEIGHBOURS_EACH_SIDE = 7
SUSPECT_RATIO = 1.6
# Fewer neighbours than this and the median is not a reference worth judging
# against; the bar is held, not guessed at.  Only a table of a handful of rows
# can produce it.
MIN_JUDGED_NEIGHBOURS = 4

# Stored-versus-refetched equality.  Both sides are the same rial integer / 10;
# anything beyond floating-point noise is a real restatement.
RESTATEMENT_ABS_TOLERANCE = 0.005

# How many examples of each counted outcome the report carries.
MAX_REPORTED = 10

REGISTER_KEY = "tgju_daily"
# Everything from this marker up to the backfill's own splice marker (or the
# end of the note) belongs to this job.  It is written BEFORE the splice
# marker because tgju_backfill keeps only what precedes its marker when it
# rewrites its own sentence; placed after, this sentence would be erased by
# the next backfill.
GAP_FILL_NOTE_MARKER = "[tgju daily gap-fill]"


class DailyRefused(Exception):
    """A symbol failed a guard; nothing is written for it."""


class Judged(NamedTuple):
    """One bar and the verdict of the junk-bar rule on it."""

    bar: HistoryRow
    verdict: str  # 'ok' | 'suspect' | 'unjudged'
    median: Optional[float]
    neighbours: int


def tehran_today(now: datetime) -> date:
    """The calendar date in Tehran at ``now``: the day still in progress."""
    return ensure_utc(now).astimezone(TEHRAN).date()


def judge_bars(bars: Sequence[HistoryRow]) -> list[Judged]:
    """Apply the junk-bar rule to every bar of one table, oldest first.

    Pure function.  Neighbours are the bars on either side IN THE SAME TABLE,
    by position, not by calendar day: TGJU skips holidays and the rule is
    about what the table printed around a close.
    """
    ordered = sorted(bars, key=lambda b: b.day)
    closes = [b.close for b in ordered]
    out: list[Judged] = []
    for i, bar in enumerate(ordered):
        lo = max(0, i - NEIGHBOURS_EACH_SIDE)
        around = closes[lo:i] + closes[i + 1:i + 1 + NEIGHBOURS_EACH_SIDE]
        if len(around) < MIN_JUDGED_NEIGHBOURS:
            out.append(Judged(bar, "unjudged", None, len(around)))
            continue
        median = statistics.median(around)
        if median <= 0:  # unreachable: the parser keeps positive closes only
            out.append(Judged(bar, "unjudged", None, len(around)))
            continue
        ratio = max(bar.close / median, median / bar.close)
        verdict = "suspect" if ratio > SUSPECT_RATIO else "ok"
        out.append(Judged(bar, verdict, median, len(around)))
    return out


def _same_value(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=1e-9, abs_tol=RESTATEMENT_ABS_TOLERANCE)


def _read_register(conn: Connection) -> dict:
    value = conn.execute(
        select(app_settings.c.value).where(app_settings.c.key == REGISTER_KEY)
    ).scalar()
    return dict(value) if isinstance(value, dict) else {}


def _write_register(conn: Connection, register: dict, now: datetime) -> None:
    updated = conn.execute(
        update(app_settings)
        .where(app_settings.c.key == REGISTER_KEY)
        .values(value=register, updated_at=now)
    )
    if updated.rowcount == 0:
        conn.execute(
            app_settings.insert().values(key=REGISTER_KEY, value=register, updated_at=now)
        )


def _has_history_rows(conn: Connection, symbol: str) -> bool:
    return conn.execute(
        select(prices.c.id)
        .where(prices.c.symbol == symbol, prices.c.source == SOURCE)
        .limit(1)
    ).first() is not None


def _day_bounds(first: date, last: date) -> tuple[datetime, datetime]:
    lo = datetime(first.year, first.month, first.day, tzinfo=timezone.utc)
    hi = datetime(last.year, last.month, last.day, tzinfo=timezone.utc) + timedelta(days=1)
    return lo, hi


def _existing_by_day(
    conn: Connection, symbol: str, first: date, last: date
) -> dict[date, dict[str, float]]:
    """What ``prices`` already holds for ``symbol``, per UTC day and source.

    Every quality counts: the hazard of a second same-day observation from
    another source is caused by the row existing, not by it being good (the
    same reasoning as tgju_backfill.LiveAnchor.cutoff).  For this job's own
    source the value is kept so a refetch can be compared with it.
    """
    lo, hi = _day_bounds(first, last)
    rows = conn.execute(
        select(prices.c.observed_at, prices.c.source, prices.c.value)
        .where(
            prices.c.symbol == symbol,
            prices.c.observed_at >= lo,
            prices.c.observed_at < hi,
        )
        .order_by(prices.c.observed_at.asc())
    ).all()
    out: dict[date, dict[str, float]] = {}
    for observed_at, source, value in rows:
        day = ensure_utc(observed_at).date()
        out.setdefault(day, {})[str(source)] = float(value)
    return out


def _gap_fill_note(slug: str, stats: dict) -> str:
    """The sentence a gap-filled instrument's note carries about those days."""
    return (
        f"{GAP_FILL_NOTE_MARKER} On {int(stats['days'])} UTC day(s) that held no "
        f"observation from any source ({stats['first']} to {stats['last']}), the "
        f"value is TGJU's published {slug} daily close (source '{SOURCE}', stamped "
        f"23:00 UTC), not a live quote. Days that already held an observation "
        f"were left as they were."
    )


def _write_gap_fill_note(conn: Connection, symbol: str, sentence: str, now: datetime) -> bool:
    """Put this job's sentence into the instrument note, before any splice note.

    Returns whether an ``instruments`` row was updated.  Text a human, a
    migration or the deep backfill wrote is kept; only this job's own previous
    sentence is replaced.
    """
    row = conn.execute(
        select(instruments.c.notes).where(instruments.c.code == symbol)
    ).first()
    if row is None:
        return False
    note = str(row[0] or "")
    head, marker, splice = note.partition(SPLICE_NOTE_MARKER)
    kept = head.split(GAP_FILL_NOTE_MARKER, 1)[0].strip()
    head = f"{kept} {sentence}".strip() if kept else sentence
    notes = f"{head} {marker}{splice}" if marker else head
    result = conn.execute(
        update(instruments)
        .where(instruments.c.code == symbol)
        .values(notes=notes, updated_at=now)
    )
    return bool(result.rowcount)


def _fetch(provider: TGJUProvider, slug: str, full: bool) -> HistoryPage:
    return provider.fetch_history_page(
        slug, max_rows=MAX_HISTORY_ROWS if full else RECENT_ROWS
    )


def ingest_symbol(
    engine: Engine,
    settings: Settings,
    symbol: str,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> dict:
    """One symbol's pass; returns its report.

    Never raises for an expected failure: a refusal or a failed fetch is a
    reported outcome, so one slug cannot sink the others.
    """
    now = ensure_utc(now or utcnow())
    gap_fill = symbol in GAP_FILL_SLUGS
    slug = GAP_FILL_SLUGS[symbol] if gap_fill else SERIES_SLUGS[symbol]
    today = tehran_today(now)
    report: dict = {
        "symbol": symbol,
        "slug": slug,
        "role": "gap_fill" if gap_fill else "series",
        "status": "refused",
        "dry_run": bool(dry_run),
        "mode": None,
        "full_history_reason": None,
        "fetched": 0,
        "records_total": None,
        "tehran_today": today.isoformat(),
        "inserted": 0,
        "raw_inserted": 0,
        "would_insert": 0,
        "unchanged": 0,
        "restated": 0,
        "restatements": [],
        "suspect": 0,
        "suspects": [],
        "held_unjudged": 0,
        "skipped_not_settled": 0,
        "skipped_other_source": 0,
        "first_written": None,
        "last_written": None,
    }
    try:
        currency, unit = SYMBOL_META[symbol]
        _, raw_unit, raw_currency = slug_meta(slug)
        if raw_unit.split("/", 1)[1] != unit:
            # A mesghal price stored per gram is a 4.6x error that no range
            # check catches; the two tables must agree before anything runs.
            raise DailyRefused(
                f"unit mismatch: TGJU {slug} is {raw_unit} but {symbol} is stored "
                f"per {unit}"
            )

        with engine.connect() as conn:
            register = _read_register(conn)
            has_rows = _has_history_rows(conn, symbol)
        state = register.get(symbol) if isinstance(register.get(symbol), dict) else None
        full_reason: Optional[str] = None
        if not has_rows:
            full_reason = f"no '{SOURCE}' row for this symbol yet"
        elif state is None or not state.get("full_pass_at"):
            full_reason = "this job has not completed a full pass for this symbol"

        provider = TGJUProvider(
            courtesy_delay=settings.provider_courtesy_delay,
            backoff_base=settings.provider_backoff_base,
            timeout=settings.http_timeout_seconds,
        )
        try:
            page = _fetch(provider, slug, full=full_reason is not None)
            if full_reason is None and page.bars:
                seen = (state or {}).get("newest_bar_seen")
                oldest = min(b.day for b in page.bars)
                if seen and oldest > date.fromisoformat(seen):
                    full_reason = (
                        f"the {RECENT_ROWS} newest rows start {oldest.isoformat()}, "
                        f"after the newest bar the previous pass saw ({seen}); rows "
                        f"in between were never read"
                    )
                    page = _fetch(provider, slug, full=True)
        except Exception as exc:  # noqa: BLE001 — one slug must not sink the job
            log.warning("tgju daily fetch failed for %s (%s): %s", symbol, slug, exc)
            report["status"] = "error"
            report["reason"] = str(exc)
            return report

        full = full_reason is not None
        report["mode"] = "full" if full else "recent"
        report["full_history_reason"] = full_reason
        report["fetched"] = len(page.bars)
        report["records_total"] = page.records_total
        if full:
            truncated = _truncation_error(page, MAX_HISTORY_ROWS)
            if truncated:
                raise DailyRefused(truncated)
        if not page.bars:
            raise DailyRefused(f"TGJU returned no parseable bar for {slug}")
        bad_dates = _bar_date_error(page.rows, bar_date_window(now))
        if bad_dates:
            raise DailyRefused(bad_dates)

        judged = judge_bars(page.bars)
        first_day = judged[0].bar.day
        last_day = judged[-1].bar.day
        with engine.connect() as conn:
            existing = _existing_by_day(conn, symbol, first_day, last_day)

        price_rows: list[dict] = []
        raw_rows: list[dict] = []
        written_days: list[date] = []
        for item in judged:
            bar = item.bar
            stamp = close_stamp(bar.day)
            if bar.day >= today or stamp > now:
                # Not settled yet: the day is still in progress in Tehran, or
                # its 23:00 UTC availability stamp has not arrived.
                report["skipped_not_settled"] += 1
                continue
            # The provider's own factor — the one live quotes and the deep
            # backfill use — never re-derived here.
            value = normalize_history_value(slug, bar.close)
            payload = {
                "slug": slug,
                "kind": "daily_close",
                "bar_date": bar.day.isoformat(),
                "jalali_date": bar.jalali,
                "close_text": bar.close_text,
                "availability_utc": stamp.isoformat(),
                "normalization": "rial_to_toman",
                "normalized_value": value,
                "normalized_currency": currency,
            }
            on_day = existing.get(bar.day, {})
            if SOURCE in on_day and _same_value(on_day[SOURCE], value):
                # Already stored, and TGJU still says the same: there is
                # nothing to judge.  (Re-judging it would let the truncated
                # neighbourhood at the old edge of a 40-row page file a
                # spurious suspect row against a close that was fine.)
                report["unchanged"] += 1
                continue
            if item.verdict == "suspect":
                report["suspect"] += 1
                ratio = max(bar.close / item.median, item.median / bar.close)
                if len(report["suspects"]) < MAX_REPORTED:
                    report["suspects"].append({
                        "date": bar.day.isoformat(),
                        "close_rial": bar.close,
                        "neighbour_median_rial": item.median,
                        "ratio": round(ratio, 4),
                        "neighbours": item.neighbours,
                    })
                raw_rows.append(_raw_row(
                    symbol, bar, stamp, raw_unit, raw_currency, now,
                    quality="suspect",
                    payload=payload | {
                        "suspect_reason": (
                            f"close is {ratio:.2f}x away from the median of its "
                            f"{item.neighbours} neighbouring closes; more than "
                            f"{SUSPECT_RATIO}x"
                        ),
                        "neighbour_median_rial": item.median,
                    },
                    dedupe_key=(
                        f"{PROVIDER_CODE}|{symbol}|history|{bar.day.isoformat()}|"
                        f"suspect|{format(bar.close, '.10g')}"
                    ),
                ))
                continue
            if item.verdict == "unjudged":
                report["held_unjudged"] += 1
                continue

            if SOURCE in on_day:
                # TGJU now publishes a different close for a day already
                # stored.  Counted and reported; the stored row stays.
                report["restated"] += 1
                if len(report["restatements"]) < MAX_REPORTED:
                    report["restatements"].append({
                        "date": bar.day.isoformat(),
                        "stored": on_day[SOURCE],
                        "tgju_now": value,
                    })
                continue
            if on_day:
                # Another source already observed this UTC day.  A second,
                # different-source close would change which value the daily
                # close reads and fake a provider disagreement.
                report["skipped_other_source"] += 1
                continue

            written_days.append(bar.day)
            price_rows.append({
                "symbol": symbol, "value": value, "currency": currency,
                "unit": unit, "source": SOURCE, "observed_at": stamp,
                "collected_at": now, "quality": "ok",
            })
            raw_rows.append(_raw_row(
                symbol, bar, stamp, raw_unit, raw_currency, now,
                quality="ok", payload=payload,
                dedupe_key=f"{PROVIDER_CODE}|{symbol}|history|{bar.day.isoformat()}",
            ))

        if report["restated"]:
            log.warning(
                "tgju daily %s: %d stored close(s) differ from TGJU's current table "
                "and were left as stored; first %s",
                symbol, report["restated"], report["restatements"][0],
            )
        if report["suspect"]:
            log.info(
                "tgju daily %s: %d close(s) held as suspect (junk-bar rule)",
                symbol, report["suspect"],
            )

        report["would_insert"] = len(price_rows)
        if written_days:
            report["first_written"] = written_days[0].isoformat()
            report["last_written"] = written_days[-1].isoformat()
        if dry_run:
            report["status"] = "dry_run"
            return report

        with engine.begin() as conn:
            inserted = insert_ignore(conn, prices, price_rows)
            raw_inserted = insert_ignore(conn, raw_observations, raw_rows)
            # Re-read inside the transaction so two symbols' passes never
            # overwrite each other's register entries.
            register = _read_register(conn)
            entry = dict(register.get(symbol) or {})
            entry.update({
                "slug": slug,
                "role": report["role"],
                "last_pass_at": now.isoformat(),
                "last_mode": report["mode"],
                "newest_bar_seen": max(
                    last_day.isoformat(), str(entry.get("newest_bar_seen") or "")
                ),
            })
            if full:
                entry["full_pass_at"] = now.isoformat()
                entry["records_total"] = page.records_total
            if gap_fill and inserted:
                stats = dict(entry.get("gap_fill") or {})
                stats["days"] = int(stats.get("days", 0)) + int(inserted)
                stats["first"] = min(
                    filter(None, [stats.get("first"), report["first_written"]])
                )
                stats["last"] = max(
                    filter(None, [stats.get("last"), report["last_written"]])
                )
                entry["gap_fill"] = stats
                report["instrument_note_updated"] = _write_gap_fill_note(
                    conn, symbol, _gap_fill_note(slug, stats), now
                )
            register[symbol] = entry
            _write_register(conn, register, now)
        report["inserted"] = int(inserted)
        report["raw_inserted"] = int(raw_inserted)
        report["status"] = "ok"
        log.info(
            "tgju daily %s: %s pass, %d written, %d unchanged, %d restated, %d suspect",
            symbol, report["mode"], inserted, report["unchanged"],
            report["restated"], report["suspect"],
        )
        return report
    except DailyRefused as exc:
        # WARNING and above is mirrored into app_issues by core.issues.
        log.warning("tgju daily refused for %s: %s", symbol, exc)
        report["status"] = "refused"
        report["reason"] = str(exc)
        return report


def _raw_row(
    symbol: str,
    bar: HistoryRow,
    stamp: datetime,
    raw_unit: str,
    raw_currency: str,
    now: datetime,
    *,
    quality: str,
    payload: dict,
    dedupe_key: str,
) -> dict:
    """The audit row: TGJU's own number, in TGJU's own unit."""
    return {
        "provider_code": PROVIDER_CODE, "symbol": symbol,
        "raw_value": bar.close, "unit": raw_unit, "currency": raw_currency,
        "raw_payload": payload, "observed_at": stamp, "collected_at": now,
        "quality": quality, "dedupe_key": dedupe_key,
    }


class DailyIngestFailed(Exception):
    """Every attempted symbol failed; carries the full report."""

    def __init__(self, message: str, report: dict) -> None:
        super().__init__(message)
        self.report = report


def supported_symbols() -> list[str]:
    return list(SERIES_SLUGS) + list(GAP_FILL_SLUGS)


def run_tgju_daily(
    engine: Engine,
    settings: Settings,
    symbols: Optional[Sequence[str]] = None,
    dry_run: bool = False,
    now: Optional[datetime] = None,
) -> dict:
    """One pass over the requested symbols (empty = all eight).

    Raises ``ValueError`` naming any symbol this job does not handle, so the
    caller gets a 400 instead of a silently shorter list, and
    :class:`DailyIngestFailed` when EVERY symbol failed — the Go scheduler
    records job success from the status code, and a pass that stored nothing
    because everything broke must not enter that history as a success.
    """
    requested = list(symbols) if symbols else supported_symbols()
    unknown = [s for s in requested if s not in SERIES_SLUGS and s not in GAP_FILL_SLUGS]
    if unknown:
        details = "; ".join(
            f"{s}: {REFUSED_SYMBOLS[s]}" if s in REFUSED_SYMBOLS
            else f"{s}: not a TGJU daily-close symbol"
            for s in unknown
        )
        raise ValueError(
            f"unsupported symbol(s) for the TGJU daily job: {details}. "
            f"Supported: {', '.join(supported_symbols())}"
        )
    now = ensure_utc(now or utcnow())
    ordered = list(dict.fromkeys(requested))
    results = [ingest_symbol(engine, settings, s, dry_run=dry_run, now=now) for s in ordered]
    failed = [r for r in results if r["status"] in ("refused", "error")]
    out = {
        "dry_run": bool(dry_run),
        "as_of": now.isoformat(),
        "tehran_today": tehran_today(now).isoformat(),
        "symbols": results,
        "total_inserted": sum(int(r["inserted"]) for r in results),
        "total_would_insert": sum(int(r["would_insert"]) for r in results),
        "total_suspect": sum(int(r["suspect"]) for r in results),
        "total_restated": sum(int(r["restated"]) for r in results),
        "failed": [{"symbol": r["symbol"], "reason": r.get("reason", "")} for r in failed],
    }
    if results and len(failed) == len(results):
        raise DailyIngestFailed(
            f"every symbol failed ({len(failed)} of {len(results)})", out
        )
    if not dry_run:
        JOB_LAST_SUCCESS.labels(job="tgju_daily").set(time.time())
    return out
