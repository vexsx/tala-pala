"""Milli Gold (milli.gold) keyless HTML provider — 18k gram price only.

The homepage server-renders the 1-gram 18k price next to the label
``قیمت ۱ گرم طلای ۱۸ عیار`` as e.g. ``182,050,000ریال`` (verified
2026-07-20; no JS required, no anti-bot in the way).  Values are **RIAL**,
divided by 10 to toman during normalization.  Digits may be ASCII or
Persian/Arabic-Indic and the label often carries zero-width joiners — all
normalized by :mod:`.htmlparse` before matching.

``observed_at`` is the collection time (the page shows no machine-readable
quote timestamp).  If the page ever starts serving challenge pages the
provider fails gracefully with a clear error — never bypassed.
"""
from __future__ import annotations

import re
from typing import Optional

from ..db import utcnow
from .base import Observation, Provider, ProviderError
from .htmlparse import to_float, visible_text

HOME_URL = "https://milli.gold/"

# after digit normalization the label reads "قیمت 1 گرم طلای 18 عیار"; the
# rial amount may hug the word ریال with no space ("182,050,000ریال")
_LABEL_RE = re.compile(r"قیمت\s*1\s*گرم\s*طلای\s*18\s*عیار")
_RIAL_PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{7,})\s*ریال")


def parse_home(html: str) -> Optional[Observation]:
    """Extract the current 18k gram price (RIAL → ÷10 toman) or None."""
    text = visible_text(html)
    label = _LABEL_RE.search(text)
    if not label:
        return None
    match = _RIAL_PRICE_RE.search(text, label.end())
    if not match:
        return None
    value = to_float(match.group(1))
    if value is None:
        return None
    return Observation(
        provider_code="milligold",
        symbol="IR_GOLD_18K",
        raw_value=value,
        raw_unit="IRR/gram (html)",
        raw_currency="IRR",
        value=value / 10.0,   # rial -> toman
        currency="IRT",
        unit="gram",
        observed_at=utcnow(),
        raw_payload={"matched": match.group(0)},
    )


class MilligoldProvider(Provider):
    code = "milligold"
    category = "iran_gold"

    def fetch(self) -> list[Observation]:
        html = self._get_text(HOME_URL)
        observation = parse_home(html)
        if observation is None:
            raise ProviderError(
                "milligold: no 18k price found on the homepage "
                "(layout changed or challenge page served; not bypassing)"
            )
        return [observation]
