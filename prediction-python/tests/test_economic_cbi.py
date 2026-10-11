"""The CBI monetary workbooks: parsing, verification, archiving, vintages.

The happy paths run against five REAL editions, kept whole and gzipped
(``tests/fixtures/cbi_<edition>_<CBI's filename>.xls.gz``, ~60 KB each).  Not
trimmed, and that is forced rather than chosen: trimming a legacy .xls means
rewriting its OLE container, which would replace the very bytes whose sha256
the archive records and drop the "last saved" stamp that becomes
``published_at``.  Decompressed, each is byte-identical to what www.cbi.ir
served on 2026-10-11 (the sha256 values below).  Between them they cover:

* 1399-04 — the first Excel edition, sheet «Sheet1», old base-money method;
* 1400-12 — an Esfand edition whose period is the tail of the title cell and
  whose header has no month row at all (the row above the years is «مانده»),
  base money at Esfand 1400 = 6,039.7 on the old method;
* 1402-12 — the same Esfand restated on CBI's MFSMCG 2016 method (5,981.1),
  the month row printed as ``0.0``, the footnote «از اسفند 1399 به بعد»;
* 1403-07 — the edition whose central-bank sheet sits one column left of every
  other edition's (the parser reads only the titled sheet, and shows it);
* 1404-12 — the newest: M2 155,812.2, base money «(کاملا مقدماتی)», the
  footnote «از سال 1400 به بعد», a '#' growth cell.

The refusal and drift paths run against the REAL rows of those workbooks with
one thing changed — a label respelled, every column shifted, one number off —
because the point of each is a workbook CBI has not published (yet).  Those
are row grids in the shape xlrd's ``row_values`` yields, which is what
:func:`app.economic.cbi.parse_sheet` takes.
"""
from __future__ import annotations

import gzip
import io
import os
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
import xlrd
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql

from app.db import (
    economic_observations,
    economic_series,
    ensure_utc,
    instruments,
    source_documents,
)
from app.economic import cbi, jalali, sci
from app.economic.catalog import (
    BASE_MONEY_CODE,
    CBI_UNIT,
    DOCUMENT_INGEST_PROVIDERS,
    DOCUMENT_INGEST_ROUTES,
    cbi_specs,
    codes_for_provider,
    spec_for_code,
)
from app.economic.cbi import (
    ACCOUNTING_TOLERANCE,
    METHOD_OLD,
    METHOD_REVISED,
    ROW_TARGETS,
    CbiParseError,
    CbiStaleEditionError,
    Edition,
    backfill_available_at,
    excel_can_carry,
    ingest_edition_file,
    label_key,
    newest_stored_edition,
    parse_sheet,
    read_workbook,
    read_workbook_bytes,
    saved_at_from_summary,
)
from app.economic.store import (
    ensure_series,
    point_in_time,
    record_observation,
    stored_value,
    vintages_for_period,
)

from .conftest import FIXTURES_DIR

CODES = ("CBI_LIQUIDITY", "CBI_BASE_MONEY", "CBI_MONEY_M1", "CBI_QUASI_MONEY")

# fixture -> (CBI's filename, sha256 of the workbook as served, its OLE "last
# saved" time). Measured 2026-10-11.
EDITIONS = {
    "1399-04": ("cbi_1399-04_pol9904.xls.gz", "pol9904.xls",
                "8975f7746088584bfe0f6810793b4a838c004ffa77d15154528223293a472bce",
                datetime(2020, 11, 14, 9, 32, 53, tzinfo=timezone.utc)),
    "1400-12": ("cbi_1400-12_pol1400-12.xls.gz", "pol1400,12.xls",
                "77230fff3f9b0d837d708c2aa7ddc9b3d20a890259f07583ff519f4ab8ae0eb7",
                datetime(2022, 8, 22, 7, 55, 46, tzinfo=timezone.utc)),
    "1402-12": ("cbi_1402-12_pol1402-12.xls.gz", "pol1402,12.xls",
                "8b11db6008ac97a82f69d5cf1844480e3247f02899f6244b2b60e625344740bb",
                datetime(2024, 12, 30, 7, 17, 53, tzinfo=timezone.utc)),
    "1403-07": ("cbi_1403-07_pol1403-7.xls.gz", "pol1403,7.xls",
                "7e58bc071c1e5e163508e95b0a7a7989a4c7041af4acb592e236ea5e52968e9f",
                datetime(2024, 12, 7, 9, 58, 22, tzinfo=timezone.utc)),
    "1404-12": ("cbi_1404-12_140412.xls.gz", "140412.xls",
                "648af2335b07d1b8a7f3531765e5dc0784071fd708ec64b3d9ab4cd4f6b0ea0f",
                datetime(2026, 6, 21, 5, 41, 31, tzinfo=timezone.utc)),
}

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def _bytes(edition: str) -> bytes:
    with gzip.open(os.path.join(FIXTURES_DIR, EDITIONS[edition][0])) as handle:
        return handle.read()


def _file(tmp_path, edition: str) -> str:
    """The real workbook, decompressed under CBI's own filename."""
    path = tmp_path / EDITIONS[edition][1]
    path.write_bytes(_bytes(edition))
    return str(path)


def _rows(edition: str) -> list[list]:
    book = xlrd.open_workbook(file_contents=_bytes(edition), logfile=io.StringIO())
    sheet = book.sheet_by_index(0)
    return [sheet.row_values(r) for r in range(sheet.nrows)]


def _row(grid, label: str) -> int:
    """The index of the row whose label (the text nearest the numbers) is ``label``."""
    key = label_key(label)
    for index, row in enumerate(grid):
        texts = [cell for cell in row[:5] if isinstance(cell, str) and cell.strip()]
        if texts and label_key(texts[-1]) == key:
            return index
    raise AssertionError(f"no row labelled {label!r}")


def _cell(grid, text: str) -> tuple[int, int]:
    for r, row in enumerate(grid):
        for c, cell in enumerate(row):
            if isinstance(cell, str) and text in cell:
                return r, c
    raise AssertionError(f"no cell containing {text!r}")


def _values(parsed, code):
    return [(point.ref_period_label, point.value, point.method)
            for point in parsed.observations[code]]


# --- the shared Jalali and Persian helpers ------------------------------------


