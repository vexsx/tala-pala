"""Parsers for the TSETMC market-level payloads.

Each parser takes the decoded JSON exactly as the endpoint served it and
returns plain records, refusing anything that is not the shape it was written
against.  Nothing here touches the database, and nothing here corrects a
value: the one correction this package makes (the power of ten in
:mod:`app.bourse.scale`) is computed over a PARSED series, so a parser never
has to decide what a value "should" have been.

The payloads, as measured on 2026-09-29:

=========================  =====================================================
``GetIndexB2History/<i>``  ``{"indexB2": [{insCode, dEven, xNivInuClMresIbs,
                           xNivInuPbMresIbs, xNivInuPhMresIbs}]}`` — close, low,
                           high; no open.  Ascending.  The settled sessions
                           only: fetched mid-session, the newest row is the
                           previous session.
``GetIndexB1LastAll/All/<f>``  ``{"indexB1": [{insCode, xDrNivJIdx004,
                           xVarIdxJRfV, lVal30, ...}]}`` — every index's live
                           value and its percent change.  Flow 1 is the bourse,
                           2 the Farabourse.
``GetMarketValueByFlow/<f>/9999``  ``{"marketValue": [{deven, marketCap}]}`` —
                           rials, descending.
``GetClientTypeHistory/<i>``  ``{"clientType": [{recDate, insCode, buy_I_Value,
                           ...}]}`` — counts arrive as JSON floats on some fields
                           (``buy_I_Count: 2549.0``) and ints on others.
``GetMarketOverview/<f>``  ``{"marketOverview": {...}}`` — one live object.
``GetSectorsSummary``      ``{"sectorSummeries": [{cSecVal, lSecVal, c1..c4}]}``
                           — sic, TSETMC's spelling; cSecVal carries a trailing
                           space ("01 ").
``GetMarketWatch``         ``{"marketwatch": [{insCode, insID, lva, lvc, csv,
                           ...}]}`` — every instrument, 3,786 rows; insCode a
                           STRING here.  yVal/flow/cGrValCot come and go
                           between responses, so nothing reads them.
``GetStaticData``          ``{"staticData": [{id, code, type, name, ...}]}`` —
                           ``code`` an int, ``name`` space-padded to 150.
``GetInstrmentsHistoryInDay/<d>``  ``{"closingPriceDailyHistoryWithInstDetails":
                           [{insCode, pClosing, priceYesterday, qTotCap, ...}]}``
                           — traded instruments only; ``dEven`` is 0 on every
                           row; insCode a bare JSON NUMBER, most above 2^53.
=========================  =====================================================
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..equities.adjust import BarParseError, fold_name, fold_symbol, parse_deven

# Tehran has kept UTC+03:30 without daylight saving since 2022, and kept DST
# before that; the zone database knows both, so a snapshot from any year lands
# on the right UTC instant without a hand-written offset.
TEHRAN = ZoneInfo("Asia/Tehran")

# TSETMC's flow numbers, and the market vocabulary 0028/0029 use for them.
FLOW_MARKETS = {1: "bourse", 2: "farabourse"}


class MarketParseError(BarParseError):
    """A market-level payload is not the shape this parser was written for.

    A subclass of :class:`BarParseError` so the ingest endpoint's existing
    "this is a statement about the payload" handling applies unchanged.
    """


def _list_field(payload: Any, field: str) -> list:
    if not isinstance(payload, dict):
        raise MarketParseError(
            f"expected a JSON object with a {field!r} list, got {type(payload).__name__}"
        )
    rows = payload.get(field)
    if not isinstance(rows, list):
        raise MarketParseError(
            f"payload carries no {field!r} list; keys present: {sorted(payload)[:8]}. "
            "The endpoint shape changed or the wrong file was posted."
        )
    if not rows:
        raise MarketParseError(f"{field!r} is empty: nothing to ingest")
    return rows


def _num(row: dict, key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MarketParseError(f"{key}={value!r} is not a number")
    out = float(value)
    if math.isnan(out) or math.isinf(out):
        raise MarketParseError(f"{key}={value!r} is not a finite number")
    return out


def _count(row: dict, key: str) -> int:
    """A count that TSETMC may serve as 2549.0.  Integral or refused."""
    value = _num(row, key)
    if value < 0 or value != int(value):
        raise MarketParseError(f"{key}={value!r} is not a non-negative whole count")
    return int(value)


def _code(value: Any, what: str) -> str:
    """An insCode, which TSETMC serves as a string on one endpoint and a
    number on another.  Digits only, as everywhere else in this repo."""
    text = str(value if value is not None else "").strip()
    if not text.isdigit():
        raise MarketParseError(f"{what} insCode {value!r} is not a plain number")
    return text


_INS_CODE_RE = re.compile(r"\A[0-9]{1,20}\Z")


def exact_code(value: Any, what: str) -> str:
    """An insCode exactly as served: a digit string, or a JSON INTEGER.

    Never a float.  GetInstrmentsHistoryInDay serves insCode as a bare JSON
    number and 2,012 of the 2,290 on 2026-09-28 exceed 2^53, where a float
    has already rounded away the last digits — فولاد's 46348559193224090
    becomes 46348559193224088, another instrument's name or nobody's.  Python's json
    keeps an integer literal exact, so an int here is the served digits and a
    float means something upstream already went through a double.
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise MarketParseError(
            f"{what} insCode {value!r} arrived as a {type(value).__name__}; an insCode "
            "above 2^53 does not survive a float, so only digits or an integer are accepted"
        )
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise MarketParseError(f"{what} insCode {value!r} is not a plain number")
    if not _INS_CODE_RE.match(text):
        raise MarketParseError(f"{what} insCode {value!r} is not a plain number")
    return text


