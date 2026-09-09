"""The Statistical Centre of Iran's urban CPI workbook: parse, archive, store.

SCI is the legally designated statistical authority, so this is the first
primary document in the economic layer rather than somebody's redistribution
of one — and the first ingest that WRITES ``source_documents``, the table
migration 0024 created and deliberately never filled.

**This module is fed a file, it never fetches one.**  amar.org.ir is
unreachable from the production host: DNS resolves (217.218.11.77), TCP
connects on both 443 and 80, and then nothing answers — no TLS handshake
completes at 1.2 or 1.3 and plain HTTP returns nothing either (diagnosed
2026-09-09).  That is application-layer filtering, not a TLS fault, and it
means a server-side cron would fail every tick forever.  So
``scripts/sci_fetch.py`` discovers and downloads the workbook somewhere that
can reach SCI, copies it to the server, and calls
``POST /internal/economic/sci-cpi`` inside the compose network.  Migration 0027
states the same thing in the provider row.

THE THREE HAZARDS THE PARSER EXISTS TO HANDLE
---------------------------------------------
Each was hit on the real workbook (``ts_urban_140505-14050618165804.xlsx``,
sha256 ``b28e3375b772f37ef5e440ca6ba3378b33173226b0388507953f0c102d259114``),
and each guard below exists because of the failure it prevents.

1. *Mixed Persian/Arabic orthography.*  The category labels use Arabic KAF
   (U+0643) and Arabic YEH (U+064A) — ``مسكن``, ``خريد وسايل نقليه`` — while the
   month headers in the SAME sheet use Persian YEH (U+06CC), and ZWNJ appears
   inside labels.  Matching a hardcoded Persian ``ک`` finds nothing.  Both
   sides are normalised (:func:`normalize_fa`) rather than either form being
   assumed.

2. *Exact equality, never substring.*  ``مسکن`` is a row of its own AND a
   substring of ``04 - مسکن ، آب ، برق ، گاز و سایر سوخت‌ها``, so a substring
   match would bind the housing series to the whole utilities division.

3. *Merged year cells.*  Row 2 carries the Jalali year once per twelve-month
   block and is empty for the other eleven columns.  The year is carried
   forward across its block; without that, every observation after Farvardin
   lands in the wrong year.

AND THE RULE THAT TIES THEM TOGETHER
------------------------------------
If ANY target series is missing, the WHOLE workbook is refused.  A layout
change that moves one row must not ingest the other three and look successful:
that would leave one series silently frozen at its last good vintage while the
ingest report says "ok".  The same reasoning refuses a workbook whose base year
does not average 100 — a rebase restates the level history, and levels stored
under a ``base_period`` label they no longer belong to are worse than no data.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional

import openpyxl
from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from ..db import ensure_utc, insert_ignore, source_documents, utcnow
from .catalog import SCI_PROVIDER, SCI_SHEET, sci_specs
from .store import (
    STATUS_INSERTED,
    STATUS_RACED,
    STATUS_REVISED,
    STATUS_UNCHANGED,
    ensure_series,
    record_observation,
)

log = logging.getLogger(__name__)

# The sheet holding the index LEVELS: 51 rows x 301 columns in the workbook
# this was written against. Its siblings carry month-on-month and
# year-on-year percentage changes, which are different measures and would need
# their own catalog entries rather than being folded into these.
SHEET = SCI_SHEET

BASE_YEAR = 1400          # the workbook's stated base: 1400 = 100
BASE_TOLERANCE = 0.01     # the twelve months of the base year must average 100

MEDIA_TYPE_XLSX = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
# Where SCI publishes the statistics workbooks. The filename is the only part
# that changes between releases; scripts/sci_fetch.py scrapes the listing page
# rather than hardcoding a URL, and this constant only reconstructs the
# canonical location when a caller posts a file without saying where it came
# from (see :func:`ingest_sci_cpi`).
SCI_STATISTICS_URL = "https://amar.org.ir/Portals/0/Statistics/"


class SciParseError(ValueError):
    """The workbook is not the shape this parser was written for."""


# --- Jalali <-> Gregorian ----------------------------------------------------
#
# The inverse of :func:`app.features.engineering.gregorian_to_jalali`, and the
# same integer algorithm, so the two can never disagree about a boundary. The
# repo already owns the forward direction; this adds only the direction it
# lacks. Round-tripped over every month boundary in 1381..1405 with zero
# mismatches, which tests/test_economic_sci.py keeps as a test rather than a
# claim.


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> date:
    """The Gregorian date of a Jalali (Solar Hijri) year/month/day."""
    jy2 = jy - 979
    n = 365 * jy2 + (jy2 // 33) * 8 + ((jy2 % 33) + 3) // 4
    n += (jm - 1) * 31 if jm <= 6 else 186 + (jm - 7) * 30
    n += jd - 1 + 79
    gy = 1600 + 400 * (n // 146097)
    n %= 146097
    leap = True
    if n >= 36525:
        n -= 1
        gy += 100 * (n // 36524)
        n %= 36524
        if n >= 365:
            n += 1
        else:
            leap = False
    gy += 4 * (n // 1461)
    n %= 1461
    if n >= 366:
        leap = False
        n -= 1
        gy += n // 365
        n %= 365
    months = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 12 and n >= months[gm]:
        n -= months[gm]
        gm += 1
    return date(gy, gm + 1, n + 1)


def jalali_month_span(jy: int, jm: int) -> tuple[date, date]:
    """Gregorian ``[start, end]`` of one Jalali month, both inclusive.

    The end is derived by stepping back one day from the NEXT month's first
    day rather than from a table of month lengths, so a leap year (Esfand
    having 30 days instead of 29) needs no special case and cannot drift.
    """
    start = jalali_to_gregorian(jy, jm, 1)
    next_year, next_month = (jy + 1, 1) if jm == 12 else (jy, jm + 1)
    return start, jalali_to_gregorian(next_year, next_month, 1) - timedelta(days=1)


# --- Persian text normalisation ---------------------------------------------

_ARABIC_TO_PERSIAN = {
    0x0643: 0x06A9,  # ARABIC KAF     -> PERSIAN KEHEH
    0x064A: 0x06CC,  # ARABIC YEH     -> PERSIAN YEH
    0x0649: 0x06CC,  # ALEF MAKSURA   -> PERSIAN YEH
    0x0629: 0x0647,  # TEH MARBUTA    -> HEH
}
# ZWNJ, LRM, RLM, NBSP, BOM: invisible in the spreadsheet, fatal to equality.
_STRIP = dict.fromkeys([0x200C, 0x200E, 0x200F, 0x00A0, 0xFEFF])


def normalize_fa(text: object) -> str:
    """Fold a Persian label to a comparable form.

    Without this, ``مسكن`` (Arabic kaf) and ``مسکن`` (Persian keheh) are
    different strings that render identically, and the lookup silently misses —
    which, before the all-or-nothing rule below, meant ingesting three series
    out of four and reporting success.
    """
    folded = unicodedata.normalize("NFKC", str(text or ""))
    folded = folded.translate(_STRIP).translate(_ARABIC_TO_PERSIAN)
    return re.sub(r"\s+", " ", folded).strip()


JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)
_MONTH_INDEX = {normalize_fa(name): i + 1 for i, name in enumerate(JALALI_MONTHS)}


def _targets() -> dict[str, str]:
    """``{normalised row label: series code}`` for the rows we extract.

    Derived from the catalog rather than restated here, so the label a spec
    advertises (``SeriesSpec.indicator``, kept in SCI's own orthography so it
    can be grepped against a downloaded file) and the label this parser looks
    for cannot drift apart. A collision would make one series unreachable, so
    it is refused at import rather than resolved arbitrarily.
    """
    targets: dict[str, str] = {}
    for spec in sci_specs():
        key = normalize_fa(spec.indicator)
        if not key:
            raise SciParseError(f"catalog entry {spec.code} has an empty row label")
        if key in targets:
            raise SciParseError(
                f"catalog entries {targets[key]} and {spec.code} both claim the "
                f"row label {key!r}; one of them could never be extracted"
            )
        targets[key] = spec.code
    return targets


TARGETS: dict[str, str] = _targets()


# --- records ----------------------------------------------------------------


@dataclass(frozen=True)
class SciObservation:
    """One extracted monthly index level.

    Deliberately not :class:`app.economic.SeriesPoint`: that record describes
    what a PROVIDER FETCH returned, and nothing here is fetched. It also
    carries the Jalali coordinates, which the base-year check runs on and which
    a Gregorian-only record would force this module to re-derive from a label.
    """

    code: str
    jalali_year: int
    jalali_month: int
    ref_period_label: str        # '1405-05'
    ref_period_start: date
    ref_period_end: date
    value: float


def published_at_from_filename(name: str) -> Optional[date]:
    """The publication date SCI embeds in its own filename.

    ``ts_urban_140505-14050618165804.xlsx`` is the Mordad 1405 edition
    published 1405/06/18 at 16:58:04, i.e. 2026-09-09.  Returns ``None`` when
    the name carries no such stamp — an absent date is reported as absent, not
    guessed from the clock.

    Only the DAY is taken.  The clock time in the filename is Tehran-local and
    this does not convert it, so ``published_at`` is the start of that day in
    UTC: a lower bound on the publication instant, never later than the truth.
    That is safe because no read depends on it — every point-in-time read
    filters on ``available_at`` and nothing else (see
    :mod:`app.economic.store`) — and it is still the first series in this
    system whose ``published_at`` is a real date rather than NULL.
    """
    match = re.search(r"-(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})\.xlsx?$", name)
    if not match:
        return None
    jy, jm, jd = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if not (1300 <= jy <= 1500 and 1 <= jm <= 12 and 1 <= jd <= 31):
        return None
    return jalali_to_gregorian(jy, jm, jd)


def is_incomplete_month(period_end: date, now: datetime) -> bool:
    """True when the Jalali month ``period_end`` closes has not finished yet.

    The monthly counterpart of
    :func:`app.economic.is_incomplete_annual_period`, and it exists for the
    same reason: a figure for a period that has not ended is not a measurement
    of it.  SCI has never published a running month — it publishes around day
    10 of the following one — so in practice this is always False, and it is
    here so that if SCI ever did, the value would be stored flagged rather
    than scored as an observation.
    """
    return ensure_utc(now).date() <= period_end


# --- parsing ----------------------------------------------------------------


# The workbook's series begins in 1381 and a Jalali year cannot be far in the
# future; anything outside that band in the merged-year header is a shifted
# column or a stray cell, not data.
MIN_JALALI_YEAR = 1300
MAX_JALALI_YEAR = 1500


def _checked_jalali_year(cell: object) -> int:
    """Read a Jalali year from the merged header row, or refuse the workbook.

    Without this bound, any truthy integer became a year: a stray ``7`` silently
    stamped twelve observations with reference periods in 628 AD, and every
    other guard in this module still passed -- the values, the labels and the
    base check were all internally consistent, they were simply attached to the
    wrong millennium. A very large value escaped instead as a bare ValueError
    out of the date arithmetic, which is a crash rather than an explanation.
    """
    try:
        year = int(cell)
    except (TypeError, ValueError) as exc:
        raise SciParseError(
            f"the year header carries {cell!r}, which is not a Jalali year"
        ) from exc
    if not (MIN_JALALI_YEAR <= year <= MAX_JALALI_YEAR):
        raise SciParseError(
            f"the year header carries {year}, outside the plausible Jalali range "
            f"{MIN_JALALI_YEAR}..{MAX_JALALI_YEAR}. The header row has shifted or a "
            "stray cell is being read as a year; refusing the workbook rather than "
            "stamping observations with a reference period centuries away."
        )
    return year


def parse_workbook(rows: Iterable[Iterable[object]]) -> dict[str, list[SciObservation]]:
    """Parse the rows of sheet ``جدول 1`` into per-series observations.

    ``rows`` is what ``openpyxl``'s ``iter_rows(values_only=True)`` yields, so
    this half is testable without a file and the file half stays trivial.
    """
    grid = [list(row) for row in rows]
    if len(grid) < 4:
        raise SciParseError(
            f"sheet has {len(grid)} rows; expected a header block plus data"
        )

    year_row, month_row = grid[1], grid[2]

    # Carry the merged year across its twelve month columns. openpyxl reports a
    # merged range's value on the first cell and None on the rest, so without
    # this every observation after Farvardin would land in the wrong year.
    axis: list[tuple[int, int, int]] = []   # (column, jalali_year, jalali_month)
    current_year: Optional[int] = None
    for col in range(1, len(month_row)):
        cell = year_row[col] if col < len(year_row) else None
        if isinstance(cell, (int, float)) and not isinstance(cell, bool) and cell:
            current_year = _checked_jalali_year(cell)
        elif isinstance(cell, str) and cell.strip().isdigit():
            current_year = _checked_jalali_year(cell.strip())
        month = _MONTH_INDEX.get(normalize_fa(month_row[col]))
        if current_year is not None and month is not None:
            axis.append((col, current_year, month))
    if not axis:
        raise SciParseError(
            "no (year, month) columns were recognised in the header rows; the "
            "sheet layout changed"
        )

    found: dict[str, list[SciObservation]] = {}
    for row in grid[3:]:
        # Exact equality on the NORMALISED label, never a substring: 'مسکن' is
        # its own row and also a substring of the utilities division's label.
        code = TARGETS.get(normalize_fa(row[0] if row else None))
        if code is None or code in found:
            continue
        points: list[SciObservation] = []
        for col, jalali_year, jalali_month in axis:
            value = row[col] if col < len(row) else None
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                # SCI prints '-' for a period it has no value for. Skipped and
                # reported, never zero-filled: a zero index level would read as
                # a 100% collapse in prices.
                continue
            start, end = jalali_month_span(jalali_year, jalali_month)
            points.append(
                SciObservation(
                    code=code,
                    jalali_year=jalali_year,
                    jalali_month=jalali_month,
                    ref_period_label=f"{jalali_year}-{jalali_month:02d}",
                    ref_period_start=start,
                    ref_period_end=end,
                    value=float(value),
                )
            )
        found[code] = points

    missing = sorted(set(TARGETS.values()) - set(found))
    if missing:
        # Refuse the WHOLE workbook. A layout change that moved one row would
        # otherwise ingest the rest and look like a success, leaving one series
        # silently frozen at its last good vintage.
        raise SciParseError(
            f"expected series not found in the workbook: {', '.join(missing)}. "
            "The sheet layout or its labels changed; refusing the whole file "
            "rather than ingesting a partial set."
        )

    for code, points in found.items():
        if not points:
            raise SciParseError(
                f"{code} matched a row but carried no numeric observations"
            )
        base = [point.value for point in points if point.jalali_year == BASE_YEAR]
        if len(base) != 12:
            raise SciParseError(
                f"{code}: base year {BASE_YEAR} has {len(base)} months, expected 12"
            )
        mean = sum(base) / 12.0
        if abs(mean - 100.0) > BASE_TOLERANCE:
            # The stated base is 1400=100 and it is checkable from the data
            # itself. If SCI rebases, the levels are no longer spliceable with
            # what is already stored, and that is a decision for a human.
            raise SciParseError(
                f"{code}: base year {BASE_YEAR} averages {mean:.4f}, not 100. "
                "The workbook appears to be on a different base; refusing rather "
                "than storing restated levels under the stored base_period label."
            )
    return found


def read_workbook(path: str) -> dict[str, list[SciObservation]]:
    """Open the ``.xlsx`` at ``path`` and parse its levels sheet."""
    if not os.path.isfile(path):
        # Not an SciParseError: nothing has been said about the DOCUMENT here,
        # only about the caller's path, and the endpoint keeps the two apart
        # when it decides whether the provider's health changed.
        raise FileNotFoundError(f"no workbook at {path}")
    try:
        workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises several unrelated types
        # BadZipFile, InvalidFileException, KeyError from a damaged container:
        # all of them mean the same thing to a caller, and none of them should
        # reach the endpoint as a 500.
        raise SciParseError(
            f"{os.path.basename(path)} is not a readable .xlsx workbook: {exc}"
        ) from exc
    try:
        if SHEET not in workbook.sheetnames:
            raise SciParseError(
                f"sheet {SHEET!r} is not in {os.path.basename(path)}; it has "
                f"{', '.join(workbook.sheetnames)}"
            )
        return parse_workbook(workbook[SHEET].iter_rows(values_only=True))
    finally:
        workbook.close()


# --- source document --------------------------------------------------------


def record_source_document(
    conn,
    *,
    provider_code: str,
    url: str,
    title: str,
    media_type: str,
    byte_size: int,
    content_sha256: str,
    fetched_at: datetime,
) -> tuple[int, bool]:
    """Archive the artifact a value was read from; return ``(id, inserted)``.

    Deduped on ``(provider_code, content_sha256)`` — the constraint 0024
    already declares, on the grounds that the same bytes re-fetched from the
    same provider are the same document.  That is what makes re-posting a file
    a no-op rather than a second archive row with a second set of observations
    hanging off it.

    ``storage_path`` is left empty on purpose.  The durable copy lives beside
    the backups on the host; the service only ever sees a temporary copy inside
    its own container, and recording that path would claim a retrievability
    this service cannot offer.  The hash is what makes the row defensible: any
    copy of the file can be checked against it.
    """
    values = {
        "provider_code": provider_code,
        "url": url,
        "title": title,
        "media_type": media_type,
        "byte_size": int(byte_size),
        "content_sha256": content_sha256,
        "storage_path": "",
        "fetched_at": fetched_at,
    }
    inserted = insert_ignore(conn, source_documents, [values])
    document_id = conn.execute(
        select(source_documents.c.id).where(
            and_(
                source_documents.c.provider_code == provider_code,
                source_documents.c.content_sha256 == content_sha256,
            )
        )
    ).scalar_one()
    return int(document_id), bool(inserted)


def _sha256_and_size(path: str) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _empty_counts() -> dict[str, Any]:
    return {
        "inserted": 0, "unchanged": 0, "revised": 0, "raced": 0, "projections": 0,
    }


# --- ingest -----------------------------------------------------------------


def ingest_sci_cpi(
    engine: Engine,
    path: str,
    filename: str = "",
    url: str = "",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Ingest one SCI urban-CPI workbook from disk.

    The whole workbook is parsed and verified BEFORE anything is written, so a
    file that fails any guard leaves the database exactly as it was.  Then, in
    one transaction: the document is archived in ``source_documents``, the four
    series are registered from the catalog, and every observation is written
    through :func:`app.economic.store.record_observation` — so vintages, the
    ``available_at`` monotonicity rule and the value-only "unchanged" decision
    all apply here unchanged.  Every row carries the archived document's id.

    **Idempotent.**  Re-posting the same file re-uses the deduped document row
    and writes no observation, because every value compares equal to what is
    stored.  A file whose values DIFFER writes new vintages beside the prints
    they revise, never over them.

    ``filename`` is what SCI called the file; it carries the publication
    timestamp and is the only place ``published_at`` can come from, so a copy
    renamed in transit loses that date (and says so in the report) rather than
    having one invented for it.  ``path`` is where the bytes are now, which is
    a temporary location and is not provenance.

    ``url`` is where the document was obtained.  When the caller does not say,
    it is reconstructed as SCI's canonical statistics path for that filename —
    the same location ``scripts/sci_fetch.py`` discovers — and the report says
    the URL was reconstructed.  The integrity claim rests on
    ``content_sha256`` either way.
    """
    started = ensure_utc(now) or utcnow()
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no workbook at {path}")
    name = filename or os.path.basename(path)
    if not name:
        raise SciParseError("no filename: nothing to derive a publication date from")

    content_sha256, byte_size = _sha256_and_size(path)
    series = read_workbook(path)

    published_date = published_at_from_filename(name)
    published_at = (
        datetime.combine(published_date, time.min, tzinfo=timezone.utc)
        if published_date is not None
        else None
    )
    # The instant this system had the file in hand: the first moment it could
    # have known ANY value in it, and therefore the availability stamp for all
    # of them. Not the moment SCI published, which is `published_at` above and
    # is a claim by the source.
    available_at = started
    document_url = url or (SCI_STATISTICS_URL + name)

    report: dict[str, Any] = {
        "workbook": name,
        "path": path,
        "sha256": content_sha256,
        "byte_size": byte_size,
        "published_at": published_at.isoformat() if published_at else None,
        "available_at": available_at.isoformat(),
        "series": {},
        "inserted": 0,
        "unchanged": 0,
        "revised": 0,
        "raced": 0,
        "started_at": started.isoformat(),
    }
    if published_at is None:
        report["published_at_note"] = (
            f"{name!r} carries no Jalali publication timestamp, so published_at "
            "is NULL for these rows; available_at carries the whole "
            "point-in-time claim, as it does for every other series here."
        )

    specs = {spec.code: spec for spec in sci_specs()}
    with engine.begin() as conn:
        document_id, document_inserted = record_source_document(
            conn,
            provider_code=SCI_PROVIDER,
            url=document_url,
            title=f"Statistical Centre of Iran, urban CPI time series ({name})",
            media_type=MEDIA_TYPE_XLSX,
            byte_size=byte_size,
            content_sha256=content_sha256,
            fetched_at=available_at,
        )
        report["document"] = {
            "id": document_id,
            "status": "inserted" if document_inserted else "existing",
            "url": document_url,
            "url_reconstructed": not url,
        }

        for code, points in sorted(series.items()):
            spec = specs[code]
            series_id = ensure_series(conn, spec)
            counts = _empty_counts()
            counts["observations"] = len(points)
            for point in points:
                is_projection = is_incomplete_month(point.ref_period_end, available_at)
                counts["projections"] += int(is_projection)
                written = record_observation(
                    conn,
                    series_id,
                    ref_period_start=point.ref_period_start,
                    ref_period_end=point.ref_period_end,
                    ref_period_label=point.ref_period_label,
                    value=point.value,
                    available_at=available_at,
                    published_at=published_at,
                    is_projection=is_projection,
                    source_document_id=document_id,
                    collected_at=available_at,
                )
                if written.status == STATUS_INSERTED:
                    counts["inserted"] += 1
                elif written.status == STATUS_REVISED:
                    counts["revised"] += 1
                    log.info(
                        "SCI revision: %s %s %s -> %s (vintage %d)",
                        code, point.ref_period_label, written.previous_value,
                        point.value, written.vintage,
                    )
                elif written.status == STATUS_UNCHANGED:
                    counts["unchanged"] += 1
                elif written.status == STATUS_RACED:
                    counts["raced"] += 1
            counts["status"] = "ok"
            counts["series_id"] = series_id
            counts["first_period"] = points[0].ref_period_label
            counts["last_period"] = points[-1].ref_period_label
            counts["last_value"] = points[-1].value
            report["series"][code] = counts
            for key in ("inserted", "unchanged", "revised", "raced"):
                report[key] += counts[key]

    report["finished_at"] = utcnow().isoformat()
    log.info(
        "SCI ingest %s: document %s (%s), %d inserted, %d unchanged, %d revised",
        name, document_id, report["document"]["status"],
        report["inserted"], report["unchanged"], report["revised"],
    )
    return report
