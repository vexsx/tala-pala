"""Split-conformal prediction intervals (Addendum 15).

Residuals are collected during walk-forward validation as *relative* errors
``(actual - pred) / pred``. An interval around a new point forecast is built
from those residuals.

Why this file was rewritten
--------------------------
The previous implementation took ``np.quantile(res, alpha/2)`` and
``np.quantile(res, 1 - alpha/2)`` directly. ``np.quantile`` interpolates
*between* order statistics, which is the plug-in estimator of a population
quantile — it carries no finite-sample coverage guarantee. Fed the 8–12
holdout residuals this pipeline actually produces, a Monte-Carlo study under
perfect iid exchangeability measured the realized coverage of the nominal
90% band at **0.72 / 0.76 / 0.77 / 0.78 for n = 8 / 10 / 11 / 12**. Every
shipped "90% interval" was structurally a ~78% interval, and because the
shortfall is an estimator bias rather than a distributional drift, the live
ACI loop could not repair it.

Split conformal prediction (Vovk et al.; Lei et al. 2018, JASA) fixes this by
using an *order statistic* rather than an interpolated quantile. For ``n``
exchangeable residuals and target coverage ``1 - alpha``, the
``k = ceil((n+1) * (1-alpha))``-th smallest absolute residual gives finite-
sample coverage ``>= 1 - alpha`` — provided ``k <= n``, i.e.
``n >= ceil(1/alpha) - 1`` (9 residuals for a 90% band).

Two regimes are therefore used:

* ``n >= TWO_SIDED_MIN(alpha)`` (19 at alpha=0.1): signed two-sided order
  statistics, which preserve genuine skew (gold's upside tail differs from
  its downside).
* ``n >= SYMMETRIC_MIN(alpha)`` (9 at alpha=0.1): symmetric band on absolute
  residuals — valid, slightly wider, and reachable with this pipeline's
  holdout sizes.
* below that: the largest observed absolute residual, inflated by the
  extrapolation factor ``(n+1)(1-alpha)/n``, and flagged as low-evidence.
  Coverage is NOT guaranteed here; the flag exists so the UI can say so.

Intervals are only as exchangeable as the residual pool. The pool comes from
the winner's *holdout* folds (folds it was not selected on) precisely so the
residuals are not the optimistically small ones the winner minimized.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

DEFAULT_ALPHA = 0.1  # 90% nominal coverage

# Fallback half-width when there is essentially no residual evidence at all.
MIN_EVIDENCE_RADIUS = 0.05


def relative_residuals(preds: Sequence[float], actuals: Sequence[float]) -> list[float]:
    out: list[float] = []
    for p, a in zip(preds, actuals):
        if p != 0:
            out.append((float(a) - float(p)) / float(p))
    return out


def symmetric_min_n(alpha: float = DEFAULT_ALPHA) -> int:
    """Residuals needed for a VALID symmetric conformal band at ``alpha``."""
    return max(1, math.ceil(1.0 / alpha) - 1)


def two_sided_min_n(alpha: float = DEFAULT_ALPHA) -> int:
    """Residuals needed before both signed tails have their own order statistic."""
    return max(1, math.ceil(2.0 / alpha) - 1)


def conformal_radius(residuals: Sequence[float], alpha: float = DEFAULT_ALPHA) -> tuple[float, bool]:
    """Symmetric conformal half-width on |relative residual|.

    Returns ``(radius, guaranteed)`` where ``guaranteed`` is True when the
    finite-sample coverage guarantee actually holds (``k <= n``).
    """
    res = np.abs(np.asarray(list(residuals), dtype=float))
    res = res[np.isfinite(res)]
    n = res.size
    if n == 0:
        return MIN_EVIDENCE_RADIUS, False
    k = math.ceil((n + 1) * (1.0 - alpha))
    ordered = np.sort(res)
    if k <= n:
        return float(ordered[k - 1]), True
    # Not enough residuals for the guarantee: extrapolate beyond the largest
    # observed error instead of silently interpolating below it.
    inflation = (n + 1) * (1.0 - alpha) / n
    return float(max(ordered[-1] * inflation, MIN_EVIDENCE_RADIUS)), False


def conformal_interval(
    point: float, residuals: Sequence[float], alpha: float = DEFAULT_ALPHA
) -> tuple[float, float, dict]:
    """``(lower, upper, diagnostics)`` around ``point``.

    Uses signed two-sided order statistics when there is enough evidence for
    both tails (preserving skew), otherwise the symmetric conformal band.
    """
    res = np.asarray(list(residuals), dtype=float)
    res = res[np.isfinite(res)]
    n = int(res.size)
    diag: dict = {
        "n_residuals": n,
        "alpha": round(float(alpha), 4),
        "method": "none",
        "coverage_guaranteed": False,
        "min_n_symmetric": symmetric_min_n(alpha),
        "min_n_two_sided": two_sided_min_n(alpha),
    }

    if n >= two_sided_min_n(alpha):
        ordered = np.sort(res)
        # Lower tail: floor((n+1)*alpha/2)-th order statistic (1-indexed);
        # upper tail: ceil((n+1)*(1-alpha/2))-th. Both exist in this branch.
        lo_idx = max(1, math.floor((n + 1) * (alpha / 2.0)))
        hi_idx = min(n, math.ceil((n + 1) * (1.0 - alpha / 2.0)))
        lo_q = float(ordered[lo_idx - 1])
        hi_q = float(ordered[hi_idx - 1])
        diag.update(method="conformal_two_sided", coverage_guaranteed=True,
                    lower_residual=round(lo_q, 6), upper_residual=round(hi_q, 6))
    else:
        radius, guaranteed = conformal_radius(res, alpha)
        lo_q, hi_q = -radius, radius
        diag.update(
            method="conformal_symmetric" if guaranteed else "conformal_extrapolated",
            coverage_guaranteed=guaranteed,
            radius=round(float(radius), 6),
        )

    lower = point * (1.0 + lo_q)
    upper = point * (1.0 + hi_q)
    if lower > upper:  # degenerate residual pool
        lower, upper = upper, lower
    diag["rel_width_pct"] = round(((upper - lower) / point * 100.0) if point else 0.0, 4)
    return lower, upper, diag


def empirical_interval(
    point: float, residuals: Sequence[float], alpha: float = DEFAULT_ALPHA
) -> tuple[float, float]:
    """Backwards-compatible two-tuple wrapper over :func:`conformal_interval`."""
    lower, upper, _ = conformal_interval(point, residuals, alpha)
    return lower, upper


# --- adaptive conformal (ACI-style, batch form) ------------------------------
# Adaptive Conformal Inference (Gibbs & Candès 2021; arXiv:2202.07282) adjusts
# the miscoverage level alpha online: intervals that under-cover get a smaller
# effective alpha (wider quantiles), over-covering ones a larger alpha
# (tighter quantiles).  We run the batch analogue driven by the live coverage
# statistics the evaluate job already maintains.
#
# NOTE: ACI corrects DRIFT (the residual distribution moving), not estimator
# bias. The conformal order statistic above is what makes the nominal level
# honest in the first place; ACI then tracks changes around it.
ACI_GAIN = 0.5          # step size on the coverage error
ACI_MIN_ALPHA = 0.02    # never tighter than the 98% band quantiles
ACI_MAX_ALPHA = 0.30    # never looser than the 70% band quantiles
ACI_MIN_N = 20          # matured predictions before live coverage is trusted


def adaptive_alpha(
    live_coverage: float | None,
    n: int,
    alpha: float = DEFAULT_ALPHA,
    target: float = 1.0 - DEFAULT_ALPHA,
) -> float:
    """Effective miscoverage level from live interval performance.

    ``alpha_eff = alpha + ACI_GAIN * (live_coverage - target)``, clamped to
    [ACI_MIN_ALPHA, ACI_MAX_ALPHA]; with fewer than ``ACI_MIN_N`` matured
    predictions (or no stats) the nominal ``alpha`` is returned unchanged.

    Example: live coverage 0.75 against a 0.90 target gives
    ``0.1 + 0.5*(-0.15) = 0.025`` -> the 1.25%/98.75% residual quantiles,
    i.e. a substantially wider, self-correcting interval.
    """
    if live_coverage is None or n < ACI_MIN_N:
        return alpha
    return float(np.clip(alpha + ACI_GAIN * (float(live_coverage) - target),
                         ACI_MIN_ALPHA, ACI_MAX_ALPHA))


# --- coverage evidence -------------------------------------------------------
# A coverage RATE is meaningless without the denominator that produced it, and
# below a certain denominator it is meaningless with it too.
#
# A nominal 90% band expects one miss every ten scored points. So "coverage =
# 1.0" over a handful of points is not weak evidence that the band is wide —
# it is no evidence at all about the level: at 11 scored points a perfectly
# calibrated 90% band produces zero misses with probability 0.9**11 = 0.31,
# i.e. roughly one run in three. Production run 37 published
# ``interval_coverage = 1.0`` for XAUUSD 1d off exactly ONE scored fold.
#
# MIN_SCORED_FOR_COVERAGE is the smallest denominator at which a rate may be
# published at all. At 20 points two misses are expected and P(zero | truly
# 90%) falls to 0.9**20 = 0.12, so a perfect score is at least an unusual
# outcome rather than a routine one; the 95% Wilson interval around an
# observed 0.90 at n=20 is [0.70, 0.97] — still wide, but a measurement
# instead of a coin flip. It is deliberately the same bar the live loops
# already use before they trust a coverage number (predicting.MIN_COVERAGE_N,
# ACI_MIN_N, Go's models.minCoverageN), so the whole system has ONE evidence
# threshold for coverage.
#
# A bar this high obliges the CALLER to bring a fold set that can clear it.
# When the published block was scored from the ~12-fold holdout tail it never
# could — 2 scored folds against 20, for every candidate at every horizon —
# which is a plumbing failure, not a reason to lower the bar. See
# ``training.fold_metrics``.
MIN_SCORED_FOR_COVERAGE = 20


@dataclass(frozen=True)
class CoverageEvidence:
    """A coverage measurement that cannot be read without its denominator.

    Deliberately not a float: the caller has to reach for :attr:`rate` (which
    withholds a rate that no denominator supports) or for the raw
    ``hits``/``scored`` pair, and cannot accidentally publish a bare number.

    ``offered`` and ``warmup`` exist because ``scored`` is smaller than the
    number of folds and the gap needs explaining rather than discovering: the
    first ``warmup`` folds only build the residual pool, so a 40-fold
    validation scores 30 and an 11-fold one scores 1.
    """

    hits: int
    scored: int
    min_scored: int = MIN_SCORED_FOR_COVERAGE
    offered: int = 0     # points handed to the scorer (folds, matured rows, ...)
    warmup: int = 0      # of those, the ones that only fed the residual pool

    @property
    def sufficient(self) -> bool:
        """Is the denominator large enough to say anything about the level?"""
        return self.scored >= self.min_scored

    @property
    def observed_rate(self) -> Optional[float]:
        """What actually happened, however thin — for diagnostics, not display."""
        return self.hits / self.scored if self.scored else None

    @property
    def rate(self) -> Optional[float]:
        """The publishable rate; ``None`` unless the evidence supports one."""
        return self.observed_rate if self.sufficient else None

    @property
    def status(self) -> str:
        if not self.scored:
            return "not_scored"
        return "measured" if self.sufficient else "insufficient_evidence"

    def as_published(self) -> dict:
        """The stored/JSON form: a rate that always travels with its numbers.

        A nested block, not flat sibling keys, so that no consumer can pick up
        a bare ``interval_coverage`` float and attribute it to whichever fold
        set the neighbouring metrics were computed on.
        """
        return {
            "rate": self.rate,
            "hits": self.hits,
            "scored_folds": self.scored,
            "total_folds": self.offered,
            "residual_warmup_folds": self.warmup,
            "min_scored_folds": self.min_scored,
            "status": self.status,
        }


def coverage(
    actuals: Sequence[float], intervals: Sequence[tuple[float, float]]
) -> Optional[float]:
    """Fraction of actuals inside their intervals; None when nothing scored."""
    pairs = list(zip(actuals, intervals))
    if not pairs:
        return None
    hits = sum(1 for a, (lo, hi) in pairs if lo <= float(a) <= hi)
    return hits / len(pairs)


def walk_forward_coverage(
    preds: Sequence[float],
    actuals: Sequence[float],
    alpha: float = DEFAULT_ALPHA,
    min_history: int = 10,
    min_scored: int = MIN_SCORED_FOR_COVERAGE,
) -> CoverageEvidence:
    """Coverage where each fold's interval uses only residuals of PRIOR folds
    (no peeking), mirroring how intervals are used in production.

    Returns a :class:`CoverageEvidence`, NOT a bare rate. The distinction
    matters because the number of scored folds is *not* the number of folds:
    the first ``min_history`` folds only contribute residuals, so a 11-fold
    validation scores ONE fold. Callers that published the bare float were
    reporting "100% interval coverage" off a single observation.

    Corollary for callers: this needs a LONG fold set. Handed a ~12-fold
    holdout tail it can score at most 2 folds and will never clear
    ``min_scored`` — which is why the published measurement runs over the full
    walk-forward fold set (see ``training.fold_metrics``).
    """
    residuals: list[float] = []
    hits = 0
    total = 0
    offered = 0
    for p, a in zip(preds, actuals):
        offered += 1
        if len(residuals) >= min_history:
            lo, hi = empirical_interval(float(p), residuals, alpha)
            total += 1
            if lo <= float(a) <= hi:
                hits += 1
        if p != 0:
            residuals.append((float(a) - float(p)) / float(p))
    return CoverageEvidence(hits=hits, scored=total, min_scored=min_scored,
                            offered=offered, warmup=offered - total)
