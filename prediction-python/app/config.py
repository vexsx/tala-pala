"""Runtime configuration.

Values come from environment variables per the repository-level ``.env.example``
contract.  ``POSTGRES_PASSWORD`` and ``INTERNAL_API_TOKEN`` additionally support
``*_FILE`` variants (Docker secrets); when the ``*_FILE`` variable is set the
file content takes precedence over the plain variable.

No secrets are ever hardcoded here.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from urllib.parse import quote

log = logging.getLogger(__name__)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _secret(name: str, default: str = "") -> str:
    """Resolve a secret honoring the ``NAME_FILE`` Docker-secret convention."""
    file_path = os.environ.get(f"{name}_FILE", "").strip()
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:  # fall back to the plain env variable
            log.warning("could not read %s_FILE=%s: %s", name, file_path, exc)
    return _env(name, default)


def _database_url() -> str:
    explicit = _env("DATABASE_URL")
    if explicit:
        return explicit
    user = _env("POSTGRES_USER", "goldpred")
    password = _secret("POSTGRES_PASSWORD", "")
    host = _env("POSTGRES_HOST", "localhost")
    port = _env("POSTGRES_PORT", "5432")
    db = _env("POSTGRES_DB", "goldpred")
    return (
        f"postgresql+psycopg://{quote(user, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}/{db}"
    )


@dataclass
class Settings:
    """Immutable-ish bag of runtime settings (constructed once per process)."""

    database_url: str = field(default_factory=_database_url)
    internal_api_token: str = field(default_factory=lambda: _secret("INTERNAL_API_TOKEN", ""))
    prediction_port: int = field(default_factory=lambda: int(_env("PREDICTION_PORT", "8500")))
    models_dir: str = field(default_factory=lambda: _env("MODELS_DIR", "/app/models"))
    http_timeout_seconds: float = field(
        default_factory=lambda: float(_env("HTTP_TIMEOUT_SECONDS", "15"))
    )
    raw_retention_days: int = field(default_factory=lambda: int(_env("RAW_RETENTION_DAYS", "365")))
    # Bounded retention (Addendum 17). Conservative defaults: these tables are
    # small relative to raw_observations, and several feed live calibration —
    # app/core/retention.py additionally floors deletions at the windows the
    # self-learning loops actually read, whatever these are set to.
    prediction_retention_days: int = field(
        default_factory=lambda: int(_env("PREDICTION_RETENTION_DAYS", "730")))
    signal_retention_days: int = field(
        default_factory=lambda: int(_env("SIGNAL_RETENTION_DAYS", "365")))
    training_run_keep: int = field(
        default_factory=lambda: int(_env("TRAINING_RUN_KEEP", "200")))
    model_version_retention_days: int = field(
        default_factory=lambda: int(_env("MODEL_VERSION_RETENTION_DAYS", "180")))
    alert_event_retention_days: int = field(
        default_factory=lambda: int(_env("ALERT_EVENT_RETENTION_DAYS", "180")))
    audit_retention_days: int = field(
        default_factory=lambda: int(_env("AUDIT_RETENTION_DAYS", "730")))
    # News/intelligence flags (Addendum 18). Four INDEPENDENT switches so the
    # subsystem can be observed without being trusted: collection can run while
    # the UI is hidden, or the UI can show history while collection is paused.
    # NEWS_ML_ENABLED gates the only one that can change a forecast, and it
    # stays false until chronological evidence justifies otherwise.
    news_collection_enabled: bool = field(
        default_factory=lambda: _env("NEWS_COLLECTION_ENABLED", "false").lower() == "true")
    news_api_enabled: bool = field(
        default_factory=lambda: _env("NEWS_API_ENABLED", "true").lower() == "true")
    news_ui_enabled: bool = field(
        default_factory=lambda: _env("NEWS_UI_ENABLED", "true").lower() == "true")
    news_ml_enabled: bool = field(
        default_factory=lambda: _env("NEWS_ML_ENABLED", "false").lower() == "true")
    news_llm_enabled: bool = field(
        default_factory=lambda: _env("NEWS_LLM_ENABLED", "false").lower() == "true")
    news_retention_days: int = field(
        default_factory=lambda: int(_env("NEWS_RETENTION_DAYS", "540")))
    gdelt_min_interval_seconds: float = field(
        default_factory=lambda: float(_env("GDELT_MIN_INTERVAL_SECONDS", "5")))
    stale_minutes: int = field(default_factory=lambda: int(_env("STALE_MINUTES", "30")))
    navasan_api_key: str = field(default_factory=lambda: _env("NAVASAN_API_KEY", ""))
    metals_dev_api_key: str = field(default_factory=lambda: _env("METALS_DEV_API_KEY", ""))
    brsapi_api_key: str = field(default_factory=lambda: _env("BRSAPI_KEY", ""))
    # Tehran-exchange gold funds: "ticker:SYMBOL,..." (empty -> provider defaults)
    tsetmc_funds: str = field(default_factory=lambda: _env("TSETMC_FUNDS", ""))
    # Fixed Tehran-local fetch slots for the TSE funds job ("HH:MM,HH:MM,...").
    # 3 slots x 2 funds = 6 requests/day against the ~10/day free-tier quota;
    # 18:00 is deliberately post-close to capture the settled closing data.
    tsetmc_fetch_times: str = field(
        default_factory=lambda: _env("TSETMC_FETCH_TIMES", "12:00,15:00,18:00")
    )
    alanchand_token: str = field(default_factory=lambda: _env("ALANCHAND_TOKEN", ""))
    # Tehran market hours (Addendum 1): Sat-Wed open window, Asia/Tehran local,
    # "HH:MM" strings; Thursday and Friday are always closed for Iranian
    # symbols. IR_GOLD_18K trades 24h on trading days and ignores this window.
    market_tehran_open: str = field(default_factory=lambda: _env("MARKET_TEHRAN_OPEN", "12:00"))
    market_tehran_close: str = field(default_factory=lambda: _env("MARKET_TEHRAN_CLOSE", "20:00"))
    # TSE gold-fund session (Addendum 7): Sat-Wed, Asia/Tehran local.
    market_tse_open: str = field(default_factory=lambda: _env("MARKET_TSE_OPEN", "12:00"))
    market_tse_close: str = field(default_factory=lambda: _env("MARKET_TSE_CLOSE", "18:00"))
    # Courtesy delay between outbound provider requests (seconds); 0 in tests.
    provider_courtesy_delay: float = field(
        default_factory=lambda: float(_env("PROVIDER_COURTESY_DELAY", "1.0"))
    )
    # Base of the exponential retry backoff (seconds); 0 in tests.
    provider_backoff_base: float = field(
        default_factory=lambda: float(_env("PROVIDER_BACKOFF_BASE", "0.75"))
    )
    # Multi-timeframe trend alignment (Addendum 20). A TECHNICAL INDICATOR
    # ONLY: these periods never reach model input, model selection, confidence,
    # intervals or the buy/sell policy — they only decide when the 1D/4H/1H
    # stacks are called aligned. On by default because it reads existing price
    # history and writes only its own two tables.
    trend_alignment_enabled: bool = field(
        default_factory=lambda: _env("TREND_ALIGNMENT_ENABLED", "true").lower() == "true")
    trend_alignment_ma_type: str = field(
        default_factory=lambda: _env("TREND_ALIGNMENT_MA_TYPE", "ema").strip().lower())
    trend_alignment_fast_period: int = field(
        default_factory=lambda: int(_env("TREND_ALIGNMENT_FAST_PERIOD", "26")))
    trend_alignment_mid_period: int = field(
        default_factory=lambda: int(_env("TREND_ALIGNMENT_MID_PERIOD", "48")))
    trend_alignment_slow_period: int = field(
        default_factory=lambda: int(_env("TREND_ALIGNMENT_SLOW_PERIOD", "220")))

    def __post_init__(self) -> None:
        """Reject a trend-alignment configuration that cannot mean anything.

        ``fast < mid < slow`` is not a preference, it is what makes the stack
        test a trend test: with the periods swapped, ``price > ma26 > ma48 >
        ma220`` would be comparing a slow average against a fast one and the
        resulting "alignment" would be noise wearing the label of a signal.
        Failing here (process start) rather than at the first evaluation means
        a typo in the environment is visible immediately instead of turning
        into a silently wrong indicator hours later.

        The rule itself lives in :meth:`TrendConfig.validate` so the job, the
        engine and this validation can never drift apart; the import is local
        to keep ``config`` free of module-level dependencies on ``app``.
        """
        from .models.trend_alignment import TrendConfig

        TrendConfig(
            enabled=self.trend_alignment_enabled,
            ma_type=self.trend_alignment_ma_type,  # type: ignore[arg-type]
            fast=self.trend_alignment_fast_period,
            mid=self.trend_alignment_mid_period,
            slow=self.trend_alignment_slow_period,
        ).validate()


def get_settings() -> Settings:
    """Build settings from the current environment (cheap; no caching so tests stay isolated)."""
    return Settings()
