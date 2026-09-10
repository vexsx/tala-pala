"""FastAPI application: internal-only prediction service (port 8500).

Auth contract (docs/CONTRACTS.md): every ``/internal/*`` request requires the
``X-Internal-Token`` header except ``/internal/health`` and
``/internal/metrics``.  The service is never exposed publicly — it lives on
the Docker-internal network and only the Go API talks to it.
"""
from __future__ import annotations

import hmac
import logging
import os
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .backtest.engine import run_and_store as run_backtest_and_store
from .config import Settings, get_settings
from .db import create_db_engine, db_ok
from .jobs.backfill import coverage_report, run_backfill
from .jobs.cleanup import run_cleanup
from .jobs.collect import run_collect
from .jobs.evaluate import run_evaluate
from .jobs.features import run_generate_features
from .metrics import render_metrics
from .models.predicting import predict_all
from .models.training import train_all
from .providers import registry

log = logging.getLogger(__name__)

TOKEN_EXEMPT_PATHS = {"/internal/health", "/internal/metrics"}


class CollectRequest(BaseModel):
    jobs: list[str] = Field(default_factory=list)


class HorizonsRequest(BaseModel):
    horizons: list[str] = Field(default_factory=list)
    symbols: list[str] = Field(default_factory=list)


class CustomPredictRequest(BaseModel):
    days: int = 7
    fee_pct: Optional[float] = None
    spread_pct: Optional[float] = None
    slippage_pct: Optional[float] = None


class BackfillRequest(BaseModel):
    """Historical backfill of exogenous daily history (empty = macro default)."""

    symbols: list[str] = Field(default_factory=list)
    range: str = "5y"


class TGJUBackfillRequest(BaseModel):
    """Deep-history gap-fill from TGJU (empty symbols = all three Iranian ones).

    ``ignore_sources`` is the operator override for a junk join reference: a
    ``prices`` source named here is not eligible to become the live value the
    backfill is checked against.  It cannot move the write boundary — no
    request may make this job write into a day that already holds a row.
    """

    symbols: list[str] = Field(default_factory=list)
    dry_run: bool = False
    ignore_sources: list[str] = Field(default_factory=list)


class NewsCollectRequest(BaseModel):
    """Optional narrowing of a news collection pass (empty = every collector)."""

    sources: list[str] = Field(default_factory=list)
    dry_run: bool = False


class SignalsRequest(BaseModel):
    """Optional narrowing of a signals pass (empty = every eligible symbol)."""

    symbols: list[str] = Field(default_factory=list)


class TrendAlignmentRequest(BaseModel):
    """Optional narrowing of a trend-alignment pass (empty = every symbol)."""

    symbols: list[str] = Field(default_factory=list)


class EconomicIngestRequest(BaseModel):
    """Optional narrowing of an economic ingest (empty = every enabled series)."""

    codes: list[str] = Field(default_factory=list)


class SciCpiIngestRequest(BaseModel):
    """One SCI urban-CPI workbook, already on disk in this container.

    ``path`` is where the bytes are now — a temporary copy, and not
    provenance.  ``filename`` is what SCI called the file; it carries the
    Jalali publication timestamp, so it is the only place ``published_at`` can
    come from and a copy renamed in transit loses that date rather than having
    one invented for it.  It defaults to ``path``'s basename.  ``url`` is where
    the document was obtained; when it is empty the canonical SCI statistics
    path for that filename is recorded and the response says so.
    """

    path: str
    filename: str = ""
    url: str = ""


