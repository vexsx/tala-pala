"""gold-api.com — keyless global spot prices (XAU, XAG).

``GET https://api.gold-api.com/price/XAU`` (response verified 2026-07-20)::

    {"currency": "USD", "currencySymbol": "$", "exchangeRate": 1.0,
     "name": "Gold", "price": 4010.100098, "symbol": "XAU",
     "updatedAt": "2026-07-20T12:58:29Z",
     "updatedAtReadable": "a few seconds ago"}

``price`` is the USD spot per troy ounce; ``/price/XAG`` returns the same
shape for silver.  ``updatedAt`` is ISO-8601 UTC.
"""
from __future__ import annotations

from typing import Any, Optional

from dateutil import parser as dateparser

from ..db import utcnow
from .base import Observation, Provider, ProviderError

PRICE_URL = "https://api.gold-api.com/price/{api_symbol}"

# gold-api symbol -> canonical symbol (both USD per troy ounce)
SYMBOL_MAP: dict[str, str] = {"XAU": "XAUUSD", "XAG": "XAGUSD"}


def parse_price(payload: Any, api_symbol: str) -> Optional[Observation]:
    """Extract one spot quote from a /price/<symbol> payload (defensive)."""
    symbol = SYMBOL_MAP.get(api_symbol)
    if symbol is None or not isinstance(payload, dict):
        return None
    value = payload.get("price")
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    observed_at = utcnow()
    if payload.get("updatedAt"):
        try:
            parsed = dateparser.isoparse(str(payload["updatedAt"]))
            if parsed.tzinfo is not None:
                observed_at = parsed
        except (ValueError, OverflowError):
            pass
    return Observation(
        provider_code="gold_api",
        symbol=symbol,
        raw_value=float(value),
        raw_unit="USD/ozt",
        raw_currency=str(payload.get("currency") or "USD"),
        value=float(value),
        currency="USD",
        unit="ozt",
        observed_at=observed_at,
        raw_payload={k: payload.get(k) for k in ("symbol", "name", "price", "updatedAt")},
    )


class GoldAPIProvider(Provider):
    code = "gold_api"
    category = "global_gold"

    def fetch(self) -> list[Observation]:
        observations: list[Observation] = []
        errors: list[str] = []
        for api_symbol in SYMBOL_MAP:
            try:
                payload = self._get_json(PRICE_URL.format(api_symbol=api_symbol))
            except ProviderError as exc:
                errors.append(str(exc))
                continue
            obs = parse_price(payload, api_symbol)
            if obs is not None:
                observations.append(obs)
            else:
                errors.append(f"{api_symbol}: unparseable price payload")
        if not observations:
            raise ProviderError(f"gold_api: no observations ({'; '.join(errors)})")
        return observations
