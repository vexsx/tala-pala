"""Outlier detection, jump rule, premium cross-check and dedupe keys."""
from __future__ import annotations

from datetime import datetime, timezone

from app.core import validation


def test_dedupe_key_deterministic_and_distinct():
    ts = datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc)
    k1 = validation.build_dedupe_key("tgju", "IR_GOLD_18K", ts, 182954000.0)
    k2 = validation.build_dedupe_key("tgju", "IR_GOLD_18K", ts, 182954000.0)
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex
    # any component change changes the key
    assert k1 != validation.build_dedupe_key("yahoo", "IR_GOLD_18K", ts, 182954000.0)
    assert k1 != validation.build_dedupe_key("tgju", "USD_IRT", ts, 182954000.0)
    assert k1 != validation.build_dedupe_key("tgju", "IR_GOLD_18K", ts, 182954001.0)


def test_dedupe_key_naive_datetime_treated_as_utc():
    naive = datetime(2026, 7, 20, 10, 0, 0)
    aware = datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc)
    assert validation.build_dedupe_key("p", "S", naive, 1.0) == \
        validation.build_dedupe_key("p", "S", aware, 1.0)


def test_sanity_ranges_catch_unit_mixups():
    # rial value that was NOT divided by 10 for USD_IRT is still in range,
    # but a gram/ounce mixup on XAUUSD is caught
    assert validation.sanity_ok("XAUUSD", 3350.0)
    assert not validation.sanity_ok("XAUUSD", 107.7)  # per-gram value slipped in
    assert validation.sanity_ok("IR_GOLD_18K", 18_295_400.0)
    assert not validation.sanity_ok("IR_GOLD_18K", 1_829.0)  # thousands mixup
    assert validation.sanity_ok("US10Y", 4.35)
    assert not validation.sanity_ok("US10Y", 43.5)  # un-normalized ^TNX


def test_mad_outlier():
    window = [100.0, 101.0, 99.5, 100.5, 100.2, 99.8, 100.1]
    assert not validation.is_mad_outlier(100.3, window)
    assert validation.is_mad_outlier(150.0, window)
    # tiny windows never flag
    assert not validation.is_mad_outlier(150.0, [100.0, 101.0])


def test_classify_jump_needs_second_source():
    recent = [100.0] * 10
    quality, reason = validation.classify_observation("XAUUSD", 3350.0, [], None)
    assert quality == "ok" and reason is None
    # >15% jump vs last good => suspect
    quality, reason = validation.classify_observation("XAUUSD", 4000.0, recent + [3350.0], 3350.0)
    assert quality == "suspect"
    assert "jump" in (reason or "")
    # within 15% => ok (window consistent)
    window = [3300.0, 3310.0, 3320.0, 3340.0, 3350.0, 3345.0]
    quality, _ = validation.classify_observation("XAUUSD", 3400.0, window, 3350.0)
    assert quality == "ok"


def test_classify_out_of_range_is_outlier():
    quality, reason = validation.classify_observation("XAUUSD", 33500.0, [], None)
    assert quality == "outlier"
    assert "range" in (reason or "")


def test_values_agree():
    assert validation.values_agree(100.0, 101.0)
    assert not validation.values_agree(100.0, 110.0)


def test_premium_suspect():
    # xau=3300, usd=100000 -> theoretical 18k ~ 7.957m IRT
    assert validation.premium_suspect(8_100_000.0, 3300.0, 100_000.0) is None
    reason = validation.premium_suspect(18_295_400.0, 3300.0, 100_000.0)
    assert reason is not None and "premium" in reason
    reason_low = validation.premium_suspect(4_000_000.0, 3300.0, 100_000.0)
    assert reason_low is not None