def _ascending_unique(items: list, key, what: str) -> list:
    items.sort(key=key)
    seen = set()
    for item in items:
        k = key(item)
        if k in seen:
            raise MarketParseError(
                f"{what} carries {k} twice. A session is one row; two rows for the "
                "same date mean a corrupt or concatenated payload."
            )
        seen.add(k)
    return items


# --- index history -----------------------------------------------------------


@dataclass(frozen=True)
class IndexValue:
    trade_date: date
    close: float
    low: Optional[float]
    high: Optional[float]


@dataclass(frozen=True)
class IndexHistory:
    ins_code: str
    values: list[IndexValue]
    # Rows whose close was zero or negative.  Not an index level — a fault —
    # so they never reach the table; counted so the report can say so.
    dropped_nonpositive: int
    dropped_dates: list[date]


def parse_index_history(payload: Any, ins_code: str = "") -> IndexHistory:
    """Parse ``GetIndexB2History``.  The insCode comes from the payload itself
    and must be a single one — and the one asked for, when one was."""
    rows = _list_field(payload, "indexB2")
    codes = {_code(r.get("insCode"), "index") for r in rows if isinstance(r, dict)}
    if len(codes) != 1 or len(rows) != sum(isinstance(r, dict) for r in rows):
        raise MarketParseError(
            f"an index history must carry exactly one insCode; found {sorted(codes)[:4]}"
        )
    code = codes.pop()
    if ins_code and code != ins_code:
        raise MarketParseError(
            f"asked for index {ins_code} and the payload carries {code}. Refusing to "
            "store one index's values under another's code."
        )
    values: list[IndexValue] = []
    dropped: list[date] = []
    for r in rows:
        d = parse_deven(r.get("dEven"))
        close = _num(r, "xNivInuClMresIbs")
        if close <= 0:
            dropped.append(d)
            continue
        low = r.get("xNivInuPbMresIbs")
        high = r.get("xNivInuPhMresIbs")
        values.append(
            IndexValue(
                trade_date=d,
                close=close,
                # As served, including a zero — they are evidence, not a band,
                # and nothing reads them (0029's header).
                low=_num(r, "xNivInuPbMresIbs") if low is not None else None,
                high=_num(r, "xNivInuPhMresIbs") if high is not None else None,
            )
        )
    if not values:
        raise MarketParseError(f"index {code}: every row has a non-positive close")
    _ascending_unique(values, lambda v: v.trade_date, f"index {code}")
    return IndexHistory(
        ins_code=code,
        values=values,
        dropped_nonpositive=len(dropped),
        dropped_dates=sorted(dropped),
    )


