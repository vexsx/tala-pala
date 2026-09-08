"""Provider parsing against recorded payloads. No test here touches a network.

Fixture provenance, all fetched live on 2026-09-08:

* ``worldbank_cpi_irn.json`` and ``worldbank_cpi_are.json`` — complete,
  unedited responses of
  ``api.worldbank.org/v2/country/{IRN,ARE}/indicator/FP.CPI.TOTL
  ?format=json&per_page=200``.  The UAE one is here precisely because 47 of
  its 66 annual rows have ``"value": null``.
* ``imf_pcpipch.json`` — the real
  ``imf.org/external/datamapper/api/v1/PCPIPCH/IRN`` response reduced from 228
  economies to three, each economy's year map kept verbatim.  The three are
  IRN (the series under test), TUR (a second catalog country) and SDN — which
  is the FIRST key in the real response, and therefore what an adapter that
  trusted the ignored country path segment would hand back as Iran's inflation.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import httpx
import pytest
import respx

from app.economic.catalog import spec_for_code
from app.providers import imf_weo, worldbank
from app.providers.base import ProviderError

from .conftest import load_fixture_json

# Pinned so the projection boundary is a property of the test, not of the day
# it runs on. This is the date every fixture was recorded.
NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)

WB_IRN = "WB_CPI_IRN"
WB_ARE = "WB_CPI_ARE"
IMF_IRN = "IMF_PCPIPCH_IRN"


def _wb_provider(settings):
    return worldbank.WorldBankProvider(
        timeout=settings.http_timeout_seconds,
        courtesy_delay=settings.provider_courtesy_delay,
        backoff_base=settings.provider_backoff_base,
    )


def _imf_provider(settings):
    return imf_weo.IMFWeoProvider(
        timeout=settings.http_timeout_seconds,
        courtesy_delay=settings.provider_courtesy_delay,
        backoff_base=settings.provider_backoff_base,
    )


# --- World Bank -------------------------------------------------------------


def test_worldbank_parses_the_two_element_envelope():
    payload = load_fixture_json("worldbank_cpi_irn.json")
    fetch = worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)

    assert len(fetch.points) == 66          # 1960..2025, none missing for Iran
    assert fetch.skipped_null == 0
    # Metadata comes from the FIRST element; rows from the second.
    assert fetch.meta["last_updated"] == "2026-07-13"
    assert fetch.meta["total"] == 66
    assert fetch.meta["pages"] == 1

    by_label = {point.ref_period_label: point for point in fetch.points}
    # The base the catalog claims is the base the data shows.
    assert by_label["2010"].value == pytest.approx(100.0)
    assert by_label["2025"].value == pytest.approx(4030.3356899712)

    latest = by_label["2025"]
    assert latest.code == WB_IRN
    assert latest.provider_code == "worldbank"
    assert latest.ref_period_start == date(2025, 1, 1)
    assert latest.ref_period_end == date(2025, 12, 31)
    assert latest.is_projection is False
    assert latest.is_nowcast is False
    # A dataset-wide "lastupdated" is not this observation's publication date.
    assert latest.published_at is None
    assert latest.raw_payload["lastupdated"] == "2026-07-13"


def test_worldbank_points_come_back_oldest_first():
    payload = load_fixture_json("worldbank_cpi_irn.json")
    fetch = worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)
    starts = [point.ref_period_start for point in fetch.points]
    assert starts == sorted(starts)
    assert starts[0] == date(1960, 1, 1)


def test_worldbank_skips_null_values_and_never_zero_fills():
    """47 of the UAE's 66 years have no value. A CPI of 0 would be a lie."""
    payload = load_fixture_json("worldbank_cpi_are.json")
    fetch = worldbank.parse_series(payload, spec_for_code(WB_ARE), now=NOW)

    assert fetch.skipped_null == 47
    assert len(fetch.points) == 19
    assert all(point.value > 0 for point in fetch.points)
    assert 0.0 not in [point.value for point in fetch.points]
    # The gap years are absent, not present-and-zero.
    labels = {point.ref_period_label for point in fetch.points}
    assert "2005" not in labels
    assert "2025" in labels


def test_worldbank_reports_the_apis_own_error_envelope():
    """A one-element list with 'message' is an error, not the envelope."""
    payload = [
        {"message": [{"id": "120", "key": "Invalid value",
                      "value": "The provided parameter value is not valid"}]}
    ]
    with pytest.raises(ProviderError, match="The provided parameter value is not valid"):
        worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)


