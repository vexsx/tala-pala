"""Unit/currency normalization helpers.

Canonical storage rules (docs/CONTRACTS.md):

* Iranian values are stored in **IRT (toman)**; providers quoting rials (TGJU)
  are divided by 10 and the raw rial value is preserved in ``raw_observations``.
* ``^TNX`` (Yahoo's US 10-year note ticker) quotes ten times the yield:
  a quote of ``43.5`` means **4.35%** — divide by 10.
* Global metals are USD per troy ounce; sanity constants below let validation
  catch gram/ounce mix-ups.
"""
from __future__ import annotations

from .formula import TROY_OUNCE_GRAMS  # re-export for convenience

__all__ = [
    "TROY_OUNCE_GRAMS",
    "SYMBOL_META",
    "rial_to_toman",
    "toman_to_rial",
    "tnx_to_pct",
]

# symbol -> (currency, unit) for normalized `prices` rows.
SYMBOL_META: dict[str, tuple[str, str]] = {
    "IR_GOLD_18K": ("IRT", "gram"),
    "IR_COIN_EMAMI": ("IRT", "coin"),
    "USD_IRT": ("IRT", "usd"),
    "XAUUSD": ("USD", "ozt"),
    "XAGUSD": ("USD", "ozt"),
    "BRENT_OIL": ("USD", "bbl"),
    "DXY": ("INDEX", "index"),
    "US10Y": ("PCT", "pct"),
}


def rial_to_toman(value_irr: float) -> float:
    """1 toman = 10 rials."""
    return value_irr / 10.0


def toman_to_rial(value_irt: float) -> float:
    return value_irt * 10.0


def tnx_to_pct(tnx_quote: float) -> float:
    """Yahoo ^TNX quotes 10x the yield: 43.5 -> 4.35 (percent)."""
    return tnx_quote / 10.0
