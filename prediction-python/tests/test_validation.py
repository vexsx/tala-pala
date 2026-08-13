"""Outlier detection, jump rule, premium cross-check and dedupe keys."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

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


# --- corroboration by repetition (single-source escape hatch) ----------------


def _history(*rows):
    """(minutes ago, value, quality) -> the newest-first shape the walk takes."""
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    return now, [
        (now - timedelta(minutes=minutes), value, quality)
        for minutes, value, quality in rows
    ]


def test_suspect_streak_counts_a_consistent_run():
    now, history = _history(
        (30, 4.681, "suspect"), (60, 4.690, "suspect"), (95, 4.675, "suspect"),
    )
    streak = validation.suspect_streak(4.682, now, history)
    assert streak.length == 4  # three stored + the candidate
    assert streak.span_minutes == pytest.approx(95.0)
    assert streak.since == now - timedelta(minutes=95)


def test_suspect_streak_stops_at_an_accepted_value():
    """A good value in between means the series was never stuck, so the older
    suspects cannot be counted as part of the current run."""
    now, history = _history(
        (30, 4.681, "suspect"), (60, 4.690, "ok"), (95, 4.675, "suspect"),
    )
    assert validation.suspect_streak(4.682, now, history).length == 2


def test_suspect_streak_stops_at_a_disagreeing_value():
    """A source flapping between levels never accumulates a run: each quote
    only corroborates the ones it actually agrees with."""
    now, history = _history(
        (30, 4.681, "suspect"), (60, 0.470, "suspect"), (95, 4.675, "suspect"),
    )
    assert validation.suspect_streak(4.682, now, history).length == 2


def test_suspect_streak_of_a_one_off_spike_is_one():
    now, history = _history((30, 0.4697, "ok"), (60, 0.4701, "ok"))
    streak = validation.suspect_streak(9.9, now, history)
    assert streak.length == 1 and streak.span_minutes == 0.0


def test_suspect_streak_ignores_re_observations_of_the_same_instant():
    """Several rows carrying one source timestamp are one observation; the
    span must not be inflated by re-reading the same quote."""
    now, history = _history(
        (30, 4.681, "suspect"), (30, 4.683, "suspect"), (90, 4.675, "suspect"),
    )
    streak = validation.suspect_streak(4.682, now, history)
    assert streak.length == 3
    assert streak.span_minutes == pytest.approx(90.0)


def test_sustained_by_repetition_needs_both_count_and_span():
    since = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    enough = validation.SuspectStreak(
        validation.SUSTAIN_MIN_OBSERVATIONS, validation.SUSTAIN_MIN_SPAN_MINUTES, since
    )
    assert validation.sustained_by_repetition(enough)
    # a fast ticker: five quotes inside one minute is one instant, not a level
    burst = validation.SuspectStreak(validation.SUSTAIN_MIN_OBSERVATIONS, 1.0, since)
    assert not validation.sustained_by_repetition(burst)
    # two points ninety minutes apart is a line, not a sustained level
    sparse = validation.SuspectStreak(2, validation.SUSTAIN_MIN_SPAN_MINUTES, since)
    assert not validation.sustained_by_repetition(sparse)


def test_alert_threshold_precedes_automatic_acceptance():
    """The operator must hear about a held series before it re-levels itself,
    otherwise the warning only ever narrates a decision already taken."""
    assert validation.SUSPECT_ALERT_AFTER < validation.SUSTAIN_MIN_OBSERVATIONS


def test_premium_suspect():
    # xau=3300, usd=100000 -> theoretical 18k ~ 7.957m IRT
    assert validation.premium_suspect(8_100_000.0, 3300.0, 100_000.0) is None
    reason = validation.premium_suspect(18_295_400.0, 3300.0, 100_000.0)
    assert reason is not None and "premium" in reason
    reason_low = validation.premium_suspect(4_000_000.0, 3300.0, 100_000.0)
    assert reason_low is not None