def test_the_calendar_and_text_helpers_moved_without_changing():
    """sci.py re-exports them, so SCI's callers and the CBI reader share one
    implementation and cannot disagree about a month boundary or a letter."""
    assert sci.jalali_month_span is jalali.jalali_month_span
    assert sci.normalize_fa is jalali.normalize_fa
    assert sci.JALALI_MONTHS == jalali.JALALI_MONTHS


def test_month_arithmetic_steps_the_label_not_the_calendar():
    assert jalali.shift_month(1404, 1, -1) == (1403, 12)
    assert jalali.shift_month(1403, 12, 1) == (1404, 1)
    assert jalali.shift_month(1404, 12, -12) == (1403, 12)
    assert jalali.shift_month(1400, 6, 30) == (1402, 12)
    assert jalali.period_label(1404, 3) == "1404-03"


def test_month_names_match_exactly_and_in_either_orthography():
    assert jalali.month_number("فروردين") == 1          # Arabic yeh, as 1399's list prints it
    assert jalali.month_number(" اسفند ") == 12
    assert jalali.month_number("دی") == 10
    assert jalali.month_number("دیروز") is None          # contains دی, is not دی
    assert jalali.month_number(0.0) is None


def test_persian_digits_are_folded_where_numbers_are_read_from_text():
    assert jalali.fold_digits("اسفند۱۴۰۴") == "اسفند1404"
    assert jalali.fold_digits("١٤٠٠") == "1400"
    # normalize_fa alone leaves them: NFKC has no mapping for either set.
    assert jalali.normalize_fa("۱۴۰۴") == "۱۴۰۴"


def test_the_month_spans_of_the_editions_are_the_measured_ones():
    # Esfand 1404 ends 2026-03-20; 1403 is a leap year, 1404 is not.
    assert jalali.jalali_month_span(1404, 12) == (date(2026, 2, 20), date(2026, 3, 20))
    assert jalali.jalali_month_span(1403, 12) == (date(2025, 2, 19), date(2025, 3, 20))
    assert jalali.jalali_month_span(1399, 4) == (date(2020, 6, 21), date(2020, 7, 21))


# --- the catalog ----------------------------------------------------------------


def test_the_four_series_carry_the_identifiers_the_design_fixes():
    specs = {spec.code: spec for spec in cbi_specs()}
    assert sorted(specs) == sorted(CODES)
    assert codes_for_provider("cbi") == list(specs)
    for spec in specs.values():
        assert spec.provider_code == "cbi"
        assert spec.country == "IRN"
        assert spec.frequency == "M"
        assert spec.calendar == "jalali"
        assert spec.measure == "level"
        assert spec.seasonal_adjustment == "nsa"
        assert spec.unit == CBI_UNIT == "thousand_billion_rial"
        assert spec.quote_currency == "IRR"
        assert spec.quality_tier == "official"
        assert spec.revisable is True
        assert spec.base_period == ""
        assert spec.decimals == 1
        assert spec.publication_lag_days is None
        # What travels with every number into the UI.
        assert "END-OF-MONTH STOCK" in spec.notes
        assert "thousand billion rials" in spec.notes and "divide by" in spec.notes
    assert specs["CBI_LIQUIDITY"].name_en == "Liquidity (M2)"
    assert specs["CBI_LIQUIDITY"].name_fa == "نقدینگی"
    assert specs[BASE_MONEY_CODE].name_fa == "پایه پولی"


def test_base_money_declares_its_method_break():
    spec = spec_for_code(BASE_MONEY_CODE)
    # Levels on the two methods are not comparable; growth chains within one.
    assert spec.splice_policy == "chain_growth"
    assert "MFSMCG" in spec.notes and "6,039.7 -> 5,981.1" in spec.notes
    assert all(spec_for_code(code).splice_policy == "none"
               for code in CODES if code != BASE_MONEY_CODE)


def test_the_catalog_keeps_cbis_orthography_and_the_parser_folds_it():
    """Labels as CBI prints them (Arabic yeh) so they can be grepped against a
    workbook; the parser's targets are the folded forms of exactly these."""
    indicators = {spec.code: spec.indicator for spec in cbi_specs()}
    assert "ي" in indicators["CBI_LIQUIDITY"] and "ي" in indicators[BASE_MONEY_CODE]
    for code, label in indicators.items():
        assert ROW_TARGETS[label_key(label)] == code
    assert spec_for_code("CBI_LIQUIDITY").provider_series_id.startswith("sei_monetary:")


def test_the_scheduled_economic_job_reports_cbi_with_its_own_route(engine, settings):
    from app.jobs.economic import ingest_economic

    assert {"sci", "cbi"} <= DOCUMENT_INGEST_PROVIDERS
    result = ingest_economic(engine, settings, list(CODES))
    assert result["series_failed"] == 0 and result["series_ingested"] == 0
    for code in CODES:
        entry = result["series"][code]
        assert entry["status"] == "document_ingest"
        assert "/internal/economic/cbi-monetary" in entry["reason"]
        assert "cbi_history_fetch.py" in entry["reason"]
    # SCI's reason is still SCI's, not a shared string.
    assert "sci_fetch.py" in DOCUMENT_INGEST_ROUTES["sci"]
    assert result["providers"] == {}


# --- labels ---------------------------------------------------------------------


def test_label_keys_fold_letters_zwnj_footnotes_and_qualifiers():
    assert label_key("پايه پولي از سمت منابع(کاملا مقدماتی)") == label_key("پایه پولی از سمت منابع")
    assert label_key("نقدينگي برحسب اجزاي تشکيل‌دهنده") == label_key("نقدینگی برحسب اجزای تشکیل دهنده")
    assert label_key("نقدينگي(1)") == label_key("نقدینگی")
    assert label_key("  پول * ") == label_key("پول")
    # Exact keys: neither contains the other's match.
    assert label_key("شبه پول") != label_key("پول")


# --- the real editions ----------------------------------------------------------


def test_the_first_excel_edition_with_its_sheet1_layout():
    parsed, saved_at = read_workbook_bytes(_bytes("1399-04"))
    assert parsed.edition == Edition(1399, 4)
    assert parsed.sheet_name == "Sheet1"
    assert parsed.unit == "thousand_billion_rial"
    assert parsed.checks["columns"] == ["1398-04", "1398-12", "1399-04"]
    assert _values(parsed, "CBI_LIQUIDITY") == [
        ("1398-04", Decimal("20237.0"), None),
        ("1398-12", Decimal("24721.5"), None),
        ("1399-04", Decimal("27397.8"), None),
    ]
    assert _values(parsed, BASE_MONEY_CODE) == [
        ("1398-04", Decimal("2774.3"), METHOD_OLD),
        ("1398-12", Decimal("3528.5"), METHOD_OLD),
        ("1399-04", Decimal("3628.6"), METHOD_OLD),
    ]
    assert parsed.revision_start is None
    assert saved_at == EDITIONS["1399-04"][3]


