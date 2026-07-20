"""Exact-number tests for the theoretical price formulas (docs/CONTRACTS.md)."""
from __future__ import annotations

import pytest

from app.core.formula import (
    TROY_OUNCE_GRAMS,
    k18_theoretical_irt,
    premium_pct,
    pure_gram_irt,
    pure_gram_usd,
)


def test_troy_ounce_constant():
    assert TROY_OUNCE_GRAMS == 31.1034768


def test_pure_gram_usd_exact():
    # 3300 / 31.1034768
    assert pure_gram_usd(3300.0) == pytest.approx(106.09746367647234, rel=1e-12)


def test_pure_gram_irt_exact():
    assert pure_gram_irt(3300.0, 100_000.0) == pytest.approx(10_609_746.367647234, rel=1e-12)


def test_k18_exact():
    # 3300 / 31.1034768 * 100000 * 0.75
    assert k18_theoretical_irt(3300.0, 100_000.0) == pytest.approx(
        7_957_309.775735426, rel=1e-12
    )


def test_premium_pct():
    theo = k18_theoretical_irt(3300.0, 100_000.0)
    observed = theo * 1.05
    assert premium_pct(observed, theo) == pytest.approx(5.0, rel=1e-9)
    assert premium_pct(theo, theo) == pytest.approx(0.0, abs=1e-12)
    assert premium_pct(theo * 0.9, theo) == pytest.approx(-10.0, rel=1e-9)


def test_premium_zero_theoretical_raises():
    with pytest.raises(ValueError):
        premium_pct(1.0, 0.0)
