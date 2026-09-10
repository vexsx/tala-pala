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
    Date,
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
    # Migration 0026.  Note the absence of a ``server_default`` — the other
    # three symbol columns in this file carry ``server_default="IR_GOLD_18K"``
    # and 0026 exists precisely to stop that pattern spreading to the table
    # that publishes buy/sell calls.  A writer that forgets the symbol must
    # fail its INSERT, not quietly file its row under gold.
    Column("symbol", Text, nullable=False),
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
    # Every read is now "the latest row(s) for THIS symbol"; 0002's
    # idx_signals_time (generated_at DESC) leads on the wrong column for all
    # of them.
    Index("idx_signals_symbol_time", "symbol", text("generated_at DESC")),
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

# --- tables mirroring database/migrations/0019_trend_alignment.up.sql -------

# Multi-timeframe trend alignment (Addendum 20).  A technical indicator only:
# nothing here feeds model input, model selection, confidence, intervals or the
# buy/sell policy.  ``trend_alignment_states`` is the current per-symbol
# conclusion; ``trend_alignment_events`` is the append-only log of ENTRIES into
# a full alignment.
trend_alignment_states = Table(
    "trend_alignment_states",
    metadata,
    Column("symbol", Text, primary_key=True),
    Column("alignment", Text, nullable=False),
    Column("previous_alignment", Text),
    Column("timeframes", JSON, nullable=False, default=dict),
    Column("ma_type", Text, nullable=False, server_default="ema"),
    Column("fast_period", Integer, nullable=False, server_default=text("26")),
    Column("mid_period", Integer, nullable=False, server_default=text("48")),
    Column("slow_period", Integer, nullable=False, server_default=text("220")),
    Column("data_fresh", Boolean, nullable=False, server_default=text("FALSE")),
    Column("latest_1h_candle_close", _TS),
    Column("latest_4h_candle_close", _TS),
    Column("latest_1d_candle_close", _TS),
    Column("last_bullish_alert_at", _TS),
    Column("last_bearish_alert_at", _TS),
    Column("state_version", Integer, nullable=False, server_default=text("1")),
    Column("calculated_at", _TS, nullable=False, server_default=func.now()),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint("alignment IN ('full_bullish', 'full_bearish', 'not_aligned')"),
    CheckConstraint(
        "previous_alignment IS NULL OR previous_alignment IN "
        "('full_bullish', 'full_bearish', 'not_aligned')"
    ),
)

trend_alignment_events = Table(
    "trend_alignment_events",
    metadata,
    _big_pk(),
    Column("symbol", Text, nullable=False),
    Column("alignment", Text, nullable=False),
    Column("previous_alignment", Text),
    Column("occurred_at", _TS, nullable=False, server_default=func.now()),
    Column("latest_1h_candle_close", _TS, nullable=False),
    Column("latest_4h_candle_close", _TS, nullable=False),
    Column("latest_1d_candle_close", _TS, nullable=False),
    Column("timeframes", JSON, nullable=False, default=dict),
    Column("ma_type", Text, nullable=False, server_default="ema"),
    Column("alert_event_id", BigInteger().with_variant(Integer, "sqlite")),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint("alignment IN ('full_bullish', 'full_bearish')"),
    # Mirrored by name because it is inferred as the ON CONFLICT target: the
    # duplicate guard is the database's, not the evaluator's (a restart must
    # not re-fire an alert the user already saw).
    Index(
        "uq_trend_alignment_event_identity",
        "symbol",
        "alignment",
        "latest_1d_candle_close",
        "latest_4h_candle_close",
        "latest_1h_candle_close",
        unique=True,
    ),
    Index("idx_trend_alignment_events_symbol_time", "symbol", "occurred_at"),
)

# --- table mirroring database/migrations/0021_trend_alignment_performance ----

