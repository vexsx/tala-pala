"""Reading Iranian official documents: the Jalali calendar and Persian text.

Two publishers feed the economic layer from Persian documents — the
Statistical Centre of Iran (:mod:`app.economic.sci`) and the Central Bank of
Iran (:mod:`app.economic.cbi`) — and both need the same two things: a Jalali
month turned into the Gregorian span the store keys on, and a Persian label
folded to a form that compares equal however the publisher spelled it.  They
lived in ``sci.py`` while SCI was the only reader; they are here so the CBI
reader does not import the SCI module to get them, and so the two can never
disagree about a month boundary or a letter.

Nothing in this module knows about either publisher.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta
from typing import Optional

# --- Jalali <-> Gregorian ----------------------------------------------------
#
# The inverse of :func:`app.features.engineering.gregorian_to_jalali`, and the
# same integer algorithm, so the two can never disagree about a boundary: day
# counts from the epoch 979-01-01 (Jalali) = 1600-01-01 (Gregorian) using the
# 33-year arithmetic leap cycle (eight leap years in every 33, ``jy2 // 33 * 8
# + ((jy2 % 33) + 3) // 4``), then a Gregorian 400/100/4-year decomposition.
# The repo already owns the forward direction; this adds only the direction it
# lacks. Round-tripped over every month boundary in 1381..1405 with zero
# mismatches and checked against known dates (1400-01-01 = 2021-03-21; 1403 a
# leap year with a 30-day Esfand, 1404 not), which tests/test_economic_sci.py
# keeps as tests rather than claims.


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


def shift_month(jy: int, jm: int, months: int) -> tuple[int, int]:
    """``(year, month)`` ``months`` Jalali months after (or before) the given one.

    Arithmetic on the label, never on Gregorian dates: a Jalali month is not a
    fixed number of days, so "30 days earlier" can land in the same month or
    skip one, and Farvardin minus one is Esfand of the PREVIOUS year.
    """
    index = jy * 12 + (jm - 1) + months
    return index // 12, index % 12 + 1


def period_label(jy: int, jm: int) -> str:
    """The stored label of a Jalali month: ``'1404-12'``."""
    return f"{jy}-{jm:02d}"


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
    which, before the all-or-nothing rule in each parser, meant ingesting three
    series out of four and reporting success.
    """
    folded = unicodedata.normalize("NFKC", str(text or ""))
    folded = folded.translate(_STRIP).translate(_ARABIC_TO_PERSIAN)
    return re.sub(r"\s+", " ", folded).strip()


# Extended Arabic-Indic (Persian, U+06F0..) and Arabic-Indic (U+0660..) digits.
# NFKC does NOT fold either to ASCII -- they have no compatibility mapping -- so
# a year printed as ۱۴۰۴ survives normalize_fa unchanged and must be folded
# separately wherever a number is read out of text.
_DIGITS = {ord(digit): str(index) for index, digit in enumerate("۰۱۲۳۴۵۶۷۸۹")}
_DIGITS.update({ord(digit): str(index) for index, digit in enumerate("٠١٢٣٤٥٦٧٨٩")})


def fold_digits(text: str) -> str:
    """Persian and Arabic-Indic digits to ASCII; nothing else changes."""
    return text.translate(_DIGITS)


JALALI_MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)
_MONTH_INDEX = {normalize_fa(name): i + 1 for i, name in enumerate(JALALI_MONTHS)}


def month_number(text: object) -> Optional[int]:
    """1..12 for a cell that is exactly a Jalali month name, else ``None``.

    Exact equality after normalisation: ``دی`` is a month and also a substring
    of many words, so anything that merely CONTAINS a month name is not one.
    """
    return _MONTH_INDEX.get(normalize_fa(text))
