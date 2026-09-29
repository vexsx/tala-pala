"""The Yahoo daily backfill (app/jobs/backfill.py): what a stored close claims.

Every row is stamped 23:00 UTC on its bar's own date, so the newest bar a
session-time run receives — today's, still forming — would claim a close that
does not exist yet.  Measured on the local rehearsal of 2026-09-29: a run at
12:18 UTC stored DXY and BRENT_OIL at 2026-09-29 23:00, and /prices/current
served Brent's forming price as current, not stale.
"""
from __future__ import annotations

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
