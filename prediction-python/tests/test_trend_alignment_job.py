"""Transition/persistence tests for ``app/jobs/trend_alignment.py``.

``tests/test_trend_alignment.py`` covers the rules; this file covers the part
the rules cannot get right on their own — deciding, against a database that
outlives the process, whether a conclusion is NEWS. Every test here is a case
where a plausible implementation fires an alert it should not, or stays silent
when it should speak:

* the same closed candles evaluated twice (a scheduler that runs every few
  minutes does this constantly);
* a restart with the state already persisted (an in-memory "previous" would
  re-fire the alert on every deploy);
* a state row that no longer remembers the transition (a restored backup, or
  two evaluators racing) — the unique index, not the comparison, has to be the
  guard;
* re-entry after a break, which MUST fire again, because the candles moved on.

Series are built from real hourly price rows and aggregated by the job's own
SQL, so the buckets under test are the buckets production computes. Periods
are shrunk to 2/3/4 so a handful of days of history is genuinely enough for
the slow MA — the alignment logic is period-agnostic, and 220-period warm-up
would only make the fixtures slower, not more truthful.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.config import Settings
from app.db import prices, trend_alignment_events, trend_alignment_states
from app.jobs.trend_alignment import (
    DISCLAIMER,
    SUPPORTED_SYMBOLS,
    TREND_ALERT_TYPE,
    run_trend_alignment,
)

UTC = timezone.utc
BASE = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)

GOLD = "IR_GOLD_18K"
GLOBAL = "XAUUSD"

# Hourly phases of the fixture series. Flat first so the opening state is
# genuinely not_aligned (every MA equals the price, and the strict stack test
# calls that neutral rather than guessing a direction).
FLAT_HOURS = 6 * 24
TREND_HOURS = 5 * 24
BREAK_HOURS = 2 * 24
REENTRY_HOURS = 4 * 24


def at(hours: int) -> datetime:
    """The instant ``hours`` after the series starts."""
    return BASE + timedelta(hours=hours)


# Evaluation instants, each the last hour of a phase.
FLAT_END = at(FLAT_HOURS - 1)
TRENDING = at(FLAT_HOURS + TREND_HOURS - 1)
BROKEN = at(FLAT_HOURS + TREND_HOURS + BREAK_HOURS - 1)
REENTERED = at(FLAT_HOURS + TREND_HOURS + BREAK_HOURS + REENTRY_HOURS - 1)


def _ramp(hours: int, start: float, step: float) -> list[float]:
    return [start + step * (i + 1) for i in range(hours)]


def _series(direction: float, base: float = 5000.0) -> list[float]:
    """Flat, then trending, then a sharp counter-move, then trending again."""
    flat = [base] * FLAT_HOURS
    trend = _ramp(TREND_HOURS, base, 4.0 * direction)
    brk = _ramp(BREAK_HOURS, trend[-1], -8.0 * direction)
    reentry = _ramp(REENTRY_HOURS, brk[-1], 12.0 * direction)
    return flat + trend + brk + reentry


def seed_prices(engine, symbol: str, values, *, start: datetime = BASE, quality="ok") -> None:
    """One good hourly observation per value, so each hour is one candle."""
    rows = [
        {
            "symbol": symbol,
            "value": float(value),
            "currency": "IRT",
            "unit": "gram",
            "source": "test",
            "observed_at": start + timedelta(hours=index),
            "quality": quality,
        }
        for index, value in enumerate(values)
    ]
    with engine.begin() as conn:
        conn.execute(prices.insert(), rows)


@pytest.fixture()
def trend_settings(settings: Settings) -> Settings:
    """Short periods: 4 closed candles per timeframe are enough to conclude."""
    settings.trend_alignment_enabled = True
    settings.trend_alignment_ma_type = "ema"
    settings.trend_alignment_fast_period = 2
    settings.trend_alignment_mid_period = 3
    settings.trend_alignment_slow_period = 4
    return settings


@pytest.fixture()
def bullish_engine(engine):
    seed_prices(engine, GOLD, _series(direction=1.0))
    return engine


def events(engine, symbol: str = GOLD) -> list:
    with engine.connect() as conn:
        return conn.execute(
            select(trend_alignment_events)
            .where(trend_alignment_events.c.symbol == symbol)
            .order_by(trend_alignment_events.c.id)
        ).mappings().all()


def state(engine, symbol: str = GOLD):
    with engine.connect() as conn:
        return conn.execute(
            select(trend_alignment_states).where(trend_alignment_states.c.symbol == symbol)
        ).mappings().first()


def run(engine, settings, when: datetime, symbols=(GOLD,)) -> dict:
    return run_trend_alignment(engine, settings, symbols=list(symbols), now=when)


# --- the fixture series is what the tests claim it is ------------------------


def test_series_walks_through_the_four_states(bullish_engine, trend_settings):
    """Guards the fixture itself: the phases must produce the states used below."""
    seen = [
        run(bullish_engine, trend_settings, when)["symbols"][GOLD]["alignment"]
        for when in (FLAT_END, TRENDING, BROKEN, REENTERED)
    ]
    assert seen == ["not_aligned", "full_bullish", "not_aligned", "full_bullish"]


# --- transitions -------------------------------------------------------------


def test_entering_full_bullish_creates_exactly_one_event(bullish_engine, trend_settings):
    run(bullish_engine, trend_settings, FLAT_END)
    outcome = run(bullish_engine, trend_settings, TRENDING)["symbols"][GOLD]

    assert outcome["alignment"] == "full_bullish"
    assert outcome["previous_alignment"] == "not_aligned"
    assert outcome["event_created"] is True

    rows = events(bullish_engine)
    assert len(rows) == 1
    assert rows[0]["alignment"] == "full_bullish"
    assert rows[0]["previous_alignment"] == "not_aligned"
    # The event carries the candles it was decided on, not just a timestamp.
    assert rows[0]["latest_1d_candle_close"] is not None
    assert rows[0]["latest_4h_candle_close"] is not None
    assert rows[0]["latest_1h_candle_close"] is not None
    assert rows[0]["timeframes"]["1d"]["trend"] == "bullish"


def test_staying_aligned_creates_no_further_event(bullish_engine, trend_settings):
    """Being aligned is not an event; only becoming aligned is.

    The second run sees NEW closed candles (an hour later) that still say
    full_bullish — the case a candle-identity-only guard would get wrong.
    """
    run(bullish_engine, trend_settings, FLAT_END)
    run(bullish_engine, trend_settings, TRENDING)
    later = run(bullish_engine, trend_settings, TRENDING + timedelta(hours=1))

    assert later["symbols"][GOLD]["alignment"] == "full_bullish"
    assert later["symbols"][GOLD]["event_created"] is False
    assert later["events"] == 0
    assert len(events(bullish_engine)) == 1
    # A fresh candle triple, so the run really did re-decide rather than skip.
    assert (
        later["symbols"][GOLD]["candle_identity"]["1h"]
        != events(bullish_engine)[0]["latest_1h_candle_close"].isoformat()
    )


def test_break_and_re_entry_creates_a_second_event(bullish_engine, trend_settings):
    """Alignment lost and regained is news again — the candles moved on."""
    run(bullish_engine, trend_settings, FLAT_END)
    run(bullish_engine, trend_settings, TRENDING)
    broken = run(bullish_engine, trend_settings, BROKEN)
    regained = run(bullish_engine, trend_settings, REENTERED)

    assert broken["symbols"][GOLD]["alignment"] == "not_aligned"
    assert broken["symbols"][GOLD]["event_created"] is False
    assert regained["symbols"][GOLD]["event_created"] is True

    rows = events(bullish_engine)
    assert len(rows) == 2
    assert [r["alignment"] for r in rows] == ["full_bullish", "full_bullish"]
    assert rows[1]["previous_alignment"] == "not_aligned"
    assert rows[1]["latest_1h_candle_close"] > rows[0]["latest_1h_candle_close"]


def test_same_closed_candles_cannot_produce_a_second_event(bullish_engine, trend_settings):
    """The unique index is the guard, not the state comparison.

    The state row is rolled back to what it was before the transition — a
    restored backup, or a second evaluator that read the state before the
    first one wrote it. The comparison therefore says "transition!" a second
    time, and the database is what refuses.
    """
    run(bullish_engine, trend_settings, FLAT_END)
    run(bullish_engine, trend_settings, TRENDING)
    assert len(events(bullish_engine)) == 1

    with bullish_engine.begin() as conn:
        conn.execute(
            trend_alignment_states.update()
            .where(trend_alignment_states.c.symbol == GOLD)
            .values(alignment="not_aligned", previous_alignment=None)
        )

    replayed = run(bullish_engine, trend_settings, TRENDING)
    assert replayed["symbols"][GOLD]["alignment"] == "full_bullish"
    assert replayed["symbols"][GOLD]["event_created"] is False
    assert replayed["events"] == 0
    assert len(events(bullish_engine)) == 1


def test_restart_with_state_already_persisted_is_silent(bullish_engine, trend_settings):
    """A new process, a new Settings object, the same database: no re-fire."""
    run(bullish_engine, trend_settings, FLAT_END)
    run(bullish_engine, trend_settings, TRENDING)
    before = state(bullish_engine)

    restarted = Settings(
        database_url="sqlite://",
        internal_api_token="test-internal-token",
        trend_alignment_fast_period=2,
        trend_alignment_mid_period=3,
        trend_alignment_slow_period=4,
    )
    after_restart = run(bullish_engine, restarted, TRENDING)

    assert after_restart["symbols"][GOLD]["event_created"] is False
    assert len(events(bullish_engine)) == 1
    after = state(bullish_engine)
    assert after["alignment"] == before["alignment"] == "full_bullish"
    assert after["previous_alignment"] == "not_aligned"
    # Re-confirming the same alignment is not a state change.
    assert after["state_version"] == before["state_version"]


def test_entering_full_bearish_creates_a_bearish_event(engine, trend_settings):
    seed_prices(engine, GLOBAL, _series(direction=-1.0))
    run(engine, trend_settings, FLAT_END, symbols=(GLOBAL,))
    outcome = run(engine, trend_settings, TRENDING, symbols=(GLOBAL,))["symbols"][GLOBAL]

    assert outcome["alignment"] == "full_bearish"
    assert outcome["event_created"] is True
    rows = events(engine, GLOBAL)
    assert len(rows) == 1
    assert rows[0]["alignment"] == "full_bearish"
    assert rows[0]["timeframes"]["1h"]["trend"] == "bearish"


# --- states that must never fire ---------------------------------------------


def test_unavailable_timeframe_never_produces_an_event(engine, trend_settings):
    """Two days of history: 1H and 4H can conclude, 1D cannot.

    Carrying the other two timeframes into a "full" alignment would present a
    conclusion the daily series does not support.
    """
    seed_prices(engine, GOLD, _ramp(2 * 24, 5000.0, 4.0))
    outcome = run(engine, trend_settings, at(2 * 24 - 1))["symbols"][GOLD]

    assert outcome["alignment"] == "not_aligned"
    assert outcome["event_created"] is False
    assert events(engine) == []
    stored = state(engine)
    assert stored["alignment"] == "not_aligned"
    assert stored["timeframes"]["1d"]["trend"] == "unavailable"
    assert stored["timeframes"]["1h"]["trend"] == "bullish"
    # Why it is unavailable is recorded: too little history, not a data gap.
    assert "required for the slow MA" in stored["timeframes"]["1d"]["reason"]


def test_disabled_flag_writes_nothing(bullish_engine, trend_settings):
    trend_settings.trend_alignment_enabled = False
    result = run(bullish_engine, trend_settings, TRENDING)

    assert result["enabled"] is False
    assert "TREND_ALIGNMENT_ENABLED" in result["reason"]
    assert result["symbols"] == {}
    assert state(bullish_engine) is None
    assert events(bullish_engine) == []


def test_unsupported_symbol_is_skipped_not_evaluated(bullish_engine, trend_settings):
    result = run(bullish_engine, trend_settings, TRENDING, symbols=("IR_COIN_EMAMI",))

    assert result["skipped"] == 1
    assert result["evaluated"] == 0
    assert result["symbols"]["IR_COIN_EMAMI"]["reason"] == "unsupported symbol"
    assert state(bullish_engine, "IR_COIN_EMAMI") is None


def test_suspicious_prices_are_not_candles(engine, trend_settings):
    """quality != 'ok' rows are excluded, exactly as candles.go excludes them."""
    seed_prices(engine, GOLD, _series(direction=1.0), quality="suspicious")
    outcome = run(engine, trend_settings, TRENDING)["symbols"][GOLD]

    assert outcome["hourly_candles"] == 0
    assert outcome["daily_candles"] == 0
    assert outcome["alignment"] == "not_aligned"
    assert events(engine) == []


# --- isolation ---------------------------------------------------------------


def test_symbols_keep_separate_state(engine, trend_settings):
    seed_prices(engine, GOLD, _series(direction=1.0))
    seed_prices(engine, GLOBAL, _series(direction=-1.0))

    run(engine, trend_settings, FLAT_END, symbols=SUPPORTED_SYMBOLS)
    result = run(engine, trend_settings, TRENDING, symbols=SUPPORTED_SYMBOLS)

    assert result["evaluated"] == 2
    assert result["events"] == 2
    assert state(engine, GOLD)["alignment"] == "full_bullish"
    assert state(engine, GLOBAL)["alignment"] == "full_bearish"
    assert [r["alignment"] for r in events(engine, GOLD)] == ["full_bullish"]
    assert [r["alignment"] for r in events(engine, GLOBAL)] == ["full_bearish"]

    # A break in one symbol leaves the other's state untouched.
    run(engine, trend_settings, BROKEN, symbols=(GOLD,))
    assert state(engine, GOLD)["alignment"] == "not_aligned"
    assert state(engine, GLOBAL)["alignment"] == "full_bearish"


def test_one_failing_symbol_does_not_stop_the_other(engine, trend_settings, monkeypatch):
    seed_prices(engine, GOLD, _series(direction=1.0))
    seed_prices(engine, GLOBAL, _series(direction=-1.0))
    from app.jobs import trend_alignment as job

    original = job._load_candles

    def explode(conn, symbol, unit, limit, now):
        if symbol == GOLD:
            raise RuntimeError("simulated series failure")
        return original(conn, symbol, unit, limit, now)

    monkeypatch.setattr(job, "_load_candles", explode)
    result = run(engine, trend_settings, TRENDING, symbols=SUPPORTED_SYMBOLS)

    assert result["failed"] == 1
    assert result["evaluated"] == 1
    assert result["symbols"][GOLD]["status"] == "error"
    assert state(engine, GOLD) is None
    assert state(engine, GLOBAL)["alignment"] == "full_bearish"


# --- the in-app alert --------------------------------------------------------

# The alerts tables belong to migration 0003 and are NOT mirrored in app/db.py
# (the Python service never wrote them before), so the SQLite fixture creates
# the columns this job actually uses.
ALERT_SCHEMA = (
    "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT NOT NULL)",
    "CREATE TABLE alerts ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " user_id TEXT NOT NULL REFERENCES users(id),"
    " alert_type TEXT NOT NULL,"
    " condition TEXT NOT NULL DEFAULT '{}',"
    " enabled BOOLEAN NOT NULL DEFAULT 1,"
    " cooldown_minutes INT NOT NULL DEFAULT 60,"
    " last_triggered_at TIMESTAMP,"
    " created_at TIMESTAMP,"
    " updated_at TIMESTAMP)",
    "CREATE TABLE alert_events ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " alert_id INTEGER NOT NULL REFERENCES alerts(id),"
    " user_id TEXT NOT NULL REFERENCES users(id),"
    " triggered_at TIMESTAMP NOT NULL,"
    " message TEXT NOT NULL,"
    " payload TEXT NOT NULL DEFAULT '{}',"
    " acknowledged BOOLEAN NOT NULL DEFAULT 0)",
)


def subscribe(engine, *, alert_type: str = TREND_ALERT_TYPE, enabled: bool = True) -> None:
    with engine.begin() as conn:
        for ddl in ALERT_SCHEMA:
            conn.execute(text(ddl))
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u1', 'a@example.com')"))
        conn.execute(
            text(
                "INSERT INTO alerts (user_id, alert_type, enabled) "
                "VALUES ('u1', :alert_type, :enabled)"
            ),
            {"alert_type": alert_type, "enabled": enabled},
        )


def alert_rows(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT id, alert_id, user_id, message, payload FROM alert_events ORDER BY id")
        ).mappings().all()


def test_transition_writes_an_alert_for_a_subscriber(bullish_engine, trend_settings):
    subscribe(bullish_engine)
    run(bullish_engine, trend_settings, FLAT_END)
    outcome = run(bullish_engine, trend_settings, TRENDING)["symbols"][GOLD]

    assert outcome["alerted"] is True
    rows = alert_rows(bullish_engine)
    assert len(rows) == 1
    message = rows[0]["message"]
    assert GOLD in message
    assert "BULLISH" in message
    # The evidence travels with the alert: every timeframe, its price and MAs.
    for timeframe in ("1D", "4H", "1H"):
        assert timeframe in message
    for period in ("EMA2", "EMA3", "EMA4"):
        assert period in message
    assert DISCLAIMER in message
    assert "not financial advice" in message

    event = events(bullish_engine)[0]
    assert event["alert_event_id"] == rows[0]["id"]
    stored = state(bullish_engine)
    assert stored["last_bullish_alert_at"] is not None
    assert stored["last_bearish_alert_at"] is None


def test_no_subscriber_still_records_the_event(bullish_engine, trend_settings):
    """The event is the record; the alert is only the notification."""
    subscribe(bullish_engine, alert_type="price_above")
    run(bullish_engine, trend_settings, FLAT_END)
    outcome = run(bullish_engine, trend_settings, TRENDING)["symbols"][GOLD]

    assert outcome["event_created"] is True
    assert outcome["alerted"] is False
    assert alert_rows(bullish_engine) == []
    assert events(bullish_engine)[0]["alert_event_id"] is None
    assert state(bullish_engine)["last_bullish_alert_at"] is None


def test_a_disabled_subscription_is_not_notified(bullish_engine, trend_settings):
    subscribe(bullish_engine, enabled=False)
    run(bullish_engine, trend_settings, FLAT_END)
    outcome = run(bullish_engine, trend_settings, TRENDING)["symbols"][GOLD]

    assert outcome["event_created"] is True
    assert outcome["alerted"] is False
    assert alert_rows(bullish_engine) == []


def test_an_unwritable_alert_never_loses_the_event(bullish_engine, trend_settings):
    """No alerts table at all: the transition is still recorded and not retried.

    Losing the event would be worse than losing the notification — the next
    run would re-decide the same transition and the user would eventually get
    an alert dated hours after the candles that caused it.
    """
    run(bullish_engine, trend_settings, FLAT_END)
    outcome = run(bullish_engine, trend_settings, TRENDING)["symbols"][GOLD]

    assert outcome["event_created"] is True
    assert outcome["alerted"] is False
    assert len(events(bullish_engine)) == 1
    assert state(bullish_engine)["alignment"] == "full_bullish"


# --- configuration -----------------------------------------------------------


@pytest.mark.parametrize(
    "fast,mid,slow",
    [(48, 26, 220), (26, 26, 220), (26, 48, 48), (0, 48, 220)],
)
def test_settings_reject_periods_that_are_not_ordered(fast, mid, slow):
    """A swapped pair would compare a slow average against a fast one."""
    with pytest.raises(ValueError):
        Settings(
            database_url="sqlite://",
            trend_alignment_fast_period=fast,
            trend_alignment_mid_period=mid,
            trend_alignment_slow_period=slow,
        )


def test_settings_reject_an_unknown_ma_type():
    with pytest.raises(ValueError):
        Settings(database_url="sqlite://", trend_alignment_ma_type="wma")


def test_defaults_are_the_documented_periods():
    defaults = Settings(database_url="sqlite://")
    assert defaults.trend_alignment_enabled is True
    assert defaults.trend_alignment_ma_type == "ema"
    assert (
        defaults.trend_alignment_fast_period,
        defaults.trend_alignment_mid_period,
        defaults.trend_alignment_slow_period,
    ) == (26, 48, 220)


# --- endpoint ----------------------------------------------------------------


def test_evaluate_endpoint_requires_the_internal_token(client):
    assert client.post("/internal/trend-alignment/evaluate").status_code == 401


def test_evaluate_endpoint_runs_the_pass(client, engine, settings):
    settings.trend_alignment_fast_period = 2
    settings.trend_alignment_mid_period = 3
    settings.trend_alignment_slow_period = 4
    seed_prices(engine, GOLD, _series(direction=1.0))

    response = client.post(
        "/internal/trend-alignment/evaluate",
        headers={"X-Internal-Token": "test-internal-token"},
        json={"symbols": [GOLD]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["evaluated"] == 1
    assert body["symbols"][GOLD]["symbol"] == GOLD
    # `now` is the wall clock here, so the fixture series is long stale: the
    # endpoint must still answer with a state rather than raise.
    assert body["symbols"][GOLD]["alignment"] in {
        "not_aligned", "full_bullish", "full_bearish",
    }
