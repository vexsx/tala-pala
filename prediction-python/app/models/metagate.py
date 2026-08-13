"""Meta-labeling gate: the system learns to judge its own forecasts.

López de Prado's meta-labeling idea (Advances in Financial Machine Learning,
2018): keep the primary model for *direction*, and train a secondary
classifier on realized outcomes to predict *whether the primary call will be
right*. Here the training data is the app's own ``predictions`` table — every
matured prediction is one labeled example (features known at prediction time,
label = did the direction call hit).

The evaluate job refits the gate as outcomes accumulate and stores it as
plain coefficients in ``app_settings['meta_gate']``; the prediction pass
applies it with numpy only (no sklearn needed at inference). Every part is
causal: features are those stored ON the prediction row, the label arrives
strictly later.
"""
from __future__ import annotations

import logging
from typing import NamedTuple, Optional

import numpy as np
from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..db import predictions, utcnow

log = logging.getLogger(__name__)

META_GATE_KEY = "meta_gate"
MIN_SAMPLES = 40          # matured, non-flat predictions before the gate exists
MAX_SAMPLES = 500         # most recent examples used for the refit
# lbfgs budget, and the number the convergence check reads back: sklearn emits
# its ConvergenceWarning exactly when n_iter_ reaches this, so the two must be
# the same constant or the check silently stops matching the warning.
MAX_ITER = 1000
REGIMES = ("trending_up", "trending_down", "ranging", "high_volatility")
HORIZON_DAYS = {"1h": 1 / 24, "4h": 4 / 24, "eod": 1.0, "1d": 1.0,
                "3d": 3.0, "7d": 7.0, "30d": 30.0}

PRIMARY_SYMBOL = "IR_GOLD_18K"

FEATURE_NAMES = (
    "rel_width",        # (upper - lower) / point — model's own uncertainty
    "abs_expected_pct", # size of the predicted move
    "confidence",       # pre-gate confidence stored at prediction time
    "log_horizon_days", # horizon scale
    "data_fresh",       # was input data fresh
    "is_global",        # symbol != IR_GOLD_18K: the two instruments have
                        # different hit-rate structure; pooling them without
                        # this feature bled one's calibration into the other
    *(f"regime_{r}" for r in REGIMES),
)

# Indicator (0/1) features. Their support is not a distance question — a level
# is either one the gate saw examples of or it is not — so they are checked by
# COUNT rather than by standard deviations (see :func:`_support`).
INDICATOR_FEATURES = frozenset({"data_fresh", "is_global", *(f"regime_{r}" for r in REGIMES)})

# ``log_horizon_days`` is not continuous either, and this is the feature the
# whole gate turns on. Training rows can only ever carry the seven horizons the
# scheduler emits (1h/4h/eod/1d/3d/7d/30d — four distinct day counts once eod
# and 1d collapse), while ``/internal/predict/custom`` accepts ANY integer day
# count in 1..90. So the column is a handful of spikes with wide empty gaps,
# and a marginal-Gaussian |z| cannot see the gaps: measured on a real 500-row
# fit, 10d (ZERO training rows) scored |z| = 1.62 and was accepted, while 30d
# (31 rows — the only evidence that exists out there) scored 3.35 and was
# refused. The check was anti-correlated with actual support.
#
# It is therefore checked by EVIDENCE: :func:`fit_meta_gate` persists the row
# count behind every horizon it trained on, and a horizon with fewer than
# GATE_MIN_LEVEL_ROWS rows is not scored at all. It gets no neighbour fallback,
# for the same reason the indicator levels get none: a 20-day forecast graded
# against 30-day outcomes is not a measurement of 20-day skill. See the module
# note on the evidence boundary below.
COUNT_CHECKED_FEATURES = INDICATOR_FEATURES | {"log_horizon_days"}

