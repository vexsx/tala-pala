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

* **Daily history** (used by the seed script, the deep backfill and the
  scheduled daily-close job, never by the live collect loop):
  ``https://api.tgju.org/v1/market/indicator/summary-table-data/<slug>``
  returns DataTables JSON ``{"recordsTotal": N, "data": [[open, low, high,
  close, "<span ...>change</span>", "<span ...>pct</span>", "2026/07/19",
  "1405/04/28"], ...]}`` with paging params ``start``/``length``, newest row
  first.  Measured 2026-09-29: ``length=N`` returns the N newest rows, a large
  ``length`` (or none) returns the whole table, ``length=-1`` silently DROPS
  the oldest row, and the day in progress is never in the table.

Iranian instruments are quoted in **RIALS** and normalized to IRT (÷10) per
docs/CONTRACTS.md; ``ons`` is USD and passed through.  Requests must carry a
User-Agent (UA-less clients get empty responses) — we identify honestly via
the base class UA and never bypass captchas or auth walls.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple, Optional

from ..core.normalize import rial_to_toman
from ..db import utcnow
from .base import Observation, Provider, ProviderError

LIVE_URLS = (
    "https://call2.tgju.org/ajax.json",
    "https://call3.tgju.org/ajax.json",
    "https://call4.tgju.org/ajax.json",
)
HISTORY_URL = "https://api.tgju.org/v1/market/indicator/summary-table-data/{slug}"


# Rows requested per history call.  1200 covers roughly the last three years,
# which is all the seed script ever wanted.  It is NOT enough for the whole
# table — verified from production on 2026-09-09: geram18 3498 rows, sekee
# 4284 — so the deep-history backfill passes its own, larger cap and checks
# the counts (see app/jobs/tgju_backfill.py).
DEFAULT_HISTORY_ROWS = 1200

TEHRAN_OFFSET = timezone(timedelta(hours=3, minutes=30))  # no DST since 2022

# slug -> (canonical symbol, raw unit, raw currency)
SLUG_MAP: dict[str, tuple[str, str, str]] = {
    "geram18": ("IR_GOLD_18K", "IRR/gram", "IRR"),
    "sekee": ("IR_COIN_EMAMI", "IRR/coin", "IRR"),
    "price_dollar_rl": ("USD_IRT", "IRR/usd", "IRR"),
    "ons": ("XAUUSD", "USD/ozt", "USD"),
}

# Slugs read ONLY from the daily history table, never from the live snapshot
# (app/jobs/tgju_daily.py, migration 0031).  Same layout as SLUG_MAP, and kept
# apart from it on purpose: parse_live walks SLUG_MAP, so a slug added there is
# also collected live on every tick, which is exactly what these are not.  They
# are one settled close per day, published by TGJU after the day is over.
#
# Units measured 2026-09-29 against the table's own numbers: silver_999 is
# rials per GRAM (5,065,700 IRR / 244,800 IRT per USD = 64.36 USD/ozt against
# COMEX 61.14, a +5.3% local premium); the coins are per COIN; geram24 is per
# gram and mesghal per MESGHAL (4.6083 g at 705 per mille), both published by
# TGJU as fixed multiples of geram18 (x4/3 and x4.3318).  Deliberately NOT
# 'silver': that slug is the global ounce in USD and its history endpoint hangs.
HISTORY_SLUG_MAP: dict[str, tuple[str, str, str]] = {
    "silver_999": ("IR_SILVER_999", "IRR/gram", "IRR"),
    "sekeb": ("IR_COIN_BAHAR", "IRR/coin", "IRR"),
    "nim": ("IR_COIN_HALF", "IRR/coin", "IRR"),
    "rob": ("IR_COIN_QUARTER", "IRR/coin", "IRR"),
    "gerami": ("IR_COIN_GERAMI", "IRR/coin", "IRR"),
    "geram24": ("IR_GOLD_24K", "IRR/gram", "IRR"),
    "mesghal": ("IR_GOLD_MESGHAL", "IRR/mesghal", "IRR"),
}


def slug_meta(slug: str) -> tuple[str, str, str]:
    """(canonical symbol, raw unit, raw currency) for a live or history-only slug.

    Raises ``KeyError`` for a slug in neither map: an unknown slug has no unit,
    and guessing one is how a mesghal price ends up stored per gram.
    """
    if slug in SLUG_MAP:
        return SLUG_MAP[slug]
    return HISTORY_SLUG_MAP[slug]


def known_history_slug(slug: str) -> bool:
    """Whether the daily history table may be read for ``slug``."""
    return slug in SLUG_MAP or slug in HISTORY_SLUG_MAP


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


