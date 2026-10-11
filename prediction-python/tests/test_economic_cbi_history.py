"""The CBI time-series viewer's quarterly history, and the script that fetches it.

``tests/fixtures/cbi_tsdview_quarterly.json`` is 57 rows trimmed out of the
real 1,120-row answer of tsdview's ``value/graph`` (2026-10-11), unchanged
row for row: the four series and the currency-in-circulation category the
research capture also asked for, at eleven quarter ends — 1352-12 (the first
with values), 1370-12 (which the viewer dates 1229 although 1370 was a leap
year), 1375-12 (dated 1230), 1396-12, 1397-09, 1397-12, 1398-03, 1398-06,
1399-12 (base money already on the revised method there), 1400-03 and 1404-09
(M2 135,344.5 against the edition's 135,344.6) — plus two of the viewer's
empty rows.
"""
from __future__ import annotations

import copy
import gzip
import importlib.util
import json
import os
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.db import economic_observations, ensure_utc, source_documents
from app.economic.catalog import BASE_MONEY_CODE, spec_for_code
from app.economic.cbi import ingest_edition_file
from app.economic.cbi_history import (
    CATEGORY_IDS,
    THOUSAND_BILLION_RIAL_UNIT_ID,
    CbiHistoryError,
    ingest_cbi_history,
    parse_history,
)
from app.economic.store import ensure_series, point_in_time, record_observation

from .conftest import FIXTURES_DIR, TEST_TOKEN, load_fixture_json

AUTH = {"X-Internal-Token": TEST_TOKEN}
FIXTURE = os.path.join(FIXTURES_DIR, "cbi_tsdview_quarterly.json")
NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
# Reads are pinned to a cutoff after every ingest here, never to the wall
# clock: the ingests are stamped with NOW/LATER, which can be ahead of it.
FAR = datetime(2030, 1, 1, tzinfo=timezone.utc)

WRITTEN = ["1352-12", "1370-12", "1375-12", "1396-12", "1397-09", "1398-03"]
OVERLAP = ["1397-12", "1398-06", "1399-12", "1400-03", "1404-09"]
CURRENCY_IN_CIRCULATION = "3505fc25-5fe8-8f09-e063-4ceaa8c0b336"


def _payload():
    return copy.deepcopy(load_fixture_json("cbi_tsdview_quarterly.json"))


