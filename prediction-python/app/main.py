"""FastAPI application: internal-only prediction service (port 8500).

Auth contract (docs/CONTRACTS.md): every ``/internal/*`` request requires the
``X-Internal-Token`` header except ``/internal/health`` and
``/internal/metrics``.  The service is never exposed publicly — it lives on
the Docker-internal network and only the Go API talks to it.
"""
from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .backtest.engine import run_and_store as run_backtest_and_store
from .config import Settings, get_settings
from .db import create_db_engine, db_ok
from .jobs.cleanup import run_cleanup
from .jobs.collect import run_collect
from .jobs.evaluate import run_evaluate
from .jobs.features import run_generate_features
from .metrics import render_metrics
from .models.predicting import predict_all
from .models.training import train_all
from .providers.registry import providers_health
from .signals.engine import generate_signal

log = logging.getLogger(__name__)

TOKEN_EXEMPT_PATHS = {"/internal/health", "/internal/metrics"}


class CollectRequest(BaseModel):
    jobs: list[str] = Field(default_factory=list)


class HorizonsRequest(BaseModel):
    horizons: list[str] = Field(default_factory=list)


class BacktestRequest(BaseModel):
    horizon: str = "1d"
    fee_pct: float = 0.5
    spread_pct: float = 1.0
    slippage_pct: float = 0.1
    min_holding_days: int = 1
    start: Optional[str] = None
    end: Optional[str] = None


def create_app(settings: Optional[Settings] = None, engine=None) -> FastAPI:
    settings = settings or get_settings()
    engine = engine if engine is not None else create_db_engine(settings.database_url)

    app = FastAPI(
        title="goldpred prediction service",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine

    @app.middleware("http")
    async def internal_token_middleware(request: Request, call_next):
        path = request.url.path
        if path.startswith("/internal") and path not in TOKEN_EXEMPT_PATHS:
            supplied = request.headers.get("X-Internal-Token", "")
            expected = settings.internal_api_token
            if not expected or not hmac.compare_digest(supplied, expected):
                return JSONResponse(
                    status_code=401,
                    content={"error": {"code": "unauthorized",
                                       "message": "missing or invalid X-Internal-Token"}},
                )
        return await call_next(request)

    # -- endpoints (sync handlers run in the threadpool) ---------------------

    @app.get("/internal/health")
    def health() -> dict:
        return {"status": "ok", "db": db_ok(engine), "version": __version__}

    @app.get("/internal/metrics")
    def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    @app.get("/internal/providers/health")
    def provider_health() -> list[dict]:
        return providers_health(engine)

    @app.post("/internal/collect")
    def collect(req: Optional[CollectRequest] = None) -> dict:
        jobs = req.jobs if req and req.jobs else None
        return run_collect(engine, settings, jobs)

    @app.post("/internal/features/generate")
    def features_generate() -> dict:
        return run_generate_features(engine, settings)

    @app.post("/internal/train")
    def train(req: Optional[HorizonsRequest] = None) -> dict:
        horizons = req.horizons if req and req.horizons else None
        return train_all(engine, settings, horizons)

    @app.post("/internal/predict")
    def predict(req: Optional[HorizonsRequest] = None) -> dict:
        horizons = req.horizons if req and req.horizons else None
        return predict_all(engine, settings, horizons)

    @app.post("/internal/signals/generate")
    def signals_generate() -> dict:
        return generate_signal(engine, settings)

    @app.post("/internal/backtest")
    def backtest(req: Optional[BacktestRequest] = None) -> dict:
        payload = req.model_dump() if req else {}
        try:
            return run_backtest_and_store(engine, settings, payload)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "bad_request", "message": str(exc)}},
            )  # type: ignore[return-value]

    @app.post("/internal/evaluate")
    def evaluate() -> dict:
        return run_evaluate(engine, settings)

    @app.post("/internal/maintenance/cleanup")
    def maintenance_cleanup() -> dict:
        return run_cleanup(engine, settings)

    return app


app: Optional[FastAPI] = None


def get_app() -> FastAPI:
    """Lazy module-level app for uvicorn (`app.main:get_app` factory)."""
    global app
    if app is None:
        app = create_app()
    return app


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    settings = get_settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.prediction_port)
