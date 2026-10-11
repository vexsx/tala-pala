"""The Central Bank of Iran's monetary aggregates: discover, parse, verify, archive, store.

What this reads
---------------
گزیده آمارهای اقتصادی (Selected Economic Indicators), its monetary and banking
part, one Excel edition per month since Tir 1399 (before that the editions are
PDF only).  The first sheet, «مانده برخی از متغیرهای عمده پولی و اعتباری در
پایان <month> <year>», prints each aggregate at three month-ends — the same
month last year, last Esfand, and the current month (an Esfand edition prints
Esfand Y-2, Y-1 and Y) — followed by the growth between them.  Four rows are
stored: base money read on its sources side (پایه پولی از سمت منابع), liquidity
read on its supply side (نقدینگی برحسب عوامل موثر بر عرضه), money (پول) and
quasi-money (شبه پول), in thousand billion rials, exactly as printed.

Unlike SCI, this module FETCHES: www.cbi.ir answers the production host
(measured 2026-10-11; 6-20 s a request because the host's resolver times out
before a lookup succeeds, which is why every request here has a 90 s budget
and four attempts with backoff).  :class:`CbiSite` walks the section page →
the year's month list → the month page → its «فرمت Excel» link, and accepts
the response only if it is an OLE2 compound file (``d0cf11e0``).  An HTML page
where a workbook should be is a challenge or an error page: the run fails and
says so, and nothing is retried past it or worked around.

THE HAZARDS THE PARSER EXISTS TO HANDLE (each seen in a real edition)
---------------------------------------------------------------------
1. *Nothing is where it was.*  The sheet is «Sheet1» (1399), «متغیرهای عمده» or
   «متغیرهای پولی» (Khordad 1400); the levels sit in columns 4-6 or 3-5; the
   header is on row 5 or 6; the period is a cell of its own («اسفند1404», no
   space) or the tail of the title itself (Esfand 1400); the row above the
   years carries month names, ``0.0`` (Esfand 1401/1402) or the «مانده» header
   itself (Esfand 1400).  So the sheet is found by its title, the columns by
   their «مانده»/«رشد» headers and the year row, the months by name, and the
   rows by label — never by a fixed row or column.
2. *The labels are not spelled the same way twice.*  Arabic yeh/kaf beside
   Persian ones, ZWNJ (``تشکيل‌دهنده``), footnote markers («پايه پولي (1)»,
   «نقدينگي(1)»), and qualifiers («(کاملا مقدماتی)», fully preliminary).  Both
   sides are folded by :func:`label_key` and compared with EXACT equality.
3. *The columns must be the months they claim.*  The parsed (year, month) of
   the three level columns must be exactly the three the edition's own title
   implies, in order, or the workbook is refused: a header misread by one
   column would otherwise store every value one period off.
4. *The printed numbers must agree with each other.*  Before anything is
   written: base money on the sources side equals the uses side; liquidity by
   supply factors equals liquidity by components; liquidity equals money plus
   quasi-money; and the growth CBI prints next to each of the four rows equals
   the growth of the levels it printed (``#`` stands for growth beyond ±500%
   and is accepted only there).  A parse that bound a row to the wrong label
   or a column to the wrong month cannot pass all of them.
5. *The unit is stated, and checked.*  «(هزار میلیارد ریال- درصد- واحد درصد)».
   Anything else — the PDFs of 1390 and earlier are in billion rials — is
   refused, never rescaled.

AND THE RULE THAT TIES THEM TOGETHER
------------------------------------
If ANY of it fails, the WHOLE workbook is refused and nothing is written —
the same rule :mod:`app.economic.sci` states, for the same reason: a layout
change that broke one row must not ingest the other three and look
successful.

What a stored row claims
------------------------
* ``value``: the printed number as an exact :class:`~decimal.Decimal`
  (155,812.2 is stored as 155812.2, not as the nearest binary float).
* ``method`` (base money only): ``'revised'`` when the edition carries CBI's
  footnote on the MFSMCG 2016 redefinition and the month is on or after the
  start the footnote states («از اسفند 1399 به بعد» / «از سال 1400 به بعد»),
  ``'old'`` otherwise.  A footnote that mentions MFSMCG without a start this
  can read refuses the workbook: a method that cannot be stated is not
  guessed.  ``None`` for the three other series.  An edition that would
  restate a month the store holds as ``'revised'`` as ``'old'`` is refused
  too, unless its workbook was saved before the one that printed the revised
  figure (a file CBI never re-saved after the redefinition: what it printed
  then) or it is replayed deliberately with ``allow_older``.  CBI does not
  un-revise a month, so a later workbook doing so is a footnote this parser
  can no longer find, and storing it would put a method break where there is
  none.
* ``published_at``: the workbook's own OLE "last saved" time — CBI states no
  publication date, and a file cannot have been published before it was saved
  (140412.xls: 2026-06-21 05:41:31 UTC, the day CBI's news item announced it).
* ``available_at``: when THIS system had the file (a live run), or, for the
  backfill of editions published before this system read the CBI, the
  last-saved time plus one day — never earlier than the newest availability
  already stored from an edition, so availability stays monotone — flagged
  ``available_at_approx``.  See :func:`backfill_available_at`.

Editions are ingested oldest first, and an edition OLDER than the newest one
already stored is refused (:class:`CbiStaleEditionError`): available_at would
stamp its older numbers as the newest vintage.  The same edition re-posted is
idempotent, and the same edition with different bytes (CBI replaces files in
place: every 1403/01-06 workbook was re-saved on 2024-12-30) writes a new
vintage wherever a number changed.
"""
from __future__ import annotations

import html as html_module
import io
import logging
import os
import re
import struct
import tempfile
import threading
import time as time_module
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Optional, Sequence, Union

import httpx
import xlrd
from sqlalchemy import and_, func, select
from sqlalchemy.engine import Connection, Engine

from ..db import (
    data_providers,
    economic_observations,
    ensure_utc,
    source_documents,
    utcnow,
)
from ..metrics import JOB_LAST_SUCCESS
from ..providers.base import Provider, ProviderError
from .catalog import BASE_MONEY_CODE, CBI_PROVIDER, cbi_specs
from .jalali import (
    JALALI_MONTHS,
    fold_digits,
    jalali_month_span,
    month_number,
    normalize_fa,
    period_label,
)
from .store import (
    STATUS_INSERTED,
    STATUS_RACED,
    STATUS_REVISED,
    STATUS_UNCHANGED,
    ensure_series,
    file_sha256_and_size,
    record_observation,
    record_source_document,
)

log = logging.getLogger(__name__)

JOB_NAME = "cbi_monetary"

BASE_URL = "https://www.cbi.ir"
# The monetary part of گزیده آمارهای اقتصادی: its year dropdown and the newest
# year's month list.
SECTION_URL = BASE_URL + "/category/2692.aspx"
MEDIA_TYPE_XLS = "application/vnd.ms-excel"
# The OLE2 compound-file signature every legacy .xls starts with.
OLE_SIGNATURE = bytes.fromhex("d0cf11e0a1b11ae1")

# The first edition CBI published as Excel (Tir 1399). Every earlier month is a
# PDF linked straight from the year page, and is not read here.
FIRST_EXCEL_EDITION = (1399, 4)

# Network budget, sized for the production host: a lookup alone took 5 s there
# and whole requests 6-20 s (2026-10-11). Four attempts, 5/10/20 s apart, so a
# resolver that fails twice in a row still gets through.
TIMEOUT_SECONDS = 90.0
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 5.0
COURTESY_DELAY_SECONDS = 1.0

# The backfill's reconstructed availability: the workbook's last-saved time
# plus this. A day, because "saved" is not "posted": 140412.xls was saved at
# 05:41 UTC and announced the same day, but a file saved in the evening could
# appear the next morning.
BACKFILL_AVAILABILITY_LAG = timedelta(days=1)

# CBI prints one decimal. Two printed numbers that are each rounded to ±0.05
# and a printed total rounded to ±0.05 can differ by 0.15 without anything
# being wrong; anything further apart is.
ACCOUNTING_TOLERANCE = Decimal("0.15")
# Growth is printed to one decimal (±0.05) and recomputed here from levels that
# are themselves rounded; over every captured edition the largest difference
# for the four rows read was 0.05 (measured 2026-10-11, 51 workbooks).
GROWTH_TOLERANCE = Decimal("0.1")
# CBI's footnote: «نشانه # برای رشدهای بالاتر و پایینتر از 500 درصد می‌باشد».
GROWTH_OVERFLOW_MARK = "#"
GROWTH_OVERFLOW_PCT = Decimal(500)

