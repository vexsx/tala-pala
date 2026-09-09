"""Scheduler-callable buy/hold/sell generation across the eligible universe.

The orchestration half only.  What an asset can carry lives in
``app/signals/universe.py``, how a symbol's inputs are assembled and scored
lives in ``app/signals/engine.py``, and what a round trip costs lives in
``app/core/costs.py``.  What is added here is what only matters once seven
assets are scored on a schedule.

*One symbol's failure is its own.*  Each symbol is assembled and written
inside its own transaction, so a missing series, an unexpected NaN or a
constraint violation costs exactly that symbol.  The others are still
published.  This is the same isolation ``app/jobs/economic.py`` applies
per series, and it matters more here than there: before this job existed the
whole pass was one gold-shaped function, so anything that raised produced no
signal at all — and the API's "latest signal" read has no way to tell a
missing row from a stale one.

*Withheld is not failed.*  A symbol that cannot support enough factors to say
anything publishes nothing and reports WHY, and the pass still succeeded.  The
two Tehran gold ETFs hold about fifty days of history between them, so this is
an ordinary daily outcome rather than an exception, and counting it as a
failure would make the job's health signal useless.

*A pass in which every symbol FAILED is a failed job.*  Same reasoning as the
economic ingest: the Go scheduler records job success from the HTTP status,
and a pass that wrote nothing because everything raised must not enter that
history green.  A pass where everything was WITHHELD is not a failure — the
engine ran and answered, and its answer was "not enough to say".

*The freshness gauge follows that rule and nothing else.*
``JOB_LAST_SUCCESS`` answers one question — when did this job last complete a
pass? — and it is now set on exactly the passes that do not raise above.  It
used to be gated on an empty ``errors`` list, which meant a single symbol
raising held the gauge back for all seven and had a staleness alert fire about
signals that were freshly written.  Which symbol failed is reported per symbol
in ``report['symbols']``; a single global gauge cannot carry that and must not
pretend to.  Note that ``app/jobs/economic.py`` still gates its own gauge on
``not result['errors']`` and therefore has the same defect for a partially
failed ingest; it is left alone here rather than changed as a drive-by.

*The universe is published, not implied.*  ``app_settings['signal_universe']``
carries the eligible list, the deliberate exclusions, the asset classes that
are not collected at all, and the reason each withheld symbol published
nothing on this pass.  The overview endpoint projects that row; it decides
none of it.  Without this, a page listing seven assets would silently imply
that seven assets are the investable universe — and the honest statement, that
cars, housing and Tehran-listed equities have zero rows in ``prices``, would
live nowhere a reader could reach.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional, Sequence

from sqlalchemy.engine import Engine

from ..config import Settings
from ..db import utcnow
from ..metrics import JOB_LAST_SUCCESS
from ..signals import universe
from ..signals.engine import generate_signal_for, publish_decision_policy

log = logging.getLogger(__name__)

JOB_NAME = "signals"

# Published to app_settings so the Go API can state the gap instead of
# recomputing it. Mirrors DECISION_POLICY_KEY's role for the cost contract.
SIGNAL_UNIVERSE_KEY = "signal_universe"


class SignalsGenerationFailed(RuntimeError):
    """Every symbol attempted raised; the pass published nothing.

    Carries the full report as :attr:`report` so the caller can still say
    which symbol failed and why — the point is to make the failure impossible
    to return as a success, not to lose the detail.
    """

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        failed = report.get("failed", 0)
        super().__init__(
            f"signals: all {failed} symbol(s) failed, nothing was published: "
            + "; ".join(report.get("errors", []))[:1000]
        )


def _requested(symbols: Optional[Sequence[str]]) -> list[str]:
    """Validate and order the symbols for this pass.

    An ineligible symbol is a 400 naming it, never a silent drop: a caller
    that asked for six symbols and got five back has been told something
    false about the sixth.
    """
    if not symbols:
        return list(universe.SIGNAL_SYMBOLS)
    unknown = [s for s in symbols if s not in universe.SIGNAL_SYMBOLS]
    if unknown:
        details = []
        for symbol in unknown:
            stated = universe.EXCLUDED_SYMBOLS.get(symbol)
            details.append(f"{symbol}" + (f" ({stated})" if stated else ""))
        raise ValueError(
            "not eligible for a buy/hold/sell signal: "
            + "; ".join(details)
            + ". Eligible symbols: "
            + ", ".join(universe.SIGNAL_SYMBOLS)
        )
    # De-duplicate while keeping the caller's order.
    seen: set[str] = set()
    return [s for s in symbols if not (s in seen or seen.add(s))]


def run_signals(
    engine: Engine,
    settings: Settings,
    symbols: Optional[Sequence[str]] = None,
) -> dict[str, Any]:
    """Generate signals for every eligible symbol (or the ones named).

    Returns a per-symbol report plus the published universe.  Raises
    :class:`ValueError` for an ineligible symbol (the endpoint turns that into
    a 400) and :class:`SignalsGenerationFailed` when every attempted symbol
    raised.
    """
    requested = _requested(symbols)
    report: dict[str, Any] = {
        "started_at": utcnow().isoformat(),
        "symbols": {},
        "errors": [],
        "published": 0,
        "withheld": 0,
        "failed": 0,
    }

    # Once per pass, before the loop: it is gold's contract and it must not
    # depend on which symbol happened to run last.
    report["decision_policy"] = publish_decision_policy(engine)

    withheld: dict[str, str] = {}
    for symbol in requested:
        try:
            outcome = generate_signal_for(engine, settings, symbol)
        except Exception as exc:  # noqa: BLE001 — one symbol must not sink the pass
            log.warning("signal generation failed for %s: %s", symbol, exc)
            report["symbols"][symbol] = {
                "status": "error", "reason": type(exc).__name__,
            }
            report["errors"].append(f"{symbol}: {exc}")
            report["failed"] += 1
            continue
        report["symbols"][symbol] = outcome.summary()
        if outcome.published:
            report["published"] += 1
        else:
            report["withheld"] += 1
            withheld[symbol] = outcome.withheld_reason or "no reason recorded"

    report["universe"] = publish_universe(engine, requested, withheld)
    report["finished_at"] = utcnow().isoformat()
    if report["failed"] and not report["published"] and not report["withheld"]:
        # Nothing ran to completion. The caller must not be able to read this
        # as a successful pass; see the module docstring.
        raise SignalsGenerationFailed(report)
    # The gauge is the exact complement of the raise above, and deliberately
    # so. It answers "when did this job last complete a pass?" — a staleness
    # alert reads it — and it used to be gated on `not report["errors"]`,
    # which made one symbol raising freeze it for every other symbol. Gold
    # could publish normally on seven consecutive passes and the gauge would
    # sit at whatever time the coin last stopped failing, so the alert fired
    # about signals that were in fact perfectly fresh. That contradicted this
    # module's own first promise, "one symbol's failure is its own": a failure
    # that silences the health signal for all seven is not its own.
    #
    # A pass that produced ANY completed outcome — a published row or a stated
    # withholding — is a pass that ran. Per-symbol failures are already
    # reported in `errors` and `report["symbols"][s]["status"] == "error"`,
    # which is where a partial outage belongs; the gauge is not the place to
    # encode it, because it has no way to say WHICH symbol.
    JOB_LAST_SUCCESS.labels(job=JOB_NAME).set(time.time())
    return report


def publish_universe(
    engine: Engine,
    attempted: Sequence[str],
    withheld: dict[str, str],
) -> dict[str, Any]:
    """Write the eligibility contract the overview endpoint projects.

    ``unavailable`` deliberately mixes two kinds of entry — symbols this
    system collects but does not score, and asset classes it does not collect
    at all — because a reader asking "why is my car not here?" and one asking
    "why is there no dollar-index call?" are asking the same question, and
    splitting the answer across two fields is how one of them stops being
    answered.
    """
    contract: dict[str, Any] = {
        "as_of": utcnow().isoformat(),
        "eligible": list(universe.SIGNAL_SYMBOLS),
        "attempted": list(attempted),
        # Eligible, ran, and published nothing — with the engine's own reason.
        # Distinct from `unavailable`, which is about assets that were never
        # candidates.
        "withheld": [
            {"symbol": symbol, "reason": reason}
            for symbol, reason in withheld.items()
        ],
        "unavailable": universe.unavailable(),
        "min_scoreable_factors": universe.MIN_SCOREABLE_FACTORS,
        "contract_version": 1,
    }
    from .evaluate import upsert_setting

    try:
        upsert_setting(engine, SIGNAL_UNIVERSE_KEY, contract)
    except Exception as exc:  # noqa: BLE001 — never sink a pass that produced rows
        log.warning("signal universe persist failed: %s", exc)
    return contract
