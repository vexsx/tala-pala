"""Maintenance job: prune old raw observations and old issue-log rows."""
from __future__ import annotations

import time
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import app_issues, raw_observations, utcnow
from ..metrics import JOB_LAST_SUCCESS

ISSUE_RETENTION_DAYS = 30  # the Issues tab is a debugging aid, not an archive


def run_cleanup(engine: Engine, settings: Settings) -> dict:
    cutoff = utcnow() - timedelta(days=settings.raw_retention_days)
    issue_cutoff = utcnow() - timedelta(days=ISSUE_RETENTION_DAYS)
    with engine.begin() as conn:
        result = conn.execute(
            delete(raw_observations).where(raw_observations.c.collected_at < cutoff)
        )
        issues_result = conn.execute(
            delete(app_issues).where(app_issues.c.occurred_at < issue_cutoff)
        )
    JOB_LAST_SUCCESS.labels(job="cleanup").set(time.time())
    return {
        "deleted_raw_observations": int(result.rowcount or 0),
        "deleted_app_issues": int(issues_result.rowcount or 0),
        "cutoff": cutoff.isoformat(),
        "retention_days": settings.raw_retention_days,
    }
