"""Empirical (conformal-style) prediction intervals.

Residuals are collected during walk-forward validation as *relative* errors
``(actual - pred) / pred``.  An interval around a new point forecast is built
from the empirical residual quantiles, so the nominal coverage is honest with
respect to the validation distribution.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np

DEFAULT_ALPHA = 0.1  # 90% nominal coverage


def relative_residuals(preds: Sequence[float], actuals: Sequence[float]) -> list[float]:
    out: list[float] = []
    for p, a in zip(preds, actuals):
        if p != 0:
            out.append((float(a) - float(p)) / float(p))
    return out


def empirical_interval(
    point: float, residuals: Sequence[float], alpha: float = DEFAULT_ALPHA
) -> tuple[float, float]:
    """(lower, upper) around ``point`` from empirical residual quantiles.

    With too few residuals a conservative ±5% band is used.
    """
    res = np.asarray(list(residuals), dtype=float)
    if res.size < 5:
        lo_q, hi_q = -0.05, 0.05
    else:
        lo_q = float(np.quantile(res, alpha / 2.0))
        hi_q = float(np.quantile(res, 1.0 - alpha / 2.0))
    lower = point * (1.0 + lo_q)
    upper = point * (1.0 + hi_q)
    if lower > upper:  # degenerate residuals
        lower, upper = upper, lower
    return lower, upper


# --- adaptive conformal (ACI-style, batch form) ------------------------------
# Adaptive Conformal Inference (Gibbs & Candès 2021; arXiv:2202.07282) adjusts
# the miscoverage level alpha online: intervals that under-cover get a smaller
# effective alpha (wider quantiles), over-covering ones a larger alpha
# (tighter quantiles).  We run the batch analogue driven by the live coverage
# statistics the evaluate job already maintains.
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


def coverage(
    actuals: Sequence[float], intervals: Sequence[tuple[float, float]]
) -> float:
    """Fraction of actuals inside their intervals."""
    pairs = list(zip(actuals, intervals))
    if not pairs:
        return 0.0
    hits = sum(1 for a, (lo, hi) in pairs if lo <= float(a) <= hi)
    return hits / len(pairs)


def walk_forward_coverage(
    preds: Sequence[float],
    actuals: Sequence[float],
    alpha: float = DEFAULT_ALPHA,
    min_history: int = 10,
) -> float:
    """Coverage where each fold's interval uses only residuals of PRIOR folds
    (no peeking), mirroring how intervals are used in production."""
    residuals: list[float] = []
    hits = 0
    total = 0
    for p, a in zip(preds, actuals):
        if len(residuals) >= min_history:
            lo, hi = empirical_interval(float(p), residuals, alpha)
            total += 1
            if lo <= float(a) <= hi:
                hits += 1
        if p != 0:
            residuals.append((float(a) - float(p)) / float(p))
    return hits / total if total else 0.0