# --- live index values (B1) --------------------------------------------------


@dataclass(frozen=True)
class LiveIndex:
    ins_code: str
    value: float
    change_pct: float
    name_fa: str


def parse_index_live(payload: Any) -> dict[str, LiveIndex]:
    """Parse ``GetIndexB1LastAll`` into {insCode: live value}.

    Used only as a CROSS-CHECK on the corrected history, never stored as a
    session value: it is an intraday figure for a session that has not
    settled, and the history already carries every settled one.
    """
    out: dict[str, LiveIndex] = {}
    for r in _list_field(payload, "indexB1"):
        if not isinstance(r, dict):
            raise MarketParseError("an indexB1 row is not an object")
        code = _code(r.get("insCode"), "live index")
        value = _num(r, "xDrNivJIdx004")
        if value <= 0:
            continue  # no live figure to check against; not a fault of the history
        out[code] = LiveIndex(
            ins_code=code,
            value=value,
            change_pct=_num(r, "xVarIdxJRfV"),
            name_fa=fold_name(r.get("lVal30")),
        )
    return out


# --- market value ------------------------------------------------------------


@dataclass(frozen=True)
class MarketValues:
    market: str
    values: list[tuple[date, float]]  # (trade_date, rials), ascending
    dropped_nonpositive: int


def parse_market_values(payload: Any, market: str) -> MarketValues:
    if market not in FLOW_MARKETS.values():
        raise MarketParseError(f"unknown market {market!r}")
    values: list[tuple[date, float]] = []
    dropped = 0
    for r in _list_field(payload, "marketValue"):
        if not isinstance(r, dict):
            raise MarketParseError("a marketValue row is not an object")
        cap = _num(r, "marketCap")
        if cap <= 0:
            dropped += 1
            continue
        values.append((parse_deven(r.get("deven")), cap))
    if not values:
        raise MarketParseError(f"{market}: every market value is non-positive")
    _ascending_unique(values, lambda v: v[0], f"{market} market value")
    return MarketValues(market=market, values=values, dropped_nonpositive=dropped)


# --- client type (حقیقی/حقوقی) -------------------------------------------------


# (column, payload key) — I is individuals (حقیقی), N institutions (حقوقی).
CLIENT_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("buy_i_count", "buy_I_Count", True),
    ("buy_n_count", "buy_N_Count", True),
    ("sell_i_count", "sell_I_Count", True),
    ("sell_n_count", "sell_N_Count", True),
    ("buy_i_volume", "buy_I_Volume", False),
    ("buy_n_volume", "buy_N_Volume", False),
    ("sell_i_volume", "sell_I_Volume", False),
    ("sell_n_volume", "sell_N_Volume", False),
    ("buy_i_value", "buy_I_Value", False),
    ("buy_n_value", "buy_N_Value", False),
    ("sell_i_value", "sell_I_Value", False),
    ("sell_n_value", "sell_N_Value", False),
)


@dataclass(frozen=True)
class ClientFlows:
    ins_code: str
    rows: list[dict[str, Any]]  # each: trade_date + every CLIENT_FIELDS column


