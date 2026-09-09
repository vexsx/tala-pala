"""The SCI urban-CPI ingest: parsing, refusal, archiving, and idempotence.

The happy path runs against ``tests/fixtures/sci_ts_urban_fixture.xlsx`` — 29 KB
trimmed out of the real 1.2 MB workbook, with the real values, the real mixed
Persian/Arabic orthography and the real merged year cells. It is deliberately
NOT a mock: a mock encodes what this parser already assumes, and the three
hazards in :mod:`app.economic.sci` are all things the real file does and a
hand-written one would not.

The refusal paths run against synthetic sheets, because the point of each is a
workbook SCI has never published: one with a row missing, one on a different
base. Those are built as row grids in the shape ``openpyxl`` yields, which is
also the shape :func:`app.economic.sci.parse_workbook` takes.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone

import openpyxl
import pytest
from sqlalchemy import func, select

from app.db import (
    economic_observations,
    economic_series,
    ensure_utc,
    source_documents,
)
from app.economic.catalog import DOCUMENT_INGEST_PROVIDERS, sci_specs, spec_for_code
from app.economic.sci import (
    BASE_YEAR,
    JALALI_MONTHS,
    SHEET,
    TARGETS,
    SciParseError,
    ingest_sci_cpi,
    is_incomplete_month,
    jalali_month_span,
    jalali_to_gregorian,
    normalize_fa,
    parse_workbook,
    published_at_from_filename,
    read_workbook,
)
from app.economic.store import point_in_time, vintages_for_period
from app.features.engineering import gregorian_to_jalali

from .conftest import FIXTURES_DIR, TEST_TOKEN

AUTH = {"X-Internal-Token": TEST_TOKEN}

# The real workbook this parser was written against, and the name the fetcher
# hands over. Both are load-bearing: the sha256 is the archive's identity and
# the filename is the only place published_at can come from.
REAL_FILENAME = "ts_urban_140505-14050618165804.xlsx"
FIXTURE = f"{FIXTURES_DIR}/sci_ts_urban_fixture.xlsx"

CODES = ("SCI_CPI_URBAN", "SCI_CPI_HOUSING", "SCI_CPI_RENT", "SCI_CPI_VEHICLES")
OBSERVATIONS_PER_SERIES = 293       # Farvardin 1381 .. Mordad 1405

# Measured on the real workbook, 2026-09-09.
LATEST = {
    "SCI_CPI_URBAN": 683.59,
    "SCI_CPI_HOUSING": 440.98,
    "SCI_CPI_RENT": 437.39,
    "SCI_CPI_VEHICLES": 637.11,
}

# SCI's own orthography: ARABIC kaf (U+0643) and yeh (U+064A), which is what
# the labels in the sheet actually use.
ARABIC_LABELS = {
    "SCI_CPI_URBAN": "شاخص كل",
    "SCI_CPI_HOUSING": "مسكن",
    "SCI_CPI_RENT": "اجاره",
    "SCI_CPI_VEHICLES": "071 - خريد وسايل نقليه",
}
# The same four rows spelled with PERSIAN keheh (U+06A9) and yeh (U+06CC),
# which is how they render and how a human would retype them.
PERSIAN_LABELS = {
    "SCI_CPI_URBAN": "شاخص کل",
    "SCI_CPI_HOUSING": "مسکن",
    "SCI_CPI_RENT": "اجاره",
    "SCI_CPI_VEHICLES": "071 - خرید وسایل نقلیه",
}
# The utilities division. 'مسکن' is a substring of it (and it carries a ZWNJ in
# 'سوخت‌ها'), so a substring match would bind the housing series to this row.
UTILITIES_LABEL = "04 - مسکن ، آب ، برق ، گاز و سایر سوخت‌ها"

FIRST_INGEST = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)
SECOND_INGEST = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)


# --- synthetic sheets -------------------------------------------------------


def _grid(rows, years=(BASE_YEAR,)):
    """A sheet in the shape ``iter_rows(values_only=True)`` yields.

    Row 1 is decorative, row 2 carries each Jalali year ONCE at the start of
    its twelve-month block (openpyxl reports a merged range's value on its
    first cell and None on the rest, so this is literally what the parser
    sees), row 3 carries the month names, and the data rows follow.
    """
    year_row: list = [None]
    month_row: list = [None]
    for year in years:
        for index, month in enumerate(JALALI_MONTHS):
            year_row.append(year if index == 0 else None)
            month_row.append(month)
    grid = [[None] * len(month_row), year_row, month_row]
    for label, values in rows:
        grid.append([label] + list(values))
    return grid


def _all_four(labels=None, value=100.0, years=(BASE_YEAR,)):
    """Every target row present and exactly on base: the accepted shape."""
    labels = labels or ARABIC_LABELS
    values = [value] * (12 * len(years))
    return _grid([(labels[code], values) for code in CODES], years=years)


def _write_xlsx(path, grid, sheet_name=SHEET):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = sheet_name
    for row in grid:
        sheet.append(list(row))
    workbook.save(path)
    return str(path)


# --- Jalali <-> Gregorian ---------------------------------------------------


def test_jalali_inverse_agrees_with_the_repos_forward_conversion():
    """Zero mismatches over every month boundary 1381..1405.

    ``app.features.engineering.gregorian_to_jalali`` already owns the forward
    direction and feature engineering depends on it; a second, independently
    derived inverse that disagreed at one boundary would put an observation in
    the wrong month with nothing to catch it.
    """
    mismatches = []
    for jalali_year in range(1381, 1406):
        for jalali_month in range(1, 13):
            start, end = jalali_month_span(jalali_year, jalali_month)
            if gregorian_to_jalali(start.year, start.month, start.day) != (
                jalali_year, jalali_month, 1
            ):
                mismatches.append(("start", jalali_year, jalali_month, start))
            back = gregorian_to_jalali(end.year, end.month, end.day)
            if back[:2] != (jalali_year, jalali_month):
                mismatches.append(("end", jalali_year, jalali_month, end))
    assert mismatches == []


def test_jalali_month_spans_are_the_measured_ones():
    assert jalali_to_gregorian(1400, 1, 1) == date(2021, 3, 21)
    # Mordad 1405 — the latest month in the workbook this was written against.
    assert jalali_month_span(1405, 5) == (date(2026, 7, 23), date(2026, 8, 22))
    # Farvardin 1381 — the first.
    assert jalali_month_span(1381, 1) == (date(2002, 3, 21), date(2002, 4, 20))


def test_esfand_length_follows_the_leap_year_without_a_month_table():
    """The span ends one day before the next month starts, so a 30-day Esfand
    needs no special case. 1403 is a leap year; 1404 is not."""
    assert jalali_month_span(1403, 12) == (date(2025, 2, 19), date(2025, 3, 20))
    assert (jalali_month_span(1403, 12)[1] - jalali_month_span(1403, 12)[0]).days == 29
    assert (jalali_month_span(1404, 12)[1] - jalali_month_span(1404, 12)[0]).days == 28


# --- label normalisation ----------------------------------------------------


def test_the_catalog_keeps_scis_arabic_orthography_and_matching_normalises_it():
    """The specs advertise the label as SCI prints it (Arabic kaf/yeh) so it can
    be grepped against a downloaded file; the parser folds both sides."""
    indicators = {spec.code: spec.indicator for spec in sci_specs()}
    assert indicators == ARABIC_LABELS
    assert "ك" in indicators["SCI_CPI_URBAN"]        # ARABIC KAF, not keheh
    assert "ي" in indicators["SCI_CPI_VEHICLES"]     # ARABIC YEH, not U+06CC
    assert TARGETS == {PERSIAN_LABELS[code]: code for code in CODES}


def test_arabic_and_persian_spellings_of_the_same_label_both_match():
    for labels in (ARABIC_LABELS, PERSIAN_LABELS):
        series = parse_workbook(_all_four(labels=labels))
        assert sorted(series) == sorted(CODES)


def test_zwnj_and_padding_do_not_break_a_label():
    padded = dict(ARABIC_LABELS)
    padded["SCI_CPI_RENT"] = "  ‏اجاره‌  "   # RLM, ZWNJ, stray spaces
    assert sorted(parse_workbook(_all_four(labels=padded))) == sorted(CODES)
    assert normalize_fa("‏اجاره‌ ") == "اجاره"


def test_housing_binds_to_its_own_row_not_the_division_that_contains_it():
    """'مسکن' is a row AND a substring of the utilities division's label.

    The division is listed FIRST here, so a substring match would take its
    values and the real housing row would then be skipped as a duplicate.
    """
    months = [100.0] * 12
    division = [7.0] * 12
    grid = _grid(
        [(UTILITIES_LABEL, division)]
        + [(ARABIC_LABELS[code], months) for code in CODES]
    )
    series = parse_workbook(grid)
    assert [point.value for point in series["SCI_CPI_HOUSING"]] == months


# --- the merged year cell ---------------------------------------------------


def test_the_merged_year_is_carried_across_its_twelve_month_block():
    """Row 2 states the year once. Without the carry-forward every observation
    after Farvardin lands in the previous block's year."""
    values = [100.0] * 12 + [200.0] * 12
    grid = _grid(
        [(ARABIC_LABELS[code], values) for code in CODES], years=(1400, 1401)
    )
    points = parse_workbook(grid)["SCI_CPI_URBAN"]

    assert [point.ref_period_label for point in points] == (
        [f"1400-{month:02d}" for month in range(1, 13)]
        + [f"1401-{month:02d}" for month in range(1, 13)]
    )
    assert [point.jalali_year for point in points] == [1400] * 12 + [1401] * 12
    # ...and the Gregorian spans follow the year, not just the label.
    assert points[13].ref_period_start == jalali_month_span(1401, 2)[0]


