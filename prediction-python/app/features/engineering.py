"""Point-in-time feature engineering for IR_GOLD_18K.

Leakage policy: every entry point filters/asserts ``observed_at <= as_of``
via :func:`assert_no_future`, and all rolling/lag computations are strictly
backward-looking (pandas rolling windows end at the current row; ``shift`` is
positive; forward-fill only propagates the past).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

from ..core.formula import KARAT_18_PURITY, TROY_OUNCE_GRAMS

LAGS = (1, 2, 3, 5, 10, 20)
ROLL_WINDOWS = (5, 10, 20)
RSI_PERIOD = 14
PREMIUM_Z_WINDOW = 30
ADX_PERIOD = 14
STOCH_PERIOD = 14
STOCH_SMOOTH = 3
CCI_PERIOD = 20
CHANNEL_PERIOD = 20   # Donchian + Keltner
CORR_WINDOW = 20      # rolling 18k-vs-XAU log-return correlation
DRAWDOWN_WINDOW = 90
PREMIUM_MOM_LAG = 5


class LeakageError(AssertionError):
    """A feature computation attempted to use data from after ``as_of``."""


def assert_no_future(
    df: pd.DataFrame, as_of: datetime, column: str = "observed_at"
) -> None:
    """Hard guard: raise if any row is observed after ``as_of``."""
    if df.empty:
        return
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    stamps = pd.to_datetime(df[column], utc=True)
    latest = stamps.max()
    if latest > pd.Timestamp(as_of):
        raise LeakageError(
            f"feature input contains rows observed after as_of "
            f"({latest.isoformat()} > {as_of.isoformat()})"
        )


def gregorian_to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    """Convert a Gregorian date to Jalali (standard integer algorithm)."""
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy - 1600
    g_day_no = 365 * gy2 + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
    g_day_no += g_d_m[gm - 1]
    if gm > 2 and ((gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0):
        g_day_no += 1
    g_day_no += gd - 1
    j_day_no = g_day_no - 79
    j_np = j_day_no // 12053
    j_day_no %= 12053
    jy = 979 + 33 * j_np + 4 * (j_day_no // 1461)
    j_day_no %= 1461
    if j_day_no >= 366:
        jy += (j_day_no - 1) // 365
        j_day_no = (j_day_no - 1) % 365
    if j_day_no < 186:
        jm = 1 + j_day_no // 31
        jd = 1 + j_day_no % 31
    else:
        jm = 7 + (j_day_no - 186) // 30
        jd = 1 + (j_day_no - 186) % 30
    return jy, jm, jd


def jalali_month(ts: pd.Timestamp) -> int:
    return gregorian_to_jalali(ts.year, ts.month, ts.day)[1]


def rsi(series: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Classic Wilder-less RSI over simple rolling means (causal)."""
    delta = series.diff()
    gain = delta.clip(lower=0.0).rolling(period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period).mean()
    rs = gain / loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # all-gain windows => RSI 100
    out = out.where(~(loss == 0.0) | gain.isna(), 100.0)
    return out


def daily_close(df: pd.DataFrame, symbol: str) -> pd.Series:
    """Last good observation per UTC day for one symbol.

    Expects columns ``symbol``, ``observed_at``, ``value``.
    """
    sub = df[df["symbol"] == symbol].copy()
    if sub.empty:
        return pd.Series(dtype=float)
    sub["observed_at"] = pd.to_datetime(sub["observed_at"], utc=True)
    sub = sub.sort_values("observed_at")
    sub["day"] = sub["observed_at"].dt.floor("D")
    series = sub.groupby("day")["value"].last().astype(float)
    series.index.name = None
    return series


def hourly_close(df: pd.DataFrame, symbol: str) -> pd.Series:
    """Last good observation per UTC hour for one symbol."""
    sub = df[df["symbol"] == symbol].copy()
    if sub.empty:
        return pd.Series(dtype=float)
    sub["observed_at"] = pd.to_datetime(sub["observed_at"], utc=True)
    sub = sub.sort_values("observed_at")
    sub["hour"] = sub["observed_at"].dt.floor("h")
    series = sub.groupby("hour")["value"].last().astype(float)
    series.index.name = None
    return series


