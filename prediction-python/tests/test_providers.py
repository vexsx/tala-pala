"""Provider parsing tests against saved fixtures + one respx round trip."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import respx
from sqlalchemy import func, insert, select

from app.core import validation
from app.db import app_settings, data_providers, prices, raw_observations, utcnow
from app.jobs.collect import PEER_DISPERSION_KEY, run_collect
from app.providers import (
    alanchand,
    brsapi,
    gold_api,
    metals_dev,
    milligold,
    navasan,
    pricedb,
    registry,
    stooq,
    tgju,
    yahoo,
)
from app.providers.base import Observation, Provider, ProviderError

from .conftest import load_fixture_json, load_fixture_text


# --- TGJU -------------------------------------------------------------------


def test_tgju_parse_live_fixture():
    payload = load_fixture_json("tgju_live.json")
    observations = {o.symbol: o for o in tgju.parse_live(payload)}
    assert set(observations) == {"IR_GOLD_18K", "IR_COIN_EMAMI", "USD_IRT", "XAUUSD"}

    gold = observations["IR_GOLD_18K"]
    assert gold.raw_value == 182_954_000.0
    assert gold.raw_currency == "IRR"
    assert gold.value == pytest.approx(18_295_400.0)  # rial -> toman
    assert gold.currency == "IRT" and gold.unit == "gram"
    # ts is Tehran local (+03:30): 14:15:39 -> 10:45:39 UTC
    assert gold.observed_at.tzinfo is not None
    assert gold.observed_at.astimezone(timezone.utc).hour == 10
    assert gold.observed_at.astimezone(timezone.utc).minute == 45

    usd = observations["USD_IRT"]
    assert usd.value == pytest.approx(106_530.0)

    ons = observations["XAUUSD"]
    assert ons.raw_currency == "USD"
    assert ons.value == pytest.approx(3349.61)  # USD passes through, no /10

    coin = observations["IR_COIN_EMAMI"]
    assert coin.value == pytest.approx(191_500_000.0)


def test_tgju_parse_history_fixture():
    payload = load_fixture_json("tgju_history_geram18.json")
    rows = tgju.parse_history(payload, "geram18")
    assert len(rows) == 3
    # sorted ascending by date
    assert [d.isoformat() for d, _ in rows] == ["2026-07-16", "2026-07-18", "2026-07-19"]
    assert rows[-1][1] == pytest.approx(182_954_000.0)  # raw rial close
    assert tgju.normalize_history_value("geram18", rows[-1][1]) == pytest.approx(18_295_400.0)
    assert tgju.normalize_history_value("ons", 3349.61) == pytest.approx(3349.61)


def test_tgju_strip_html_and_persian_digits():
    assert tgju.strip_html('<span class="low" dir="ltr">3707000</span>') == "3707000"
    assert tgju._to_float("۱۲۳۴") == 1234.0
    assert tgju._to_float("not a number") is None
    assert tgju._to_float("182,954,000") == 182_954_000.0


@respx.mock
def test_tgju_fetch_falls_back_to_next_host(settings):
    respx.get("https://call2.tgju.org/ajax.json").mock(
        return_value=httpx.Response(500)
    )
    respx.get("https://call3.tgju.org/ajax.json").mock(
        return_value=httpx.Response(200, json=load_fixture_json("tgju_live.json"))
    )
    provider = tgju.TGJUProvider(timeout=2.0, courtesy_delay=0.0, backoff_base=0.0)
    observations = provider.fetch()
    assert {o.symbol for o in observations} >= {"IR_GOLD_18K", "USD_IRT"}


@respx.mock
def test_tgju_auth_wall_fails_without_retry(settings):
    route2 = respx.get("https://call2.tgju.org/ajax.json").mock(
        return_value=httpx.Response(403)
    )
    respx.get("https://call3.tgju.org/ajax.json").mock(return_value=httpx.Response(403))
    respx.get("https://call4.tgju.org/ajax.json").mock(return_value=httpx.Response(403))
    provider = tgju.TGJUProvider(timeout=2.0, courtesy_delay=0.0, backoff_base=0.0)
    with pytest.raises(ProviderError):
        provider.fetch()
    assert route2.call_count == 1  # 403 is never retried (no bypassing)


# --- Yahoo ------------------------------------------------------------------


def test_yahoo_parse_gcf():
    obs = yahoo.parse_chart(load_fixture_json("yahoo_gcf.json"), "GC=F")
    assert obs is not None
    assert obs.symbol == "XAUUSD"
    assert obs.value == pytest.approx(3352.4)
    assert obs.currency == "USD" and obs.unit == "ozt"
    assert obs.observed_at.tzinfo is not None


def test_yahoo_tnx_scaling():
    obs = yahoo.parse_chart(load_fixture_json("yahoo_tnx.json"), "^TNX")
    assert obs is not None
    assert obs.symbol == "US10Y"
    assert obs.raw_value == pytest.approx(43.5)
    assert obs.value == pytest.approx(4.35)  # 10x quote handled
    assert obs.currency == "PCT"
    assert obs.raw_unit == "TNX_index" and obs.raw_currency == "INDEX"


def test_yahoo_tnx_direct_quote_is_not_divided():
    """Yahoo also serves ^TNX as the yield itself. Dividing that anyway is
    what stored five years of US10Y ten times too small; the raw row now says
    which convention was read so the next flip is visible in the table."""
    obs = yahoo.parse_chart(load_fixture_json("yahoo_tnx_direct.json"), "^TNX")
    assert obs is not None
    assert obs.raw_value == pytest.approx(4.697)
    assert obs.value == pytest.approx(4.697)
    assert obs.currency == "PCT"
    assert obs.raw_unit == "pct" and obs.raw_currency == "PCT"


def test_yahoo_tnx_history_uses_the_same_rule():
    """Backfill writes the normalized number into `prices` AND into
    raw_observations, so a wrong reading here poisons five years at once."""
    pairs = yahoo.parse_chart_history(load_fixture_json("yahoo_tnx_direct.json"), "^TNX")
    assert [round(v, 3) for _, v in pairs] == [4.697]
    index_pairs = yahoo.parse_chart_history(load_fixture_json("yahoo_tnx.json"), "^TNX")
    assert [round(v, 3) for _, v in index_pairs] == [4.35]


def _tnx_history_payload(closes):
    return {"chart": {"result": [{
        "meta": {"currency": "USD", "symbol": "^TNX"},
        "timestamp": [1600000000 + i * 86400 for i in range(len(closes))],
        "indicators": {"quote": [{"close": list(closes)}]},
    }]}}


def test_yahoo_tnx_history_settles_the_convention_from_the_whole_series():
    """The ambiguous band is where the per-bar rule silently loses.

    An index-form history is ONE download under ONE convention, so every bar
    must be read the same way. Judged bar by bar, the low-rate years survive
    the 25% ceiling under both readings and are stored unscaled — and
    ``SANITY_RANGES["US10Y"]`` is (0.0, 25.0), so 12.8 for a 1.28% yield is
    accepted downstream as an ordinary value. The series settles it: one bar
    above the ceiling proves the convention for all of them.

    Reachable through ``POST /internal/backfill/history`` — the job migration
    0022 recommends for recovering the days the freeze cost.
    """
    true_yields = [1.28, 1.51, 2.38, 3.48, 4.68]
    index_quotes = [round(y * 10, 2) for y in true_yields]  # 12.8 15.1 23.8 34.8 46.8
    pairs = yahoo.parse_chart_history(_tnx_history_payload(index_quotes), "^TNX")

    assert [round(v, 3) for _, v in pairs] == true_yields
    # the three that used to slip through did so because they looked plausible
    for quote in (12.8, 15.1, 23.8):
        assert validation.sanity_ok("US10Y", quote), "premise: the band is not caught downstream"


def test_yahoo_tnx_plain_yield_history_is_never_divided():
    """The other direction of the same rule, and the more dangerous one: the
    unconditional divide is what stored five years ten times too small."""
    true_yields = [1.28, 1.51, 2.38, 3.48, 4.68]
    pairs = yahoo.parse_chart_history(_tnx_history_payload(true_yields), "^TNX")
    assert [round(v, 3) for _, v in pairs] == true_yields


def test_yahoo_tnx_history_never_mixes_conventions_within_one_payload():
    """No payload may come back part index, part yield.

    Every returned bar has to be the same multiple of its quote — that is what
    "one download, one convention" means, and mixing was the actual defect.
    """
    for closes in ([12.8, 15.1, 23.8, 34.8, 46.8], [1.28, 1.51, 2.38, 3.48, 4.68]):
        pairs = yahoo.parse_chart_history(_tnx_history_payload(closes), "^TNX")
        ratios = {round(close / value, 6) for close, (_, value) in zip(closes, pairs)}
        assert len(ratios) == 1, f"mixed conventions in one payload: {ratios}"
        assert ratios <= {1.0, 10.0}


def test_yahoo_non_tnx_history_is_untouched_by_the_series_rule():
    """GC=F closes above 25 are ounces of gold, not a convention signal."""
    pairs = yahoo.parse_chart_history(load_fixture_json("yahoo_gcf.json"), "GC=F")
    assert pairs[0][1] == pytest.approx(3330.1)


def test_yahoo_parse_history():
    pairs = yahoo.parse_chart_history(load_fixture_json("yahoo_gcf.json"), "GC=F")
    assert len(pairs) == 2  # null close dropped
    assert pairs[0][1] == pytest.approx(3330.1)
    assert pairs[0][0].tzinfo is not None


def test_yahoo_parse_garbage():
    assert yahoo.parse_chart({"chart": {"result": []}}, "GC=F") is None
    assert yahoo.parse_chart(None, "GC=F") is None


# --- Stooq ------------------------------------------------------------------


def test_stooq_parse_quote_csv():
    observations = stooq.parse_quote_csv(load_fixture_text("stooq_quote.csv"))
    by_symbol = {o.symbol: o for o in observations}
    assert by_symbol["XAUUSD"].value == pytest.approx(3352.4)
    assert by_symbol["XAGUSD"].value == pytest.approx(38.21)
    assert by_symbol["XAUUSD"].observed_at.tzinfo is not None


def test_stooq_antibot_html_raises_not_bypasses(settings):
    with respx.mock:
        respx.get(host="stooq.com", path="/q/l/").mock(
            return_value=httpx.Response(200, text="<html>challenge</html>")
        )
        provider = stooq.StooqProvider(timeout=2.0, courtesy_delay=0.0, backoff_base=0.0)
        with pytest.raises(ProviderError):
            provider.fetch()


# --- Navasan ----------------------------------------------------------------


def test_navasan_parse_latest():
    observations = navasan.parse_latest(load_fixture_json("navasan_latest.json"))
    by_symbol = {o.symbol: o for o in observations}
    assert set(by_symbol) == {"USD_IRT", "IR_GOLD_18K"}
    # values are TOMAN already: no division
    assert by_symbol["USD_IRT"].value == pytest.approx(106_530.0)
    assert by_symbol["IR_GOLD_18K"].value == pytest.approx(18_295_400.0)
    assert by_symbol["IR_GOLD_18K"].raw_currency == "IRT"


def test_navasan_requires_key():
    with pytest.raises(ValueError):
        navasan.NavasanProvider(api_key="")


# --- Alanchand --------------------------------------------------------------


def test_alanchand_token_api_parse_rial_division():
    payload = [
        {"slug": "18ayar", "price": 182_954_000},
        {"slug": "sekkeh", "price": "1,915,000,000"},
        {"slug": "unknown", "price": 1},
    ]
    observations = alanchand.parse_api_payload(payload)
    by_symbol = {o.symbol: o for o in observations}
    assert set(by_symbol) == {"IR_GOLD_18K", "IR_COIN_EMAMI"}
    gold = by_symbol["IR_GOLD_18K"]
    assert gold.raw_value == 182_954_000.0
    assert gold.raw_currency == "IRR"
    assert gold.value == pytest.approx(18_295_400.0)  # rial -> toman
    assert gold.currency == "IRT" and gold.unit == "gram"
    # dict-wrapped shapes are tolerated too
    wrapped = alanchand.parse_api_payload({"currencies": [{"slug": "usd", "price": 1_065_300}]})
    assert wrapped[0].symbol == "USD_IRT"
    assert wrapped[0].value == pytest.approx(106_530.0)


@respx.mock
def test_alanchand_token_fetch_sends_bearer(settings):
    gold_route = respx.get(
        "https://api.alanchand.com", params={"type": "gold", "symbols": "18ayar,sekkeh"}
    ).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"slug": "18ayar", "price": 182_954_000},
                {"slug": "sekkeh", "price": 1_915_000_000},
            ],
        )
    )
    respx.get(
        "https://api.alanchand.com", params={"type": "currencies", "symbols": "usd"}
    ).mock(return_value=httpx.Response(200, json=[{"slug": "usd", "price": 1_065_300}]))
    provider = alanchand.AlanchandProvider(
        token="secret-token", timeout=2.0, courtesy_delay=0.0, backoff_base=0.0
    )
    by_symbol = {o.symbol: o for o in provider.fetch()}
    assert set(by_symbol) == {"IR_GOLD_18K", "IR_COIN_EMAMI", "USD_IRT"}
    assert by_symbol["IR_GOLD_18K"].value == pytest.approx(18_295_400.0)
    assert by_symbol["IR_COIN_EMAMI"].value == pytest.approx(191_500_000.0)
    assert (
        gold_route.calls.last.request.headers["Authorization"] == "Bearer secret-token"
    )


@respx.mock
def test_alanchand_bad_token_fails_fast_no_retry(settings):
    route = respx.get("https://api.alanchand.com").mock(
        return_value=httpx.Response(401)
    )
    provider = alanchand.AlanchandProvider(
        token="bad", timeout=2.0, courtesy_delay=0.0, backoff_base=0.0
    )
    with pytest.raises(ProviderError):
        provider.fetch()
    assert route.call_count == 2  # one attempt per query type, 401 never retried


def test_alanchand_parse_gold_page_fixture():
    obs = alanchand.parse_gold_page(load_fixture_text("alanchand_18ayar.html"))
    assert obs is not None
    assert obs.symbol == "IR_GOLD_18K"
    assert obs.raw_value == 181_679_700.0  # page quotes RIAL
    assert obs.raw_currency == "IRR"
    assert obs.raw_unit == "IRR/gram (html)"  # auditable source method
    assert obs.value == pytest.approx(18_167_970.0)  # rial -> toman
    assert obs.observed_at.tzinfo is not None


def test_alanchand_parse_gold_page_skips_real_price():
    # "Real Price" (theoretical) before any current quote must NOT be used
    html = "<h1>18K Gold per Gram</h1><p>Real Price 182,350,000 IRR</p>"
    assert alanchand.parse_gold_page(html) is None
    assert alanchand.parse_gold_page("<html>challenge</html>") is None


@respx.mock
def test_alanchand_keyless_html_mode(settings):
    respx.get("https://alanchand.com/en/gold-price/18ayar").mock(
        return_value=httpx.Response(200, text=load_fixture_text("alanchand_18ayar.html"))
    )
    provider = alanchand.AlanchandProvider(
        timeout=2.0, courtesy_delay=0.0, backoff_base=0.0  # no token -> HTML mode
    )
    (obs,) = provider.fetch()
    assert obs.symbol == "IR_GOLD_18K"
    assert obs.value == pytest.approx(18_167_970.0)


def test_alanchand_legacy_parse_html_fixture():
    observations = alanchand.parse_html(load_fixture_text("alanchand_page.html"))
    by_symbol = {o.symbol: o for o in observations}
    assert by_symbol["IR_GOLD_18K"].value == pytest.approx(18_295_400.0)
    assert by_symbol["USD_IRT"].value == pytest.approx(106_530.0)
    assert by_symbol["IR_COIN_EMAMI"].value == pytest.approx(191_500_000.0)


def test_alanchand_legacy_parse_json_payload():
    payload = {
        "gold": [{"slug": "18ayar", "price": 18295400}],
        "currency": [{"slug": "usd", "price": "106,530"}],
    }
    observations = alanchand.parse_json_payload(payload)
    assert {o.symbol for o in observations} == {"IR_GOLD_18K", "USD_IRT"}


# --- Milli Gold (milli.gold) --------------------------------------------------


def test_milligold_parse_home_fixture_returns_current_not_day_high():
    """Real page slice (2026-07-21): the day high 184,911,000 renders BEFORE
    the current price in text order — the parser must return the CURRENT
    price from the text-deepOcean-focus element (production bug regression)."""
    obs = milligold.parse_home(load_fixture_text("milligold_home.html"))
    assert obs is not None
    assert obs.symbol == "IR_GOLD_18K"
    assert obs.raw_value == 183_830_000.0  # current, NOT the 184,911,000 high
    assert obs.raw_currency == "IRR"
    assert obs.raw_unit == "IRR/gram (html)"
    assert obs.value == pytest.approx(18_383_000.0)  # rial -> toman
    assert obs.observed_at.tzinfo is not None


def test_milligold_fallback_skips_high_low_when_class_anchor_gone():
    # No deepOcean-focus class anywhere: the text fallback must skip amounts
    # labeled as change %, day high (بالاترین, AFTER the number per RTL DOM
    # order) and day low, and return the unlabeled current amount.
    html = (
        "<div>1,73 % تغییرات</div>"
        "<div>184,911,000ریال بالاترین قیمت</div>"
        "<div>181,500,000ریال پایین‌ترین قیمت</div>"
        "<div>183,830,000ریال</div>"
        "<div>قیمت ۱ گرم طلای ۱۸ عیار</div>"
    )
    obs = milligold.parse_home(html)
    assert obs is not None
    assert obs.raw_value == 183_830_000.0


def test_milligold_parse_persian_digits_and_zero_width():
    # Persian/Arabic-Indic digits, Persian thousands separator and a
    # zero-width non-joiner inside the label must all be handled, via the
    # class-anchored path
    html = (
        "<div>قیمت ۱ گرم طلای‌ ۱۸ عیار</div>"
        '<p class="font-bold text-deepOcean-focus">۱۸۲٬۰۵۰٬۰۰۰ ریال</p>'
    )
    obs = milligold.parse_home(html)
    assert obs is not None
    assert obs.raw_value == 182_050_000.0
    assert obs.value == pytest.approx(18_205_000.0)


def test_milligold_parse_garbage():
    assert milligold.parse_home("<html>challenge page</html>") is None
    assert milligold.parse_home("قیمت ۱ گرم طلای ۱۸ عیار بدون قیمت") is None


@respx.mock
def test_milligold_fetch_round_trip(settings):
    respx.get("https://milli.gold/").mock(
        return_value=httpx.Response(200, text=load_fixture_text("milligold_home.html"))
    )
    provider = milligold.MilligoldProvider(
        timeout=2.0, courtesy_delay=0.0, backoff_base=0.0
    )
    (obs,) = provider.fetch()
    assert obs.symbol == "IR_GOLD_18K"
    assert obs.value == pytest.approx(18_383_000.0)


@respx.mock
def test_milligold_layout_change_raises_not_bypasses(settings):
    respx.get("https://milli.gold/").mock(
        return_value=httpx.Response(200, text="<html>js challenge</html>")
    )
    provider = milligold.MilligoldProvider(
        timeout=2.0, courtesy_delay=0.0, backoff_base=0.0
    )
    with pytest.raises(ProviderError):
        provider.fetch()


# --- pricedb (margani/pricedb GitHub dataset) --------------------------------


def test_pricedb_parse_latest_fixtures():
    gold = pricedb.parse_latest(
        load_fixture_json("pricedb_geram18_latest.json"), "geram18"
    )
    assert gold is not None
    assert gold.symbol == "IR_GOLD_18K"
    assert gold.raw_value == 186_994_000.0
    assert gold.raw_currency == "IRR"
    assert gold.value == pytest.approx(18_699_400.0)  # rial -> toman
    assert gold.currency == "IRT" and gold.unit == "gram"
    assert gold.observed_at.tzinfo is not None

    usd = pricedb.parse_latest(
        load_fixture_json("pricedb_price_dollar_rl_latest.json"), "price_dollar_rl"
    )
    assert usd is not None
    assert usd.symbol == "USD_IRT"
    assert usd.value == pytest.approx(165_890.0)  # 1,658,900 rial -> toman

    coin = pricedb.parse_latest(
        load_fixture_json("pricedb_sekee_latest.json"), "sekee"
    )
    assert coin is not None
    assert coin.symbol == "IR_COIN_EMAMI"
    assert coin.value == pytest.approx(190_010_000.0)


def test_pricedb_parse_history_fixture():
    rows = pricedb.parse_history(
        load_fixture_json("pricedb_geram18_history.json"), "geram18"
    )
    assert len(rows) == 6
    days = [d for d, _ in rows]
    assert days == sorted(days)  # ascending
    assert days[0].isoformat() == "2023-12-02"
    assert rows[-1][1] == pytest.approx(186_994_000.0)  # raw rial close
    assert pricedb.normalize_history_value("geram18", rows[-1][1]) == pytest.approx(
        18_699_400.0
    )


def test_pricedb_parse_garbage():
    assert pricedb.parse_latest(None, "geram18") is None
    assert pricedb.parse_latest({"p": "n/a"}, "geram18") is None
    assert pricedb.parse_latest({"p": "186,994,000"}, "unknown_slug") is None
    assert pricedb.parse_history({"not": "a list"}, "geram18") == []


@respx.mock
def test_pricedb_fetch_partial_slugs(settings):
    base = "https://raw.githubusercontent.com/margani/pricedb/main/tgju/current"
    respx.get(f"{base}/geram18/latest.json").mock(
        return_value=httpx.Response(
            200, json=load_fixture_json("pricedb_geram18_latest.json")
        )
    )
    respx.get(f"{base}/sekee/latest.json").mock(return_value=httpx.Response(404))
    respx.get(f"{base}/price_dollar_rl/latest.json").mock(
        return_value=httpx.Response(
            200, json=load_fixture_json("pricedb_price_dollar_rl_latest.json")
        )
    )
    provider = pricedb.PriceDBProvider(timeout=2.0, courtesy_delay=0.0, backoff_base=0.0)
    observations = provider.fetch()
    assert {o.symbol for o in observations} == {"IR_GOLD_18K", "USD_IRT"}


# --- gold-api.com ------------------------------------------------------------


def test_gold_api_parse_xau():
    obs = gold_api.parse_price(load_fixture_json("gold_api_xau.json"), "XAU")
    assert obs is not None
    assert obs.symbol == "XAUUSD"
    assert obs.value == pytest.approx(4010.100098)
    assert obs.currency == "USD" and obs.unit == "ozt"
    # updatedAt "2026-07-20T12:58:29Z" parsed as aware UTC
    assert obs.observed_at.tzinfo is not None
    assert obs.observed_at.astimezone(timezone.utc).hour == 12
    assert obs.observed_at.astimezone(timezone.utc).minute == 58


def test_gold_api_parse_xag():
    obs = gold_api.parse_price(load_fixture_json("gold_api_xag.json"), "XAG")
    assert obs is not None
    assert obs.symbol == "XAGUSD"
    assert obs.value == pytest.approx(56.911999)


def test_gold_api_parse_garbage():
    assert gold_api.parse_price(None, "XAU") is None
    assert gold_api.parse_price({"price": -1}, "XAU") is None
    assert gold_api.parse_price({"price": 4010.1}, "XPT") is None  # unmapped


@respx.mock
def test_gold_api_fetch_survives_one_symbol_failing(settings):
    respx.get("https://api.gold-api.com/price/XAU").mock(
        return_value=httpx.Response(200, json=load_fixture_json("gold_api_xau.json"))
    )
    respx.get("https://api.gold-api.com/price/XAG").mock(
        return_value=httpx.Response(500)
    )
    provider = gold_api.GoldAPIProvider(timeout=2.0, courtesy_delay=0.0, backoff_base=0.0)
    observations = provider.fetch()
    assert {o.symbol for o in observations} == {"XAUUSD"}


# --- BrsApi.ir ---------------------------------------------------------------


def test_brsapi_parse_fixture_toman_no_division():
    observations = brsapi.parse_gold_currency(
        load_fixture_json("brsapi_gold_currency.json")
    )
    by_symbol = {o.symbol: o for o in observations}
    assert set(by_symbol) == {"IR_GOLD_18K", "IR_COIN_EMAMI", "USD_IRT", "XAUUSD"}

    gold = by_symbol["IR_GOLD_18K"]
    # unit is 'تومان' (TOMAN): already IRT scale, NO /10
    assert gold.raw_value == 6_214_700.0
    assert gold.value == pytest.approx(6_214_700.0)
    assert gold.raw_currency == "IRT" and gold.currency == "IRT"
    assert gold.unit == "gram"
    # observed_at from time_unix (epoch seconds, UTC)
    assert gold.observed_at == datetime.fromtimestamp(1747573140, tz=timezone.utc)

    assert by_symbol["USD_IRT"].value == pytest.approx(81_650.0)
    assert by_symbol["IR_COIN_EMAMI"].value == pytest.approx(69_805_000.0)

    ons = by_symbol["XAUUSD"]
    assert ons.value == pytest.approx(3201.0)
    assert ons.raw_currency == "USD" and ons.currency == "USD"


def test_brsapi_defensive_rial_unit_divides():
    payload = {
        "gold": [
            {
                "time_unix": 1747573140,
                "symbol": "IR_GOLD_18K",
                "price": 62_147_000,
                "unit": "ریال",  # never observed live; defensive /10
            }
        ]
    }
    (obs,) = brsapi.parse_gold_currency(payload)
    assert obs.raw_currency == "IRR"
    assert obs.value == pytest.approx(6_214_700.0)
    assert obs.currency == "IRT"


def test_brsapi_requires_key():
    with pytest.raises(ValueError):
        brsapi.BrsApiProvider(api_key="")


def test_registry_builds_new_providers(settings):
    assert registry.build_provider("pricedb", settings) is not None
    assert registry.build_provider("gold_api", settings) is not None
    # brsapi is keyed: disabled without BRSAPI_KEY, enabled with it
    settings.brsapi_api_key = ""
    assert registry.build_provider("brsapi", settings) is None
    settings.brsapi_api_key = "test-key"
    provider = registry.build_provider("brsapi", settings)
    assert isinstance(provider, brsapi.BrsApiProvider)


def test_registry_builds_html_providers(settings):
    assert isinstance(
        registry.build_provider("milligold", settings), milligold.MilligoldProvider
    )
    # alanchand is always built: HTML mode without a token, API mode with one
    settings.alanchand_token = ""
    keyless = registry.build_provider("alanchand", settings)
    assert isinstance(keyless, alanchand.AlanchandProvider)
    assert keyless.token == ""
    settings.alanchand_token = "tok"
    keyed = registry.build_provider("alanchand", settings)
    assert isinstance(keyed, alanchand.AlanchandProvider)
    assert keyed.token == "tok"


# --- metals.dev -------------------------------------------------------------


def test_metals_dev_parse():
    payload = {
        "status": "success",
        "metals": {"gold": 3350.2, "silver": 38.1},
        "timestamps": {"metal": "2026-07-20T10:00:00.354Z"},
    }
    observations = metals_dev.parse_latest(payload)
    by_symbol = {o.symbol: o for o in observations}
    assert by_symbol["XAUUSD"].value == pytest.approx(3350.2)
    assert by_symbol["XAGUSD"].value == pytest.approx(38.1)
    assert by_symbol["XAUUSD"].observed_at.tzinfo is not None


# --- hamrahgold (Addendum 10: 24/7 primary 18k source) ------------------------

def test_hamrahgold_midpoint_and_normalization(monkeypatch):
    from app.providers.hamrahgold import HamrahGoldProvider

    sell = load_fixture_json("hamrahgold_sell.json")
    buy = load_fixture_json("hamrahgold_buy.json")
    provider = HamrahGoldProvider(courtesy_delay=0.0, backoff_base=0.0)
    monkeypatch.setattr(
        provider, "_get_json",
        lambda url, params: sell if params["type"] == "sell" else buy,
    )
    obs = provider.fetch()
    assert len(obs) == 1
    o = obs[0]
    assert o.symbol == "IR_GOLD_18K"
    assert o.raw_currency == "IRR" and o.currency == "IRT"
    # midpoint of 188,370,000 / 187,450,000 rial -> 18,791,000 toman
    assert o.value == pytest.approx(18_791_000.0)
    assert o.raw_payload["spread_pct"] == pytest.approx(0.4896, abs=1e-3)


def test_hamrahgold_single_side_still_quotes(monkeypatch):
    from app.providers.base import ProviderError
    from app.providers.hamrahgold import HamrahGoldProvider

    sell = load_fixture_json("hamrahgold_sell.json")

    def get(url, params):
        if params["type"] == "sell":
            return sell
        raise ProviderError("buy side down")

    provider = HamrahGoldProvider(courtesy_delay=0.0, backoff_base=0.0)
    monkeypatch.setattr(provider, "_get_json", get)
    obs = provider.fetch()
    assert obs[0].value == pytest.approx(18_837_000.0)
    assert obs[0].raw_payload["sides"] == 1


def test_hamrahgold_total_failure_raises(monkeypatch):
    from app.providers.base import ProviderError
    from app.providers.hamrahgold import HamrahGoldProvider

    provider = HamrahGoldProvider(courtesy_delay=0.0, backoff_base=0.0)
    monkeypatch.setattr(
        provider, "_get_json",
        lambda url, params: (_ for _ in ()).throw(ProviderError("down")),
    )
    with pytest.raises(ProviderError):
        provider.fetch()


# --- bitmax (Addendum 11: 24/7 USDT/toman as the USD proxy) -------------------

def test_bitmax_parses_usdt_toman(monkeypatch):
    from app.providers.bitmax import BitmaxProvider

    payload = load_fixture_json("bitmax_watcher.json")
    provider = BitmaxProvider(courtesy_delay=0.0, backoff_base=0.0)
    monkeypatch.setattr(provider, "_get_json", lambda url, params=None: payload)
    obs = provider.fetch()
    assert len(obs) == 1
    o = obs[0]
    assert o.symbol == "USD_IRT"
    assert o.value == pytest.approx(192_676.0)  # already toman
    assert o.currency == "IRT" and o.unit == "usd"
    assert o.raw_payload["instrument"] == "USDT"
    assert o.raw_payload["change"] == pytest.approx(0.01052256)


def test_bitmax_rejects_malformed(monkeypatch):
    from app.providers.base import ProviderError
    from app.providers.bitmax import BitmaxProvider

    provider = BitmaxProvider(courtesy_delay=0.0, backoff_base=0.0)
    monkeypatch.setattr(provider, "_get_json", lambda url, params=None: {"message": {}})
    with pytest.raises(ProviderError):
        provider.fetch()


# --- cross-provider dispersion in a collect cycle -----------------------------


def _seed_provider(engine, code, priority, category="iran_gold"):
    with engine.begin() as conn:
        conn.execute(
            insert(data_providers).values(
                code=code, name=code.title(), base_url="https://example.invalid",
                category=category, priority=priority, enabled=True,
                consecutive_failures=0,
            )
        )


class CountingStub(Provider):
    """Serves canned observations and counts how often it was fetched."""

    def __init__(self, observations, max_attempts=3):
        super().__init__(timeout=1.0, courtesy_delay=0.0, backoff_base=0.0)
        self._observations = observations
        self.max_attempts = max_attempts  # 1 => quota-billed provider
        self.fetch_count = 0

    def fetch(self):
        self.fetch_count += 1
        return list(self._observations)


def _iran_obs(code, symbol, toman, observed_at, unit="gram", payload=None):
    """An Iranian quote as providers emit it: rial raw value, toman normalized."""
    return Observation(
        provider_code=code, symbol=symbol, raw_value=toman * 10,
        raw_unit=f"IRR/{unit}", raw_currency="IRR", value=toman,
        currency="IRT", unit=unit, observed_at=observed_at, raw_payload=payload,
    )


def _patch_registry(monkeypatch, stubs):
    from app.providers import registry as registry_mod

    monkeypatch.setattr(
        registry_mod, "build_provider", lambda code, settings: stubs.get(code)
    )


def _raw_rows(engine, symbol):
    with engine.connect() as conn:
        rows = conn.execute(
            select(raw_observations).where(raw_observations.c.symbol == symbol)
        ).all()
    return {r._mapping["provider_code"]: r._mapping for r in rows}


def test_collect_records_peer_quotes_and_dispersion(engine, settings, monkeypatch):
    """Two providers quoting 18k in one cycle: the priority-1 value is still the
    one served, the peer quote is no longer discarded, and their disagreement is
    summarised on the winner's raw row."""
    _seed_provider(engine, "primary", priority=1)
    _seed_provider(engine, "secondary", priority=5)
    now = utcnow()
    stubs = {
        "primary": CountingStub(
            [_iran_obs("primary", "IR_GOLD_18K", 18_300_000.0,
                       now - timedelta(minutes=1), payload={"spread_pct": 0.49})]
        ),
        # consulted only because the coin is still missing — its 18k quote used
        # to be dropped on the floor by the "symbol already satisfied" check
        "secondary": CountingStub(
            [
                _iran_obs("secondary", "IR_GOLD_18K", 18_420_000.0,
                          now - timedelta(minutes=2)),
                _iran_obs("secondary", "IR_COIN_EMAMI", 191_500_000.0,
                          now - timedelta(minutes=2), unit="coin"),
            ]
        ),
    }
    _patch_registry(monkeypatch, stubs)

    result = run_collect(engine, settings, ["iran_gold"])
    assert result["collected"].get("IR_GOLD_18K") == 1

    # (a) the priority-1 value is the only 18k row in prices
    with engine.connect() as conn:
        price_rows = conn.execute(
            select(prices).where(prices.c.symbol == "IR_GOLD_18K")
        ).all()
    assert len(price_rows) == 1
    assert price_rows[0]._mapping["source"] == "primary"
    assert float(price_rows[0]._mapping["value"]) == 18_300_000.0

    # (b) both observations are on record
    raws = _raw_rows(engine, "IR_GOLD_18K")
    assert set(raws) == {"primary", "secondary"}
    assert raws["secondary"]["quality"] == "ok"
    assert float(raws["secondary"]["raw_value"]) == 184_200_000.0  # rial kept

    # (c) dispersion recorded on the winner, provider payload preserved
    payload = raws["primary"]["raw_payload"]
    assert payload["spread_pct"] == 0.49
    dispersion = payload[PEER_DISPERSION_KEY]
    assert dispersion["n_sources"] == 2
    assert dispersion["n_agreeing"] == 2
    # normalized toman values, since the peer's own row keeps only rials
    assert dispersion["values"] == {
        "primary": 18_300_000.0, "secondary": 18_420_000.0
    }
    assert dispersion["spread_pct"] == pytest.approx(0.6536, abs=1e-3)
    assert dispersion["mad"] == pytest.approx(60_000.0)
    # only the canonical row carries the summary
    assert (raws["secondary"]["raw_payload"] or {}).get(PEER_DISPERSION_KEY) is None