def test_a_sheet_with_no_recognisable_month_header_is_refused():
    grid = _grid([(ARABIC_LABELS[code], [100.0] * 12) for code in CODES])
    grid[2] = [None] + ["month"] * 12
    with pytest.raises(SciParseError, match="no \\(year, month\\) columns"):
        parse_workbook(grid)


# --- gaps -------------------------------------------------------------------


def test_a_period_printed_as_a_dash_is_skipped_not_zero_filled():
    """SCI prints '-' for a period it has no value for. A zero index level
    would read as a 100% collapse in prices."""
    values = [100.0] * 12 + ["-"] + [200.0] * 11
    grid = _grid(
        [(ARABIC_LABELS[code], values) for code in CODES], years=(1400, 1401)
    )
    points = parse_workbook(grid)["SCI_CPI_RENT"]

    assert len(points) == 23
    assert "1401-01" not in {point.ref_period_label for point in points}
    assert all(point.value > 0 for point in points)
    assert not any(point.value == 0.0 for point in points)


# --- all-or-nothing ---------------------------------------------------------


def test_one_missing_target_row_refuses_the_whole_workbook():
    """A layout change that moves one row must not ingest the other three and
    look successful, leaving that series frozen at its last good vintage."""
    present = [code for code in CODES if code != "SCI_CPI_VEHICLES"]
    grid = _grid([(ARABIC_LABELS[code], [100.0] * 12) for code in present])

    with pytest.raises(SciParseError) as excinfo:
        parse_workbook(grid)

    message = str(excinfo.value)
    assert "SCI_CPI_VEHICLES" in message
    assert "refusing the whole file" in message
    # None of the three that WERE found leak out of the refusal.
    for code in present:
        assert code not in message