# Human-readable names for the driver text: a user reading "declined to score"
# should not have to know the feature vector's column names.
FEATURE_LABELS = {
    "rel_width": "this interval width",
    "abs_expected_pct": "a move of this size",
    "confidence": "this confidence level",
    "log_horizon_days": "this forecast horizon",
    "data_fresh": "this data-freshness state",
    "is_global": "this instrument",
    **{f"regime_{r}": f"the {r.replace('_', ' ')} regime" for r in REGIMES},
}

# --- applicability domain ----------------------------------------------------
# A logistic regression is linear in the standardized features and unbounded.
# Nothing in it knows where the training cloud ended, so a point far outside
# that cloud gets an answer that is confident and entirely extrapolated.
#
# Measured on the live IR_GOLD_18K 30d forecast: rel_width sat 5.6 SD and
# log_horizon_days 2.9 SD above the training means. That second reading turned
# out to mean nothing at all (see COUNT_CHECKED_FEATURES: an SD on a spiky
# discrete column measures nothing), but the first stands — a 30d forecast
# carries a far wider interval than anything the gate was fitted on.
# The gate reported p_hit = 0.055 while its own training pool hit
# 57.5% of the time, and 0.5*confidence + 0.5*p_hit shipped that to the user
# plus a "below coin-flip reliability" warning. Independently, this system's
# real direction hit rate is 54% on non-flat calls. A 5% estimate was not a
# reading of skill; it was the linear logit run off the end of its data.
#
# The check below needs no new training data: per-feature mean/std/n and the
# per-horizon row counts all come out of the same fit. On the continuous
# features it is symmetric in |z|, so an implausibly NARROW interval is exactly
# as out-of-support as a wide one; on the discrete ones (indicator levels and
# the horizon) it counts rows, because "how far" is not a question those
# columns can answer.
#
# Thresholds. For the genuinely continuous features (rel_width,
# abs_expected_pct, confidence), |z| > 3 is a region a few-hundred-row fit has
# essentially no data in (~0.3% of a normal's mass — under one expected row at
# n=233), so the logit there is pure extrapolation and the gate refuses.
# Between 2 and 3 SD the fit is thin but real: the score is published AS THE
# MODEL COMPUTED IT and flagged ``thin_support``.
#
# There used to be a shrink here — ``(1-s)*p + s*base_rate`` with
# ``s = (|z|-2)/(3-2)`` — and it was worse than the cliff it smoothed. At
# |z| = 2.996 the returned number was 99.6% the base rate: a constant carrying
# nothing about the forecast, yet published as "scored" with a p_hit and
# blended 50/50 into shipped confidence. Measured on the production gate, it
# turned a raw 0.076 into 0.525 — it MANUFACTURED confidence at exactly the
# point where the evidence had failed, and one more day of horizon then flipped
# it to None. A support failure must be visible, never silently substituted.
#
# Limitation worth stating: only marginals are stored, so this cannot detect a
# point that is unremarkable on every feature yet sits in a combination that
# never occurred.
GATE_THIN_Z = 2.0          # beyond this, the score is flagged as thinly supported
GATE_MAX_Z = 3.0           # beyond this, refuse to score at all
# A discrete LEVEL (an indicator level, or one horizon) backed by fewer rows
# than this is not something the gate measured — same one-in-ten scale used for
# coverage evidence elsewhere.
GATE_MIN_LEVEL_ROWS = 10

# --- the evidence boundary, and why there is a step at it ---------------------
# The gate scores a horizon it has evidence for and declines one it does not.
# With evidence at 1/3/7/30 days that means four scored day counts out of the
# ninety ``/internal/predict/custom`` accepts, and shipped confidence therefore
# CHANGES between a scored day and a declined one — 7d is gate-blended, 8d is
# not. That step is intended. It is the point at which this system stops having
# a measurement of its own skill, and moving the number there is the only honest
# thing to do with that fact.
#
# A previous round tried to smooth it by BORROWING the nearest evidenced horizon
# within a 1.5x radius. That is deleted, on evidence:
#
# * it did not remove the step, it moved it to the borrow radius, and made the
#   largest one there WORSE — measured 0.060 across 19d -> 20d against a project
#   tolerance of 0.05, flipping the published self-assessment from a refusal to
#   a 0.89 endorsement on one extra day of horizon;
# * and it bought that smoothness with a partly fictional claim. A 20-day
#   forecast scored on 30-day outcomes is not a measurement of 20-day skill,
#   however prominently the substitution is disclosed.
#
# So there is no fallback. What the gate publishes about a horizon is either
# backed by that horizon's own matured predictions or it is a refusal.