def test_collect_single_source_dispersion_is_null(engine, settings, monkeypatch):
    """One provider covering the whole job: dispersion is unknown, not zero."""
    _seed_provider(engine, "primary", priority=1)
    now = utcnow()
    stubs = {
        "primary": CountingStub(
            [
                _iran_obs("primary", "IR_GOLD_18K", 18_300_000.0,
                          now - timedelta(minutes=1)),
                _iran_obs("primary", "IR_COIN_EMAMI", 191_500_000.0,
                          now - timedelta(minutes=1), unit="coin"),
            ]
        )
    }
    _patch_registry(monkeypatch, stubs)

    run_collect(engine, settings, ["iran_gold"])

    raws = _raw_rows(engine, "IR_GOLD_18K")
    assert set(raws) == {"primary"}
    assert (raws["primary"]["raw_payload"] or {}).get(PEER_DISPERSION_KEY) is None


def test_collect_dispersion_never_refetches_quota_provider(engine, settings, monkeypatch):
    """A quota-billed provider (max_attempts == 1, e.g. tse_funds/brsapi) is
    fetched exactly once per cycle: its dispersion contribution comes from the
    response already in the fetch cache, never from an extra request."""
    _seed_provider(engine, "primary", priority=1)
    _seed_provider(engine, "quota", priority=5)
    now = utcnow()
    stubs = {
        "primary": CountingStub(
            [_iran_obs("primary", "IR_GOLD_18K", 18_300_000.0,
                       now - timedelta(minutes=1))]
        ),
        "quota": CountingStub(
            [
                _iran_obs("quota", "IR_GOLD_18K", 18_420_000.0,
                          now - timedelta(minutes=2)),
                _iran_obs("quota", "IR_COIN_EMAMI", 191_500_000.0,
                          now - timedelta(minutes=2), unit="coin"),
            ],
            max_attempts=1,
        ),
    }
    _patch_registry(monkeypatch, stubs)

    run_collect(engine, settings, ["iran_gold"])

    assert stubs["quota"].fetch_count == 1
    assert stubs["primary"].fetch_count == 1
    dispersion = _raw_rows(engine, "IR_GOLD_18K")["primary"]["raw_payload"][
        PEER_DISPERSION_KEY
    ]
    assert dispersion["n_sources"] == 2  # measured from the cached response


