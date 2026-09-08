"""History backfill CLI: ``python -m app.seed.seed_history``.

Order of preference per symbol:

1. ``--from-csv PATH --symbol SYM`` — explicit CSV import (columns
   ``date,value`` or ``Date,Close``);
2. TGJU daily history (``summary-table-data``) for IR_GOLD_18K / USD_IRT —
   real backfill, rial values normalized to toman;
3. margani/pricedb history.json (GitHub dataset mirroring TGJU, rial values
   normalized to toman) for IR_GOLD_18K / USD_IRT;
4. bundled sample CSVs in ``data_samples/`` when live Iranian history is
   unavailable (source recorded as ``seed_sample`` so it is distinguishable).

Rows are written to ``prices`` with INSERT .. ON CONFLICT DO NOTHING, so the
command is idempotent.

Why XAUUSD is NOT seeded here any more
--------------------------------------
This module used to write XAUUSD from TGJU ``ons`` (the **spot** ounce), then
Stooq (**spot**), then Yahoo ``GC=F`` (a COMEX **futures** proxy) — three
different instruments landing in one symbol, whichever answered first.  Spot
and a front-month future differ by the cost of carry, so that splice
manufactures a level discontinuity at whatever date the source changed and
silently corrupts every long-run gold statistic computed across it.
``app/jobs/tgju_backfill.py`` refuses XAUUSD for exactly this reason and says
so to the caller; leaving the same splice reachable from the seeder would have
made the two paths contradict each other, and the seeder is the one an
operator runs by hand on a fresh install.

The consistent choice is the seeder losing the symbol rather than the backfill
job gaining the splice, because XAUUSD already has ONE honest history path:
``app/jobs/backfill.py`` (``POST /internal/backfill/history``) pulls the same
``GC=F`` series the live collector uses, stamps each close at 23:00 UTC
instead of this module's look-ahead 12:00, and writes the ``raw_observations``
audit row this module never wrote.  ``scripts/init.sh`` calls it.

The bundled XAUUSD sample stays, for the offline demo install that has no
network at all; it is written under ``seed_sample`` so it is never mistaken
for a measurement.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from datetime import date, datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from ..config import Settings, get_settings
from ..core.normalize import SYMBOL_META
from ..db import create_db_engine, insert_ignore, prices, utcnow
from ..providers import pricedb
from ..providers.base import ProviderError
from ..providers.pricedb import PriceDBProvider
from ..providers.tgju import TGJUProvider, normalize_history_value

log = logging.getLogger(__name__)

SAMPLES = {
    "IR_GOLD_18K": "ir_gold_18k_daily_sample.csv",
    "USD_IRT": "usd_irt_daily_sample.csv",
    "XAUUSD": "xauusd_daily_sample.csv",
}
# Deliberately no 'XAUUSD': 'ons' (module docstring). TGJU's ons is the spot
# ounce and our XAUUSD is the GC=F futures proxy; they are different
# instruments and must not share a symbol.
TGJU_SLUGS = {"IR_GOLD_18K": "geram18", "USD_IRT": "price_dollar_rl"}
PRICEDB_SLUGS = {"IR_GOLD_18K": "geram18", "USD_IRT": "price_dollar_rl"}
# Symbols this command seeds from the network. XAUUSD is absent on purpose:
# its history belongs to app/jobs/backfill.py, which uses the same GC=F series
# the live collector does, the honest 23:00 UTC availability stamp, and a
# raw_observations audit row. It is still listed in SAMPLES so that
# --offline (a demo install with no network) has something to show.
NETWORK_SYMBOLS = ("IR_GOLD_18K", "USD_IRT")
SEED_SYMBOLS = ("IR_GOLD_18K", "USD_IRT", "XAUUSD")
DEFAULT_SAMPLES_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data_samples",
)


def _noon_utc(day: date) -> datetime:
    """Daily closes are stored at 12:00 UTC (deterministic, mid-day)."""
    return datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=timezone.utc)


def store_daily(
    engine: Engine,
    symbol: str,
    rows: Iterable[tuple[date, float]],
    source: str,
) -> int:
    currency, unit = SYMBOL_META[symbol]
    now = utcnow()
    payload = [
        {
            "symbol": symbol,
            "value": float(value),
            "currency": currency,
            "unit": unit,
            "source": source,
            "observed_at": _noon_utc(day),
            "collected_at": now,
            "quality": "ok",
        }
        for day, value in rows
        if value > 0
    ]
    with engine.begin() as conn:
        return insert_ignore(conn, prices, payload)


def read_csv_rows(path: str) -> list[tuple[date, float]]:
    """Read (date, value) rows from a CSV with flexible headers."""
    out: list[tuple[date, float]] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return out
        fields = {name.lower(): name for name in reader.fieldnames}
        date_col = fields.get("date")
        value_col = fields.get("value") or fields.get("close")
        if date_col is None or value_col is None:
            raise SystemExit(
                f"{path}: need 'date' and 'value'/'close' columns, got {reader.fieldnames}"
            )
        for row in reader:
            try:
                day = datetime.strptime(str(row[date_col]).strip(), "%Y-%m-%d").date()
                value = float(str(row[value_col]).replace(",", "").strip())
            except (TypeError, ValueError):
                continue
            out.append((day, value))
    return out


def _count(engine: Engine, symbol: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                select(func.count()).select_from(prices).where(prices.c.symbol == symbol)
            ).scalar_one()
        )


# TGJU's deepest table ('ons', global gold) starts 1979-12-26; nothing they
# publish predates that, and a bar cannot come from the future.
_MIN_HISTORY_YEAR = 1970


def _refuse_implausible_dates(symbol: str, slug: str, rows: list) -> None:
    """Raise when any parsed bar date falls outside the plausible window."""
    today = date.today()
    for day, _close in rows:
        if not (_MIN_HISTORY_YEAR <= day.year <= today.year + 1):
            raise ProviderError(
                f"tgju {slug}: bar dated {day.isoformat()} is outside the "
                f"plausible window {_MIN_HISTORY_YEAR}..{today.year + 1} — "
                "the Gregorian column may be carrying a Jalali date; refusing "
                f"the whole payload for {symbol}"
            )


def seed_from_network(engine: Engine, settings: Settings, symbol: str) -> int:
    """Try TGJU history, then pricedb.  Returns inserted count.

    Iranian symbols only — see the module docstring for why XAUUSD is not
    seeded from here.
    """
    kwargs = {
        "timeout": settings.http_timeout_seconds,
        "courtesy_delay": settings.provider_courtesy_delay,
        "backoff_base": settings.provider_backoff_base,
    }
    if symbol in TGJU_SLUGS:
        slug = TGJU_SLUGS[symbol]
        try:
            raw_rows = TGJUProvider(**kwargs).fetch_history(slug)
            # Column 6 of the summary table is documented as Gregorian and
            # column 7 as Jalali, but a provider-side column shift would put a
            # Jalali year here and it would parse CLEANLY -- '1405/01/19' is a
            # valid date in the year 1405. Nothing downstream would notice: we
            # would write a price observed six centuries ago, and the
            # relative-value engine would then measure a return over a 600-year
            # window. jobs/tgju_backfill.py guards this on its own path; the
            # seeder reads the same parser and needs the same guard.
            #
            # The whole payload is refused rather than the offending rows
            # dropped: a shifted column makes EVERY row suspect, and silently
            # skipping some would quietly lose history instead of failing.
            _refuse_implausible_dates(symbol, slug, raw_rows)
            rows = [(day, normalize_history_value(slug, close)) for day, close in raw_rows]
            inserted = store_daily(engine, symbol, rows, "tgju")
            log.info("tgju history for %s: %d rows inserted", symbol, inserted)
            return inserted
        except (ProviderError, Exception) as exc:  # noqa: BLE001
            log.warning("tgju history failed for %s: %s", symbol, exc)
    if symbol in PRICEDB_SLUGS:
        slug = PRICEDB_SLUGS[symbol]
        try:
            raw_rows = PriceDBProvider(**kwargs).fetch_history(slug)
            rows = [
                (day, pricedb.normalize_history_value(slug, close))
                for day, close in raw_rows
            ]
            inserted = store_daily(engine, symbol, rows, "pricedb")
            log.info("pricedb history for %s: %d rows inserted", symbol, inserted)
            return inserted
        except (ProviderError, Exception) as exc:  # noqa: BLE001
            log.warning("pricedb history failed for %s: %s", symbol, exc)
    return 0


def seed_from_samples(engine: Engine, samples_dir: str, symbol: str) -> int:
    filename = SAMPLES.get(symbol)
    if filename is None:
        return 0
    path = os.path.join(samples_dir, filename)
    if not os.path.exists(path):
        log.warning("sample file missing: %s", path)
        return 0
    return store_daily(engine, symbol, read_csv_rows(path), "seed_sample")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.seed.seed_history",
        description="Backfill daily price history into the prices table.",
    )
    parser.add_argument("--from-csv", metavar="PATH", help="import one CSV file")
    parser.add_argument(
        "--symbol", default="IR_GOLD_18K",
        help="symbol for --from-csv (default IR_GOLD_18K)",
    )
    parser.add_argument("--samples-dir", default=DEFAULT_SAMPLES_DIR)
    parser.add_argument(
        "--offline", action="store_true",
        help="skip network sources; use bundled samples only",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    settings = get_settings()
    engine = create_db_engine(settings.database_url)

    if args.from_csv:
        if args.symbol not in SYMBOL_META:
            raise SystemExit(f"unknown symbol {args.symbol!r}")
        inserted = store_daily(
            engine, args.symbol, read_csv_rows(args.from_csv), "csv_import"
        )
        print(f"{args.symbol}: {inserted} rows imported from {args.from_csv}")
        return 0

    for symbol in SEED_SYMBOLS:
        existing = _count(engine, symbol)
        inserted = 0
        if not args.offline and symbol in NETWORK_SYMBOLS:
            inserted = seed_from_network(engine, settings, symbol)
        # The sample fallback exists for an install with no usable network.
        # For XAUUSD that now means --offline only: online, its history comes
        # from POST /internal/backfill/history (GC=F), and dropping synthetic
        # rows in front of that real series would recreate the splice this
        # module was just cleaned of.
        may_use_samples = args.offline or symbol in NETWORK_SYMBOLS
        if may_use_samples and inserted == 0 and existing + inserted < 10:
            inserted = seed_from_samples(engine, args.samples_dir, symbol)
            if inserted:
                print(f"{symbol}: live history unavailable; "
                      f"loaded {inserted} bundled sample rows")
        elif symbol not in NETWORK_SYMBOLS and not args.offline:
            print(f"{symbol}: not seeded here — history comes from "
                  f"POST /internal/backfill/history (app/jobs/backfill.py)")
        print(f"{symbol}: existing={existing} inserted={inserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