def compute_feature_frame(
    gold: pd.Series,
    usd_irt: Optional[pd.Series] = None,
    xau_usd: Optional[pd.Series] = None,
    gold_fund: Optional[pd.Series] = None,
    fund_flow: Optional[pd.Series] = None,
) -> pd.DataFrame:
    """Causal feature matrix indexed like ``gold`` (a DatetimeIndex series).

    Every row uses ONLY information available at that row's timestamp.
    """
    gold = gold.astype(float)
    df = pd.DataFrame({"close": gold})
    close = df["close"]

    for k in LAGS:
        df[f"lag_{k}"] = close.shift(k)
        df[f"ret_{k}"] = close.pct_change(k)
    for w in ROLL_WINDOWS:
        df[f"roll_mean_{w}"] = close.rolling(w).mean()
        df[f"roll_std_{w}"] = close.rolling(w).std()
        df[f"close_vs_mean_{w}"] = close / df[f"roll_mean_{w}"] - 1.0
    df["momentum_10"] = close / close.shift(10) - 1.0
    df[f"rsi_{RSI_PERIOD}"] = rsi(close)
    df["vol_20"] = close.pct_change().rolling(20).std()

    # --- OHLC-style indicators (Addendum 2) ----------------------------------
    # The daily series is synthesized from ticks (last good observation per
    # day) with no true OHLC, so intraday high/low are APPROXIMATED with the
    # backward-looking rolling max/min of the daily closes (2-day window for
    # bar-level high/low, longer windows for channel indicators).  Documented
    # approximation; all windows end at the current row (causal).
    high = close.rolling(2).max()
    low = close.rolling(2).min()
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    # ADX(14) over simple rolling means (Wilder-less, matching rsi() above)
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0.0), up_move, 0.0), index=df.index
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0.0), down_move, 0.0),
        index=df.index,
    )
    atr_14 = tr.rolling(ADX_PERIOD).mean().replace(0.0, np.nan)
    plus_di = 100.0 * plus_dm.rolling(ADX_PERIOD).mean() / atr_14
    minus_di = 100.0 * minus_dm.rolling(ADX_PERIOD).mean() / atr_14
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    df["adx_14"] = dx.rolling(ADX_PERIOD).mean()

    # Stochastic %K/%D (14, 3) and Williams %R(14)
    high_14 = high.rolling(STOCH_PERIOD).max()
    low_14 = low.rolling(STOCH_PERIOD).min()
    stoch_range = (high_14 - low_14).replace(0.0, np.nan)
    df["stoch_k"] = 100.0 * (close - low_14) / stoch_range
    df["stoch_d"] = df["stoch_k"].rolling(STOCH_SMOOTH).mean()
    df["williams_r_14"] = -100.0 * (high_14 - close) / stoch_range

    # CCI(20) on the (approximated) typical price
    tp = (high + low + close) / 3.0
    tp_mean = tp.rolling(CCI_PERIOD).mean()
    tp_mad = tp.rolling(CCI_PERIOD).apply(
        lambda x: float(np.mean(np.abs(x - x.mean()))), raw=True
    )
    df["cci_20"] = (tp - tp_mean) / (0.015 * tp_mad.replace(0.0, np.nan))

    # Donchian(20) channel: pct distance of close from the upper/lower band
    donchian_upper = high.rolling(CHANNEL_PERIOD).max()
    donchian_lower = low.rolling(CHANNEL_PERIOD).min()
    df["donchian_upper_dist_20"] = close / donchian_upper - 1.0
    df["donchian_lower_dist_20"] = close / donchian_lower - 1.0

    # Keltner(20, 2xATR): distance of close from the EMA20 mid, normalized by
    # the 2xATR band half-width (+1 = at the upper band, -1 = at the lower)
    ema_20 = close.ewm(span=CHANNEL_PERIOD, adjust=False).mean()
    atr_20 = tr.rolling(CHANNEL_PERIOD).mean().replace(0.0, np.nan)
    df["keltner_pos_20"] = (close - ema_20) / (2.0 * atr_20)

    # drawdown from the 90-day high (uses whatever history exists early on)
    df["drawdown_90d_pct"] = (
        close / close.rolling(DRAWDOWN_WINDOW, min_periods=1).max() - 1.0
    ) * 100.0

    index = pd.DatetimeIndex(df.index)
    df["dow"] = index.dayofweek
    for d in range(7):  # day-of-week one-hot (Monday=0 .. Sunday=6)
        df[f"dow_{d}"] = (index.dayofweek == d).astype(float)
    df["hour"] = index.hour
    df["jalali_month"] = [jalali_month(ts) for ts in index]

    def _align(aux: pd.Series) -> pd.Series:
        # forward-fill = propagate the PAST only (causal)
        return aux.astype(float).reindex(index.union(aux.index)).ffill().reindex(index)

    usd = _align(usd_irt) if usd_irt is not None and not usd_irt.empty else None
    xau = _align(xau_usd) if xau_usd is not None and not xau_usd.empty else None

    if usd is not None:
        df["usd_irt"] = usd
        df["usd_ret_1"] = usd.pct_change()
        df["usd_ret_5"] = usd.pct_change(5)
    if xau is not None:
        df["xau_usd"] = xau
        df["xau_ret_1"] = xau.pct_change()
        df["xau_ret_5"] = xau.pct_change(5)
        # rolling 20d correlation of 18k vs XAUUSD daily log-returns
        gold_logret = np.log(close).diff()
        xau_logret = np.log(xau).diff()
        df["corr_xau_20"] = gold_logret.rolling(CORR_WINDOW).corr(xau_logret)
    if usd is not None and xau is not None:
        theoretical = xau / TROY_OUNCE_GRAMS * usd * KARAT_18_PURITY
        premium = (close - theoretical) / theoretical * 100.0
        df["theoretical_18k"] = theoretical
        df["premium_pct"] = premium
        prem_mean = premium.rolling(PREMIUM_Z_WINDOW).mean()
        prem_std = premium.rolling(PREMIUM_Z_WINDOW).std()
        df["premium_z_30"] = (premium - prem_mean) / prem_std.replace(0.0, np.nan)
        # premium momentum: 5-day change of premium_pct
        df["premium_mom_5"] = premium.diff(PREMIUM_MOM_LAG)

    # Tehran-exchange gold funds (Addendum 7): exchange-traded price discovery
    # (continuous 12:00-17:00 auction with real volume) plus retail sentiment.
    fund = _align(gold_fund) if gold_fund is not None and not gold_fund.empty else None
    flow = _align(fund_flow) if fund_flow is not None and not fund_flow.empty else None
    if fund is not None:
        df["fund_ret_1"] = fund.pct_change()
        df["fund_ret_5"] = fund.pct_change(5)
        # fund/physical ratio z-score: relative valuation of the exchange
        # units vs the physical gram (unit scales differ; z-score removes it)
        ratio = fund / close
        ratio_mean = ratio.rolling(PREMIUM_Z_WINDOW).mean()
        ratio_std = ratio.rolling(PREMIUM_Z_WINDOW).std()
        df["fund_ratio_z_30"] = (ratio - ratio_mean) / ratio_std.replace(0.0, np.nan)
    if flow is not None:
        # retail net buying, % of volume (positive = individuals net buyers)
        df["fund_flow"] = flow
        df["fund_flow_ma5"] = flow.rolling(5).mean()
        df["fund_flow_chg_5"] = flow.diff(5)

    return df