def test_dispersion_summary_needs_two_sources():
    assert validation.dispersion_summary({"primary": 18_300_000.0}) is None
    summary = validation.dispersion_summary(
        {"primary": 18_300_000.0, "secondary": 18_420_000.0}
    )
    assert summary["n_sources"] == 2 and summary["n_agreeing"] == 2
    assert summary["median"] == pytest.approx(18_360_000.0)
    assert summary["spread_abs"] == pytest.approx(120_000.0)
    assert summary["spread_pct"] == pytest.approx(0.6536, abs=1e-3)
    assert summary["mad_pct"] == pytest.approx(0.3268, abs=1e-3)
    # a source beyond the confirmation tolerance stops counting as agreeing
    wide = validation.dispersion_summary(
        {"a": 18_300_000.0, "b": 18_420_000.0, "c": 25_000_000.0}
    )
    assert wide["n_sources"] == 3 and wide["n_agreeing"] == 2
    # percent-of-median is meaningless for a series that oscillates around zero
    oscillating = validation.dispersion_summary({"a": 1.0, "b": -1.0})
    assert oscillating["spread_pct"] is None and oscillating["mad_pct"] is None
    assert oscillating["mad"] == pytest.approx(1.0)


# --- Addendum 19: provider circuit breaker -----------------------------------

