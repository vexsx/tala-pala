"""End-to-end pipeline smoke test on SQLite: seed prices -> features -> train
-> predict -> signals -> backtest -> evaluate.  Candidates are narrowed to the
cheap models to keep the suite fast; the full candidate set is exercised by
unit tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from sqlalchemy import insert, select

import app.models.training as training
from app.db import feature_snapshots, model_versions, predictions, prices, signals

from .conftest import TEST_TOKEN

AUTH = {"X-Internal-Token": TEST_TOKEN}
N_DAYS = 160


@pytest.fixture()
def seeded(engine):
    rng = np.random.default_rng(21)
    start = datetime.now(timezone.utc) - timedelta(days=N_DAYS)
    gold, usd, xau = 6_000_000.0, 90_000.0, 3_000.0
    rows = []
    for i in range(N_DAYS):
        ts = (start + timedelta(days=i)).replace(minute=0, second=0, microsecond=0)
        gold *= 1.0 + rng.normal(0.002, 0.008)
        usd *= 1.0 + rng.normal(0.001, 0.004)
        xau *= 1.0 + rng.normal(0.0005, 0.006)
        for symbol, value, currency, unit in (
            ("IR_GOLD_18K", gold, "IRT", "gram"),
            ("USD_IRT", usd, "IRT", "usd"),
            ("XAUUSD", xau, "USD", "ozt"),
        ):
            rows.append(
                dict(symbol=symbol, value=value, currency=currency, unit=unit,
                     source="seed", observed_at=ts, collected_at=ts, quality="ok")
            )
    with engine.begin() as conn:
        conn.execute(insert(prices), rows)
    return engine


def test_full_pipeline(client, seeded, engine, monkeypatch):
    monkeypatch.setattr(training, "CANDIDATES", ("naive", "sma", "ses"))

    # features
    resp = client.post("/internal/features/generate", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["generated"] == 1
    with engine.connect() as conn:
        assert len(conn.execute(select(feature_snapshots)).all()) == 1

    # train
    resp = client.post("/internal/train", json={"horizons": ["1d", "1h"]}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "succeeded"
    assert body["horizons"]["1d"]["enabled"] is True
    assert "1d" in body["selected"]
    # hourly horizon disabled: only daily data was seeded (<14 days hourly span)
    assert body["horizons"]["1h"]["enabled"] is False
    with engine.connect() as conn:
        active = conn.execute(
            select(model_versions).where(
                model_versions.c.symbol == "IR_GOLD_18K",
                model_versions.c.horizon == "1d",
                model_versions.c.is_active.is_(True),
            )
        ).all()
    assert len(active) == 1
    assert active[0]._mapping["artifact_path"]

    # predict
    resp = client.post("/internal/predict", json={"horizons": ["1d"]}, headers=AUTH)
    assert resp.status_code == 200
    preds = resp.json()["predictions"]
    # multi-symbol (Addendum 8): one prediction per forecast symbol
    assert {p["symbol"] for p in preds} == {"IR_GOLD_18K", "XAUUSD"}
    for pred in preds:
        assert pred["direction"] in ("up", "down", "flat")
        assert pred["lower_bound"] <= pred["point_forecast"] <= pred["upper_bound"]
        assert 0.0 <= pred["confidence"] <= 1.0
        assert pred["warnings"]  # always carries the uncertainty disclaimer
    with engine.connect() as conn:
        assert len(conn.execute(select(predictions)).all()) == len(preds)

    # signals
    resp = client.post("/internal/signals/generate", headers=AUTH)
    assert resp.status_code == 200
    sig = resp.json()
    assert sig["signal"] in ("strong_buy", "buy", "hold", "sell", "strong_sell")
    assert "not financial advice" in sig["explanation"]
    with engine.connect() as conn:
        assert len(conn.execute(select(signals)).all()) == 1

    # backtest
    resp = client.post(
        "/internal/backtest",
        json={"horizon": "1d", "fee_pct": 0.5, "spread_pct": 1.0,
              "slippage_pct": 0.1, "min_holding_days": 1},
        headers=AUTH,
    )
    assert resp.status_code == 200
    bt = resp.json()
    assert bt["status"] == "succeeded"
    assert "buy_and_hold" in bt["benchmarks"]

    # evaluate: no matured predictions yet (target_time in the future)
    resp = client.post("/internal/evaluate", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["evaluated"] == 0

    # cleanup runs
    resp = client.post("/internal/maintenance/cleanup", headers=AUTH)
    assert resp.status_code == 200
    assert "deleted_raw_observations" in resp.json()
