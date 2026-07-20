"""Stooq CSV provider — fallback for global metals.

``https://stooq.com/q/l/?s=xauusd&f=sd2t2ohlcv&h&e=csv`` returns::

    Symbol,Date,Time,Open,High,Low,Close,Volume
    XAUUSD,2026-07-18,22:59:57,3350.1,3360.4,3340.9,3355.2,0

NOTE (2026-07): stooq now fronts scripted access with a JS anti-bot
challenge, so this adapter frequently fails for automated clients.  We keep
it as a best-effort fallback, fail cleanly, and NEVER attempt to bypass the
challenge — Yahoo is the working global fallback.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from typing import Optional

from ..core.normalize import SYMBOL_META
from ..db import utcnow
from .base import Observation, Provider, ProviderError

QUOTE_URL = "https://stooq.com/q/l/"
HISTORY_URL = "https://stooq.com/q/d/l/"

# stooq symbol -> canonical symbol
STOOQ_MAP: dict[str, str] = {
    "xauusd": "XAUUSD",
    "xagusd": "XAGUSD",
}


def parse_quote_csv(text: str) -> list[Observation]:
    """Parse the light quote CSV (one row per symbol)."""
    out: list[Observation] = []
    reader = csv.DictReader(io.StringIO(text.strip()))
    for row in reader:
        stooq_sym = str(row.get("Symbol", "")).strip().lower()
        symbol = STOOQ_MAP.get(stooq_sym)
        if symbol is None:
            continue
        try:
            close = float(row.get("Close", ""))
        except (TypeError, ValueError):
            continue
        if close <= 0:
            continue
        observed_at = utcnow()
        try:
            observed_at = datetime.strptime(
                f"{row.get('Date', '')} {row.get('Time', '')}", "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
        currency, unit = SYMBOL_META[symbol]
        out.append(
            Observation(
                provider_code="stooq",
                symbol=symbol,
                raw_value=close,
                raw_unit=f"{currency}/{unit}",
                raw_currency=currency,
                value=close,
                currency=currency,
                unit=unit,
                observed_at=observed_at,
                raw_payload={k: row.get(k) for k in ("Symbol", "Date", "Time", "Close")},
            )
        )
    return out


def parse_history_csv(text: str) -> list[tuple[date, float]]:
    """Parse the daily-history CSV (Date,Open,High,Low,Close,...)."""
    out: list[tuple[date, float]] = []
    reader = csv.DictReader(io.StringIO(text.strip()))
    for row in reader:
        try:
            day = datetime.strptime(str(row.get("Date", "")), "%Y-%m-%d").date()
            close = float(row.get("Close", ""))
        except (TypeError, ValueError):
            continue
        if close > 0:
            out.append((day, close))
    out.sort(key=lambda pair: pair[0])
    return out


class StooqProvider(Provider):
    code = "stooq"
    category = "global_gold"

    def __init__(self, symbols: Optional[list[str]] = None, **kwargs) -> None:
        super().__init__(**kwargs)
        self.symbols = list(symbols or STOOQ_MAP.keys())

    def fetch(self) -> list[Observation]:
        text = self._get_text(
            QUOTE_URL,
            params={"s": ",".join(self.symbols), "f": "sd2t2ohlcv", "h": "", "e": "csv"},
        )
        observations = parse_quote_csv(text)
        if not observations:
            # Typical when the anti-bot challenge serves HTML instead of CSV.
            raise ProviderError(
                "stooq: no parseable rows (likely JS anti-bot challenge; not bypassing)"
            )
        return observations

    def fetch_history(self, stooq_symbol: str) -> list[tuple[date, float]]:
        text = self._get_text(HISTORY_URL, params={"s": stooq_symbol, "i": "d"})
        rows = parse_history_csv(text)
        if not rows:
            raise ProviderError(
                f"stooq: empty history for {stooq_symbol} (likely anti-bot challenge)"
            )
        return rows