def test_breaker_stays_closed_below_threshold():
    from app.providers.registry import BREAKER_THRESHOLD, breaker_cooldown_minutes
    for fails in range(BREAKER_THRESHOLD):
        assert breaker_cooldown_minutes(fails) == 0.0


def test_breaker_cooldown_backs_off_and_caps():
    from app.providers.registry import (BREAKER_MAX_MINUTES, BREAKER_THRESHOLD,
                                        breaker_cooldown_minutes)
    first = breaker_cooldown_minutes(BREAKER_THRESHOLD)
    second = breaker_cooldown_minutes(BREAKER_THRESHOLD + 1)
    assert 0 < first < second
    # tgju reached 1945 consecutive failures; the cooldown must not run away.
    assert breaker_cooldown_minutes(1945) == BREAKER_MAX_MINUTES


def test_breaker_opens_during_cooldown_and_closes_after():
    from datetime import timedelta

    from app.db import utcnow
    from app.providers.registry import BREAKER_THRESHOLD, breaker_open
    now = utcnow()
    row = {"code": "tgju", "consecutive_failures": BREAKER_THRESHOLD + 3,
           "last_error_at": now - timedelta(minutes=1)}
    assert breaker_open(row, now) is True
    # Once the cooldown elapses the provider is retried: the breaker delays,
    # it never disables permanently.
    row["last_error_at"] = now - timedelta(hours=3)
    assert breaker_open(row, now) is False