# The measured track record of the same indicator, one row per (symbol,
# window).  ``basis`` says which alignment was actually replayed — the real
# 1D+4H+1H one, or the 1D leg alone where intraday history does not reach —
# and the statistic columns are NULLABLE because "never happened" and
# "happened and returned nothing" are different facts.  The counts stay NOT
# NULL: a count of zero IS the measurement.
trend_alignment_performance = Table(
    "trend_alignment_performance",
    metadata,
    Column("symbol", Text, primary_key=True),
    Column("window_days", Integer, primary_key=True),
    Column("basis", Text, nullable=False),
    Column("computed_at", _TS, nullable=False, server_default=func.now()),
    Column("evaluated_from", _TS),
    Column("evaluated_to", _TS),
    Column("samples", Integer, nullable=False, server_default=text("0")),
    Column("bullish_episodes", Integer, nullable=False, server_default=text("0")),
    Column("bearish_episodes", Integer, nullable=False, server_default=text("0")),
    Column("bullish_bars", Integer, nullable=False, server_default=text("0")),
    Column("bearish_bars", Integer, nullable=False, server_default=text("0")),
    Column("unaligned_bars", Integer, nullable=False, server_default=text("0")),
    Column("fwd_return_bullish_pct", Float),
    Column("fwd_return_bearish_pct", Float),
    Column("fwd_return_baseline_pct", Float),
    Column("hit_rate_bullish", Float),
    Column("hit_rate_bearish", Float),
    Column("note", Text, nullable=False, server_default=""),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint("basis IN ('full_mtf', 'daily_only')"),
    Index("idx_trend_alignment_performance_symbol_window", "symbol", "window_days"),
)

# --- tables mirroring database/migrations/0024_instruments_economic_series --

# The instrument vocabulary every other reader projects.  0024 seeds it with
# exactly the market symbols that exist today; ``app/economic/catalog.py``
# adds the ``kind='economic_series'`` rows.  There is deliberately no foreign
# key from ``prices.symbol`` yet (see the migration's own note), so nothing
# here can turn an unregistered provider symbol into a collect failure.
instruments = Table(
    "instruments",
    metadata,
    Column("code", Text, primary_key=True),
    Column("kind", Text, nullable=False),
    Column("name_en", Text, nullable=False),
    Column("name_fa", Text, nullable=False, server_default=""),
    Column("domain", Text, nullable=False),
    Column("quote_currency", Text, nullable=False),
    Column("unit", Text, nullable=False),
    Column("decimals", Integer, nullable=False, server_default=text("0")),
    Column("calendar_class", Text, nullable=False, server_default="always_open"),
    Column("quality_tier", Text, nullable=False, server_default="official"),
    Column("is_proxy", Boolean, nullable=False, server_default=text("FALSE")),
    Column("is_derived", Boolean, nullable=False, server_default=text("FALSE")),
    Column("enabled", Boolean, nullable=False, server_default=text("TRUE")),
    Column("notes", Text, nullable=False, server_default=""),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint(
        "kind IN ('market_price','economic_series','equity','index','basket','fx')"
    ),
    CheckConstraint(
        "calendar_class IN ('always_open','tehran_bazaar','tse_session','global','none')"
    ),
    CheckConstraint(
        "quality_tier IN ('official','official_mirror','commercial','proxy',"
        "'estimate','experimental')"
    ),
    # Partial in Postgres (``WHERE enabled``).  The predicate is dialect-scoped
    # so the SQLite mirror the tests build still gets the index, unfiltered.
    Index(
        "idx_instruments_kind_domain",
        "kind",
        "domain",
        postgresql_where=text("enabled"),
    ),
)

# The artifact a value was read from.  Same content re-fetched from the same
# provider is the same document, which is what the unique constraint says.
source_documents = Table(
    "source_documents",
    metadata,
    _big_pk(),
    Column("provider_code", Text, nullable=False),
    Column("url", Text, nullable=False),
    Column("title", Text, nullable=False, server_default=""),
    Column("media_type", Text, nullable=False, server_default=""),
    Column("byte_size", Integer, nullable=False, server_default=text("0")),
    Column("content_sha256", Text, nullable=False),
    Column("storage_path", Text, nullable=False, server_default=""),
    Column("fetched_at", _TS, nullable=False, server_default=func.now()),
    UniqueConstraint("provider_code", "content_sha256", name="source_documents_unique"),
    Index("idx_source_documents_fetched", text("fetched_at DESC")),
)