def test_nothing_is_written_when_a_workbook_is_refused(engine, tmp_path):
    present = [code for code in CODES if code != "SCI_CPI_RENT"]
    path = _write_xlsx(
        tmp_path / "ts_urban_140505-14050618165804.xlsx",
        _grid([(ARABIC_LABELS[code], [100.0] * 12) for code in present]),
    )

    with pytest.raises(SciParseError):
        ingest_sci_cpi(engine, path, filename=REAL_FILENAME, now=FIRST_INGEST)

    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(economic_observations)
        ).scalar() == 0
        # Not even the archive row: a document nothing could be read from is
        # not a document worth citing.
        assert conn.execute(
            select(func.count()).select_from(source_documents)
        ).scalar() == 0


# --- the base year ----------------------------------------------------------


def test_the_base_year_must_average_one_hundred():
    """1400=100 is the stated base and it is checkable from the data itself. A
    rebase restates the level history; storing restated levels under the old
    base_period label would be silently wrong forever."""
    with pytest.raises(SciParseError) as excinfo:
        parse_workbook(_all_four(value=112.5))

    message = str(excinfo.value)
    assert "112.5000" in message and "not 100" in message
    assert "different base" in message


def test_the_base_year_check_averages_rather_than_demanding_each_month_is_100():
    """The twelve months of the base year vary; it is their MEAN that is 100."""
    around_100 = [95.0, 105.0] * 6
    grid = _grid([(ARABIC_LABELS[code], around_100) for code in CODES])
    assert sorted(parse_workbook(grid)) == sorted(CODES)


