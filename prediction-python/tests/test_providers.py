"""Provider parsing tests against saved fixtures + one respx round trip."""
from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest
import respx

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
from app.providers.base import ProviderError

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