class GateVerdict(NamedTuple):
    """What the gate did, and why — so a refusal can be shown, not swallowed.

    ``p_hit`` is None whenever the gate must not touch confidence; ``status``
    distinguishes the reasons a reader cares about:

    * ``scored``          — a usable probability, exactly as the fitted model
      computed it (see ``thin_support`` for how well backed it is);
    * ``out_of_support``  — the gate declined: this point is outside the data
      it was fitted on, where its answer would be extrapolation;
    * ``untrained``       — no gate exists yet;
    * ``unusable``        — a gate exists but cannot score this vector (stale
      feature set / schema, malformed coefficients, zero point forecast).

    ``thin_support`` marks a score whose worst feature sits between
    :data:`GATE_THIN_Z` and :data:`GATE_MAX_Z` — real evidence, but little of
    it. It is a published FACT ABOUT THE FORECAST, never an adjustment folded
    into ``p_hit``: every caller can see it and say so.
    """

    p_hit: Optional[float]
    status: str
    max_abs_z: Optional[float] = None
    worst_feature: Optional[str] = None
    thin_support: bool = False


def _horizon_days(horizon: str) -> Optional[float]:
    """Days ahead for a scheduled label ('3d', 'eod') or a custom '12d'."""
    days = HORIZON_DAYS.get(horizon)
    if days is None:
        try:
            days = float(str(horizon).rstrip("d"))
        except ValueError:
            return None
    return float(days) if days > 0 else None  # log(0) is not a feature value


def horizon_key(days: float) -> str:
    """Stable key for the per-horizon row counts persisted with the gate.

    Keyed by DAYS, not by label: 'eod' and '1d' are the same one-day horizon
    and their evidence pools, while a custom '10d' must be able to look itself
    up without the scheduler ever having named it.
    """
    return f"{float(days):.6f}"


def evidence_key(symbol: str, days: float) -> str:
    """Evidence key, scoped to the INSTRUMENT as well as the horizon.

    Pooling the count across symbols let one instrument license another's
    horizon: with 60 matured XAUUSD 30d rows and none of its own, a 30-day
    IR_GOLD_18K request was scored on XAUUSD's outcomes and moved shipped
    confidence 0.825 -> 0.719. Worse, live calibration IS per-symbol, so that
    request was simultaneously "gate scored" and "not calibrated" — which
    handed the gate an UNBLENDED confidence, re-creating the very scale
    mismatch the blend was introduced to remove. Scoping the count restores
    the invariant: the gate scores a horizon only when THIS instrument has
    matured predictions at it, and a calibration block therefore exists.
    """
    return f"{symbol}|{horizon_key(days)}"


def _evidenced_rows(horizon_rows: dict, days: Optional[float], symbol: str,
                    min_rows: int = GATE_MIN_LEVEL_ROWS) -> bool:
    """Does this symbol have >= ``min_rows`` training rows at this horizon?"""
    if days is None:
        return False
    rows = horizon_rows.get(evidence_key(symbol, days))
    return isinstance(rows, (int, float)) and int(rows) >= min_rows