METHOD_OLD = "old"
METHOD_REVISED = "revised"
METHOD_FOOTNOTE_MARK = "MFSMCG"

UNIT_THOUSAND_BILLION_RIAL = "thousand_billion_rial"

# How far down the sheet the title, unit and header rows can be. They are on
# rows 2-6 in every edition seen; 12 leaves room without reaching the data.
HEADER_SCAN_ROWS = 12

MIN_JALALI_YEAR = 1300
MAX_JALALI_YEAR = 1500


class CbiParseError(ValueError):
    """The workbook is not the shape this parser was written for, or its
    printed numbers do not agree with each other."""


class CbiStaleEditionError(ValueError):
    """The edition is older than one already ingested.

    ``available_at`` is when THIS system had the file, which is right for
    point-in-time reads and is exactly why it cannot catch this: an Azar
    workbook ingested today is stamped today, passes
    :func:`app.economic.store.record_observation`'s monotonicity guard, and
    writes its OLDER numbers as the newest vintage.  The edition label the
    workbook prints in its own title is what detects it.
    """


class CbiBackfillRequiredError(ValueError):
    """A fetch of the newest editions was asked for, and nothing is stored yet.

    The first population is the backfill (``backfill_from``), which stamps the
    69 editions published before this system read the CBI with reconstructed
    availability.  Ingesting today's edition first would make every older one
    "older than an edition already stored" and refuse it.
    """


class CbiFetchError(RuntimeError):
    """www.cbi.ir did not give us the document: network, markup or content."""


class CbiRunInProgressError(RuntimeError):
    """Another CBI ingest is running in this service right now.

    Two runs must not interleave. The one-off backfill takes about half an
    hour and is started by hand; if the scheduled run (or a posted workbook)
    lands in the middle of it, that run reads the backfill's progress as "the
    newest stored edition", ingests every later edition stamped with TODAY's
    availability instead of the reconstructed one, and the backfill's next
    edition is then refused as older than stored. So the second caller is told
    to come back, and nothing is written.
    """


# --- text --------------------------------------------------------------------

_QUALIFIER_RE = re.compile(r"\([^()]*\)|[*#]")
_MONTH_YEAR_RE = re.compile(
    r"(" + "|".join(normalize_fa(name) for name in JALALI_MONTHS) + r")\s*(\d{4})"
)


def label_key(text: object) -> str:
    """Fold a row label to the form two spellings of it share.

    :func:`app.economic.jalali.normalize_fa` (Arabic/Persian letters, ZWNJ,
    invisible marks), then drops every parenthesised part — footnote numbers
    «(1)» and qualifiers «(کاملا مقدماتی)» — and stray ``*``/``#`` markers, then
    all whitespace, because CBI writes «تشکیل‌دهنده» with a ZWNJ and could as
    easily write it with a space.  Comparison on the result is still EXACT:
    «پول» and «شبه پول» stay different keys, and no label matches by
    containing another.
    """
    folded = fold_digits(normalize_fa(text))
    folded = _QUALIFIER_RE.sub(" ", folded)
    return re.sub(r"\s+", "", folded)


def _title_key() -> str:
    return label_key("متغیرهای عمده پولی و اعتباری")


TITLE_KEY = _title_key()


# --- records -----------------------------------------------------------------


@dataclass(frozen=True, order=True)
class Edition:
    """One monthly edition, named by the month it reports (its title's month)."""

    year: int
    month: int

    @property
    def label(self) -> str:
        return period_label(self.year, self.month)

    @property
    def is_esfand(self) -> bool:
        return self.month == 12

    @classmethod
    def parse(cls, label: str) -> "Edition":
        """``'1404-12'`` -> ``Edition(1404, 12)``; refuses anything else."""
        match = re.fullmatch(r"(\d{4})-(\d{2})", (label or "").strip())
        if not match:
            raise ValueError(f"{label!r} is not an edition label of the form YYYY-MM")
        year, month = int(match.group(1)), int(match.group(2))
        if not (MIN_JALALI_YEAR <= year <= MAX_JALALI_YEAR and 1 <= month <= 12):
            raise ValueError(f"{label!r} is not a Jalali month")
        return cls(year, month)

    def columns(self) -> tuple[tuple[int, int], ...]:
        """The three (year, month) level columns this edition prints, in order.

        A monthly edition: the same month last year, last Esfand, this month.
        An Esfand edition: Esfand two years back, last year, this year.
        """
        if self.is_esfand:
            return ((self.year - 2, 12), (self.year - 1, 12), (self.year, 12))
        return ((self.year - 1, self.month), (self.year - 1, 12), (self.year, self.month))


def excel_can_carry(year: int, month: int) -> bool:
    """True when some Excel edition prints the stock at the end of this month.

    Derived from :meth:`Edition.columns` and :data:`FIRST_EXCEL_EDITION`
    rather than restated: month ``p`` is printed by the edition of ``p``
    itself, by the edition of the same month a year later (if ``p`` is not
    Esfand), and — for an Esfand — by every edition of the following year and
    by the Esfand edition two years on.  So Tir 1398 onwards is covered, plus
    Esfand 1397 (the Esfand 1399 edition's oldest column), and Farvardin to
    Khordad 1398 is not.  The quarterly history (:mod:`app.economic.cbi_history`)
    writes only months this returns False for, so the two sources never write
    the same period.
    """
    first = Edition(*FIRST_EXCEL_EDITION)
    candidates = [Edition(year, month)]
    if month == 12:
        candidates += [Edition(year + 1, 12), Edition(year + 2, 12)]
    else:
        candidates.append(Edition(year + 1, month))
    return any(
        candidate >= first and (year, month) in candidate.columns()
        for candidate in candidates
    )


@dataclass(frozen=True)
class CbiObservation:
    """One end-of-month stock as an edition prints it."""

    code: str
    year: int
    month: int
    value: Decimal
    method: Optional[str] = None

    @property
    def ref_period_label(self) -> str:
        return period_label(self.year, self.month)


@dataclass(frozen=True)
class ParsedEdition:
    """Everything one workbook says, verified, before any storage decision."""

    edition: Edition
    unit: str
    sheet_name: str
    # code -> its three observations, oldest month first.
    observations: dict[str, tuple[CbiObservation, ...]]
    base_money_method: str               # the edition's newest base-money month
    revision_start: Optional[tuple[int, int]]
    checks: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkbookFile:
    """The bytes' identity: hash, size and the timestamp they carry about themselves."""

    sha256: str
    byte_size: int
    saved_at: Optional[datetime]


# --- the workbook's own metadata ---------------------------------------------

# [MS-OLEPS] 2.21: PIDSI_LASTSAVE_DTM in the SummaryInformation property set,
# stored as a VT_FILETIME (0x0040): 100-ns intervals since 1601-01-01 UTC.
_PIDSI_LASTSAVE_DTM = 13
_VT_FILETIME = 0x0040
_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


