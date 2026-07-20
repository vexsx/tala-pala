"""Theoretical gold price formulas (docs/CONTRACTS.md, validated by tests).

Pure functions only — no I/O.
"""
from __future__ import annotations

TROY_OUNCE_GRAMS = 31.1034768  # grams per troy ounce (exact definition)
KARAT_18_PURITY = 0.750


def pure_gram_usd(xau_usd: float) -> float:
    """USD price of one gram of pure (24k) gold given XAUUSD per troy ounce."""
    return xau_usd / TROY_OUNCE_GRAMS


def pure_gram_irt(xau_usd: float, usd_irt: float) -> float:
    """IRT (toman) price of one gram of pure gold."""
    return pure_gram_usd(xau_usd) * usd_irt


def k18_theoretical_irt(xau_usd: float, usd_irt: float) -> float:
    """Theoretical IRT price of one gram of 18-karat gold (75.0% purity)."""
    return pure_gram_irt(xau_usd, usd_irt) * KARAT_18_PURITY


def premium_pct(observed_18k_irt: float, theoretical_18k_irt: float) -> float:
    """Observed market premium over the theoretical 18k price, in percent."""
    if theoretical_18k_irt == 0:
        raise ValueError("theoretical price must be non-zero")
    return (observed_18k_irt - theoretical_18k_irt) / theoretical_18k_irt * 100.0