def _support(
    feats: list[float], mean: np.ndarray, std: np.ndarray, n_train: int,
    horizon_rows: dict, days: Optional[float], symbol: str,
) -> tuple[float, Optional[str]]:
    """``(max_abs_z, worst_feature)`` — how far outside the fit this point is.

    Continuous features are scored in standard deviations. A feature that was
    CONSTANT in training had its std floored to 1.0 by :func:`fit_meta_gate`,
    which is the right scale here anyway: |x - mean| is then exactly the units
    the logit contribution is computed in, so a deviation that cannot move the
    output does not trigger a refusal.

    Features that are DISCRETE in training are checked by COUNT instead (see
    :data:`COUNT_CHECKED_FEATURES`): their support is not a distance question,
    and a z-rule silently accepts levels the gate has zero examples of. ``inf``
    puts "never seen" on the same scale as an unreachable z.

    That includes the horizon, and it takes no neighbour: a day count with
    fewer than :data:`GATE_MIN_LEVEL_ROWS` matured predictions of its own is
    refused outright, exactly like an unseen regime or instrument.
    """
    worst_z = 0.0
    worst_name: Optional[str] = None
    for i, name in enumerate(FEATURE_NAMES):
        x = float(feats[i])
        if name not in COUNT_CHECKED_FEATURES:
            z = abs((x - float(mean[i])) / float(std[i]))
        elif name == "log_horizon_days":
            # evidence, not distance: how many training rows had THIS horizon
            if _evidenced_rows(horizon_rows, days, symbol):
                continue
            z = float("inf")
        else:  # indicator level
            if n_train <= 0:
                continue  # gate predates the stored sample count
            # mean of a 0/1 column IS the share of rows at level 1
            share = float(mean[i]) if x >= 0.5 else 1.0 - float(mean[i])
            if share * n_train >= GATE_MIN_LEVEL_ROWS:
                continue
            z = float("inf")
        if z > worst_z:
            worst_z, worst_name = z, name
    return worst_z, worst_name


def _row_features(
    point: float, lower: float, upper: float, expected_pct: float,
    confidence: float, horizon: str, regime: str, data_fresh: bool,
    symbol: str = PRIMARY_SYMBOL,
) -> Optional[list[float]]:
    if point == 0:
        return None
    days = _horizon_days(horizon)
    if days is None:
        return None
    feats = [
        (upper - lower) / abs(point),
        abs(expected_pct),
        float(confidence),
        float(np.log(days)),
        1.0 if data_fresh else 0.0,
        0.0 if symbol == PRIMARY_SYMBOL else 1.0,
    ]
    feats.extend(1.0 if regime == r else 0.0 for r in REGIMES)
    return feats


# --- the fitted design has to have full column rank --------------------------
# ``FEATURE_NAMES`` is a good vector to PUBLISH and a bad matrix to FIT. Two
# things in it are structurally degenerate, in every refit, by construction:
#
# * the four ``regime_*`` columns are a COMPLETE one-hot with no reference
#   level. Whenever every training row carries a regime in :data:`REGIMES` they
#   sum to 1, so after standardization ``sum_j std_j * z_ij == 0`` for every row
#   i — the classic dummy-variable trap against the intercept sklearn fits
#   separately. Measured on a 500-row production-shaped fit: the augmented
#   design (10 features + intercept) has rank 10 of 11, sigma_max 34.6 vs
#   sigma_min 6.7e-15, i.e. singular to machine precision.
# * ``data_fresh`` is TRUE on essentially every row (a stale-input prediction is
#   rare), and ``is_global`` is constant whenever only one instrument has
#   matured rows in the window. :func:`fit_meta_gate` floors their std to 1.0,
#   which turns them into columns of exact zeros — no information, but still a
#   parameter, and another direction the log-likelihood is flat in. With both,
#   the measured rank falls to 9 of 11.
#
# A flat direction is not fatal to the OPTIMUM — the L2 prior is what makes it
# unique — but it is what the optimizer's conditioning is left standing on: the
# curvature there comes only from the penalty while every informative direction
# carries ~n times more, and lbfgs's stopping rule is an ABSOLUTE gradient bound
# (``gtol = tol = 1e-4``) on a gradient whose informative components scale with
# n. That is the shape of a fit that runs out of iterations, and raising
# max_iter does not address it: across every configuration measured here a fit
# that converges does so in under 30 iterations, so one that needs more than a
# thousand is stuck, not slow.
#
# Stated honestly: the singular design is measured, the production STALL is not
# reproduced here — it needs the pinned scikit-learn (1.5.2, requirements.txt),
# and no interpreter on the dev machine can install it. What is reproduced is
# that separation is not the explanation (complete separation on this same
# design converges in 10-16 iterations, quasi-separation on a rare indicator
# level in 17) and that iterations are not the explanation either (raising the
# limit to 100_000 changes n_iter_ by zero). The degenerate design is the one
# candidate cause that is present in every refit, and it is a defect whether or
# not it is the whole of that warning.
#
# So the fit runs on an independent SUBSET of the columns and the coefficients
# are scattered back into a full-length vector, zero in the dropped slots. That
# keeps the stored gate's shape, ``score_meta_gate``'s dot product and every
# applicability check (which read mean/std/n, not coef) exactly as they were: a
# dropped column was constant or redundant in training, and its own contribution
# to the logit is zero either way.
_RANK_TOL = 1e-8