def test_breaker_never_blocks_a_provider_with_no_recorded_error():
    from app.providers.registry import breaker_open
    assert breaker_open({"code": "x", "consecutive_failures": 999}) is False


def test_load_provider_rows_drops_broken_providers(engine):
    """A blocked provider must not spend the collect budget the healthy
    fallbacks behind it still need."""
    from datetime import timedelta

    from app.db import data_providers, utcnow
    from app.providers.registry import load_provider_rows
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(data_providers.insert().values(
            code="broken", name="Broken", category="iran_gold", priority=1,
            enabled=True, consecutive_failures=50, last_error_at=now - timedelta(seconds=30),
            last_error="access denied"))
        conn.execute(data_providers.insert().values(
            code="healthy", name="Healthy", category="iran_gold", priority=5,
            enabled=True, consecutive_failures=0))
    codes = [r["code"] for r in load_provider_rows(engine, ["iran_gold"])]
    assert "broken" not in codes
    assert "healthy" in codes


# --- a single-source symbol must be able to escape "suspect" ------------------
#
# US10Y has exactly one provider, so the "confirmed by a second source" rule
# had no reachable exit: Yahoo's ^TNX convention change made every quote a
# ~896% jump, every jump was held, and the series stopped for two days without
# a single alert. These cover both halves of the fix — the level does get
# accepted once it is sustained, and a spike still does not.