economic_series = Table(
    "economic_series",
    metadata,
    _big_pk(),
    Column(
        "code",
        Text,
        ForeignKey("instruments.code", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    ),
    Column("frequency", Text, nullable=False),
    Column("calendar", Text, nullable=False, server_default="gregorian"),
    Column("measure", Text, nullable=False),
    Column("seasonal_adjustment", Text, nullable=False, server_default="nsa"),
    Column("base_period", Text, nullable=False, server_default=""),
    Column("provider_code", Text, nullable=False),
    Column("provider_series_id", Text, nullable=False, server_default=""),
    # NULL means "not characterised", which is a different fact from zero.
    Column("publication_lag_days", Integer),
    Column("revisable", Boolean, nullable=False, server_default=text("TRUE")),
    Column("splice_policy", Text, nullable=False, server_default="none"),
    Column("enabled", Boolean, nullable=False, server_default=text("TRUE")),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint("frequency IN ('D','W','M','Q','A')"),
    CheckConstraint("calendar IN ('gregorian','jalali')"),
    CheckConstraint("measure IN ('index','level','yoy_pct','mom_pct','ratio','rate')"),
    CheckConstraint("seasonal_adjustment IN ('nsa','sa','unknown')"),
    CheckConstraint("splice_policy IN ('none','chain_growth')"),
    Index(
        "idx_economic_series_provider",
        "provider_code",
        postgresql_where=text("enabled"),
    ),
)

# Bitemporal, append-only.  A revision inserts a row with vintage+1; nothing is
# ever updated in place, so the first print stays readable forever.  Every
# point-in-time read filters on ``available_at`` and nothing else — the same
# rule migration 0017 states for news_articles.
economic_observations = Table(
    "economic_observations",
    metadata,
    _big_pk(),
    Column(
        "series_id",
        BigInteger().with_variant(Integer, "sqlite"),
        ForeignKey("economic_series.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("ref_period_start", Date, nullable=False),
    Column("ref_period_end", Date, nullable=False),
    Column("ref_period_label", Text, nullable=False, server_default=""),
    Column("value", _NUM, nullable=False),
    # NULL when the source does not state a publication moment, which is the
    # normal case for both P0 providers.
    Column("published_at", _TS),
    Column("available_at", _TS, nullable=False),
    Column("vintage", Integer, nullable=False, server_default=text("1")),
    Column("is_nowcast", Boolean, nullable=False, server_default=text("FALSE")),
    Column("is_projection", Boolean, nullable=False, server_default=text("FALSE")),
    Column(
        "source_document_id",
        BigInteger().with_variant(Integer, "sqlite"),
        # RESTRICT, matching migration 0024. SET NULL would let a delete on
        # source_documents mutate a stored observation after insert — the one
        # append-only violation this table is written to forbid — and a mirror
        # that encodes the opposite rule means no test could ever catch the
        # regression.
        ForeignKey("source_documents.id", ondelete="RESTRICT"),
    ),
    Column("collected_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint("vintage >= 1"),
    UniqueConstraint(
        "series_id", "ref_period_start", "vintage", name="economic_obs_unique"
    ),
    CheckConstraint(
        "ref_period_end >= ref_period_start", name="economic_obs_period"
    ),
    # Mirrored with the same column order and direction as the migration: the
    # point-in-time read in app/economic/store.py is written to match it.
    Index(
        "idx_econ_obs_pit",
        "series_id",
        text("ref_period_start DESC"),
        text("available_at DESC"),
        text("vintage DESC"),
    ),
    Index("idx_econ_obs_available", text("available_at DESC")),
)

# --- tables mirroring database/migrations/0028_equity_bars.up.sql -----------
#
# Tehran equity daily bars.  Deliberately NOT economic_observations: a bar is
# five prices, two counts and a turnover for one SESSION, TSETMC does not
# revise it, and it has no publication lag to model.  0028's header states the
# full reasoning, including why this registry is separate from ``instruments``.

equity_instruments = Table(
    "equity_instruments",
    metadata,
    # TEXT, not an integer: TSETMC serves the same insCode as a JSON string on
    # one endpoint and a JSON number on another, and it is a name, not a
    # quantity.
    Column("ins_code", Text, primary_key=True),
    # Folded to Persian orthography with ZWNJ removed — the lookup key.
    Column("symbol_fa", Text, nullable=False),
    # Folded for the same confusables but with ZWNJ KEPT: there it separates
    # words, and stripping it glues معدنی‌وصنعتی‌چادرملو into one token.
    Column("name_fa", Text, nullable=False),
    Column("market", Text, nullable=False),
    Column("board", Text, nullable=False, server_default=""),
    Column("sector_code", Text, nullable=False, server_default=""),
    Column("sector_fa", Text, nullable=False, server_default=""),
    Column("isin", Text, nullable=False, server_default=""),
    # The optional bridge into the modelled-instrument vocabulary.  NULL for
    # every seeded row: listed is not the same as modelled.
    Column(
        "instrument_code",
        Text,
        ForeignKey("instruments.code", ondelete="RESTRICT"),
    ),
    # Coverage, maintained by the ingest.  NULL until the first bar lands.
    Column("first_bar", Date),
    Column("last_bar", Date),
    Column("bar_count", Integer, nullable=False, server_default=text("0")),
    Column("enabled", Boolean, nullable=False, server_default=text("TRUE")),
    Column("notes", Text, nullable=False, server_default=""),
    Column("created_at", _TS, nullable=False, server_default=func.now()),
    Column("updated_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint("market IN ('bourse','farabourse')"),
    UniqueConstraint("symbol_fa", name="equity_instruments_symbol_unique"),
    Index(
        "idx_equity_instruments_enabled", "symbol_fa", postgresql_where=text("enabled")
    ),
    Index("idx_equity_instruments_sector", "sector_code"),
)

# RAW ONLY.  Every column is exactly what TSETMC served for that session; an
# adjusted number is never written here (tests/test_equity_adjust.py asserts
# it).  The adjustment is a factor on ``corporate_actions``, applied at read.
equity_bars = Table(
    "equity_bars",
    metadata,
    _big_pk(),
    Column(
        "ins_code",
        Text,
        ForeignKey("equity_instruments.ins_code", ondelete="CASCADE"),
        nullable=False,
    ),
    # TSETMC's dEven: an integer GREGORIAN date, despite everything else on
    # that site being Jalali.
    Column("trade_date", Date, nullable=False),
    # Zero on a halted session, where zero means "no trade occurred".
    Column("open", _NUM, nullable=False),
    Column("high", _NUM, nullable=False),
    Column("low", _NUM, nullable=False),
    # pDrCotVal, the last trade.  NOT always inside [low, high]: 9,530 of the
    # 77,344 measured bars fall outside it -- every halted bar (low=high=0,
    # pDrCotVal carries yesterday forward) plus 10 genuinely traded ones.
    Column("close", _NUM, nullable=False),
    # pClosing, TSE's قیمت پایانی.  The basis every return and every corporate
    # action is computed on, and NOT bounded by [low, high] — 285 of فولاد's
    # 4,221 traded bars sit outside it, by up to 3.9%.
    Column("final_close", _NUM, nullable=False),
    # priceYesterday.  Its disagreement with the previous session's
    # final_close IS the corporate action.  Zero only on an instrument's first
    # bar, where there is no yesterday.
    Column("price_yesterday", _NUM, nullable=False),
    Column("volume", BigInteger, nullable=False),
    Column("trade_count", BigInteger, nullable=False),
    Column("value", _NUM, nullable=False),
    Column("collected_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint("final_close > 0"),
    CheckConstraint("price_yesterday >= 0"),
    CheckConstraint("volume >= 0"),
    CheckConstraint("trade_count >= 0"),
    CheckConstraint("value >= 0"),
    CheckConstraint("low <= high", name="equity_bars_band"),
    CheckConstraint(
        "open >= 0 AND high >= 0 AND low >= 0 AND close >= 0",
        name="equity_bars_nonneg",
    ),
    UniqueConstraint("ins_code", "trade_date", name="equity_bars_unique"),
    Index("idx_equity_bars_read", "ins_code", text("trade_date DESC")),
)

# One detected action, kept with BOTH numbers that imply it so the ratio is
# auditable rather than asserted.
corporate_actions = Table(
    "corporate_actions",
    metadata,
    _big_pk(),
    Column(
        "ins_code",
        Text,
        ForeignKey("equity_instruments.ins_code", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("effective_date", Date, nullable=False),
    Column("prev_trade_date", Date, nullable=False),
    Column("prev_close", _NUM, nullable=False),
    Column("price_yesterday", _NUM, nullable=False),
    # Which of TSETMC's two signals this came from.  'reference_restated' is
    # explained by (prev_close, price_yesterday); 'close_restated' — a halted
    # bar whose own close was restated — by (price_yesterday, restated_close),
    # which is why that column exists rather than 32 rows carrying a ratio
    # nothing on the row can check.
    Column("kind", Text, nullable=False, server_default="reference_restated"),
    Column("restated_close", _NUM),
    Column("ratio", _NUM, nullable=False),
    # The product of this ratio and every LATER one.  A back-adjusted close is
    # final_close * cumulative_factor(first action after that bar), else 1.0.
    Column("cumulative_factor", _NUM, nullable=False),
    Column("adjustment_version", Text, nullable=False),
    Column("detected_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint("prev_close > 0"),
    CheckConstraint("price_yesterday > 0"),
    CheckConstraint("ratio > 0"),
    CheckConstraint("cumulative_factor > 0"),
    CheckConstraint("kind IN ('reference_restated','close_restated')"),
    CheckConstraint("restated_close IS NULL OR restated_close > 0"),
    CheckConstraint("prev_trade_date < effective_date", name="corporate_actions_order"),
    CheckConstraint(
        "(kind = 'reference_restated' AND restated_close IS NULL)"
        " OR (kind = 'close_restated' AND restated_close IS NOT NULL)",
        name="corporate_actions_evidence",
    ),
    UniqueConstraint(
        "ins_code",
        "effective_date",
        "adjustment_version",
        name="corporate_actions_unique",
    ),
    Index(
        "idx_corporate_actions_read",
        "ins_code",
        "adjustment_version",
        "effective_date",
    ),
)

# The gate's verdict, per symbol per version.  REDESIGN's P3 gate — "adjustment
# must be validated before any return, ratio or score is computed from it" — is
# enforceable only because this is a ROW: the read API refuses to serve an
# adjusted series whose verdict here is not 'validated'.
equity_adjustments = Table(
    "equity_adjustments",
    metadata,
    _big_pk(),
    Column(
        "ins_code",
        Text,
        ForeignKey("equity_instruments.ins_code", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("adjustment_version", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("actions_applied", Integer, nullable=False, server_default=text("0")),
    Column("bars_total", Integer, nullable=False, server_default=text("0")),
    Column("pre_listing_bars", Integer, nullable=False, server_default=text("0")),
    Column("sessions_checked", Integer, nullable=False, server_default=text("0")),
    Column("worst_return", _NUM),
    Column("worst_return_date", Date),
    Column("reopenings", Integer, nullable=False, server_default=text("0")),
    # Both bounds the verdict was reached under, so a row can be re-checked
    # against the constants that produced it after they change.
    Column("max_session_return", _NUM, nullable=False),
    Column("max_reopening_return", _NUM, nullable=False, server_default=text("3.0")),
    Column("session_gap_days", Integer, nullable=False),
    Column("first_bar", Date),
    Column("last_bar", Date),
    Column("refusal_reason", Text, nullable=False, server_default=""),
    Column("computed_at", _TS, nullable=False, server_default=func.now()),
    CheckConstraint("status IN ('validated','refused')"),
    # A refusal must say why: an empty reason leaves an operator nothing to act
    # on and hides a bug in the writer behind a well-formed row.
    CheckConstraint(
        "status <> 'refused' OR length(refusal_reason) > 0",
        name="equity_adjustments_reason",
    ),
    UniqueConstraint(
        "ins_code", "adjustment_version", name="equity_adjustments_unique"
    ),
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
