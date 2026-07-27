"""Orchestration tests for the collection pass (``app/jobs/news_collect.py``).

Every collector is monkeypatched here, so nothing in this file says anything
about whether a feed is up or parses correctly — that is what
``tests/test_news_collectors.py`` covers.  What is tested is the part that only
the orchestrator can get wrong, and that is expensive to discover in
production:

* a flag believed to be off while the job still fetches (the flag is read off
  ``Settings``, so a test can prove the gate rather than trust it);
* one raising collector taking the pass down with it, which would mean a single
  broken feed silently stops the other two from ever collecting;
* an unapproved source being reported as a failure — that would burn the
  circuit breaker on a source nobody approved, and make "not permitted" look
  like "broken";
* ``dry_run`` not reaching the collectors, which is the difference between
  verifying a source and accumulating rows from it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import metadata
from app.jobs.news_collect import COLLECTORS, run_news_collection
from app.news import news_collection_attempts, news_source_policies, news_sources, registry
from app.news.sources import fed, gdelt, ofac


@pytest.fixture()
def news_engine(engine):
    """Engine with the news tables present, regardless of import order."""
    metadata.create_all(engine)
    return engine


@pytest.fixture()
def collecting_settings(settings):
    """Settings with collection switched on; it is off in every other context."""
    settings.news_collection_enabled = True
    return settings


class CollectorSpy:
    """Stand-in for a collector's ``collect()``; records how it was called."""

    def __init__(self, source_code, *, items_new=0, status="ok", raises=None):
        self.source_code = source_code
        self.items_new = items_new
        self.status = status
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, engine, settings, *, dry_run=False):
        self.calls.append({"dry_run": dry_run})
        if self.raises is not None:
            raise self.raises
        return {
            "source": self.source_code,
            "parser_version": "spy",
            "status": self.status,
            "reason": "",
            "dry_run": dry_run,
            "http_status": 200,
            "raw_payload_id": None,
            "items_seen": self.items_new,
            "items_new": self.items_new,
            "items_duplicate": 0,
        }

    @property
    def called(self) -> bool:
        return bool(self.calls)


def seed_source(engine, code, *, enabled=True, approval_state="approved", min_interval=0):
    """One operational row plus its policy row — both are required to poll."""
    with engine.begin() as conn:
        conn.execute(
            news_sources.insert().values(
                code=code,
                name=f"{code} (test)",
                feed_url="https://example.invalid/feed",
                homepage_url="https://example.invalid",
                kind="rss",
                jurisdiction="us",
                language="en",
                enabled=enabled,
                policy_status="approved" if approval_state == "approved" else "exploratory",
                policy_note="test fixture",
                min_interval_seconds=min_interval,
            )
        )
        conn.execute(
            news_source_policies.insert().values(
                source_code=code,
                access_method="rss",
                auth_type="none",
                approval_state=approval_state,
                user_agent_policy="honest",
                backfill_allowed=False,
                store_full_body=False,
                attribution_required=True,
                min_interval_seconds=min_interval,
                policy_note="test fixture",
                reviewed_by="test",
            )
        )


def patch_collectors(monkeypatch, **overrides) -> dict[str, CollectorSpy]:
    """Replace every collector's ``collect`` with a spy; returns them by code."""
    spies: dict[str, CollectorSpy] = {}
    for module in (fed, ofac, gdelt):
        code = module.SOURCE_CODE
        spy = overrides.get(code) or CollectorSpy(code)
        monkeypatch.setattr(module, "collect", spy)
        spies[code] = spy
    return spies


def approve_all(engine, **kwargs) -> None:
    for module in (fed, ofac, gdelt):
        seed_source(engine, module.SOURCE_CODE, **kwargs)


# --- the flag ----------------------------------------------------------------


def test_collectors_registered_for_all_three_sources():
    """The pass covers fed, OFAC and GDELT and nothing else."""
    assert [module.SOURCE_CODE for module in COLLECTORS] == [
        fed.SOURCE_CODE,
        ofac.SOURCE_CODE,
        gdelt.SOURCE_CODE,
    ]