def test_a_base_year_missing_months_is_refused():
    short = [100.0] * 11 + ["-"]
    grid = _grid([(ARABIC_LABELS[code], short) for code in CODES])
    with pytest.raises(SciParseError, match="has 11 months, expected 12"):
        parse_workbook(grid)


def test_the_real_fixture_is_exactly_on_base():
    """Not a tolerance story: all four series average 100.000000 over 1400."""
    for code, points in read_workbook(FIXTURE).items():
        base = [p.value for p in points if p.jalali_year == BASE_YEAR]
        assert len(base) == 12
        assert sum(base) / 12.0 == pytest.approx(100.0, abs=1e-9), code


# --- the real workbook ------------------------------------------------------


def test_the_fixture_parses_to_the_measured_shape_and_values():
    series = read_workbook(FIXTURE)

    assert sorted(series) == sorted(CODES)
    for code, points in series.items():
        assert len(points) == OBSERVATIONS_PER_SERIES, code
        assert points[0].ref_period_label == "1381-01"
        assert points[0].ref_period_start == date(2002, 3, 21)
        assert points[-1].ref_period_label == "1405-05"
        assert points[-1].ref_period_start == date(2026, 7, 23)
        assert points[-1].ref_period_end == date(2026, 8, 22)
        assert round(points[-1].value, 2) == LATEST[code]
    # Every period is one Jalali month, contiguous, and strictly increasing.
    starts = [point.ref_period_start for point in series["SCI_CPI_URBAN"]]
    assert starts == sorted(starts)
    assert len(set(starts)) == OBSERVATIONS_PER_SERIES


def test_a_missing_sheet_names_what_the_file_does_have(tmp_path):
    path = _write_xlsx(tmp_path / "x.xlsx", _all_four(), sheet_name="Sheet1")
    with pytest.raises(SciParseError, match="Sheet1"):
        read_workbook(path)


def test_a_file_that_is_not_a_workbook_is_refused_not_raised_through(tmp_path):
    path = tmp_path / "ts_urban.xlsx"
    path.write_bytes(b"<html>access denied</html>")
    with pytest.raises(SciParseError, match="not a readable .xlsx"):
        read_workbook(str(path))


def test_a_path_that_is_not_there_is_not_a_statement_about_the_document(tmp_path):
    """FileNotFoundError, not SciParseError: the endpoint uses the distinction
    to decide whether the PROVIDER's health changed."""
    with pytest.raises(FileNotFoundError):
        read_workbook(str(tmp_path / "absent.xlsx"))


# --- published_at -----------------------------------------------------------


def test_published_at_comes_from_scis_own_filename():
    """1405/06/18 16:58:04 -> 2026-09-09. The first series in this system whose
    published_at is a real date rather than NULL."""
    assert published_at_from_filename(REAL_FILENAME) == date(2026, 9, 9)


def test_a_filename_with_no_timestamp_yields_no_published_at():
    """Reported as absent, never guessed from the clock."""
    assert published_at_from_filename("ts_urban.xlsx") is None
    assert published_at_from_filename("sci_ts_urban_fixture.xlsx") is None
    # A stamp that is not a Jalali date is not one.
    assert published_at_from_filename("ts_urban_140505-14051318165804.xlsx") is None


