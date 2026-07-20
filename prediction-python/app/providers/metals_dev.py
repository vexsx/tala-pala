"""metals.dev keyed API — optional global metals source (needs METALS_DEV_API_KEY).

``GET https://api.metals.dev/v1/latest?api_key=<KEY>&currency=USD&unit=toz``::

    {"status": "success",
     "metals": {"gold": 3350.2, "silver": 38.1},
     "timestamps": {"metal": "2026-07-20T10:00:00.354Z"}}
"""
from __future__ import annotations

from typing import Any, Optional

from dateutil import parser as dateparser

from ..db import utcnow
from .base import Observation, Provider, ProviderError

LATEST_URL = "https://api.metals.dev/v1/latest"

METAL_MAP: dict[str, str] = {"gold": "XAUUSD", "silver": "XAGUSD"}


def parse_latest(payload: Any) -> list[Observation]:
    out: list[Observation] = []
    if not isinstance(payload, dict):
        return out
    metals = payload.get("metals")
    if not isinstance(metals, dict):
        return out
    observed_at = utcnow()
    stamps = payload.get("timestamps")
    if isinstance(stamps, dict) and stamps.get("metal"):
        try:
            parsed = dateparser.isoparse(str(stamps["metal"]))
            if parsed.tzinfo is not None:
                observed_at = parsed
        except (ValueError, OverflowError):
            pass
    for metal, symbol in METAL_MAP.items():
        value = metals.get(metal)
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        out.append(
            Observation(
                provider_code="metals_dev",
                symbol=symbol,
                raw_value=float(value),
                raw_unit="USD/ozt",
                raw_currency="USD",
                value=float(value),
                currency="USD",
                unit="ozt",
                observed_at=observed_at,
                raw_payload={"metal": metal, "value": value},
            )
        )
    return out


class MetalsDevProvider(Provider):
    code = "metals_dev"
    category = "global_gold"

    def __init__(self, api_key: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not api_key:
            raise ValueError("MetalsDevProvider requires METALS_DEV_API_KEY")
        self.api_key = api_key

    def fetch(self) -> list[Observation]:
        payload = self._get_json(
            LATEST_URL,
            params={"api_key": self.api_key, "currency": "USD", "unit": "toz"},
        )
        if isinstance(payload, dict) and payload.get("status") not in (None, "success"):
            raise ProviderError(f"metals_dev: API error status={payload.get('status')}")
        observations = parse_latest(payload)
        if not observations:
            raise ProviderError("metals_dev: no parseable metals in response")
        return observations
