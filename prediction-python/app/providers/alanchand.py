"""Alanchand provider for Iranian gold / FX — two modes.

1. **Token mode** (``ALANCHAND_TOKEN`` set): the documented paid API
   ``https://api.alanchand.com?type=gold&symbols=...`` with a Bearer token
   (docs/data-sources.md).  API figures are **RIAL**, divided by 10 to toman
   during normalization; the raw rial value is kept for ``raw_observations``.
2. **Keyless HTML mode** (no token): the public server-rendered English page
   ``https://alanchand.com/en/gold-price/18ayar`` shows the CURRENT 18k gram
   price as e.g. ``181,679,700 IRR`` (verified 2026-07-20; values are rial,
   ÷10 → toman).  Only ``IR_GOLD_18K`` is available in this mode, and the
   page's separate "Real Price" (their theoretical parity value) is
   explicitly skipped.  If the page ever starts serving anti-bot challenges
   the provider fails gracefully — never bypassed (base 401/403 handling).

``observed_at`` is the collection time in both modes — neither surface
exposes a reliable machine-readable per-quote timestamp.

The pre-2026-07 keyless endpoints (``/api/gold`` JSON + homepage embedded
state) stopped yielding data (migration 0006); their parsers are kept at the
bottom of this module as LEGACY for fixture tests and forensics only.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..db import utcnow
from .base import Observation, Provider, ProviderError
from .htmlparse import to_float, visible_text

API_URL = "https://api.alanchand.com"
GOLD_PAGE_URL = "https://alanchand.com/en/gold-price/18ayar"

# our slug -> (canonical symbol, unit)
SLUG_MAP: dict[str, tuple[str, str]] = {
    "18ayar": ("IR_GOLD_18K", "gram"),
    "sekkeh": ("IR_COIN_EMAMI", "coin"),
    "usd": ("USD_IRT", "usd"),
}

# (type, symbols) query pairs issued in token mode
API_QUERIES: tuple[tuple[str, str], ...] = (
    ("gold", "18ayar,sekkeh"),
    ("currencies", "usd"),
)

# HTML mode: label of the current-price block, and rial amounts such as
# "181,679,700 IRR" in the tag-stripped page text
_GOLD_LABEL_RE = re.compile(r"18K\s*Gold\s*per\s*Gram", re.IGNORECASE)
_IRR_PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{7,})\s*IRR", re.IGNORECASE)
_REAL_PRICE_RE = re.compile(r"real\s*price", re.IGNORECASE)


def _rial_observation(
    slug: str, rial_value: float, payload: Optional[dict], method: str
) -> Observation:
    """RIAL-quoted value -> normalized toman Observation (÷10)."""
    symbol, unit = SLUG_MAP[slug]
    return Observation(
        provider_code="alanchand",
        symbol=symbol,
        raw_value=rial_value,
        raw_unit=f"IRR/{unit}" + (" (html)" if method == "html" else ""),
        raw_currency="IRR",
        value=rial_value / 10.0,
        currency="IRT",
        unit=unit,
        observed_at=utcnow(),
        raw_payload=payload,
    )


def parse_api_payload(payload: Any) -> list[Observation]:
    """Parse the token API response (RIAL figures, ÷10 → toman).

    Tolerates the shapes seen in their docs: a bare list of items, or a dict
    with item arrays under ``gold`` / ``currency`` / ``currencies`` / ``coin``
    / ``data`` / ``result``.  Items carry ``slug`` (or ``symbol``) plus
    ``price`` (or ``sell`` / ``value``).
    """
    items: list[dict] = []
    if isinstance(payload, list):
        items = [x for x in payload if isinstance(x, dict)]
    elif isinstance(payload, dict):
        for key in ("gold", "currency", "currencies", "coin", "data", "result"):
            block = payload.get(key)
            if isinstance(block, list):
                items.extend(x for x in block if isinstance(x, dict))
    out: list[Observation] = []
    for item in items:
        slug = str(item.get("slug") or item.get("symbol") or "").lower()
        if slug not in SLUG_MAP:
            continue
        value = to_float(item.get("price") or item.get("sell") or item.get("value"))
        if value is None:
            continue
        out.append(_rial_observation(slug, value, dict(item), method="api"))
    return out


def parse_gold_page(html: str) -> Optional[Observation]:
    """Extract the CURRENT 18k price from the /en/gold-price/18ayar page.

    Works on the tag-stripped visible text: the first rial amount after the
    "18K Gold per Gram" label whose preceding text does not mention
    "Real Price" (the page's theoretical value, deliberately skipped).
    """
    text = visible_text(html)
    label = _GOLD_LABEL_RE.search(text)
    if not label:
        return None
    for match in _IRR_PRICE_RE.finditer(text, label.end()):
        if _REAL_PRICE_RE.search(text, label.end(), match.start()):
            return None  # walked past the current quote into "Real Price"
        value = to_float(match.group(1))
        if value is None:
            continue
        return _rial_observation(
            "18ayar", value, {"matched": match.group(0)}, method="html"
        )
    return None


class AlanchandProvider(Provider):
    code = "alanchand"
    category = "iran_gold"

    def __init__(self, token: str = "", **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.token = token

    def fetch(self) -> list[Observation]:
        if self.token:
            return self._fetch_api()
        return self._fetch_html()

    def _fetch_api(self) -> list[Observation]:
        headers = {"Authorization": f"Bearer {self.token}"}
        out: list[Observation] = []
        errors: list[str] = []
        for api_type, symbols in API_QUERIES:
            try:
                payload = self._get_json(
                    API_URL,
                    params={"type": api_type, "symbols": symbols},
                    headers=headers,
                )
                out.extend(parse_api_payload(payload))
            except ProviderError as exc:
                errors.append(str(exc))
        if not out:
            raise ProviderError(
                "alanchand: no observations from token API "
                f"({'; '.join(errors) or 'no parseable items'})"
            )
        return out

    def _fetch_html(self) -> list[Observation]:
        html = self._get_text(GOLD_PAGE_URL)
        observation = parse_gold_page(html)
        if observation is None:
            raise ProviderError(
                "alanchand: no current 18k price found on the gold page "
                "(layout changed or challenge page served; not bypassing)"
            )
        return [observation]


# ---------------------------------------------------------------------------
# LEGACY parsers (pre-2026-07 keyless endpoints; kept for fixture tests and
# forensics only — fetch() no longer uses them).  These surfaces displayed
# TOMAN values, so no rial division is applied here.
# ---------------------------------------------------------------------------

_PRICE_NEAR_SLUG = {
    slug: re.compile(
        r'"slug"\s*:\s*"' + re.escape(slug) + r'"[^{}]*?"price"\s*:\s*"?([\d,.]+)"?',
        re.IGNORECASE | re.DOTALL,
    )
    for slug in SLUG_MAP
}


def _legacy_observation(slug: str, value: float, payload: Optional[dict]) -> Observation:
    symbol, unit = SLUG_MAP[slug]
    return Observation(
        provider_code="alanchand",
        symbol=symbol,
        raw_value=value,
        raw_unit=f"IRT/{unit}",
        raw_currency="IRT",   # legacy surfaces quoted toman (documented assumption)
        value=value,
        currency="IRT",
        unit=unit,
        observed_at=utcnow(),
        raw_payload=payload,
    )


def parse_json_payload(payload: Any) -> list[Observation]:
    """LEGACY: parse ``{"gold":[{"slug","price"},...],"currency":[...]}`` JSON."""
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
        value = to_float(item.get("price"))
        if value is None:
            continue
        out.append(_legacy_observation(slug, value, dict(item)))
    return out


def parse_html(html: str) -> list[Observation]:
    """LEGACY: regex the embedded JSON of the old homepage for known slugs."""
    out: list[Observation] = []
    for slug, pattern in _PRICE_NEAR_SLUG.items():
        match = pattern.search(html)
        if not match:
            continue
        value = to_float(match.group(1))
        if value is None:
            continue
        out.append(_legacy_observation(slug, value, {"matched": match.group(0)[:200]}))
    return out
