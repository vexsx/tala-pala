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
=========================  =====================================================
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from ..equities.adjust import BarParseError, fold_name, parse_deven

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
