"""Rial/toman and ^TNX normalization tests."""
from __future__ import annotations

import pytest

from app.core.normalize import (
    SYMBOL_META,
    TNX_MAX_YIELD_PCT,
    rial_to_toman,
    tnx_quote_meta,
    tnx_to_pct,
    toman_to_rial,
)
from app.core.validation import SANITY_RANGES, sanity_ok


def test_rial_to_toman():
    assert rial_to_toman(182_954_000.0) == pytest.approx(18_295_400.0)
    assert rial_to_toman(10.0) == 1.0


def test_toman_roundtrip():
    assert toman_to_rial(rial_to_toman(1_065_300.0)) == pytest.approx(1_065_300.0)


def test_tnx_scaling():
    # ^TNX under the CBOE index convention quotes 10x the yield: 43.5 => 4.35%
    assert tnx_to_pct(43.5) == pytest.approx(4.35)
    assert tnx_to_pct(42.5) == pytest.approx(4.25)


def test_tnx_direct_percent_quote_is_not_divided():
    """The defect that cost five years of US10Y history: Yahoo served the
    yield itself (4.697 = 4.697%) and it was divided anyway, storing 0.4697."""
    assert tnx_to_pct(4.697) == pytest.approx(4.697)
    assert tnx_to_pct(1.174) == pytest.approx(1.174)
    # ... and after the 2026-08-11 flip back to the index form, the same
    # function reads 46.82 as 4.682% without any code change.
    assert tnx_to_pct(46.82) == pytest.approx(4.682)


def test_tnx_rule_is_the_documented_plausibility_ceiling():
    """Dividing is a repair for a quote that cannot be a yield, and the
    threshold is the sanity band's own ceiling — not a second opinion."""
    assert TNX_MAX_YIELD_PCT == SANITY_RANGES["US10Y"][1]
    assert tnx_to_pct(TNX_MAX_YIELD_PCT) == pytest.approx(TNX_MAX_YIELD_PCT)
    just_over = TNX_MAX_YIELD_PCT + 0.01
    assert tnx_to_pct(just_over) == pytest.approx(just_over / 10.0)
    # every reading the rule produces is storable, from either convention
    assert sanity_ok("US10Y", tnx_to_pct(46.82))
    assert sanity_ok("US10Y", tnx_to_pct(4.697))


def test_tnx_quote_meta_labels_the_convention_used():
    """raw_observations must record which reading was applied, or the
    normalization it exists to make auditable is unauditable."""
    assert tnx_quote_meta(46.82) == ("TNX_index", "INDEX")
    assert tnx_quote_meta(4.697) == ("pct", "PCT")


def test_symbol_meta_contract():
    assert SYMBOL_META["IR_GOLD_18K"] == ("IRT", "gram")
    assert SYMBOL_META["XAUUSD"] == ("USD", "ozt")
    assert SYMBOL_META["USD_IRT"] == ("IRT", "usd")
    assert SYMBOL_META["US10Y"] == ("PCT", "pct")
    assert SYMBOL_META["DXY"] == ("INDEX", "index")