def test_an_unfinished_month_would_be_flagged_rather_than_scored():
    mordad_end = jalali_month_span(1405, 5)[1]
    assert is_incomplete_month(mordad_end, datetime(2026, 8, 22, tzinfo=timezone.utc))
    assert not is_incomplete_month(
        mordad_end, datetime(2026, 8, 23, tzinfo=timezone.utc)
    )


# --- ingest -----------------------------------------------------------------


@pytest.fixture()
def ingested(engine):
    return ingest_sci_cpi(
        engine, FIXTURE, filename=REAL_FILENAME, now=FIRST_INGEST
    )


def _stored(engine, code):
    with engine.connect() as conn:
        series_id = conn.execute(
            select(economic_series.c.id).where(economic_series.c.code == code)
        ).scalar_one()
        return [
            dict(row._mapping)
            for row in conn.execute(
                select(economic_observations)
                .where(economic_observations.c.series_id == series_id)
                .order_by(economic_observations.c.ref_period_start)
            )
        ]


def test_ingest_stores_every_observation_of_every_series(engine, ingested):
    assert ingested["inserted"] == 4 * OBSERVATIONS_PER_SERIES
    assert ingested["unchanged"] == 0 and ingested["revised"] == 0
    for code in CODES:
        counts = ingested["series"][code]
        assert counts["status"] == "ok"
        assert counts["inserted"] == OBSERVATIONS_PER_SERIES
        assert counts["last_period"] == "1405-05"
        assert round(counts["last_value"], 2) == LATEST[code]
        # SCI publishes only finished months, and this one had finished.
        assert counts["projections"] == 0

    rows = _stored(engine, "SCI_CPI_URBAN")
    assert len(rows) == OBSERVATIONS_PER_SERIES
    assert rows[-1]["ref_period_label"] == "1405-05"
    assert all(row["vintage"] == 1 for row in rows)


def test_ingest_registers_the_series_from_the_catalog(engine, ingested):
    with engine.connect() as conn:
        rows = {
            row._mapping["code"]: dict(row._mapping)
            for row in conn.execute(
                select(economic_series).where(
                    economic_series.c.provider_code == "sci"
                )
            )
        }
    assert sorted(rows) == sorted(CODES)
    for code, row in rows.items():
        spec = spec_for_code(code)
        assert row["frequency"] == "M"
        assert row["calendar"] == "jalali"
        assert row["measure"] == "index"
        assert row["base_period"] == "1400=100"
        # SCI restated the whole level history at the 1400 rebase and publishes
        # no concordance: growth rates chain across a rebase, levels do not.
        assert row["splice_policy"] == "chain_growth"
        assert row["provider_series_id"] == spec.provider_series_id


def test_these_are_the_catalogs_first_official_series():
    """Everything else here is a mirror or an estimate; SCI is the legally
    designated statistical authority publishing its own numbers."""
    for spec in sci_specs():
        assert spec.quality_tier == "official"
        assert spec.calendar == "jalali"
        assert spec.splice_policy == "chain_growth"
        assert spec.revisable is True
        # One measured lag is not a characterisation; it lives in the notes.
        assert spec.publication_lag_days is None
        assert "1400=100" in spec.notes


def test_every_observation_cites_the_archived_document(engine, ingested):
    """A value whose source document cannot be identified later is not
    point-in-time defensible, and the citation cannot be back-filled."""
    document_id = ingested["document"]["id"]
    published = datetime(2026, 9, 9, tzinfo=timezone.utc)
    for code in CODES:
        rows = _stored(engine, code)
        assert {row["source_document_id"] for row in rows} == {document_id}
        assert {ensure_utc(row["published_at"]) for row in rows} == {published}
        assert {ensure_utc(row["available_at"]) for row in rows} == {FIRST_INGEST}