def _independent_columns(Xs: np.ndarray) -> list[int]:
    """Column indices that add rank to ``[1 | Xs]``, scanned left to right.

    Modified Gram-Schmidt against a basis seeded with the intercept, because
    the intercept is a fitted (and unpenalized) parameter: a complete one-hot
    is redundant *against it*, not against the other features alone.

    Left-to-right and greedy on purpose. The choice must be a function of the
    feature ORDER, not of the data, or successive refits would drop different
    members of the same collinear group and the stored coefficients would jump
    between hours without the model having learned anything.
    """
    n_rows = Xs.shape[0]
    basis = [np.full(n_rows, 1.0 / np.sqrt(n_rows))]
    keep: list[int] = []
    for j in range(Xs.shape[1]):
        col = np.asarray(Xs[:, j], dtype=float)
        norm = float(np.linalg.norm(col))
        if norm <= 0.0:
            continue  # constant in training: standardized to exact zeros
        resid = col.copy()
        for _ in range(2):  # twice-is-enough re-orthogonalization
            for vec in basis:
                resid -= float(resid @ vec) * vec
        length = float(np.linalg.norm(resid))
        if length <= _RANK_TOL * norm:
            continue  # a linear combination of the intercept and kept columns
        basis.append(resid / length)
        keep.append(j)
    return keep


def _recover_base(point: float, expected_change_pct: float) -> Optional[float]:
    if expected_change_pct <= -100:
        return None
    base = point / (1.0 + expected_change_pct / 100.0)
    return base if base else None


