"""margani/pricedb GitHub dataset — keyless Iranian price mirror.

The MIT-licensed repository https://github.com/margani/pricedb is refreshed by
a GitHub Actions workflow and serves TGJU-scraped quotes as static JSON via
raw.githubusercontent.com.  Layout (verified 2026-07-20 against the live repo
tree and raw files)::

    tgju/current/<slug>/latest.json          # {"p": "186,994,000",
                                             #  "h": "...", "l": "...",
                                             #  "ts": "2026-04-28 00:00:00"}
    tgju/current/<slug>/history.json         # array of the same objects,
                                             # one per day, oldest first
    tgju/current/<slug>/hourly-history.json  # same shape, hourly resolution

Unit determination — values are **RIALS**, so normalize ÷10 to IRT:

* the README (https://github.com/margani/pricedb) describes the dataset as
  "prices of currencies, gold, etc in IRR (Iranian Rial)";
* the data directory is literally ``tgju/`` and TGJU quotes rials
  (see providers/tgju.py / docs/CONTRACTS.md);
* fetched values match TGJU's rial scale, e.g. ``geram18`` latest.json
  (https://raw.githubusercontent.com/margani/pricedb/main/tgju/current/geram18/latest.json)
  showed "186,994,000" — ~18.7M toman per 18k gram, coherent with TGJU.

``ts`` strings are Tehran-local like their TGJU source and are parsed with the
same helper.  The dataset updates at most daily and can lag when the upstream
Actions workflow stalls, so quotes may be older than live providers —
``observed_at`` reports the file's own timestamp honestly and the collect
loop's freshness/priority machinery does the rest.  If raw.githubusercontent
is unreachable the provider simply fails fast (no scraping fallback).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from ..core.normalize import rial_to_toman
from .base import Observation, Provider, ProviderError
from .tgju import parse_ts

BASE_URL = "https://raw.githubusercontent.com/margani/pricedb/main/tgju/current/{slug}/{file}"

# slug -> (canonical symbol, raw unit, raw currency) — all quoted in rials.
SLUG_MAP: dict[str, tuple[str, str, str]] = {
    "geram18": ("IR_GOLD_18K", "IRR/gram", "IRR"),
    "sekee": ("IR_COIN_EMAMI", "IRR/coin", "IRR"),
    "price_dollar_rl": ("USD_IRT", "IRR/usd", "IRR"),
}

_NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")


def _to_float(cell: Any) -> Optional[float]:
    """Parse a pricedb numeric cell ('186,994,000' or plain number)."""
    if cell is None:
        return None
    if isinstance(cell, (int, float)):
        return float(cell)
    s = str(cell).strip()
    if not _NUM_RE.match(s):
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _make_observation(slug: str, raw_value: float, item: dict) -> Observation:
    symbol, raw_unit, raw_currency = SLUG_MAP[slug]
    return Observation(
        provider_code="pricedb",
        symbol=symbol,
        raw_value=raw_value,
        raw_unit=raw_unit,
        raw_currency=raw_currency,
        value=rial_to_toman(raw_value),  # IRR -> IRT per docs/CONTRACTS.md
        currency="IRT",
        unit=raw_unit.split("/", 1)[1],
        observed_at=parse_ts(item.get("ts")),
        raw_payload={k: item.get(k) for k in ("p", "h", "l", "ts")},
    )


def parse_latest(payload: Any, slug: str) -> Optional[Observation]:
    """Parse one ``latest.json`` object ({"p", "h", "l", "ts"})."""
    if slug not in SLUG_MAP or not isinstance(payload, dict):
        return None
    value = _to_float(payload.get("p"))
    if value is None or value <= 0:
        return None
    return _make_observation(slug, value, payload)


def parse_history(payload: Any, slug: str) -> list[tuple[date, float]]:
    """Parse ``history.json`` (array of {"p","h","l","ts"}) to (date, raw
    rial close) pairs, ascending by date, one row per day (last write wins)."""
    out: dict[date, float] = {}
    if slug not in SLUG_MAP or not isinstance(payload, list):
        return []
    for item in payload:
        if not isinstance(item, dict):
            continue
        close = _to_float(item.get("p"))
        if close is None or close <= 0:
            continue
        day = parse_ts(item.get("ts")).date()
        out[day] = close
    return sorted(out.items())


def normalize_history_value(slug: str, raw_close: float) -> float:
    """Apply the same rial->toman normalization used for live quotes."""
    return rial_to_toman(raw_close)  # every SLUG_MAP entry is IRR


class PriceDBProvider(Provider):
    """Static-JSON mirror of TGJU quotes; also exposes daily history for
    seeding (fetch_history), same call shape as TGJUProvider."""

    code = "pricedb"
    category = "iran_gold"

    def fetch(self) -> list[Observation]:
        observations: list[Observation] = []
        errors: list[str] = []
        for slug in SLUG_MAP:
            url = BASE_URL.format(slug=slug, file="latest.json")
            try:
                payload = self._get_json(url)
            except ProviderError as exc:
                errors.append(str(exc))
                continue
            obs = parse_latest(payload, slug)
            if obs is not None:
                observations.append(obs)
            else:
                errors.append(f"{slug}: unparseable latest.json")
        if not observations:
            raise ProviderError(f"pricedb: no observations ({'; '.join(errors)})")
        return observations

    def fetch_history(self, slug: str) -> list[tuple[date, float]]:
        """Daily close history (raw rial values — normalize via
        :func:`normalize_history_value`)."""
        if slug not in SLUG_MAP:
            raise ValueError(f"unknown pricedb slug: {slug}")
        payload = self._get_json(BASE_URL.format(slug=slug, file="history.json"))
        rows = parse_history(payload, slug)
        if not rows:
            raise ProviderError(f"pricedb: empty history for {slug}")
        return rows