def test_the_source_document_row_is_the_first_real_write_to_that_table(
    engine, ingested
):
    with open(FIXTURE, "rb") as handle:
        expected_sha = hashlib.sha256(handle.read()).hexdigest()

    with engine.connect() as conn:
        rows = [
            dict(row._mapping) for row in conn.execute(select(source_documents))
        ]
    assert len(rows) == 1
    document = rows[0]
    assert document["provider_code"] == "sci"
    assert document["content_sha256"] == expected_sha == ingested["sha256"]
    assert document["byte_size"] == ingested["byte_size"] > 0
    assert document["media_type"].endswith("spreadsheetml.sheet")
    assert REAL_FILENAME in document["title"]
    # Reconstructed from the filename, and the report says so rather than
    # letting a caller mistake it for a URL somebody actually fetched.
    assert document["url"].endswith(REAL_FILENAME)
    assert ingested["document"]["url_reconstructed"] is True
    # The durable copy lives beside the backups on the host; the service only
    # ever sees a temporary copy, so it claims no retrievable path.
    assert document["storage_path"] == ""


def test_an_explicit_url_is_recorded_as_given(engine):
    url = "https://amar.org.ir/Portals/0/Statistics/" + REAL_FILENAME
    report = ingest_sci_cpi(
        engine, FIXTURE, filename=REAL_FILENAME, url=url, now=FIRST_INGEST
    )
    assert report["document"]["url"] == url
    assert report["document"]["url_reconstructed"] is False


def test_the_path_is_not_provenance_but_the_filename_is(engine):
    """The fixture's own basename carries no publication timestamp. Posting it
    without the SCI filename stores NULL and says why, rather than inventing a
    date from the clock."""
    report = ingest_sci_cpi(engine, FIXTURE, now=FIRST_INGEST)

    assert report["published_at"] is None
    assert "no Jalali publication timestamp" in report["published_at_note"]
    assert all(row["published_at"] is None for row in _stored(engine, "SCI_CPI_RENT"))


def test_re_posting_the_same_file_writes_nothing(engine, ingested):
    with engine.connect() as conn:
        before = conn.execute(
            select(func.count()).select_from(economic_observations)
        ).scalar()

    again = ingest_sci_cpi(
        engine, FIXTURE, filename=REAL_FILENAME, now=SECOND_INGEST
    )

    assert again["inserted"] == 0 and again["revised"] == 0
    assert again["unchanged"] == 4 * OBSERVATIONS_PER_SERIES
    # Same bytes, same provider: the same document, deduped on the hash.
    assert again["document"]["status"] == "existing"
    assert again["document"]["id"] == ingested["document"]["id"]
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(economic_observations)
        ).scalar() == before
        assert conn.execute(
            select(func.count()).select_from(source_documents)
        ).scalar() == 1


def test_a_restated_value_becomes_a_new_vintage_beside_the_first_print(
    engine, ingested, tmp_path
):
    """SCI revises. The print that stood before the revision stays readable,
    which is the whole reason this store is bitemporal."""
    original = read_workbook(FIXTURE)
    revised_month = original["SCI_CPI_URBAN"][-1]
    rows = []
    for code in CODES:
        points = original[code]
        values = [point.value for point in points]
        if code == "SCI_CPI_URBAN":
            values[-1] = values[-1] + 1.5
        rows.append((ARABIC_LABELS[code], values))
    # Same year blocks as the fixture, so the rebuilt sheet lines its columns
    # up with the values taken out of it. 1405 is short (five months
    # published); its trailing columns simply carry nothing.
    years = sorted({point.jalali_year for point in original["SCI_CPI_URBAN"]})
    path = _write_xlsx(
        tmp_path / "ts_urban_140505-14050620090000.xlsx", _grid(rows, years=years)
    )

    report = ingest_sci_cpi(
        engine,
        path,
        filename="ts_urban_140505-14050620090000.xlsx",
        now=SECOND_INGEST,
    )

    assert report["revised"] == 1
    assert report["series"]["SCI_CPI_URBAN"]["revised"] == 1
    history = vintages_for_period(
        engine, "SCI_CPI_URBAN", revised_month.ref_period_start
    )
    assert [item["vintage"] for item in history] == [1, 2]
    assert history[0]["value"] == pytest.approx(revised_month.value)
    assert history[1]["value"] == pytest.approx(revised_month.value + 1.5)
    # A second document, cited by the revision and not by the first print.
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(source_documents)
        ).scalar() == 2