def _tnx_obs(code, pct_value, observed_at):
    """A ^TNX-shaped quote: the provider's index number and its percent value."""
    return Observation(
        provider_code=code, symbol="US10Y", raw_value=pct_value * 10.0,
        raw_unit="TNX_index", raw_currency="INDEX", value=pct_value,
        currency="PCT", unit="pct", observed_at=observed_at,
    )


# Deliberately not a flat line. A constant series has zero MAD, so the robust
# outlier test fires on ANY move and every quote below would come back suspect
# for a reason that has nothing to do with what these tests are about. The
# last entry is 1.0 so the newest row — the "last good" a jump is measured
# against — is exactly `level`.
_US10Y_DRIFT = (1.0, 1.008, 0.994, 1.003, 0.997, 1.006, 0.992, 1.004, 0.998, 1.0)


def _seed_us10y_prices(engine, level, now, days=10, newest_offset=None):
    """A settled daily series around `level`, oldest first."""
    rows = [
        (timedelta(days=d), level * _US10Y_DRIFT[i % len(_US10Y_DRIFT)])
        for i, d in enumerate(range(days, 0, -1))
    ]
    if newest_offset is not None:
        rows.append((newest_offset, level))
    with engine.begin() as conn:
        conn.execute(insert(prices), [
            dict(symbol="US10Y", value=value, currency="PCT", unit="pct",
                 source="yahoo", observed_at=now - off, collected_at=now - off,
                 quality="ok")
            for off, value in rows
        ])


