"""TGJU (tgju.org) provider — primary Iranian gold / FX source.

Endpoint layout (verified 2026-07-20):

* **Live snapshot**: ``https://call2.tgju.org/ajax.json`` (fallback hosts
  ``call3.tgju.org`` then ``call4.tgju.org`` — other callN hosts do not
  resolve).  The payload has top-level keys ``current`` / ``tolerance_low`` /
  ``tolerance_high`` / ``last``; under ``current`` each indicator looks like::

      "geram18": {"p": "182,954,000", "h": "...", "l": "...", "d": "...",
                  "dp": 2.58, "dt": "low", "t": "۱۴:۱۵:۳۹",
                  "t_en": "14:15:39", "ts": "2026-07-20 14:15:39"}

  ``p`` is the last price as a comma-formatted **rial** string (except
  ``ons``, which is the global ounce in USD); ``ts`` is Tehran local time.

* **Daily history** (used by the seed script, not the live collect loop):
  ``https://api.tgju.org/v1/market/indicator/summary-table-data/<slug>``
  returns DataTables JSON ``{"recordsTotal": N, "data": [[open, low, high,
  close, "<span ...>change</span>", "<span ...>pct</span>", "2026/07/19",
  "1405/04/28"], ...]}`` with paging params ``start``/``length``.

Iranian instruments are quoted in **RIALS** and normalized to IRT (÷10) per
docs/CONTRACTS.md; ``ons`` is USD and passed through.  Requests must carry a
User-Agent (UA-less clients get empty responses) — we identify honestly via
the base class UA and never bypass captchas or auth walls.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from ..core.normalize import rial_to_toman
from ..db import utcnow
from .base import Observation, Provider, ProviderError

LIVE_URLS = (
    "https://call2.tgju.org/ajax.json",
    "https://call3.tgju.org/ajax.json",
    "https://call4.tgju.org/ajax.json",
)
HISTORY_URL = "https://api.tgju.org/v1/market/indicator/summary-table-data/{slug}"

TEHRAN_OFFSET = timezone(timedelta(hours=3, minutes=30))  # no DST since 2022

# slug -> (canonical symbol, raw unit, raw currency)
SLUG_MAP: dict[str, tuple[str, str, str]] = {
    "geram18": ("IR_GOLD_18K", "IRR/gram", "IRR"),
    "sekee": ("IR_COIN_EMAMI", "IRR/coin", "IRR"),
    "price_dollar_rl": ("USD_IRT", "IRR/usd", "IRR"),
    "ons": ("XAUUSD", "USD/ozt", "USD"),
}

_PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
_NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(cell: Any) -> str:
    """Remove HTML tags from a change cell like '<span class="low">3707000</span>'."""
    return _TAG_RE.sub("", str(cell)).strip()


def _to_float(cell: Any) -> Optional[float]:
    """Parse a TGJU numeric cell defensively (commas, Persian digits, HTML)."""
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    s = strip_html(cell).translate(_PERSIAN_DIGITS).replace("‌", "")
    if not _NUM_RE.match(s):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def parse_ts(ts: Any) -> datetime:
    """Parse TGJU 'YYYY-MM-DD HH:MM:SS' Tehran-local timestamps to aware UTC."""
    if isinstance(ts, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                local = datetime.strptime(ts.strip(), fmt).replace(tzinfo=TEHRAN_OFFSET)
                return local.astimezone(timezone.utc)
            except ValueError:
                continue
    return utcnow()


def _make_observation(
    slug: str, raw_value: float, observed_at: datetime, payload: Optional[dict]
) -> Observation:
    symbol, raw_unit, raw_currency = SLUG_MAP[slug]
    unit = raw_unit.split("/", 1)[1]
    if raw_currency == "IRR":
        value, currency = rial_to_toman(raw_value), "IRT"
    else:  # 'ons' — already USD per troy ounce
        value, currency = raw_value, "USD"
    return Observation(
        provider_code="tgju",
        symbol=symbol,
        raw_value=raw_value,
        raw_unit=raw_unit,
        raw_currency=raw_currency,
        value=value,
        currency=currency,
        unit=unit,
        observed_at=observed_at,
        raw_payload=payload,
    )


def parse_live(payload: Any) -> list[Observation]:
    """Parse the callN ajax.json 'current' block for all known slugs."""
    out: list[Observation] = []
    if not isinstance(payload, dict):
        return out
    current = payload.get("current")
    if not isinstance(current, dict):
        return out
    for slug in SLUG_MAP:
        item = current.get(slug)
        if not isinstance(item, dict):
            continue
        value = _to_float(item.get("p"))
        if value is None or value <= 0:
            continue
        observed_at = parse_ts(item.get("ts"))
        keep = {k: item.get(k) for k in ("p", "h", "l", "d", "dp", "dt", "ts")}
        out.append(_make_observation(slug, value, observed_at, keep))
    return out


def parse_history(payload: Any, slug: str) -> list[tuple[date, float]]:
    """Parse summary-table-data rows to (gregorian_date, close) pairs.

    Row layout: [open, low, high, close, change_html, change_pct_html,
    'YYYY/MM/DD' (Gregorian), 'YYYY/MM/DD' (Jalali)].
    """
    out: list[tuple[date, float]] = []
    if slug not in SLUG_MAP or not isinstance(payload, dict):
        return out
    rows = payload.get("data")
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            continue
        close = _to_float(row[3])
        if close is None or close <= 0:
            continue
        raw_date = strip_html(row[6]).translate(_PERSIAN_DIGITS)
        try:
            day = datetime.strptime(raw_date, "%Y/%m/%d").date()
        except ValueError:
            continue
        out.append((day, close))
    out.sort(key=lambda pair: pair[0])
    return out


def normalize_history_value(slug: str, raw_close: float) -> float:
    """Apply the same rial->toman normalization used for live quotes."""
    _, _, raw_currency = SLUG_MAP[slug]
    return rial_to_toman(raw_close) if raw_currency == "IRR" else raw_close


class TGJUProvider(Provider):
    """Live snapshot provider; also exposes daily history for seeding.

    Serves Iranian symbols AND a coherent XAUUSD (``ons``) from the same feed,
    so it doubles as a global_gold source ahead of Yahoo.
    """

    code = "tgju"
    category = "iran_gold"

    def fetch(self) -> list[Observation]:
        errors: list[str] = []
        for url in LIVE_URLS:
            try:
                payload = self._get_json(url)
            except ProviderError as exc:
                errors.append(str(exc))
                continue
            observations = parse_live(payload)
            if observations:
                return observations
            errors.append(f"{url}: no parseable indicators")
        raise ProviderError(f"tgju: no observations ({'; '.join(errors) or 'empty'})")

    def fetch_history(self, slug: str, max_rows: int = 1200) -> list[tuple[date, float]]:
        """Daily close history (raw provider units — normalize via
        :func:`normalize_history_value`)."""
        if slug not in SLUG_MAP:
            raise ValueError(f"unknown tgju slug: {slug}")
        payload = self._get_json(
            HISTORY_URL.format(slug=slug),
            params={"start": 0, "length": max_rows},
        )
        rows = parse_history(payload, slug)
        if not rows:
            raise ProviderError(f"tgju: empty history for {slug}")
        return rows