def test_an_esfand_edition_with_the_period_in_the_title_and_no_month_row():
    grid = _rows("1400-12")
    # The shape that makes this edition different: the period is the tail of
    # the title cell, and the row above the years is the «مانده» header.
    title_row, _ = _cell(grid, "در پايان اسفند 1400")
    assert any(cell == "مانده" for cell in grid[title_row + 1])
    parsed = parse_sheet(grid)
    assert parsed.edition == Edition(1400, 12)
    assert parsed.checks["columns"] == ["1398-12", "1399-12", "1400-12"]
    assert _values(parsed, BASE_MONEY_CODE)[-1] == ("1400-12", Decimal("6039.7"), METHOD_OLD)
    assert _values(parsed, "CBI_LIQUIDITY")[-1] == ("1400-12", Decimal("48324.4"), None)


def test_the_restated_esfand_edition_reads_the_revised_method():
    parsed, saved_at = read_workbook_bytes(_bytes("1402-12"))
    assert parsed.edition == Edition(1402, 12)
    assert parsed.revision_start == (1399, 12)          # «از اسفند 1399 به بعد»
    assert _values(parsed, BASE_MONEY_CODE) == [
        ("1400-12", Decimal("5981.1"), METHOD_REVISED),
        ("1401-12", Decimal("8548.0"), METHOD_REVISED),
        ("1402-12", Decimal("10921.3"), METHOD_REVISED),
    ]
    # M2 at Esfand 1400 is the same in both editions: only base money moved.
    assert _values(parsed, "CBI_LIQUIDITY")[0] == ("1400-12", Decimal("48324.4"), None)
    # CBI replaced this file in place; its last-saved stamp says when.
    assert saved_at == EDITIONS["1402-12"][3]


def test_the_1403_07_edition_reads_its_titled_sheet_only():
    """Its central-bank sheet is one column left of every other edition's
    (balances from column 3, not 4). The parser never reads that sheet: it
    reads the sheet whose title names the monetary aggregates."""
    book = xlrd.open_workbook(file_contents=_bytes("1403-07"), logfile=io.StringIO())
    central_bank = book.sheet_by_index(2)
    header = central_bank.row_values(4)
    assert header[3] == "مهر" and header[4] == "اسفند"
    newest = xlrd.open_workbook(file_contents=_bytes("1404-12"), logfile=io.StringIO())
    assert newest.sheet_by_index(2).row_values(4)[3] == ""

    parsed, _ = read_workbook_bytes(_bytes("1403-07"))
    assert parsed.sheet_name == "متغیرهای عمده"
    assert parsed.checks["columns"] == ["1402-07", "1402-12", "1403-07"]
    assert _values(parsed, "CBI_LIQUIDITY") == [
        ("1402-07", Decimal("71778.4"), None),
        ("1402-12", Decimal("78774.5"), None),
        ("1403-07", Decimal("92334.5"), None),
    ]
    assert [m for *_, m in _values(parsed, BASE_MONEY_CODE)] == [METHOD_REVISED] * 3


def test_the_newest_edition_and_its_exact_decimals():
    parsed, saved_at = read_workbook_bytes(_bytes("1404-12"))
    assert parsed.edition == Edition(1404, 12)
    assert parsed.revision_start == (1400, 1)           # «از سال 1400 به بعد»
    m2 = parsed.observations["CBI_LIQUIDITY"][-1]
    assert m2.value == Decimal("155812.2")
    assert str(m2.value) == "155812.2"                  # not 155812.20000000001164...
    assert isinstance(m2.value, Decimal)
    assert parsed.observations[BASE_MONEY_CODE][-1].value == Decimal("21959.3")
    # M2 = M1 + quasi-money, exactly, as printed.
    m1 = parsed.observations["CBI_MONEY_M1"][-1].value
    quasi = parsed.observations["CBI_QUASI_MONEY"][-1].value
    assert m1 + quasi == m2.value == Decimal("155812.2")
    assert saved_at == datetime(2026, 6, 21, 5, 41, 31, tzinfo=timezone.utc)
    # Every check passed with room to spare, and the report says by how much.
    assert Decimal(parsed.checks["accounting_max_gap"]) == 0
    assert Decimal(parsed.checks["growth_max_gap_pct"]) < Decimal("0.05")


# --- drift the parser tolerates --------------------------------------------------


def _shift(grid, columns: int):
    if columns >= 0:
        return [[""] * columns + list(row) for row in grid]
    return [list(row)[-columns:] for row in grid]


@pytest.mark.parametrize("edition", ["1404-12", "1403-07", "1399-04"])
@pytest.mark.parametrize("columns", [-1, 2])
def test_columns_are_found_by_their_headers_not_their_position(edition, columns):
    """Editions put the levels in columns 3-5 or 4-6; the parse must not care."""
    expected = parse_sheet(_rows(edition))
    shifted = parse_sheet(_shift(_rows(edition), columns))
    assert shifted.observations == expected.observations
    assert shifted.checks["columns"] == expected.checks["columns"]


def test_rows_are_found_by_their_labels_not_their_position():
    grid = _rows("1404-12")
    expected = parse_sheet(grid)
    padded = [[""] * len(grid[0]) for _ in range(3)] + grid
    # And the quasi-money row moved above money.
    m1, quasi = _row(padded, "پول"), _row(padded, "شبه پول")
    padded[m1], padded[quasi] = padded[quasi], padded[m1]
    assert parse_sheet(padded).observations == expected.observations


def test_persian_and_arabic_spellings_zwnj_and_footnotes_all_match():
    grid = _rows("1404-12")
    expected = parse_sheet(grid).observations
    base = _row(grid, "پایه پولی از سمت منابع")
    components = _row(grid, "نقدینگی برحسب اجزای تشکیل دهنده")
    m2 = _row(grid, "نقدینگی برحسب عوامل موثر بر عرضه")
    grid[base][3] = "پایه پولی از سمت منابع (3)*"                  # Persian yeh + markers
    grid[components][3] = "نقدينگي برحسب اجزاي تشکيل دهنده"        # a space for the ZWNJ
    grid[m2][3] = "‏نقدينگي برحسب عوامل موثر بر عرضه  "        # RLM, padding
    assert parse_sheet(grid).observations == expected


