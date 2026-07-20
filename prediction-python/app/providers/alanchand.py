"""Alanchand fallback provider for Iranian gold / FX.

Alanchand has no stable documented public API.  Strategy:

1. try a JSON endpoint (``/api/gold``) — some deployments expose
   ``{"gold": [{"slug": "18ayar", "price": ...}], "currency": [...]}``;
2. otherwise fetch the homepage and regex-extract the embedded JSON state
   (Next.js style), looking for ``"slug":"18ayar" ... "price":N`` pairs.

Assumption (documented, guarded by validation sanity ranges): Alanchand
displays **TOMAN** values, so no rial division is applied.  ``observed_at`` is
the collection time — the page does not expose a reliable per-quote timestamp.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..db import utcnow
from .base import Observation, Provider, ProviderError

API_URL = "https://alanchand.com/api/gold"
PAGE_URL = "https://alanchand.com/"

# our slug -> (canonical symbol, unit)
SLUG_MAP: dict[str, tuple[str, str]] = {
    "18ayar": ("IR_GOLD_18K", "gram"),
    "sekkeh": ("IR_COIN_EMAMI", "coin"),
    "usd": ("USD_IRT", "usd"),
}

_PRICE_NEAR_SLUG = {
    slug: re.compile(
        r'"slug"\s*:\s*"' + re.escape(slug) + r'"[^{}]*?"price"\s*:\s*"?([\d,.]+)"?',
        re.IGNORECASE | re.DOTALL,
    )
    for slug in SLUG_MAP
}


def _to_float(raw: Any) -> Optional[float]:
    try:
        value = float(str(raw).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _observation(slug: str, value: float, payload: Optional[dict]) -> Observation:
    symbol, unit = SLUG_MAP[slug]
    return Observation(
        provider_code="alanchand",
        symbol=symbol,
        raw_value=value,
        raw_unit=f"IRT/{unit}",
        raw_currency="IRT",   # Alanchand quotes toman (documented assumption)
        value=value,
        currency="IRT",
        unit=unit,
        observed_at=utcnow(),
        raw_payload=payload,
    )


def parse_json_payload(payload: Any) -> list[Observation]:
    """Parse ``{"gold":[{"slug","price"},...],"currency":[...]}`` style JSON."""
    out: list[Observation] = []
    if not isinstance(payload, dict):
        return out
    items: list[dict] = []
    for key in ("gold", "currency", "coin"):
        block = payload.get(key)
        if isinstance(block, list):
            items.extend(x for x in block if isinstance(x, dict))
    for item in items:
        slug = str(item.get("slug", "")).lower()
        if slug not in SLUG_MAP:
            continue
        value = _to_float(item.get("price"))
        if value is None:
            continue
        out.append(_observation(slug, value, dict(item)))
    return out


def parse_html(html: str) -> list[Observation]:
    """Regex the embedded JSON of the HTML page for known slugs."""
    out: list[Observation] = []
    for slug, pattern in _PRICE_NEAR_SLUG.items():
        match = pattern.search(html)
        if not match:
            continue
        value = _to_float(match.group(1))
        if value is None:
            continue
        out.append(_observation(slug, value, {"matched": match.group(0)[:200]}))
    return out


class AlanchandProvider(Provider):
    code = "alanchand"
    category = "iran_gold"

    def fetch(self) -> list[Observation]:
        errors: list[str] = []
        try:
            payload = self._get_json(API_URL)
            observations = parse_json_payload(payload)
            if observations:
                return observations
            errors.append("api: no parseable items")
        except ProviderError as exc:
            errors.append(str(exc))

        try:
            html = self._get_text(PAGE_URL)
            observations = parse_html(html)
            if observations:
                return observations
            errors.append("html: no embedded prices found")
        except ProviderError as exc:
            errors.append(str(exc))

        raise ProviderError(f"alanchand: no observations ({'; '.join(errors)})")
