"""Bounded retention: correctness of the protective floors (Addendum 17)."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.core.retention import (MIN_AUDIT_RETENTION_DAYS,
                                protected_prediction_cutoff, run_retention)
from app.db import model_versions, predictions, utcnow


def _pred(engine, days_ago: float, matured: bool = True, symbol="IR_GOLD_18K",
          horizon="1d"):
    at = utcnow() - timedelta(days=days_ago)
    with engine.begin() as conn:
        conn.execute(predictions.insert().values(
            symbol=symbol, horizon=horizon, model_name="naive",
            predicted_at=at, target_time=at + timedelta(hours=24),
            point_forecast=100.0, lower_bound=95.0, upper_bound=105.0,
            expected_change_pct=1.0, direction="up", confidence=0.5,
            drivers=[], warnings=[],
            actual_value=101.0 if matured else None,
            actual_recorded_at=at + timedelta(hours=25) if matured else None,
        ))


def _count(engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(select(func.count()).select_from(predictions)).scalar())


def test_dry_run_deletes_nothing(engine, settings):
    for d in (900, 800, 700):
        _pred(engine, d, matured=False)
    before = _count(engine)
    out = run_retention(engine, settings, dry_run=True)
    assert out["dry_run"] is True
    assert _count(engine) == before


def test_calibration_window_is_never_deleted(engine, settings):
    """Rows the meta-gate / calibration / ensemble loops still read must
    survive even when the configured retention would remove them."""
    for d in (5000, 4000, 3000):
        _pred(engine, d)
    settings.prediction_retention_days = 1
    floor, reason = protected_prediction_cutoff(engine)
    assert floor is not None
    assert "meta-gate" in reason
    run_retention(engine, settings, dry_run=False)
    assert _count(engine) == 3, "calibration window was deleted"


def test_unmatured_row_outside_every_window_can_expire(engine, settings):
    _pred(engine, 5000, matured=False)
    for d in (1, 2, 3):
        _pred(engine, d)
    settings.prediction_retention_days = 1
    run_retention(engine, settings, dry_run=False)
    assert _count(engine) == 3


def test_active_model_versions_are_preserved(engine, settings):
    old = utcnow() - timedelta(days=9999)
    with engine.begin() as conn:
        conn.execute(model_versions.insert().values(
            symbol="IR_GOLD_18K", horizon="1d", model_name="naive", version="v-active",
            trained_at=old, metrics={}, baseline_metrics={}, params={},
            artifact_path="/models/active.joblib", is_active=True))
        conn.execute(model_versions.insert().values(
            symbol="IR_GOLD_18K", horizon="1d", model_name="rf", version="v-old",
            trained_at=old, metrics={}, baseline_metrics={}, params={},
            artifact_path="/models/old.joblib", is_active=False))
    settings.model_version_retention_days = 1
    run_retention(engine, settings, dry_run=False)
    with engine.connect() as conn:
        versions = {r[0] for r in conn.execute(select(model_versions.c.version))}
    assert "v-active" in versions, "an ACTIVE model version was deleted"
    assert "v-old" not in versions


def test_audit_retention_has_a_hard_floor(engine, settings):
    settings.audit_retention_days = 1
    out = run_retention(engine, settings, dry_run=True)
    audit = [d for d in out["details"] if d["table"] == "audit_logs"][0]
    assert str(MIN_AUDIT_RETENTION_DAYS) in audit["floor"]


def test_retention_is_idempotent(engine, settings):
    for d in (900, 800):
        _pred(engine, d, matured=False)
    _pred(engine, 1)
    settings.prediction_retention_days = 1
    run_retention(engine, settings, dry_run=False)
    second = run_retention(engine, settings, dry_run=False)["total_deleted"]
    assert second == 0, f"second pass deleted {second} rows; not idempotent"
