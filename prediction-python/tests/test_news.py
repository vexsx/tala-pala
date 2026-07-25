"""Tests for the news/event subsystem skeleton: feed parsing and the four
clocks (event / publication / ingestion / revision), dedupe primitives, the
hypothesis taxonomy, and the idempotent ingestion job.

No test asserts anything about news improving a forecast: there is no
historical archive, so no news feature reaches a model.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx

from app.db import ensure_utc, metadata
from app.jobs import news as news_job
from app.news import (
    ArticleRecord,
    NewsProvider,
    dedupe,
    news_article_versions,
    news_articles,
    news_sources,
    taxonomy,
)
from app.providers import fedpress
from app.providers.base import ProviderError

from .conftest import load_fixture_text

FOMC_TITLE = "Federal Reserve issues FOMC statement"
FOMC_URL = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260715a.htm"

PUBLISHED = datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)   # 14:00 EST
INGESTED = datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)
LATER = INGESTED + timedelta(hours=3)

EXPECTED_CATEGORIES = {
    "us_monetary_policy", "us_inflation", "us_labor", "yields", "dollar_strength",
    "global_risk_off", "geopolitical_escalation", "geopolitical_deescalation",
    "sanctions_escalation", "sanctions_relief", "iran_fx_policy",
    "iran_monetary_policy", "domestic_gold_regulation", "exchange_disruption",
    "energy_shock", "data_outage",
}


# --- fixtures/helpers ---------------------------------------------------------


@pytest.fixture()
def news_engine(engine):
    """Shared test engine with the news tables present.

    ``app.news`` registers its tables on ``app.db.metadata`` at import time;
    re-running ``create_all`` (checkfirst) makes that independent of whether
    this module happened to be imported before the ``engine`` fixture ran.
    """
    metadata.create_all(engine)
    return engine


@pytest.fixture()
def news_on(monkeypatch):
    monkeypatch.setenv(news_job.NEWS_ENABLED_ENV, "true")


class _StubProvider(NewsProvider):
    """News provider that replays canned records (or fails on demand)."""

    def __init__(self, code, records=None, error=None):
        super().__init__(timeout=1.0, courtesy_delay=0.0, backoff_base=0.0)
        self.code = code
        self._records = list(records or [])
        self._error = error

    def fetch_articles(self, ingested_at=None):
        if self._error is not None:
            raise ProviderError(self._error)
        return list(self._records)


def _use_providers(monkeypatch, providers: dict) -> None:
    monkeypatch.setattr(
        news_job, "build_news_provider", lambda code, settings: providers.get(code)
    )


def _seed_source(engine, code="fed_press", enabled=True, policy_status="approved",
                 min_interval=0, last_polled=None) -> None:
    with engine.begin() as conn:
        conn.execute(
            news_sources.insert().values(
                code=code,
                name=f"{code} (test)",
                feed_url="https://example.invalid/feed.xml",
                kind="rss",
                jurisdiction="us",
                language="en",
                enabled=enabled,
                policy_status=policy_status,
                policy_note="test fixture",
                min_interval_seconds=min_interval,
                last_polled_at=last_polled,
            )
        )


def _record(title=FOMC_TITLE, url=FOMC_URL, summary="Rates unchanged.",
            published=PUBLISHED, ingested=INGESTED, source="fed_press") -> ArticleRecord:
    return ArticleRecord(
        source_code=source,
        external_id=url,
        url=url,
        title=title,
        summary=summary,
        published_at=published,
        ingested_at=ingested,
    )


def _articles(engine) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(news_articles.select())]


def _versions(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            news_article_versions.select().order_by(news_article_versions.c.version)
        )
        return [dict(r._mapping) for r in rows]


# --- feed parsing and the publication/ingestion clocks ------------------------


def test_parse_feed_extracts_publication_time():
    records = fedpress.parse_feed(load_fixture_text("fed_press.xml"), INGESTED)
    assert len(records) == 4
    by_title = {r.title: r for r in records}

    fomc = by_title[FOMC_TITLE]
    assert fomc.published_at == PUBLISHED          # 14:00 EST -> 19:00 UTC
    assert fomc.published_at_estimated is False
    assert fomc.url == FOMC_URL
    assert fomc.language == "en"
    assert fomc.raw_payload["categories"] == ["Monetary Policy"]

    # numeric offset form parses identically
    speech = by_title["Speech by Governor Adams on the economic outlook and monetary policy"]
    assert speech.published_at == datetime(2026, 7, 14, 14, 30, tzinfo=timezone.utc)


def test_publication_time_is_distinct_from_ingestion_time():
    records = fedpress.parse_feed(load_fixture_text("fed_press.xml"), INGESTED)
    fomc = next(r for r in records if r.title == FOMC_TITLE)
    assert fomc.ingested_at == INGESTED
    assert fomc.published_at != fomc.ingested_at
    assert fomc.published_at < fomc.ingested_at


def test_missing_pubdate_falls_back_to_ingestion_time_and_is_flagged():
    records = fedpress.parse_feed(load_fixture_text("fed_press.xml"), INGESTED)
    dateless = next(
        r for r in records
        if r.title == "Federal Reserve Board announces termination of enforcement action"
    )
    # Conservative direction: never claims to be older than we can evidence.
    assert dateless.published_at == INGESTED
    assert dateless.published_at_estimated is True


def test_pubdate_without_timezone_is_flagged_estimated():
    aware, estimated = fedpress.parse_pub_date("Wed, 15 Jul 2026 14:00:00 EST")
    assert aware == PUBLISHED and estimated is False

    naive, estimated = fedpress.parse_pub_date("Wed, 15 Jul 2026 14:00:00")
    assert naive == datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc)
    assert estimated is True  # the true offset could move it by hours

    assert fedpress.parse_pub_date("not a date")[0] is None
    assert fedpress.parse_pub_date("")[0] is None
    assert fedpress.parse_iso_date("2026-07-15T14:00:00Z")[0] == datetime(
        2026, 7, 15, 14, 0, tzinfo=timezone.utc
    )
    assert fedpress.parse_iso_date("garbage")[0] is None


def test_malformed_feed_yields_zero_articles_without_raising():
    for broken in ("", "   ", "<rss><channel><item>", "not xml at all",
                   "<html><body>challenge</body></html>"):
        assert fedpress.parse_feed(broken, INGESTED) == []


@respx.mock
def test_provider_fetch_stamps_ingestion_time():
    respx.get(fedpress.FEED_URL).mock(
        return_value=httpx.Response(200, text=load_fixture_text("fed_press.xml"))
    )
    provider = fedpress.FedPressProvider(timeout=2.0, courtesy_delay=0.0, backoff_base=0.0)
    records = provider.fetch_articles(ingested_at=INGESTED)
    assert records
    assert all(r.ingested_at == INGESTED for r in records)
    assert all(r.source_code == "fed_press" for r in records)


@respx.mock
def test_provider_reports_unparseable_feed_as_source_failure():
    respx.get(fedpress.FEED_URL).mock(
        return_value=httpx.Response(200, text="<html><body>challenge</body></html>")
    )
    provider = fedpress.FedPressProvider(timeout=2.0, courtesy_delay=0.0, backoff_base=0.0)
    with pytest.raises(ProviderError):
        provider.fetch_articles(ingested_at=INGESTED)


def test_news_provider_is_not_a_price_provider():
    provider = fedpress.FedPressProvider(timeout=2.0, courtesy_delay=0.0, backoff_base=0.0)
    with pytest.raises(ProviderError):
        provider.fetch()


# --- dedupe primitives --------------------------------------------------------


def test_canonical_url_normalizes_host_tracking_and_fragment():
    messy = (
        "HTTPS://WWW.Federalreserve.gov:443/newsevents/pressreleases/"
        "monetary20260715a.htm?utm_source=rss&utm_medium=feed&fbclid=abc#content"
    )
    assert dedupe.canonical_url(messy) == (
        "https://federalreserve.gov/newsevents/pressreleases/monetary20260715a.htm"
    )
    # a trailing slash is not a different article
    assert dedupe.canonical_url("https://example.org/news/") == "https://example.org/news"
    assert dedupe.canonical_url("https://example.org/") == "https://example.org"


def test_canonical_url_sorts_query_and_drops_credentials():
    assert dedupe.canonical_url("https://example.org/a?b=2&a=1") == dedupe.canonical_url(
        "https://example.org/a?a=1&b=2"
    )
    assert dedupe.canonical_url("https://user:secret@example.org/a") == "https://example.org/a"
    assert dedupe.canonical_url("www.example.org/a") == "https://example.org/a"
    assert dedupe.canonical_url("   ") == ""


def test_content_hash_ignores_markup_and_case_but_not_wording():
    base = dedupe.content_hash(FOMC_TITLE, "Rates unchanged.")
    assert len(base) == 64
    assert base == dedupe.content_hash("FEDERAL  RESERVE <b>issues</b> FOMC statement",
                                       "Rates unchanged.")
    assert base != dedupe.content_hash(FOMC_TITLE, "Rates cut by 25 basis points.")


def test_near_duplicate_titles_detected():
    assert dedupe.is_near_duplicate(FOMC_TITLE, "Federal Reserve Board issues FOMC statement")
    assert not dedupe.is_near_duplicate(
        FOMC_TITLE, "Federal Reserve Board announces approval of application by Acme Bancorp"
    )
    # an empty side can never be a duplicate of anything
    assert dedupe.title_similarity(FOMC_TITLE, "") == 0.0
    assert dedupe.normalize_title("The Minutes of the Meeting") == "minutes meeting"


def test_find_near_duplicate_picks_best_candidate():
    candidates = [
        (1, "Federal Reserve Board announces personnel changes"),
        (2, FOMC_TITLE),
        (3, "Minutes of the Federal Open Market Committee"),
    ]
    assert dedupe.find_near_duplicate(
        "Federal Reserve Board issues FOMC statement", candidates
    ) == 2
    assert dedupe.find_near_duplicate("Beige Book published", candidates) is None


# --- taxonomy (hypotheses, not measurements) ---------------------------------


def test_taxonomy_categories_are_complete_and_well_formed():
    assert set(taxonomy.CATEGORY_CODES) == EXPECTED_CATEGORIES
    for code in taxonomy.CATEGORY_CODES:
        category = taxonomy.get(code)
        assert category.code == code
        assert category.label and category.prior_polarity
        # nothing here may ever claim to be a measured effect
        assert category.evidence == taxonomy.EVIDENCE_HYPOTHESIS
        channels = taxonomy.channels(code)
        assert set(channels) == set(taxonomy.CHANNEL_NAMES)
        for channel in channels.values():
            assert channel.direction in taxonomy.DIRECTIONS
            assert channel.prior_strength in taxonomy.PRIOR_STRENGTHS
            assert channel.rationale
            # "no mechanism" and "no prior weight" must agree
            assert (channel.direction == taxonomy.NONE) == (
                channel.prior_strength == taxonomy.NO_PRIOR
            )


def test_taxonomy_priors_flip_with_polarity():
    assert taxonomy.opposite(taxonomy.UP) == taxonomy.DOWN
    assert taxonomy.opposite(taxonomy.DOWN) == taxonomy.UP
    # a missing mechanism stays missing; an unknown sign stays unknown
    assert taxonomy.opposite(taxonomy.NONE) == taxonomy.NONE
    assert taxonomy.opposite(taxonomy.AMBIGUOUS) == taxonomy.AMBIGUOUS

    escalation = taxonomy.get("sanctions_escalation")
    relief = taxonomy.get("sanctions_relief")
    assert taxonomy.opposite(escalation.usd_irt.direction) == relief.usd_irt.direction
    assert (
        taxonomy.opposite(escalation.local_premium.direction)
        == relief.local_premium.direction
    )
    # an Iran-only event has no channel into global gold
    assert escalation.xauusd.direction == taxonomy.NONE
    # the operational marker is not a market event at all
    outage = taxonomy.get("data_outage")
    assert {c.direction for c in taxonomy.channels(outage.code).values()} == {taxonomy.NONE}
    assert taxonomy.is_known("us_inflation") and not taxonomy.is_known("nonsense")


# --- ingestion job ------------------------------------------------------------


def test_ingest_disabled_by_default(news_engine, settings, monkeypatch):
    monkeypatch.delenv(news_job.NEWS_ENABLED_ENV, raising=False)
    _seed_source(news_engine)
    _use_providers(monkeypatch, {"fed_press": _StubProvider("fed_press", [_record()])})

    out = news_job.run_news_ingest(news_engine, settings, now=INGESTED)

    assert out["enabled"] is False
    assert out["articles_inserted"] == 0
    assert _articles(news_engine) == []


def test_ingest_stores_publication_and_ingestion_times_separately(
    news_engine, settings, monkeypatch, news_on
):
    records = fedpress.parse_feed(load_fixture_text("fed_press.xml"), INGESTED)
    _seed_source(news_engine)
    _use_providers(monkeypatch, {"fed_press": _StubProvider("fed_press", records)})

    out = news_job.run_news_ingest(news_engine, settings, now=INGESTED)

    assert out["enabled"] is True
    assert out["sources_polled"] == 1
    assert out["articles_fetched"] == 4
    assert out["articles_inserted"] == 4

    stored = {row["title"]: row for row in _articles(news_engine)}
    fomc = stored[FOMC_TITLE]
    assert ensure_utc(fomc["published_at"]) == PUBLISHED
    assert ensure_utc(fomc["ingested_at"]) == INGESTED
    assert ensure_utc(fomc["published_at"]) != ensure_utc(fomc["ingested_at"])
    assert fomc["revised_at"] is None          # no edit observed yet
    assert bool(fomc["published_at_estimated"]) is False
    assert fomc["canonical_url"] == dedupe.canonical_url(FOMC_URL)

    dateless = stored["Federal Reserve Board announces termination of enforcement action"]
    assert bool(dateless["published_at_estimated"]) is True
    assert ensure_utc(dateless["published_at"]) == INGESTED


def test_ingest_is_idempotent(news_engine, settings, monkeypatch, news_on):
    records = fedpress.parse_feed(load_fixture_text("fed_press.xml"), INGESTED)
    _seed_source(news_engine)
    _use_providers(monkeypatch, {"fed_press": _StubProvider("fed_press", records)})

    first = news_job.run_news_ingest(news_engine, settings, now=INGESTED)
    second = news_job.run_news_ingest(news_engine, settings, now=LATER)

    assert first["articles_inserted"] == 4
    assert second["articles_inserted"] == 0
    assert second["versions_inserted"] == 0
    assert second["unchanged"] == 4
    assert len(_articles(news_engine)) == 4
    assert len(_versions(news_engine)) == 4


def test_edited_article_appends_version_without_overwriting_history(
    news_engine, settings, monkeypatch, news_on
):
    _seed_source(news_engine)
    original = _record(summary="Rates unchanged.")
    _use_providers(monkeypatch, {"fed_press": _StubProvider("fed_press", [original])})
    news_job.run_news_ingest(news_engine, settings, now=INGESTED)

    edited = _record(summary="Rates unchanged. Corrected: one member dissented.",
                     ingested=LATER)
    _use_providers(monkeypatch, {"fed_press": _StubProvider("fed_press", [edited])})
    out = news_job.run_news_ingest(news_engine, settings, now=LATER)

    assert out["versions_inserted"] == 1
    assert out["articles_inserted"] == 0
    assert len(_articles(news_engine)) == 1        # an edit is not a new article

    versions = _versions(news_engine)
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["summary"] == "Rates unchanged."   # history preserved
    assert versions[1]["summary"] == edited.summary
    assert versions[0]["content_hash"] != versions[1]["content_hash"]

    article = _articles(news_engine)[0]
    assert article["n_versions"] == 2
    assert article["summary"] == edited.summary
    assert ensure_utc(article["published_at"]) == PUBLISHED   # first publication kept
    # the source did not move its own timestamp, so all we know is when we saw it
    assert ensure_utc(article["revised_at"]) == LATER

    # a revision that DOES move the source's timestamp is dated by the source
    restated_at = PUBLISHED + timedelta(hours=6)
    restated = _record(summary="Rates unchanged. Statement reissued in full.",
                       published=restated_at, ingested=LATER + timedelta(hours=1))
    _use_providers(monkeypatch, {"fed_press": _StubProvider("fed_press", [restated])})
    news_job.run_news_ingest(news_engine, settings, now=LATER + timedelta(hours=1))

    article = _articles(news_engine)[0]
    assert article["n_versions"] == 3
    assert ensure_utc(article["published_at"]) == PUBLISHED
    assert ensure_utc(article["revised_at"]) == restated_at
    assert ensure_utc(_versions(news_engine)[2]["published_at"]) == restated_at


def test_duplicate_urls_collapse_to_one_article(news_engine, settings, monkeypatch, news_on):
    _seed_source(news_engine)
    same_article_again = _record(
        url="https://federalreserve.gov/newsevents/pressreleases/"
            "monetary20260715a.htm?utm_source=rss#content"
    )
    _use_providers(
        monkeypatch,
        {"fed_press": _StubProvider("fed_press", [_record(), same_article_again])},
    )

    out = news_job.run_news_ingest(news_engine, settings, now=INGESTED)

    assert out["articles_fetched"] == 2
    assert out["articles_inserted"] == 1
    assert out["unchanged"] == 1
    assert len(_articles(news_engine)) == 1
    assert len(_versions(news_engine)) == 1


def test_near_duplicate_titles_collapse_to_one_story(
    news_engine, settings, monkeypatch, news_on
):
    _seed_source(news_engine)
    restated = _record(
        title="Federal Reserve Board issues FOMC statement",
        url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20260715b.htm",
    )
    _use_providers(
        monkeypatch, {"fed_press": _StubProvider("fed_press", [_record(), restated])}
    )

    out = news_job.run_news_ingest(news_engine, settings, now=INGESTED)

    assert out["articles_inserted"] == 1     # one distinct story
    assert out["near_duplicates"] == 1
    rows = _articles(news_engine)
    assert len(rows) == 2
    originals = [r for r in rows if r["duplicate_of"] is None]
    copies = [r for r in rows if r["duplicate_of"] is not None]
    assert len(originals) == 1 and len(copies) == 1
    assert copies[0]["duplicate_of"] == originals[0]["id"]


def test_source_failure_does_not_sink_the_job(
    news_engine, settings, monkeypatch, news_on, caplog
):
    _seed_source(news_engine, code="fed_press")
    _seed_source(news_engine, code="other_feed")
    _use_providers(
        monkeypatch,
        {
            "fed_press": _StubProvider("fed_press", [_record()]),
            "other_feed": _StubProvider("other_feed", error="feed host unreachable"),
        },
    )

    with caplog.at_level(logging.WARNING):
        out = news_job.run_news_ingest(news_engine, settings, now=INGESTED)

    assert out["sources_failed"] == 1
    assert out["sources_polled"] == 1
    assert out["articles_inserted"] == 1          # the healthy source still ingested
    assert any("other_feed" in message for message in out["errors"])
    assert any(record.levelname == "WARNING" for record in caplog.records)

    with news_engine.connect() as conn:
        failed = conn.execute(
            news_sources.select().where(news_sources.c.code == "other_feed")
        ).first()._mapping
    assert failed["consecutive_failures"] == 1
    assert "unreachable" in failed["last_error"]
    # the attempt consumed its slot even though it failed
    assert ensure_utc(failed["last_polled_at"]) == INGESTED


@respx.mock
def test_malformed_feed_source_yields_zero_articles_without_raising(
    news_engine, settings, monkeypatch, news_on
):
    respx.get(fedpress.FEED_URL).mock(
        return_value=httpx.Response(200, text="<rss><channel><item>")
    )
    _seed_source(news_engine)
    monkeypatch.setattr(
        news_job,
        "build_news_provider",
        lambda code, s: fedpress.FedPressProvider(
            timeout=2.0, courtesy_delay=0.0, backoff_base=0.0
        ),
    )

    out = news_job.run_news_ingest(news_engine, settings, now=INGESTED)

    assert out["sources_failed"] == 1
    assert out["articles_inserted"] == 0
    assert _articles(news_engine) == []


def test_courtesy_interval_skips_recently_polled_source(
    news_engine, settings, monkeypatch, news_on
):
    _seed_source(news_engine, min_interval=900,
                 last_polled=INGESTED - timedelta(minutes=5))
    _use_providers(monkeypatch, {"fed_press": _StubProvider("fed_press", [_record()])})

    skipped = news_job.run_news_ingest(news_engine, settings, now=INGESTED)
    assert skipped["sources_skipped"] == 1
    assert skipped["sources_polled"] == 0
    assert _articles(news_engine) == []

    polled = news_job.run_news_ingest(
        news_engine, settings, now=INGESTED + timedelta(minutes=20)
    )
    assert polled["sources_polled"] == 1
    assert polled["articles_inserted"] == 1


def test_unapproved_and_disabled_sources_are_never_polled(news_engine, settings, news_on):
    _seed_source(news_engine, code="gdelt", enabled=False, policy_status="exploratory")
    _seed_source(news_engine, code="blocked_feed", enabled=True, policy_status="excluded")

    out = news_job.run_news_ingest(news_engine, settings, now=INGESTED)

    assert out["sources_polled"] == 0
    assert out["sources_skipped"] == 0    # neither row is even loaded
    assert _articles(news_engine) == []
    # and no fetcher exists for the exploratory source in the first place
    assert news_job.build_news_provider("gdelt", settings) is None
    assert news_job.build_news_provider("fed_press", settings) is not None
