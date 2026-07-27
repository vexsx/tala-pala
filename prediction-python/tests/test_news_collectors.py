"""Collector tests: timestamps, snapshot diffs, Iran rules, and the GDELT throttle.

Every test runs off a fixture under ``tests/fixtures`` with ``safe_get``
monkeypatched, so nothing here reaches the network — which also means these
tests say nothing about whether a source is up, only about what this code does
with what a source returns.

The properties under test are the ones that would be expensive to discover in
production: a fetch time stored as a publication time is invisible until a
backtest silently leaks; a snapshot diff that mistakes XML reordering for a
designation change invents events; and a throttle that a retry path can skip is
the reason GDELT rate-limited this host in the first place.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.db import ensure_utc, metadata
from app.news import (
    news_articles,
    news_collection_attempts,
    news_raw_payloads,
    news_source_queries,
    news_sources,
    safefetch,
)
from app.news import sources as news_sources_pkg
from app.news.sources import fed, gdelt, ofac, sha256_text

from .conftest import load_fixture_text

FETCHED_AT = datetime(2026, 7, 15, 20, 30, tzinfo=timezone.utc)
FOMC_TITLE = "Federal Reserve issues FOMC statement"
FOMC_URL = "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260715a.htm"
SPEECH_TITLE = "Speech by Governor Adams on the economic outlook and monetary policy"
DATELESS_TITLE = "Federal Reserve Board announces termination of enforcement action"


# --- helpers ------------------------------------------------------------------


class StubResponse:
    """A ``FetchOutcome`` as the safefetch adapter would return one."""

    def __init__(self, status_code: int, text: str, content_type: str = "text/xml"):
        self.status_code = status_code
        self.text = text
        self.content_type = content_type
        self.fetched_at = FETCHED_AT


class FakeClock:
    """Monotonic clock that only advances when something sleeps on it."""

    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


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


@pytest.fixture(autouse=True)
def reset_gdelt_throttle():
    """The throttle is process state on purpose; tests must not inherit it."""
    gdelt.THROTTLE.reset()
    yield
    gdelt.THROTTLE.reset()


def seed_source(engine, code, *, feed_url="https://example.invalid/feed", enabled=True,
                policy_status="approved", min_interval=0):
    with engine.begin() as conn:
        conn.execute(
            news_sources.insert().values(
                code=code,
                name=f"{code} (test)",
                feed_url=feed_url,
                kind="rss",
                jurisdiction="us",
                language="en",
                enabled=enabled,
                policy_status=policy_status,
                policy_note="test fixture",
                min_interval_seconds=min_interval,
            )
        )


def seed_query(engine, code, query_text, *, max_records=75, enabled=True) -> int:
    with engine.begin() as conn:
        result = conn.execute(
            news_source_queries.insert().values(
                source_code=gdelt.SOURCE_CODE,
                code=code,
                query_text=query_text,
                description="test query",
                enabled=enabled,
                max_records=max_records,
            )
        )
        return int(result.inserted_primary_key[0])


def rows(engine, table, order_by=None):
    with engine.begin() as conn:
        statement = table.select()
        if order_by is not None:
            statement = statement.order_by(order_by)
        return [dict(row._mapping) for row in conn.execute(statement)]


def provenance_of(row) -> dict:
    """``raw_payload`` as a dict (SQLite may hand back the JSON as text)."""
    payload = row["raw_payload"]
    return json.loads(payload) if isinstance(payload, str) else payload


def assert_links_payload(row, raw_payload_id):
    """The article records which stored body it was derived from.

    Asserted on the column when the SQLAlchemy mirror carries 0017's
    ``raw_payload_id``, and always on the provenance JSON, which every mirror
    generation carries.
    """
    assert provenance_of(row)["raw_payload_id"] == raw_payload_id
    if "raw_payload_id" in news_articles.c:
        assert row["raw_payload_id"] == raw_payload_id


# --- the safefetch adapter -------------------------------------------------------


def test_safe_get_returns_a_status_instead_of_raising_it(monkeypatch):
    captured = {}

    def capture(url, *, allowed_hosts, policy):
        captured.update(url=url, hosts=tuple(allowed_hosts), policy=policy)
        raise safefetch.FetchHTTPError("rate limited", status_code=429, retryable=True)

    monkeypatch.setattr(news_sources_pkg, "fetch", capture)

    outcome = news_sources_pkg.safe_get(
        "https://api.gdeltproject.org/api/v2/doc/doc",
        allow_hosts=("api.gdeltproject.org",),
        max_bytes=4096,
        timeout=7.0,
    )

    # A status is an answer about the source: GDELT has to see the 429 to stop
    # its pass, which an exception unwinding past the loop would prevent.
    assert outcome.status_code == 429
    assert outcome.text == ""
    assert captured["hosts"] == ("api.gdeltproject.org",)
    assert captured["policy"].max_bytes == 4096
    assert captured["policy"].read_timeout == 7.0
    # Retrying inside the fetch layer would bypass this package's own throttle.
    assert captured["policy"].max_attempts == 1


def test_safe_get_propagates_a_failure_that_carries_no_status(monkeypatch):
    def blocked(url, *, allowed_hosts, policy):
        raise safefetch.FetchBlocked("host not on the allowlist")

    monkeypatch.setattr(news_sources_pkg, "fetch", blocked)

    with pytest.raises(safefetch.FetchError):
        news_sources_pkg.safe_get(
            "https://elsewhere.invalid/feed",
            allow_hosts=("www.federalreserve.gov",),
            max_bytes=1024,
            timeout=5.0,
        )


# --- fed: the publication clock ------------------------------------------------


def test_fed_keeps_the_feeds_own_publication_time():
    articles = fed.parse_releases(
        load_fixture_text("fed_press.xml"), fetched_at=FETCHED_AT
    )
    by_title = {article.title: article for article in articles}

    fomc = by_title[FOMC_TITLE]
    assert fomc.source_published_at == datetime(2026, 7, 15, 19, 0, tzinfo=timezone.utc)
    assert fomc.published_at_is_estimated is False
    assert fomc.source_published_at != FETCHED_AT

    # A numeric UTC offset parses to the same instant a named zone would.
    speech = by_title[SPEECH_TITLE]
    assert speech.source_published_at == datetime(2026, 7, 14, 14, 30, tzinfo=timezone.utc)
    assert speech.published_at_is_estimated is False


def test_fed_missing_pubdate_leaves_the_publication_time_null_and_flags_it():
    articles = fed.parse_releases(
        load_fixture_text("fed_press.xml"), fetched_at=FETCHED_AT
    )
    dateless = next(a for a in articles if a.title == DATELESS_TITLE)

    # The distinction the whole subsystem rests on: "the source stated no
    # publication time" must not become "published at the moment we fetched".
    assert dateless.source_published_at is None
    assert dateless.published_at_is_estimated is True
    assert dateless.available_at == FETCHED_AT


def test_fed_available_at_is_when_we_could_first_have_acted():
    articles = fed.parse_releases(
        load_fixture_text("fed_press.xml"), fetched_at=FETCHED_AT
    )
    for article in articles:
        assert article.available_at >= FETCHED_AT
        if article.source_published_at is not None:
            assert article.available_at == max(article.source_published_at, FETCHED_AT)

    # A source that post-dates its own item is available at ITS time, not ours.
    future = FETCHED_AT + timedelta(hours=2)
    late = fed.parse_releases(
        f"""<?xml version="1.0"?><rss><channel><item>
            <title>Embargoed release</title>
            <link>https://www.federalreserve.gov/newsevents/pressreleases/x.htm</link>
            <pubDate>{future.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
        </item></channel></rss>""",
        fetched_at=FETCHED_AT,
    )
    assert late[0].available_at == future


def test_fed_malformed_body_yields_no_articles_without_raising():
    for broken in ("", "   ", "<rss><channel><item>", "not xml", "<html>challenge</html>"):
        assert fed.parse_releases(broken, fetched_at=FETCHED_AT) == []


# --- fed: document kind --------------------------------------------------------


def test_fed_classifies_releases_and_keeps_a_routine_speech_low_importance():
    statement = fed.classify_release(FOMC_TITLE, FOMC_URL)
    assert statement.kind == fed.KIND_FOMC_STATEMENT
    assert statement.importance == "high"

    minutes = fed.classify_release(
        "Minutes of the Federal Open Market Committee, June 16-17, 2026",
        "https://www.federalreserve.gov/monetarypolicy/fomcminutes20260617.htm",
    )
    assert minutes.kind == fed.KIND_FOMC_MINUTES

    speech = fed.classify_release(
        SPEECH_TITLE, "https://www.federalreserve.gov/newsevents/speech/adams20260714a.htm"
    )
    assert speech.kind == fed.KIND_SPEECH
    # A speech is a routine document however monetary its subject; importance
    # here describes the document type, and nothing has measured a speech yet.
    assert speech.importance == "low"
    assert speech.importance != "high"

    # A speech ABOUT the statement is still a speech: URL evidence wins.
    about = fed.classify_release(
        "Speech on the FOMC statement and the outlook",
        "https://www.federalreserve.gov/newsevents/speech/adams20260716a.htm",
    )
    assert about.kind == fed.KIND_SPEECH
    assert about.importance == "low"

    other = fed.classify_release(
        "Testimony on supervision", "https://www.federalreserve.gov/newsevents/testimony/x.htm"
    )
    assert other.kind == fed.KIND_OTHER
    assert other.rule_id.startswith(fed.KIND_RULES_VERSION)


# --- fed: collection ------------------------------------------------------------


def test_fed_collect_is_disabled_by_default(news_engine, settings, monkeypatch):
    seed_source(news_engine, fed.SOURCE_CODE)

    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("collection ran with NEWS_COLLECTION_ENABLED off")

    monkeypatch.setattr(fed, "safe_get", forbidden)

    out = fed.collect(news_engine, settings)

    assert out["status"] == "skipped"
    assert out["reason"] == "collection_disabled"
    assert rows(news_engine, news_raw_payloads) == []


def test_fed_collect_stores_the_body_then_the_articles(
    news_engine, collecting_settings, monkeypatch
):
    body = load_fixture_text("fed_press.xml")
    seed_source(news_engine, fed.SOURCE_CODE)
    monkeypatch.setattr(fed, "safe_get", lambda url, **kw: StubResponse(200, body))

    out = fed.collect(news_engine, collecting_settings)

    assert out["status"] == "ok"
    assert out["items_new"] == 4

    payloads = rows(news_engine, news_raw_payloads)
    assert len(payloads) == 1
    # The stored hash identifies the bytes we received, not the rows we derived.
    assert payloads[0]["body_sha256"] == sha256_text(body)
    assert payloads[0]["body"] == body
    assert bool(payloads[0]["truncated"]) is False
    assert payloads[0]["parser_version"] == fed.PARSER_VERSION

    stored = {row["title"]: row for row in rows(news_engine, news_articles)}
    fomc = stored[FOMC_TITLE]
    assert_links_payload(fomc, out["raw_payload_id"])
    assert ensure_utc(fomc["published_at"]) == datetime(
        2026, 7, 15, 19, 0, tzinfo=timezone.utc
    )
    assert bool(fomc["published_at_estimated"]) is False
    assert provenance_of(fomc)["release_kind"] == fed.KIND_FOMC_STATEMENT
    assert provenance_of(fomc)["importance"] == "high"

    speech = stored[SPEECH_TITLE]
    assert provenance_of(speech)["importance"] == "low"

    dateless = stored[DATELESS_TITLE]
    # published_at is NOT NULL in the schema, so it holds the only defensible
    # value (when we could act) with the flag that says it was not stated.
    assert bool(dateless["published_at_estimated"]) is True
    assert provenance_of(dateless)["source_published_at"] is None
    assert ensure_utc(dateless["published_at"]) == ensure_utc(
        datetime.fromisoformat(provenance_of(dateless)["available_at"])
    )

    attempts = rows(news_engine, news_collection_attempts)
    assert [a["outcome"] for a in attempts] == ["ok"]
    assert attempts[0]["items_new"] == 4


def test_fed_collect_is_idempotent_over_the_same_window(
    news_engine, collecting_settings, monkeypatch
):
    body = load_fixture_text("fed_press.xml")
    seed_source(news_engine, fed.SOURCE_CODE)
    monkeypatch.setattr(fed, "safe_get", lambda url, **kw: StubResponse(200, body))

    first = fed.collect(news_engine, collecting_settings)
    second = fed.collect(news_engine, collecting_settings)

    assert first["items_new"] == 4
    assert second["items_new"] == 0
    assert second["items_duplicate"] == 4
    assert len(rows(news_engine, news_articles)) == 4


def test_fed_collect_records_a_failed_fetch_without_storing_articles(
    news_engine, collecting_settings, monkeypatch
):
    seed_source(news_engine, fed.SOURCE_CODE)
    monkeypatch.setattr(fed, "safe_get", lambda url, **kw: StubResponse(503, ""))

    out = fed.collect(news_engine, collecting_settings)

    assert out["status"] == "error"
    assert rows(news_engine, news_articles) == []
    attempts = rows(news_engine, news_collection_attempts)
    assert attempts[0]["outcome"] == "error"
    assert attempts[0]["http_status"] == 503
    source = rows(news_engine, news_sources)[0]
    assert source["consecutive_failures"] == 1
    assert source["last_polled_at"] is not None  # stamped on failure too


# --- ofac: snapshot diff --------------------------------------------------------


def parsed_snapshots():
    a, _ = ofac.parse_sdn(load_fixture_text("ofac_sdn_a.xml"))
    b, _ = ofac.parse_sdn(load_fixture_text("ofac_sdn_b.xml"))
    return a, b


def test_ofac_parses_entries_with_the_published_namespace():
    snapshot, meta = ofac.parse_sdn(load_fixture_text("ofac_sdn_a.xml"))

    assert set(snapshot) == {"900001", "900002", "900003"}
    assert meta["publish_date"] == "07/14/2026"
    alpha = snapshot["900001"]
    assert alpha["name"] == "SYNTHETIC ALPHA SHIPPING COMPANY"
    assert alpha["programs"] == ["IFSR", "IRAN"]
    assert alpha["sdn_type"] == "Entity"


def test_ofac_diff_yields_the_expected_added_removed_and_modified_sets():
    a, b = parsed_snapshots()
    changes = ofac.diff_snapshots(a, b)
    by_kind = {}
    for change in changes:
        by_kind.setdefault(change.change, set()).add(change.uid)

    assert by_kind[ofac.CHANGE_ADDED] == {"900004"}
    assert by_kind[ofac.CHANGE_REMOVED] == {"900003"}
    assert by_kind[ofac.CHANGE_MODIFIED] == {"900001"}

    modified = next(c for c in changes if c.change == ofac.CHANGE_MODIFIED)
    assert set(modified.changed_fields) == {"programs", "remarks"}
    assert modified.previous["programs"] == ["IFSR", "IRAN"]
    assert modified.record["programs"] == ["IFSR", "IRAN", "IRGC"]

    # 900002 differs only in the order of its XML children, which is not a
    # change to the designation and must not be reported as one.
    assert "900002" not in by_kind.get(ofac.CHANGE_MODIFIED, set())


def test_ofac_diff_of_a_snapshot_against_itself_is_empty():
    a, _ = parsed_snapshots()
    assert ofac.diff_snapshots(a, a) == []


# --- ofac: Iran relevance rules --------------------------------------------------


def test_ofac_iran_relevance_requires_an_explicit_rule_and_records_the_reason():
    a, b = parsed_snapshots()

    direct = ofac.iran_relevance(a["900001"])
    assert direct.relevant is True
    assert direct.rule_id == f"{ofac.IRAN_RULES_VERSION}.direct_program"
    assert "IRAN" in direct.matched_reason

    conditional = ofac.iran_relevance(a["900002"])
    assert conditional.relevant is True
    assert conditional.rule_id.endswith("conditional_program_with_iran_evidence")
    assert "NPWMD" in conditional.matched_reason
    assert "tehran" in conditional.matched_reason.lower()

    added = ofac.iran_relevance(b["900004"])
    assert added.relevant is True
    assert "IRAN-HR" in added.matched_reason


def test_ofac_non_iran_designation_is_not_flagged_iran():
    a, _ = parsed_snapshots()

    # Shares the NPWMD program with the Iran-linked entry and carries no Iran
    # evidence at all: the shared program alone must not flag it.
    verdict = ofac.iran_relevance(a["900003"])

    assert verdict.relevant is False
    assert verdict.rule_id.endswith("conditional_program_without_iran_evidence")
    assert verdict.matched_reason  # the negative decision is recorded too
    assert verdict.matched_terms == ()


def test_ofac_word_boundary_stops_a_substring_from_flagging_iran():
    record = {
        "name": "TIRANA HOLDINGS",
        "programs": ["NPWMD"],
        "remarks": "No connection asserted.",
    }
    assert ofac.iran_relevance(record).relevant is False


# --- ofac: normalization ---------------------------------------------------------


def build_changes_articles():
    a, b = parsed_snapshots()
    changes = ofac.diff_snapshots(a, b)
    return ofac.build_articles(
        changes,
        snapshot_sha256="b" * 64,
        previous_sha256="a" * 64,
        list_url=ofac.DEFAULT_LIST_URL,
        list_meta={"publish_date": "07/15/2026", "record_count": "3"},
        fetched_at=FETCHED_AT,
    )


def test_ofac_removal_is_recorded_as_a_fact_not_as_relief():
    articles = {a.provenance["uid"]: a for a in build_changes_articles()}
    removal = articles["900003"]

    assert removal.provenance["change"] == ofac.CHANGE_REMOVED
    assert removal.provenance["interpretation"]["hypothesis_only"] is True
    assert removal.provenance["interpretation"]["note"] == ofac.REMOVAL_NOTE
    assert "relief" not in removal.title.lower()
    assert "relief" not in removal.summary.lower()
    # The removed entry was never Iran-related, and delisting does not make it so.
    assert removal.provenance["iran_relevance"]["relevant"] is False


def test_ofac_articles_state_no_publication_time_and_carry_the_matched_reason():
    articles = build_changes_articles()
    assert articles

    for article in articles:
        # The list states a publication DATE and no time; inventing an hour
        # would be inventing evidence.
        assert article.source_published_at is None
        assert article.published_at_is_estimated is True
        assert article.available_at == FETCHED_AT
        relevance = article.provenance["iran_relevance"]
        assert relevance["rules_version"] == ofac.IRAN_RULES_VERSION
        assert relevance["matched_reason"]
        assert article.provenance["list_publish_date"] == "07/15/2026"

    modified = next(a for a in articles if a.provenance["change"] == ofac.CHANGE_MODIFIED)
    assert modified.provenance["changed_fields"] == ["programs", "remarks"]
    assert modified.provenance["previous_values"]["programs"] == ["IFSR", "IRAN"]


# --- ofac: collection -------------------------------------------------------------


def test_ofac_first_snapshot_is_a_baseline_and_the_next_one_diffs(
    news_engine, collecting_settings, monkeypatch
):
    snapshot_a = load_fixture_text("ofac_sdn_a.xml")
    snapshot_b = load_fixture_text("ofac_sdn_b.xml")
    seed_source(news_engine, ofac.SOURCE_CODE, feed_url=ofac.DEFAULT_LIST_URL)
    served = {"body": snapshot_a}
    monkeypatch.setattr(ofac, "safe_get", lambda url, **kw: StubResponse(200, served["body"]))

    first = ofac.collect(news_engine, collecting_settings)

    # Day one has nothing to compare against: reporting every entry as "added"
    # would manufacture designations that did not happen today.
    assert first["baseline"] is True
    assert first["reason"] == "first_snapshot"
    assert (first["added"], first["removed"], first["modified"]) == (0, 0, 0)
    assert first["items_seen"] == 3
    assert rows(news_engine, news_articles) == []
    assert len(rows(news_engine, news_raw_payloads)) == 1

    served["body"] = snapshot_b
    second = ofac.collect(news_engine, collecting_settings)

    assert second["baseline"] is False
    assert (second["added"], second["removed"], second["modified"]) == (1, 1, 1)
    assert second["items_new"] == 3
    assert len(rows(news_engine, news_raw_payloads)) == 2

    stored = rows(news_engine, news_articles)
    assert len(stored) == 3
    by_uid = {provenance_of(row)["uid"]: row for row in stored}
    assert set(by_uid) == {"900001", "900003", "900004"}
    assert_links_payload(by_uid["900004"], second["raw_payload_id"])
    assert provenance_of(by_uid["900004"])["snapshot_sha256"] == sha256_text(snapshot_b)
    assert provenance_of(by_uid["900004"])["previous_snapshot_sha256"] == sha256_text(
        snapshot_a
    )


def test_ofac_identical_snapshot_is_not_stored_twice(
    news_engine, collecting_settings, monkeypatch
):
    body = load_fixture_text("ofac_sdn_a.xml")
    seed_source(news_engine, ofac.SOURCE_CODE, feed_url=ofac.DEFAULT_LIST_URL)
    monkeypatch.setattr(ofac, "safe_get", lambda url, **kw: StubResponse(200, body))

    ofac.collect(news_engine, collecting_settings)
    again = ofac.collect(news_engine, collecting_settings)

    assert again["unchanged_snapshot"] is True
    assert again["reason"] == "snapshot_unchanged"
    assert len(rows(news_engine, news_raw_payloads)) == 1
    # The fetch still happened and is still evidenced.
    assert len(rows(news_engine, news_collection_attempts)) == 2


def test_ofac_unregistered_source_does_not_fetch(news_engine, collecting_settings, monkeypatch):
    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("fetched a source with no registry row")

    monkeypatch.setattr(ofac, "safe_get", forbidden)

    out = ofac.collect(news_engine, collecting_settings)

    assert out["reason"] == "source_not_registered"


# --- gdelt: throttle ---------------------------------------------------------------


def test_gdelt_throttle_spaces_every_request_including_retries(
    news_engine, collecting_settings, monkeypatch
):
    clock = FakeClock()
    monkeypatch.setattr(gdelt.THROTTLE, "monotonic", clock.monotonic)
    monkeypatch.setattr(gdelt.THROTTLE, "sleep", clock.sleep)
    gdelt.THROTTLE.reset()

    seed_source(news_engine, gdelt.SOURCE_CODE, feed_url=gdelt.DEFAULT_API_URL)
    seed_query(news_engine, "q_gold", "gold price")
    seed_query(news_engine, "q_rial", "iranian rial")

    body = load_fixture_text("gdelt_doc.json")
    request_times: list[float] = []
    attempts = {"n": 0}

    def flaky(url, **kwargs):
        request_times.append(clock.now)
        attempts["n"] += 1
        # Every first attempt fails transiently, so each query retries once:
        # the retry is the request most likely to slip past a loop-level delay.
        if attempts["n"] % 2 == 1:
            return StubResponse(503, "")
        return StubResponse(200, body, content_type="application/json")

    monkeypatch.setattr(gdelt, "safe_get", flaky)

    out = gdelt.collect(news_engine, collecting_settings)

    assert len(request_times) == 4  # two queries, one retry each
    gaps = [later - earlier for earlier, later in zip(request_times, request_times[1:])]
    assert gaps and all(gap >= gdelt.MIN_REQUEST_INTERVAL_SECONDS for gap in gaps)
    assert clock.sleeps == [5.0, 5.0, 5.0]
    assert out["status"] == "ok"


def test_gdelt_throttle_is_module_state_shared_across_calls(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(gdelt.THROTTLE, "monotonic", clock.monotonic)
    monkeypatch.setattr(gdelt.THROTTLE, "sleep", clock.sleep)
    gdelt.THROTTLE.reset()

    assert gdelt.THROTTLE.wait(5.0) == 0.0      # nothing to wait for yet
    assert gdelt.THROTTLE.wait(5.0) == 5.0      # a second caller pays the gap
    clock.now += 12.0
    assert gdelt.THROTTLE.wait(5.0) == 0.0      # enough time already passed


def test_gdelt_configured_interval_cannot_go_below_the_floor(settings):
    settings.gdelt_min_interval_seconds = 0.1
    assert gdelt.min_interval_seconds(settings) == gdelt.MIN_REQUEST_INTERVAL_SECONDS

    settings.gdelt_min_interval_seconds = 30.0
    assert gdelt.min_interval_seconds(settings) == 30.0


def test_gdelt_rate_limit_stops_the_whole_pass(
    news_engine, collecting_settings, monkeypatch
):
    clock = FakeClock()
    monkeypatch.setattr(gdelt.THROTTLE, "monotonic", clock.monotonic)
    monkeypatch.setattr(gdelt.THROTTLE, "sleep", clock.sleep)
    seed_source(news_engine, gdelt.SOURCE_CODE, feed_url=gdelt.DEFAULT_API_URL)
    seed_query(news_engine, "q_a", "gold price")
    seed_query(news_engine, "q_b", "iranian rial")

    calls = {"n": 0}

    def rate_limited(url, **kwargs):
        calls["n"] += 1
        return StubResponse(429, "")

    monkeypatch.setattr(gdelt, "safe_get", rate_limited)

    out = gdelt.collect(news_engine, collecting_settings)

    # A host that told us to stop is not retried and the second query is never
    # attempted: this is the failure that got the source disabled in 0016.
    assert calls["n"] == 1
    assert out["status"] == "throttled"
    assert out["reason"] == "rate_limited"
    assert rows(news_engine, news_collection_attempts)[0]["outcome"] == "throttled"


# --- gdelt: normalization and dedupe -------------------------------------------------


def test_gdelt_no_configured_queries_is_a_no_op(
    news_engine, collecting_settings, monkeypatch
):
    seed_source(news_engine, gdelt.SOURCE_CODE, feed_url=gdelt.DEFAULT_API_URL)

    def forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("queried GDELT with no configured query")

    monkeypatch.setattr(gdelt, "safe_get", forbidden)

    out = gdelt.collect(news_engine, collecting_settings)

    assert out["reason"] == "no_queries_configured"
    assert out["queries_run"] == 0


def test_gdelt_seendate_is_kept_apart_from_publication_time():
    document = json.loads(load_fixture_text("gdelt_doc.json"))
    articles = gdelt.parse_articles(
        document, query_text="gold price", query_id=7, fetched_at=FETCHED_AT
    )
    assert len(articles) == 3

    first = articles[0]
    # GDELT states when its crawler saw the page, never when the outlet
    # published it; the two must not be conflated.
    assert first.provenance["gdelt_seendate"] == "20260715T141500Z"
    assert first.provenance["gdelt_seen_at"] == "2026-07-15T14:15:00+00:00"
    assert first.source_published_at is None
    assert first.published_at_is_estimated is True
    assert first.provenance["publication_time_available"] is False
    assert first.available_at == FETCHED_AT
    assert first.provenance["query_text"] == "gold price"
    assert first.query_id == 7

    persian = articles[1]
    assert persian.language == "fa"


def test_gdelt_tone_is_stored_as_unverified_metadata_only():
    document = json.loads(load_fixture_text("gdelt_doc.json"))
    # artlist carries no tone today; a mode that returns one must not turn it
    # into a score by arriving.
    document["articles"][0]["tone"] = "-3.2"

    articles = gdelt.parse_articles(
        document, query_text="gold price", query_id=1, fetched_at=FETCHED_AT
    )

    unverified = articles[0].provenance["unverified"]
    assert unverified["tone"] == "-3.2"
    assert "unverified" in unverified["note"]
    assert "unverified" not in articles[1].provenance


def test_gdelt_same_url_from_two_queries_is_counted_once(
    news_engine, collecting_settings, monkeypatch
):
    clock = FakeClock()
    monkeypatch.setattr(gdelt.THROTTLE, "monotonic", clock.monotonic)
    monkeypatch.setattr(gdelt.THROTTLE, "sleep", clock.sleep)

    document = json.loads(load_fixture_text("gdelt_doc.json"))
    shared = document["articles"][1]              # returned by both queries
    unique = copy.deepcopy(document["articles"][0])
    unique["url"] = "https://other.example.net/2026/07/15/synthetic-third-fixture"
    unique["title"] = "Synthetic fixture item: third story - Example Net"
    second_document = {"articles": [copy.deepcopy(shared), unique]}

    seed_source(news_engine, gdelt.SOURCE_CODE, feed_url=gdelt.DEFAULT_API_URL)
    seed_query(news_engine, "q_a_gold", "gold price")
    seed_query(news_engine, "q_b_rial", "iranian rial")

    bodies = [json.dumps(document), json.dumps(second_document)]
    served: list[str] = []

    def serve(url, **kwargs):
        served.append(url)
        return StubResponse(200, bodies[len(served) - 1], content_type="application/json")

    monkeypatch.setattr(gdelt, "safe_get", serve)

    out = gdelt.collect(news_engine, collecting_settings)

    assert out["queries_run"] == 2
    assert out["items_seen"] == 5                 # 3 + 2 records returned
    assert out["cross_query_duplicates"] == 1     # the shared story
    assert out["duplicates_dropped"] == 2         # plus the tracking-param twin
    assert out["items_new"] == 3

    stored = rows(news_engine, news_articles)
    assert len(stored) == 3
    canonicals = [row["canonical_url"] for row in stored]
    assert len(set(canonicals)) == 3
    # The surviving row keeps the query that actually produced it.
    shared_row = next(
        row for row in stored if "synthetic-currency-fixture" in row["canonical_url"]
    )
    assert provenance_of(shared_row)["query_text"] == "gold price"

    # Each query's response is stored under its own query id.
    payloads = rows(news_engine, news_raw_payloads, order_by=news_raw_payloads.c.id)
    assert len(payloads) == 2
    assert payloads[0]["query_id"] != payloads[1]["query_id"]
    assert "gold+price" in payloads[0]["request_url"]