def test_persian_digits_in_the_title_period():
    grid = _rows("1404-12")
    r, c = _cell(grid, "اسفند1404")
    grid[r][c] = "اسفند ۱۴۰۴"
    assert parse_sheet(grid).edition == Edition(1404, 12)


def test_the_sheet_name_does_not_matter():
    grid = _rows("1404-12")
    assert parse_sheet(grid, sheet_name="Sheet1").edition == parse_sheet(grid).edition


# --- refusals ---------------------------------------------------------------------


def test_one_missing_row_refuses_the_whole_workbook():
    grid = _rows("1404-12")
    del grid[_row(grid, "شبه پول")]
    with pytest.raises(CbiParseError) as excinfo:
        parse_sheet(grid)
    message = str(excinfo.value)
    assert "CBI_QUASI_MONEY" in message and "refusing the whole file" in message
    for code in ("CBI_LIQUIDITY", "CBI_MONEY_M1", BASE_MONEY_CODE):
        assert code not in message


def test_a_check_row_missing_refuses_too():
    grid = _rows("1404-12")
    del grid[_row(grid, "پایه پولی از سمت مصارف")]
    with pytest.raises(CbiParseError, match="base_money_uses"):
        parse_sheet(grid)


def test_a_label_on_two_rows_is_refused_rather_than_chosen():
    grid = _rows("1404-12")
    grid.append(list(grid[_row(grid, "پول")]))
    with pytest.raises(CbiParseError, match="CBI_MONEY_M1: the label appears on rows"):
        parse_sheet(grid)


def test_an_unknown_unit_is_refused_never_rescaled():
    grid = _rows("1404-12")
    r, c = _cell(grid, "هزار ميليارد ريال")
    grid[r][c] = "(ميليارد ريال- درصد- واحد درصد)"
    with pytest.raises(CbiParseError, match="«میلیارد ریال», not «هزار میلیارد ریال»"):
        parse_sheet(grid)


def test_a_sheet_that_states_no_unit_is_refused():
    grid = _rows("1404-12")
    r, c = _cell(grid, "هزار ميليارد ريال")
    grid[r][c] = "(درصد- واحد درصد)"
    with pytest.raises(CbiParseError, match="states no unit"):
        parse_sheet(grid)


def test_a_sheet_without_the_title_is_not_this_sheet():
    grid = _rows("1404-12")
    r, c = _cell(grid, "متغيرهاي عمده")
    grid[r][c] = "ارقام عمده پولي و اعتباري"
    with pytest.raises(CbiParseError, match="is not in the first"):
        parse_sheet(grid)


def test_a_title_naming_two_periods_is_refused():
    grid = _rows("1404-12")
    r, c = _cell(grid, "اسفند1404")
    grid[r][c] = "اسفند1404 و بهمن1404"
    with pytest.raises(CbiParseError, match="exactly one month and year"):
        parse_sheet(grid)


def test_columns_that_are_not_the_editions_months_are_refused():
    """A header misread by one column would store every value one month off."""
    grid = _rows("1403-07")
    months = grid[5]
    assert months[4] == "مهر"
    months[4] = "آبان"
    with pytest.raises(CbiParseError, match="must print the balances at 1402-07, 1402-12, 1403-07"):
        parse_sheet(grid)


def test_only_an_esfand_edition_may_omit_its_month_names():
    grid = _rows("1403-07")
    grid[5] = [cell if not isinstance(cell, str) or cell not in ("مهر", "اسفند") else ""
               for cell in grid[5]]
    with pytest.raises(CbiParseError, match="no month name above it"):
        parse_sheet(grid)


def _bump(grid, label, amount, columns=(4, 5, 6)):
    row = grid[_row(grid, label)]
    for col in columns:
        row[col] = row[col] + amount


def test_base_money_sources_must_equal_uses():
    grid = _rows("1404-12")
    _bump(grid, "پایه پولی از سمت مصارف", 1.0, columns=(6,))
    with pytest.raises(CbiParseError, match="base money, sources against uses at 1404-12"):
        parse_sheet(grid)


def test_m2_supply_side_must_equal_components():
    grid = _rows("1404-12")
    _bump(grid, "نقدینگی برحسب اجزای تشکیل دهنده", 1.0, columns=(4,))
    with pytest.raises(CbiParseError, match="supply side against components at 1402-12"):
        parse_sheet(grid)


def test_m2_must_equal_m1_plus_quasi_money():
    grid = _rows("1404-12")
    _bump(grid, "شبه پول", 2.0, columns=(5,))
    with pytest.raises(CbiParseError, match=r"money \+ quasi-money at 1403-12"):
        parse_sheet(grid)


def test_rounding_within_the_printed_precision_is_not_a_failure():
    grid = _rows("1404-12")
    _bump(grid, "شبه پول", 0.1)
    parsed = parse_sheet(grid)
    assert Decimal(parsed.checks["accounting_max_gap"]) == Decimal("0.1")
    assert Decimal("0.1") <= ACCOUNTING_TOLERANCE


def test_the_printed_growth_must_match_the_printed_levels():
    grid = _rows("1404-12")
    row = grid[_row(grid, "نقدینگی برحسب عوامل موثر بر عرضه")]
    assert row[8] == 53.3
    row[8] = 50.0
    with pytest.raises(CbiParseError, match="prints 50.0% growth in column 8, but its own levels give 53.27%"):
        parse_sheet(grid)


def test_monthly_growth_compares_this_month_with_last_year_and_last_esfand():
    """1403-07: 21.3% over Mehr 1402 and 9.5% over Esfand 1402 for base money.
    Swapping the two printed rates is caught: the pairs are not interchangeable."""
    grid = _rows("1403-07")
    row = grid[_row(grid, "پایه پولی از سمت منابع")]
    assert (row[7], row[8]) == (21.3, 9.5)
    row[7], row[8] = row[8], row[7]
    with pytest.raises(CbiParseError, match="CBI_BASE_MONEY: CBI prints 9.5% growth in column 7"):
        parse_sheet(grid)


def _base_money_levels(grid, levels, growth):
    for label in ("پایه پولی از سمت منابع", "پایه پولی از سمت مصارف"):
        row = grid[_row(grid, label)]
        row[4:7] = levels
        row[7:9] = growth


