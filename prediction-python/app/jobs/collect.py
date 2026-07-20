"""Collection job: fetch -> validate -> dedupe -> store -> health/metrics.

Job categories map to canonical symbols; providers come from the
``data_providers`` registry ordered by priority.  Fallback semantics: a
lower-priority provider is only consulted for symbols the earlier providers
did not deliver a *good* value for.  Suspicious values (>15% jump vs last
good, or MAD outliers) are stored in ``raw_observations`` only, unless a
second source confirms them within tolerance — then both are promoted.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Sequence

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..config import Settings
from ..core import validation
from ..core.market_hours import is_acceptably_fresh
from ..db import insert_ignore, prices, raw_observations, utcnow
from ..metrics import COLLECT_FAILURE, COLLECT_SUCCESS, JOB_LAST_SUCCESS, LAST_PRICE_TS
from ..providers import registry
from ..providers.base import Observation, ProviderError

log = logging.getLogger(__name__)

JOB_SYMBOLS: dict[str, set[str]] = {
    "iran_gold": {"IR_GOLD_18K", "IR_COIN_EMAMI"},
    "fx": {"USD_IRT"},
    "global": {"XAUUSD", "XAGUSD"},
    "macro": {"BRENT_OIL", "DXY", "US10Y"},
}

# provider-registry categories consulted per job.  Note: global_gold providers
# come FIRST for the global job — TGJU's 'ons' quote is a useful backup but its
# ticker frequently lags the live market by 30-60 minutes (verified 2026-07-20:
# ons ts trailed geram18 ts by 40 minutes), so live sources take precedence.
JOB_PROVIDER_CATEGORIES: dict[str, list[str]] = {
    "iran_gold": ["iran_gold"],
    "fx": ["fx", "iran_gold"],
    "global": ["global_gold", "iran_gold"],
    "macro": ["global_gold", "macro"],
}

RECENT_WINDOW = 30  # good values used for the MAD outlier test


def _recent_values(engine: Engine, symbol: str, limit: int = RECENT_WINDOW) -> list[float]:
    stmt = (
        select(prices.c.value)
        .where(prices.c.symbol == symbol, prices.c.quality == "ok")
        .order_by(prices.c.observed_at.desc())
        .limit(limit)
    )
    with engine.connect() as conn:
        return [float(v) for (v,) in conn.execute(stmt)]


def _store(engine: Engine, obs: Observation, quality: str) -> tuple[bool, bool]:
    """Write raw_observations always; prices only for quality='ok'.

    Returns (raw_inserted, price_inserted).
    """
    dedupe = validation.build_dedupe_key(
        obs.provider_code, obs.symbol, obs.observed_at, obs.raw_value
    )
    now = utcnow()
    with engine.begin() as conn:
        raw_inserted = insert_ignore(
            conn,
            raw_observations,
            [
                {
                    "provider_code": obs.provider_code,
                    "symbol": obs.symbol,
                    "raw_value": obs.raw_value,
                    "unit": obs.raw_unit,
                    "currency": obs.raw_currency,
                    "raw_payload": obs.raw_payload,
                    "observed_at": obs.observed_at,
                    "collected_at": now,
                    "quality": quality,
                    "dedupe_key": dedupe,
                }
            ],
        )
        price_inserted = 0
        if quality == "ok":
            price_inserted = insert_ignore(
                conn,
                prices,
                [
                    {
                        "symbol": obs.symbol,
                        "value": obs.value,
                        "currency": obs.currency,
                        "unit": obs.unit,
                        "source": obs.provider_code,
                        "observed_at": obs.observed_at,
                        "collected_at": now,
                        "quality": "ok",
                    }
                ],
            )
    return bool(raw_inserted), bool(price_inserted)


def run_collect(
    engine: Engine, settings: Settings, jobs: Optional[Sequence[str]] = None
) -> dict:
    """Execute the collection pass; returns docs/CONTRACTS.md response shape."""
    requested = [j for j in (jobs or list(JOB_SYMBOLS)) if j in JOB_SYMBOLS]
    collected: dict[str, int] = {}
    errors: list[str] = []
    fetch_cache: dict[str, list[Observation]] = {}
    failed_providers: set[str] = set()

    # A stale observation still gets stored, but does NOT satisfy the symbol —
    # fallback continues so a provider with a lagging ticker (e.g. TGJU 'ons')
    # cannot mask a fresher source further down the priority list.  The gate is
    # market-hours aware (Addendum 1): while a market is closed, last-session
    # data satisfies the symbol instead of spamming "only stale values" errors
    # every Iranian evening/Friday and global weekend.

    for job in requested:
        symbols_needed = set(JOB_SYMBOLS[job])
        stale_only: set[str] = set()
        provider_rows = registry.load_provider_rows(
            engine, JOB_PROVIDER_CATEGORIES[job]
        )
        # pending suspects awaiting confirmation by a second source
        suspects: dict[str, list[Observation]] = {}

        for row in provider_rows:
            if not symbols_needed:
                break
            code = str(row["code"])
            if code in failed_providers:
                continue
            if code not in fetch_cache:
                provider = registry.build_provider(code, settings)
                if provider is None:
                    continue  # unknown or keyed-but-unconfigured provider
                try:
                    fetch_cache[code] = provider.fetch()
                    registry.record_success(engine, code)
                except (ProviderError, Exception) as exc:  # noqa: BLE001
                    failed_providers.add(code)
                    registry.record_failure(engine, code, str(exc))
                    COLLECT_FAILURE.labels(provider=code).inc()
                    errors.append(f"{code}: {exc}")
                    continue

            for obs in fetch_cache[code]:
                if obs.symbol not in symbols_needed:
                    continue
                recent = _recent_values(engine, obs.symbol)
                last_good = recent[0] if recent else None
                quality, reason = validation.classify_observation(
                    obs.symbol, obs.value, recent, last_good
                )
                if quality == "outlier":
                    _store(engine, obs, "outlier")
                    errors.append(f"{code}/{obs.symbol}: rejected ({reason})")
                    continue
                if quality == "suspect":
                    confirmed = any(
                        validation.values_agree(obs.value, other.value)
                        for other in suspects.get(obs.symbol, [])
                    )
                    if not confirmed:
                        suspects.setdefault(obs.symbol, []).append(obs)
                        _store(engine, obs, "suspect")
                        errors.append(
                            f"{code}/{obs.symbol}: held as suspect ({reason}); "
                            "awaiting confirmation by a second source"
                        )
                        continue  # symbol NOT satisfied -> fallback continues
                    # confirmed by an earlier suspect: promote both
                    for prior in suspects.pop(obs.symbol, []):
                        if validation.values_agree(obs.value, prior.value):
                            _, promoted = _store(engine, prior, "ok")
                            if promoted:
                                collected[obs.symbol] = collected.get(obs.symbol, 0) + 1
                                COLLECT_SUCCESS.labels(
                                    provider=prior.provider_code, symbol=obs.symbol
                                ).inc()
                _, price_inserted = _store(engine, obs, "ok")
                if price_inserted:
                    collected[obs.symbol] = collected.get(obs.symbol, 0) + 1
                    COLLECT_SUCCESS.labels(provider=code, symbol=obs.symbol).inc()
                    LAST_PRICE_TS.labels(symbol=obs.symbol).set(
                        obs.observed_at.timestamp()
                    )
                if is_acceptably_fresh(obs.symbol, obs.observed_at, utcnow(), settings):
                    symbols_needed.discard(obs.symbol)
                else:
                    stale_only.add(obs.symbol)

        for symbol in sorted(symbols_needed):
            if symbol in stale_only:
                errors.append(
                    f"{job}: only stale values available for {symbol} "
                    "(market closed or sources lagging)"
                )
            else:
                errors.append(f"{job}: no good value collected for {symbol}")

    JOB_LAST_SUCCESS.labels(job="collect").set(time.time())
    return {"collected": collected, "errors": errors}
