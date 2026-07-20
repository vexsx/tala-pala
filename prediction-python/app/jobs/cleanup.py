"""Maintenance job: prune old raw observations per RAW_RETENTION_DAYS."""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import raw_observations, utcnow
from ..metrics import JOB_LAST_SUCCESS


def run_cleanup(engine: Engine, settings: Settings) -> dict:
    cutoff = utcnow() - timedelta(days=settings.raw_retention_days)
    with engine.begin() as conn:
        result = conn.execute(
            delete(raw_observations).where(raw_observations.c.collected_at < cutoff)
        )
    JOB_LAST_SUCCESS.labels(job="cleanup").set(time.time())
    return {
        "deleted_raw_observations": int(result.rowcount or 0),
        "cutoff": cutoff.isoformat(),
        "retention_days": settings.raw_retention_days,
    }