def test_worldbank_refuses_a_payload_for_another_country():
    """A number that cannot state its provenance does not ship."""
    payload = load_fixture_json("worldbank_cpi_are.json")
    with pytest.raises(ProviderError, match="refusing to store another country"):
        worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)


def test_worldbank_refuses_a_payload_for_another_indicator():
    payload = load_fixture_json("worldbank_cpi_irn.json")
    payload[1][0]["indicator"] = {"id": "NY.GDP.MKTP.CD", "value": "GDP"}
    with pytest.raises(ProviderError, match="refusing to store another series"):
        worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)


def test_worldbank_verifies_the_base_period_against_the_payload():
    """The catalog's '2010=100' is a claim; the payload states the same thing in
    every row ("Consumer price index (2010 = 100)") and it is checked, not
    assumed."""
    payload = load_fixture_json("worldbank_cpi_irn.json")
    assert payload[1][0]["indicator"]["value"] == "Consumer price index (2010 = 100)"

    fetch = worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)

    # Reported as the base the PAYLOAD stated, not the catalog's echoed back.
    assert fetch.meta["base_period"] == "2010=100"
    assert fetch.meta["base_period"] == spec_for_code(WB_IRN).base_period


def test_worldbank_refuses_a_rebased_series_rather_than_relabelling_it():
    """A World Bank rebase restates every level against a new year.

    Storing 2021-based levels under the catalog's '2010=100' would leave no
    as-of read able to tell the two bases apart — the series would silently
    become two incomparable histories under one code. Adopting the new base
    automatically would be no better: it needs a human decision about
    splice_policy, and this adapter performs no splice.
    """
    payload = load_fixture_json("worldbank_cpi_irn.json")
    for row in payload[1]:
        row["indicator"]["value"] = "Consumer price index (2021 = 100)"

    with pytest.raises(ProviderError) as excinfo:
        worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)

    message = str(excinfo.value)
    assert "2021=100" in message      # what arrived
    assert "2010=100" in message      # what the catalog declares
    assert "splice_policy" in message


def test_worldbank_refuses_a_rebase_that_appears_partway_down_the_payload():
    """Half a payload on a new base is exactly as unstorable as all of it."""
    payload = load_fixture_json("worldbank_cpi_irn.json")
    payload[1][30]["indicator"]["value"] = "Consumer price index (2021 = 100)"
    with pytest.raises(ProviderError, match="rebase"):
        worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)


def test_worldbank_reads_the_base_out_of_several_spellings():
    """Only a changed BASE may stop the ingest; a reworded label must not."""
    assert worldbank.base_period_in_name("Consumer price index (2010 = 100)") == "2010=100"
    assert worldbank.base_period_in_name("Consumer price index, 2010=100") == "2010=100"
    assert worldbank.base_period_in_name("CPI (2014 - 2016 = 100)") == "2014-2016=100"
    # No base stated at all is not evidence of a rebase, so it parses to ''.
    assert worldbank.base_period_in_name("Consumer price index") == ""
    assert worldbank.base_period_in_name("") == ""


def test_worldbank_proceeds_but_warns_when_the_name_states_no_base(caplog):
    """An API that drops the base from a display string has not rebased, and
    refusing seven series over a reworded label would be worse than saying so."""
    payload = load_fixture_json("worldbank_cpi_irn.json")
    for row in payload[1]:
        row["indicator"]["value"] = "Consumer price index"

    with caplog.at_level("WARNING"):
        fetch = worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)

    assert len(fetch.points) == 66
    assert fetch.meta["base_period"] == ""      # never the catalog's value
    assert "could not be verified" in caplog.text
    # Said once for the series, not 66 times.
    assert caplog.text.count("could not be verified") == 1


def test_imf_series_are_not_base_checked_because_a_rate_has_no_base():
    """A percent change has no base period, and inventing one to check would be
    a claim about the data rather than a check on it."""
    assert spec_for_code(IMF_IRN).base_period == ""
    fetch = imf_weo.parse_series(
        load_fixture_json("imf_pcpipch.json"), spec_for_code(IMF_IRN), now=NOW
    )
    assert "base_period" not in fetch.meta


def test_worldbank_refuses_a_payload_that_is_not_the_envelope():
    with pytest.raises(ProviderError, match="expected the .metadata, rows. envelope"):
        worldbank.parse_series({"rows": []}, spec_for_code(WB_IRN), now=NOW)


def test_worldbank_refuses_a_series_with_no_rows():
    payload = [{"page": 1, "pages": 1, "per_page": 200, "total": 0}, None]
    with pytest.raises(ProviderError, match="no rows"):
        worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)