def fit_meta_gate(engine: Engine) -> Optional[dict]:
    """Fit the gate on matured predictions; returns the storable dict or None.

    Flat calls (predicted direction 'flat') are excluded — the gate scores
    directional conviction, and a flat call has none to score.
    """
    stmt = (
        select(
            predictions.c.point_forecast, predictions.c.lower_bound,
            predictions.c.upper_bound, predictions.c.expected_change_pct,
            predictions.c.confidence, predictions.c.raw_confidence,
            predictions.c.horizon,
            predictions.c.regime, predictions.c.data_fresh,
            predictions.c.direction, predictions.c.actual_value,
            predictions.c.symbol,
        )
        .where(predictions.c.actual_value.is_not(None))
        .order_by(predictions.c.target_time.desc())
        .limit(MAX_SAMPLES)
    )
    with engine.connect() as conn:
        rows = conn.execute(stmt).all()

    X: list[list[float]] = []
    y: list[int] = []
    horizon_rows: dict[str, int] = {}
    for (point, lower, upper, exp_pct, conf, raw_conf, horizon, regime, fresh,
         direction, actual, symbol) in rows:
        if direction == "flat":
            continue
        point = float(point)
        base = _recover_base(point, float(exp_pct))
        if base is None:
            continue
        # Train on the PRE-gate confidence: the stored blended value contains
        # the previous gate's own output (self-reference). Old rows without
        # raw_confidence fall back to the blended value.
        conf_feature = float(raw_conf) if raw_conf is not None else float(conf)
        feats = _row_features(point, float(lower), float(upper), float(exp_pct),
                              conf_feature, str(horizon), str(regime), bool(fresh),
                              str(symbol))
        if feats is None:
            continue
        pred_sign = np.sign(point - base)
        real_sign = np.sign(float(actual) - base)
        if pred_sign == 0:
            continue
        X.append(feats)
        y.append(1 if pred_sign == real_sign else 0)
        # count the horizon evidence on exactly the rows that entered the fit
        days = _horizon_days(str(horizon))
        if days is not None:
            key = evidence_key(str(symbol), days)
            horizon_rows[key] = horizon_rows.get(key, 0) + 1

    if len(y) < MIN_SAMPLES or len(set(y)) < 2:
        return None  # not enough evidence (or degenerate labels) yet

    from sklearn.linear_model import LogisticRegression

    Xa = np.asarray(X, dtype=float)
    mean = Xa.mean(axis=0)
    std = Xa.std(axis=0)
    # floor, not ==0: near-constant features would round to a stored std of 0
    # and divide-by-zero at apply time
    std[std < 1e-6] = 1.0
    Xs = (Xa - mean) / std

    keep = _independent_columns(Xs)
    if not keep:
        # Every column constant: no fit to be had (and nothing to score with).
        log.warning("meta_gate refit skipped: all %d features were constant "
                    "across %d training rows", len(FEATURE_NAMES), len(y))
        return None
    clf = LogisticRegression(C=1.0, max_iter=MAX_ITER)
    clf.fit(Xs[:, keep], np.asarray(y))

    # Refuse to SHIP a non-converged fit. sklearn only warns, and a warning
    # leaves half-optimized coefficients in app_settings — which the prediction
    # pass then blends 50/50 into user-visible confidence. Returning None keeps
    # whatever gate is already stored (jobs/evaluate.py upserts only a truthy
    # result), so the published self-assessment stays one the optimizer
    # actually finished, and the reason is in the Issues tab rather than in a
    # library warning nobody reads.
    iterations = int(np.max(clf.n_iter_))
    if iterations >= MAX_ITER:
        log.warning(
            "meta_gate refit did not converge (%d lbfgs iterations at the "
            "%d limit) on %d rows, %d of %d columns independent; keeping the "
            "previously stored gate",
            iterations, MAX_ITER, len(y), len(keep), len(FEATURE_NAMES),
        )
        return None

    # Scatter back: a dropped column contributes exactly zero to the logit.
    coef = np.zeros(len(FEATURE_NAMES), dtype=float)
    coef[keep] = clf.coef_[0]
    return {
        "feature_names": list(FEATURE_NAMES),
        "mean": [round(float(v), 8) for v in mean],
        "std": [round(float(v), 8) for v in std],
        "coef": [round(float(v), 8) for v in coef],
        "intercept": round(float(clf.intercept_[0]), 8),
        "n": int(len(y)),
        "base_rate": round(float(np.mean(y)), 4),
        # what the gate actually SAW per horizon, keyed by days (see
        # :func:`horizon_key`). This is the applicability evidence for the one
        # feature that is discrete in training and continuous at request time;
        # without it the gate cannot tell 10d (no examples) from 30d (31).
        "horizon_evidence": dict(sorted(horizon_rows.items())),
        "trained_at": utcnow().isoformat(),
    }