class HistoryRow(NamedTuple):
    """One parsed daily bar, with the close exactly as TGJU printed it.

    ``close`` is the parsed number in the provider's own unit (rials);
    ``close_text`` is the cell itself ("5,065,700"), kept so an audit row can
    show what was published rather than only what we made of it; ``jalali`` is
    the table's own Jalali date for the same day, for a reader checking a bar
    against TGJU's page.
    """

    day: date
    close: float
    close_text: str
    jalali: str


def parse_history_rows(payload: Any, slug: str) -> list[HistoryRow]:
    """Parse summary-table-data rows, oldest first.

    Row layout: [open, low, high, close, change_html, change_pct_html,
    'YYYY/MM/DD' (Gregorian), 'YYYY/MM/DD' (Jalali)].  Rows whose close is not
    a positive number, or whose Gregorian date does not parse, are dropped.
    """
    out: list[HistoryRow] = []
    if not known_history_slug(slug) or not isinstance(payload, dict):
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
        jalali = strip_html(row[7]).translate(_PERSIAN_DIGITS) if len(row) > 7 else ""
        out.append(HistoryRow(day, close, strip_html(row[3]), jalali))
    out.sort(key=lambda r: r.day)
    return out


def parse_history(payload: Any, slug: str) -> list[tuple[date, float]]:
    """Parse summary-table-data rows to (gregorian_date, close) pairs.

    Row layout: [open, low, high, close, change_html, change_pct_html,
    'YYYY/MM/DD' (Gregorian), 'YYYY/MM/DD' (Jalali)].
    """
    return [(r.day, r.close) for r in parse_history_rows(payload, slug)]


class HistoryPage(NamedTuple):
    """One ``summary-table-data`` response plus the evidence about truncation.

    Parsed rows alone cannot say whether a history is COMPLETE: a payload cut
    short by the ``length`` parameter and a payload that simply is that short
    look identical once parsed.  ``returned_rows`` (what the payload actually
    carried) and ``records_total`` (what the server says the table holds) are
    kept beside the rows so a caller can compare the two and refuse a
    truncated series rather than silently store a fragment of one.

    ``returned_rows`` counts the payload's rows BEFORE parsing, because
    :func:`parse_history` legitimately drops unparseable ones; comparing the
    parsed count against ``records_total`` would report a truncation every
    time the provider emitted one junk row.
    """

    rows: list[tuple[date, float]]
    returned_rows: int
    records_total: Optional[int]
    # The same bars as ``rows`` with the published close text and Jalali date
    # kept (see HistoryRow).  Defaulted so a page built from bare pairs — as
    # tests of the deep backfill do — is still a valid page.
    bars: tuple[HistoryRow, ...] = ()


def _records_total(payload: Any) -> Optional[int]:
    """The DataTables row count the server reports, or None if it did not."""
    if not isinstance(payload, dict):
        return None
    for key in ("recordsTotal", "recordsFiltered"):
        value = payload.get(key)
        if isinstance(value, bool):  # bool is an int subclass; not a count
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    return None


def history_page(payload: Any, slug: str) -> HistoryPage:
    """Parse a history payload, keeping the counts that prove completeness."""
    bars = parse_history_rows(payload, slug)
    rows = [(b.day, b.close) for b in bars]
    data = payload.get("data") if isinstance(payload, dict) else None
    returned = len(data) if isinstance(data, list) else len(rows)
    return HistoryPage(rows=rows, returned_rows=returned,
                       records_total=_records_total(payload), bars=tuple(bars))


def normalize_history_value(slug: str, raw_close: float) -> float:
    """Apply the same rial->toman normalization used for live quotes."""
    _, _, raw_currency = slug_meta(slug)
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

    def fetch_history_page(
        self, slug: str, max_rows: int = DEFAULT_HISTORY_ROWS
    ) -> HistoryPage:
        """Daily close history WITH the counts that reveal truncation.

        Raw provider units — normalize via :func:`normalize_history_value`.
        """
        if not known_history_slug(slug):
            raise ValueError(f"unknown tgju slug: {slug}")
        payload = self._get_json(
            HISTORY_URL.format(slug=slug),
            params={"start": 0, "length": int(max_rows)},
        )
        page = history_page(payload, slug)
        if not page.rows:
            raise ProviderError(f"tgju: empty history for {slug}")
        return page

    def fetch_history(
        self, slug: str, max_rows: int = DEFAULT_HISTORY_ROWS
    ) -> list[tuple[date, float]]:
        """Daily close history (raw provider units — normalize via
        :func:`normalize_history_value`).

        Truncation-blind by construction: it returns rows and nothing that
        could contradict them, so a caller that needs the WHOLE table must use
        :meth:`fetch_history_page` and compare its counts.
        """
        return self.fetch_history_page(slug, max_rows=max_rows).rows
