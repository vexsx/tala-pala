"""Yahoo Finance chart API — global gold / macro symbols.

``GET https://query1.finance.yahoo.com/v8/finance/chart/<ticker>`` with
``interval=1d&range=1d``; the latest quote is ``chart.result[0].meta``
(``regularMarketPrice`` + ``regularMarketTime`` epoch seconds).

Ticker notes:

* ``GC=F`` (COMEX gold futures) is used as the XAUUSD proxy, ``SI=F`` for
  XAGUSD, ``BZ=F`` for Brent, ``DX-Y.NYB`` for DXY.
* ``^TNX`` quotes **ten times** the US 10-year yield (43.5 => 4.35%);
  normalization divides by 10 (see core.normalize.tnx_to_pct).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..core.normalize import SYMBOL_META, tnx_to_pct
from ..db import utcnow
from .base import Observation, Provider, ProviderError

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"

# yahoo ticker -> canonical symbol
TICKER_MAP: dict[str, str] = {
    "GC=F": "XAUUSD",
    "SI=F": "XAGUSD",
    "BZ=F": "BRENT_OIL",
    "DX-Y.NYB": "DXY",
    "^TNX": "US10Y",
}


def _normalize(symbol: str, raw_value: float) -> float:
    if symbol == "US10Y":
        return tnx_to_pct(raw_value)
    return raw_value


def parse_chart(payload: Any, ticker: str) -> Optional[Observation]:
    """Extract the latest quote from a v8 chart payload (defensive)."""
    symbol = TICKER_MAP.get(ticker)
    if symbol is None or not isinstance(payload, dict):
        return None
    try:
        result = payload["chart"]["result"][0]
        meta = result["meta"]
    except (KeyError, IndexError, TypeError):
        return None
    raw_value = meta.get("regularMarketPrice")
    if not isinstance(raw_value, (int, float)) or raw_value <= 0:
        return None
    epoch = meta.get("regularMarketTime")
    observed_at = (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        if isinstance(epoch, (int, float))
        else utcnow()
    )
    currency, unit = SYMBOL_META[symbol]
    raw_currency = str(meta.get("currency") or currency)
    raw_unit = "TNX_index" if symbol == "US10Y" else f"{raw_currency}/{unit}"
    return Observation(
        provider_code="yahoo",
        symbol=symbol,
        raw_value=float(raw_value),
        raw_unit=raw_unit,
        raw_currency=raw_currency if symbol != "US10Y" else "INDEX",
        value=_normalize(symbol, float(raw_value)),
        currency=currency,
        unit=unit,
        observed_at=observed_at,
        raw_payload={"meta": {k: meta.get(k) for k in
                              ("symbol", "currency", "regularMarketPrice",
                               "regularMarketTime")}},
    )


def parse_chart_history(payload: Any, ticker: str) -> list[tuple[datetime, float]]:
    """Extract (utc timestamp, close) pairs from a ranged chart payload (seeding)."""
    out: list[tuple[datetime, float]] = []
    try:
        result = payload["chart"]["result"][0]
        stamps = result["timestamp"]
        closes = result["indicators"]["quote"][0]["close"]
    except (KeyError, IndexError, TypeError):
        return out
    symbol = TICKER_MAP.get(ticker, "")
    for epoch, close in zip(stamps, closes):
        if close is None or not isinstance(epoch, (int, float)):
            continue
        value = _normalize(symbol, float(close)) if symbol else float(close)
        out.append((datetime.fromtimestamp(epoch, tz=timezone.utc), value))
    return out


class YahooProvider(Provider):
    """One class parameterized by the ticker map."""

    code = "yahoo"
    category = "global_gold"

    def __init__(self, tickers: Optional[dict[str, str]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.tickers = dict(tickers or TICKER_MAP)

    def fetch(self) -> list[Observation]:
        observations: list[Observation] = []
        errors: list[str] = []
        for ticker in self.tickers:
            try:
                payload = self._get_json(
                    CHART_URL.format(ticker=ticker),
                    params={"interval": "1d", "range": "1d"},
                )
                obs = parse_chart(payload, ticker)
                if obs is not None:
                    observations.append(obs)
                else:
                    errors.append(f"{ticker}: unparseable chart payload")
            except ProviderError as exc:
                errors.append(str(exc))
        if not observations:
            raise ProviderError(f"yahoo: no observations ({'; '.join(errors)})")
        return observations

    def fetch_history(self, ticker: str, range_: str = "3y") -> list[tuple[datetime, float]]:
        """Daily close history for seeding (not part of the collect loop)."""
        payload = self._get_json(
            CHART_URL.format(ticker=ticker),
            params={"interval": "1d", "range": range_},
        )
        return parse_chart_history(payload, ticker)