def parse_client_types(payload: Any, ins_code: str = "") -> ClientFlows:
    """Parse ``GetClientTypeHistory``.  Values stay as served — the identities
    they should satisfy (buy total = sell total = the bar's traded value) are
    checked where they are READ, per session, so a session that fails them is
    excluded from an aggregate rather than silently trusted or silently lost."""
    rows = _list_field(payload, "clientType")
    codes = {_code(r.get("insCode"), "client type") for r in rows if isinstance(r, dict)}
    if len(codes) != 1 or len(rows) != sum(isinstance(r, dict) for r in rows):
        raise MarketParseError(
            f"a client-type history must carry exactly one insCode; found {sorted(codes)[:4]}"
        )
    code = codes.pop()
    if ins_code and code != ins_code:
        raise MarketParseError(
            f"asked for {ins_code} and the client-type payload carries {code}"
        )
    out: list[dict[str, Any]] = []
    for r in rows:
        row: dict[str, Any] = {"trade_date": parse_deven(r.get("recDate"))}
        for column, key, is_count in CLIENT_FIELDS:
            if is_count:
                row[column] = _count(r, key)
            else:
                v = _num(r, key)
                if v < 0:
                    raise MarketParseError(
                        f"{code} {row['trade_date']}: {key}={v!r} is negative"
                    )
                row[column] = v
        out.append(row)
    _ascending_unique(out, lambda r: r["trade_date"], f"client types of {code}")
    return ClientFlows(ins_code=code, rows=out)


# --- overview and sector summary --------------------------------------------


def _tehran_instant(deven: Any, heven: Any) -> datetime:
    """TSETMC's (dEven, hEven) — a Gregorian date and an HHMMSS integer in
    Tehran local time — as a UTC instant."""
    d = parse_deven(deven)
    try:
        h = int(heven)
    except (TypeError, ValueError) as exc:
        raise MarketParseError(f"hEven {heven!r} is not an integer time") from exc
    hh, mm, ss = h // 10000, (h // 100) % 100, h % 100
    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59):
        raise MarketParseError(f"hEven {h} is not an HHMMSS time")
    local = datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=TEHRAN)
    return local.astimezone(timezone.utc)


def _positive_or_none(row: dict, key: str) -> Optional[float]:
    """TSETMC sends 0.0 for a figure a market does not have (the Farabourse's
    equal-weighted index).  That is "not stated", not zero."""
    v = _num(row, key)
    return v if v != 0 else None


def parse_market_overview(payload: Any, market: str) -> dict[str, Any]:
    if market not in FLOW_MARKETS.values():
        raise MarketParseError(f"unknown market {market!r}")
    if not isinstance(payload, dict) or not isinstance(payload.get("marketOverview"), dict):
        raise MarketParseError("payload carries no marketOverview object")
    mo = payload["marketOverview"]
    index_value = _positive_or_none(mo, "indexLastValue")
    ew_value = _positive_or_none(mo, "indexEqualWeightedLastValue")
    return {
        "market": market,
        "activity_at": _tehran_instant(
            mo.get("marketActivityDEven"), mo.get("marketActivityHEven")
        ),
        "index_value": index_value,
        "index_change": _num(mo, "indexChange") if index_value is not None else None,
        "ew_index_value": ew_value,
        "ew_index_change": (
            _num(mo, "indexEqualWeightedChange") if ew_value is not None else None
        ),
        "trade_count": _count(mo, "marketActivityZTotTran"),
        "trade_volume": _num(mo, "marketActivityQTotTran"),
        "trade_value": _num(mo, "marketActivityQTotCap"),
        "market_value": _positive_or_none(mo, "marketValue"),
        "market_state": str(mo.get("marketState") or "").strip(),
        "market_state_title": fold_name(mo.get("marketStateTitle")),
    }


