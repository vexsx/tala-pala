"""Helpers for parsing prices out of public server-rendered HTML pages.

Used by the keyless HTML modes of :mod:`.alanchand` and :mod:`.milligold`.
Per project policy (docs/data-sources.md) HTML parsing is only done against
pages that render their values server-side with no anti-bot measures; when a
page starts serving challenges the providers fail gracefully — nothing is
ever bypassed.
"""
from __future__ import annotations

import html as html_module
import re
from typing import Any, Optional

_TAG_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")

# zero-width / directional characters that Persian pages sprinkle into text
_INVISIBLES = dict.fromkeys(map(ord, "​‌‍‎‏﻿"), " ")

# Persian (U+06F0..) and Arabic-Indic (U+0660..) digits -> ASCII; the Persian
# thousands separator U+066C -> ',' and decimal separator U+066B -> '.'
_DIGIT_MAP = {ord(p): str(i) for i, p in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_DIGIT_MAP.update({ord(a): str(i) for i, a in enumerate("٠١٢٣٤٥٦٧٨٩")})
_DIGIT_MAP[0x066C] = ","
_DIGIT_MAP[0x066B] = "."


def normalize_digits(text: str) -> str:
    """ASCII-fy Persian/Arabic-Indic digits and separators."""
    return text.translate(_DIGIT_MAP)


def visible_text(html: str) -> str:
    """Tag-stripped, entity-decoded, digit-normalized, whitespace-collapsed
    text of an HTML document — what a reader effectively sees."""
    text = html_module.unescape(html)
    text = text.translate(_INVISIBLES)
    text = _TAG_RE.sub(" ", text)
    text = normalize_digits(text)
    return _WS_RE.sub(" ", text)


def to_float(raw: Any) -> Optional[float]:
    """Comma-tolerant positive float parse (None on garbage/non-positive)."""
    try:
        value = float(normalize_digits(str(raw)).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