def _write(tmp_path, payload, name="tsdview.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _rows_for(payload, code, kh_date):
    return [r for r in payload if r["CATEGORYID"] == CATEGORY_IDS[code] and r["khDate"] == kh_date]


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar()


def _store_edition(engine, tmp_path, fixture, name, now=NOW):
    path = tmp_path / name
    with gzip.open(os.path.join(FIXTURES_DIR, fixture)) as handle:
        path.write_bytes(handle.read())
    return ingest_edition_file(engine, str(path), now=now)


# --- parsing --------------------------------------------------------------------


def test_the_payload_parses_into_the_four_series():
    points, shape = parse_history(_payload())
    assert sorted(points) == sorted(CATEGORY_IDS)
    for code, history in points.items():
        assert [p.ref_period_label for p in history] == sorted(WRITTEN + OVERLAP), code
    assert shape["gaps"] == 2
    # Asked for by the research capture, mapped to nothing here: counted, not stored.
    assert shape["other_categories"] == {CURRENCY_IN_CIRCULATION: 11}


def test_values_are_the_viewers_decimal_strings_exactly():
    points, _ = parse_history(_payload())
    m2 = {p.ref_period_label: p.value for p in points["CBI_LIQUIDITY"]}
    assert m2["1370-12"] == Decimal("28.6284")
    assert m2["1352-12"] == Decimal("0.518")
    assert m2["1404-09"] == Decimal("135344.5")
    assert all(isinstance(value, Decimal) for value in m2.values())


def test_the_period_is_the_month_and_the_viewers_day_is_not_checked():
    """The viewer stamps Esfand 29 in 1370, a leap year whose Esfand had 30
    days; the period is the month either way."""
    payload = _payload()
    assert _rows_for(payload, "CBI_LIQUIDITY", "13701229")
    assert _rows_for(payload, "CBI_LIQUIDITY", "13751230")
    points, _ = parse_history(payload)
    assert "1370-12" in {p.ref_period_label for p in points["CBI_LIQUIDITY"]}


def test_another_unit_is_refused_never_rescaled():
    payload = _payload()
    _rows_for(payload, "CBI_BASE_MONEY", "13961229")[0]["unitId"] = "BILLION_RIAL"
    with pytest.raises(CbiHistoryError, match="not the thousand-billion-rial unit"):
        parse_history(payload)
    assert THOUSAND_BILLION_RIAL_UNIT_ID.startswith("2C129749")


def test_another_frequency_is_refused():
    payload = _payload()
    _rows_for(payload, "CBI_MONEY_M1", "13961229")[0]["subFrequencyId"] = "ANNUAL"
    with pytest.raises(CbiHistoryError, match="is not quarterly"):
        parse_history(payload)


def test_a_month_that_ends_no_quarter_is_refused():
    payload = _payload()
    _rows_for(payload, "CBI_LIQUIDITY", "13961229")[0]["khDate"] = "13961130"
    with pytest.raises(CbiHistoryError, match="not the end of a quarter"):
        parse_history(payload)


def test_a_date_that_contradicts_its_year_is_refused():
    payload = _payload()
    _rows_for(payload, "CBI_LIQUIDITY", "13961229")[0]["datePerYear"] = 1395
    with pytest.raises(CbiHistoryError, match="says year 1395"):
        parse_history(payload)


@pytest.mark.parametrize("amount", ["-1", "0", "n/a", "NaN"])
def test_a_value_that_is_not_a_positive_number_is_refused(amount):
    payload = _payload()
    _rows_for(payload, "CBI_QUASI_MONEY", "13961229")[0]["amount"] = amount
    with pytest.raises(CbiHistoryError, match="has the value"):
        parse_history(payload)


def test_a_missing_series_refuses_the_whole_payload():
    payload = [r for r in _payload() if r["CATEGORYID"] != CATEGORY_IDS["CBI_MONEY_M1"]]
    with pytest.raises(CbiHistoryError, match="no values for CBI_MONEY_M1"):
        parse_history(payload)


def test_two_different_values_for_one_quarter_are_refused():
    payload = _payload()
    twin = copy.deepcopy(_rows_for(payload, "CBI_LIQUIDITY", "13961229")[0])
    twin["amount"] = "15300.0"
    payload.append(twin)
    with pytest.raises(CbiHistoryError, match="appears twice"):
        parse_history(payload)


def test_a_payload_that_is_not_the_list_is_refused():
    with pytest.raises(CbiHistoryError, match="list of value rows"):
        parse_history({"error": "unauthorised"})
    with pytest.raises(CbiHistoryError, match="not an object"):
        parse_history(["row"])


# --- ingest ---------------------------------------------------------------------


def test_only_the_quarters_no_edition_can_carry_are_written(engine):
    report = ingest_cbi_history(engine, FIXTURE, now=NOW)
    assert report["inserted"] == 4 * len(WRITTEN)
    for code in CATEGORY_IDS:
        counts = report["series"][code]
        assert counts["written_months"] == len(WRITTEN)
        assert counts["overlapping_months_not_written"] == len(OVERLAP)
        stored = point_in_time(engine, code, as_of=FAR)
        assert [o.ref_period_label for o in stored.observations] == WRITTEN
    m2 = point_in_time(engine, "CBI_LIQUIDITY", as_of=FAR).observations
    assert m2[0].ref_period_start == date(1974, 2, 20)       # Esfand 1352
    assert m2[0].value == pytest.approx(0.518)


def test_history_rows_say_when_this_system_got_them_and_nothing_more(engine):
    ingest_cbi_history(engine, FIXTURE, now=NOW)
    for code in CATEGORY_IDS:
        for item in point_in_time(engine, code, as_of=FAR).observations:
            assert item.available_at == NOW
            assert item.published_at is None
            assert item.available_at_approx is False
            assert item.method == ("old" if code == BASE_MONEY_CODE else None)


def test_the_payload_is_archived_under_its_own_label(engine):
    report = ingest_cbi_history(engine, FIXTURE, now=NOW)
    with engine.connect() as conn:
        documents = [dict(r._mapping) for r in conn.execute(select(source_documents))]
    assert len(documents) == 1
    document = documents[0]
    assert document["provider_code"] == "cbi"
    assert document["edition_label"] == "tsdview"
    assert document["media_type"] == "application/json"
    assert document["document_saved_at"] is None
    assert document["url"] == "https://tsdview.cis.cbi.ir/backend/api/value/graph"
    assert document["content_sha256"] == report["sha256"]
    assert ensure_utc(document["fetched_at"]) == NOW


def test_re_posting_the_payload_writes_nothing(engine):
    ingest_cbi_history(engine, FIXTURE, now=NOW)
    again = ingest_cbi_history(engine, FIXTURE, now=LATER)
    assert again["inserted"] == 0 and again["unchanged"] == 4 * len(WRITTEN)
    assert again["document"]["status"] == "existing"
    assert _count(engine, economic_observations) == 4 * len(WRITTEN)


def test_a_restated_history_value_is_a_new_vintage(engine, tmp_path):
    ingest_cbi_history(engine, FIXTURE, now=NOW)
    payload = _payload()
    _rows_for(payload, "CBI_LIQUIDITY", "13961229")[0]["amount"] = "15299.9"
    report = ingest_cbi_history(engine, _write(tmp_path, payload), now=LATER)
    assert report["revised"] == 1
    m2 = {o.ref_period_label: o for o in point_in_time(engine, "CBI_LIQUIDITY", as_of=FAR).observations}
    assert m2["1396-12"].vintage == 2 and m2["1396-12"].value == pytest.approx(15299.9)


def test_the_overlap_agrees_with_the_stored_editions(engine, tmp_path):
    """Esfand 1399 is in the Esfand 1400 edition: M2, M1 and quasi-money agree
    to the printed decimal; base money does not, and is reported, because the
    viewer already carries the revised method there (4,506.7 against 4,588.9)."""
    _store_edition(engine, tmp_path, "cbi_1400-12_pol1400-12.xls.gz", "pol1400,12.xls")
    report = ingest_cbi_history(engine, FIXTURE, now=LATER)
    overlap = report["overlap_with_editions"]
    for code in ("CBI_LIQUIDITY", "CBI_MONEY_M1", "CBI_QUASI_MONEY"):
        assert overlap[code] == {"compared": 1, "agreed": 1, "disagreements": []}
    assert overlap[BASE_MONEY_CODE]["disagreements"] == [{
        "period": "1399-12", "tsdview": "4506.7", "stored": "4588.9", "stored_method": "old",
    }]
    # And the edition's months were left exactly as they were.
    m2 = {o.ref_period_label: o for o in point_in_time(engine, "CBI_LIQUIDITY", as_of=FAR).observations}
    assert m2["1399-12"].vintage == 1 and m2["1399-12"].value == pytest.approx(34761.7)


def test_a_viewer_that_disagrees_with_a_stored_edition_is_refused_whole(engine, tmp_path):
    _store_edition(engine, tmp_path, "cbi_1400-12_pol1400-12.xls.gz", "pol1400,12.xls")
    before = _count(engine, economic_observations)
    payload = _payload()
    _rows_for(payload, "CBI_MONEY_M1", "13991230")[0]["amount"] = "6909600"   # x1000: another unit
    with pytest.raises(CbiHistoryError, match="CBI_MONEY_M1: the viewer says 6909600 at 1399-12"):
        ingest_cbi_history(engine, _write(tmp_path, payload), now=LATER)
    assert _count(engine, economic_observations) == before
    assert _count(engine, source_documents) == 1


def test_the_last_printed_digit_is_tolerated_and_no_more(engine, tmp_path):
    """Measured: M2 at Azar 1404 is 135,344.5 in the viewer and 135,344.6 in
    the Azar 1404 edition."""
    series_id = ensure_series(engine, spec_for_code("CBI_LIQUIDITY"))
    record_observation(engine, series_id, ref_period_start=date(2025, 11, 22),
                       ref_period_end=date(2025, 12, 21), ref_period_label="1404-09",
                       value=Decimal("135344.6"), available_at=NOW)
    report = ingest_cbi_history(engine, FIXTURE, now=LATER, dry_run=True)
    assert report["overlap_with_editions"]["CBI_LIQUIDITY"]["agreed"] == 1

    payload = _payload()
    _rows_for(payload, "CBI_LIQUIDITY", "14040930")[0]["amount"] = "135344.4"
    with pytest.raises(CbiHistoryError, match="135344.4 at 1404-09"):
        ingest_cbi_history(engine, _write(tmp_path, payload), now=LATER)


def test_the_history_first_does_not_disturb_the_backfill_after_it(engine, tmp_path):
    """The two sources never write the same month, so the order they run in
    does not matter: a backfill after the history still reconstructs its
    availability from the workbook, not from the history's ingest time."""
    first = ingest_cbi_history(engine, FIXTURE, now=LATER)
    path = tmp_path / "pol1400,12.xls"
    with gzip.open(os.path.join(FIXTURES_DIR, "cbi_1400-12_pol1400-12.xls.gz")) as handle:
        path.write_bytes(handle.read())
    report = ingest_edition_file(engine, str(path), backfill=True, now=LATER)
    assert report["available_at"] == "2022-08-23T07:55:46+00:00"
    assert report["available_at_approx"] is True and report["inserted"] == 12

    # But posted first, the history was checked against nothing, and its
    # report says so instead of showing an empty disagreement list as
    # agreement. Posting it again after the edition runs the check.
    assert first["overlap_unverified"] == ["CBI_LIQUIDITY", "CBI_MONEY_M1", "CBI_QUASI_MONEY"]
    assert "NOT checked against a printed edition" in first["overlap_note"]
    again = ingest_cbi_history(engine, FIXTURE, now=LATER)
    assert again["overlap_unverified"] == [] and "overlap_note" not in again
    assert again["overlap_with_editions"]["CBI_LIQUIDITY"]["agreed"] == 1
    assert again["inserted"] == 0


def test_a_dry_run_writes_nothing(engine):
    report = ingest_cbi_history(engine, FIXTURE, now=NOW, dry_run=True)
    assert report["dry_run"] is True and report["inserted"] == 4 * len(WRITTEN)
    assert _count(engine, economic_observations) == 0
    assert _count(engine, source_documents) == 0


def test_a_file_that_is_not_json_is_refused(engine, tmp_path):
    path = tmp_path / "tsdview.json"
    path.write_text("<html>blocked</html>", encoding="utf-8")
    with pytest.raises(CbiHistoryError, match="not the viewer's JSON answer"):
        ingest_cbi_history(engine, str(path), now=NOW)
    with pytest.raises(FileNotFoundError):
        ingest_cbi_history(engine, str(tmp_path / "absent.json"), now=NOW)


# --- the endpoint ---------------------------------------------------------------


def test_the_endpoint_ingests_and_is_idempotent(client, engine):
    first = client.post("/internal/economic/cbi-history", json={"path": FIXTURE}, headers=AUTH)
    assert first.status_code == 200
    assert first.json()["inserted"] == 4 * len(WRITTEN)
    second = client.post("/internal/economic/cbi-history", json={"path": FIXTURE}, headers=AUTH)
    assert second.status_code == 200 and second.json()["inserted"] == 0


def test_the_endpoint_refuses_a_bad_payload_with_400(client, engine, tmp_path):
    payload = _payload()
    _rows_for(payload, "CBI_LIQUIDITY", "13961229")[0]["unitId"] = "X"
    resp = client.post("/internal/economic/cbi-history",
                       json={"path": _write(tmp_path, payload)}, headers=AUTH)
    assert resp.status_code == 400
    assert "thousand-billion-rial" in resp.json()["error"]["message"]
    assert _count(engine, economic_observations) == 0
    missing = client.post("/internal/economic/cbi-history",
                          json={"path": str(tmp_path / "absent.json")}, headers=AUTH)
    assert missing.status_code == 400


# --- scripts/cbi_history_fetch.py ---------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FETCH_SCRIPT = os.path.join(REPO_ROOT, "scripts", "cbi_history_fetch.py")


@pytest.fixture(scope="module")
def script():
    if not os.path.isfile(FETCH_SCRIPT):
        pytest.skip("scripts/ is not mounted — run from a full checkout")
    spec = importlib.util.spec_from_file_location("cbi_history_fetch", FETCH_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["cbi_history_fetch"] = module
    spec.loader.exec_module(module)
    return module


def _fixture_bytes() -> bytes:
    with open(FIXTURE, "rb") as handle:
        return handle.read()


def test_the_script_asks_for_the_same_series_the_service_reads(script):
    from app.economic import cbi_history

    assert script.CATEGORIES == cbi_history.CATEGORY_IDS
    assert script.QUARTERLY == cbi_history.QUARTERLY_SUBFREQUENCY_ID
    body = script.request_body(datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert body == {
        "fromDate": "13500101",
        "toDateDate": "14061230",
        "categoryIds": list(cbi_history.CATEGORY_IDS.values()),
        "calender": "FA",
        "subFrequencyId": cbi_history.QUARTERLY_SUBFREQUENCY_ID,
    }
    assert script.current_jalali_year(datetime(2026, 3, 1, tzinfo=timezone.utc)) == 1404
    assert script.current_jalali_year(datetime(2026, 3, 21, tzinfo=timezone.utc)) == 1405
    assert script.USER_AGENT == "IranGoldPredictor/1.0 (+self-hosted analytics)"


def test_the_script_verifies_what_it_ships(script):
    summary = script.verify(_fixture_bytes())
    assert summary["rows"] == 57
    assert summary["values"] == {code: 11 for code in script.CATEGORIES}
    assert summary["newest"] == "14040930"
    for bad, reason in ((b"<html>error</html>", "HTML page"), (b"{}", "non-empty list"),
                        (b"not json", "not JSON")):
        with pytest.raises(SystemExit, match=reason):
            script.verify(bad)
    partial = [r for r in json.loads(_fixture_bytes())
               if r["CATEGORYID"] != script.CATEGORIES["CBI_BASE_MONEY"]]
    with pytest.raises(SystemExit, match="no values for CBI_BASE_MONEY"):
        script.verify(json.dumps(partial).encode())


def test_the_remote_command_is_built_from_a_name_the_script_made(script):
    raw = _fixture_bytes()
    name = script.payload_name(raw, datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert name.startswith("tsdview_quarterly_20261011_") and name.endswith(".json")
    command = script.remote_ingest_command(name)
    assert "http://prediction-service:8500/internal/economic/cbi-history" in command
    assert f"/opt/tala-pala/backups/cbi/{name}" in command
    assert json.dumps({"path": f"/tmp/{name}"}) in command
    assert '-e TOKEN="$TOKEN"' in command
    for hostile in ("x; rm -rf /", "../../etc/passwd", "tsdview_quarterly_20261011_$(id).json"):
        with pytest.raises(SystemExit, match="refusing the payload name"):
            script.remote_ingest_command(hostile)


def test_a_dry_run_from_a_saved_file_touches_neither_network_nor_server(script, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a dry run must not run ssh/scp or fetch")

    monkeypatch.setattr(script.subprocess, "run", forbidden)
    monkeypatch.setattr(script.urllib.request, "urlopen", forbidden)
    assert script.main(["--from-file", FIXTURE, "--dry-run", "--out-dir", str(tmp_path)]) == 0
    saved = list(tmp_path.glob("tsdview_quarterly_*.json"))
    assert len(saved) == 1 and saved[0].read_bytes() == _fixture_bytes()


def test_a_missing_out_dir_is_made_not_a_traceback(script, tmp_path, monkeypatch):
    """The first production run passed an --out-dir that did not exist yet and
    lost the fetched payload to a FileNotFoundError."""
    monkeypatch.setattr(script.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("a dry run must not run ssh/scp")))
    out = tmp_path / "not" / "yet"
    assert script.main(["--from-file", FIXTURE, "--dry-run", "--out-dir", str(out)]) == 0
    assert len(list(out.glob("tsdview_quarterly_*.json"))) == 1


def test_a_real_run_uploads_then_ingests_inside_the_compose_network(script, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(script.subprocess, "run", lambda argv, check: calls.append(argv))
    assert script.main(["--from-file", FIXTURE, "--out-dir", str(tmp_path),
                        "--host", "ubuntu@example"]) == 0
    assert [argv[0] for argv in calls] == ["ssh", "scp", "ssh"]
    assert "chmod 700" in calls[0][2]
    name = next(tmp_path.glob("tsdview_quarterly_*.json")).name
    assert calls[1][-1] == f"ubuntu@example:/opt/tala-pala/backups/cbi/{name}"
    assert calls[2] == ["ssh", "ubuntu@example", script.remote_ingest_command(name)]


def test_the_fetch_retries_transient_failures_and_not_refusals(script, monkeypatch):
    import urllib.error

    monkeypatch.setattr(script, "BACKOFF_SECONDS", 0.0)
    attempts = []

    class Answer:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.raw

    def flaky(request, timeout):
        attempts.append(request)
        assert request.get_header("User-agent") == script.USER_AGENT
        assert timeout >= 60
        if len(attempts) < 3:
            raise urllib.error.URLError("temporary failure in name resolution")
        return Answer(_fixture_bytes())

    monkeypatch.setattr(script.urllib.request, "urlopen", flaky)
    assert script.fetch(script.request_body()) == _fixture_bytes()
    assert len(attempts) == 3
    assert json.loads(attempts[0].data)["calender"] == "FA"

    attempts.clear()

    def refused(request, timeout):
        attempts.append(request)
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(script.urllib.request, "urlopen", refused)
    with pytest.raises(SystemExit, match="refused the request"):
        script.fetch(script.request_body())
    assert len(attempts) == 1