def test_worldbank_refuses_a_series_that_is_entirely_null():
    payload = load_fixture_json("worldbank_cpi_irn.json")
    for row in payload[1]:
        row["value"] = None
    with pytest.raises(ProviderError, match="no usable observations"):
        worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)


def test_worldbank_flags_a_year_that_has_not_finished():
    """The World Bank has never returned the running year — if it does, that
    figure is not a completed annual measurement and must not read as one."""
    payload = load_fixture_json("worldbank_cpi_irn.json")
    payload[1].insert(0, dict(payload[1][0], date="2026", value=5000.0))
    fetch = worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)
    flagged = {point.ref_period_label for point in fetch.points if point.is_projection}
    assert flagged == {"2026"}


def test_worldbank_skips_a_non_annual_period_rather_than_guessing():
    payload = load_fixture_json("worldbank_cpi_irn.json")
    payload[1].insert(0, dict(payload[1][0], date="2025M07", value=4100.0))
    fetch = worldbank.parse_series(payload, spec_for_code(WB_IRN), now=NOW)
    assert all(point.ref_period_label != "2025M07" for point in fetch.points)
    assert len(fetch.points) == 66


@respx.mock
def test_worldbank_fetch_requests_the_documented_endpoint(settings):
    route = respx.get(
        host="api.worldbank.org", path="/v2/country/IRN/indicator/FP.CPI.TOTL"
    ).mock(return_value=httpx.Response(200, json=load_fixture_json("worldbank_cpi_irn.json")))

    fetch = _wb_provider(settings).fetch_detail(spec_for_code(WB_IRN), now=NOW)

    assert route.called
    request = route.calls[0].request
    assert request.url.params["format"] == "json"
    assert request.url.params["per_page"] == "200"
    assert len(fetch.points) == 66


@respx.mock
def test_worldbank_http_failure_becomes_a_provider_error(settings):
    respx.get(host="api.worldbank.org").mock(return_value=httpx.Response(503))
    with pytest.raises(ProviderError):
        _wb_provider(settings).fetch_detail(spec_for_code(WB_IRN), now=NOW)


def test_worldbank_refuses_a_series_it_does_not_serve(settings):
    with pytest.raises(ProviderError, match="served by 'imf_weo'"):
        _wb_provider(settings).fetch_detail(spec_for_code(IMF_IRN), now=NOW)


def test_economic_providers_are_not_price_providers(settings):
    """Wiring one into the collect job fails loudly instead of silently."""
    for provider in (_wb_provider(settings), _imf_provider(settings)):
        with pytest.raises(ProviderError, match="not a price provider"):
            provider.fetch()


# --- IMF DataMapper ---------------------------------------------------------


def test_imf_selects_the_requested_country_not_the_first_one():
    """The endpoint ignores its own country path segment and returns every
    economy; SDN is the first key. Taking it would have published Sudan's
    inflation as Iran's, silently and forever."""
    payload = load_fixture_json("imf_pcpipch.json")
    assert next(iter(payload["values"]["PCPIPCH"])) == "SDN"

    fetch = imf_weo.parse_series(payload, spec_for_code(IMF_IRN), now=NOW)

    by_label = {point.ref_period_label: point.value for point in fetch.points}
    assert by_label["1980"] == pytest.approx(20.6)     # Iran
    assert by_label["1980"] != pytest.approx(26.5)     # Sudan, the first key
    assert by_label["2025"] == pytest.approx(50.9)
    assert fetch.meta["countries_returned"] == 3


def test_imf_flags_every_unfinished_year_as_a_projection():
    """The series runs to 2031 with nothing in the payload separating forecast
    from history. The current year is not over, so it is a forecast too."""
    payload = load_fixture_json("imf_pcpipch.json")
    fetch = imf_weo.parse_series(payload, spec_for_code(IMF_IRN), now=NOW)

    projections = sorted(
        point.ref_period_label for point in fetch.points if point.is_projection
    )
    observations = sorted(
        point.ref_period_label for point in fetch.points if not point.is_projection
    )
    assert projections == ["2026", "2027", "2028", "2029", "2030", "2031"]
    assert observations[-1] == "2025"
    assert "2026" not in observations
    # The two sets partition the series: every point is one or the other.
    assert len(projections) + len(observations) == len(fetch.points)
    assert fetch.meta["projection_from"] == 2026


def test_imf_projection_boundary_moves_with_the_clock():
    payload = load_fixture_json("imf_pcpipch.json")
    next_year = imf_weo.parse_series(
        payload, spec_for_code(IMF_IRN),
        now=datetime(2027, 1, 1, tzinfo=timezone.utc),
    )
    flagged = {point.ref_period_label for point in next_year.points if point.is_projection}
    assert flagged == {"2027", "2028", "2029", "2030", "2031"}


