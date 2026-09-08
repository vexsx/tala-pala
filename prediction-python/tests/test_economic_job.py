"""The ingest pass end to end: catalog -> provider -> vintages -> endpoint.

Network access is mocked with respx against the recorded fixtures described in
tests/test_economic_providers.py. The endpoint tests use a stub provider
instead, following the pattern in tests/test_api.py, because respx and the
FastAPI TestClient both want to own httpx's transport.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.db import data_providers, economic_observations, economic_series, instruments
from app.economic import SeriesFetch, SeriesPoint
from app.economic.store import point_in_time, vintages_for_period
from app.jobs.economic import EconomicIngestFailed, ingest_economic
from app.providers import registry
from app.providers.imf_weo import IMFWeoProvider

from .conftest import TEST_TOKEN, load_fixture_json

AUTH = {"X-Internal-Token": TEST_TOKEN}

WB_IRN = "WB_CPI_IRN"
WB_ARE = "WB_CPI_ARE"
IMF_IRN = "IMF_PCPIPCH_IRN"

WB_PATH = "/v2/country/{iso3}/indicator/FP.CPI.TOTL"
IMF_URL = "https://www.imf.org/external/datamapper/api/v1/PCPIPCH/IRN"


def _mock_worldbank(iso3: str, payload):
    return respx.get(host="api.worldbank.org", path=WB_PATH.format(iso3=iso3)).mock(
        return_value=httpx.Response(200, json=payload)
    )


def _stored(engine, code: str) -> list[dict]:
    with engine.connect() as conn:
        series_id = conn.execute(
            select(economic_series.c.id).where(economic_series.c.code == code)
        ).scalar_one()
        rows = conn.execute(
            select(economic_observations)
            .where(economic_observations.c.series_id == series_id)
            .order_by(economic_observations.c.ref_period_start)
        ).all()
    return [dict(row._mapping) for row in rows]


# --- a first ingest ---------------------------------------------------------


@respx.mock
def test_first_ingest_stores_every_period_as_vintage_1(engine, settings):
    _mock_worldbank("IRN", load_fixture_json("worldbank_cpi_irn.json"))

    result = ingest_economic(engine, settings, [WB_IRN])

    counts = result["series"][WB_IRN]
    assert result["errors"] == []
    assert counts["status"] == "ok"
    assert (counts["inserted"], counts["unchanged"], counts["revised"]) == (66, 0, 0)
    assert counts["skipped_null"] == 0
    # The report states the provenance of what it stored, not only the count.
    assert counts["source_meta"]["last_updated"] == "2026-07-13"

    rows = _stored(engine, WB_IRN)
    assert len(rows) == 66
    assert {row["vintage"] for row in rows} == {1}
    assert all(row["available_at"] is not None for row in rows)
    # Neither source stamps an observation with a publication moment.
    assert all(row["published_at"] is None for row in rows)
    # One fetch, one availability instant.
    assert len({row["available_at"] for row in rows}) == 1


@respx.mock
def test_ingest_registers_the_instrument_and_series_rows(engine, settings):
    _mock_worldbank("IRN", load_fixture_json("worldbank_cpi_irn.json"))
    ingest_economic(engine, settings, [WB_IRN])

    with engine.connect() as conn:
        instrument = conn.execute(
            select(instruments).where(instruments.c.code == WB_IRN)
        ).one()._mapping
    assert instrument["kind"] == "economic_series"
    assert instrument["domain"] == "macro"
    assert instrument["quality_tier"] == "official_mirror"
    assert "2010=100" in instrument["notes"] or instrument["notes"]


@respx.mock
def test_reingesting_an_unchanged_source_writes_nothing(engine, settings):
    payload = load_fixture_json("worldbank_cpi_irn.json")
    _mock_worldbank("IRN", payload)

    first = ingest_economic(engine, settings, [WB_IRN])["series"][WB_IRN]
    second = ingest_economic(engine, settings, [WB_IRN])["series"][WB_IRN]

    assert first["inserted"] == 66
    assert (second["inserted"], second["unchanged"], second["revised"]) == (0, 66, 0)
    assert len(_stored(engine, WB_IRN)) == 66


# --- a revision -------------------------------------------------------------


@respx.mock
def test_a_revised_source_value_becomes_vintage_2_and_the_first_print_survives(
    engine, settings
):
    original = load_fixture_json("worldbank_cpi_irn.json")
    route = _mock_worldbank("IRN", original)
    first = ingest_economic(engine, settings, [WB_IRN])["series"][WB_IRN]
    first_ingest_at = datetime.fromisoformat(first["available_at"])

    revised_payload = load_fixture_json("worldbank_cpi_irn.json")
    revised_payload[1][0]["value"] = 4111.2      # the 2025 row, restated
    route.mock(return_value=httpx.Response(200, json=revised_payload))

    second = ingest_economic(engine, settings, [WB_IRN])["series"][WB_IRN]

    assert (second["inserted"], second["unchanged"], second["revised"]) == (0, 65, 1)
    assert len(_stored(engine, WB_IRN)) == 67       # one row added, none replaced

    prints = vintages_for_period(engine, WB_IRN, date(2025, 1, 1))
    assert [item["vintage"] for item in prints] == [1, 2]
    assert prints[0]["value"] == pytest.approx(4030.3356899712)
    assert prints[1]["value"] == pytest.approx(4111.2)

    # Read as of the first ingest: the number that stood then.
    as_it_was = point_in_time(engine, WB_IRN, as_of=first_ingest_at)
    assert as_it_was.observations[-1].ref_period_label == "2025"
    assert as_it_was.observations[-1].value == pytest.approx(4030.3356899712)
    assert as_it_was.vintages == frozenset({1})

    # Read now: the number that stands now.
    latest = point_in_time(engine, WB_IRN)
    assert latest.observations[-1].value == pytest.approx(4111.2)
    assert latest.vintages == frozenset({1, 2})


# --- projections ------------------------------------------------------------


@respx.mock
def test_imf_projections_are_stored_flagged_and_separable(engine, settings):
    respx.get(IMF_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("imf_pcpipch.json"))
    )

    counts = ingest_economic(engine, settings, [IMF_IRN])["series"][IMF_IRN]
    assert counts["status"] == "ok"
    assert counts["projections"] == 6            # 2026..2031

    rows = _stored(engine, IMF_IRN)
    projections = [row for row in rows if row["is_projection"]]
    observations = [row for row in rows if not row["is_projection"]]
    assert [row["ref_period_label"] for row in projections] == [
        "2026", "2027", "2028", "2029", "2030", "2031"
    ]
    assert max(row["ref_period_label"] for row in observations) == "2025"
    # A projection is never a nowcast and never carries a publication claim.
    assert all(row["is_nowcast"] is False for row in rows)

    # The distinction survives a point-in-time read, which is what stops a
    # forecast being scored as an outcome: the default read simply has none.
    served = point_in_time(engine, IMF_IRN)
    assert all(item.is_projection is False for item in served.observations)
    assert max(item.ref_period_label for item in served.observations) == "2025"

    forecasts = point_in_time(engine, IMF_IRN, include_projections=True)
    forecast_years = {
        item.ref_period_label for item in forecasts.observations if item.is_projection
    }
    assert forecast_years == {"2026", "2027", "2028", "2029", "2030", "2031"}


@respx.mock
def test_the_new_year_does_not_turn_a_projection_into_an_observation(
    engine, settings, monkeypatch
):
    """The blocker, end to end: a forecast must not become a measurement.

    is_projection is computed from the clock, because neither source states it.
    So the first ingest of 2027 re-delivers the SAME 68.9 the WEO printed for
    Iran's 2026 — now for a finished year, so the adapter computes
    is_projection=False. Storing that would file a forecast as a measured 2026
    inflation rate on a series whose reported 2026 data the IMF does not
    publish until the April 2027 WEO. Nothing is stored, and the row keeps the
    flag it was written with.
    """
    import app.jobs.economic as job

    payload = load_fixture_json("imf_pcpipch.json")
    respx.get(IMF_URL).mock(return_value=httpx.Response(200, json=payload))

    first = ingest_economic(engine, settings, [IMF_IRN])["series"][IMF_IRN]
    assert first["projections"] == 6                      # 2026..2031

    monkeypatch.setattr(
        job, "utcnow", lambda: datetime(2027, 1, 2, 6, 0, tzinfo=timezone.utc)
    )
    second = ingest_economic(engine, settings, [IMF_IRN])["series"][IMF_IRN]

    # The adapter did flag one fewer period — 2026 is over — and the store
    # declined to act on it, because the IMF's NUMBER did not change.
    assert second["projections"] == 5                     # 2027..2031
    assert (second["inserted"], second["revised"]) == (0, 0)
    assert second["unchanged"] == first["inserted"]

    rows = _stored(engine, IMF_IRN)
    assert len(rows) == first["inserted"]                 # no manufactured vintage
    stored_2026 = next(row for row in rows if row["ref_period_label"] == "2026")
    assert bool(stored_2026["is_projection"]) is True
    assert stored_2026["vintage"] == 1

    # The read layer therefore still refuses to serve 2026 as an observation.
    served = point_in_time(engine, IMF_IRN)
    assert "2026" not in {item.ref_period_label for item in served.observations}
    assert served.is_revised is False


# --- gaps -------------------------------------------------------------------


@respx.mock
def test_null_source_values_are_skipped_not_stored_as_zero(engine, settings):
    _mock_worldbank("ARE", load_fixture_json("worldbank_cpi_are.json"))

    counts = ingest_economic(engine, settings, [WB_ARE])["series"][WB_ARE]

    assert counts["inserted"] == 19
    assert counts["skipped_null"] == 47
    rows = _stored(engine, WB_ARE)
    assert len(rows) == 19
    assert all(float(row["value"]) > 0 for row in rows)
    assert not any(float(row["value"]) == 0.0 for row in rows)
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(economic_observations)
        ).scalar() == 19


# --- containment ------------------------------------------------------------


@respx.mock
def test_one_failing_series_does_not_sink_the_pass(engine, settings):
    _mock_worldbank("IRN", load_fixture_json("worldbank_cpi_irn.json"))
    respx.get(host="api.worldbank.org", path=WB_PATH.format(iso3="TUR")).mock(
        return_value=httpx.Response(503)
    )

    result = ingest_economic(engine, settings, ["WB_CPI_TUR", WB_IRN])

    assert result["series"]["WB_CPI_TUR"]["status"] == "error"
    assert result["series"][WB_IRN]["status"] == "ok"
    assert result["series"][WB_IRN]["inserted"] == 66
    assert len(result["errors"]) == 1
    assert "WB_CPI_TUR" in result["errors"][0]
    assert result["series_ingested"] == 1
    assert result["series_failed"] == 1
    # The failed series wrote nothing at all, not a partial series.
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(economic_series)
        ).scalar() == 1


def _register_provider(engine, code: str, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            data_providers.insert().values(
                code=code, name=name, base_url="",
                category="global_macro", priority=10, enabled=True,
                consecutive_failures=0,
            )
        )


def _provider_row(engine, code: str) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                select(data_providers).where(data_providers.c.code == code)
            ).one()._mapping
        )


@respx.mock
def test_a_failure_is_recorded_against_the_provider_row(engine, settings):
    _register_provider(engine, "worldbank", "World Bank")
    respx.get(host="api.worldbank.org").mock(return_value=httpx.Response(503))

    with pytest.raises(EconomicIngestFailed):
        ingest_economic(engine, settings, [WB_IRN])

    row = _provider_row(engine, "worldbank")
    assert row["consecutive_failures"] == 1
    assert "WB_CPI_IRN" in row["last_error"]


@respx.mock
def test_provider_health_is_the_whole_pass_not_the_last_series(engine, settings):
    """One data_providers row backs seven series, so health must be aggregated.

    Recording it per series let the seventh overwrite the first six:
    record_success zeroes consecutive_failures and NULLs last_error, so a
    provider that answered once in seven attempts ended the pass looking
    untouched — and /internal/providers/health showed a healthy provider that
    had just failed six times.
    """
    _register_provider(engine, "worldbank", "World Bank")
    failing = [f"WB_CPI_{iso3}" for iso3 in ("IRN", "TUR", "SAU", "PAK", "EGY", "RUS")]
    # The UAE route is registered FIRST so it wins the match; everything else
    # this provider is asked for gets the 503. The one that works is the LAST
    # series of the pass, which is what used to erase the other six.
    _mock_worldbank("ARE", load_fixture_json("worldbank_cpi_are.json"))
    respx.get(host="api.worldbank.org").mock(return_value=httpx.Response(503))

    result = ingest_economic(engine, settings, failing + [WB_ARE])

    assert result["series_failed"] == 6
    assert result["series_ingested"] == 1
    row = _provider_row(engine, "worldbank")
    assert row["consecutive_failures"] == 1          # the pass failed, once
    assert row["last_error"] is not None
    assert "6 of 7 series failed" in row["last_error"]
    for code in failing:
        assert code in row["last_error"]
    # The report says the same thing the provider row says.
    assert result["providers"]["worldbank"] == {
        "attempted": 7, "failed": 6, "healthy": False
    }


@respx.mock
def test_a_clean_pass_records_one_success_per_provider(engine, settings):
    _register_provider(engine, "worldbank", "World Bank")
    with engine.begin() as conn:
        conn.execute(
            data_providers.update()
            .where(data_providers.c.code == "worldbank")
            .values(consecutive_failures=3, last_error="an earlier outage")
        )
    _mock_worldbank("IRN", load_fixture_json("worldbank_cpi_irn.json"))

    result = ingest_economic(engine, settings, [WB_IRN])

    row = _provider_row(engine, "worldbank")
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is None
    assert row["last_success_at"] is not None
    assert result["providers"]["worldbank"] == {
        "attempted": 1, "failed": 0, "healthy": True
    }


@respx.mock
def test_one_providers_outage_does_not_touch_the_other_provider(engine, settings):
    """worldbank and imf_weo are separate rows and separate verdicts."""
    _register_provider(engine, "worldbank", "World Bank")
    _register_provider(engine, "imf_weo", "IMF DataMapper")
    respx.get(host="api.worldbank.org").mock(return_value=httpx.Response(503))
    respx.get(IMF_URL).mock(
        return_value=httpx.Response(200, json=load_fixture_json("imf_pcpipch.json"))
    )

    result = ingest_economic(engine, settings, [WB_IRN, IMF_IRN])

    assert _provider_row(engine, "worldbank")["consecutive_failures"] == 1
    assert _provider_row(engine, "imf_weo")["consecutive_failures"] == 0
    assert result["providers"]["imf_weo"]["healthy"] is True
    assert result["providers"]["worldbank"]["healthy"] is False


@respx.mock
def test_a_pass_where_every_series_fails_is_signalled_as_a_failure(engine, settings):
    """A pass that ingested nothing must not be readable as a success.

    The Go scheduler records job success from the HTTP status, so returning 200
    with an errors list here wrote "economic: ok" into the job history while
    every provider was down — a total outage behind a green run.
    """
    respx.get(host="api.worldbank.org").mock(return_value=httpx.Response(503))

    with pytest.raises(EconomicIngestFailed) as excinfo:
        ingest_economic(engine, settings, [WB_IRN, "WB_CPI_TUR"])

    # The report survives: the failure is signalled, not summarised away.
    report = excinfo.value.report
    assert report["series_failed"] == 2
    assert report["series_ingested"] == 0
    assert len(report["errors"]) == 2
    assert set(report["series"]) == {WB_IRN, "WB_CPI_TUR"}
    assert "all 2 series failed" in str(excinfo.value)


@respx.mock
def test_a_partial_failure_is_still_a_successful_pass(engine, settings):
    """One provider down while the other ingests is not a failed job."""
    _mock_worldbank("IRN", load_fixture_json("worldbank_cpi_irn.json"))
    respx.get(host="api.worldbank.org", path=WB_PATH.format(iso3="TUR")).mock(
        return_value=httpx.Response(503)
    )

    result = ingest_economic(engine, settings, [WB_IRN, "WB_CPI_TUR"])

    assert result["series_ingested"] == 1
    assert result["series_failed"] == 1
    assert len(result["errors"]) == 1


def test_a_pass_with_nothing_to_attempt_is_not_a_failure(engine, settings, monkeypatch):
    """Every named series disabled: nothing was tried, so nothing broke."""
    import dataclasses

    from app.economic import catalog

    disabled = dict(catalog._BY_CODE)
    disabled[WB_IRN] = dataclasses.replace(catalog.spec_for_code(WB_IRN), enabled=False)
    monkeypatch.setattr(catalog, "_BY_CODE", disabled)

    result = ingest_economic(engine, settings, [WB_IRN])

    assert result["series"][WB_IRN]["status"] == "disabled"
    assert result["series_failed"] == 0
    assert result["providers"] == {}


def test_an_unknown_code_is_refused_not_silently_dropped(engine, settings):
    with pytest.raises(ValueError, match="not in the economic catalog"):
        ingest_economic(engine, settings, ["WB_CPI_IRN", "WB_CPI_ATLANTIS"])


def test_a_price_provider_cannot_serve_an_economic_series(engine, settings, monkeypatch):
    from app.providers.yahoo import YahooProvider

    monkeypatch.setattr(
        registry, "build_provider", lambda code, s: YahooProvider(courtesy_delay=0.0)
    )
    with pytest.raises(EconomicIngestFailed) as excinfo:
        ingest_economic(engine, settings, [WB_IRN])
    report = excinfo.value.report
    assert report["series"][WB_IRN]["status"] == "error"
    assert "does not serve economic series" in report["errors"][0]


# --- the endpoint -----------------------------------------------------------


class _StubProvider(IMFWeoProvider):
    """Two real IMF values, no network. Subclassed so the job's provider-type
    check sees the same class it would in production."""

    def fetch_detail(self, spec, now=None) -> SeriesFetch:
        return SeriesFetch(
            points=[
                SeriesPoint(
                    provider_code="imf_weo", code=spec.code,
                    ref_period_start=date(2025, 1, 1), ref_period_end=date(2025, 12, 31),
                    ref_period_label="2025", value=50.9,
                ),
                SeriesPoint(
                    provider_code="imf_weo", code=spec.code,
                    ref_period_start=date(2026, 1, 1), ref_period_end=date(2026, 12, 31),
                    ref_period_label="2026", value=68.9, is_projection=True,
                ),
            ],
            skipped_null=0,
            meta={"countries_returned": 228},
        )


def test_endpoint_requires_the_internal_token(client):
    assert client.post("/internal/economic/ingest").status_code == 401


def test_endpoint_ingests_and_reports_per_series_counts(client, engine, monkeypatch):
    monkeypatch.setattr(
        registry, "build_provider", lambda code, s: _StubProvider(courtesy_delay=0.0)
    )
    resp = client.post(
        "/internal/economic/ingest", json={"codes": [IMF_IRN]}, headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["series"][IMF_IRN]["inserted"] == 2
    assert body["errors"] == []

    rows = _stored(engine, IMF_IRN)
    assert [row["ref_period_label"] for row in rows] == ["2025", "2026"]
    assert [bool(row["is_projection"]) for row in rows] == [False, True]


def test_endpoint_answers_502_when_the_whole_pass_failed(client, monkeypatch):
    """Non-2xx is how the Go scheduler learns a job failed (internalclient
    turns any non-2xx into an APIError), so a pass that ingested nothing must
    not answer 200 — while the body still carries the per-series report."""

    def _explode(code, s):
        raise RuntimeError("the provider is down")

    monkeypatch.setattr(registry, "build_provider", _explode)

    resp = client.post(
        "/internal/economic/ingest", json={"codes": [IMF_IRN]}, headers=AUTH
    )

    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["code"] == "upstream_failed"
    assert body["series_ingested"] == 0
    assert body["series_failed"] == 1
    assert body["series"][IMF_IRN]["status"] == "error"
    assert "the provider is down" in body["errors"][0]


def test_endpoint_refuses_an_unknown_code_with_400(client):
    resp = client.post(
        "/internal/economic/ingest", json={"codes": ["WB_CPI_ATLANTIS"]}, headers=AUTH
    )
    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "bad_request"
    assert "WB_CPI_ATLANTIS" in error["message"]


def test_endpoint_with_no_body_means_the_whole_catalog(client, monkeypatch):
    monkeypatch.setattr(
        registry, "build_provider", lambda code, s: _StubProvider(courtesy_delay=0.0)
    )
    resp = client.post("/internal/economic/ingest", headers=AUTH)
    assert resp.status_code == 200
    from app.economic.catalog import CATALOG

    assert set(resp.json()["series"]) == {spec.code for spec in CATALOG}
