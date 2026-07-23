"""Hamrah Gold (pwa.hamrahgold.com) — 24/7 online gold trading platform.

The PWA's public, unauthenticated price ticker (the same numbers shown to
any visitor on the pre-login onboard page) serves JSON at::

    GET https://pwa.hamrahgold.com/api/v1/market/price/xau/changes?type=sell
    GET https://pwa.hamrahgold.com/api/v1/market/price/xau/changes?type=buy

    {"success": true, "data": {"current": 188370000, "type": "sell",
     "changes": {"1h": {"price":..., "percent":...}, "4h": ..., "1d": ...,
                 "1w": ..., "1mo": ...}}}

``current`` is RIAL per gram of 18k gold (platform price). The provider
emits the **buy/sell midpoint** as the canonical ``IR_GOLD_18K`` value
(platform fair value; both sides preserved in ``raw_payload`` along with the
spread). Being an online platform it quotes around the clock — including
hours when the physical bazaar is closed — which is why it runs at the top
of the provider priority order.

Ethics note (repo policy): this is the PUBLIC pre-login ticker fetched with
the project's honest User-Agent at the normal collect cadence — no accounts,
no CAPTCHA/auth bypass, and no private Hamrah Gold account data (which the
project deliberately never touches).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from ..core.normalize import rial_to_toman
from ..db import utcnow
from .base import Observation, Provider, ProviderError

log = logging.getLogger(__name__)

PRICE_URL = "https://pwa.hamrahgold.com/api/v1/market/price/xau/changes"


def parse_side(payload: Any) -> Optional[float]:
    """Extract ``data.current`` (rial/gram) from one side's payload."""
    if not isinstance(payload, dict) or not payload.get("success"):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    current = data.get("current")
    if not isinstance(current, (int, float)) or current <= 0:
        return None
    return float(current)


class HamrahGoldProvider(Provider):
    """Public price ticker of the Hamrah Gold online platform (keyless)."""

    code = "hamrahgold"
    category = "iran_gold"

    def fetch(self) -> list[Observation]:
        sides: dict[str, float] = {}
        errors: list[str] = []
        for side in ("sell", "buy"):
            try:
                payload = self._get_json(PRICE_URL, params={"type": side})
            except ProviderError as exc:
                errors.append(f"{side}: {exc}")
                continue
            value = parse_side(payload)
            if value is None:
                errors.append(f"{side}: unparseable payload")
                continue
            sides[side] = value

        if not sides:
            raise ProviderError(
                "hamrahgold: no price side delivered"
                + (f" ({'; '.join(errors)})" if errors else "")
            )

        # midpoint when both sides arrived; single side is still a valid quote
        mid_rial = sum(sides.values()) / len(sides)
        spread_pct = (
            (sides["sell"] - sides["buy"]) / mid_rial * 100.0
            if len(sides) == 2 and mid_rial > 0
            else None
        )
        return [
            Observation(
                provider_code=self.code,
                symbol="IR_GOLD_18K",
                raw_value=mid_rial,
                raw_unit="IRR/gram",
                raw_currency="IRR",
                value=rial_to_toman(mid_rial),
                currency="IRT",
                unit="gram",
                observed_at=utcnow(),  # live ticker; no timestamp in payload
                raw_payload={
                    "sell_rial": sides.get("sell"),
                    "buy_rial": sides.get("buy"),
                    "spread_pct": round(spread_pct, 4) if spread_pct is not None else None,
                    "sides": len(sides),
                },
            )
        ]
