"""Tests for the Tehran-exchange gold-fund integration (Addendum 7):
provider parsing, Jalali conversion, TSE market calendar, validation rules,
and fund features in the engineering frame."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from app.core import validation
from app.core.market_hours import closure_started_at, is_market_open
from app.features.engineering import compute_feature_frame, gregorian_to_jalali
from app.providers.base import ProviderError
from app.providers.tse_funds import (
    DEFAULT_FUNDS,
    TSEFundsProvider,
    jalali_to_gregorian,
    parse_funds_config,
    parse_observed_at,
    parse_symbol_payload,
)

NOW = datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc)


def _payload(pl=1_250_000, tvol=1_000_000, buy_i=700_000, sell_i=400_000, **extra):
    base = {
        "date": "1405-04-30", "time": "15:30:00", "l18": "عیار",
        "pl": pl, "pc": pl, "tvol": tvol, "tval": pl * tvol,
        "Buy_I_Volume": buy_i, "Sell_I_Volume": sell_i,
        "Buy_N_Volume": tvol - buy_i, "Sell_N_Volume": tvol - sell_i,
        "Buy_CountI": 1000, "Sell_CountI": 800,
    }
    base.update(extra)
    return base


# --- config ------------------------------------------------------------------

def test_parse_funds_config_default_and_custom():
    assert parse_funds_config("") == DEFAULT_FUNDS
    custom = parse_funds_config("عیار:IR_GOLD_FUND_AYAR, زر:ir_gold_fund_zar")
    assert custom == {"عیار": "IR_GOLD_FUND_AYAR", "زر": "IR_GOLD_FUND_ZAR"}
    # non-fund symbols are refused -> fall back to defaults when nothing valid
    assert parse_funds_config("خودرو:IKCO") == DEFAULT_FUNDS


# --- jalali ------------------------------------------------------------------

def test_jalali_gregorian_roundtrip():
    for g in ((2026, 7, 21), (2025, 3, 12), (2024, 12, 31), (2026, 3, 21)):
        j = gregorian_to_jalali(*g)
        assert jalali_to_gregorian(*j) == g


def test_parse_observed_at_converts_tehran_to_utc():
    # 1403-12-22 Jalali = 2025-03-12 Gregorian; 15:53:06 Tehran = 12:23:06 UTC
    ts = parse_observed_at("1403-12-22", "15:53:06")
    assert ts == datetime(2025, 3, 12, 12, 23, 6, tzinfo=timezone.utc)
    assert parse_observed_at("garbage", "15:00:00") is None


# --- payload parsing ---------------------------------------------------------

def test_parse_symbol_payload_price_and_flow():
    obs, flow = parse_symbol_payload(_payload(), "عیار", "IR_GOLD_FUND_AYAR", NOW)
    assert obs is not None
    assert obs.symbol == "IR_GOLD_FUND_AYAR"
    assert obs.raw_currency == "IRR" and obs.currency == "IRT"
    assert obs.value == pytest.approx(125_000.0)  # rial -> toman
    assert flow is not None
    assert flow["net_i"] == pytest.approx(300_000)
    assert flow["tvol"] == pytest.approx(1_000_000)


def test_parse_symbol_payload_rejects_zero_price():
    obs, flow = parse_symbol_payload(_payload(pl=0, pc=0), "عیار", "X", NOW)
    assert obs is None and flow is None


def test_provider_composite_flow_volume_weighted(monkeypatch):
    provider = TSEFundsProvider(api_key="k", funds={"عیار": "IR_GOLD_FUND_AYAR",
                                                    "طلا": "IR_GOLD_FUND_TALA"})
    payloads = {
        "عیار": _payload(tvol=1_000_000, buy_i=700_000, sell_i=400_000),  # +30%
        "طلا": _payload(pl=90_000, tvol=3_000_000, buy_i=900_000, sell_i=1_500_000),  # -20%
    }
    monkeypatch.setattr(
        provider, "_get_json", lambda url, params: payloads[params["l18"]]
    )
    obs = provider.fetch()
    symbols = {o.symbol for o in obs}
    assert {"IR_GOLD_FUND_AYAR", "IR_GOLD_FUND_TALA", "IR_GOLD_FUND_FLOW"} == symbols
    flow = next(o for o in obs if o.symbol == "IR_GOLD_FUND_FLOW")
    # (300k - 600k) / 4M * 100 = -7.5% (volume-weighted, not averaged)
    assert flow.value == pytest.approx(-7.5)
    assert flow.currency == "PCT"


def test_provider_requires_key_and_raises_when_empty(monkeypatch):
    with pytest.raises(ValueError):
        TSEFundsProvider(api_key="")
    provider = TSEFundsProvider(api_key="k")
    monkeypatch.setattr(provider, "_get_json",
                        lambda url, params: (_ for _ in ()).throw(ProviderError("401")))
    with pytest.raises(ProviderError):
        provider.fetch()


# --- market hours (TSE: Sat-Wed 12:00-17:00 Tehran) --------------------------

def _mh_settings():
    return Settings(
        database_url="sqlite://", market_tehran_open="12:00",
        market_tehran_close="20:00", market_tse_open="12:00",
        market_tse_close="17:00", stale_minutes=30,
    )


def utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


def test_tse_fund_calendar():
    s = _mh_settings()
    # Tue 2026-07-21 13:00 Tehran (09:30 UTC): open
    assert is_market_open("IR_GOLD_FUND_AYAR", utc(2026, 7, 21, 9, 30), s)
    # Tue 17:00 Tehran boundary (13:30 UTC): closed (exclusive)
    assert not is_market_open("IR_GOLD_FUND_AYAR", utc(2026, 7, 21, 13, 30), s)
    # Tue 11:59 Tehran: not yet open
    assert not is_market_open("IR_GOLD_FUND_AYAR", utc(2026, 7, 21, 8, 29), s)
    # Thursday 2026-07-23 13:00 Tehran: closed (unlike the physical market)
    assert not is_market_open("IR_GOLD_FUND_AYAR", utc(2026, 7, 23, 9, 30), s)
    assert is_market_open("IR_GOLD_18K", utc(2026, 7, 23, 9, 30), s)  # physical open Thu
    # Friday: closed; Saturday 13:00 Tehran: open again
    assert not is_market_open("IR_GOLD_FUND_FLOW", utc(2026, 7, 24, 9, 30), s)
    assert is_market_open("IR_GOLD_FUND_FLOW", utc(2026, 7, 25, 9, 30), s)


def test_tse_fund_closure_spans_thu_and_fri():
    s = _mh_settings()
    # Friday noon: the last session closed WEDNESDAY 17:00 Tehran (13:30 UTC)
    start = closure_started_at("IR_GOLD_FUND_AYAR", utc(2026, 7, 24, 9, 0), s)
    assert start == utc(2026, 7, 22, 13, 30)


# --- validation --------------------------------------------------------------

def test_flow_symbol_oscillation_is_ok():
    quality, _ = validation.classify_observation(
        "IR_GOLD_FUND_FLOW", -22.0, [5.0, 3.0, -1.0, 8.0, 2.0], 5.0
    )
    assert quality == "ok"  # a sign-flipping swing must NOT be held as suspect
    quality, _ = validation.classify_observation("IR_GOLD_FUND_FLOW", 150.0, [], None)
    assert quality == "outlier"  # but the sanity bounds still apply


def test_fund_price_jump_still_guarded():
    quality, _ = validation.classify_observation(
        "IR_GOLD_FUND_AYAR", 200_000.0, [100_000.0] * 10, 100_000.0
    )
    assert quality == "suspect"  # fund PRICES keep the normal jump guard


# --- features ----------------------------------------------------------------

def _series(vals, start="2025-01-01"):
    idx = pd.date_range(start, periods=len(vals), freq="D", tz="UTC")
    return pd.Series(np.asarray(vals, dtype=float), index=idx)


def test_feature_frame_includes_fund_features():
    n = 60
    gold = _series(np.linspace(100_000, 110_000, n))
    fund = _series(np.linspace(1_000, 1_120, n))
    flow = _series(np.sin(np.linspace(0, 6, n)) * 10)
    frame = compute_feature_frame(gold, gold_fund=fund, fund_flow=flow)
    for col in ("fund_ret_1", "fund_ret_5", "fund_ratio_z_30",
                "fund_flow", "fund_flow_ma5", "fund_flow_chg_5"):
        assert col in frame.columns, col
    assert frame["fund_ret_1"].iloc[-1] == pytest.approx(
        fund.pct_change().iloc[-1]
    )
    # without fund context the columns must be absent (no NaN pollution)
    plain = compute_feature_frame(gold)
    assert "fund_ret_1" not in plain.columns


# --- fetch-slot quota guard ---------------------------------------------------

def _slot_settings():
    return Settings(database_url="sqlite://", tsetmc_fetch_times="12:00,15:00,18:00")


def test_funds_job_slots(engine):
    from datetime import timedelta as _td

    from app.db import raw_observations, utcnow
    from app.jobs.collect import funds_job_due

    s = _slot_settings()
    # Tue 2026-07-21. Before the first slot (11:30 Tehran = 08:00 UTC): not due
    assert not funds_job_due(engine, s, now=utc(2026, 7, 21, 8, 0))
    # 12:05 Tehran (08:35 UTC), nothing fetched yet: due
    assert funds_job_due(engine, s, now=utc(2026, 7, 21, 8, 35))

    # record a fetch at 12:06 Tehran -> 12-slot consumed, 14:00 Tehran not due
    with engine.begin() as conn:
        conn.execute(raw_observations.insert().values(
            provider_code="tse_funds", symbol="IR_GOLD_FUND_AYAR",
            raw_value=1.0, unit="u", currency="IRR",
            observed_at=utc(2026, 7, 21, 8, 36), collected_at=utc(2026, 7, 21, 8, 36),
            quality="ok", dedupe_key="slot-test-1",
        ))
    assert not funds_job_due(engine, s, now=utc(2026, 7, 21, 9, 30))   # 13:00 Tehran
    # 15:05 Tehran (11:35 UTC): the 15:00 slot is unconsumed -> due
    assert funds_job_due(engine, s, now=utc(2026, 7, 21, 11, 35))
    # 18:05 Tehran (14:35 UTC): 18:00 slot (post-close by design) -> due
    assert funds_job_due(engine, s, now=utc(2026, 7, 21, 14, 35))


def test_funds_job_skips_thu_fri_and_never_repays(engine):
    from app.jobs.collect import funds_job_due

    s = _slot_settings()
    # Thursday 2026-07-23 and Friday 2026-07-24, mid-slot times: never due
    assert not funds_job_due(engine, s, now=utc(2026, 7, 23, 8, 35))
    assert not funds_job_due(engine, s, now=utc(2026, 7, 24, 11, 35))
    # Saturday after ALL slots passed with no fetches: due exactly once
    # (max(passed) = 18:00 slot; one round covers it, quota is not repaid)
    assert funds_job_due(engine, s, now=utc(2026, 7, 25, 15, 0))  # 18:30 Tehran
