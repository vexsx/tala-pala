"""Maintenance job: prune old raw observations, old issue-log rows, and
superseded model artifacts (every training run writes a fresh .joblib per
symbol×horizon; without pruning the models volume grows unboundedly)."""
from __future__ import annotations

import logging
import os
import time
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import app_issues, model_versions, raw_observations, utcnow
from ..metrics import JOB_LAST_SUCCESS

log = logging.getLogger(__name__)

ISSUE_RETENTION_DAYS = 30     # the Issues tab is a debugging aid, not an archive
ARTIFACT_RETENTION_DAYS = 14  # superseded artifacts kept this long for rollback


def prune_model_artifacts(engine: Engine, settings: Settings) -> int:
    """Delete .joblib artifacts that are not referenced by any ACTIVE model
    version and are older than ARTIFACT_RETENTION_DAYS (by file mtime).

    Deliberately conservative: only files ending in .joblib inside
    ``settings.models_dir`` are candidates, referenced paths are always kept
    regardless of age, and any single unlink failure is logged and skipped.
    """
    root = settings.models_dir
    if not root or not os.path.isdir(root):
        return 0
    with engine.connect() as conn:
        keep = {
            os.path.realpath(str(path))
            for (path,) in conn.execute(
                select(model_versions.c.artifact_path).where(
                    model_versions.c.is_active,
                    model_versions.c.artifact_path.is_not(None),
                )
            )
        }
    cutoff_ts = time.time() - ARTIFACT_RETENTION_DAYS * 86400
    removed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(".joblib"):
                continue
            path = os.path.join(dirpath, name)
            if os.path.realpath(path) in keep:
                continue
            try:
                if os.path.getmtime(path) < cutoff_ts:
                    os.unlink(path)
                    removed += 1
            except OSError as exc:
                log.warning("artifact prune failed for %s: %s", path, exc)
    return removed


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
    pruned_artifacts = prune_model_artifacts(engine, settings)
    JOB_LAST_SUCCESS.labels(job="cleanup").set(time.time())
    return {
        "deleted_raw_observations": int(result.rowcount or 0),
        "deleted_app_issues": int(issues_result.rowcount or 0),
        "pruned_model_artifacts": pruned_artifacts,
        "cutoff": cutoff.isoformat(),
        "retention_days": settings.raw_retention_days,
    }
