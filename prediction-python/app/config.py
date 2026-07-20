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
    stale_minutes: int = field(default_factory=lambda: int(_env("STALE_MINUTES", "30")))
    navasan_api_key: str = field(default_factory=lambda: _env("NAVASAN_API_KEY", ""))
    metals_dev_api_key: str = field(default_factory=lambda: _env("METALS_DEV_API_KEY", ""))
    brsapi_api_key: str = field(default_factory=lambda: _env("BRSAPI_KEY", ""))
    # Courtesy delay between outbound provider requests (seconds); 0 in tests.
    provider_courtesy_delay: float = field(
        default_factory=lambda: float(_env("PROVIDER_COURTESY_DELAY", "1.0"))
    )
    # Base of the exponential retry backoff (seconds); 0 in tests.
    provider_backoff_base: float = field(
        default_factory=lambda: float(_env("PROVIDER_BACKOFF_BASE", "0.75"))
    )


def get_settings() -> Settings:
    """Build settings from the current environment (cheap; no caching so tests stay isolated)."""
    return Settings()
