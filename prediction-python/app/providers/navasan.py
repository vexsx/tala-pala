"""Navasan keyed API (api.navasan.tech) — optional, enabled only with an API key.

``GET https://api.navasan.tech/latest/?api_key=<KEY>`` returns items such as::

    {"usd_sell": {"value": "106500", "change": 120, "date": "..."},
     "18ayar":   {"value": "8850000", ...}}

Unit assumptions verified per field (documented; sanity checks in
core.validation guard against drift):

* ``usd_sell`` — TOMAN per USD (free-market sell rate);
* ``18ayar``   — TOMAN per gram of 18k gold.

``observed_at`` is the collection time; Navasan's ``date`` string has no
reliable timezone, so it is preserved in the raw payload only.
"""
from __future__ import annotations

from typing import Any, Optional

from ..db import utcnow
from .base import Observation, Provider, ProviderError

LATEST_URL = "https://api.navasan.tech/latest/"

# navasan item key -> (canonical symbol, unit) — values already in TOMAN.
ITEM_MAP: dict[str, tuple[str, str]] = {
    "usd_sell": ("USD_IRT", "usd"),
    "18ayar": ("IR_GOLD_18K", "gram"),
}


def _to_float(raw: Any) -> Optional[float]:
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def parse_latest(payload: Any) -> list[Observation]:
    out: list[Observation] = []
    if not isinstance(payload, dict):
        return out
    for key, (symbol, unit) in ITEM_MAP.items():
        item = payload.get(key)
        if not isinstance(item, dict):
            continue
        value = _to_float(item.get("value"))
        if value is None:
            continue
        out.append(
            Observation(
                provider_code="navasan",
                symbol=symbol,
                raw_value=value,
                raw_unit=f"IRT/{unit}",
                raw_currency="IRT",  # toman per Navasan docs (verified per field)
                value=value,
                currency="IRT",
                unit=unit,
                observed_at=utcnow(),
                raw_payload=dict(item),
            )
        )
    return out


class NavasanProvider(Provider):
    code = "navasan"
    category = "fx"

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not api_key:
            raise ValueError("NavasanProvider requires NAVASAN_API_KEY")
        self.api_key = api_key

    def fetch(self) -> list[Observation]:
        payload = self._get_json(LATEST_URL, params={"api_key": self.api_key})
        observations = parse_latest(payload)
        if not observations:
            raise ProviderError("navasan: no parseable items in response")
        return observations