def saved_at_from_summary(stream: bytes) -> Optional[datetime]:
    """The last-saved time from an OLE ``\\x05SummaryInformation`` stream.

    Read from the property-set layout [MS-OLEPS] describes: a 28-byte header,
    then (FMTID, offset) of the first property set, whose section starts with
    its size and property count followed by (property id, offset) pairs.
    ``None`` when the stream is short, the property is absent or is not a
    FILETIME, or the value is zero — an absent date is reported as absent.
    """
    try:
        section = struct.unpack_from("<I", stream, 44)[0]
        _size, count = struct.unpack_from("<II", stream, section)
        for index in range(count):
            prop_id, offset = struct.unpack_from("<II", stream, section + 8 + 8 * index)
            if prop_id != _PIDSI_LASTSAVE_DTM:
                continue
            vtype, filetime = struct.unpack_from("<IQ", stream, section + offset)
            if vtype != _VT_FILETIME or filetime == 0:
                return None
            return _FILETIME_EPOCH + timedelta(microseconds=filetime // 10)
    except struct.error:
        return None
    return None


def workbook_saved_at(data: bytes) -> Optional[datetime]:
    """The workbook's own "last saved" time, from its OLE container."""
    try:
        container = xlrd.compdoc.CompDoc(data, logfile=io.StringIO())
        stream = container.get_named_stream("\x05SummaryInformation")
    except Exception:  # noqa: BLE001 - xlrd raises CompDocError and struct errors alike
        return None
    if not stream:
        return None
    return saved_at_from_summary(stream)


# --- parsing -----------------------------------------------------------------


def _is_number(cell: object) -> bool:
    return isinstance(cell, (int, float)) and not isinstance(cell, bool)


def _exact(cell: object) -> Decimal:
    """The printed number as an exact decimal: ``155812.2`` -> ``Decimal('155812.2')``.

    xlrd hands back a binary float. ``repr`` of a float is the shortest string
    that round-trips to it, which for a cell CBI typed as 155812.2 is exactly
    "155812.2" — so the Decimal is the number as printed, not the float's
    binary expansion (155812.20000000001164...).
    """
    return Decimal(repr(float(cell)))


def _year(cell: object) -> Optional[int]:
    if _is_number(cell) and float(cell).is_integer():
        year = int(cell)
        if MIN_JALALI_YEAR <= year <= MAX_JALALI_YEAR:
            return year
    return None


def _row_texts(row: Sequence[object]) -> list[tuple[int, str]]:
    return [
        (col, fold_digits(normalize_fa(cell)))
        for col, cell in enumerate(row)
        if isinstance(cell, str) and cell.strip()
    ]


def _find_title_row(grid: list[list[object]]) -> int:
    for index, row in enumerate(grid[:HEADER_SCAN_ROWS]):
        if any(TITLE_KEY in label_key(text) for _, text in _row_texts(row)):
            return index
    raise CbiParseError(
        "the title «مانده برخی از متغیرهای عمده پولی و اعتباری در پایان …» is not "
        f"in the first {HEADER_SCAN_ROWS} rows; this is not the sheet this parser "
        "reads"
    )


def _edition_from_title(row: Sequence[object]) -> Edition:
    found = set()
    for _, text in _row_texts(row):
        for name, year in _MONTH_YEAR_RE.findall(text):
            found.add(Edition(int(year), month_number(name) or 0))
    if len(found) != 1:
        stated = ", ".join(sorted(edition.label for edition in found)) or "none"
        raise CbiParseError(
            f"the title row must name exactly one month and year; it names {stated}"
        )
    edition = found.pop()
    if not (MIN_JALALI_YEAR <= edition.year <= MAX_JALALI_YEAR) or edition.month == 0:
        raise CbiParseError(f"the title names {edition.label}, which is not a Jalali month")
    return edition


_UNIT_RE = re.compile(r"((?:هزار\s*میلیارد|میلیارد|میلیون|هزار)\s*)?(ریال|تومان)")
_KNOWN_UNITS = {"هزارمیلیاردریال": UNIT_THOUSAND_BILLION_RIAL}


def _unit(grid: list[list[object]], until_row: int) -> str:
    """The unit the sheet states in its header, or a refusal.

    Every edition read states «هزار میلیارد ریال» (thousand billion rials). A
    different unit is refused rather than converted: the stored series is
    declared in exactly this unit, and a rescale applied here is a number this
    system made up, not one CBI printed.
    """
    stated = set()
    for row in grid[: until_row + 1]:
        for _, text in _row_texts(row):
            for match in _UNIT_RE.finditer(text):
                stated.add(re.sub(r"\s+", " ", match.group(0)).strip())
    if not stated:
        raise CbiParseError(
            "the sheet states no unit (expected «هزار میلیارد ریال»); refusing to "
            "guess what the numbers are in"
        )
    if len(stated) > 1:
        raise CbiParseError(f"the sheet states more than one unit: {', '.join(sorted(stated))}")
    phrase = stated.pop()
    unit = _KNOWN_UNITS.get(phrase.replace(" ", ""))
    if unit is None:
        raise CbiParseError(
            f"the sheet is in «{phrase}», not «هزار میلیارد ریال». These series are "
            "stored in thousand billion rials exactly as printed; refusing rather "
            "than rescaling"
        )
    return unit


def _find_year_row(grid: list[list[object]], start: int) -> int:
    for index in range(start, min(len(grid), start + HEADER_SCAN_ROWS)):
        if sum(1 for cell in grid[index][1:] if _year(cell) is not None) >= 3:
            return index
    raise CbiParseError("no header row of Jalali years was found under the title")


def _header_column(grid, rows: range, predicate, what: str) -> int:
    columns = sorted(
        col
        for index in rows
        for col, text in _row_texts(grid[index])
        if predicate(text)
    )
    if not columns:
        raise CbiParseError(f"the {what} header was not found above the year row")
    return columns[0]


def _level_columns(
    grid: list[list[object]], title_row: int, year_row: int, edition: Edition
) -> tuple[list[tuple[int, int, int]], int]:
    """``([(column, year, month)], growth column)``: the three balance columns,
    verified against the edition, and where the printed growth begins."""
    header_rows = range(title_row, year_row)
    # Exact, because the title itself begins «مانده برخی …»; «مانده در پایان» is
    # how CBI heads the same block on its other sheets.
    first = _header_column(
        grid, header_rows, lambda t: t in ("مانده", "مانده در پایان"), "«مانده» (balance)"
    )
    growth = _header_column(
        grid, header_rows, lambda t: t.startswith("رشد"), "«رشد» (growth)"
    )
    if growth <= first:
        raise CbiParseError(
            f"the growth header (column {growth}) is not to the right of the balance "
            f"header (column {first})"
        )
    years = grid[year_row]
    months = grid[year_row - 1] if year_row > 0 else []
    columns = []
    for col in range(first, growth):
        year = _year(years[col]) if col < len(years) else None
        if year is None:
            continue
        month = month_number(months[col]) if col < len(months) else None
        if month is None:
            # An Esfand edition prints years only; the row above holds nothing,
            # 0.0, or the «مانده» header. Any other edition must name its months.
            if not edition.is_esfand:
                raise CbiParseError(
                    f"column {col} has the year {year} but no month name above it, "
                    f"in the {edition.label} edition (only an Esfand edition omits them)"
                )
            month = 12
        columns.append((col, year, month))
    found = tuple((year, month) for _, year, month in columns)
    if found != edition.columns():
        raise CbiParseError(
            f"the {edition.label} edition must print the balances at "
            f"{', '.join(period_label(*p) for p in edition.columns())}; its header "
            f"says {', '.join(period_label(*p) for p in found) or 'nothing'}. The "
            "layout changed; refusing rather than storing values against the wrong "
            "months"
        )
    return columns, growth


# The rows read. The four series come from the catalog, so the label a spec
# advertises and the label looked for here cannot drift apart; the two check
# rows are only ever compared, never stored.
CHECK_BASE_MONEY_USES = "base_money_uses"
CHECK_M2_COMPONENTS = "m2_components"
_CHECK_LABELS = {
    CHECK_BASE_MONEY_USES: "پايه پولي از سمت مصارف",
    CHECK_M2_COMPONENTS: "نقدينگي برحسب اجزاي تشکيل‌دهنده",
}
M2_CODE = "CBI_LIQUIDITY"
M1_CODE = "CBI_MONEY_M1"
QUASI_CODE = "CBI_QUASI_MONEY"


def _row_targets() -> dict[str, str]:
    targets: dict[str, str] = {}
    labelled = [(spec.indicator, spec.code) for spec in cbi_specs()] + [
        (label, key) for key, label in _CHECK_LABELS.items()
    ]
    for label, key in labelled:
        folded = label_key(label)
        if not folded:
            raise CbiParseError(f"{key} has an empty row label")
        if folded in targets:
            raise CbiParseError(
                f"{targets[folded]} and {key} both claim the row label {label!r}"
            )
        targets[folded] = key
    return targets


ROW_TARGETS: dict[str, str] = _row_targets()


def _row_label(row: Sequence[object], before: int) -> str:
    """The rightmost text left of the first balance column.

    The label column moves with the layout (3 in most editions, 2 when the
    whole table sits one column further left) and the column left of it holds
    a group heading on some rows («پايه پولي (1)», «نقدينگي»), so the label is
    the text nearest the numbers, not a fixed column.
    """
    texts = [text for col, text in _row_texts(row) if col < before]
    return texts[-1] if texts else ""


def _growth_cell(cell: object) -> Optional[Decimal]:
    if _is_number(cell):
        return _exact(cell)
    return None


def _check_growth(
    key: str,
    row: Sequence[object],
    levels: list[Decimal],
    growth_cols: tuple[int, int],
    edition: Edition,
    worst: dict[str, Decimal],
) -> None:
    """The two printed growth rates against the growth of the printed levels.

    A monthly edition prints this month over the same month last year, then
    over last Esfand; an Esfand edition prints last year's Esfand over the one
    before, then this Esfand over last (``levels`` are oldest first either way).
    """
    oldest, last_esfand, current = levels
    if edition.is_esfand:
        pairs = ((last_esfand, oldest), (current, last_esfand))
    else:
        pairs = ((current, oldest), (current, last_esfand))
    for col, (numerator, denominator) in zip(growth_cols, pairs):
        computed = (numerator / denominator - 1) * 100
        cell = row[col] if col < len(row) else ""
        printed = _growth_cell(cell)
        if printed is None:
            if isinstance(cell, str) and cell.strip() == GROWTH_OVERFLOW_MARK:
                if abs(computed) > GROWTH_OVERFLOW_PCT:
                    continue
                raise CbiParseError(
                    f"{key}: CBI prints '#' (growth beyond ±500%) in column {col}, but "
                    f"the printed levels give {computed:.1f}%"
                )
            raise CbiParseError(
                f"{key}: the growth cell in column {col} is {cell!r}, not a number; "
                "the printed growth cannot be checked against the levels"
            )
        gap = abs(printed - computed)
        worst["growth"] = max(worst["growth"], gap)
        if gap > GROWTH_TOLERANCE:
            raise CbiParseError(
                f"{key}: CBI prints {printed}% growth in column {col}, but its own "
                f"levels give {computed:.2f}%. A row or column is bound to the wrong "
                "label or month; refusing the workbook"
            )


_REVISION_START_RE = re.compile(
    r"از\s*(?:سال\s*(\d{4})|("
    + "|".join(normalize_fa(name) for name in JALALI_MONTHS)
    + r")\s*(\d{4}))\s*به\s*بعد"
)


def _revision_start(grid: list[list[object]]) -> Optional[tuple[int, int]]:
    """Where CBI's base-money redefinition starts, as its footnote states it.

    ``None`` when no cell mentions MFSMCG: the edition predates the
    redefinition and every base-money month in it is on the old method.
    """
    notes = [
        fold_digits(normalize_fa(cell))
        for row in grid
        for cell in row
        if isinstance(cell, str) and METHOD_FOOTNOTE_MARK in cell
    ]
    if not notes:
        return None
    starts = set()
    for note in notes:
        for year, month_name, month_year in _REVISION_START_RE.findall(note):
            if year:
                starts.add((int(year), 1))
            else:
                starts.add((int(month_year), month_number(month_name) or 0))
    if len(starts) != 1:
        raise CbiParseError(
            "the workbook's footnote names the MFSMCG 2016 redefinition of base money "
            f"but states {'no' if not starts else 'more than one'} start this parser "
            "can read («از اسفند 1399 به بعد», «از سال 1400 به بعد»). The method of "
            "each base-money month cannot be stated, so the workbook is refused "
            "rather than guessed"
        )
    return starts.pop()


def parse_sheet(rows: Iterable[Iterable[object]], sheet_name: str = "") -> ParsedEdition:
    """Parse and verify the rows of the first sheet.

    ``rows`` is what xlrd's ``row_values`` yields — ``''`` for an empty cell,
    floats for numbers — so this half is testable from row grids and the file
    half stays trivial.
    """
    grid = [list(row) for row in rows]
    title_row = _find_title_row(grid)
    edition = _edition_from_title(grid[title_row])
    year_row = _find_year_row(grid, title_row + 1)
    unit = _unit(grid, year_row)
    columns, growth_col = _level_columns(grid, title_row, year_row, edition)
    first_col = columns[0][0]
    # The first two columns under «رشد در پایان»: growth to the current month
    # from the same month last year and from last Esfand (an Esfand edition:
    # the last two years' Esfand-over-Esfand growth). The two «سهم از رشد»
    # columns after them are contributions, and are not read.
    growth_cols = (growth_col, growth_col + 1)

    found: dict[str, tuple[int, list[Decimal]]] = {}
    for index in range(year_row + 1, len(grid)):
        row = grid[index]
        key = ROW_TARGETS.get(label_key(_row_label(row, first_col)))
        if key is None:
            continue
        if key in found:
            raise CbiParseError(
                f"{key}: the label appears on rows {found[key][0]} and {index}; "
                "refusing to choose between them"
            )
        levels = []
        for col, year, month in columns:
            cell = row[col] if col < len(row) else ""
            if not _is_number(cell):
                raise CbiParseError(
                    f"{key}: the balance at {period_label(year, month)} (row {index}, "
                    f"column {col}) is {cell!r}, not a number"
                )
            value = _exact(cell)
            if value <= 0:
                raise CbiParseError(
                    f"{key}: the balance at {period_label(year, month)} is {value}; a "
                    "monetary aggregate is positive"
                )
            levels.append(value)
        found[key] = (index, levels)

    missing = sorted(set(ROW_TARGETS.values()) - set(found))
    if missing:
        # Refuse the WHOLE workbook: a layout change that moved one row must not
        # ingest the rest and look like a success.
        raise CbiParseError(
            f"expected rows not found in the workbook: {', '.join(missing)}. The "
            "sheet layout or its labels changed; refusing the whole file rather than "
            "ingesting a partial set"
        )

    worst = {"accounting": Decimal(0), "growth": Decimal(0)}

    def agree(what: str, left: list[Decimal], right: list[Decimal]) -> None:
        for (col, year, month), a, b in zip(columns, left, right):
            gap = abs(a - b)
            worst["accounting"] = max(worst["accounting"], gap)
            if gap > ACCOUNTING_TOLERANCE:
                raise CbiParseError(
                    f"{what} at {period_label(year, month)}: {a} against {b} (column "
                    f"{col}). CBI's own accounting does not close on what was read; "
                    "refusing the workbook"
                )

    levels = {key: values for key, (_, values) in found.items()}
    agree(
        "base money, sources against uses",
        levels[BASE_MONEY_CODE], levels[CHECK_BASE_MONEY_USES],
    )
    agree(
        "liquidity, supply side against components",
        levels[M2_CODE], levels[CHECK_M2_COMPONENTS],
    )
    agree(
        "liquidity against money + quasi-money",
        levels[M2_CODE],
        [m1 + quasi for m1, quasi in zip(levels[M1_CODE], levels[QUASI_CODE])],
    )
    for code in (BASE_MONEY_CODE, M2_CODE, M1_CODE, QUASI_CODE):
        _check_growth(code, grid[found[code][0]], levels[code], growth_cols, edition, worst)

    revision_start = _revision_start(grid)

    def method_of(year: int, month: int) -> str:
        if revision_start is not None and (year, month) >= revision_start:
            return METHOD_REVISED
        return METHOD_OLD

    observations: dict[str, tuple[CbiObservation, ...]] = {}
    for spec in cbi_specs():
        observations[spec.code] = tuple(
            CbiObservation(
                code=spec.code,
                year=year,
                month=month,
                value=value,
                method=method_of(year, month) if spec.code == BASE_MONEY_CODE else None,
            )
            for (_, year, month), value in zip(columns, levels[spec.code])
        )
    return ParsedEdition(
        edition=edition,
        unit=unit,
        sheet_name=sheet_name,
        observations=observations,
        base_money_method=method_of(edition.year, edition.month),
        revision_start=revision_start,
        checks={
            # The largest disagreement each check found, reported so a workbook
            # that passes close to a tolerance is visible before one fails it.
            "accounting_max_gap": str(worst["accounting"].quantize(Decimal("0.0001"))),
            "growth_max_gap_pct": str(worst["growth"].quantize(Decimal("0.0001"))),
            "rows": {key: index for key, (index, _) in sorted(found.items())},
            "columns": [period_label(year, month) for _, year, month in columns],
        },
    )


def read_workbook_bytes(data: bytes, name: str = "") -> tuple[ParsedEdition, Optional[datetime]]:
    """Parse a whole ``.xls`` held in memory; return it and its last-saved time.

    The sheet is the one whose title names «متغیرهای عمده پولی و اعتباری» —
    found by what it says, because its NAME has been «Sheet1», «متغیرهای عمده»
    and «متغیرهای پولی».  Anything that is not an OLE2 compound file is refused
    before xlrd sees it, so an HTML error page cannot be "parsed".
    """
    label = name or "the document"
    if not data.startswith(OLE_SIGNATURE):
        head = data[:64].lstrip().lower()
        what = (
            "an HTML page (a challenge or an error page)"
            if head.startswith((b"<", b"<!doctype"))
            else f"bytes starting {data[:8].hex() or 'with nothing'}"
        )
        raise CbiParseError(
            f"{label} is not an Excel 97-2003 workbook: it is {what}, not an OLE2 "
            "compound file (d0cf11e0)"
        )
    try:
        book = xlrd.open_workbook(file_contents=data, logfile=io.StringIO(), on_demand=True)
    except Exception as exc:  # noqa: BLE001 - xlrd raises several unrelated types
        raise CbiParseError(f"{label} is not a readable .xls workbook: {exc}") from exc
    try:
        for index in range(book.nsheets):
            sheet = book.sheet_by_index(index)
            head = [sheet.row_values(r) for r in range(min(sheet.nrows, HEADER_SCAN_ROWS))]
            if any(TITLE_KEY in label_key(text) for row in head for _, text in _row_texts(row)):
                rows = [sheet.row_values(r) for r in range(sheet.nrows)]
                parsed = parse_sheet(rows, sheet_name=sheet.name)
                break
            book.unload_sheet(index)
        else:
            raise CbiParseError(
                f"no sheet of {label} carries the title «متغیرهای عمده پولی و "
                f"اعتباری»; its sheets are {', '.join(book.sheet_names())}"
            )
    finally:
        book.release_resources()
    return parsed, workbook_saved_at(data)


def read_workbook(path: str) -> tuple[ParsedEdition, WorkbookFile]:
    """Open the ``.xls`` at ``path``; parse it and identify its bytes."""
    if not os.path.isfile(path):
        # Not a CbiParseError: nothing has been said about the DOCUMENT, only
        # about the caller's path, and the endpoint keeps the two apart.
        raise FileNotFoundError(f"no workbook at {path}")
    with open(path, "rb") as handle:
        data = handle.read()
    parsed, saved_at = read_workbook_bytes(data, os.path.basename(path))
    sha256, size = file_sha256_and_size(path)
    return parsed, WorkbookFile(sha256=sha256, byte_size=size, saved_at=saved_at)


# --- what is already stored --------------------------------------------------


def _edition_documents():
    """CBI archive rows that are monthly editions (labelled 'YYYY-MM')."""
    return and_(
        source_documents.c.provider_code == CBI_PROVIDER,
        source_documents.c.media_type == MEDIA_TYPE_XLS,
    )


def _connection(bind: Union[Engine, Connection]):
    return bind.connect() if isinstance(bind, Engine) else nullcontext(bind)


def newest_stored_edition(bind: Union[Engine, Connection]) -> Optional[Edition]:
    """The newest edition this deployment has ingested, or ``None``.

    Read off the archive: an edition's document row is written in the same
    transaction as its observations, so it exists exactly when the edition
    was ingested — including an edition whose every value was unchanged — and
    a run that failed (or a dry run) rolled the row back with the rest.
    ``'YYYY-MM'`` labels sort chronologically as text.
    """
    with _connection(bind) as conn:
        label = conn.execute(
            select(func.max(source_documents.c.edition_label))
            .where(_edition_documents())
        ).scalar()
    return Edition.parse(label) if label else None


def newest_edition_available_at(conn: Connection) -> Optional[datetime]:
    """The newest ``available_at`` stored from any monthly edition."""
    value = conn.execute(
        select(func.max(economic_observations.c.available_at))
        .select_from(
            economic_observations.join(
                source_documents,
                economic_observations.c.source_document_id == source_documents.c.id,
            )
        )
        .where(_edition_documents())
    ).scalar()
    return ensure_utc(value)


def backfill_available_at(
    saved_at: Optional[datetime], floor: Optional[datetime], now: datetime
) -> tuple[datetime, bool]:
    """``(available_at, reconstructed?)`` for an edition ingested by the backfill.

    The edition existed before this system read the CBI, so "when did we first
    have it" (today) is true and useless to a backtest.  The reconstruction is
    the workbook's last-saved time plus :data:`BACKFILL_AVAILABILITY_LAG`, and
    it is moved LATER, never earlier, in two cases:

    * it is never before ``floor``, the newest availability already stored from
      an edition: editions are ingested oldest first, a workbook can be re-saved
      long after it was published (the 1403/01-06 files carry 2024-12-30), and
      :func:`~app.economic.store.record_observation` refuses a vintage that
      claims to precede the print it revises.  Later is always safe; it only
      makes a backtest see the number later than it might have;
    * it is never after ``now``: we have the file now.

    With no last-saved time there is nothing to reconstruct from, and the
    answer is the observed one (``now``, not reconstructed).
    """
    if saved_at is None:
        return now, False
    candidate = ensure_utc(saved_at) + BACKFILL_AVAILABILITY_LAG
    if floor is not None and candidate < floor:
        candidate = floor
    if candidate >= now:
        return now, False
    return candidate, True


# --- ingest ------------------------------------------------------------------


def _empty_counts() -> dict[str, Any]:
    return {"inserted": 0, "unchanged": 0, "revised": 0, "raced": 0, "projections": 0}


def ingest_edition_file(
    bind: Union[Engine, Connection],
    path: str,
    *,
    url: str = "",
    filename: str = "",
    backfill: bool = False,
    allow_older: bool = False,
    dry_run: bool = False,
    expected: Optional[Edition] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ingest one edition from disk; return its report.

    The whole workbook is parsed and verified BEFORE anything is written, so a
    file that fails any check leaves the database exactly as it was.  Then, in
    one transaction: the document is archived in ``source_documents`` (sha256,
    edition label, last-saved time), the four series are registered from the
    catalog, and every observation is written through
    :func:`app.economic.store.record_observation` — vintages, the monotone
    ``available_at`` rule and the "unchanged" decision all apply.

    ``expected`` is the edition the website's month page said this file is;
    a workbook whose own title disagrees is refused.  ``backfill`` stamps
    reconstructed availability (:func:`backfill_available_at`); otherwise the
    file is available from the moment this call had it.  ``dry_run`` performs
    every step inside a transaction that is rolled back, so the report's
    counts are exactly what a real run would write.

    ``bind`` is normally the engine, and the edition gets a transaction of its
    own.  A :class:`~sqlalchemy.engine.Connection` joins the CALLER's open
    transaction instead and neither commits nor rolls back: that is how a
    website dry run ingests every edition into one transaction it rolls back at
    the end, so the second edition is compared with the first exactly as a real
    run would compare it (:func:`ingest_cbi_monetary`).
    """
    started = ensure_utc(now) or utcnow()
    name = filename or os.path.basename(path)
    parsed, document = read_workbook(path)
    edition = parsed.edition
    if expected is not None and expected != edition:
        raise CbiParseError(
            f"{name}: the CBI website lists this file as the {expected.label} edition "
            f"but the workbook's own title says {edition.label}; refusing rather than "
            "filing it under either"
        )

    published_at = document.saved_at
    report: dict[str, Any] = {
        "edition": edition.label,
        "workbook": name,
        "sheet": parsed.sheet_name,
        "sha256": document.sha256,
        "byte_size": document.byte_size,
        "unit": parsed.unit,
        "saved_at": document.saved_at.isoformat() if document.saved_at else None,
        "published_at": published_at.isoformat() if published_at else None,
        "base_money_method": parsed.base_money_method,
        "revision_start": period_label(*parsed.revision_start) if parsed.revision_start else None,
        "checks": parsed.checks,
        "dry_run": bool(dry_run),
        "series": {},
        **_empty_counts(),
    }
    if published_at is None:
        report["published_at_note"] = (
            "the workbook carries no last-saved timestamp, so published_at is NULL; "
            "available_at carries the whole point-in-time claim"
        )

    document_url = url or SECTION_URL
    report["url"] = document_url
    report["url_reconstructed"] = not url

    write = dict(
        name=name, parsed=parsed, document=document, published_at=published_at,
        document_url=document_url, backfill=backfill, allow_older=allow_older,
        started=started, report=report,
    )
    if isinstance(bind, Connection):
        _write_edition(bind, **write)
    else:
        conn = bind.connect()
        transaction = conn.begin()
        try:
            _write_edition(conn, **write)
            if dry_run:
                transaction.rollback()
            else:
                transaction.commit()
        except BaseException:
            transaction.rollback()
            raise
        finally:
            conn.close()

    log.info(
        "CBI %s edition %s: document %s (%s), %d inserted, %d unchanged, %d revised%s",
        "dry run of" if dry_run else "ingest of", edition.label, report["document"]["id"],
        report["document"]["status"], report["inserted"], report["unchanged"],
        report["revised"],
        " (availability reconstructed)" if report["available_at_approx"] else "",
    )
    return report


def _print_saved_at(
    conn: Connection, series_id: int, ref_period_start, vintage: Optional[int]
) -> Optional[datetime]:
    """The last-saved time of the workbook behind one stored print, if known."""
    if vintage is None:
        return None
    value = conn.execute(
        select(source_documents.c.document_saved_at)
        .select_from(
            economic_observations.join(
                source_documents,
                economic_observations.c.source_document_id == source_documents.c.id,
            )
        )
        .where(
            and_(
                economic_observations.c.series_id == series_id,
                economic_observations.c.ref_period_start == ref_period_start,
                economic_observations.c.vintage == vintage,
            )
        )
    ).scalar()
    return ensure_utc(value)


def _saved_before(incoming: Optional[datetime], stored: Optional[datetime]) -> bool:
    """True when the incoming workbook was saved before the one behind a stored print.

    Such a workbook predates the print it would restate, so an old-method
    figure in it is simply what CBI printed THEN — an edition whose file CBI
    never re-saved after the redefinition (it replaced some in place, not
    necessarily all) — and is stored as the vintage it is.  Unknown on either
    side is not "before".
    """
    return incoming is not None and stored is not None and ensure_utc(incoming) < stored


def _write_edition(
    conn: Connection,
    *,
    name: str,
    parsed: ParsedEdition,
    document: WorkbookFile,
    published_at: Optional[datetime],
    document_url: str,
    backfill: bool,
    allow_older: bool,
    started: datetime,
    report: dict[str, Any],
) -> None:
    """The write half of :func:`ingest_edition_file`, inside ``conn``'s transaction.

    Everything it decides from the store — the newest edition, the backfill's
    availability floor, each period's current print — is read through the
    same connection it writes through, so an edition written earlier in the
    same transaction counts exactly as a committed one does.
    """
    edition = parsed.edition
    newest = newest_stored_edition(conn)
    if newest is not None and edition < newest and not allow_older:
        raise CbiStaleEditionError(
            f"{name!r} is the {edition.label} edition, but this deployment already "
            f"holds the {newest.label} edition. Ingesting it would write older values "
            "as the newest vintage and they would become the answer every reader gets "
            "-- available_at cannot prevent that, because it records when THIS system "
            "saw the file, not which edition it is. Pass allow_older=True only to "
            "replay deliberately."
        )

    if backfill:
        available_at, approx = backfill_available_at(
            document.saved_at, newest_edition_available_at(conn), started
        )
    else:
        available_at, approx = started, False
    report["available_at"] = available_at.isoformat()
    report["available_at_approx"] = approx
    if backfill and not approx:
        report["available_at_note"] = (
            "observed, not reconstructed: the workbook carries no last-saved time"
            if document.saved_at is None
            else "observed: the reconstruction would not be earlier than now"
        )

    document_id, document_inserted = record_source_document(
        conn,
        provider_code=CBI_PROVIDER,
        url=document_url,
        title=(
            "Central Bank of Iran, Selected Economic Indicators (monetary), "
            f"{edition.label} edition ({name})"
        ),
        media_type=MEDIA_TYPE_XLS,
        byte_size=document.byte_size,
        content_sha256=document.sha256,
        fetched_at=started,
        edition_label=edition.label,
        document_saved_at=document.saved_at,
    )
    report["document"] = {
        "id": document_id,
        "status": "inserted" if document_inserted else "existing",
    }

    specs = {spec.code: spec for spec in cbi_specs()}
    for code, points in sorted(parsed.observations.items()):
        series_id = ensure_series(conn, specs[code])
        counts = _empty_counts() | {"observations": len(points), "revisions": []}
        for point in points:
            start, end = jalali_month_span(point.year, point.month)
            # A stock CBI prints is always for a month that has ended; the
            # check exists so that one that had not would be stored flagged
            # rather than scored.
            is_projection = available_at.date() <= end
            counts["projections"] += int(is_projection)
            written = record_observation(
                conn,
                series_id,
                ref_period_start=start,
                ref_period_end=end,
                ref_period_label=point.ref_period_label,
                value=point.value,
                available_at=available_at,
                published_at=published_at,
                is_projection=is_projection,
                source_document_id=document_id,
                collected_at=started,
                method=point.method,
                available_at_approx=approx,
            )
            if (
                written.status == STATUS_REVISED
                and written.previous_method == METHOD_REVISED
                and point.method == METHOD_OLD
                and not allow_older
                and not _saved_before(
                    document.saved_at,
                    _print_saved_at(conn, series_id, start, written.previous_vintage),
                )
            ):
                # The caller's transaction is rolled back with this, so the
                # rows written before it in this edition go too.
                footnote = (
                    "starts the redefinition later" if parsed.revision_start
                    else "does not mention MFSMCG"
                )
                raise CbiParseError(
                    f"{name}: the {edition.label} edition reads base money at "
                    f"{point.ref_period_label} as the OLD method (its footnote "
                    f"{footnote}), but the store already holds that month on CBI's "
                    "REVISED method, from a workbook saved no later than this one. "
                    "CBI does not un-revise a month: it has most likely dropped or "
                    "reworded the footnote this parser reads the method from, and "
                    "storing it would label revised figures 'old' and break growth "
                    "where there is no break. Refusing the workbook; a deliberate "
                    "replay passes allow_older=True"
                )
            counts[written.status] += 1
            if written.status == STATUS_REVISED:
                counts["revisions"].append({
                    "period": point.ref_period_label,
                    "from": written.previous_value,
                    "to": str(point.value),
                    "from_method": written.previous_method,
                    "to_method": point.method,
                    "vintage": written.vintage,
                })
                log.info(
                    "CBI revision: %s %s %s -> %s (vintage %d)",
                    code, point.ref_period_label, written.previous_value,
                    point.value, written.vintage,
                )
        counts["periods"] = [point.ref_period_label for point in points]
        counts["values"] = [str(point.value) for point in points]
        if code == BASE_MONEY_CODE:
            counts["methods"] = [point.method for point in points]
        report["series"][code] = counts
        for key in (STATUS_INSERTED, STATUS_UNCHANGED, STATUS_REVISED, STATUS_RACED):
            report[key] += counts[key]
        report["projections"] += counts["projections"]


# --- the website --------------------------------------------------------------


@dataclass(frozen=True)
class MonthLink:
    """One month of a year's list: the edition, and where its page is."""

    edition: Edition
    url: str
    excel: bool          # a month page with an Excel link, not a direct PDF


@dataclass(frozen=True)
class Download:
    """A workbook as it arrived."""

    url: str
    filename: str
    content: bytes


_HREF_RE = re.compile(r"\A/(simplelist|page)/(\d+)\.aspx\Z", re.IGNORECASE)
_SELECT_RE = re.compile(
    r"<select[^>]*ddlYearList[^>]*>(.*?)</select>", re.IGNORECASE | re.DOTALL
)
_OPTION_RE = re.compile(
    r"<option[^>]*value\s*=\s*[\"'](\d+)[\"'][^>]*>(.*?)</option>",
    re.IGNORECASE | re.DOTALL,
)
_LIST_RE = re.compile(
    r"<ul[^>]*class\s*=\s*[\"']simplelist[\"'][^>]*>(.*?)</ul>", re.IGNORECASE | re.DOTALL
)
_ITEM_RE = re.compile(
    r"<a[^>]*href\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_EXCEL_LINK_TEXT = "فرمتexcel"
_DISPOSITION_RE = re.compile(r"filename\s*=\s*\"?([^\";]+)\"?", re.IGNORECASE)


def _text(fragment: str) -> str:
    return normalize_fa(html_module.unescape(_TAG_RE.sub(" ", fragment)))


def _absolute(href: str) -> Optional[str]:
    """A same-site list or page link, made absolute; anything else is ``None``.

    Only ``/simplelist/N.aspx`` and ``/page/N.aspx`` are followed: a link this
    module did not expect is never fetched, whatever host it names.
    """
    href = html_module.unescape(href).strip()
    if _HREF_RE.match(href):
        return BASE_URL + href
    return None


def parse_year_options(page: str) -> dict[int, str]:
    """``{jalali_year: year list url}`` from the section page's year dropdown."""
    match = _SELECT_RE.search(page)
    if match is None:
        raise CbiFetchError(
            "the section page has no year list (ddlYearList); CBI changed the page "
            "or answered something else"
        )
    years: dict[int, str] = {}
    for value, label in _OPTION_RE.findall(match.group(1)):
        text = fold_digits(_text(label))
        if text.isdigit() and MIN_JALALI_YEAR <= int(text) <= MAX_JALALI_YEAR:
            years[int(text)] = f"{BASE_URL}/simplelist/{value}.aspx"
    if not years:
        raise CbiFetchError("the section page's year list names no Jalali year")
    return years


def parse_month_list(page: str, year: int, allow_empty: bool = False) -> list[MonthLink]:
    """The months a year's list page (or the section page) links to, in month order.

    A month whose link is a ``/page/`` is a document linked directly — the PDF
    of the years before Tir 1399 — and is reported as such; a ``/simplelist/``
    is a month page carrying the Excel link.

    Every item of the list must be exactly that: a Jalali month name linking
    to a same-site list or page.  Anything else REFUSES the page rather than
    being skipped (an off-site link is still never fetched).  In every year
    list CBI has published the items are the twelve month names and nothing
    else, so an item that is not one means CBI changed how it labels them —
    «فروردین ۱۴۰۵», say — and skipping it would make the scheduled run report
    success while it silently stopped finding new editions, and make a
    backfill pass over an edition it can then never ingest (older than
    stored).

    A page with no month list at all is a failure — unless ``allow_empty``,
    which the walk passes for the section page only: a new Jalali year can
    appear in the dropdown before its first month is published, and that is
    "nothing yet", not a changed page.
    """
    block = _LIST_RE.search(page)
    if block is None:
        if allow_empty:
            return []
        raise CbiFetchError(f"the {year} month list (ul.simplelist) is not on the page")
    months: dict[int, MonthLink] = {}
    for href, label in _ITEM_RE.findall(block.group(1)):
        text = _text(label)
        month = month_number(text)
        url = _absolute(href)
        if month is None:
            raise CbiFetchError(
                f"the {year} month list has an item «{text}» that is not a Jalali month "
                "name; CBI changed how it labels the months, and skipping it could "
                "skip an edition for good"
            )
        if url is None:
            raise CbiFetchError(
                f"the {year} month list links {JALALI_MONTHS[month - 1]} to {href!r}, "
                "which is not a same-site /simplelist/ or /page/ link; it is not "
                "followed, and the month is not skipped silently either"
            )
        if month in months:
            raise CbiFetchError(f"the {year} list links {JALALI_MONTHS[month - 1]} twice")
        months[month] = MonthLink(
            edition=Edition(year, month), url=url, excel="/simplelist/" in url.lower()
        )
    return [months[month] for month in sorted(months)]


def parse_excel_link(page: str) -> str:
    """The «فرمت Excel» link of a month page; exactly one, or a refusal."""
    block = _LIST_RE.search(page)
    if block is None:
        raise CbiFetchError("the month page has no document list (ul.simplelist)")
    links = [
        _absolute(href)
        for href, label in _ITEM_RE.findall(block.group(1))
        if re.sub(r"\s+", "", _text(label)).lower() == _EXCEL_LINK_TEXT
    ]
    links = [link for link in links if link and "/page/" in link.lower()]
    if len(links) != 1:
        raise CbiFetchError(
            f"the month page offers {len(links)} «فرمت Excel» links; expected exactly one"
        )
    return links[0]


class CbiSite(Provider):
    """www.cbi.ir, read the way its own pages link to the workbooks.

    A :class:`~app.providers.base.Provider` for the HTTP conventions every
    adapter here keeps — the honest User-Agent, a courtesy delay before each
    request, bounded retries with exponential backoff for transient failures
    (DNS and connection errors, timeouts, 5xx, 429), and a 401/403 refused
    outright, never retried or bypassed — with CBI's own budget: 90 s and four
    attempts, because the production host's resolver fails before it answers.
    """

    code = CBI_PROVIDER
    category = "iran_macro"
    max_attempts = MAX_ATTEMPTS

    def __init__(
        self,
        timeout: float = TIMEOUT_SECONDS,
        courtesy_delay: float = COURTESY_DELAY_SECONDS,
        backoff_base: float = BACKOFF_BASE_SECONDS,
    ) -> None:
        super().__init__(timeout=timeout, courtesy_delay=courtesy_delay, backoff_base=backoff_base)
        self.requests = 0

    def fetch(self):  # pragma: no cover - never wired into price collection
        raise ProviderError("cbi: a document source, not a price provider")

    def _get(self, url: str) -> httpx.Response:
        self.requests += 1
        try:
            return self._request(url)
        except ProviderError as exc:
            raise CbiFetchError(str(exc)) from exc

    def page(self, url: str) -> str:
        response = self._get(url)
        content_type = response.headers.get("content-type", "")
        if "html" not in content_type.lower():
            raise CbiFetchError(f"{url} answered {content_type or 'no content type'}, not HTML")
        return response.text

    def months(self, year_url: Optional[str], year: int) -> list[MonthLink]:
        return parse_month_list(self.page(year_url or SECTION_URL), year)

    def download(self, link: MonthLink) -> Download:
        """The workbook behind a month page: its «فرمت Excel» link, verified.

        Accepted only when the bytes are an OLE2 compound file. An HTML body —
        whatever the status — is a challenge or an error page and fails the
        run; it is never retried with other headers or worked around.
        """
        url = parse_excel_link(self.page(link.url))
        response = self._get(url)
        content = response.content
        content_type = response.headers.get("content-type", "")
        if not content.startswith(OLE_SIGNATURE):
            looks_html = "html" in content_type.lower() or content[:64].lstrip().startswith(b"<")
            raise CbiFetchError(
                f"{url} ({link.edition.label}) answered "
                + ("an HTML page" if looks_html else f"{len(content)} bytes of {content_type or 'unknown type'}")
                + " where the Excel workbook should be. That is a challenge or an error "
                "page, not the document; the run stops here and nothing is bypassed"
            )
        disposition = _DISPOSITION_RE.search(response.headers.get("content-disposition", ""))
        filename = os.path.basename(disposition.group(1).strip()) if disposition else ""
        return Download(url=url, filename=filename or f"{link.edition.label}.xls", content=content)


def make_site() -> CbiSite:
    """The site client the endpoint uses (tests substitute their own)."""
    return CbiSite()


# --- runs --------------------------------------------------------------------


def _listed_editions(
    site: CbiSite, since: Edition, report: dict[str, Any]
) -> list[MonthLink]:
    """Every Excel edition from ``since`` on that the website lists, oldest first."""
    section = site.page(SECTION_URL)
    years = parse_year_options(section)
    newest_year = max(years)
    links: list[MonthLink] = []
    for year in sorted(years):
        if year < since.year:
            continue
        # The section page already carries the newest year's month list; any
        # other year is one more request. If the section page ever showed a
        # different year's list, each workbook's own title would disagree with
        # the edition it was filed under, and the ingest refuses that.
        if year == newest_year:
            months = parse_month_list(section, year, allow_empty=True)
            if not months:
                report["years_without_months"].append(year)
        else:
            months = parse_month_list(site.page(years[year]), year)
        for link in months:
            if link.edition < since:
                continue
            if not link.excel:
                report["pdf_only"].append(link.edition.label)
                continue
            links.append(link)
    return links


def _run(
    bind: Union[Engine, Connection],
    site: CbiSite,
    links: Sequence[MonthLink],
    *,
    backfill: bool,
    dry_run: bool,
    report: dict[str, Any],
    now: Optional[datetime],
) -> None:
    """Download and ingest each edition, oldest first; stop at the first failure.

    Stopping is deliberate. An edition that fails is not stored, so the newest
    stored edition stays before it and the next run retries it; carrying on to
    a NEWER edition would make the failed one "older than an edition already
    stored" and lock it out for good.

    ``bind`` is the engine for a real run (each edition commits on its own, so
    a failure keeps what was stored before it) and, for a dry run, the one
    connection whose transaction :func:`ingest_cbi_monetary` rolls back.
    """
    with tempfile.TemporaryDirectory(prefix="cbi-") as workdir:
        for link in links:
            entry: dict[str, Any] = {"edition": link.edition.label, "month_page": link.url}
            report["editions"].append(entry)
            try:
                download = site.download(link)
                path = os.path.join(workdir, f"{link.edition.label}.xls")
                with open(path, "wb") as handle:
                    handle.write(download.content)
                entry.update(
                    ingest_edition_file(
                        bind,
                        path,
                        url=download.url,
                        filename=download.filename,
                        backfill=backfill,
                        dry_run=dry_run,
                        expected=link.edition,
                        now=now,
                    )
                )
                entry["status"] = "ok"
            except (CbiFetchError, CbiParseError, CbiStaleEditionError) as exc:
                entry["status"] = "error"
                entry["error"] = str(exc)
                report["failed"] = {"edition": link.edition.label, "error": str(exc),
                                    "kind": type(exc).__name__}
                return
            for key in ("inserted", "unchanged", "revised", "raced"):
                report[key] += entry.get(key, 0)


# One CBI ingest at a time in this service (it runs as a single uvicorn
# process). See CbiRunInProgressError for what an interleaving does.
_RUN_LOCK = threading.Lock()


def ingest_cbi_monetary(
    engine: Engine,
    *,
    fetch_latest: bool = False,
    backfill_from: Optional[str] = None,
    path: Optional[str] = None,
    filename: str = "",
    url: str = "",
    allow_older: bool = False,
    dry_run: bool = False,
    now: Optional[datetime] = None,
    site: Optional[CbiSite] = None,
) -> dict[str, Any]:
    """One run in exactly one of three modes; returns the report.

    ``fetch_latest`` — the scheduled run.  Every Excel edition the website
    lists from the newest stored one on (that one included, because CBI
    replaces files in place), oldest first, available from the moment this run
    had it.  Editions arrive in batches (1404/10 and 1404/11 both on
    2026-06-06), and a monthly edition prints only its own month, last Esfand
    and the same month last year — so taking only the newest would lose the
    month before it for good.  Refuses with :class:`CbiBackfillRequiredError`
    when nothing is stored yet.

    ``backfill_from='1399-04'`` — the first population, and its resumption.
    Every Excel edition from that one on, oldest first, with availability
    reconstructed from each workbook (:func:`backfill_available_at`).  An
    edition older than the newest already stored is skipped without being
    downloaded, so a backfill that stopped part-way resumes where it stopped.

    ``path`` — one workbook already on disk (a file fetched elsewhere, or a
    replay), available from now; ``filename``/``url`` record what CBI called it
    and where it came from.

    The website modes stop at the first edition that fails and report it under
    ``failed`` — see :func:`_run`.

    ``dry_run`` in a website mode ingests every edition into ONE transaction
    and rolls it back at the end, so each edition is compared with the ones
    before it in the same run — its revisions, its unchanged months and the
    backfill's monotone availability are reported exactly as a real run would
    store them.  (Rolled back per edition instead, every edition would be
    measured against the store as it was before the run, and a dry run of the
    backfill would show none of the base-money restatements it exists to
    preview.)  The transaction stays open while the editions download.

    Only one run at a time: a call made while another is in progress raises
    :class:`CbiRunInProgressError` and touches nothing.
    """
    modes = [bool(fetch_latest), backfill_from is not None, path is not None]
    if sum(modes) != 1:
        raise ValueError(
            "exactly one of fetch_latest, backfill_from or path must be given"
        )
    if not _RUN_LOCK.acquire(blocking=False):
        raise CbiRunInProgressError(
            "another CBI ingest is running in this service (the one-off backfill takes "
            "about half an hour). Interleaving with it would stamp the editions after "
            "its progress point with today's availability and lock the backfill out of "
            "them, so nothing was done; the next scheduled run picks up from wherever "
            "it finished."
        )
    try:
        return _ingest_cbi_monetary(
            engine, fetch_latest=fetch_latest, backfill_from=backfill_from, path=path,
            filename=filename, url=url, allow_older=allow_older, dry_run=dry_run,
            now=now, site=site,
        )
    finally:
        _RUN_LOCK.release()


def _ingest_cbi_monetary(
    engine: Engine,
    *,
    fetch_latest: bool,
    backfill_from: Optional[str],
    path: Optional[str],
    filename: str,
    url: str,
    allow_older: bool,
    dry_run: bool,
    now: Optional[datetime],
    site: Optional[CbiSite],
) -> dict[str, Any]:
    """:func:`ingest_cbi_monetary`'s body, under its lock."""
    started = ensure_utc(now) or utcnow()
    clock = time_module.monotonic()

    if path is not None:
        report = ingest_edition_file(
            engine, path, url=url, filename=filename, allow_older=allow_older,
            dry_run=dry_run, now=started,
        )
        report["mode"] = "path"
        report["status"] = "ok"
        if not dry_run:
            JOB_LAST_SUCCESS.labels(job=JOB_NAME).set(time_module.time())
        return report

    mode = "fetch_latest" if fetch_latest else "backfill"
    switched_off = provider_switched_off(engine)
    if switched_off:
        return {"mode": mode, "status": "skipped", "reason": switched_off,
                "requests": 0, "editions": [], "failed": None}

    site = site or make_site()
    newest = newest_stored_edition(engine)
    report: dict[str, Any] = {
        "mode": mode,
        "status": "ok",
        "dry_run": bool(dry_run),
        "newest_stored_before": newest.label if newest else None,
        "started_at": started.isoformat(),
        "editions": [],
        "pdf_only": [],
        "years_without_months": [],
        "skipped_older_than_stored": None,
        "failed": None,
        "inserted": 0, "unchanged": 0, "revised": 0, "raced": 0,
    }
    if fetch_latest:
        if newest is None:
            raise CbiBackfillRequiredError(
                "no CBI edition is stored yet, so there is nothing to fetch the newest "
                "editions after. Run the backfill first -- POST "
                "/internal/economic/cbi-monetary {\"backfill_from\": \"1399-04\"} -- which "
                "ingests every Excel edition from Tir 1399 with availability "
                "reconstructed from each workbook; this scheduled run keeps it current "
                "from then on."
            )
        since = newest
    else:
        try:
            requested = Edition.parse(backfill_from or "")
        except ValueError as exc:
            raise ValueError(f"backfill_from: {exc}") from exc
        first = Edition(*FIRST_EXCEL_EDITION)
        if requested < first:
            report["note"] = (
                f"CBI published no Excel edition before {first.label}; the backfill "
                f"starts there instead of at {requested.label}"
            )
            requested = first
        since = requested
        if newest is not None and newest > requested:
            since = newest

    if not fetch_latest and newest is not None and newest > requested:
        # Not downloaded at all: each is older than an edition already stored,
        # and would be refused for exactly that reason.
        report["skipped_older_than_stored"] = {
            "from": requested.label,
            "before": newest.label,
            "reason": (
                f"the {newest.label} edition is already stored; an older edition "
                "would be refused, so none is downloaded and the backfill resumes "
                f"at {newest.label}"
            ),
        }
    links = _listed_editions(site, since, report)
    if dry_run:
        with engine.connect() as conn:
            transaction = conn.begin()
            try:
                _run(conn, site, links, backfill=not fetch_latest, dry_run=True,
                     report=report, now=now)
            finally:
                transaction.rollback()
    else:
        _run(engine, site, links, backfill=not fetch_latest, dry_run=False,
             report=report, now=now)
    report["requests"] = site.requests
    report["seconds"] = round(time_module.monotonic() - clock, 1)
    report["finished_at"] = utcnow().isoformat()
    newest_after = newest_stored_edition(engine)
    report["newest_stored_after"] = newest_after.label if newest_after else None
    if report["failed"] is not None:
        report["status"] = "failed"
    elif not dry_run:
        JOB_LAST_SUCCESS.labels(job=JOB_NAME).set(time_module.time())
    return report


def provider_switched_off(engine: Engine) -> Optional[str]:
    """Why the website modes are skipped, when an operator switched CBI off.

    ``data_providers.enabled = FALSE`` for ``cbi`` stops every request to
    www.cbi.ir — the same off-switch the TGJU daily job honours — and the run
    reports itself skipped rather than failed: an operator's switch is a
    decision, not an outage.  A deployment without the row (migration 0034
    not applied) has no switch to read, and the run goes ahead.
    """
    with engine.connect() as conn:
        enabled = conn.execute(
            select(data_providers.c.enabled).where(data_providers.c.code == CBI_PROVIDER)
        ).scalar()
    if enabled is False:
        return (
            f"data_providers.enabled is FALSE for '{CBI_PROVIDER}': an operator switched "
            "the CBI off, so no request was made"
        )
    return None
