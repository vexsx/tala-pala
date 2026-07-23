"""Database access layer.

The Postgres schema is created and migrated by the Go service
(``database/migrations``).  This module only *mirrors* that schema as
SQLAlchemy Core ``Table`` metadata so the Python service can read and write —
it must NEVER create or alter tables in production.  Tests create the tables
from this metadata against an in-memory SQLite database, which is why:

* JSONB columns are declared with the generic :class:`sqlalchemy.JSON` type,
* ``TEXT[]`` (``training_runs.horizons``) uses a SQLite JSON variant,
* ``BIGSERIAL`` primary keys use an ``Integer`` variant on SQLite so that
  autoincrement works.

All timestamps are timezone-aware UTC.  SQLite drops tzinfo on read, so use
:func:`ensure_utc` whenever a timestamp column is read back.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import Connection, Engine

metadata = MetaData()

# --- type helpers -----------------------------------------------------------

def _big_pk() -> Column:
    return Column(
        "id",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )


_NUM = Numeric(asdecimal=False)  # floats everywhere; Postgres NUMERIC on the wire
_TS = DateTime(timezone=True)
_TEXT_ARRAY = ARRAY(Text).with_variant(JSON(), "sqlite")

# --- tables mirroring database/migrations/0001_market_data.up.sql ----------

data_providers = Table(
    "data_providers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", Text, nullable=False, unique=True),
    Column("name", Text, nullable=False),
    Column("base_url", Text, nullable=False, server_default=""),
    Column("category", Text, nullable=False),
    Column("priority", Integer, nullable=False, server_default=text("100")),
    Column("enabled", Boolean, nullable=False, server_default=text("TRUE")),
    Column("last_success_at", _TS),
    Column("last_error_at", _TS),
    Column("last_error", Text),
    Column("consecutive_failures", Integer, nullable=False, server_default=text("0")),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
)

raw_observations = Table(
    "raw_observations",
    metadata,
    _big_pk(),
    Column("provider_code", Text, nullable=False),
    Column("symbol", Text, nullable=False),
    Column("raw_value", _NUM, nullable=False),
    Column("unit", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("raw_payload", JSON, nullable=True),
    Column("observed_at", _TS, nullable=False),
    Column("collected_at", _TS, nullable=False, server_default=func.now()),
    Column("quality", Text, nullable=False, server_default="ok"),
    Column("dedupe_key", Text, nullable=False, unique=True),
    Index("idx_raw_obs_symbol_time", "symbol", "observed_at"),
)

prices = Table(
    "prices",
    metadata,
    _big_pk(),
    Column("symbol", Text, nullable=False),
    Column("value", _NUM, nullable=False),
    Column("currency", Text, nullable=False),
    Column("unit", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("observed_at", _TS, nullable=False),
    Column("collected_at", _TS, nullable=False, server_default=func.now()),
    Column("quality", Text, nullable=False, server_default="ok"),
    CheckConstraint("value > 0 OR currency IN ('INDEX','PCT')", name="prices_positive"),
    UniqueConstraint("symbol", "observed_at", "source", name="prices_unique"),
    Index("idx_prices_symbol_time", "symbol", "observed_at"),
)

feature_snapshots = Table(
    "feature_snapshots",
    metadata,
    _big_pk(),
    Column("symbol", Text, nullable=False, server_default="IR_GOLD_18K"),
    Column("as_of", _TS, nullable=False),
    Column("features", JSON, nullable=False),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    UniqueConstraint("symbol", "as_of", name="feature_snapshots_unique"),
)

# --- tables mirroring database/migrations/0002_models_predictions.up.sql ----

model_versions = Table(
    "model_versions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", Text, nullable=False, server_default="IR_GOLD_18K"),
    Column("horizon", Text, nullable=False),
    Column("model_name", Text, nullable=False),
    Column("version", Text, nullable=False),
    Column("trained_at", _TS, nullable=False, server_default=func.now()),
    Column("training_start", _TS),
    Column("training_end", _TS),
    Column("n_observations", Integer),
    Column("metrics", JSON, nullable=False, default=dict),
    Column("baseline_metrics", JSON, nullable=False, default=dict),
    Column("params", JSON, nullable=False, default=dict),
    Column("artifact_path", Text),
    Column("is_active", Boolean, nullable=False, server_default=text("FALSE")),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    UniqueConstraint("symbol", "horizon", "model_name", "version", name="model_versions_unique"),
)

training_runs = Table(
    "training_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("started_at", _TS, nullable=False, server_default=func.now()),
    Column("finished_at", _TS),
    Column("status", Text, nullable=False, server_default="running"),
    Column("horizons", _TEXT_ARRAY, nullable=False, default=list),
    Column("models_evaluated", JSON, nullable=False, default=list),
    Column("selected", JSON, nullable=False, default=dict),
    Column("error", Text),
    Column("notes", Text),
)

predictions = Table(
    "predictions",
    metadata,
    _big_pk(),
    Column("symbol", Text, nullable=False, server_default="IR_GOLD_18K"),
    Column("horizon", Text, nullable=False),
    Column("model_version_id", Integer, ForeignKey("model_versions.id", ondelete="SET NULL")),
    Column("model_name", Text, nullable=False),
    Column("predicted_at", _TS, nullable=False),
    Column("target_time", _TS, nullable=False),
    Column("point_forecast", _NUM, nullable=False),
    Column("lower_bound", _NUM, nullable=False),
    Column("upper_bound", _NUM, nullable=False),
    Column("expected_change_pct", Float, nullable=False),
    Column("direction", Text, nullable=False),
    Column("confidence", Float, nullable=False),
    # Confidence BEFORE the meta-gate blend (migration 0015): the gate trains
    # on this so its own output never feeds back into its features.
    Column("raw_confidence", Float),
    Column("regime", Text, nullable=False, server_default="unknown"),
    Column("drivers", JSON, nullable=False, default=list),
    Column("data_fresh", Boolean, nullable=False, server_default=text("TRUE")),
    Column("warnings", JSON, nullable=False, default=list),
    Column("actual_value", _NUM),
    Column("actual_recorded_at", _TS),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    Index("idx_predictions_horizon_time", "horizon", "predicted_at"),
)

signals = Table(
    "signals",
    metadata,
    _big_pk(),
    Column("generated_at", _TS, nullable=False, server_default=func.now()),
    Column("signal", Text, nullable=False),
    Column("score", Integer, nullable=False),
    Column("confidence", Float, nullable=False),
    Column("explanation", Text, nullable=False),
    Column("supporting", JSON, nullable=False, default=list),
    Column("conflicting", JSON, nullable=False, default=list),
    Column("risks", JSON, nullable=False, default=list),
    Column("invalidation", Text, nullable=False, server_default=""),
    Column("review_at", _TS),
    Column("data_fresh", Boolean, nullable=False, server_default=text("TRUE")),
    Column("inputs", JSON, nullable=False, default=dict),
)

backtest_runs = Table(
    "backtest_runs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    Column("horizon", Text, nullable=False),
    Column("params", JSON, nullable=False, default=dict),
    Column("period_start", _TS),
    Column("period_end", _TS),
    Column("results", JSON, nullable=False, default=dict),
    Column("status", Text, nullable=False, server_default="succeeded"),
    Column("error", Text),
)

# --- tables mirroring database/migrations/0003_users_portfolio_alerts.up.sql -

# Shared key/value settings store (schema owned by the Go migrations).  Python
# only reads/writes well-namespaced keys such as 'live_calibration'
# (jobs/evaluate.py); the Go-seeded keys are left untouched.
app_settings = Table(
    "app_settings",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", JSON, nullable=False),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
)

# --- tables mirroring database/migrations/0008_app_issues.up.sql ------------

# Central issue log shared by all services (Issues tab). Written by the
# logging bridge in app/core/issues.py; read/served by the Go API.
app_issues = Table(
    "app_issues",
    metadata,
    _big_pk(),
    Column("occurred_at", _TS, nullable=False, server_default=func.now()),
    Column("service", Text, nullable=False),
    Column("level", Text, nullable=False),
    Column("source", Text, nullable=False, server_default=""),
    Column("message", Text, nullable=False),
    Column("details", JSON, nullable=False, default=dict),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
)

# --- helpers ----------------------------------------------------------------


def create_db_engine(database_url: str) -> Engine:
    """Create the SQLAlchemy engine (2.x future style)."""
    kwargs: dict[str, Any] = {"pool_pre_ping": True}
    if database_url.startswith("sqlite"):
        from sqlalchemy.pool import StaticPool

        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    return create_engine(database_url, **kwargs)


def utcnow() -> datetime:
    """Timezone-aware current UTC time.  Never use naive datetimes."""
    return datetime.now(timezone.utc)


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Attach/convert to UTC.  SQLite returns naive datetimes; treat them as UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def insert_ignore(conn: Connection, table: Table, rows: Iterable[dict]) -> int:
    """INSERT ... ON CONFLICT DO NOTHING, one row at a time (volumes are small).

    Returns the number of rows actually inserted.  Works on both PostgreSQL and
    SQLite; other dialects fall back to try/except inserts.
    """
    dialect = conn.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - not used in this project
        dialect_insert = None

    inserted = 0
    for row in rows:
        if dialect_insert is not None:
            # RETURNING gives an exact inserted-or-skipped answer on both
            # PostgreSQL and SQLite; rowcount is unreliable here (psycopg
            # reports -1 for ON CONFLICT DO NOTHING).
            stmt = (
                dialect_insert(table)
                .values(**row)
                .on_conflict_do_nothing()
                .returning(*table.primary_key.columns)
            )
            if conn.execute(stmt).first() is not None:
                inserted += 1
        else:  # pragma: no cover
            try:
                conn.execute(table.insert().values(**row))
                inserted += 1
            except Exception:
                pass
    return inserted


def db_ok(engine: Engine) -> bool:
    """Cheap connectivity probe used by /internal/health."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
