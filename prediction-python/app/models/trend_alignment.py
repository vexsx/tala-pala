"""Multi-timeframe trend alignment (Addendum 20).

Three moving averages (default EMA 26/48/220) evaluated INDEPENDENTLY on the
1D, 4H and 1H candle series of one symbol. A timeframe is bullish only under a
strict stack ``price > ma26 > ma48 > ma220``, bearish only under the strict
mirror, and neutral otherwise — direction is never guessed from a partial
stack. Full alignment requires all three timeframes to agree.

Two properties this module is built around:

* **Closed candles only.** The official state is read off the last COMPLETED
  candle of each timeframe. A price tick inside the forming candle must never
  flip the official state, because an alert fired on an unfinished candle can
  un-fire, and an alert that un-fires is worse than no alert.

* **Unavailable is a first-class state.** Insufficient warm-up, a gap, or
  stale data yields ``unavailable``, which can never take part in a full
  alignment. Carrying the previous trend forward would present an old
  conclusion as a current one.

The quantitative core here is pure: it takes candle lists and returns states.
Persistence, transition detection and alerting live in ``app/jobs``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional, Sequence

TrendState = Literal["bullish", "bearish", "neutral", "unavailable"]
Alignment = Literal["full_bullish", "full_bearish", "not_aligned"]
MaType = Literal["ema", "sma"]

# Timeframe -> bucket width. 4H is resampled from the hourly series: the
# candle API stores only day/hour truncations, so no native 4H exists.
TIMEFRAME_SECONDS: dict[str, int] = {"1h": 3600, "4h": 4 * 3600, "1d": 86400}
TIMEFRAMES: tuple[str, ...] = ("1d", "4h", "1h")

# Warm-up beyond the slow period. An EMA seeded on exactly `slow` points is
# still dominated by its seed; a further 30% of history makes the value stable
# enough to compare against a price.
WARMUP_FACTOR = 1.3


@dataclass(frozen=True)
class Candle:
    """One completed bucket. ``start`` is the bucket's opening instant (UTC)."""

    start: datetime
    open: float
    high: float
    low: float
    close: float


@dataclass
class TimeframeResult:
    timeframe: str
    trend: TrendState
    price: Optional[float] = None
    ma26: Optional[float] = None
    ma48: Optional[float] = None
    ma220: Optional[float] = None
    candle_open_time: Optional[datetime] = None
    candle_close_time: Optional[datetime] = None
    confirmed: bool = False
    data_fresh: bool = False
    ma_type: MaType = "ema"
    history_points: int = 0
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "timeframe": self.timeframe,
            "trend": self.trend,
            "price": self.price,
            "ma26": self.ma26,
            "ma48": self.ma48,
            "ma220": self.ma220,
            "candle_open_time": _iso(self.candle_open_time),
            "candle_close_time": _iso(self.candle_close_time),
            "confirmed": self.confirmed,
            "data_fresh": self.data_fresh,
            "ma_type": self.ma_type,
            "history_points": self.history_points,
            "reason": self.reason,
        }


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if value else None


# --- moving averages ---------------------------------------------------------