def test_disabled_flag_runs_no_collector(news_engine, settings, monkeypatch):
    """The flag is the gate: approved sources are irrelevant while it is off."""
    assert settings.news_collection_enabled is False  # the default, asserted
    approve_all(news_engine)
    spies = patch_collectors(monkeypatch)

    result = run_news_collection(news_engine, settings)

    assert result["enabled"] is False
    assert result["reason"] == "NEWS_COLLECTION_ENABLED is off"
    assert result["sources"] == {}
    assert result["articles_inserted"] == 0
    assert not any(spy.called for spy in spies.values())


def test_disabled_flag_writes_no_attempt_row(news_engine, settings, monkeypatch):
    """An off pass leaves no trace at all, not even bookkeeping."""
    approve_all(news_engine)
    patch_collectors(monkeypatch)

    run_news_collection(news_engine, settings)

    with news_engine.connect() as conn:
        assert conn.execute(select(news_collection_attempts)).first() is None


# --- failure isolation -------------------------------------------------------


def test_one_raising_collector_does_not_stop_the_others(
    news_engine, collecting_settings, monkeypatch
):
    """The whole point of the pass: a crash is contained to its own source."""
    approve_all(news_engine)
    boom = CollectorSpy(ofac.SOURCE_CODE, raises=RuntimeError("snapshot parse blew up"))
    spies = patch_collectors(monkeypatch, **{ofac.SOURCE_CODE: boom})
    spies[fed.SOURCE_CODE].items_new = 3
    spies[gdelt.SOURCE_CODE].items_new = 2

    result = run_news_collection(news_engine, collecting_settings)

    assert result["enabled"] is True
    assert result["sources_failed"] == 1
    assert result["sources_polled"] == 2
    # The other two ran, and their counts survived the failure of the third.
    assert spies[fed.SOURCE_CODE].called and spies[gdelt.SOURCE_CODE].called
    assert set(result["sources"]) == {
        fed.SOURCE_CODE,
        ofac.SOURCE_CODE,
        gdelt.SOURCE_CODE,
    }
    assert result["articles_inserted"] == 5
    assert result["sources"][ofac.SOURCE_CODE]["status"] == "error"
    assert any(entry.startswith(f"{ofac.SOURCE_CODE}:") for entry in result["errors"])


def test_raising_collector_is_recorded_against_its_registry_row(
    news_engine, collecting_settings, monkeypatch
):
    """A crash the collector never got to report must still reach the breaker.

    Without this the circuit breaker never sees the failure and the job
    re-invokes a collector that throws on every single tick.
    """
    approve_all(news_engine)
    boom = CollectorSpy(fed.SOURCE_CODE, raises=ValueError("unparseable feed"))
    patch_collectors(monkeypatch, **{fed.SOURCE_CODE: boom})

    run_news_collection(news_engine, collecting_settings)

    with news_engine.connect() as conn:
        attempt = conn.execute(
            select(news_collection_attempts).where(
                news_collection_attempts.c.source_code == fed.SOURCE_CODE
            )
        ).first()
        failures = conn.execute(
            select(news_sources.c.consecutive_failures).where(
                news_sources.c.code == fed.SOURCE_CODE
            )
        ).scalar()
    assert attempt is not None
    assert attempt._mapping["outcome"] == "error"
    assert attempt._mapping["error_class"] == "ValueError"
    assert failures == 1


def test_collector_reporting_error_status_counts_as_failed(
    news_engine, collecting_settings, monkeypatch
):
    """A collector that handled its own failure is still a failed source."""
    approve_all(news_engine)
    failed = CollectorSpy(gdelt.SOURCE_CODE, status="error")
    patch_collectors(monkeypatch, **{gdelt.SOURCE_CODE: failed})

    result = run_news_collection(news_engine, collecting_settings)

    assert result["sources_failed"] == 1
    assert result["sources_polled"] == 2


def test_empty_outcome_counts_as_polled(news_engine, collecting_settings, monkeypatch):
    """A feed with nothing new was reached; that is not a failure."""
    approve_all(news_engine)
    quiet = CollectorSpy(fed.SOURCE_CODE, status="empty")
    patch_collectors(monkeypatch, **{fed.SOURCE_CODE: quiet})

    result = run_news_collection(news_engine, collecting_settings)

    assert result["sources_polled"] == 3
    assert result["sources_failed"] == 0


