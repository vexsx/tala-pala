"""Collection job: fetch -> validate -> dedupe -> store -> health/metrics.

Job categories map to canonical symbols; providers come from the
``data_providers`` registry ordered by priority.  Fallback semantics: a
lower-priority provider is only consulted for symbols the earlier providers
did not deliver a *good* value for.  Suspicious values (>15% jump vs last
good, or MAD outliers) are stored in ``raw_observations`` only, unless a
second source confirms them within tolerance — then both are promoted.

Cross-provider dispersion (Addendum 3) is measured in a second pass over the
responses already in the fetch cache: fallback stops *storing* once a symbol
is satisfied, but the lower-priority providers consulted for the job's other
symbols have usually quoted it too, and those quotes were being thrown away —
leaving the advertised "provider disagreement" signal structurally empty.
Peer quotes now land in ``raw_observations`` (never in ``prices``: which value
is *served* is still decided by priority alone) and their summary rides on the
winner's ``raw_payload`` under ``peer_dispersion``.  That key, rather than a
new table, because the row already carries (symbol, cycle, canonical source),
JSONB is queryable in place (``raw_payload->'peer_dispersion'``), and the
retention job prunes the measurement together with the observation it
describes — a table would add a migration, a retention rule and a join for no
extra information.
"""
from __future__ import annotations

import logging
import time
from typing import Optional, Sequence

from datetime import datetime, timedelta, timezone
from datetime import time as dt_time
from typing import Optional as _Optional

from zoneinfo import ZoneInfo

TEHRAN_TZ = ZoneInfo("Asia/Tehran")

from sqlalchemy import func, select, update
from sqlalchemy.engine import Engine

from ..config import Settings
from ..core import validation
from ..core.market_hours import is_acceptably_fresh, is_market_open
from ..db import app_settings, ensure_utc, insert_ignore, prices, raw_observations, utcnow
from ..metrics import COLLECT_FAILURE, COLLECT_SUCCESS, JOB_LAST_SUCCESS, LAST_PRICE_TS
from ..providers import registry
from ..providers.base import Observation, ProviderError

log = logging.getLogger(__name__)

JOB_SYMBOLS: dict[str, set[str]] = {
    "iran_gold": {"IR_GOLD_18K", "IR_COIN_EMAMI"},
    "fx": {"USD_IRT"},
    "global": {"XAUUSD", "XAGUSD"},
    "macro": {"BRENT_OIL", "DXY", "US10Y"},
    # Tehran-exchange gold funds (Addendum 7): unit prices + retail net flow.
    # Mirrors the DEFAULT_FUNDS budget (2 funds; free-tier quota) — symbols
    # added via TSETMC_FUNDS are stored anyway, this set only drives the
    # "collected everything?" bookkeeping.
    "funds": {"IR_GOLD_FUND_AYAR", "IR_GOLD_FUND_TALA", "IR_GOLD_FUND_FLOW"},
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
    "funds": ["iran_fund"],
}

RECENT_WINDOW = 30  # good values used for the MAD outlier test

# raw_payload key carrying the cross-provider dispersion of the cycle that
# produced the row (see the module docstring for why it lives here).
PEER_DISPERSION_KEY = "peer_dispersion"

# TSE funds quota guard: BrsApi's free tier budgets the TSETMC_Symbol
# endpoint at ~10 requests/day and each funds round costs one call per fund.
# The job fires at FIXED Tehran-local slots (TSETMC_FETCH_TIMES, default
# 12:00 / 15:00 / 18:00 -> 3 rounds x 2 funds = 6 requests/day): one round
# per slot, at the first collect tick at/after the slot time. 18:00 runs
# after the 17:00 close on purpose - it captures the settled closing data.
# Thursdays and Fridays (no TSE session) spend nothing.
FUNDS_JOB = "funds"
_TSE_THURSDAY, _TSE_FRIDAY = 3, 4  # Python weekday(): Monday=0


def _parse_fetch_times(raw: str) -> list[dt_time]:
    out: list[dt_time] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            hh, mm = part.split(":")
            out.append(dt_time(int(hh), int(mm)))
        except (TypeError, ValueError):
            continue
    return sorted(out) or [dt_time(12, 0), dt_time(15, 0), dt_time(18, 0)]


FUNDS_ATTEMPT_KEY = "tse_funds_last_attempt"