def parse_sector_summary(payload: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in _list_field(payload, "sectorSummeries"):
        if not isinstance(r, dict):
            raise MarketParseError("a sector summary row is not an object")
        code = str(r.get("cSecVal") or "").strip()
        if not code.isdigit():
            raise MarketParseError(f"sector code {r.get('cSecVal')!r} is not numeric")
        out.append(
            {
                "sector_code": code,
                "sector_fa": fold_name(r.get("lSecVal")),
                # TSETMC's own four buckets, in its own order (its tooltips:
                # "more than 2% price decrease", "less than 2% decrease", "less
                # than 2% increase", "more than 2% increase").
                "down_over_2": _count(r, "c1"),
                "down_under_2": _count(r, "c2"),
                "up_under_2": _count(r, "c3"),
                "up_over_2": _count(r, "c4"),
            }
        )
    _ascending_unique(out, lambda r: r["sector_code"], "sector summary")
    return out


# --- the share universe (migration 0030) --------------------------------------


# insID prefix -> market.  Measured on GetMarketWatch 2026-09-29: IRO1 648
# rows, IRO3 354, IRO7 150, IRO5 10 — and IRO5 checked with GetInstrumentInfo
# (خگلپا: the Farabourse's نوآفرین growth market, 1.8 billion shares
# outstanding, sector 34), so it is a company, not a fund or a bond.  Options
# (IRO9/IROF), bonds (IRB*), funds (IRT*) and rights (IRR*) carry their
# underlying's or issuer's `csv`, and letting one in would book its turnover
# to that sector.
SHARE_MARKETS = {"IRO1": "bourse", "IRO3": "farabourse", "IRO7": "paye", "IRO5": "sme"}
# insID[8:] -> board.  Every board is its own insCode and its own order book.
SHARE_BOARDS = {"0001": "main", "0002": "block", "0003": "secondary"}
_INS_ID_RE = re.compile(r"\AIR[A-Z0-9]{10}\Z")
_SECTOR_RE = re.compile(r"\A[0-9]{2}\Z")


@dataclass(frozen=True)
class ListedShare:
    ins_code: str
    ins_id: str        # the market-watch insID, e.g. IRO1FOLD0001 — NOT the ISIN
    symbol_fa: str     # fold_symbol(lva)
    name_fa: str       # fold_name(lvc)
    market: str        # a SHARE_MARKETS value
    board: str         # a SHARE_BOARDS value, else 'other'
    company_code: str  # insID[:8]
    sector_code: str   # csv.strip(); '' when TSETMC served no two-digit code


@dataclass(frozen=True)
class MarketWatch:
    shares: list[ListedShare]
    rows_total: int  # every instrument in the payload, shares or not


def parse_market_watch(payload: Any) -> MarketWatch:
    """Parse ``GetMarketWatch`` down to the SHARES it lists.

    Classified by insID and nothing else: yVal/flow/cGrValCot are absent from
    some responses to the very same URL.  A row with a share prefix must carry
    everything a share row needs — a shape change there is refused, not
    skipped, because skipping would mark every such share delisted.
    """
    rows = _list_field(payload, "marketwatch")
    shares: list[ListedShare] = []
    seen: set[str] = set()
    for r in rows:
        if not isinstance(r, dict):
            raise MarketParseError("a marketwatch row is not an object")
        ins_id = str(r.get("insID") or "").strip()
        market = SHARE_MARKETS.get(ins_id[:4])
        if market is None:
            continue
        if not _INS_ID_RE.match(ins_id):
            raise MarketParseError(f"market-watch insID {ins_id!r} is not a 12-character id")
        code = exact_code(r.get("insCode"), f"market-watch row {ins_id}")
        if code in seen:
            raise MarketParseError(f"the market watch lists insCode {code} twice")
        seen.add(code)
        symbol = fold_symbol(r.get("lva"))
        if not symbol:
            raise MarketParseError(f"market-watch row {ins_id} ({code}) carries no symbol")
        csv = str(r.get("csv") or "").strip()
        shares.append(
            ListedShare(
                ins_code=code,
                ins_id=ins_id,
                symbol_fa=symbol,
                name_fa=fold_name(r.get("lvc")),
                market=market,
                board=SHARE_BOARDS.get(ins_id[8:], "other"),
                company_code=ins_id[:8],
                sector_code=csv if _SECTOR_RE.match(csv) else "",
            )
        )
    if not shares:
        raise MarketParseError(
            f"the market watch's {len(rows)} rows include no share (insID IRO1/IRO3/IRO5/"
            "IRO7); the response is not the market watch this was written against"
        )
    return MarketWatch(shares=shares, rows_total=len(rows))


def parse_static_sectors(payload: Any) -> dict[str, str]:
    """``GetStaticData``'s industrial groups as {two-digit code: folded name}.

    ``code`` is an int (1 for "01"), zero-padded here to match the market
    watch's ``csv``; the name is space-padded to 150 characters and stripped.
    """
    out: dict[str, str] = {}
    for r in _list_field(payload, "staticData"):
        if not isinstance(r, dict):
            raise MarketParseError("a staticData row is not an object")
        if r.get("type") != "IndustrialGroup":
            continue
        raw = r.get("code")
        if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 99:
            raise MarketParseError(f"industrial group code {raw!r} is not an integer 0..99")
        code = f"{raw:02d}"
        if code in out:
            raise MarketParseError(f"industrial group {code} is listed twice")
        out[code] = fold_name(r.get("name"))
    if not out:
        raise MarketParseError("staticData carries no IndustrialGroup rows")
    return out


# --- one market session (GetInstrmentsHistoryInDay) ---------------------------


DAY_FILE_FIELD = "closingPriceDailyHistoryWithInstDetails"


def parse_day_file(payload: Any) -> dict[str, dict]:
    """Index a day file's rows by exact insCode, WITHOUT reading their values.

    Every row's insCode is validated — a float among them means the file went
    through a double somewhere and none of its codes can be trusted — but the
    prices are read only for the rows that are stored (:func:`parse_session_row`),
    so a malformed bond row cannot cost the day its shares.  The file carries
    no date of its own (``dEven`` is 0 on every row); the caller supplies it.
    """
    out: dict[str, dict] = {}
    for r in _list_field(payload, DAY_FILE_FIELD):
        if not isinstance(r, dict):
            raise MarketParseError("a day-file row is not an object")
        code = exact_code(r.get("insCode"), "day-file")
        if code in out:
            raise MarketParseError(
                f"the day file carries insCode {code} twice; one session is one row"
            )
        out[code] = r
    return out


def parse_session_row(row: dict) -> dict[str, Any]:
    """One share's session from a day-file row, or MarketParseError naming why
    it cannot be stored — the table's CHECKs, stated before the database has
    to state them for the whole day."""
    close = _num(row, "pClosing")
    if close <= 0:
        raise MarketParseError(f"pClosing={close!r} is not a closing price")
    # MEASURED, and the reason this column is worth storing: priceYesterday
    # here is TSETMC's corporate-action-ADJUSTED reference, not the previous
    # session's close.  On فولاد's ex-dates the day file says 3,982 on
    # 2025-03-12 (previous close 5,530) and 4,800 on 2024-07-22 (previous
    # close 5,200) — exactly GetClosingPriceDailyList's priceYesterday for
    # those sessions (tests/test_market_shares.py pins the first).  So
    # close / price_yesterday over a share's traded sessions chains into a
    # return a capital increase does not break, without the share's bars.
    reference = _num(row, "priceYesterday")
    if reference <= 0:
        raise MarketParseError(
            f"priceYesterday={reference!r}: no reference price (a first session has none)"
        )
    last_trade: Optional[float] = None
    if row.get("pDrCotVal") is not None:
        last_trade = _num(row, "pDrCotVal")
        if last_trade < 0:
            raise MarketParseError(f"pDrCotVal={last_trade!r} is negative")
        # 0 is TSETMC's "not stated"; the column is NULL for that, not zero.
        last_trade = last_trade or None
    value = _num(row, "qTotCap")
    volume = _num(row, "qTotTran5J")
    if value < 0 or volume < 0:
        raise MarketParseError(f"a negative value or volume ({value!r}, {volume!r})")
    return {
        "close": close,
        "last_trade": last_trade,
        "price_yesterday": reference,
        "value": value,
        "volume": volume,
        "trades": _count(row, "zTotTran"),
    }