def build_snapshot(
    prices_df: pd.DataFrame, as_of: datetime, symbol: str = "IR_GOLD_18K"
) -> Optional[dict]:
    """Features for ``symbol`` using only rows with ``observed_at <= as_of``.

    Returns a JSON-safe dict (NaNs dropped) or None when there is no data.
    """
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    df = prices_df.copy()
    if df.empty:
        return None
    df["observed_at"] = pd.to_datetime(df["observed_at"], utc=True)
    df = df[df["observed_at"] <= pd.Timestamp(as_of)]
    if df.empty:
        return None
    assert_no_future(df, as_of)

    gold = daily_close(df, symbol)
    if gold.empty:
        return None
    usd = daily_close(df, "USD_IRT")
    xau = daily_close(df, "XAUUSD")
    fund = daily_close(df, "IR_GOLD_FUND_AYAR")
    flow = daily_close(df, "IR_GOLD_FUND_FLOW")
    frame = compute_feature_frame(
        gold,
        usd if not usd.empty else None,
        xau if not xau.empty else None,
        gold_fund=fund if not fund.empty else None,
        fund_flow=flow if not flow.empty else None,
    )
    last = frame.iloc[-1]
    features = {
        key: (round(float(val), 10) if isinstance(val, (int, float, np.floating)) else val)
        for key, val in last.items()
        if val is not None and not (isinstance(val, float) and np.isnan(val))
    }
    features = {k: v for k, v in features.items() if not pd.isna(v)}
    features["n_daily_points"] = int(len(gold))
    return features