# --- permission and cadence --------------------------------------------------


def test_unapproved_source_is_skipped_not_failed(
    news_engine, collecting_settings, monkeypatch
):
    """Policy says no: skipped with a reason, and its collector never runs."""
    seed_source(news_engine, fed.SOURCE_CODE)
    seed_source(news_engine, ofac.SOURCE_CODE, approval_state="policy_review_required")
    seed_source(news_engine, gdelt.SOURCE_CODE, enabled=False)
    spies = patch_collectors(monkeypatch)

    result = run_news_collection(news_engine, collecting_settings)

    assert result["sources_failed"] == 0
    assert result["sources_polled"] == 1
    assert result["sources_skipped"] == 2
    assert result["errors"] == []
    for code in (ofac.SOURCE_CODE, gdelt.SOURCE_CODE):
        assert result["sources"][code]["status"] == "skipped"
        assert result["sources"][code]["reason"]
        assert not spies[code].called
    assert spies[fed.SOURCE_CODE].called


def test_unregistered_source_is_skipped_not_failed(
    news_engine, collecting_settings, monkeypatch
):
    """No registry row at all is the same answer as no approval: skipped."""
    spies = patch_collectors(monkeypatch)

    result = run_news_collection(news_engine, collecting_settings)

    assert result["sources_skipped"] == 3
    assert result["sources_failed"] == 0
    assert result["errors"] == []
    assert not any(spy.called for spy in spies.values())


def refuse_gate(*codes):
    """A ``registry.should_attempt`` that refuses the named sources.

    Stubbed rather than driven by ``last_polled_at`` / ``consecutive_failures``
    columns: the cadence and the breaker are the registry's own logic (and its
    own tests), while what matters here is that the orchestrator obeys the
    verdict — for either reason — before invoking a collector.
    """
    refused = set(codes)

    def should_attempt(source, now=None):
        if source.code in refused:
            return False, "polled 60s ago; min interval 900s"
        return True, ""

    return should_attempt


def test_gate_refusal_is_skipped_not_failed(
    news_engine, collecting_settings, monkeypatch
):
    """Cadence/breaker refusal is a skip carrying the registry's own reason."""
    approve_all(news_engine)
    monkeypatch.setattr(registry, "should_attempt", refuse_gate(fed.SOURCE_CODE))
    spies = patch_collectors(monkeypatch)

    result = run_news_collection(news_engine, collecting_settings)

    assert not spies[fed.SOURCE_CODE].called
    assert result["sources"][fed.SOURCE_CODE]["status"] == "skipped"
    assert result["sources"][fed.SOURCE_CODE]["reason"] == "polled 60s ago; min interval 900s"
    assert result["sources_failed"] == 0
    # The refusal is per source: the other two still ran on this tick.
    assert result["sources_polled"] == 2


def test_gate_refusal_does_not_move_the_poll_marker(
    news_engine, collecting_settings, monkeypatch
):
    """A skip must not stamp ``last_polled_at`` or record an attempt row.

    If it did, a job scheduled more often than the courtesy interval would push
    the due time forward on every tick and the source it was being polite to
    would never be polled at all.
    """
    approve_all(news_engine)
    monkeypatch.setattr(registry, "should_attempt", refuse_gate(fed.SOURCE_CODE))
    patch_collectors(monkeypatch)

    run_news_collection(news_engine, collecting_settings)

    with news_engine.connect() as conn:
        marker = conn.execute(
            select(news_sources.c.last_polled_at).where(
                news_sources.c.code == fed.SOURCE_CODE
            )
        ).scalar()
        attempts = conn.execute(
            select(news_collection_attempts).where(
                news_collection_attempts.c.source_code == fed.SOURCE_CODE
            )
        ).all()
    assert marker is None
    assert attempts == []