def funds_job_due(
    engine: Engine, settings: Settings, now: _Optional[object] = None
) -> bool:
    """True when the TSE funds job should spend its request budget now.

    Due when the most recent passed slot today has not been fetched yet
    (i.e. the last tse_funds fetch predates that slot). If several slots
    were missed (downtime), only ONE round fires - quota is never repaid.
    """
    at = ensure_utc(now or utcnow())
    local = at.astimezone(TEHRAN_TZ)
    if local.weekday() in (_TSE_THURSDAY, _TSE_FRIDAY):
        return False
    passed = [t for t in _parse_fetch_times(settings.tsetmc_fetch_times)
              if local.time() >= t]
    if not passed:
        return False
    slot_local = datetime.combine(local.date(), max(passed), tzinfo=TEHRAN_TZ)
    slot_utc = slot_local.astimezone(timezone.utc)
    with engine.connect() as conn:
        last = conn.execute(
            select(func.max(raw_observations.c.collected_at)).where(
                raw_observations.c.provider_code == "tse_funds"
            )
        ).scalar()
        # A failed fetch stores no rows, which used to leave the slot "due"
        # on every subsequent collect tick — burning the ~10/day BrsApi quota
        # on a broken key or mirror outage. The attempt marker consumes the
        # slot regardless of outcome.
        attempt = conn.execute(
            select(app_settings.c.value).where(app_settings.c.key == FUNDS_ATTEMPT_KEY)
        ).scalar()
    marks = [ensure_utc(last)] if last is not None else []
    if isinstance(attempt, dict) and attempt.get("at"):
        try:
            marks.append(ensure_utc(datetime.fromisoformat(attempt["at"])))
        except (TypeError, ValueError):
            pass
    return not marks or max(marks) < slot_utc


def mark_funds_attempt(engine: Engine, at: _Optional[object] = None) -> None:
    """Persist the funds-slot attempt marker (success or failure alike)."""
    from ..jobs.evaluate import upsert_setting

    upsert_setting(engine, FUNDS_ATTEMPT_KEY,
                   {"at": ensure_utc(at or utcnow()).isoformat()})


def _recent_values(engine: Engine, symbol: str, limit: int = RECENT_WINDOW) -> list[float]:
    stmt = (
        select(prices.c.value)
        .where(prices.c.symbol == symbol, prices.c.quality == "ok")
        .order_by(prices.c.observed_at.desc())
        .limit(limit)
    )
    with engine.connect() as conn:
        return [float(v) for (v,) in conn.execute(stmt)]