def test_a_point_in_time_read_before_the_ingest_sees_nothing(engine, ingested):
    """available_at is when THIS system could first have known the value, and a
    cutoff before that is correctly answered with silence."""
    before = point_in_time(
        engine, "SCI_CPI_URBAN", as_of=datetime(2026, 9, 1, tzinfo=timezone.utc)
    )
    assert before.observations == ()

    after = point_in_time(
        engine, "SCI_CPI_URBAN", as_of=datetime(2026, 9, 10, tzinfo=timezone.utc)
    )
    assert len(after.observations) == OBSERVATIONS_PER_SERIES
    assert after.vintages == frozenset({1})
    assert round(after.observations[-1].value, 2) == LATEST["SCI_CPI_URBAN"]


# --- the endpoint -----------------------------------------------------------


def test_endpoint_requires_the_internal_token(client):
    assert client.post("/internal/economic/sci-cpi").status_code == 401


def test_endpoint_ingests_and_reports(client, engine):
    resp = client.post(
        "/internal/economic/sci-cpi",
        json={"path": FIXTURE, "filename": REAL_FILENAME},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["workbook"] == REAL_FILENAME
    assert body["inserted"] == 4 * OBSERVATIONS_PER_SERIES
    assert body["published_at"].startswith("2026-09-09")
    assert sorted(body["series"]) == sorted(CODES)
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(economic_observations)
        ).scalar() == 4 * OBSERVATIONS_PER_SERIES


def test_endpoint_is_idempotent(client):
    payload = {"path": FIXTURE, "filename": REAL_FILENAME}
    first = client.post("/internal/economic/sci-cpi", json=payload, headers=AUTH)
    second = client.post("/internal/economic/sci-cpi", json=payload, headers=AUTH)

    assert first.json()["inserted"] == 4 * OBSERVATIONS_PER_SERIES
    assert second.status_code == 200
    assert second.json()["inserted"] == 0
    assert second.json()["document"]["status"] == "existing"


def test_endpoint_refuses_a_changed_layout_with_400(client, engine, tmp_path):
    present = [code for code in CODES if code != "SCI_CPI_HOUSING"]
    path = _write_xlsx(
        tmp_path / REAL_FILENAME,
        _grid([(ARABIC_LABELS[code], [100.0] * 12) for code in present]),
    )

    resp = client.post(
        "/internal/economic/sci-cpi",
        json={"path": path, "filename": REAL_FILENAME},
        headers=AUTH,
    )

    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "bad_request"
    assert "SCI_CPI_HOUSING" in error["message"]
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(economic_observations)
        ).scalar() == 0


def test_endpoint_refuses_a_missing_path_with_400(client):
    resp = client.post(
        "/internal/economic/sci-cpi",
        json={"path": "/tmp/there-is-no-such-workbook.xlsx"},
        headers=AUTH,
    )
    assert resp.status_code == 400
    assert "there-is-no-such-workbook" in resp.json()["error"]["message"]


# --- the scheduled job leaves this provider alone ---------------------------


def test_the_polled_ingest_reports_sci_without_attempting_a_fetch(engine, settings):
    """amar.org.ir cannot be reached from this host at all, so polling it would
    fail on every tick and park the breaker on a provider that is not down."""
    from app.jobs.economic import ingest_economic

    assert "sci" in DOCUMENT_INGEST_PROVIDERS
    result = ingest_economic(engine, settings, list(CODES))

    assert result["errors"] == []
    assert result["series_failed"] == 0
    assert result["series_ingested"] == 0
    for code in CODES:
        entry = result["series"][code]
        assert entry["status"] == "document_ingest"
        assert "/internal/economic/sci-cpi" in entry["reason"]
    # Nothing was attempted, so no provider health verdict was invented.
    assert result["providers"] == {}
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(economic_observations)
        ).scalar() == 0