def test_registry_read_failure_is_contained(
    news_engine, collecting_settings, monkeypatch
):
    """An unreadable registry means nothing is approved, not an exception.

    Fails closed and returns counts: a scheduler tick reports the problem
    instead of raising it through the endpoint, and no source is polled without
    a readable approval.
    """
    approve_all(news_engine)

    def boom(engine):
        raise RuntimeError("news_source_policies is unreadable")

    monkeypatch.setattr(registry, "approved_sources", boom)
    spies = patch_collectors(monkeypatch)

    result = run_news_collection(news_engine, collecting_settings)

    assert result["enabled"] is True
    assert result["sources_polled"] == 0
    assert result["sources_skipped"] == 3
    assert any(entry.startswith("registry:") for entry in result["errors"])
    assert not any(spy.called for spy in spies.values())


# --- narrowing and dry runs --------------------------------------------------


def test_sources_filter_runs_only_the_named_collector(
    news_engine, collecting_settings, monkeypatch
):
    approve_all(news_engine)
    spies = patch_collectors(monkeypatch)

    result = run_news_collection(
        news_engine, collecting_settings, sources=[fed.SOURCE_CODE]
    )

    assert set(result["sources"]) == {fed.SOURCE_CODE}
    assert spies[fed.SOURCE_CODE].called
    assert not spies[ofac.SOURCE_CODE].called
    assert not spies[gdelt.SOURCE_CODE].called


def test_unknown_requested_source_is_reported(
    news_engine, collecting_settings, monkeypatch
):
    """An operator typo must not look like a successful pass."""
    approve_all(news_engine)
    patch_collectors(monkeypatch)

    result = run_news_collection(
        news_engine, collecting_settings, sources=["not_a_source"]
    )

    assert result["sources"]["not_a_source"]["status"] == "skipped"
    assert result["sources_skipped"] == 1
    assert result["sources_polled"] == 0


def test_dry_run_is_passed_through_to_every_collector(
    news_engine, collecting_settings, monkeypatch
):
    approve_all(news_engine)
    spies = patch_collectors(monkeypatch)

    result = run_news_collection(news_engine, collecting_settings, dry_run=True)

    assert result["dry_run"] is True
    for spy in spies.values():
        assert spy.calls == [{"dry_run": True}]


def test_dry_run_default_is_false(news_engine, collecting_settings, monkeypatch):
    approve_all(news_engine)
    spies = patch_collectors(monkeypatch)

    result = run_news_collection(news_engine, collecting_settings)

    assert result["dry_run"] is False
    for spy in spies.values():
        assert spy.calls == [{"dry_run": False}]


def test_dry_run_crash_records_nothing(news_engine, collecting_settings, monkeypatch):
    """A dry run leaves no trace beyond the request it already made."""
    approve_all(news_engine)
    boom = CollectorSpy(fed.SOURCE_CODE, raises=RuntimeError("parse failed"))
    patch_collectors(monkeypatch, **{fed.SOURCE_CODE: boom})

    result = run_news_collection(news_engine, collecting_settings, dry_run=True)

    assert result["sources_failed"] == 1
    with news_engine.connect() as conn:
        assert conn.execute(select(news_collection_attempts)).first() is None


# --- endpoint ----------------------------------------------------------------


def test_endpoint_requires_the_internal_token(client):
    response = client.post("/internal/news/collect", headers={"X-Internal-Token": "wrong"})
    assert response.status_code == 401


def test_endpoint_reports_the_flag_is_off(client):
    """The endpoint exists whether or not collection is enabled, and says so."""
    from .conftest import TEST_TOKEN

    response = client.post(
        "/internal/news/collect", headers={"X-Internal-Token": TEST_TOKEN}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["reason"] == "NEWS_COLLECTION_ENABLED is off"


def test_endpoint_passes_body_through(client, engine, settings, monkeypatch):
    from .conftest import TEST_TOKEN

    metadata.create_all(engine)
    settings.news_collection_enabled = True
    approve_all(engine)
    spies = patch_collectors(monkeypatch)

    response = client.post(
        "/internal/news/collect",
        headers={"X-Internal-Token": TEST_TOKEN},
        json={"sources": [fed.SOURCE_CODE], "dry_run": True},
    )

    assert response.status_code == 200
    assert set(response.json()["sources"]) == {fed.SOURCE_CODE}
    assert spies[fed.SOURCE_CODE].calls == [{"dry_run": True}]