def sma(values: Sequence[float], period: int) -> Optional[float]:
    """Simple mean of the last ``period`` values; None when too few."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return float(sum(window) / period)


def ema(values: Sequence[float], period: int) -> Optional[float]:
    """Exponential MA seeded with the SMA of the first ``period`` values.

    SMA seeding (rather than starting from the first observation) is the
    convention every charting package uses; starting from a single point would
    make the early EMA a function of one arbitrary tick.
    """
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    current = float(sum(values[:period]) / period)
    for value in values[period:]:
        current = (float(value) - current) * k + current
    return current


def moving_average(values: Sequence[float], period: int, ma_type: MaType) -> Optional[float]:
    return sma(values, period) if ma_type == "sma" else ema(values, period)


# --- the rules ---------------------------------------------------------------


def trend_state(
    price: Optional[float],
    ma26: Optional[float],
    ma48: Optional[float],
    ma220: Optional[float],
) -> TrendState:
    """Strict stack test. Any missing input is ``unavailable``.

    Comparisons are strict on purpose: two equal MAs are not a trend, they are
    a crossover in progress, and calling that bullish would fire alerts on
    noise.
    """
    if any(v is None for v in (price, ma26, ma48, ma220)):
        return "unavailable"
    if price > ma26 > ma48 > ma220:  # type: ignore[operator]
        return "bullish"
    if price < ma26 < ma48 < ma220:  # type: ignore[operator]
        return "bearish"
    return "neutral"


def alignment(one_day: TrendState, four_hour: TrendState, one_hour: TrendState) -> Alignment:
    """Full alignment needs unanimity; any unavailable timeframe blocks it."""
    states = (one_day, four_hour, one_hour)
    if any(s == "unavailable" for s in states):
        return "not_aligned"
    if all(s == "bullish" for s in states):
        return "full_bullish"
    if all(s == "bearish" for s in states):
        return "full_bearish"
    return "not_aligned"


# --- candle construction -----------------------------------------------------


def bucket_start(at: datetime, seconds: int) -> datetime:
    """Floor an instant onto a UTC bucket boundary.

    Always computed in UTC epoch space. Flooring in local time would shift 4H
    boundaries by the zone offset — Tehran's +03:30 would land buckets on
    :30 marks and silently change every candle in the series.
    """
    epoch = int(at.astimezone(timezone.utc).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=timezone.utc)


def resample(candles: Sequence[Candle], seconds: int) -> list[Candle]:
    """Aggregate finer candles into ``seconds`` buckets with true OHLC.

    open = first open, high = max high, low = min low, close = last close.
    Buckets with no source candle are omitted rather than forward-filled: a
    synthesized candle for a period nobody quoted would be invented data.
    """
    buckets: dict[datetime, list[Candle]] = {}
    for candle in candles:
        buckets.setdefault(bucket_start(candle.start, seconds), []).append(candle)
    out: list[Candle] = []
    for start in sorted(buckets):
        group = sorted(buckets[start], key=lambda c: c.start)
        out.append(
            Candle(
                start=start,
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
            )
        )
    return out


def completed(
    candles: Sequence[Candle], seconds: int, now: datetime
) -> list[Candle]:
    """Drop the still-forming candle.

    A candle is complete once ``now`` has passed the end of its bucket. This is
    the only guard against look-ahead: everything downstream reads the last
    element of this list, so an unfinished bucket can never reach a decision.
    """
    now = now.astimezone(timezone.utc)
    return [c for c in candles if c.start + timedelta(seconds=seconds) <= now]


@dataclass
class TrendConfig:
    enabled: bool = True
    ma_type: MaType = "ema"
    fast: int = 26
    mid: int = 48
    slow: int = 220
    # A timeframe whose last completed candle is older than this many bucket
    # widths is treated as stale — the series has a hole at the near end.
    stale_buckets: float = 3.0

    def validate(self) -> None:
        if not (0 < self.fast < self.mid < self.slow):
            raise ValueError(
                f"trend alignment periods must satisfy 0 < fast < mid < slow "
                f"(got fast={self.fast} mid={self.mid} slow={self.slow})"
            )
        if self.ma_type not in ("ema", "sma"):
            raise ValueError(f"unsupported ma_type {self.ma_type!r}; use 'ema' or 'sma'")


def evaluate_timeframe(
    timeframe: str,
    candles: Sequence[Candle],
    now: datetime,
    config: TrendConfig,
) -> TimeframeResult:
    """State of one timeframe from its own candle series."""
    seconds = TIMEFRAME_SECONDS[timeframe]
    res = TimeframeResult(timeframe=timeframe, trend="unavailable", ma_type=config.ma_type)

    done = completed(candles, seconds, now)
    res.history_points = len(done)
    if not done:
        res.reason = "no completed candles"
        return res

    last = done[-1]
    res.candle_open_time = last.start
    res.candle_close_time = last.start + timedelta(seconds=seconds)
    res.confirmed = True
    res.price = last.close

    # Freshness: the newest completed candle must be recent enough that the
    # series still describes the present.
    age = (now.astimezone(timezone.utc) - res.candle_close_time).total_seconds()
    res.data_fresh = age <= config.stale_buckets * seconds
    if not res.data_fresh:
        res.reason = f"last completed candle is {age / seconds:.1f} buckets old"
        return res

    needed = int(config.slow * WARMUP_FACTOR)
    if len(done) < config.slow:
        res.reason = f"{len(done)} completed candles < {config.slow} required for the slow MA"
        return res

    closes = [c.close for c in done]
    res.ma26 = moving_average(closes, config.fast, config.ma_type)
    res.ma48 = moving_average(closes, config.mid, config.ma_type)
    res.ma220 = moving_average(closes, config.slow, config.ma_type)
    res.trend = trend_state(res.price, res.ma26, res.ma48, res.ma220)
    if len(done) < needed:
        # Enough to compute, but the slow MA is still warming up. Reported so
        # the UI can say the value is young rather than implying full support.
        res.reason = f"slow MA warming up ({len(done)}/{needed} candles)"
    return res


@dataclass
class AlignmentResult:
    symbol: str
    alignment: Alignment
    timeframes: dict[str, TimeframeResult] = field(default_factory=dict)
    calculated_at: Optional[datetime] = None
    data_fresh: bool = False

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "alignment": self.alignment,
            "timeframes": {k: v.as_dict() for k, v in self.timeframes.items()},
            "calculated_at": _iso(self.calculated_at),
            "data_fresh": self.data_fresh,
        }

    def candle_identity(self) -> dict[str, Optional[str]]:
        """The closed candles this result was computed from.

        Used as the idempotency key: the same three closed candles must never
        produce a second event, however many times the evaluator runs.
        """
        return {
            tf: _iso(self.timeframes[tf].candle_close_time) if tf in self.timeframes else None
            for tf in TIMEFRAMES
        }


def evaluate(
    symbol: str,
    hourly: Sequence[Candle],
    daily: Sequence[Candle],
    now: datetime,
    config: Optional[TrendConfig] = None,
) -> AlignmentResult:
    """Full multi-timeframe evaluation for one symbol.

    ``hourly`` feeds both the 1H timeframe and the resampled 4H one; ``daily``
    feeds 1D. Resampling happens BEFORE the completeness filter so a 4H bucket
    is judged on its own boundary, not on its constituents'.
    """
    config = config or TrendConfig()
    config.validate()
    now = now.astimezone(timezone.utc)

    series: dict[str, Sequence[Candle]] = {
        "1h": hourly,
        "4h": resample(hourly, TIMEFRAME_SECONDS["4h"]),
        "1d": daily,
    }
    results = {
        tf: evaluate_timeframe(tf, series[tf], now, config) for tf in TIMEFRAMES
    }
    return AlignmentResult(
        symbol=symbol,
        alignment=alignment(results["1d"].trend, results["4h"].trend, results["1h"].trend),
        timeframes=results,
        calculated_at=now,
        data_fresh=all(r.data_fresh for r in results.values()),
    )
