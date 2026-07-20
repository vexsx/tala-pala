"""Live-calibration loop: blend/clamp/widening math, evaluate-job persistence
to app_settings, and adaptive (live-sMAPE) ensemble weights."""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.db import app_settings, predictions, prices, utcnow
from app.jobs.evaluate import (
    LIVE_CAL_KEY,
    compute_live_calibration,
    run_evaluate,
    upsert_setting,
)
from app.models.ensemble import inverse_smape_weights, live_member_smapes
from app.models.predicting import (
    blended_confidence,
    coverage_widening,
    load_live_calibration,
)


def _insert_prediction(
    engine,
    horizon: str = "1d",
    model_name: str = "naive",
    point: float = 105.0,
    lower: float = 100.0,
    upper: float = 110.0,
    expected_pct: float = 5.0,  # base recovers to 100.0
    actual: float | None = None,
    target_offset_hours: float = -24.0,
) -> None:
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            predictions.insert().values(
                symbol="IR_GOLD_18K",
                horizon=horizon,
                model_name=model_name,
                predicted_at=now + timedelta(hours=target_offset_hours - 24),
                target_time=now + timedelta(hours=target_offset_hours),
                point_forecast=point,
                lower_bound=lower,
                upper_bound=upper,
                expected_change_pct=expected_pct,
                direction="up",
                confidence=0.5,
                drivers=[],
                warnings=[],
                actual_value=actual,
                actual_recorded_at=now if actual is not None else None,
            )
        )


# --- confidence blend --------------------------------------------------------


def test_blended_confidence_no_live_evidence_is_identity():
    assert blended_confidence(0.6, None) == pytest.approx(0.6)
    assert blended_confidence(0.6, {}) == pytest.approx(0.6)
    assert blended_confidence(0.6, {"n": 0, "dir_hit_rate": 0.9}) == pytest.approx(0.6)
    assert blended_confidence(0.6, {"n": 30, "dir_hit_rate": None}) == pytest.approx(0.6)


def test_blended_confidence_shrinks_toward_live_hit_rate():
    # n=30 -> w = max(0.3, 1 - 30/60) = 0.5
    assert blended_confidence(0.6, {"n": 30, "dir_hit_rate": 0.8}) == pytest.approx(0.7)
    # n=60 -> w hits the 0.3 floor: 0.3*0.6 + 0.7*0.9
    assert blended_confidence(0.6, {"n": 60, "dir_hit_rate": 0.9}) == pytest.approx(0.81)
    # the floor keeps validation's vote no matter how much evidence
    assert blended_confidence(0.6, {"n": 600, "dir_hit_rate": 0.9}) == pytest.approx(0.81)


def test_blended_confidence_clamps_to_5_95():
    assert blended_confidence(0.9, {"n": 60, "dir_hit_rate": 1.0}) == pytest.approx(0.95)
    assert blended_confidence(0.1, {"n": 60, "dir_hit_rate": 0.0}) == pytest.approx(0.05)
    assert blended_confidence(1.5, None) == pytest.approx(0.95)


# --- interval widening -------------------------------------------------------


def test_coverage_widening_needs_evidence_and_floor_breach():
    assert coverage_widening(None) == pytest.approx(1.0)
    assert coverage_widening({}) == pytest.approx(1.0)
    # too few matured predictions: never widen
    assert coverage_widening({"n": 10, "coverage": 0.4}) == pytest.approx(1.0)
    # coverage at/above the 0.75 floor: no widening
    assert coverage_widening({"n": 40, "coverage": 0.75}) == pytest.approx(1.0)
    assert coverage_widening({"n": 40, "coverage": 0.9}) == pytest.approx(1.0)


def test_coverage_widening_factor_and_cap():
    # 0.9 / 0.72 = 1.25
    assert coverage_widening({"n": 20, "coverage": 0.72}) == pytest.approx(1.25)
    # 0.9 / 0.5 = 1.8 -> capped at 1.5
    assert coverage_widening({"n": 20, "coverage": 0.5}) == pytest.approx(1.5)
    # degenerate zero coverage -> cap
    assert coverage_widening({"n": 20, "coverage": 0.0}) == pytest.approx(1.5)


# --- evaluate job: rolling stats + app_settings persistence -----------------


