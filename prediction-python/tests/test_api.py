"""API tests: token auth, health, metrics, collect with mocked providers."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import insert, select

from app.db import data_providers, prices, raw_observations
from app.providers.base import Observation, Provider

from .conftest import TEST_TOKEN

AUTH = {"X-Internal-Token": TEST_TOKEN}


def _seed_provider(engine, code="tgju", category="iran_gold", priority=10):
    with engine.begin() as conn:
        conn.execute(
            insert(data_providers).values(
                code=code, name=code.title(), base_url="https://example.invalid",
                category=category, priority=priority, enabled=True,
                consecutive_failures=0,
            )
        )


class StubProvider(Provider):
    code = "tgju"
    category = "iran_gold"

    def __init__(self, observations):
        super().__init__(timeout=1.0, courtesy_delay=0.0, backoff_base=0.0)
        self._observations = observations

    def fetch(self):
        return list(self._observations)


def _gold_observation(value_toman=18_295_400.0):
    return Observation(
        provider_code="tgju",
        symbol="IR_GOLD_18K",
        raw_value=value_toman * 10,
        raw_unit="IRR/gram",
        raw_currency="IRR",
        value=value_toman,
        currency="IRT",
        unit="gram",
        observed_at=datetime(2026, 7, 20, 10, 45, 39, tzinfo=timezone.utc),
        raw_payload={"p": "182,954,000"},
    )


# --- auth -------------------------------------------------------------------


def test_health_needs_no_token(client):
    resp = client.get("/internal/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] is True
    assert body["version"]


def test_metrics_needs_no_token(client):
    resp = client.get("/internal/metrics")
    assert resp.status_code == 200
    assert "goldpred" in resp.text


def test_missing_token_is_401(client):
    for method, path in [
        ("get", "/internal/providers/health"),
        ("post", "/internal/collect"),
        ("post", "/internal/train"),
        ("post", "/internal/predict"),
        ("post", "/internal/signals/generate"),
        ("post", "/internal/evaluate"),
        ("post", "/internal/maintenance/cleanup"),
    ]:
        resp = getattr(client, method)(path)
        assert resp.status_code == 401, path
        assert resp.json()["error"]["code"] == "unauthorized"


def test_wrong_token_is_401(client):
    resp = client.get("/internal/providers/health",
                      headers={"X-Internal-Token": "wrong"})
    assert resp.status_code == 401


# --- providers health -------------------------------------------------------


def test_providers_health_shape(client, engine):
    _seed_provider(engine)
    resp = client.get("/internal/providers/health", headers=AUTH)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    for key in ("code", "name", "category", "enabled", "priority", "healthy",
                "last_success_at", "consecutive_failures", "last_error"):
        assert key in row, key
    assert row["code"] == "tgju"
    assert row["healthy"] is True


# --- collect ----------------------------------------------------------------


def test_collect_writes_prices_and_raw(client, engine, monkeypatch):
    _seed_provider(engine)
    stub = StubProvider([_gold_observation()])
    from app.providers import registry

    monkeypatch.setattr(registry, "build_provider", lambda code, settings: stub)

    resp = client.post("/internal/collect", json={"jobs": ["iran_gold"]}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["collected"].get("IR_GOLD_18K") == 1

    with engine.connect() as conn:
        price_rows = conn.execute(
            select(prices).where(prices.c.symbol == "IR_GOLD_18K")
        ).all()
        raw_rows = conn.execute(select(raw_observations)).all()
    assert len(price_rows) == 1
    stored = price_rows[0]._mapping
    assert float(stored["value"]) == 18_295_400.0
    assert stored["currency"] == "IRT"
    assert stored["source"] == "tgju"
    assert len(raw_rows) == 1
    assert float(raw_rows[0]._mapping["raw_value"]) == 182_954_000.0
    assert raw_rows[0]._mapping["currency"] == "IRR"

    # provider health recorded
    with engine.connect() as conn:
        provider = conn.execute(select(data_providers)).first()._mapping
    assert provider["last_success_at"] is not None
    assert provider["consecutive_failures"] == 0


def test_collect_is_idempotent(client, engine, monkeypatch):
    _seed_provider(engine)
    stub = StubProvider([_gold_observation()])
    from app.providers import registry

    monkeypatch.setattr(registry, "build_provider", lambda code, settings: stub)

    first = client.post("/internal/collect", json={"jobs": ["iran_gold"]}, headers=AUTH)
    second = client.post("/internal/collect", json={"jobs": ["iran_gold"]}, headers=AUTH)
    assert first.json()["collected"].get("IR_GOLD_18K") == 1
    # same observation again -> deduped, nothing new collected
    assert second.json()["collected"].get("IR_GOLD_18K") is None
    with engine.connect() as conn:
        count = len(conn.execute(select(prices)).all())
    assert count == 1


def test_collect_provider_failure_records_error(client, engine, monkeypatch):
    _seed_provider(engine)

    class FailingProvider(StubProvider):
        def fetch(self):
            from app.providers.base import ProviderError

            raise ProviderError("boom")

    from app.providers import registry

    monkeypatch.setattr(
        registry, "build_provider",
        lambda code, settings: FailingProvider([]),
    )
    resp = client.post("/internal/collect", json={"jobs": ["iran_gold"]}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["collected"] == {}
    assert any("boom" in e for e in body["errors"])
    with engine.connect() as conn:
        provider = conn.execute(select(data_providers)).first()._mapping
    assert provider["consecutive_failures"] == 1
    assert "boom" in provider["last_error"]


def test_collect_jump_needs_confirmation_by_second_source(client, engine, monkeypatch):
    """A >15% jump vs the last good value is held as suspect until a second
    provider confirms it; once confirmed, both observations are promoted."""
    _seed_provider(engine, code="tgju", priority=10)
    _seed_provider(engine, code="alanchand", priority=20)

    # last good value on record: 10m IRT
    with engine.begin() as conn:
        conn.execute(
            insert(prices).values(
                symbol="IR_GOLD_18K", value=10_000_000.0, currency="IRT",
                unit="gram", source="tgju",
                observed_at=datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
                collected_at=datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
                quality="ok",
            )
        )

    def jumped(provider_code, ts_minute):
        return Observation(
            provider_code=provider_code, symbol="IR_GOLD_18K",
            raw_value=120_500_000.0, raw_unit="IRR/gram", raw_currency="IRR",
            value=12_050_000.0, currency="IRT", unit="gram",  # +20.5% jump
            observed_at=datetime(2026, 7, 20, 10, ts_minute, tzinfo=timezone.utc),
            raw_payload=None,
        )

    stubs = {
        "tgju": StubProvider([jumped("tgju", 0)]),
        "alanchand": StubProvider([jumped("alanchand", 1)]),
    }
    from app.providers import registry

    monkeypatch.setattr(
        registry, "build_provider", lambda code, settings: stubs[code]
    )
    resp = client.post("/internal/collect", json={"jobs": ["iran_gold"]}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    # both the held suspect and the confirming observation were promoted
    assert body["collected"].get("IR_GOLD_18K") == 2
    assert any("suspect" in e for e in body["errors"])
    with engine.connect() as conn:
        good = conn.execute(
            select(prices).where(
                prices.c.symbol == "IR_GOLD_18K", prices.c.quality == "ok",
                prices.c.value > 11_000_000.0,
            )
        ).all()
    assert len(good) == 2
    assert {r._mapping["source"] for r in good} == {"tgju", "alanchand"}


def test_empty_body_collect_runs_all_jobs(client, engine, monkeypatch):
    _seed_provider(engine)
    stub = StubProvider([_gold_observation()])
    from app.providers import registry

    monkeypatch.setattr(registry, "build_provider", lambda code, settings: stub)
    resp = client.post("/internal/collect", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["collected"].get("IR_GOLD_18K") == 1


def _xau(provider_code, value, observed_at):
    return Observation(
        provider_code=provider_code,
        symbol="XAUUSD",
        raw_value=value,
        raw_unit="USD/ozt",
        raw_currency="USD",
        value=value,
        currency="USD",
        unit="ozt",
        observed_at=observed_at,
        raw_payload={},
    )


def test_stale_observation_does_not_block_fresher_fallback(client, engine, monkeypatch):
    """A provider whose ticker lags (e.g. TGJU 'ons', seen 40m behind on
    2026-07-20) is stored but must NOT satisfy the symbol: a fresher
    lower-priority source is still consulted afterwards.  Fixed to a Wednesday
    mid-session so the market-hours-aware gate sees an OPEN market."""
    from datetime import timedelta

    from app.jobs import collect as collect_mod
    from app.providers import registry

    fixed_now = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)  # Wed, all open
    monkeypatch.setattr(collect_mod, "utcnow", lambda: fixed_now)

    _seed_provider(engine, code="laggy", category="global_gold", priority=5)
    _seed_provider(engine, code="fresh", category="global_gold", priority=20)

    stubs = {
        "laggy": StubProvider([_xau("laggy", 4000.0, fixed_now - timedelta(hours=2))]),
        "fresh": StubProvider([_xau("fresh", 4014.5, fixed_now)]),
    }
    monkeypatch.setattr(
        registry, "build_provider", lambda code, settings: stubs[code]
    )

    resp = client.post("/internal/collect", json={"jobs": ["global"]}, headers=AUTH)
    assert resp.status_code == 200

    with engine.connect() as conn:
        sources = {
            r._mapping["source"]
            for r in conn.execute(select(prices).where(prices.c.symbol == "XAUUSD"))
        }
    # both stored; crucially the fresh fallback was consulted at all
    assert sources == {"laggy", "fresh"}


def test_market_closed_last_session_data_satisfies_collect(client, engine, monkeypatch):
    """Addendum 1: during the global weekend closure a Friday-session
    observation satisfies the symbol — no 'only stale values' error and no
    pointless fallback churn for closed markets."""
    from app.jobs import collect as collect_mod
    from app.providers import registry

    fixed_now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)  # Saturday
    monkeypatch.setattr(collect_mod, "utcnow", lambda: fixed_now)

    _seed_provider(engine, code="laggy", category="global_gold", priority=5)
    last_session = datetime(2026, 7, 17, 20, 50, tzinfo=timezone.utc)  # Fri, pre-close
    stub = StubProvider([_xau("laggy", 4000.0, last_session)])
    monkeypatch.setattr(registry, "build_provider", lambda code, settings: stub)

    resp = client.post("/internal/collect", json={"jobs": ["global"]}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["collected"].get("XAUUSD") == 1
    assert not any("XAUUSD" in e and "stale" in e for e in body["errors"])