def score_meta_gate(
    gate: Optional[dict],
    point: float, lower: float, upper: float, expected_pct: float,
    confidence: float, horizon: str, regime: str, data_fresh: bool,
    symbol: str = PRIMARY_SYMBOL,
) -> GateVerdict:
    """P(direction call is right), or an explained refusal.

    The gate only answers inside the applicability domain it was fitted on
    (see the constants above). Outside it the verdict is
    ``out_of_support`` with ``p_hit=None`` — a refusal, not a clamped number
    and not a substituted one: squeezing an extrapolated 5% up into a nicer
    range, or replacing it with the base rate, would hide the fact that the
    model has no information here.

    Inside the domain the returned ``p_hit`` is always exactly the fitted
    model's own output. Nothing is blended into it.
    """
    if not gate:
        return GateVerdict(None, "untrained")
    feats = _row_features(point, lower, upper, expected_pct, confidence,
                          horizon, regime, data_fresh, symbol)
    if feats is None:
        return GateVerdict(None, "unusable")
    # A gate persisted before a feature-set change cannot score the new
    # vector; stay silent until the evaluate job refits it.
    stored_names = gate.get("feature_names")
    if stored_names is not None and list(stored_names) != list(FEATURE_NAMES):
        return GateVerdict(None, "unusable")
    # Same rule for the schema: a gate stored without its per-horizon evidence
    # cannot have its applicability checked on the feature that matters most,
    # and guessing (the old marginal z) is what shipped scores for horizons
    # with no examples. Silent until the next refit.
    # Read the SYMBOL-SCOPED key. A gate stored before scoping carries the old
    # flat "horizon_rows" and its counts cannot be attributed to an instrument,
    # so it is unusable rather than reinterpreted — silent until the hourly
    # evaluate job refits it, which is the safe direction.
    horizon_rows = gate.get("horizon_evidence")
    if not isinstance(horizon_rows, dict):
        return GateVerdict(None, "unusable")
    try:
        mean = np.asarray(gate["mean"], dtype=float)
        std = np.asarray(gate["std"], dtype=float)
        std = np.where(std <= 0, 1.0, std)  # defensive: stored gates predate the floor
        coef = np.asarray(gate["coef"], dtype=float)
        intercept = float(gate["intercept"])
        n_train = int(gate.get("n") or 0)
        # A malformed/truncated gate must be silent, not an IndexError three
        # frames down inside the support loop (which took the whole prediction
        # run with it).
        if not (mean.size == std.size == coef.size == len(FEATURE_NAMES)):
            return GateVerdict(None, "unusable")

        days = _horizon_days(horizon)
        max_abs_z, worst = _support(feats, mean, std, n_train, horizon_rows, days, symbol)
        if max_abs_z > GATE_MAX_Z:
            return GateVerdict(None, "out_of_support", max_abs_z, worst)

        x = (np.asarray(feats, dtype=float) - mean) / std
        z = float(np.dot(coef, x) + intercept)
        p = float(1.0 / (1.0 + np.exp(-z)))
        return GateVerdict(p, "scored", max_abs_z, worst,
                           thin_support=max_abs_z > GATE_THIN_Z)
    except (KeyError, IndexError, ValueError, TypeError):
        return GateVerdict(None, "unusable")


def apply_meta_gate(
    gate: Optional[dict],
    point: float, lower: float, upper: float, expected_pct: float,
    confidence: float, horizon: str, regime: str, data_fresh: bool,
    symbol: str = PRIMARY_SYMBOL,
) -> Optional[float]:
    """P(direction call is right) from the stored gate; None when it declines.

    Thin wrapper over :func:`score_meta_gate`, kept for tests and ad-hoc
    inspection. NOT for prediction paths: a bare float cannot carry the reason
    the gate went quiet, and a caller holding one has nothing to publish. Both
    shipping paths (``predicting._predict_one`` and ``custom.predict_custom``)
    take the verdict and emit ``_gate_driver`` for every outcome — custom.py
    used this wrapper and shipped a gate-adjusted confidence with no driver
    at all.
    """
    return score_meta_gate(gate, point, lower, upper, expected_pct, confidence,
                           horizon, regime, data_fresh, symbol).p_hit
