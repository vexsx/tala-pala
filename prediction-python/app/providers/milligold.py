"""Milli Gold (milli.gold) keyless HTML provider — 18k gram price only.

The homepage server-renders the CURRENT 1-gram 18k price inside the only
element whose class list contains ``text-deepOcean-focus`` (verified
2026-07-21), e.g.::

    <p class="font-bold text-title1 ... text-deepOcean-focus ...">183,830,000ریال</p>

The page ALSO renders the day's highest/lowest prices (labels ``بیشترین``/
``کمترین``) in plain ``text-black-11`` elements that appear BEFORE the
current price in DOM order — a naive "first rial amount after the 18k label"
match returns the day HIGH, not the current price (production bug fixed
2026-07-21).  Extraction is therefore class-anchored first, with a
label-context fallback that skips amounts near the high/low labels.

Values are **RIAL**, divided by 10 to toman during normalization.  Digits may
be ASCII or Persian/Arabic-Indic — normalized by :mod:`.htmlparse` before
matching.  ``observed_at`` is the collection time (the page shows no
machine-readable quote timestamp).  If the page ever starts serving challenge
pages the provider fails gracefully with a clear error — never bypassed.
"""
from __future__ import annotations

import re
from typing import Optional

from ..db import utcnow
from .base import Observation, Provider, ProviderError
from .htmlparse import normalize_digits, to_float, visible_text

HOME_URL = "https://milli.gold/"

# Class-anchored current price: the only element carrying text-deepOcean-focus
# with a rial amount as content (raw HTML, digits normalized beforehand).
_CURRENT_RE = re.compile(
    r'class="[^"]*text-deepOcean-focus[^"]*"[^>]*>\s*'
    r"(\d{1,3}(?:,\d{3})+|\d{7,})\s*ریال"
)

# Fallback (flattened text): rial amounts anywhere on the page, skipping any
# whose nearby context (either side — RTL layouts put the descriptor AFTER the
# number in DOM order, e.g. "184,911,000ریال بالاترین قیمت") names the day
# high/low or a change percentage.
_LABEL_RE = re.compile(r"قیمت\s*1\s*گرم\s*طلای\s*18\s*عیار")
_RIAL_PRICE_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d{7,})\s*ریال")
_HIGH_LOW_RE = re.compile(r"بیشترین|کمترین|بالاترین|پایین[‌\s]*ترین|تغییرات")
# Descriptors sit ADJACENT to their own number: keep the look-behind tight so
# a previous element's high/low label can't bleed into the next candidate's
# context; RTL layouts put the descriptor after the number, so look-ahead is
# wider.
_CONTEXT_BEFORE = 15
_CONTEXT_AFTER = 40


def _extract_current_rial(html: str) -> Optional[float]:
    """The current (not day-high/low) 18k rial amount, or None."""
    normalized = normalize_digits(html)
    match = _CURRENT_RE.search(normalized)
    if match:
        return to_float(match.group(1))
    # Layout changed and the class anchor is gone: fall back to a page-wide
    # search anchored on the 18k label's PRESENCE (as a sanity check that this
    # is still the gold page), taking the first rial amount whose surrounding
    # context on EITHER side does not name the day high/low or a change %.
    # The current price renders before the label in DOM order, so the search
    # must not be restricted to text after the label.
    text = visible_text(html)
    if not _LABEL_RE.search(text):
        return None
    for m in _RIAL_PRICE_RE.finditer(text):
        before = text[max(0, m.start() - _CONTEXT_BEFORE):m.start()]
        after = text[m.end():m.end() + _CONTEXT_AFTER]
        if _HIGH_LOW_RE.search(before) or _HIGH_LOW_RE.search(after):
            continue
        return to_float(m.group(1))
    return None


def parse_home(html: str) -> Optional[Observation]:
    """Extract the current 18k gram price (RIAL → ÷10 toman) or None."""
    value = _extract_current_rial(html)
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
        raw_payload={"matched_rial": value},
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