def _seed_suspect_run(engine, provider, level, now, count, spacing_minutes):
    """`count` already-stored suspects at `level`, oldest last."""
    with engine.begin() as conn:
        conn.execute(insert(raw_observations), [
            dict(provider_code=provider, symbol="US10Y", raw_value=level * 10.0,
                 unit="TNX_index", currency="INDEX",
                 observed_at=now - timedelta(minutes=spacing_minutes * (i + 1)),
                 collected_at=now, quality="suspect", dedupe_key=f"held-{i}")
            for i in range(count)
        ])


def _issue_capture(engine):
    """Attach the app_issues logging bridge to THIS engine for one test.

    Constructed directly rather than via ``install_issue_capture``: that
    helper is idempotent per root logger, so once any earlier test has built a
    FastAPI app it hands back the handler bound to that app's engine and this
    test would silently observe the wrong database.
    """
    import contextlib
    import logging

    from app.core.issues import DBIssueHandler

    @contextlib.contextmanager
    def _ctx():
        root = logging.getLogger()
        handler = DBIssueHandler(engine)
        root.addHandler(handler)
        try:
            yield
        finally:
            root.removeHandler(handler)

    return _ctx()


def _collect_issues(engine):
    from app.db import app_issues

    with engine.connect() as conn:
        return [r._mapping for r in conn.execute(
            select(app_issues).where(app_issues.c.source == "app.jobs.collect"))]


def test_sustained_level_from_the_only_source_is_finally_accepted(
    engine, settings, monkeypatch
):
    """Yahoo's ^TNX flip, replayed: the new level has held across four earlier
    observations, the fifth completes the run, and the series moves again."""
    _seed_provider(engine, "yahoo", priority=10, category="global_gold")
    now = utcnow()
    _seed_us10y_prices(engine, 0.4697, now)
    _seed_suspect_run(engine, "yahoo", 4.68, now, count=4, spacing_minutes=30)
    _patch_registry(monkeypatch, {
        "yahoo": CountingStub([_tnx_obs("yahoo", 4.682, now)])
    })

    with _issue_capture(engine):
        result = run_collect(engine, settings, ["macro"])

    with engine.connect() as conn:
        latest = conn.execute(
            select(prices).where(prices.c.symbol == "US10Y")
            .order_by(prices.c.observed_at.desc()).limit(1)
        ).one()._mapping
    assert float(latest["value"]) == pytest.approx(4.682)
    assert result["collected"].get("US10Y") == 1
    # and the re-level is on the record, not a silent decision
    messages = [i["message"] for i in _collect_issues(engine)]
    assert any("accepting" in m and "US10Y" in m for m in messages), messages


def test_a_one_off_spike_is_still_held(engine, settings, monkeypatch):
    """The escape hatch must not become a way in: a single outlying quote has
    a run of one and stays out of `prices`."""
    _seed_provider(engine, "yahoo", priority=10, category="global_gold")
    now = utcnow()
    _seed_us10y_prices(engine, 4.60, now)
    _patch_registry(monkeypatch, {
        "yahoo": CountingStub([_tnx_obs("yahoo", 9.9, now)])
    })

    run_collect(engine, settings, ["macro"])

    with engine.connect() as conn:
        values = [float(v) for (v,) in conn.execute(
            select(prices.c.value).where(prices.c.symbol == "US10Y"))]
        held = conn.execute(
            select(raw_observations).where(raw_observations.c.symbol == "US10Y")
        ).all()
    assert 9.9 not in values
    assert [r._mapping["quality"] for r in held] == ["suspect"]


def test_repetition_cannot_outvote_a_source_still_delivering(
    engine, settings, monkeypatch
):
    """A long consistent run is not enough on its own: while some source is
    still producing accepted values the series is not frozen, and the
    disagreeing primary keeps waiting for a real confirmation."""
    _seed_provider(engine, "primary", priority=1, category="global_gold")
    _seed_provider(engine, "fallback", priority=5, category="global_gold")
    now = utcnow()
    # the fallback wrote a price five minutes ago — well inside the run
    _seed_us10y_prices(engine, 0.4697, now, newest_offset=timedelta(minutes=5))
    _seed_suspect_run(engine, "primary", 4.68, now, count=6, spacing_minutes=30)
    _patch_registry(monkeypatch, {
        "primary": CountingStub([_tnx_obs("primary", 4.682, now)]),
        "fallback": CountingStub([_tnx_obs("fallback", 0.4699, now)]),
    })

    run_collect(engine, settings, ["macro"])

    with engine.connect() as conn:
        values = [float(v) for (v,) in conn.execute(
            select(prices.c.value).where(prices.c.symbol == "US10Y"))]
    assert 4.682 not in values
    assert pytest.approx(0.4699) in values


def test_a_frozen_series_warns_once_per_cooldown(engine, settings, monkeypatch):
    """The original failure was silence. A held series must reach app_issues —
    and must not then flood it every ten minutes."""
    from app.jobs.collect import FREEZE_ALERT_KEY

    _seed_provider(engine, "yahoo", priority=10, category="global_gold")
    now = utcnow()
    _seed_us10y_prices(engine, 0.4697, now)
    # three earlier suspects, but only 30 minutes of them: past the alert
    # threshold, short of the span acceptance needs
    _seed_suspect_run(engine, "yahoo", 4.68, now, count=3, spacing_minutes=10)
    stub = CountingStub([_tnx_obs("yahoo", 4.682, now)])
    _patch_registry(monkeypatch, {"yahoo": stub})

    with _issue_capture(engine):
        run_collect(engine, settings, ["macro"])
        first = _collect_issues(engine)
        run_collect(engine, settings, ["macro"])  # same quote, next cycle
        second = _collect_issues(engine)

    frozen = [i for i in first if "frozen" in i["message"]]
    assert len(frozen) == 1, [i["message"] for i in first]
    assert frozen[0]["level"] == "warning"
    assert "US10Y" in frozen[0]["message"] and "yahoo" in frozen[0]["message"]
    assert len([i for i in second if "frozen" in i["message"]]) == 1  # throttled
    # nothing was promoted: 30 minutes is not a sustained level
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(prices)
            .where(prices.c.symbol == "US10Y", prices.c.value > 1.0)
        ).scalar() == 0
        marker = conn.execute(select(app_settings.c.value).where(
            app_settings.c.key == FREEZE_ALERT_KEY)).scalar()
    assert "US10Y" in marker


