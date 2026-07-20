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

    index = pd.DatetimeIndex(df.index)
    df["dow"] = index.dayofweek
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
    if usd is not None and xau is not None:
        theoretical = xau / TROY_OUNCE_GRAMS * usd * KARAT_18_PURITY
        premium = (close - theoretical) / theoretical * 100.0
        df["theoretical_18k"] = theoretical
        df["premium_pct"] = premium
        prem_mean = premium.rolling(PREMIUM_Z_WINDOW).mean()
        prem_std = premium.rolling(PREMIUM_Z_WINDOW).std()
        df["premium_z_30"] = (premium - prem_mean) / prem_std.replace(0.0, np.nan)

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
    frame = compute_feature_frame(
        gold, usd if not usd.empty else None, xau if not xau.empty else None
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
