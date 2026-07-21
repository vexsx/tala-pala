"""Provider registry.

Provider *rows* live in the ``data_providers`` table (seeded by migration
0001, tunable at runtime: enabled flag, priority).  This module maps rows to
concrete provider classes, orders them by priority, exposes fallback
iteration, and records health (last_success_at / consecutive_failures /
last_error) back into the table.
"""
from __future__ import annotations

import logging
from typing import Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import data_providers, ensure_utc, utcnow
from .alanchand import AlanchandProvider
from .base import Provider
from .brsapi import BrsApiProvider
from .gold_api import GoldAPIProvider
from .metals_dev import MetalsDevProvider
from .milligold import MilligoldProvider
from .navasan import NavasanProvider
from .pricedb import PriceDBProvider
from .stooq import StooqProvider
from .tgju import TGJUProvider
from .yahoo import YahooProvider

log = logging.getLogger(__name__)

UNHEALTHY_AFTER_FAILURES = 3


def build_provider(code: str, settings: Settings) -> Optional[Provider]:
    """Instantiate a provider by registry code.

    Returns None for unknown codes (e.g. 'frankfurter', handled elsewhere) and
    for keyed providers whose API key is not configured.
    """
    kwargs = {
        "timeout": settings.http_timeout_seconds,
        "courtesy_delay": settings.provider_courtesy_delay,
        "backoff_base": settings.provider_backoff_base,
    }
    if code == "tgju":
        return TGJUProvider(**kwargs)
    if code == "alanchand":
        # two modes: documented Bearer-token API when ALANCHAND_TOKEN is set,
        # keyless HTML parsing of the public 18ayar page otherwise
        return AlanchandProvider(token=settings.alanchand_token, **kwargs)
    if code == "milligold":
        return MilligoldProvider(**kwargs)
    if code == "yahoo":
        return YahooProvider(**kwargs)
    if code == "stooq":
        return StooqProvider(**kwargs)
    if code == "pricedb":
        return PriceDBProvider(**kwargs)
    if code == "gold_api":
        return GoldAPIProvider(**kwargs)
    if code == "brsapi":
        if not settings.brsapi_api_key:
            return None
        return BrsApiProvider(api_key=settings.brsapi_api_key, **kwargs)
    if code == "tse_funds":
        # shares BRSAPI_KEY (BrsApi's TSETMC mirror; tsetmc.com itself is
        # geo-blocked outside Iran) — dormant until the key is configured
        if not settings.brsapi_api_key:
            return None
        from .tse_funds import TSEFundsProvider, parse_funds_config

        return TSEFundsProvider(
            api_key=settings.brsapi_api_key,
            funds=parse_funds_config(settings.tsetmc_funds),
            **kwargs,
        )
    if code == "navasan":
        if not settings.navasan_api_key:
            return None
        return NavasanProvider(api_key=settings.navasan_api_key, **kwargs)
    if code == "metals_dev":
        if not settings.metals_dev_api_key:
            return None
        return MetalsDevProvider(api_key=settings.metals_dev_api_key, **kwargs)
    return None


def load_provider_rows(
    engine: Engine, categories: Optional[Iterable[str]] = None
) -> list[dict]:
    """Enabled provider rows ordered by (priority, code) — lower priority first."""
    stmt = select(data_providers).where(data_providers.c.enabled.is_(True))
    if categories:
        stmt = stmt.where(data_providers.c.category.in_(list(categories)))
    stmt = stmt.order_by(data_providers.c.priority, data_providers.c.code)
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(stmt)]


def record_success(engine: Engine, code: str) -> None:
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(data_providers)
            .where(data_providers.c.code == code)
            .values(
                last_success_at=now,
                consecutive_failures=0,
                last_error=None,
                updated_at=now,
            )
        )


def record_failure(engine: Engine, code: str, error: str) -> None:
    now = utcnow()
    with engine.begin() as conn:
        conn.execute(
            update(data_providers)
            .where(data_providers.c.code == code)
            .values(
                last_error_at=now,
                last_error=error[:2000],
                consecutive_failures=data_providers.c.consecutive_failures + 1,
                updated_at=now,
            )
        )


def providers_health(engine: Engine) -> list[dict]:
    """Rows for GET /internal/providers/health (docs/CONTRACTS.md shape)."""
    stmt = select(data_providers).order_by(data_providers.c.priority, data_providers.c.code)
    out: list[dict] = []
    with engine.connect() as conn:
        for row in conn.execute(stmt):
            m = row._mapping
            last_success = ensure_utc(m["last_success_at"])
            healthy = bool(m["enabled"]) and int(m["consecutive_failures"]) < UNHEALTHY_AFTER_FAILURES
            out.append(
                {
                    "code": m["code"],
                    "name": m["name"],
                    "category": m["category"],
                    "enabled": bool(m["enabled"]),
                    "priority": int(m["priority"]),
                    "healthy": healthy,
                    "last_success_at": last_success.isoformat() if last_success else None,
                    "consecutive_failures": int(m["consecutive_failures"]),
                    "last_error": m["last_error"],
                }
            )
    return out
