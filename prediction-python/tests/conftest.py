"""Shared fixtures: in-memory SQLite engine (tables from SQLAlchemy metadata),
settings, FastAPI test client, and fixture-file loading helpers."""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings  # noqa: E402
from app.db import create_db_engine, metadata  # noqa: E402

FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

TEST_TOKEN = "test-internal-token"


def load_fixture_json(name: str):
    with open(os.path.join(FIXTURES_DIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_fixture_text(name: str) -> str:
    with open(os.path.join(FIXTURES_DIR, name), "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.fixture()
def engine():
    """Fresh in-memory SQLite DB with all tables created from metadata."""
    eng = create_db_engine("sqlite://")
    metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def settings(tmp_path) -> Settings:
    return Settings(
        database_url="sqlite://",
        internal_api_token=TEST_TOKEN,
        models_dir=str(tmp_path / "models"),
        http_timeout_seconds=2.0,
        stale_minutes=30,
        provider_courtesy_delay=0.0,
        provider_backoff_base=0.0,
    )


@pytest.fixture()
def client(engine, settings):
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings=settings, engine=engine)
    with TestClient(app) as test_client:
        yield test_client
