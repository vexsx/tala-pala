"""Tehran-exchange gold investment funds ("gold boxes": عیار، طلا، کهربا …).

Direct tsetmc.com access is geo-blocked outside Iran, so quotes come through
BrsApi's TSETMC mirror (``Api.BrsApi.ir/Tsetmc/Symbol.php``, same BRSAPI_KEY
as the market provider). Per configured fund the provider emits:

* the fund unit price (``pl`` last trade, rial -> toman), symbol per config;
* one composite ``IR_GOLD_FUND_FLOW`` observation — the volume-weighted
  *retail net-flow* percent across all funds::

      flow_pct = (Buy_I_Volume - Sell_I_Volume) / tvol * 100

  Positive = individuals (حقیقی) are net buyers from institutions — the
  classic Iranian-market sentiment gauge. Stored with currency 'PCT'
  (may legitimately be negative).

Funds trade Sat-Wed 12:00-17:00 Tehran (see core/market_hours TSE calendar).
``observed_at`` comes from the API's own Jalali date + Tehran time so closed
-market polls dedupe naturally instead of re-inserting stale rows.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .base import Observation, Provider, ProviderError

log = logging.getLogger(__name__)

SYMBOL_URL = "https://Api.BrsApi.ir/Tsetmc/Symbol.php"

# ticker (TSETMC l18) -> canonical symbol; overridable via TSETMC_FUNDS
DEFAULT_FUNDS: dict[str, str] = {
    "عیار": "IR_GOLD_FUND_AYAR",     # Mofid — the fund the user asked for
    "طلا": "IR_GOLD_FUND_TALA",      # Lotus Parsian, the oldest gold fund
    "کهربا": "IR_GOLD_FUND_KAHRABA", # Kian
}

FLOW_SYMBOL = "IR_GOLD_FUND_FLOW"

TEHRAN_OFFSET = timedelta(hours=3, minutes=30)  # Iran abolished DST in 2022


def parse_funds_config(raw: str) -> dict[str, str]:
    """Parse ``TSETMC_FUNDS`` = ``ticker:SYMBOL,ticker:SYMBOL`` (empty -> defaults)."""
    raw = (raw or "").strip()
    if not raw:
        return dict(DEFAULT_FUNDS)
    out: dict[str, str] = {}
    for part in raw.split(","):
        if ":" not in part:
            continue
        ticker, symbol = part.split(":", 1)
        ticker, symbol = ticker.strip(), symbol.strip().upper()
        if ticker and symbol.startswith("IR_GOLD_FUND"):
            out[ticker] = symbol
    return out or dict(DEFAULT_FUNDS)


def jalali_to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    """Standard integer Jalali->Gregorian conversion (inverse of engineering.py)."""
    jy2 = jy - 979
    j_day_no = 365 * jy2 + (jy2 // 33) * 8 + ((jy2 % 33) + 3) // 4
    j_day_no += (jd - 1) + (31 * (jm - 1) if jm <= 7 else 186 + 30 * (jm - 7))
    g_day_no = j_day_no + 79

    gy = 1600 + 400 * (g_day_no // 146097)
    g_day_no %= 146097
    leap = True
    if g_day_no >= 36525:
        g_day_no -= 1
        gy += 100 * (g_day_no // 36524)
        g_day_no %= 36524
        if g_day_no >= 365:
            g_day_no += 1
        else:
            leap = False
    gy += 4 * (g_day_no // 1461)
    g_day_no %= 1461
    if g_day_no >= 366:
        leap = False
        g_day_no -= 1
        gy += g_day_no // 365
        g_day_no %= 365
    months = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 12 and g_day_no >= months[gm]:
        g_day_no -= months[gm]
        gm += 1
    return gy, gm + 1, g_day_no + 1


def parse_observed_at(date_str: str, time_str: str) -> Optional[datetime]:
    """'1403-12-22' (Jalali) + '15:53:06' (Tehran local) -> aware UTC datetime."""
    try:
        jy, jm, jd = (int(x) for x in str(date_str).replace("/", "-").split("-"))
        hh, mm, ss = (int(x) for x in str(time_str).split(":"))
        gy, gm, gd = jalali_to_gregorian(jy, jm, jd)
        local = datetime(gy, gm, gd, hh, mm, ss, tzinfo=timezone(TEHRAN_OFFSET))
        return local.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def parse_symbol_payload(
    payload: dict, ticker: str, symbol: str, now_utc: datetime
) -> tuple[Optional[Observation], Optional[dict]]:
    """One fund payload -> (price observation, flow components) or (None, None).

    Flow components: {"net_i": Buy_I - Sell_I, "tvol": total volume,
    "observed_at": ...} for the composite flow calculation.
    """
    try:
        last_rial = float(payload.get("pl") or payload.get("pc") or 0)
    except (TypeError, ValueError):
        return None, None
    if last_rial <= 0:
        return None, None
    observed = parse_observed_at(
        str(payload.get("date", "")), str(payload.get("time", ""))
    ) or now_utc

    obs = Observation(
        provider_code="tse_funds",
        symbol=symbol,
        raw_value=last_rial,
        raw_unit="IRR/unit",
        raw_currency="IRR",
        value=last_rial / 10.0,  # rial -> toman
        currency="IRT",
        unit="unit",
        observed_at=observed,
        raw_payload={
            "l18": ticker,
            "pl": payload.get("pl"), "pc": payload.get("pc"),
            "tvol": payload.get("tvol"), "tval": payload.get("tval"),
            "Buy_I_Volume": payload.get("Buy_I_Volume"),
            "Sell_I_Volume": payload.get("Sell_I_Volume"),
            "Buy_N_Volume": payload.get("Buy_N_Volume"),
            "Sell_N_Volume": payload.get("Sell_N_Volume"),
            "Buy_CountI": payload.get("Buy_CountI"),
            "Sell_CountI": payload.get("Sell_CountI"),
        },
    )
    flow: Optional[dict] = None
    try:
        tvol = float(payload.get("tvol") or 0)
        buy_i = float(payload.get("Buy_I_Volume") or 0)
        sell_i = float(payload.get("Sell_I_Volume") or 0)
        if tvol > 0:
            flow = {"net_i": buy_i - sell_i, "tvol": tvol, "observed_at": observed}
    except (TypeError, ValueError):
        pass
    return obs, flow


class TSEFundsProvider(Provider):
    """Gold investment funds on the Tehran exchange, via BrsApi's TSETMC mirror."""

    code = "tse_funds"
    category = "iran_fund"

    def __init__(self, api_key: str, funds: Optional[dict[str, str]] = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        if not api_key:
            raise ValueError("TSEFundsProvider requires BRSAPI_KEY")
        self.api_key = api_key
        self.funds = funds or dict(DEFAULT_FUNDS)

    def fetch(self) -> list[Observation]:
        now = datetime.now(timezone.utc)
        observations: list[Observation] = []
        flows: list[dict] = []
        errors: list[str] = []
        for ticker, symbol in self.funds.items():
            try:
                payload = self._get_json(
                    SYMBOL_URL, params={"key": self.api_key, "l18": ticker}
                )
            except ProviderError as exc:
                errors.append(f"{ticker}: {exc}")
                continue
            if not isinstance(payload, dict):
                errors.append(f"{ticker}: unexpected payload type")
                continue
            obs, flow = parse_symbol_payload(payload, ticker, symbol, now)
            if obs is not None:
                observations.append(obs)
            if flow is not None:
                flows.append(flow)

        if flows:
            total_vol = sum(f["tvol"] for f in flows)
            if total_vol > 0:
                flow_pct = sum(f["net_i"] for f in flows) / total_vol * 100.0
                observations.append(
                    Observation(
                        provider_code=self.code,
                        symbol=FLOW_SYMBOL,
                        raw_value=flow_pct,
                        raw_unit="pct_of_volume",
                        raw_currency="PCT",
                        value=flow_pct,
                        currency="PCT",
                        unit="pct",
                        observed_at=max(f["observed_at"] for f in flows),
                        raw_payload={"n_funds": len(flows), "total_volume": total_vol},
                    )
                )

        if not observations:
            raise ProviderError(
                "tse_funds: no fund delivered data"
                + (f" ({'; '.join(errors[:3])})" if errors else "")
            )
        if errors:
            log.warning("tse_funds partial fetch: %s", "; ".join(errors))
        return observations