def test_imf_refuses_a_payload_without_the_requested_country():
    """SAU is a catalog country the trimmed payload does not carry."""
    payload = load_fixture_json("imf_pcpipch.json")
    with pytest.raises(ProviderError, match="carries no data for SAU"):
        imf_weo.parse_series(payload, spec_for_code("IMF_PCPIPCH_SAU"), now=NOW)


def test_imf_refuses_a_payload_without_the_requested_indicator():
    payload = {"values": {"NGDP_RPCH": {"IRN": {"2025": 1.0}}}}
    with pytest.raises(ProviderError, match="no 'PCPIPCH' indicator"):
        imf_weo.parse_series(payload, spec_for_code(IMF_IRN), now=NOW)


def test_imf_refuses_a_payload_with_no_values_object():
    with pytest.raises(ProviderError, match="no 'values' object"):
        imf_weo.parse_series({"api": {"version": "1"}}, spec_for_code(IMF_IRN), now=NOW)


def test_imf_skips_a_null_year_rather_than_storing_zero():
    """The recorded payload has no nulls; this copy is edited to introduce one,
    because a zero inflation print and a missing one are different facts."""
    payload = load_fixture_json("imf_pcpipch.json")
    payload["values"]["PCPIPCH"]["IRN"]["2024"] = None

    fetch = imf_weo.parse_series(payload, spec_for_code(IMF_IRN), now=NOW)

    assert fetch.skipped_null == 1
    labels = {point.ref_period_label for point in fetch.points}
    assert "2024" not in labels
    assert "2023" in labels


@respx.mock
def test_imf_fetch_requests_the_documented_endpoint(settings):
    route = respx.get(
        "https://www.imf.org/external/datamapper/api/v1/PCPIPCH/IRN"
    ).mock(return_value=httpx.Response(200, json=load_fixture_json("imf_pcpipch.json")))

    fetch = _imf_provider(settings).fetch_detail(spec_for_code(IMF_IRN), now=NOW)

    assert route.called
    assert len(fetch.points) == len(
        load_fixture_json("imf_pcpipch.json")["values"]["PCPIPCH"]["IRN"]
    )


@respx.mock
def test_imf_http_failure_becomes_a_provider_error(settings):
    respx.get(host="www.imf.org").mock(return_value=httpx.Response(500))
    with pytest.raises(ProviderError):
        _imf_provider(settings).fetch_detail(spec_for_code(IMF_IRN), now=NOW)


# --- the catalog itself -----------------------------------------------------


def test_catalog_covers_the_seven_countries_twice_and_says_what_it_is():
    from app.economic.catalog import CATALOG

    codes = {spec.code for spec in CATALOG}
    countries = ("IRN", "TUR", "SAU", "ARE", "PAK", "EGY", "RUS")
    assert codes == (
        {f"WB_CPI_{iso3}" for iso3 in countries}
        | {f"IMF_PCPIPCH_{iso3}" for iso3 in countries}
    )
    for spec in CATALOG:
        assert spec.frequency == "A"
        assert spec.calendar == "gregorian"
        assert spec.notes.strip(), f"{spec.code} ships without a note"
        assert spec.provider_code in ("worldbank", "imf_weo")

    world_bank = [spec for spec in CATALOG if spec.provider_code == "worldbank"]
    assert all(spec.measure == "index" for spec in world_bank)
    assert all(spec.base_period == "2010=100" for spec in world_bank)
    assert all(spec.quality_tier == "official_mirror" for spec in world_bank)

    imf = [spec for spec in CATALOG if spec.provider_code == "imf_weo"]
    assert all(spec.measure == "yoy_pct" for spec in imf)
    # Percent change has no base period, and inventing one would be a claim.
    assert all(spec.base_period == "" for spec in imf)


def test_imf_notes_record_what_the_api_will_not_tell_us():
    """These two caveats are the reason the series is 'estimate' tier. They
    travel with every number, so they are asserted, not merely written."""
    from app.economic.catalog import CATALOG

    for spec in [spec for spec in CATALOG if spec.provider_code == "imf_weo"]:
        assert "Article IV" in spec.notes
        assert "2018" in spec.notes
        assert "estimates start after" in spec.notes
        assert spec.quality_tier == "estimate"


def test_catalog_refuses_an_unknown_code():
    from app.economic.catalog import enabled_specs

    with pytest.raises(ValueError, match="not in the economic catalog"):
        enabled_specs(["WB_CPI_ATLANTIS"])
