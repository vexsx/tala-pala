"""Rial/toman and ^TNX normalization tests."""
from __future__ import annotations

import pytest

from app.core.normalize import SYMBOL_META, rial_to_toman, tnx_to_pct, toman_to_rial


def test_rial_to_toman():
    assert rial_to_toman(182_954_000.0) == pytest.approx(18_295_400.0)
    assert rial_to_toman(10.0) == 1.0


def test_toman_roundtrip():
    assert toman_to_rial(rial_to_toman(1_065_300.0)) == pytest.approx(1_065_300.0)


def test_tnx_scaling():
    # ^TNX quotes 10x the yield: 43.5 => 4.35%
    assert tnx_to_pct(43.5) == pytest.approx(4.35)
    assert tnx_to_pct(42.5) == pytest.approx(4.25)


def test_symbol_meta_contract():
    assert SYMBOL_META["IR_GOLD_18K"] == ("IRT", "gram")
    assert SYMBOL_META["XAUUSD"] == ("USD", "ozt")
    assert SYMBOL_META["USD_IRT"] == ("IRT", "usd")
    assert SYMBOL_META["US10Y"] == ("PCT", "pct")
    assert SYMBOL_META["DXY"] == ("INDEX", "index")
