"""The Yahoo daily backfill (app/jobs/backfill.py): what a stored close claims.

Every row is stamped 23:00 UTC on its bar's own date, so the newest bar a
session-time run receives — today's, still forming — would claim a close that
does not exist yet.  Measured on the local rehearsal of 2026-09-29: a run at
12:18 UTC stored DXY and BRENT_OIL at 2026-09-29 23:00, and /prices/current
served Brent's forming price as current, not stale.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import select

from app.db import prices, raw_observations
from app.jobs import backfill


def _bars():
    """Three daily bars as Yahoo stamps them (session start), the last today."""
    return [
        (datetime(2026, 9, 25, 0, 0, tzinfo=timezone.utc), 101.0),
        (datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc), 104.34),
        (datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc), 96.36),  # forming
    ]


def _run(monkeypatch, engine, settings, at):
    monkeypatch.setattr(backfill.YahooProvider, "fetch_history",
                        lambda self, ticker, range_="5y": _bars())
    monkeypatch.setattr(backfill, "utcnow", lambda: at)
    return backfill.backfill_symbol(engine, settings, "BRENT_OIL")


def test_todays_forming_bar_is_not_stored(monkeypatch, engine, settings):
    at = datetime(2026, 9, 29, 12, 18, tzinfo=timezone.utc)
    report = _run(monkeypatch, engine, settings, at)
    assert report["inserted"] == 2 and report["unsettled_skipped"] == 1
    with engine.connect() as conn:
        stamps = [r.observed_at for r in conn.execute(select(prices.c.observed_at))]
        raw = [r.observed_at for r in conn.execute(select(raw_observations.c.observed_at))]
    newest = max(s.replace(tzinfo=s.tzinfo or timezone.utc) for s in stamps)
    assert newest == datetime(2026, 9, 28, 23, 0, tzinfo=timezone.utc)
    assert len(raw) == 2
    # Nothing stored is dated after the run that stored it.
    assert all(s.replace(tzinfo=s.tzinfo or timezone.utc) <= at for s in stamps + raw)


def test_the_close_is_stored_once_its_stamp_has_passed(monkeypatch, engine, settings):
    _run(monkeypatch, engine, settings, datetime(2026, 9, 29, 12, 18, tzinfo=timezone.utc))
    later = _run(monkeypatch, engine, settings,
                 datetime(2026, 9, 29, 23, 30, tzinfo=timezone.utc))
    assert later["inserted"] == 1 and later["unsettled_skipped"] == 0
    assert later["skipped_existing"] == 2
    with engine.connect() as conn:
        values = sorted(float(r.value) for r in conn.execute(select(prices.c.value)))
    assert values == [96.36, 101.0, 104.34]


def _legacy_forming_row(engine, quality="ok"):
    """What a run before the fix left behind — the local rehearsal holds
    exactly this row: Brent's forming 96.36 at 12:18 UTC, stamped 23:00 UTC
    as the close of 2026-09-29, with its audit row."""
    stamp = datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc)
    collected = datetime(2026, 9, 29, 12, 18, tzinfo=timezone.utc)
    with engine.begin() as conn:
        conn.execute(prices.insert().values(
            symbol="BRENT_OIL", value=96.36, currency="USD", unit="bbl",
            source="yahoo_backfill", observed_at=stamp, collected_at=collected, quality=quality))
        conn.execute(raw_observations.insert().values(
            provider_code="yahoo", symbol="BRENT_OIL", raw_value=96.36, unit="bbl",
            currency="USD", raw_payload={"kind": "backfill"}, observed_at=stamp,
            collected_at=collected, quality="ok",
            dedupe_key="yahoo|BRENT_OIL|backfill|2026-09-29"))


def _settled_bars():
    return [
        (datetime(2026, 9, 28, 0, 0, tzinfo=timezone.utc), 104.34),
        (datetime(2026, 9, 29, 0, 0, tzinfo=timezone.utc), 97.10),  # settled since
    ]


def test_a_forming_bar_stored_before_the_fix_is_replaced_by_its_close(monkeypatch, engine,
                                                                      settings):
    """A row stored before its close existed is detectable — its stamp is
    later than when it was collected — and it is not a close: the run after
    its stamp replaces it with the settled close instead of skipping the day
    for good (a scratch replay kept 96.36 and reported skipped_existing)."""
    _legacy_forming_row(engine)
    monkeypatch.setattr(backfill.YahooProvider, "fetch_history",
                        lambda self, ticker, range_="5y": _settled_bars())
    at = datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(backfill, "utcnow", lambda: at)
    report = backfill.backfill_symbol(engine, settings, "BRENT_OIL")
    assert report["forming_replaced"] == 1 and report["inserted"] == 1
    with engine.connect() as conn:
        row = conn.execute(select(prices).where(
            prices.c.observed_at == datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc))).mappings().one()
        raw = conn.execute(select(raw_observations).where(
            raw_observations.c.dedupe_key == "yahoo|BRENT_OIL|backfill|2026-09-29")).mappings().one()
    assert (float(row["value"]), row["quality"]) == (97.10, "ok")
    collected = row["collected_at"].replace(tzinfo=row["collected_at"].tzinfo or timezone.utc)
    assert collected == at  # no longer stamped before it was collected
    assert float(raw["raw_value"]) == 97.10
    assert raw["raw_payload"]["replaced_forming_value"] == 96.36
    # And the day is a close now: the next run changes nothing.
    again = backfill.backfill_symbol(engine, settings, "BRENT_OIL")
    assert again["forming_replaced"] == 0 and again["inserted"] == 0


def test_a_forming_bar_flagged_by_migration_0033_is_replaced_too(monkeypatch, engine, settings):
    """0033 flags such rows 'suspect' so no reader serves them meanwhile; the
    next run stores the close over the flagged row, as 'ok'."""
    _legacy_forming_row(engine, quality="suspect")
    monkeypatch.setattr(backfill.YahooProvider, "fetch_history",
                        lambda self, ticker, range_="5y": _settled_bars())
    monkeypatch.setattr(backfill, "utcnow", lambda: datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc))
    assert backfill.backfill_symbol(engine, settings, "BRENT_OIL")["forming_replaced"] == 1
    with engine.connect() as conn:
        quality = conn.execute(select(prices.c.quality).where(
            prices.c.observed_at == datetime(2026, 9, 29, 23, 0, tzinfo=timezone.utc))).scalar_one()
    assert quality == "ok"


def test_migration_0033_flags_exactly_the_rows_stamped_after_their_collection():
    """The predicate is the detection: a yahoo_backfill row collected before
    its own 23:00 UTC stamp. The down migration is its exact inverse."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "database", "migrations", "0033_yahoo_forming_bars.{}.sql")
    up = open(path.format("up"), encoding="utf-8").read()
    down = open(path.format("down"), encoding="utf-8").read()
    for sql, wanted, was in ((up, "'suspect'", "'ok'"), (down, "'ok'", "'suspect'")):
        body = " ".join(line for line in sql.splitlines() if not line.strip().startswith("--"))
        assert f"SET quality = {wanted}" in body
        assert "source = 'yahoo_backfill'" in body and "observed_at > collected_at" in body
        assert f"quality = {was}" in body
        assert "raw_observations" not in body  # the provider's record stays as it was