def test_hash_means_beyond_500_percent_and_is_accepted_only_there():
    grid = _rows("1404-12")
    # Esfand 1402 -> 1403 growth of 600%: CBI prints '#'.
    _base_money_levels(grid, [1000.0, 7000.0, 21959.3], ["#", 213.7])
    parsed = parse_sheet(grid)
    assert parsed.observations[BASE_MONEY_CODE][0].value == Decimal("1000.0")

    grid = _rows("1404-12")
    _base_money_levels(grid, [10921.3, 13594.3, 21959.3], ["#", 61.5])
    with pytest.raises(CbiParseError, match="prints '#'.*give 24.5%"):
        parse_sheet(grid)


def test_a_growth_cell_that_is_not_a_number_cannot_be_checked():
    grid = _rows("1404-12")
    grid[_row(grid, "پول")][7] = ""
    with pytest.raises(CbiParseError, match="the printed growth cannot be checked"):
        parse_sheet(grid)


def test_a_balance_that_is_not_a_positive_number_is_refused():
    grid = _rows("1404-12")
    grid[_row(grid, "شبه پول")][5] = "-"
    with pytest.raises(CbiParseError, match="the balance at 1403-12 .* is '-', not a number"):
        parse_sheet(grid)
    grid = _rows("1404-12")
    grid[_row(grid, "پول")][4] = 0.0
    with pytest.raises(CbiParseError, match="a monetary aggregate is positive"):
        parse_sheet(grid)


# --- the base-money method ------------------------------------------------------


def test_an_edition_without_the_footnote_is_on_the_old_method():
    grid = _rows("1404-12")
    r, c = _cell(grid, "MFSMCG")
    grid[r][c] = ""
    parsed = parse_sheet(grid)
    assert parsed.revision_start is None
    assert {p.method for p in parsed.observations[BASE_MONEY_CODE]} == {METHOD_OLD}
    # Only base money carries a method.
    assert {p.method for p in parsed.observations["CBI_LIQUIDITY"]} == {None}


def test_the_footnote_states_where_the_revised_method_starts():
    grid = _rows("1402-12")
    r, c = _cell(grid, "MFSMCG")
    grid[r][c] = grid[r][c].replace("از اسفند 1399 به بعد", "از سال ۱۴۰۱ به بعد")
    parsed = parse_sheet(grid)
    assert parsed.revision_start == (1401, 1)
    assert [p.method for p in parsed.observations[BASE_MONEY_CODE]] == [
        METHOD_OLD, METHOD_REVISED, METHOD_REVISED,     # 1400-12 precedes 1401
    ]


def test_a_footnote_whose_start_cannot_be_read_refuses_the_workbook():
    """A method that cannot be stated is not guessed."""
    grid = _rows("1404-12")
    r, c = _cell(grid, "MFSMCG")
    grid[r][c] = grid[r][c].replace("از سال 1400 به بعد", "برای دوره های اخیر")
    with pytest.raises(CbiParseError, match="MFSMCG 2016 redefinition"):
        parse_sheet(grid)


# --- the file -------------------------------------------------------------------


def test_an_html_page_is_not_a_workbook():
    with pytest.raises(CbiParseError, match="an HTML page .*not an OLE2 compound file"):
        read_workbook_bytes(b"<!DOCTYPE html><html><body>Request rejected</body></html>")


def test_other_bytes_are_not_a_workbook_either():
    with pytest.raises(CbiParseError, match="bytes starting 504b0304"):
        read_workbook_bytes(b"PK\x03\x04 an xlsx is not an xls")
    with pytest.raises(CbiParseError, match="not a readable .xls workbook"):
        read_workbook_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"\x00" * 100)


def test_a_path_that_is_not_there_is_not_a_statement_about_the_document(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_workbook(str(tmp_path / "140412.xls"))


def test_a_summary_stream_without_a_saved_time_yields_none():
    assert saved_at_from_summary(b"") is None
    assert saved_at_from_summary(b"\x00" * 64) is None


def test_read_workbook_identifies_the_bytes(tmp_path):
    parsed, document = read_workbook(_file(tmp_path, "1404-12"))
    assert document.sha256 == EDITIONS["1404-12"][2]
    assert document.byte_size == 201216
    assert document.saved_at == EDITIONS["1404-12"][3]
    assert parsed.edition.label == "1404-12"


# --- which months an edition can carry ----------------------------------------


@pytest.mark.parametrize("period, carried", [
    ((1397, 9), False), ((1397, 12), True), ((1398, 1), False), ((1398, 3), False),
    ((1398, 4), True), ((1398, 12), True), ((1352, 12), False), ((1404, 9), True),
])
def test_excel_can_carry_follows_the_edition_shapes(period, carried):
    assert excel_can_carry(*period) is carried


def test_edition_columns_and_labels():
    assert Edition(1404, 7).columns() == ((1403, 7), (1403, 12), (1404, 7))
    assert Edition(1404, 12).columns() == ((1402, 12), (1403, 12), (1404, 12))
    assert Edition.parse("1399-04") == Edition(1399, 4) < Edition(1404, 12)
    for bad in ("1404-13", "1404-1", "14-04", "", "1404/12"):
        with pytest.raises(ValueError):
            Edition.parse(bad)


# --- ingest ---------------------------------------------------------------------


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
                .order_by(economic_observations.c.ref_period_start,
                          economic_observations.c.vintage)
            )
        ]


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar()


def test_a_live_ingest_stores_the_three_months_of_each_series(engine, tmp_path):
    report = ingest_edition_file(
        engine, _file(tmp_path, "1404-12"), url="https://www.cbi.ir/page/35570.aspx",
        filename="140412.xls", now=NOW,
    )
    assert report["edition"] == "1404-12"
    assert report["inserted"] == 12 and report["unchanged"] == 0
    assert report["available_at"] == NOW.isoformat()
    assert report["available_at_approx"] is False
    assert report["published_at"] == "2026-06-21T05:41:31+00:00"
    assert report["series"]["CBI_LIQUIDITY"]["periods"] == ["1402-12", "1403-12", "1404-12"]
    assert report["series"]["CBI_LIQUIDITY"]["values"][-1] == "155812.2"

    rows = _stored(engine, "CBI_LIQUIDITY")
    assert [r["ref_period_label"] for r in rows] == ["1402-12", "1403-12", "1404-12"]
    assert rows[-1]["value"] == pytest.approx(155812.2)
    assert rows[-1]["ref_period_start"] == date(2026, 2, 20)
    assert rows[-1]["ref_period_end"] == date(2026, 3, 20)
    assert {ensure_utc(r["available_at"]) for r in rows} == {NOW}
    assert {ensure_utc(r["published_at"]) for r in rows} == {EDITIONS["1404-12"][3]}
    assert not any(r["is_projection"] for r in rows)
    assert not any(r["available_at_approx"] for r in rows)
    assert {r["method"] for r in rows} == {None}
    assert {r["method"] for r in _stored(engine, BASE_MONEY_CODE)} == {METHOD_REVISED}