def _store(
    engine: Engine, obs: Observation, quality: str, *, canonical: bool = True
) -> tuple[bool, bool]:
    """Write raw_observations always; prices only for canonical quality='ok'.

    ``canonical=False`` records a *peer* observation — kept for dispersion
    measurement only.  It never reaches ``prices``, so the served value stays
    exactly the one the priority fallback chose.

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
        if quality == "ok" and canonical:
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


def _annotate_dispersion(engine: Engine, winner: Observation, summary: dict) -> None:
    """Attach ``summary`` to the winner's raw_observations row, keeping the
    provider's own payload keys (e.g. Hamrah Gold's ``spread_pct``)."""
    payload = dict(winner.raw_payload or {})
    payload[PEER_DISPERSION_KEY] = summary
    dedupe = validation.build_dedupe_key(
        winner.provider_code, winner.symbol, winner.observed_at, winner.raw_value
    )
    with engine.begin() as conn:
        conn.execute(
            update(raw_observations)
            .where(raw_observations.c.dedupe_key == dedupe)
            .values(raw_payload=payload)
        )


def _measure_dispersion(
    engine: Engine,
    fetch_cache: dict[str, list[Observation]],
    symbols: set[str],
    handled: set[tuple[str, str]],
    cycle_values: dict[str, dict[str, float]],
    winners: dict[str, Observation],
) -> None:
    """Store the quotes the fallback skipped and summarise the disagreement.

    Reads ONLY responses already in ``fetch_cache``: dispersion must never
    cost an extra request, or measuring it would quietly spend the daily
    budget of the request-billed providers (``tse_funds`` / ``brsapi``, both
    ``max_attempts == 1``).

    Peers are gated on UNIT SANITY only, deliberately NOT on the winner's
    recent-value window. Running ``classify_observation`` against the price
    this very cycle just wrote censors exactly what is being measured: a peer
    more than MAX_JUMP_PCT (15%) away is dropped as "suspect", so the largest
    disagreements vanish and the summary collapses to ``None`` precisely when
    dispersion matters most; and a provider with a persistent structural
    offset (retail vs wholesale) exceeds the winner's ~1.2% MAD threshold
    every cycle and is excluded forever, biasing ``spread_pct`` toward zero.
    A peer can never reach ``prices`` (``canonical=False``), so the only real
    risk is a unit mix-up, which the sanity band catches.
    """
    for symbol in sorted(symbols):
        for code in sorted(fetch_cache):
            if (code, symbol) in handled:
                continue  # already stored by the fallback pass
            for obs in fetch_cache[code]:
                if obs.symbol != symbol:
                    continue
                sane = validation.sanity_ok(symbol, float(obs.value))
                _store(engine, obs, "ok" if sane else "outlier", canonical=False)
                handled.add((code, symbol))
                if sane:
                    cycle_values.setdefault(symbol, {})[code] = float(obs.value)
                else:
                    # a peer never blocked the job, so it is a log line, not a
                    # collection error the operator has to triage
                    log.debug("peer %s/%s failed the unit sanity band", code, symbol)
        summary = validation.dispersion_summary(cycle_values.get(symbol, {}))
        winner = winners.get(symbol)
        # No winner means no canonical row was written this cycle (every source
        # repeated its last quote, or none was good): the peers are stored, but
        # an earlier cycle's row is not rewritten with today's peer view.
        if summary is not None and winner is not None:
            _annotate_dispersion(engine, winner, summary)


def run_collect(
    engine: Engine, settings: Settings, jobs: Optional[Sequence[str]] = None
) -> dict:
    """Execute the collection pass; returns docs/CONTRACTS.md response shape."""
    requested = [j for j in (jobs or list(JOB_SYMBOLS)) if j in JOB_SYMBOLS]
    collected: dict[str, int] = {}
    errors: list[str] = []
    fetch_cache: dict[str, list[Observation]] = {}
    failed_providers: set[str] = set()
    # dispersion bookkeeping, run-wide so a provider fetched for one job also
    # contributes its quotes of another job's symbols (the fx job stops at
    # bitmax, but TGJU's USD_IRT is already in the cache from iran_gold)
    attempted_symbols: set[str] = set()
    handled: set[tuple[str, str]] = set()          # (provider, symbol) stored
    cycle_values: dict[str, dict[str, float]] = {}  # symbol -> {provider: value}
    winners: dict[str, Observation] = {}            # symbol -> canonical quote

    # A stale observation still gets stored, but does NOT satisfy the symbol —
    # fallback continues so a provider with a lagging ticker (e.g. TGJU 'ons')
    # cannot mask a fresher source further down the priority list.  The gate is
    # market-hours aware (Addendum 1): while a market is closed, last-session
    # data satisfies the symbol instead of spamming "only stale values" errors
    # every Iranian evening/Friday and global weekend.

    for job in requested:
        if job == FUNDS_JOB:
            if not funds_job_due(engine, settings):
                continue  # market closed or request budget spent too recently
            mark_funds_attempt(engine)
        symbols_needed = set(JOB_SYMBOLS[job])
        stale_only: set[str] = set()
        provider_rows = registry.load_provider_rows(
            engine, JOB_PROVIDER_CATEGORIES[job]
        )
        # a job whose every provider is dormant (keyed but unconfigured, e.g.
        # tse_funds without BRSAPI_KEY) is silently skipped instead of
        # reporting "no good value" every cycle
        buildable = [
            row for row in provider_rows
            if registry.build_provider(str(row["code"]), settings) is not None
        ]
        if provider_rows and not buildable:
            continue
        attempted_symbols |= symbols_needed  # dormant jobs measure nothing
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
                handled.add((code, obs.symbol))  # every branch below stores it
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
                            cycle_values.setdefault(obs.symbol, {})[
                                prior.provider_code
                            ] = float(prior.value)
                            if promoted:
                                collected[obs.symbol] = collected.get(obs.symbol, 0) + 1
                                winners.setdefault(obs.symbol, prior)
                                COLLECT_SUCCESS.labels(
                                    provider=prior.provider_code, symbol=obs.symbol
                                ).inc()
                _, price_inserted = _store(engine, obs, "ok")
                # a repeat of the last quote inserts nothing but is still this
                # cycle's opinion of the price, so it counts towards dispersion
                cycle_values.setdefault(obs.symbol, {})[code] = float(obs.value)
                if price_inserted:
                    collected[obs.symbol] = collected.get(obs.symbol, 0) + 1
                    winners.setdefault(obs.symbol, obs)
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

    try:
        _measure_dispersion(
            engine, fetch_cache, attempted_symbols, handled, cycle_values, winners
        )
    except Exception as exc:  # noqa: BLE001
        # the prices are already committed; a broken measurement must not turn
        # a successful collection into a failed job (and a retried fetch round)
        log.warning("peer dispersion pass failed: %s", exc)
        errors.append(f"peer dispersion: {exc}")

    JOB_LAST_SUCCESS.labels(job="collect").set(time.time())
    return {"collected": collected, "errors": errors}