def test_evaluate_persists_live_calibration(engine, settings):
    # 7 hits inside the interval, 2 misses outside, 1 miss inside:
    # dir_hit_rate = 0.7, coverage = 0.8
    for _ in range(7):
        _insert_prediction(engine, actual=110.0)  # up & covered
    for _ in range(2):
        _insert_prediction(engine, actual=95.0)  # down & below lower=100
    _insert_prediction(engine, lower=98.0, actual=99.0)  # down but covered

    # one matured-but-unfilled prediction plus a matching price -> fill pass
    _insert_prediction(engine, horizon="1h", target_offset_hours=-1.0)
    with engine.begin() as conn:
        conn.execute(
            prices.insert().values(
                symbol="IR_GOLD_18K",
                value=106.0,
                currency="IRT",
                unit="gram",
                source="test",
                observed_at=utcnow() - timedelta(hours=1),
                quality="ok",
            )
        )

    summary = run_evaluate(engine, settings)
    assert summary["evaluated"] == 1
    cal = summary["live_calibration"]
    assert cal["1d"]["n"] == 10
    assert cal["1d"]["dir_hit_rate"] == pytest.approx(0.7)
    assert cal["1d"]["coverage"] == pytest.approx(0.8)
    # the freshly filled 1h prediction is part of the same calibration pass
    assert cal["1h"]["n"] == 1
    assert cal["1h"]["dir_hit_rate"] == pytest.approx(1.0)
    assert "updated_at" in cal["1d"]

    # persisted to app_settings and readable by the prediction pass
    assert load_live_calibration(engine)["1d"]["coverage"] == pytest.approx(0.8)

    # running again upserts (single row, no duplicates)
    run_evaluate(engine, settings)
    with engine.connect() as conn:
        count = conn.execute(
            select(func.count()).select_from(app_settings).where(
                app_settings.c.key == LIVE_CAL_KEY
            )
        ).scalar_one()
    assert count == 1


def test_compute_live_calibration_uses_most_recent_window(engine):
    # 5 old misses, then 5 recent hits; window=5 must only see the hits
    for i in range(5):
        _insert_prediction(engine, actual=95.0, target_offset_hours=-240.0 + i)
    for i in range(5):
        _insert_prediction(engine, actual=110.0, target_offset_hours=-24.0 + i)
    cal = compute_live_calibration(engine, window=5)
    assert cal["1d"]["n"] == 5
    assert cal["1d"]["dir_hit_rate"] == pytest.approx(1.0)


def test_upsert_setting_is_idempotent_update(engine):
    upsert_setting(engine, "live_calibration", {"1d": {"n": 1}})
    upsert_setting(engine, "live_calibration", {"1d": {"n": 2}})
    with engine.connect() as conn:
        rows = conn.execute(
            select(app_settings.c.value).where(app_settings.c.key == "live_calibration")
        ).all()
    assert len(rows) == 1
    assert rows[0][0]["1d"]["n"] == 2


def test_load_live_calibration_empty_is_fail_open(engine):
    assert load_live_calibration(engine) == {}


# --- adaptive ensemble weights ----------------------------------------------


def test_live_member_smapes_and_inverse_weights(engine):
    # 'sma' predicts perfectly, 'ses' is ~10% off -> live weights favor sma
    for _ in range(25):
        _insert_prediction(engine, model_name="sma", point=100.0, actual=100.0)
        _insert_prediction(engine, model_name="ses", point=100.0, actual=110.0)
    smapes = live_member_smapes(engine, "1d", ["sma", "ses"])
    assert smapes is not None
    assert smapes["sma"] == pytest.approx(0.0)
    assert smapes["ses"] == pytest.approx(2.0 * 10.0 / 210.0 * 100.0)
    weights = inverse_smape_weights(smapes)
    assert weights["sma"] > 0.99
    assert weights["sma"] + weights["ses"] == pytest.approx(1.0)


def test_live_member_smapes_requires_all_members_matured(engine):
    for _ in range(25):
        _insert_prediction(engine, model_name="sma", point=100.0, actual=100.0)
    # 'ses' has no matured predictions -> keep validation weights
    assert live_member_smapes(engine, "1d", ["sma", "ses"]) is None
    # below the per-member minimum -> also None
    for _ in range(10):
        _insert_prediction(engine, model_name="ses", point=100.0, actual=110.0)
    assert live_member_smapes(engine, "1d", ["sma", "ses"]) is None
    # different horizon has no rows at all
    assert live_member_smapes(engine, "7d", ["sma"]) is None