def test_the_series_are_registered_from_the_catalog(engine, tmp_path):
    ingest_edition_file(engine, _file(tmp_path, "1404-12"), now=NOW)
    with engine.connect() as conn:
        units = dict(conn.execute(
            select(instruments.c.code, instruments.c.unit)
            .where(instruments.c.code.in_(CODES))
        ).all())
        series = {
            row.code: row
            for row in conn.execute(select(economic_series).where(economic_series.c.provider_code == "cbi"))
        }
    assert units == {code: "thousand_billion_rial" for code in CODES}
    assert sorted(series) == sorted(CODES)
    assert series["CBI_LIQUIDITY"].measure == "level"
    assert series["CBI_LIQUIDITY"].calendar == "jalali"


def test_the_workbook_is_archived_with_its_hash_edition_and_saved_time(engine, tmp_path):
    report = ingest_edition_file(
        engine, _file(tmp_path, "1404-12"), url="https://www.cbi.ir/page/35570.aspx", now=NOW,
    )
    with engine.connect() as conn:
        documents = [dict(r._mapping) for r in conn.execute(select(source_documents))]
    assert len(documents) == 1
    document = documents[0]
    assert document["provider_code"] == "cbi"
    assert document["content_sha256"] == EDITIONS["1404-12"][2] == report["sha256"]
    assert document["edition_label"] == "1404-12"
    assert ensure_utc(document["document_saved_at"]) == EDITIONS["1404-12"][3]
    assert document["media_type"] == "application/vnd.ms-excel"
    assert document["url"] == "https://www.cbi.ir/page/35570.aspx"
    assert document["byte_size"] == 201216
    assert document["storage_path"] == ""
    for code in CODES:
        assert {r["source_document_id"] for r in _stored(engine, code)} == {document["id"]}


def test_a_workbook_posted_without_its_url_says_so(engine, tmp_path):
    report = ingest_edition_file(engine, _file(tmp_path, "1404-12"), now=NOW)
    assert report["url_reconstructed"] is True
    assert report["url"] == "https://www.cbi.ir/category/2692.aspx"


def test_re_posting_the_same_workbook_writes_nothing(engine, tmp_path):
    path = _file(tmp_path, "1404-12")
    first = ingest_edition_file(engine, path, now=NOW)
    again = ingest_edition_file(engine, path, now=LATER)
    assert again["inserted"] == 0 and again["revised"] == 0
    assert again["unchanged"] == 12
    assert again["document"] == {"id": first["document"]["id"], "status": "existing"}
    assert _count(engine, economic_observations) == 12
    assert _count(engine, source_documents) == 1


def test_the_same_edition_with_new_bytes_is_a_new_document_and_no_new_vintage(engine, tmp_path):
    """CBI replaces files in place (every 1403/01-06 workbook was re-saved on
    2024-12-30). Same edition label: not refused as older. Different bytes: a
    second archive row. Same numbers: no vintage."""
    path = _file(tmp_path, "1404-12")
    ingest_edition_file(engine, path, now=NOW)
    resaved = tmp_path / "140412-resaved.xls"
    resaved.write_bytes(_bytes("1404-12") + b"\x00")
    report = ingest_edition_file(engine, str(resaved), now=LATER)
    assert report["document"]["status"] == "inserted"
    assert report["unchanged"] == 12 and report["inserted"] == report["revised"] == 0
    assert _count(engine, source_documents) == 2


def test_a_revised_print_becomes_a_new_vintage_beside_the_first(engine, tmp_path):
    """Base money at Esfand 1400: 6,039.7 (old method) in the 1400-12 edition,
    5,981.1 (revised) in the 1402-12 edition."""
    ingest_edition_file(engine, _file(tmp_path, "1400-12"), backfill=True, now=NOW)
    report = ingest_edition_file(engine, _file(tmp_path, "1402-12"), backfill=True, now=NOW)

    assert report["revised"] == 1
    revision = report["series"][BASE_MONEY_CODE]["revisions"][0]
    assert revision == {
        "period": "1400-12", "from": 6039.7, "to": "5981.1",
        "from_method": METHOD_OLD, "to_method": METHOD_REVISED, "vintage": 2,
    }
    history = vintages_for_period(engine, BASE_MONEY_CODE, date(2022, 2, 20))
    assert [(h["vintage"], h["value"], h["method"]) for h in history] == [
        (1, pytest.approx(6039.7), METHOD_OLD),
        (2, pytest.approx(5981.1), METHOD_REVISED),
    ]
    # M2 for the same month is the same number in both editions: one vintage.
    assert [h["vintage"] for h in vintages_for_period(engine, "CBI_LIQUIDITY", date(2022, 2, 20))] == [1]


def test_a_method_restated_alone_is_a_new_vintage(engine):
    """The store compares the method the source states, not only the value."""
    spec = spec_for_code(BASE_MONEY_CODE)
    series_id = ensure_series(engine, spec)
    period = dict(ref_period_start=date(2022, 2, 20), ref_period_end=date(2022, 3, 20),
                  ref_period_label="1400-12")
    first = record_observation(engine, series_id, value=Decimal("6039.7"), available_at=NOW,
                               method=METHOD_OLD, **period)
    same = record_observation(engine, series_id, value=Decimal("6039.7"), available_at=LATER,
                              method=METHOD_OLD, **period)
    restated = record_observation(engine, series_id, value=Decimal("6039.7"), available_at=LATER,
                                  method=METHOD_REVISED, **period)
    assert (first.status, same.status, restated.status) == ("inserted", "unchanged", "revised")
    assert restated.vintage == 2 and restated.previous_method == METHOD_OLD


