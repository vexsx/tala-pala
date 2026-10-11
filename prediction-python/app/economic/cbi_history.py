"""The CBI time-series viewer's quarterly history of the monetary aggregates.

The monthly Excel editions (:mod:`app.economic.cbi`) begin in Tir 1399 and
reach back, through their "same month last year" and "Esfand two years back"
columns, to Tir 1398 and Esfand 1397.  Everything before that exists in two
places only: PDF bulletins (units change from billion to thousand billion
rials in 1391, digits are Persian with ``/`` or ``,`` as the decimal point,
and older ones carry no labelled base-money row) and the CBI's own time-series
viewer, tsdview.cis.cbi.ir, which answers a JSON API without a login and
holds quarter-end values of all four aggregates from 1352.  This reads the
latter.

**This module is fed a file, it never fetches one.**  tsdview answers from
outside Iran and NOT from the production host (the configured resolver fails;
with 1.1.1.1 it resolves to 80.191.60.23 and the connection times out,
measured 2026-10-11).  So ``scripts/cbi_history_fetch.py`` fetches the payload
elsewhere, copies it to the server and posts
``/internal/economic/cbi-history`` inside the compose network — the
``scripts/sci_fetch.py`` pattern.

WHAT IS WRITTEN, AND WHAT IS ONLY COMPARED
------------------------------------------
Only quarter-end months no Excel edition can carry
(:func:`app.economic.cbi.excel_can_carry` — everything before Esfand 1397,
plus Farvardin-Khordad 1398, of which only Khordad is a quarter end).  The
two sources print the same stock to different precision (the viewer to four
decimals, the workbooks to one), so writing both into one series would turn
every overlapping month into a pair of vintages that alternate with every
ingest.  The overlap is used instead as a check: where the store already holds
an Excel value for liquidity, money or quasi-money, the viewer's value must
agree with it to the workbook's last printed digit, or the whole payload is
refused — a viewer in another unit or another series mapping cannot pass.
Base money is compared and reported but not held to it: the viewer carries
the redefined method from Esfand 1399 on (its 4,506.7 there is the revised
figure), where the editions of 1399-1401 print the old one (4,588.9).

THE PAYLOAD'S OWN GUARDS
------------------------
Every row must carry the quarterly sub-frequency id and the
thousand-billion-rial unit id this was verified against — any other unit is
refused, never rescaled; a value is an exact decimal string; a period is a
quarter-end MONTH.  The day is not used: the viewer stamps Esfand 29 in
1354, 1358, 1362, 1366 and 1370, which were leap years with a 30-day Esfand,
so its day is a label, not a date to check the calendar against.  All four
series must be present.  If anything fails, nothing is written.

What a stored row claims: ``available_at`` is when THIS system ingested the
payload — the history predates this system and the viewer states no
publication date, so that is the only honest stamp — and ``published_at`` is
NULL.  Base money there is all on the old method (the redefinition starts in
1399-12/1400, long after the last month written here).
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy.engine import Engine

from ..db import ensure_utc, utcnow
from .catalog import BASE_MONEY_CODE, CBI_PROVIDER, cbi_specs
from .cbi import METHOD_OLD, excel_can_carry
from .jalali import jalali_month_span, period_label
from .store import (
    STATUS_INSERTED,
    STATUS_RACED,
    STATUS_REVISED,
    STATUS_UNCHANGED,
    ensure_series,
    file_sha256_and_size,
    point_in_time,
    record_observation,
    record_source_document,
)

log = logging.getLogger(__name__)

TSDVIEW_URL = "https://tsdview.cis.cbi.ir/backend/api/value/graph"
MEDIA_TYPE_JSON = "application/json"
EDITION_LABEL = "tsdview"

# The viewer's category ids for the four series (from its own category tree,
# 2026-10-11). Not in the catalog: the catalog's provider_series_id names the
# Excel row these series are read from, and this is a second key into the
# same series.
CATEGORY_IDS: dict[str, str] = {
    "CBI_LIQUIDITY": "3505fc25-6097-8f09-e063-4ceaa8c0b336",
    BASE_MONEY_CODE: "3505fc25-6014-8f09-e063-4ceaa8c0b336",
    "CBI_MONEY_M1": "3505fc25-6098-8f09-e063-4ceaa8c0b336",
    "CBI_QUASI_MONEY": "3505fc25-6031-8f09-e063-4ceaa8c0b336",
}
# «فصلی» (quarterly). The monthly sub-frequency returns empty values for these
# categories, and the annual one is a different measure.
QUARTERLY_SUBFREQUENCY_ID = "9848C04378D9D268F987140AB9D87A3BF71A53B447D059BB26A52B9DD0F58732"
# The unit every one of the five money categories reports: thousand billion
# rials, verified by the overlap with the Excel editions (see
# OVERLAP_TOLERANCE: the values agree to the last printed digit).
THOUSAND_BILLION_RIAL_UNIT_ID = (
    "2C129749BA41EA3F249D5DBA05769091B78C58AB01E3DBFFEC82B05EDD0B9B54"
)
QUARTER_END_MONTHS = (3, 6, 9, 12)

# The viewer prints four decimals, the workbooks one. One unit of the
# workbook's last digit, because that is what the two were measured to differ
# by: against every captured edition (2026-10-11), M1 and quasi-money agree at
# all 27 overlapping quarter-ends and M2 at 26, the 27th being Azar 1404 —
# 135,344.5 in the viewer, 135,344.6 in the Azar 1404 edition. A wrong unit or
# a wrong series is off by orders of magnitude or by a factor of three, so the
# check loses nothing by allowing the last digit.
OVERLAP_TOLERANCE = Decimal("0.1")
# Held to the overlap check. Base money is not: see the module docstring.
STRICT_OVERLAP_CODES = ("CBI_LIQUIDITY", "CBI_MONEY_M1", "CBI_QUASI_MONEY")


class CbiHistoryError(ValueError):
    """The payload is not the shape, unit or series this was written for, or
    disagrees with the Excel editions already stored."""


@dataclass(frozen=True)
class HistoryPoint:
    code: str
    year: int
    month: int
    value: Decimal

    @property
    def ref_period_label(self) -> str:
        return period_label(self.year, self.month)


_KH_DATE_RE = re.compile(r"\A(\d{4})(\d{2})(\d{2})\Z")


def parse_history(payload: Any) -> tuple[dict[str, list[HistoryPoint]], dict[str, Any]]:
    """Parse the viewer's ``value/graph`` answer into per-series points.

    Returns ``(points, shape)``: points per catalog code, oldest first, and
    what the payload held besides them (gaps, other categories) for the
    report.  Raises :class:`CbiHistoryError` on anything that makes the
    payload untrustworthy as a whole.
    """
    if not isinstance(payload, list):
        raise CbiHistoryError(
            f"expected the viewer's list of value rows, got {type(payload).__name__}"
        )
    codes = {category: code for code, category in CATEGORY_IDS.items()}
    points: dict[str, dict[tuple[int, int], Decimal]] = {code: {} for code in CATEGORY_IDS}
    shape: dict[str, Any] = {"rows": len(payload), "gaps": 0, "other_categories": {}}
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise CbiHistoryError(f"row {index} is {type(row).__name__}, not an object")
        category = str(row.get("CATEGORYID") or "")
        code = codes.get(category)
        if code is None:
            # The viewer answers what it was asked; a category not mapped here
            # (the research capture also asked for currency in circulation) is
            # counted and left alone, never stored under a guessed code.
            shape["other_categories"][category] = shape["other_categories"].get(category, 0) + 1
            continue
        amount = row.get("amount")
        if amount in (None, ""):
            # The viewer's empty rows: a period it lists with no value (the
            # quarters of 1350-1351 it has nothing for). A gap, never a zero.
            shape["gaps"] += 1
            continue
        if row.get("subFrequencyId") != QUARTERLY_SUBFREQUENCY_ID:
            raise CbiHistoryError(
                f"row {index} ({code}) is not quarterly (subFrequencyId "
                f"{row.get('subFrequencyId')!r}); refusing the payload"
            )
        if row.get("unitId") != THOUSAND_BILLION_RIAL_UNIT_ID:
            raise CbiHistoryError(
                f"row {index} ({code}) is in unit {row.get('unitId')!r}, not the "
                "thousand-billion-rial unit these series are stored in; refusing "
                "rather than rescaling"
            )
        match = _KH_DATE_RE.match(str(row.get("khDate") or ""))
        if match is None:
            raise CbiHistoryError(f"row {index} ({code}) has no Jalali date: {row.get('khDate')!r}")
        year, month = int(match.group(1)), int(match.group(2))
        if month not in QUARTER_END_MONTHS:
            raise CbiHistoryError(
                f"row {index} ({code}) is dated {match.group(0)}, which is not the end "
                "of a quarter"
            )
        if row.get("datePerYear") not in (None, year):
            raise CbiHistoryError(
                f"row {index} ({code}) says year {row.get('datePerYear')} but is dated "
                f"{match.group(0)}"
            )
        try:
            value = Decimal(str(amount).strip())
        except InvalidOperation as exc:
            raise CbiHistoryError(f"row {index} ({code}) has the value {amount!r}") from exc
        if not value.is_finite() or value <= 0:
            raise CbiHistoryError(
                f"row {index} ({code}) has the value {amount!r}; a monetary aggregate "
                "is positive"
            )
        previous = points[code].get((year, month))
        if previous is not None and previous != value:
            raise CbiHistoryError(
                f"{code} {period_label(year, month)} appears twice, as {previous} and "
                f"{value}; refusing to choose"
            )
        points[code][(year, month)] = value

    empty = sorted(code for code, values in points.items() if not values)
    if empty:
        raise CbiHistoryError(
            f"the payload carries no values for {', '.join(empty)}; all four series "
            "are expected together, and a partial history is refused whole"
        )
    return (
        {
            code: [
                HistoryPoint(code, year, month, value)
                for (year, month), value in sorted(values.items())
            ]
            for code, values in points.items()
        },
        shape,
    )


def _check_overlap(
    engine: Engine, overlap: dict[str, list[HistoryPoint]], as_of: datetime
) -> dict[str, Any]:
    """Compare the viewer with the Excel values stored for the same months.

    Read as of the ingest's own instant — the newest print of each month this
    system holds at the moment it is about to write — not as of the wall
    clock, so the comparison and the write agree about what "stored" means.
    """
    result: dict[str, Any] = {}
    for code, history in overlap.items():
        try:
            stored = point_in_time(engine, code, as_of=as_of)
        except ValueError:
            # Not registered yet: nothing ingested from an edition to compare.
            result[code] = {"compared": 0, "agreed": 0, "disagreements": []}
            continue
        by_period = {item.ref_period_label: item for item in stored.observations}
        compared = agreed = 0
        disagreements = []
        for point in history:
            item = by_period.get(point.ref_period_label)
            if item is None:
                continue
            compared += 1
            excel = Decimal(repr(item.value))
            if abs(point.value - excel) <= OVERLAP_TOLERANCE:
                agreed += 1
                continue
            disagreements.append({
                "period": point.ref_period_label,
                "tsdview": str(point.value),
                "stored": str(excel),
                "stored_method": item.method,
            })
        result[code] = {
            "compared": compared,
            "agreed": agreed,
            "disagreements": disagreements,
        }
        if code in STRICT_OVERLAP_CODES and disagreements:
            first = disagreements[0]
            raise CbiHistoryError(
                f"{code}: the viewer says {first['tsdview']} at {first['period']} where "
                f"the stored Excel edition says {first['stored']} "
                f"({len(disagreements)} of {compared} overlapping months disagree). The "
                "two are the same CBI statistic and agree to the printed decimal when "
                "the payload is what this was written for; refusing the payload"
            )
    return result


def ingest_cbi_history(
    engine: Engine,
    path: str,
    *,
    url: str = "",
    now: Optional[datetime] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Ingest one saved tsdview payload from disk; return the report.

    Parsed, split into written and overlapping months, and checked against the
    stored editions BEFORE anything is written; then, in one transaction, the
    payload is archived (sha256, edition label ``'tsdview'``) and every
    written month goes through
    :func:`app.economic.store.record_observation`.  Re-posting the same file
    writes nothing; ``dry_run`` performs it all and rolls back.
    """
    started = ensure_utc(now) or utcnow()
    if not os.path.isfile(path):
        raise FileNotFoundError(f"no payload at {path}")
    with open(path, "rb") as handle:
        raw = handle.read()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CbiHistoryError(
            f"{os.path.basename(path)} is not the viewer's JSON answer: {exc}"
        ) from exc
    points, shape = parse_history(payload)
    sha256, byte_size = file_sha256_and_size(path)

    written: dict[str, list[HistoryPoint]] = {}
    overlap: dict[str, list[HistoryPoint]] = {}
    for code, history in points.items():
        written[code] = [p for p in history if not excel_can_carry(p.year, p.month)]
        overlap[code] = [p for p in history if excel_can_carry(p.year, p.month)]
    overlap_report = _check_overlap(engine, overlap, started)

    document_url = url or TSDVIEW_URL
    report: dict[str, Any] = {
        "payload": os.path.basename(path),
        "sha256": sha256,
        "byte_size": byte_size,
        "url": document_url,
        "available_at": started.isoformat(),
        "published_at": None,
        "dry_run": bool(dry_run),
        "shape": shape,
        "overlap_with_editions": overlap_report,
        "series": {},
        "inserted": 0, "unchanged": 0, "revised": 0, "raced": 0,
    }
    specs = {spec.code: spec for spec in cbi_specs()}
    conn = engine.connect()
    transaction = conn.begin()
    try:
        document_id, document_inserted = record_source_document(
            conn,
            provider_code=CBI_PROVIDER,
            url=document_url,
            title=(
                "Central Bank of Iran time-series viewer (tsdview), quarter-end "
                "monetary aggregates"
            ),
            media_type=MEDIA_TYPE_JSON,
            byte_size=byte_size,
            content_sha256=sha256,
            fetched_at=started,
            edition_label=EDITION_LABEL,
            document_saved_at=None,
        )
        report["document"] = {
            "id": document_id,
            "status": "inserted" if document_inserted else "existing",
        }
        for code in sorted(written):
            history = written[code]
            series_id = ensure_series(conn, specs[code])
            counts: dict[str, Any] = {
                "inserted": 0, "unchanged": 0, "revised": 0, "raced": 0,
                "written_months": len(history),
                "overlapping_months_not_written": len(overlap[code]),
                "first_period": history[0].ref_period_label if history else None,
                "last_period": history[-1].ref_period_label if history else None,
            }
            for point in history:
                start, end = jalali_month_span(point.year, point.month)
                result = record_observation(
                    conn,
                    series_id,
                    ref_period_start=start,
                    ref_period_end=end,
                    ref_period_label=point.ref_period_label,
                    value=point.value,
                    available_at=started,
                    published_at=None,
                    source_document_id=document_id,
                    collected_at=started,
                    method=METHOD_OLD if code == BASE_MONEY_CODE else None,
                )
                counts[result.status] += 1
            report["series"][code] = counts
            for key in (STATUS_INSERTED, STATUS_UNCHANGED, STATUS_REVISED, STATUS_RACED):
                report[key] += counts[key]
        if dry_run:
            transaction.rollback()
        else:
            transaction.commit()
    except BaseException:
        transaction.rollback()
        raise
    finally:
        conn.close()

    report["finished_at"] = utcnow().isoformat()
    log.info(
        "CBI tsdview history %s: document %s (%s), %d inserted, %d unchanged, %d revised",
        "dry run" if dry_run else "ingest", report["document"]["id"],
        report["document"]["status"], report["inserted"], report["unchanged"],
        report["revised"],
    )
    return report
