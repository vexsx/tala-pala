"""Feature-snapshot job: point-in-time features for IR_GOLD_18K.

Reads ``prices``, computes the feature vector strictly as of *now* (leakage
guard enforced inside :mod:`app.features.engineering`), and upserts one row
into ``feature_snapshots``.
"""
from __future__ import annotations

import time

import pandas as pd
from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import feature_snapshots, insert_ignore, prices, utcnow
from ..features.engineering import build_snapshot
from ..metrics import JOB_LAST_SUCCESS

FEATURE_SYMBOLS = ("IR_GOLD_18K", "USD_IRT", "XAUUSD")


def run_generate_features(engine: Engine, settings: Settings) -> dict:
    as_of = utcnow()
    stmt = (
        select(prices.c.symbol, prices.c.observed_at, prices.c.value)
        .where(
            prices.c.symbol.in_(FEATURE_SYMBOLS),
            prices.c.quality == "ok",
            prices.c.observed_at <= as_of,
        )
        .order_by(prices.c.observed_at)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()
    if not rows:
        return {"generated": 0, "as_of": as_of.isoformat(), "reason": "no price data"}

    df = pd.DataFrame(rows, columns=["symbol", "observed_at", "value"])
    features = build_snapshot(df, as_of, symbol="IR_GOLD_18K")
    if features is None:
        return {"generated": 0, "as_of": as_of.isoformat(), "reason": "no gold series"}

    with engine.begin() as conn:
        inserted = insert_ignore(
            conn,
            feature_snapshots,
            [
                {
                    "symbol": "IR_GOLD_18K",
                    "as_of": as_of,
                    "features": features,
                    "created_at": as_of,
                }
            ],
        )
    JOB_LAST_SUCCESS.labels(job="features").set(time.time())
    return {"generated": int(inserted), "as_of": as_of.isoformat(),
            "n_features": len(features)}