def _without_footnote(edition: str, saved_at=None):
    """The real edition's sheet with CBI's MFSMCG footnote blanked, as a parsed
    workbook with bytes of its own: what a 1405 edition would look like if CBI
    stopped printing the note this parser reads the method from."""
    grid = _rows(edition)
    r, c = _cell(grid, "MFSMCG")
    grid[r][c] = ""
    return parse_sheet(grid), cbi.WorkbookFile(sha256="ab" * 32, byte_size=1, saved_at=saved_at)


def test_a_newer_edition_cannot_un_revise_a_stored_month(engine, tmp_path, monkeypatch):
    """Mehr 1403 stores Esfand 1402's base money on the revised method. The
    Esfand 1404 edition stripped of its footnote reads the same 10,921.3 as
    'old': before, that was written as vintage 2 with method 'old', labelling
    a revised figure old and putting a method break where there is none. It is
    refused whole, and nothing of it is written."""
    ingest_edition_file(engine, _file(tmp_path, "1403-07"), now=NOW)
    before = _count(engine, economic_observations)
    stripped = _without_footnote("1404-12")
    # The lambda reads ``stripped`` when called, so reassigning it below swaps
    # the workbook the next ingest sees.
    monkeypatch.setattr(cbi, "read_workbook", lambda path: stripped)
    with pytest.raises(CbiParseError, match="reads base money at 1402-12 as the OLD method"):
        ingest_edition_file(engine, "140412.xls", now=LATER)
    assert _count(engine, economic_observations) == before
    assert _count(engine, source_documents) == 1
    assert newest_stored_edition(engine) == Edition(1403, 7)
    esfand_1402 = vintages_for_period(engine, BASE_MONEY_CODE, date(2024, 2, 20))
    assert [(h["vintage"], h["method"]) for h in esfand_1402] == [(1, METHOD_REVISED)]

    # Saved AFTER the workbook behind the revised print (Mehr 1403's, 2024-12-07):
    # refused just the same.
    stripped = _without_footnote("1404-12", saved_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    with pytest.raises(CbiParseError, match="saved no later than this one"):
        ingest_edition_file(engine, "140412.xls", now=LATER)

    # A deliberate replay is the operator's call, as with an older edition.
    report = ingest_edition_file(engine, "140412.xls", allow_older=True, now=LATER)
    assert report["series"][BASE_MONEY_CODE]["revisions"][0]["to_method"] == METHOD_OLD


def test_a_workbook_saved_before_the_revision_keeps_the_method_it_printed(
    engine, tmp_path, monkeypatch
):
    """CBI replaced some editions in place after the redefinition (1402-12 and
    1403/01-06 all carry 2024-12-30), not necessarily all. A newer edition
    whose file was saved BEFORE the workbook that printed a month as revised
    predates the redefinition: its old-method figure is what CBI printed then,
    and it is stored as the vintage it is, labelled old."""
    ingest_edition_file(engine, _file(tmp_path, "1403-07"), now=NOW)       # saved 2024-12-07
    early = datetime(2024, 11, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(cbi, "read_workbook", lambda path: _without_footnote("1404-12", early))
    report = ingest_edition_file(engine, "140412.xls", now=LATER)
    revision = report["series"][BASE_MONEY_CODE]["revisions"][0]
    assert (revision["period"], revision["from_method"], revision["to_method"]) == \
        ("1402-12", METHOD_REVISED, METHOD_OLD)


def test_point_in_time_serves_the_print_that_stood_at_the_cutoff(engine, tmp_path):
    """No look-ahead: the backfill's reconstructed availability puts the old
    print before the revision, and a cutoff between them sees the old one."""
    ingest_edition_file(engine, _file(tmp_path, "1400-12"), backfill=True, now=NOW)
    ingest_edition_file(engine, _file(tmp_path, "1402-12"), backfill=True, now=NOW)

    def esfand_1400(as_of):
        series = point_in_time(engine, BASE_MONEY_CODE, as_of=as_of)
        return {o.ref_period_label: (o.value, o.method, o.vintage) for o in series.observations}.get("1400-12")

    assert esfand_1400(datetime(2022, 8, 1, tzinfo=timezone.utc)) is None     # before the edition
    assert esfand_1400(datetime(2023, 1, 1, tzinfo=timezone.utc)) == (pytest.approx(6039.7), METHOD_OLD, 1)
    assert esfand_1400(datetime(2025, 1, 1, tzinfo=timezone.utc)) == (pytest.approx(5981.1), METHOD_REVISED, 2)
    latest = point_in_time(engine, BASE_MONEY_CODE, as_of=datetime(2030, 1, 1, tzinfo=timezone.utc))
    assert latest.vintages == frozenset({1, 2})
    assert all(o.available_at_approx for o in latest.observations)


def test_the_backfill_reconstructs_availability_from_the_saved_time(engine, tmp_path):
    report = ingest_edition_file(engine, _file(tmp_path, "1399-04"), backfill=True, now=NOW)
    saved = EDITIONS["1399-04"][3]
    assert report["published_at"] == saved.isoformat()
    assert report["available_at"] == (saved + timedelta(days=1)).isoformat()
    assert report["available_at_approx"] is True
    rows = _stored(engine, "CBI_LIQUIDITY")
    assert {ensure_utc(r["available_at"]) for r in rows} == {saved + timedelta(days=1)}
    assert all(r["available_at_approx"] for r in rows)
    # collected_at stays the truth about when THIS system wrote them.
    assert {ensure_utc(r["collected_at"]) for r in rows} == {NOW}


def test_backfill_availability_never_runs_backwards_across_editions(engine, tmp_path):
    """The 1403-07 workbook was saved on 2024-12-07; the 1402-12 one, ingested
    before it, was re-saved on 2024-12-30. Availability stays monotone: later
    is safe, earlier would let a backtest read a number before its predecessor."""
    first = ingest_edition_file(engine, _file(tmp_path, "1402-12"), backfill=True, now=NOW)
    second = ingest_edition_file(engine, _file(tmp_path, "1403-07"), backfill=True, now=NOW)
    assert first["available_at"] == "2024-12-31T07:17:53+00:00"
    assert second["saved_at"] == "2024-12-07T09:58:22+00:00"
    assert second["available_at"] == first["available_at"]
    assert second["available_at_approx"] is True


def test_backfill_availability_rules():
    saved = datetime(2026, 6, 21, 5, 41, 31, tzinfo=timezone.utc)
    floor = datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert backfill_available_at(saved, None, NOW) == (saved + timedelta(days=1), True)
    assert backfill_available_at(saved, floor, NOW) == (floor, True)
    # No saved time: nothing to reconstruct from; the observed answer.
    assert backfill_available_at(None, floor, NOW) == (NOW, False)
    # Never later than the moment we have the file.
    assert backfill_available_at(NOW - timedelta(hours=2), None, NOW) == (NOW, False)


def test_an_older_edition_after_a_newer_one_is_refused(engine, tmp_path):
    ingest_edition_file(engine, _file(tmp_path, "1404-12"), now=NOW)
    before = _stored(engine, "CBI_LIQUIDITY")
    with pytest.raises(CbiStaleEditionError) as excinfo:
        ingest_edition_file(engine, _file(tmp_path, "1403-07"), now=LATER)
    message = str(excinfo.value)
    assert "1403-07" in message and "1404-12" in message
    assert "available_at cannot prevent that" in message
    assert _stored(engine, "CBI_LIQUIDITY") == before
    assert _count(engine, source_documents) == 1


def test_a_deliberate_replay_of_an_older_edition_must_be_asked_for(engine, tmp_path):
    ingest_edition_file(engine, _file(tmp_path, "1404-12"), now=NOW)
    report = ingest_edition_file(engine, _file(tmp_path, "1403-07"), allow_older=True, now=LATER)
    # Esfand 1402 is in both editions with the same numbers: unchanged.
    assert report["unchanged"] == 4 and report["inserted"] == 8
    assert newest_stored_edition(engine) == Edition(1404, 12)


def test_a_workbook_filed_under_the_wrong_edition_is_refused(engine, tmp_path):
    with pytest.raises(CbiParseError, match="lists this file as the 1404-11 edition but the workbook's own title says 1404-12"):
        ingest_edition_file(engine, _file(tmp_path, "1404-12"), expected=Edition(1404, 11), now=NOW)
    assert _count(engine, source_documents) == 0


def test_nothing_is_written_when_a_file_is_refused(engine, tmp_path):
    path = tmp_path / "140412.xls"
    path.write_bytes(b"<html>blocked</html>")
    with pytest.raises(CbiParseError):
        ingest_edition_file(engine, str(path), now=NOW)
    assert _count(engine, economic_observations) == 0
    assert _count(engine, source_documents) == 0


def test_a_dry_run_reports_exactly_what_it_would_write_and_writes_nothing(engine, tmp_path):
    report = ingest_edition_file(engine, _file(tmp_path, "1404-12"), dry_run=True, now=NOW)
    assert report["dry_run"] is True and report["inserted"] == 12
    assert _count(engine, economic_observations) == 0
    assert _count(engine, source_documents) == 0
    assert _count(engine, economic_series) == 0
    assert newest_stored_edition(engine) is None


def test_the_newest_stored_edition_ignores_the_history_payload(engine, tmp_path):
    ingest_edition_file(engine, _file(tmp_path, "1403-07"), now=NOW)
    with engine.begin() as conn:
        conn.execute(source_documents.insert().values(
            provider_code="cbi", url="https://tsdview.cis.cbi.ir/", content_sha256="x" * 64,
            media_type="application/json", edition_label="tsdview",
        ))
    assert newest_stored_edition(engine) == Edition(1403, 7)


# --- exact values in the store ------------------------------------------------


def test_a_decimal_reaches_the_driver_unchanged():
    assert stored_value(Decimal("155812.2")) == Decimal("155812.2")
    assert isinstance(stored_value(Decimal("155812.2")), Decimal)
    assert isinstance(stored_value(155812.2), float)
    with pytest.raises(ValueError, match="non-finite"):
        stored_value(Decimal("NaN"))
    # PostgreSQL (psycopg) binds the column without a float conversion, so the
    # Decimal is sent as a numeric literal; SQLite has no decimal type.
    dialect = postgresql.psycopg.dialect()
    column_type = economic_observations.c.value.type.dialect_impl(dialect)
    assert column_type.bind_processor(dialect) is None


def test_record_observation_hands_the_insert_the_exact_decimal(engine, tmp_path):
    captured = []

    @event.listens_for(engine, "before_execute")
    def capture(conn, clause, multiparams, params, execution_options):
        if getattr(clause, "table", None) is economic_observations and clause.is_insert:
            captured.append(clause.compile().params["value"])

    ingest_edition_file(engine, _file(tmp_path, "1404-12"), now=NOW)
    assert Decimal("155812.2") in captured
    assert all(isinstance(value, Decimal) for value in captured)
    assert all(re.fullmatch(r"\d+\.\d", str(value)) for value in captured)


# --- migration 0034 -------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MIGRATION = os.path.join(REPO_ROOT, "database", "migrations", "0034_cbi_monetary")


def _migration(direction):
    with open(f"{MIGRATION}.{direction}.sql", encoding="utf-8") as handle:
        return handle.read()


def test_migration_0034_registers_the_provider_and_its_columns():
    up = _migration("up")
    assert re.search(r"\('cbi','Central Bank of Iran[^']*','https://www\.cbi\.ir','iran_macro',40,TRUE\)", up)
    assert "ON CONFLICT (code) DO NOTHING" in up
    for column in ("edition_label TEXT NOT NULL DEFAULT ''", "document_saved_at TIMESTAMPTZ"):
        assert f"ALTER TABLE source_documents ADD COLUMN {column};" in up
    for column in ("method TEXT", "available_at_approx BOOLEAN NOT NULL DEFAULT FALSE"):
        assert f"ALTER TABLE economic_observations ADD COLUMN {column};" in up
    assert "CHECK (method IS NULL OR method IN ('old','revised'))" in up


def test_migration_0034_down_reverses_every_statement():
    down = _migration("down")
    for column in ("available_at_approx", "method"):
        assert f"ALTER TABLE economic_observations DROP COLUMN IF EXISTS {column};" in down
    for column in ("document_saved_at", "edition_label"):
        assert f"ALTER TABLE source_documents DROP COLUMN IF EXISTS {column};" in down
    assert "DROP CONSTRAINT IF EXISTS economic_obs_method" in down
    assert "DELETE FROM data_providers WHERE code = 'cbi';" in down


def test_the_mirror_carries_the_method_constraint(engine):
    spec = spec_for_code(BASE_MONEY_CODE)
    series_id = ensure_series(engine, spec)
    with pytest.raises(Exception):
        record_observation(
            engine, series_id, ref_period_start=date(2022, 2, 20), ref_period_end=date(2022, 3, 20),
            value=1.0, available_at=NOW, method="new",
        )