class EquityBarsIngestRequest(BaseModel):
    """Daily-bar payloads for one or more instruments, already in this container.

    A LIST of paths rather than the payloads themselves, and that is forced
    rather than stylistic: one instrument's ``GetClosingPriceDailyList``
    response is ~1.4 MB and a twenty-symbol run is ~28 MB, which cannot travel
    through the ``wget --post-data`` argv that scripts/tsetmc_fetch.py uses on
    the production host.  The files are copied in with ``docker compose cp``,
    exactly as the SCI workbook is, and this body just names them.

    The insCode is taken from each payload's own rows, never from its filename:
    a file renamed in transit must not be able to store one company's bars
    under another's code.
    """

    paths: list[str] = Field(default_factory=list)


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

    # Mirror every WARNING/ERROR into the shared app_issues table (Issues tab).
    from .core.issues import install_issue_capture

    install_issue_capture(engine)

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
        return {"status": "ok", "db": db_ok(engine), "version": __version__, "build_commit": os.environ.get("BUILD_COMMIT", "unknown")}

    @app.get("/internal/metrics")
    def metrics() -> Response:
        payload, content_type = render_metrics()
        return Response(content=payload, media_type=content_type)

    @app.get("/internal/providers/health")
    def provider_health() -> list[dict]:
        return registry.providers_health(engine)

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
        symbols = req.symbols if req and req.symbols else None
        return train_all(engine, settings, horizons, symbols)

    @app.post("/internal/predict")
    def predict(req: Optional[HorizonsRequest] = None) -> dict:
        horizons = req.horizons if req and req.horizons else None
        symbols = req.symbols if req and req.symbols else None
        return predict_all(engine, settings, horizons, symbols)

    @app.post("/internal/predict/custom")
    def predict_custom_endpoint(req: CustomPredictRequest) -> dict:
        from .models.custom import predict_custom

        try:
            return predict_custom(
                engine, settings, req.days,
                fee_pct=req.fee_pct, spread_pct=req.spread_pct,
                slippage_pct=req.slippage_pct,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "bad_request", "message": str(exc)}},
            )  # type: ignore[return-value]

    @app.post("/internal/signals/generate")
    def signals_generate(req: Optional[SignalsRequest] = None) -> dict:
        """Score every eligible asset (Addendum 28), or just the ones named.

        ``symbols`` empty means the seven symbols in
        ``app/signals/universe.py``: the assets that both have real price
        history and are things a reader can hold. One symbol failing does not
        stop the others — each is assembled and written in its own
        transaction — and a symbol that cannot support enough factors to say
        anything publishes NOTHING, with its reason, rather than a hold that
        would look like a considered neutral verdict.

        A symbol that is not eligible is refused with 400 naming it and, where
        there is one, the stated reason it is excluded (DXY and US10Y are
        macro context rather than holdings; IR_GOLD_FUND_FLOW is a ratio in
        percent, so a "buy" on it means nothing). It is never quietly dropped
        from the list the caller asked for.

        A pass in which EVERY attempted symbol raised answers 502, not 200:
        the Go scheduler records job success from the status code, and a pass
        that published nothing because everything broke must not enter that
        history as a success. The body still carries the full per-symbol
        report. A pass where symbols were merely WITHHELD is a success — the
        engine ran and its answer was "not enough to say".
        """
        from .jobs.signals import SignalsGenerationFailed, run_signals

        try:
            return run_signals(
                engine, settings, (req.symbols or None) if req else None
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "bad_request", "message": str(exc)}},
            )  # type: ignore[return-value]
        except SignalsGenerationFailed as exc:
            return JSONResponse(
                status_code=502,
                content=exc.report
                | {"error": {"code": "generation_failed", "message": str(exc)}},
            )  # type: ignore[return-value]

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

    @app.post("/internal/backfill/history")
    def backfill_history(body: BackfillRequest) -> dict:
        """Idempotent multi-year backfill of exogenous daily history.

        Manual/occasional job (not scheduled): it exists so macro features have
        enough history to be trained and ablated honestly.
        """
        return run_backfill(engine, settings, body.symbols or None, body.range)

    @app.post("/internal/backfill/tgju-history")
    def backfill_tgju_history(req: Optional[TGJUBackfillRequest] = None) -> dict:
        """Gap-fill the deep Iranian daily history TGJU has and we do not.

        Empty ``symbols`` means IR_GOLD_18K, USD_IRT and IR_COIN_EMAMI. Rows
        are only ever written for days strictly before the symbol's earliest
        existing observation, so the live, densely-collected era is never
        touched by a second same-day source, and
        a symbol whose backfill does not join continuously onto its live
        series is refused with both values reported rather than written.

        Manual/occasional and deliberately NOT scheduled: it exists so the P1
        purchasing-power and inflation-catch-up analytics have more than the
        few years of history live collection has accumulated.

        Three guards run before anything is promoted: bar dates must fall in
        a plausible window (a Jalali date parses as a valid Gregorian one),
        the backfilled span must be internally coherent bar to bar, and the
        last backfilled value must join continuously onto the first trusted
        live value. Any of them refuses the symbol with the evidence; only a
        series that is genuinely already covered reports ``up_to_date``.

        A successful write also RECORDS the splice — the instrument note says
        in words where the series definition changes, and
        ``app_settings['series_splices']`` carries the machine-readable
        register — because the era before the join and the era after it are
        not the same measurement.

        ``dry_run`` runs every check and reports exactly what would be
        written, without writing. Re-running writes nothing.

        A symbol this job will not backfill is a 400 naming it — XAUUSD in
        particular, because ours is a Yahoo COMEX futures proxy and TGJU's
        'ons' is spot (see app/jobs/tgju_backfill.py).
        """
        from .jobs.tgju_backfill import run_tgju_backfill

        try:
            return run_tgju_backfill(
                engine,
                settings,
                symbols=(req.symbols or None) if req else None,
                dry_run=bool(req.dry_run) if req else False,
                ignore_sources=(req.ignore_sources or None) if req else None,
            )
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "bad_request", "message": str(exc)}},
            )  # type: ignore[return-value]

    @app.post("/internal/news/ingest")
    def news_ingest(body: BackfillRequest) -> dict:
        """Poll approved news sources (no-op unless NEWS_ENABLED is truthy).

        Deliberately NOT scheduled: there is no historical archive, so news
        features cannot inform forecasts yet. This endpoint exists so an
        operator can start accumulating history when they choose to.
        """
        from .jobs.news import run_news_ingest

        return run_news_ingest(engine, settings, body.symbols or None)

    @app.post("/internal/news/collect")
    def news_collect(req: Optional[NewsCollectRequest] = None) -> dict:
        """Run one pass over the Addendum 18 collectors (fed, OFAC, GDELT).

        A no-op unless NEWS_COLLECTION_ENABLED is true, and even then only over
        sources the policy table approves. Distinct from /internal/news/ingest,
        which drives the older provider path in app/jobs/news.py.
        """
        from .jobs.news_collect import run_news_collection

        return run_news_collection(
            engine,
            settings,
            sources=(req.sources or None) if req else None,
            dry_run=bool(req.dry_run) if req else False,
        )

    @app.post("/internal/trend-alignment/evaluate")
    def trend_alignment_evaluate(req: Optional[TrendAlignmentRequest] = None) -> dict:
        """Evaluate 1D/4H/1H trend alignment and record entries (Addendum 20).

        A technical indicator only: it reads price history and writes its own
        state/event tables, never model input, confidence, intervals or the
        buy/sell policy. Safe to call repeatedly — an unchanged closed-candle
        set produces no new event.

        The same pass also refreshes the indicator's measured track record
        (Addendum 22) and reports it per window under
        ``symbols[<symbol>].performance``: for each of 90/60/30/14 days, the
        basis it could honestly be computed on, the bar and episode counts, the
        forward returns against an unconditional baseline, and the note saying
        what the data does not support. A backtest failure is reported in
        ``performance_error`` and leaves the live evaluation above intact.
        """
        from .jobs.trend_alignment import run_trend_alignment

        return run_trend_alignment(
            engine, settings, symbols=(req.symbols or None) if req else None
        )

    @app.post("/internal/economic/ingest")
    def economic_ingest(req: Optional[EconomicIngestRequest] = None) -> dict:
        """Ingest the declared economic catalog (migration 0024).

        ``codes`` empty means every enabled series. Each series is fetched and
        written in its own transaction, so one bad source cannot roll back the
        others; per-series counts and any failures come back in the response.

        Safe to call repeatedly: a value the source has not changed inserts
        nothing, and a value it HAS changed inserts a new vintage beside the
        print it replaced rather than overwriting it.

        A code that is not in the catalog is refused with 400 — an unknown
        series is never quietly dropped from the list the caller asked for.

        A pass in which EVERY series failed answers 502, not 200: the Go
        scheduler records job success from the status code, and a run that
        ingested nothing must not enter that history as a success. The body
        still carries the full per-series report, so the failure is
        diagnosable. A partial failure stays 200 with an ``errors`` list —
        one provider being down is not a failed job.
        """
        from .jobs.economic import EconomicIngestFailed, ingest_economic

        try:
            return ingest_economic(engine, settings, (req.codes or None) if req else None)
        except ValueError as exc:
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "bad_request", "message": str(exc)}},
            )  # type: ignore[return-value]
        except EconomicIngestFailed as exc:
            return JSONResponse(
                status_code=502,
                content=exc.report
                | {
                    "error": {
                        "code": "upstream_failed",
                        "message": str(exc),
                    }
                },
            )  # type: ignore[return-value]

    @app.post("/internal/economic/sci-cpi")
    def economic_sci_cpi(body: SciCpiIngestRequest) -> dict:
        """Ingest one Statistical Centre of Iran urban-CPI workbook from disk.

        The counterpart of /internal/economic/ingest for a source that cannot
        be polled: amar.org.ir accepts a TCP connection from the production
        host and then answers nothing, at any TLS version and over plain HTTP
        (diagnosed 2026-09-09, recorded in migration 0027). So the file is
        fetched somewhere that can reach SCI, copied in, and posted here —
        scripts/sci_fetch.py does all three. There is deliberately no cron.

        The whole workbook is parsed and verified before anything is written:
        four target rows must all be found (a layout change that moved one must
        not ingest the other three and look successful) and the twelve months
        of 1400 must average 100 (the stated base, checked from the data
        itself). A file that fails either guard leaves the database untouched
        and answers 400 with what was wrong.

        On success the document is archived in ``source_documents`` — hashed
        and deduped on (provider_code, content_sha256) — and every observation
        is written through the same bitemporal store the polled series use,
        carrying that document's id. ``published_at`` is real here rather than
        NULL: SCI embeds a Jalali publication timestamp in its own filename.

        Safe to call repeatedly. Re-posting the same file re-uses the deduped
        document row and writes no observation, because every value compares
        equal to what is stored; a workbook whose values differ writes new
        vintages beside the prints they revise, never over them.
        """
        from .economic.sci import SciParseError, ingest_sci_cpi

        try:
            report = ingest_sci_cpi(
                engine, body.path, filename=body.filename, url=body.url
            )
        except SciParseError as exc:
            # A statement about the DOCUMENT: the layout or the base changed
            # under us, which is the provider's doing and belongs on its health
            # row where an operator will see it.
            registry.record_failure(engine, "sci", str(exc))
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "bad_request", "message": str(exc)}},
            )  # type: ignore[return-value]
        except (OSError, ValueError) as exc:
            # A statement about the CALL — a path that is not there, a file
            # that is not a workbook. Not the provider's fault, so its health
            # is left alone rather than accumulating failures an operator's
            # typo caused.
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "bad_request", "message": str(exc)}},
            )  # type: ignore[return-value]
        registry.record_success(engine, "sci")
        return report

    @app.get("/internal/equities/roster")
    def equities_roster(include_disabled: bool = False) -> dict:
        """The Tehran instruments this deployment ingests and serves.

        Read by scripts/tsetmc_fetch.py over the same SSH path it already uses,
        which is what makes widening the roster an INSERT into
        ``equity_instruments`` rather than an edit to a list that then has to
        be kept in step with the database. ``include_disabled`` shows rows the
        deployment has deliberately turned off — کچاد is seeded that way, with
        the measurement that refused it on the row.
        """
        from .equities.ingest import roster

        items = roster(engine, include_disabled=include_disabled)
        return {"items": items, "count": len(items)}

    @app.post("/internal/equities/bars")
    def equities_bars(body: EquityBarsIngestRequest) -> dict:
        """Ingest Tehran equity daily bars from payload files on disk.

        The counterpart of /internal/economic/sci-cpi for a source that cannot
        be polled: cdn.tsetmc.com resolves from the production host and then
        refuses TCP 443 outright — a harder block than SCI's, where the
        connection opened and the payload was dropped (measured 2026-09-10,
        recorded in migration 0028). So the payloads are fetched somewhere that
        can reach TSETMC, copied in, and posted here; scripts/tsetmc_fetch.py
        does all three. There is deliberately no cron.

        **Per-symbol isolation.** Each path is parsed, adjusted and written in
        its own transaction. One truncated transfer costs one symbol, not the
        run, and the failure comes back in ``errors`` with its exception type.
        A pass in which EVERY path failed answers 502, not 200 — the Go
        scheduler reads job success from the status code and a run that
        ingested nothing must not enter that history as a success.

        **The gate.** Every symbol is adjusted for corporate actions (detected
        from TSETMC's own ``priceYesterday``) and then VALIDATED before its
        series is promoted: after adjustment no genuine single-session return
        may exceed 25%, a bound measured against 65,802 session pairs across
        twenty symbols and twenty-five years, where the worst legitimate move
        is 13.6% and the p99 is exactly the exchange's 5% price limit. A symbol
        that fails is stored — the raw bars and the detected actions are still
        what TSETMC served and implied — but its verdict row says ``refused``
        and the read API will not serve it adjusted. docs/REDESIGN.md makes
        that the explicit gate for this phase.

        **Idempotent.** Re-posting the same payloads inserts nothing: every bar
        and every action is already there. A payload that CONTRADICTS a stored
        bar fails that symbol rather than overwriting it — TSETMC does not
        revise a settled session, so a disagreement is a corrupt transfer or a
        real restatement, and both want a human.
        """
        from .equities.adjust import BarParseError
        from .equities.ingest import EquityIngestFailed, ingest_bar_files

        try:
            report = ingest_bar_files(engine, body.paths)
        except (ValueError, BarParseError) as exc:
            # A statement about the CALL — an empty path list, or a body that
            # named nothing this service can read. Not the provider's doing, so
            # its health row is left alone.
            return JSONResponse(
                status_code=400,
                content={"error": {"code": "bad_request", "message": str(exc)}},
            )  # type: ignore[return-value]
        except EquityIngestFailed as exc:
            # Every payload failed. That IS a statement about the data: either
            # the transfer or the endpoint shape changed under us, and it
            # belongs on the provider's health row where an operator sees it.
            registry.record_failure(engine, "tsetmc_cdn", str(exc))
            return JSONResponse(
                status_code=502,
                content=exc.report
                | {"error": {"code": "upstream_failed", "message": str(exc)}},
            )  # type: ignore[return-value]
        registry.record_success(engine, "tsetmc_cdn")
        return report

    @app.get("/internal/data/coverage")
    def data_coverage() -> dict:
        return {"coverage": coverage_report(engine)}

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
