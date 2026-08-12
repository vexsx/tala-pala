"""Measured track record of the trend alignment (Addendum 22).

This module answers one question — *what has this indicator actually done?* —
and it is built around the fact that the honest answer is not the same for
every window.

**Why there are two bases.** ``prices`` holds ticks, not exchange OHLC, and for
the evaluated symbols there was exactly ONE tick per day until the 5-minute
stream began; intraday history is therefore weeks long while daily history is
years long. The 4H and 1H legs of the alignment cannot be computed before the
intraday stream starts, so a 90-day track record of the THREE-timeframe
alignment does not exist. Rather than pad, forward-fill or interpolate the
missing intraday candles — which would fabricate the very measurement being
reported — each window is computed on the strongest basis its data supports and
says which one that was:

``full_mtf``
    The real 1D+4H+1H alignment, replayed at every 1H close. Only chosen when
    the whole window sits inside genuine intraday coverage AND all three legs
    are warmed up at the window's first bar.

``daily_only``
    The 1D leg alone — price against the same three moving averages on daily
    candles. Available for years, and never labelled as the multi-timeframe
    alignment.

**Why the replay cannot look ahead.** At each bar close the state is evaluated
by the same pure engine the live indicator uses, with ``now`` set to that bar's
close time. The engine's ``completed()`` filter then drops every candle that had
not finished at that instant, including the bar's own successors. Nothing here
re-implements a moving average or a candle: a second implementation would be a
second answer to the same question, and the backtest would stop describing the
indicator the user is actually shown.

**What is measured.** The forward return over the next 1 day from the bar's
close, attributed to the state in force at that close. The horizon is the same
for both bases so a ``full_mtf`` row and a ``daily_only`` row are comparable.
A bar whose forward observation is missing (a gap in the series, or the end of
the data) is DROPPED, never filled — so ``samples`` can be smaller than the
number of bars replayed, and the note says by how much.

**What is deliberately not measured.** No Sharpe ratio, no p-value, no
significance claim of any kind. With a few dozen overlapping bars those would be
decoration on noise. What is reported is countable: bars, episodes, mean forward
returns, the unconditional baseline over the same bars, and a plain-language
note. The baseline is not optional garnish — without it a conditional return
invites the reader to credit the indicator for a market that was rising anyway.

Pure: candles in, statistics out. No clock, no database, no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, Sequence

from .trend_alignment import (
    TIMEFRAME_SECONDS,
    WARMUP_FACTOR,
    Candle,
    TrendConfig,
    bucket_start,
    completed,
    evaluate,
    evaluate_timeframe,
    resample,
)

Basis = Literal["full_mtf", "daily_only"]
BarState = Literal["bullish", "bearish", "unaligned"]

# Reported windows, longest first (the API serves them in this order).
WINDOW_DAYS: tuple[int, ...] = (90, 60, 30, 14)

# One day forward from every bar close, on BOTH bases. A per-basis horizon
# (say 1H forward on the hourly replay) would make the two rows describe
# different questions while sitting in the same table under the same column.
FORWARD_HORIZON_SECONDS = 86400

# A UTC day counts as genuinely intraday only with at least this many distinct
# hourly buckets. The daily-only era produces exactly ONE hourly bucket per day
# — a daily observation wearing an hourly candle's clothes — and feeding those
# to the 1H and 4H legs would manufacture the intraday history this module
# exists to admit it does not have. Four is well above one and well below the
# ~24 a live stream yields, so a partial collection day still counts.
INTRADAY_MIN_BUCKETS_PER_DAY = 4

# Map the engine's vocabulary onto the three buckets this table stores. Both
# "neutral" and "unavailable" become `unaligned`: neither is a directional call,
# and the table has no column to tell them apart.
_ALIGNMENT_STATE: dict[str, BarState] = {
    "full_bullish": "bullish",
    "full_bearish": "bearish",
    "not_aligned": "unaligned",
}
_TREND_STATE: dict[str, BarState] = {
    "bullish": "bullish",
    "bearish": "bearish",
    "neutral": "unaligned",
    "unavailable": "unaligned",
}


def warmup_candles(config: TrendConfig) -> int:
    """Candles a timeframe needs before this module treats it as usable.

    The engine can compute the slow MA at exactly ``slow`` candles but flags it
    as still warming up; a basis is only chosen when every one of its legs is
    past that flag, so a row never presents a seed-dominated average as a
    measured track record.
    """
    return int(config.slow * WARMUP_FACTOR)


# --- what the data actually supports -----------------------------------------


def intraday_since(hourly: Sequence[Candle], now: datetime) -> Optional[datetime]:
    """Start of the newest unbroken run of genuinely intraday days, or None.

    Walks COMPLETE UTC days from the newest backwards and stops at the first one
    that does not clear :data:`INTRADAY_MIN_BUCKETS_PER_DAY`. The current day is
    left out of the judgement entirely rather than exempted from the threshold:
    it is still filling up, and a partial day holding one bucket is
    indistinguishable from a daily-only day holding one bucket. Ignoring it can
    only understate intraday coverage by a day, which is the safe direction —
    the alternative would let a single tick promote a window to a basis the data
    does not support.
    """
    now = now.astimezone(timezone.utc)
    day_seconds = TIMEFRAME_SECONDS["1d"]
    today = bucket_start(now, day_seconds)
    per_day: dict[datetime, int] = {}
    for candle in completed(hourly, TIMEFRAME_SECONDS["1h"], now):
        day = bucket_start(candle.start, day_seconds)
        if day >= today:
            continue
        per_day[day] = per_day.get(day, 0) + 1

    start: Optional[datetime] = None
    for day in sorted(per_day, reverse=True):
        if per_day[day] < INTRADAY_MIN_BUCKETS_PER_DAY:
            break
        start = day
    return start


@dataclass(frozen=True)
class BasisChoice:
    """The basis for one window, and the plain-language reason it was chosen."""

    basis: Basis
    reason: str
    intraday_from: Optional[datetime]


def _iso_day(value: Optional[datetime]) -> str:
    return value.date().isoformat() if value else "never"


def choose_basis(
    window_days: int,
    window_start: datetime,
    intraday_from: Optional[datetime],
    hourly: Sequence[Candle],
    daily: Sequence[Candle],
    config: TrendConfig,
) -> BasisChoice:
    """Pick the strongest basis this window's data genuinely supports.

    ``intraday_from`` is measured over the WHOLE series as of the evaluation
    instant, never as of the window start: asking "is the series intraday?" from
    inside the daily-only era would answer about that era and quietly promote a
    90-day window to the multi-timeframe basis.

    ``full_mtf`` requires all three legs to be warmed up AT THE WINDOW'S FIRST
    BAR — not merely somewhere inside it. A window that begins before the
    intraday stream would otherwise be replayed as one long "not aligned"
    stretch and labelled multi-timeframe, which reads as "the indicator said
    nothing" when the truth is "the indicator could not be computed".
    """
    warm = warmup_candles(config)
    if intraday_from is None:
        return BasisChoice(
            "daily_only",
            "there is no intraday price history at all, so the 4H and 1H legs "
            "cannot be computed for any window",
            None,
        )

    if intraday_from > window_start:
        return BasisChoice(
            "daily_only",
            f"intraday prices only begin {_iso_day(intraday_from)}, which is "
            f"inside this {window_days}-day window, so the 4H and 1H legs have "
            "no history reaching back that far",
            intraday_from,
        )

    # Count real candles rather than doing time arithmetic: a gap in collection
    # means fewer candles than the elapsed span implies, and the MAs are fed by
    # candles, not by elapsed time.
    usable_hourly = [c for c in hourly if c.start >= intraday_from]
    hourly_ready = len(completed(usable_hourly, TIMEFRAME_SECONDS["1h"], window_start))
    four_hour_ready = len(
        completed(
            resample(usable_hourly, TIMEFRAME_SECONDS["4h"]),
            TIMEFRAME_SECONDS["4h"],
            window_start,
        )
    )
    daily_ready = len(completed(daily, TIMEFRAME_SECONDS["1d"], window_start))

    short = [
        f"{name} ({have}/{warm} candles)"
        for name, have in (("4H", four_hour_ready), ("1H", hourly_ready), ("1D", daily_ready))
        if have < warm
    ]
    if short:
        return BasisChoice(
            "daily_only",
            "at the start of this window the "
            + ", ".join(short)
            + " leg had too little history to warm up the "
            f"{config.ma_type.upper()}{config.slow}",
            intraday_from,
        )
    return BasisChoice(
        "full_mtf",
        f"intraday prices from {_iso_day(intraday_from)} cover this "
        f"{window_days}-day window with all three legs warmed up",
        intraday_from,
    )


# --- the replay ---------------------------------------------------------------


@dataclass(frozen=True)
class ReplayBar:
    """One bar close: what the indicator said, and what it cost to say it."""

    close_time: datetime
    price: float
    state: BarState
    forward_return_pct: Optional[float] = None


def _close_by_close_time(
    candles: Sequence[Candle], seconds: int
) -> dict[datetime, float]:
    """Bucket CLOSE time -> closing price, for exact forward-horizon lookups."""
    delta = timedelta(seconds=seconds)
    return {candle.start + delta: candle.close for candle in candles}


def replay(
    symbol: str,
    basis: Basis,
    hourly: Sequence[Candle],
    daily: Sequence[Candle],
    window_start: datetime,
    now: datetime,
    config: TrendConfig,
) -> list[ReplayBar]:
    """Walk the window bar by bar, deciding each bar from its own past only.

    ``now`` is passed to the engine as the bar's close time, which is precisely
    what the engine's ``now`` parameter is for: everything that had not closed
    by that instant — including every later bar — is dropped before a single
    average is taken. This is the whole reason no state can be contaminated by a
    price that had not printed yet.

    The forward return is looked up at EXACTLY one horizon later. A bar whose
    horizon lands on a hole in the series gets ``None`` and is excluded from
    every statistic; nothing is carried forward to cover the hole.
    """
    now = now.astimezone(timezone.utc)
    window_start = window_start.astimezone(timezone.utc)
    seconds = TIMEFRAME_SECONDS["1h" if basis == "full_mtf" else "1d"]
    series = completed(hourly if basis == "full_mtf" else daily, seconds, now)
    forward = _close_by_close_time(series, seconds)
    horizon = timedelta(seconds=FORWARD_HORIZON_SECONDS)
    step = timedelta(seconds=seconds)

    bars: list[ReplayBar] = []
    for candle in series:
        close_time = candle.start + step
        if close_time <= window_start:
            continue
        if basis == "full_mtf":
            state = _ALIGNMENT_STATE[
                evaluate(symbol, hourly, daily, close_time, config).alignment
            ]
        else:
            state = _TREND_STATE[
                evaluate_timeframe("1d", daily, close_time, config).trend
            ]

        future = forward.get(close_time + horizon)
        change: Optional[float] = None
        if future is not None and candle.close > 0:
            change = (future / candle.close - 1.0) * 100.0
        bars.append(ReplayBar(close_time, candle.close, state, change))
    return bars


# --- statistics ---------------------------------------------------------------


def _mean(values: Sequence[float]) -> Optional[float]:
    """Arithmetic mean, or None over nothing.

    None rather than 0.0 throughout this module: "no bar was ever in this state"
    and "bars in this state averaged nothing" are different facts, and only one
    of them is a measurement of the indicator.
    """
    return float(sum(values) / len(values)) if values else None


def _hit_rate(values: Sequence[float], *, bullish: bool) -> Optional[float]:
    """Fraction of bars the call got the DIRECTION right.

    A bullish call hits when the forward return is strictly positive, a bearish
    call when it is strictly negative. An exactly flat bar is a miss on both
    sides: the call was for a move, and there was none.
    """
    if not values:
        return None
    hits = sum(1 for v in values if (v > 0 if bullish else v < 0))
    return float(hits / len(values))


def _episodes(states: Sequence[BarState], target: BarState) -> int:
    """Contiguous runs of ``target`` — how many times the state was ENTERED.

    Counted over every replayed bar, including bars with no forward return, so
    that a gap in the price series cannot split one episode into two and inflate
    the count a reader uses to judge whether a hit rate means anything.
    """
    count = 0
    previous: Optional[BarState] = None
    for state in states:
        if state == target and previous != target:
            count += 1
        previous = state
    return count


@dataclass(frozen=True)
class WindowPerformance:
    """One row of ``trend_alignment_performance``, plus replay diagnostics."""

    symbol: str
    window_days: int
    basis: Basis
    evaluated_from: Optional[datetime]
    evaluated_to: Optional[datetime]
    samples: int
    bullish_episodes: int
    bearish_episodes: int
    bullish_bars: int
    bearish_bars: int
    unaligned_bars: int
    fwd_return_bullish_pct: Optional[float]
    fwd_return_bearish_pct: Optional[float]
    fwd_return_baseline_pct: Optional[float]
    hit_rate_bullish: Optional[float]
    hit_rate_bearish: Optional[float]
    note: str
    # Bars walked, including those dropped for want of a forward observation.
    # Diagnostic only: it is reported by the job and quoted in the note, but the
    # table's column set is the wire contract and does not grow for it.
    replayed_bars: int = 0

    def as_row(self) -> dict:
        """Exactly the persisted columns, ready for the upsert."""
        return {
            "symbol": self.symbol,
            "window_days": self.window_days,
            "basis": self.basis,
            "evaluated_from": self.evaluated_from,
            "evaluated_to": self.evaluated_to,
            "samples": self.samples,
            "bullish_episodes": self.bullish_episodes,
            "bearish_episodes": self.bearish_episodes,
            "bullish_bars": self.bullish_bars,
            "bearish_bars": self.bearish_bars,
            "unaligned_bars": self.unaligned_bars,
            "fwd_return_bullish_pct": self.fwd_return_bullish_pct,
            "fwd_return_bearish_pct": self.fwd_return_bearish_pct,
            "fwd_return_baseline_pct": self.fwd_return_baseline_pct,
            "hit_rate_bullish": self.hit_rate_bullish,
            "hit_rate_bearish": self.hit_rate_bearish,
            "note": self.note,
        }

    def as_dict(self) -> dict:
        """The job/endpoint report: the row plus what was dropped and why."""
        row = self.as_row()
        row["evaluated_from"] = _iso(self.evaluated_from)
        row["evaluated_to"] = _iso(self.evaluated_to)
        row["replayed_bars"] = self.replayed_bars
        row["bars_without_forward_return"] = self.replayed_bars - self.samples
        return row


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value else None


_BASIS_LABEL: dict[Basis, str] = {
    "full_mtf": "Full 1D+4H+1H alignment replayed at every 1H close",
    "daily_only": (
        "Daily leg only — price against the three moving averages on daily "
        "candles, replayed at every daily close. This is NOT the "
        "multi-timeframe alignment"
    ),
}


def _note(
    choice: BasisChoice,
    config: TrendConfig,
    bars: Sequence[ReplayBar],
    samples: int,
    daily_warmup: int,
) -> str:
    """The per-row limitation, in the words a reader needs, not a code."""
    ma = f"{config.ma_type.upper()} {config.fast}/{config.mid}/{config.slow}"
    parts = [f"{_BASIS_LABEL[choice.basis]} ({ma}); chosen because {choice.reason}."]
    warm = warmup_candles(config)
    if daily_warmup < warm:
        # The fallback basis can itself be short of history. Saying so here is
        # the difference between "the indicator was never directional" and "the
        # indicator could not be computed", which look identical in the counts.
        parts.append(
            f"The daily leg had only {daily_warmup} of the {warm} candles it "
            f"needs to warm up the {config.ma_type.upper()}{config.slow} at the "
            "start of this window, so its earliest bars could not be evaluated "
            "at all and are counted as unaligned."
        )
    if not bars:
        parts.append(
            "No bar in this window could be replayed, so there is no track "
            "record to report — not a track record of nothing."
        )
        return " ".join(parts)

    parts.append(
        f"Forward return is measured over the next 1 day from each bar's close; "
        f"{samples} of {len(bars)} replayed bars had a priced observation one day "
        "later."
    )
    # The two reasons a bar has no forward observation are different facts and
    # are reported as such: running off the end of the data is expected and
    # harmless, while an interior gap says the series itself is holed.
    data_end = bars[-1].close_time
    horizon = timedelta(seconds=FORWARD_HORIZON_SECONDS)
    unmeasured = [b for b in bars if b.forward_return_pct is None]
    past_end = sum(1 for b in unmeasured if b.close_time + horizon > data_end)
    gaps = len(unmeasured) - past_end
    if past_end:
        parts.append(
            f"Excluded as too new to score: {past_end} (the forward day has not "
            "finished yet)."
        )
    if gaps:
        parts.append(
            f"Dropped on a hole in the price series: {gaps} (never filled in)."
        )
    if choice.basis == "full_mtf":
        parts.append(
            "Hourly bars each look a full day forward, so their windows overlap "
            "and the samples are not independent observations."
        )
    parts.append(
        "Counts are bars and episodes, not trades, and the baseline is the same "
        "bars unconditionally — read the conditional returns against it."
    )
    return " ".join(parts)


def measure_window(
    symbol: str,
    window_days: int,
    hourly: Sequence[Candle],
    daily: Sequence[Candle],
    now: datetime,
    config: TrendConfig,
) -> WindowPerformance:
    """Replay one window and reduce it to the stored statistics."""
    now = now.astimezone(timezone.utc)
    window_start = now - timedelta(days=window_days)
    choice = choose_basis(
        window_days, window_start, intraday_since(hourly, now), hourly, daily, config
    )

    # On the intraday basis the 1H/4H legs may only read candles from inside
    # genuine intraday coverage; the one-tick-per-day era would otherwise enter
    # the hourly series as real hourly candles.
    replay_hourly: Sequence[Candle] = (
        [c for c in hourly if choice.intraday_from is None or c.start >= choice.intraday_from]
        if choice.basis == "full_mtf"
        else hourly
    )
    bars = replay(
        symbol, choice.basis, replay_hourly, daily, window_start, now, config
    )

    measured = [b for b in bars if b.forward_return_pct is not None]
    by_state: dict[BarState, list[float]] = {"bullish": [], "bearish": [], "unaligned": []}
    for bar in measured:
        by_state[bar.state].append(float(bar.forward_return_pct))
    states = [bar.state for bar in bars]

    return WindowPerformance(
        symbol=symbol,
        window_days=window_days,
        basis=choice.basis,
        evaluated_from=bars[0].close_time if bars else None,
        evaluated_to=bars[-1].close_time if bars else None,
        samples=len(measured),
        bullish_episodes=_episodes(states, "bullish"),
        bearish_episodes=_episodes(states, "bearish"),
        bullish_bars=len(by_state["bullish"]),
        bearish_bars=len(by_state["bearish"]),
        unaligned_bars=len(by_state["unaligned"]),
        fwd_return_bullish_pct=_mean(by_state["bullish"]),
        fwd_return_bearish_pct=_mean(by_state["bearish"]),
        fwd_return_baseline_pct=_mean([float(b.forward_return_pct) for b in measured]),
        hit_rate_bullish=_hit_rate(by_state["bullish"], bullish=True),
        hit_rate_bearish=_hit_rate(by_state["bearish"], bullish=False),
        note=_note(
            choice,
            config,
            bars,
            len(measured),
            len(completed(daily, TIMEFRAME_SECONDS["1d"], window_start)),
        ),
        replayed_bars=len(bars),
    )


def backtest(
    symbol: str,
    hourly: Sequence[Candle],
    daily: Sequence[Candle],
    now: datetime,
    config: Optional[TrendConfig] = None,
    windows: Sequence[int] = WINDOW_DAYS,
) -> list[WindowPerformance]:
    """Every requested window for one symbol, longest first."""
    config = config or TrendConfig()
    config.validate()
    now = now.astimezone(timezone.utc)
    return [
        measure_window(symbol, days, hourly, daily, now, config)
        for days in sorted({int(d) for d in windows}, reverse=True)
    ]
