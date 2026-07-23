"""BitMax (bitmax.ir) — USDT/toman as the continuous free-market USD proxy.

The exchange's public, unauthenticated watcher API (the same numbers the
market page renders for any visitor) serves JSON at::

    GET https://api.bitmax.ir/watcher/price/alternative

    {"message": {"USDT": {"price_in_usd": 1.0, "price_in_irt": 192676.0,
                          "change": 0.0105, "market_cap": ...,
                          "volume_24h": ...}, "BTC": {...}, ...}}

``price_in_irt`` is TOMAN per USDT despite the IRT label (verified against
the rendered page). Tether trades 24/7 including Iranian off-days, so it is
the always-fresh reference for the free-market dollar; the small, visible
premium of USDT over cash dollars is genuine market information, not error.
The observation is emitted as ``USD_IRT`` (documented proxy; the raw payload
keeps the 24h change and volume).
"""
from __future__ import annotations

import logging
from typing import Any

from ..db import utcnow
from .base import Observation, Provider, ProviderError

log = logging.getLogger(__name__)

WATCHER_URL = "https://api.bitmax.ir/watcher/price/alternative"


def parse_watcher(payload: Any) -> list[Observation]:
    """Parse the watcher payload into a single USD_IRT observation."""
    if not isinstance(payload, dict):
        return []
    message = payload.get("message")
    if not isinstance(message, dict):
        return []
    usdt = message.get("USDT")
    if not isinstance(usdt, dict):
        return []
    price = usdt.get("price_in_irt")
    if not isinstance(price, (int, float)) or price <= 0:
        return []
    return [
        Observation(
            provider_code="bitmax",
            symbol="USD_IRT",
            raw_value=float(price),
            raw_unit="IRT/usdt",
            raw_currency="IRT",
            value=float(price),  # already toman (verified vs rendered page)
            currency="IRT",
            unit="usd",
            observed_at=utcnow(),  # live ticker; no timestamp in payload
            raw_payload={
                "instrument": "USDT",
                "price_in_usd": usdt.get("price_in_usd"),
                "change": usdt.get("change"),
                "volume_24h_irt": usdt.get("volume_24h"),
            },
        )
    ]


class BitmaxProvider(Provider):
    """Public USDT/toman watcher of the BitMax exchange (keyless, 24/7)."""

    code = "bitmax"
    category = "fx"

    def fetch(self) -> list[Observation]:
        payload = self._get_json(WATCHER_URL)
        observations = parse_watcher(payload)
        if not observations:
            raise ProviderError("bitmax: no parseable USDT price in watcher payload")
        return observations
