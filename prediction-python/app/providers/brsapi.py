"""BrsApi.ir keyed API — Iranian gold/coin/FX + global ounce (needs BRSAPI_KEY).

``GET https://api.brsapi.ir/Market/Gold_Currency.php?key=<KEY>`` returns
(shape verified 2026-07-20 against the published sample
https://brsapi.ir/Api/Market/Sample/FreeApi_Gold_Currency.json)::

    {"gold": [
        {"date": "1404/02/28", "time": "16:29", "time_unix": 1747573140,
         "symbol": "IR_GOLD_18K", "name_en": "18K Gold", "name": "...",
         "price": 6214700, "change_value": -95100, "change_percent": -1.53,
         "unit": "تومان"},
        ...],
     "currency": [... {"symbol": "USD", "price": 81650, "unit": "تومان"} ...],
     "cryptocurrency": [...]}

Units: Iranian quotes carry ``unit`` = 'تومان' (TOMAN), which is already the
canonical IRT scale — **NO ÷10** (unlike TGJU's rial quotes).  The global
ounce ``XAUUSD`` carries 'دلار' (USD) and passes through.  A defensive branch
still divides by 10 should a 'ریال' unit ever appear.  ``observed_at`` comes
from ``time_unix`` (epoch seconds); the Jalali ``date`` string stays in the
raw payload only.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..core.normalize import rial_to_toman
from ..db import utcnow
from .base import Observation, Provider, ProviderError

GOLD_CURRENCY_URL = "https://api.brsapi.ir/Market/Gold_Currency.php"

# BrsApi symbol -> (canonical symbol, unit)
ITEM_MAP: dict[str, tuple[str, str]] = {
    "IR_GOLD_18K": ("IR_GOLD_18K", "gram"),
    "IR_COIN_EMAMI": ("IR_COIN_EMAMI", "coin"),
    "USD": ("USD_IRT", "usd"),
    "XAUUSD": ("XAUUSD", "ozt"),
}

UNIT_TOMAN = "تومان"
UNIT_RIAL = "ریال"
UNIT_USD = "دلار"


def _parse_item(item: dict) -> Optional[Observation]:
    mapped = ITEM_MAP.get(str(item.get("symbol")))
    if mapped is None:
        return None
    symbol, unit = mapped
    raw_value = item.get("price")
    if not isinstance(raw_value, (int, float)) or raw_value <= 0:
        return None
    raw_value = float(raw_value)
    raw_unit_label = str(item.get("unit") or "")
    if raw_unit_label == UNIT_USD:
        raw_currency, value, currency = "USD", raw_value, "USD"
    elif raw_unit_label == UNIT_RIAL:  # defensive: not observed in the wild
        raw_currency, value, currency = "IRR", rial_to_toman(raw_value), "IRT"
    else:  # 'تومان' — already toman, NO ÷10
        raw_currency, value, currency = "IRT", raw_value, "IRT"
    epoch = item.get("time_unix")
    observed_at = (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        if isinstance(epoch, (int, float)) and epoch > 0
        else utcnow()
    )
    return Observation(
        provider_code="brsapi",
        symbol=symbol,
        raw_value=raw_value,
        raw_unit=f"{raw_currency}/{unit}",
        raw_currency=raw_currency,
        value=value,
        currency=currency,
        unit=unit,
        observed_at=observed_at,
        raw_payload={
            k: item.get(k)
            for k in ("date", "time", "time_unix", "symbol", "name_en", "price", "unit")
        },
    )


def parse_gold_currency(payload: Any) -> list[Observation]:
    """Parse the Gold_Currency.php payload ('gold' + 'currency' lists)."""
    out: list[Observation] = []
    if not isinstance(payload, dict):
        return out
    for section in ("gold", "currency"):
        items = payload.get(section)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            obs = _parse_item(item)
            if obs is not None:
                out.append(obs)
    return out


class BrsApiProvider(Provider):
    code = "brsapi"
    category = "iran_gold"

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not api_key:
            raise ValueError("BrsApiProvider requires BRSAPI_KEY")
        self.api_key = api_key

    def fetch(self) -> list[Observation]:
        payload = self._get_json(GOLD_CURRENCY_URL, params={"key": self.api_key})
        observations = parse_gold_currency(payload)
        if not observations:
            raise ProviderError("brsapi: no parseable items in response")
        return observations