def test_repaired_history_lets_the_real_parser_flow_again(engine, settings, monkeypatch):
    """The two halves of the US10Y fix, composed. The real parser reads an
    index quote as a percent, and against a history migration 0022 has put
    back on the right scale that is an ordinary move rather than a 900% jump —
    so the series resumes on its own, with no suspect and no intervention."""
    import dataclasses

    _seed_provider(engine, "yahoo", priority=10, category="global_gold")
    now = utcnow()
    _seed_us10y_prices(engine, 4.30, now)  # the post-0022 scale
    parsed = yahoo.parse_chart(load_fixture_json("yahoo_tnx.json"), "^TNX")
    _patch_registry(monkeypatch, {
        "yahoo": CountingStub([dataclasses.replace(parsed, observed_at=now)])
    })

    result = run_collect(engine, settings, ["macro"])

    assert not [e for e in result["errors"] if "US10Y" in e], result["errors"]
    with engine.connect() as conn:
        latest = conn.execute(
            select(prices).where(prices.c.symbol == "US10Y")
            .order_by(prices.c.observed_at.desc()).limit(1)
        ).one()._mapping
        qualities = {r._mapping["quality"] for r in conn.execute(
            select(raw_observations).where(raw_observations.c.symbol == "US10Y"))}
    assert float(latest["value"]) == pytest.approx(4.35)
    assert qualities == {"ok"}


# --- an accepted level shift is a level RESET --------------------------------
#
# Letting the series out once is not enough. The MAD window is 30 accepted
# values, so the quote immediately AFTER an accepted re-level is still an
# outlier against 29 values of the level that was just abandoned — the run
# restarts at 1 and the whole 5-observation/90-minute wait is paid again, per
# price. Simulated against run_collect over 400 cycles of the real
# 0.4697 -> 4.682 shift at the 10-minute cron: acceptances at cycles
# 10, 20, 30 ... 150 and only then every cycle. One price per 100 minutes for
# 25 hours, out of the mechanism that exists to end a freeze.


def test_the_outlier_window_stops_at_the_last_accepted_level_shift():
    """The accepted series records the reset; nothing else has to remember it.

    A value more than MAX_JUMP_PCT from the last good one cannot reach
    `prices` on its own, so a step that large between two consecutive accepted
    prices is by construction a level shift this system chose to accept.
    """
    from app.jobs.collect import _since_last_level_reset

    old, new = 0.4697, 4.682
    # newest first: two at the new level, then the abandoned one
    assert _since_last_level_reset([new, new, old, old, old]) == [new, new]
    # no shift in view -> nothing is trimmed
    assert _since_last_level_reset([4.68, 4.70, 4.66, 4.71]) == [4.68, 4.70, 4.66, 4.71]
    # ordinary volatility is not a level shift (10% < MAX_JUMP_PCT = 15%)
    assert len(_since_last_level_reset([4.68, 4.25, 4.70, 4.66])) == 4
    # the newest shift wins when a series has re-levelled more than once
    assert _since_last_level_reset([9.0, 9.1, 4.6, 4.7, 0.46]) == [9.0, 9.1]
    assert _since_last_level_reset([]) == []
    assert _since_last_level_reset([4.682]) == [4.682]


def test_an_accepted_level_shift_lets_the_series_resume_immediately(
    engine, settings, monkeypatch
):
    """The cycle after the re-level must be an ordinary collection.

    Before the fix it was another suspect: same level, same provider, and a
    window still holding 29 values of the level the system had just agreed to
    abandon. The series limped at one price per 100 minutes for ~25 hours.
    """
    _seed_provider(engine, "yahoo", priority=10, category="global_gold")
    now = utcnow()
    _seed_us10y_prices(engine, 0.4697, now)
    _seed_suspect_run(engine, "yahoo", 4.68, now, count=4, spacing_minutes=30)

    stub = CountingStub([_tnx_obs("yahoo", 4.682, now)])
    _patch_registry(monkeypatch, {"yahoo": stub})
    run_collect(engine, settings, ["macro"])  # the escape hatch fires

    # the very next cron tick, same level, a normal small move
    stub._observations = [_tnx_obs("yahoo", 4.688, now + timedelta(minutes=10))]
    result = run_collect(engine, settings, ["macro"])

    assert result["collected"].get("US10Y") == 1, result["errors"]
    with engine.connect() as conn:
        at_new_level = [
            float(v) for (v,) in conn.execute(
                select(prices.c.value).where(
                    prices.c.symbol == "US10Y", prices.c.value > 1.0)
            )
        ]
    assert sorted(at_new_level) == pytest.approx([4.682, 4.688])


def test_a_spike_is_still_rejected_right_after_a_level_reset(
    engine, settings, monkeypatch
):
    """The property the whole mechanism exists to preserve.

    Trimming the window is not a licence to accept anything: the jump test is
    measured against the newly accepted level, so an outlier relative to THAT
    is still held with a run of one.
    """
    _seed_provider(engine, "yahoo", priority=10, category="global_gold")
    now = utcnow()
    _seed_us10y_prices(engine, 0.4697, now)
    _seed_suspect_run(engine, "yahoo", 4.68, now, count=4, spacing_minutes=30)

    stub = CountingStub([_tnx_obs("yahoo", 4.682, now)])
    _patch_registry(monkeypatch, {"yahoo": stub})
    run_collect(engine, settings, ["macro"])  # re-level accepted

    stub._observations = [_tnx_obs("yahoo", 9.9, now + timedelta(minutes=10))]
    run_collect(engine, settings, ["macro"])

    with engine.connect() as conn:
        values = [float(v) for (v,) in conn.execute(
            select(prices.c.value).where(prices.c.symbol == "US10Y"))]
        qualities = [
            r._mapping["quality"] for r in conn.execute(
                select(raw_observations).where(
                    raw_observations.c.symbol == "US10Y",
                    raw_observations.c.raw_value == 99.0)
            )
        ]
    assert 9.9 not in values
    assert qualities == ["suspect"]


def test_the_outlier_test_comes_back_once_the_new_level_has_a_window(
    engine, settings, monkeypatch
):
    """Trimming disables the MAD test only until five values exist at the new
    level — after that it guards again, against the level actually in force.

    4.95 is a 5.7% move: inside MAX_JUMP_PCT, so the jump test alone would
    wave it through. It is the robust test that has to catch it, and it can
    only do that measured against the new level's own dispersion.
    """
    _seed_provider(engine, "yahoo", priority=10, category="global_gold")
    now = utcnow()
    _seed_us10y_prices(engine, 0.4697, now)
    _seed_suspect_run(engine, "yahoo", 4.68, now, count=4, spacing_minutes=30)
    stub = CountingStub([_tnx_obs("yahoo", 4.682, now)])
    _patch_registry(monkeypatch, {"yahoo": stub})
    run_collect(engine, settings, ["macro"])

    for i, pct in enumerate((4.686, 4.679, 4.690, 4.684, 4.681), start=1):
        stub._observations = [_tnx_obs("yahoo", pct, now + timedelta(minutes=10 * i))]
        assert run_collect(engine, settings, ["macro"])["collected"].get("US10Y") == 1

    stub._observations = [_tnx_obs("yahoo", 4.95, now + timedelta(minutes=70))]
    result = run_collect(engine, settings, ["macro"])

    assert validation.jump_pct(4.95, 4.681) < validation.MAX_JUMP_PCT  # premise
    assert not result["collected"].get("US10Y")
    assert any("MAD" in e for e in result["errors"]), result["errors"]
