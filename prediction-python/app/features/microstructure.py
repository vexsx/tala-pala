"""Point-in-time microstructure features from already-stored observations.

Every collected datapoint lands in ``raw_observations`` with the provider's
own payload attached, and that payload carries market detail no model reads
yet: Hamrah Gold's two-sided dealer quote (``sell_rial``, ``buy_rial``,
``spread_pct``, ``sides``), BitMax's USDT snapshot (``price_in_usd``,
``change``, ``volume_24h_irt``) and the TSETMC fund rows.  Alongside the
payloads, the *arrival pattern* of the observations themselves is data: how
often a source refreshes, how long it has been silent, how violently the
quote moved between two polls.

This module turns both into features.  It is deliberately pure — no engine,
no HTTP, no config read, no logging setup — so importing it has zero side
effects and every function is testable from a synthetic frame.  Wiring into
training happens in a later change, after ablation.

Leakage policy (the reason this lives in its own module):

* every builder is strictly backward-looking — trailing ``rolling`` windows
  end at the current row, ``shift``/``diff`` are positive, and forward-fill
  propagates only the past.  There is no centred window, no full-sample
  statistic and no negative shift anywhere below;
* :func:`asof_join` is the only supported way to put these features on
  another timeline.  It is a backward as-of join, so the row at ``t`` can
  only ever carry a feature row observed at or before ``t``; a caller cannot
  reach the future even by accident;
* :func:`build_microstructure_frame` accepts an ``as_of`` and re-uses
  ``features.engineering.assert_no_future`` as the hard guard on its input;
* every column is described by a module-level :class:`FeatureSpec` in
  :data:`FEATURE_REGISTRY` — provider, symbol, lookback, missing-value
  policy and staleness threshold — and :func:`asof_join` *applies* that
  policy, so the registry is executable documentation rather than comments.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, Union

import numpy as np
import pandas as pd

from ..core.normalize import rial_to_toman
from .engineering import LeakageError, assert_no_future

__all__ = [
    "FeatureSpec",
    "PremiumDecomposition",
    "FEATURE_REGISTRY",
    "STREAM_FEATURES",
    "DECOMPOSITION_FEATURES",
    "registry_table",
    "LeakageError",
    "POLICY_FFILL",
    "POLICY_NAN_ON_GAP",
    "AGE_COLUMN",
    "normalize_observations",
    "build_microstructure_frame",
    "unavailable_features",
    "asof_join",
    "decompose_move",
    "decompose_premium_move",
    "align_causal",
]

ObservationsLike = Union[pd.DataFrame, Sequence[Mapping[str, Any]]]
TimestampsLike = Union[pd.DatetimeIndex, pd.Series, Sequence[datetime]]

# Default binding: Hamrah Gold is the only Iranian source that publishes both
# quote sides, so it is the only stream the spread block can be built from.
PRIMARY_PROVIDER = "hamrahgold"
PRIMARY_SYMBOL = "IR_GOLD_18K"
DERIVED_PROVIDER = "derived"  # composed from several canonical series

# Payload keys, exactly as the providers write them (app/providers/*.py).
SPREAD_PCT_KEY = "spread_pct"
SELL_RIAL_KEY = "sell_rial"
BUY_RIAL_KEY = "buy_rial"
# Cross-provider dispersion is being added to the payload by another change;
# until it lands the features below degrade to NaN (never an exception).
DISPERSION_KEY = "peer_dispersion"
# The shape of that key is not fixed yet, so accept a bare number or a small
# mapping and look for the obvious percent field inside it.
DISPERSION_VALUE_KEYS = ("pct", "dispersion_pct", "gap_pct", "value")

# Windows are module constants (not call arguments) so that column names and
# registry entries can never drift apart. Counts are in OBSERVATIONS: at the
# 10-minute collect cadence 60 observations is roughly half a day.
SPREAD_WINDOW = 60
SPREAD_CHG_LAG = 5
SPREAD_MEAN_WINDOW = 20
DISPERSION_WINDOW = 60
GAP_WINDOW = 20
FREQ_WINDOW_HOURS = (6, 24)
RV_SHORT = 12
RV_LONG = 48
JUMP_WINDOW = 48
JUMP_SIGMA = 4.0
# median(|x|) -> sigma for a normal; robust to the very jumps we are hunting
MAD_TO_SIGMA = 1.4826
PREMIUM_DECOMP_LAG = 1

# Missing-value policies, applied by asof_join().
POLICY_FFILL = "ffill_within_staleness"  # carry forward, NaN past the threshold
POLICY_NAN_ON_GAP = "nan_on_gap"         # valid only on an exact timestamp match

_HOUR = 3600
SPREAD_STALENESS_S = 3 * _HOUR   # a half-day-old dealer spread is not today's
CADENCE_STALENESS_S = 1 * _HOUR
VOL_STALENESS_S = 1 * _HOUR
DECOMP_STALENESS_S = 24 * _HOUR  # decomposition runs on a daily/close series

AGE_COLUMN = "feature_age_seconds"

FEATURE_VERSION = "1.0.0"


@dataclass(frozen=True)
class FeatureSpec:
    """Everything a consumer must know before using one feature column.

    ``provider``/``symbol`` record the DEFAULT binding of the feature. The
    stream builders can be pointed at another provider stream (e.g. BitMax's
    ``USD_IRT``), in which case the binding is the builder's arguments — but
    the dealer-spread block is meaningful only for a two-sided source.

    ``staleness_seconds`` is how long a value stays usable after the
    observation that produced it; :func:`asof_join` NaNs it out beyond that.
    ``POLICY_NAN_ON_GAP`` features are never carried forward at all, so their
    threshold is 0.
    """

    name: str
    version: str
    provider: str
    symbol: str
    lookback: str
    missing_policy: str
    staleness_seconds: int
    description: str
    requires_payload_key: Optional[str] = None


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    # --- dealer spread (two-sided quote) ------------------------------------
    FeatureSpec(
        name="dealer_spread_pct",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback="1 observation",
        missing_policy=POLICY_FFILL,
        staleness_seconds=SPREAD_STALENESS_S,
        description=(
            "Dealer buy/sell spread in percent of the midpoint — the observed "
            "round-trip cost of a real trade."
        ),
        requires_payload_key=SPREAD_PCT_KEY,
    ),
    FeatureSpec(
        name=f"dealer_spread_pctile_{SPREAD_WINDOW}",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{SPREAD_WINDOW} observations",
        missing_policy=POLICY_FFILL,
        staleness_seconds=SPREAD_STALENESS_S,
        description=(
            "Rank of the current spread inside its trailing window, in [0, 1] "
            "(1 = widest the dealer has quoted in that window)."
        ),
        requires_payload_key=SPREAD_PCT_KEY,
    ),
    FeatureSpec(
        name=f"dealer_spread_z_{SPREAD_WINDOW}",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{SPREAD_WINDOW} observations",
        missing_policy=POLICY_FFILL,
        staleness_seconds=SPREAD_STALENESS_S,
        description="Spread level in trailing standard deviations (ddof=1).",
        requires_payload_key=SPREAD_PCT_KEY,
    ),
    FeatureSpec(
        name=f"dealer_spread_chg_{SPREAD_CHG_LAG}",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{SPREAD_CHG_LAG} observations",
        missing_policy=POLICY_FFILL,
        staleness_seconds=SPREAD_STALENESS_S,
        description=(
            "Change of the spread in percentage points; positive = widening "
            "(dealer pulling back), negative = compression."
        ),
        requires_payload_key=SPREAD_PCT_KEY,
    ),
    FeatureSpec(
        name=f"dealer_spread_ratio_{SPREAD_MEAN_WINDOW}",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{SPREAD_MEAN_WINDOW} observations",
        missing_policy=POLICY_FFILL,
        staleness_seconds=SPREAD_STALENESS_S,
        description=(
            "Spread over its own trailing mean; >1 = wider than the recent "
            "norm, <1 = compressed. Scale-free companion to the z-score."
        ),
        requires_payload_key=SPREAD_PCT_KEY,
    ),
    # --- cross-provider dispersion (payload key may not exist yet) ----------
    FeatureSpec(
        name="peer_dispersion_pct",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback="1 observation",
        missing_policy=POLICY_FFILL,
        staleness_seconds=SPREAD_STALENESS_S,
        description=(
            "Disagreement between providers' concurrent quotes, in percent, "
            "as recorded on the observation. Quote uncertainty, not model "
            "uncertainty. NaN wherever the payload key is absent."
        ),
        requires_payload_key=DISPERSION_KEY,
    ),
    FeatureSpec(
        name=f"peer_dispersion_z_{DISPERSION_WINDOW}",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{DISPERSION_WINDOW} observations",
        missing_policy=POLICY_FFILL,
        staleness_seconds=SPREAD_STALENESS_S,
        description="Provider dispersion in trailing standard deviations (ddof=1).",
        requires_payload_key=DISPERSION_KEY,
    ),
    # --- update frequency / staleness ---------------------------------------
    FeatureSpec(
        name="obs_gap_seconds",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback="1 observation",
        missing_policy=POLICY_FFILL,
        staleness_seconds=CADENCE_STALENESS_S,
        description=(
            "Seconds since the previous observation of this stream — how "
            "stale the quote was at the moment it arrived."
        ),
    ),
    FeatureSpec(
        name=f"obs_gap_ratio_{GAP_WINDOW}",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{GAP_WINDOW} observations",
        missing_policy=POLICY_FFILL,
        staleness_seconds=CADENCE_STALENESS_S,
        description=(
            "Gap over the trailing MEDIAN gap; >1 = the source slowed down "
            "(a collection outage or a quiet market), <1 = it sped up."
        ),
    ),
    *(
        FeatureSpec(
            name=f"obs_per_hour_{hours}h",
            version=FEATURE_VERSION,
            provider=PRIMARY_PROVIDER,
            symbol=PRIMARY_SYMBOL,
            lookback=f"{hours} hours",
            missing_policy=POLICY_FFILL,
            staleness_seconds=CADENCE_STALENESS_S,
            description=(
                f"Observations per hour over the trailing {hours}h — update "
                "intensity. The first window is partial and understates it."
            ),
        )
        for hours in FREQ_WINDOW_HOURS
    ),
    # --- realized volatility and jumps --------------------------------------
    FeatureSpec(
        name=f"realized_vol_{RV_SHORT}",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{RV_SHORT} observations",
        missing_policy=POLICY_FFILL,
        staleness_seconds=VOL_STALENESS_S,
        description=(
            "Standard deviation of log returns per OBSERVATION (not "
            "annualized — read it together with obs_per_hour_*)."
        ),
    ),
    FeatureSpec(
        name=f"realized_vol_{RV_LONG}",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{RV_LONG} observations",
        missing_policy=POLICY_FFILL,
        staleness_seconds=VOL_STALENESS_S,
        description="Slower realized volatility, same units as the short one.",
    ),
    FeatureSpec(
        name=f"realized_vol_ratio_{RV_SHORT}_{RV_LONG}",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{RV_LONG} observations",
        missing_policy=POLICY_FFILL,
        staleness_seconds=VOL_STALENESS_S,
        description=(
            "Short over long realized volatility; >1 = volatility is "
            "expanding right now relative to its own recent regime."
        ),
    ),
    FeatureSpec(
        name="jump_z",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{JUMP_WINDOW} observations",
        missing_policy=POLICY_NAN_ON_GAP,
        staleness_seconds=0,
        description=(
            "Signed log return divided by a robust scale estimated from the "
            "PRIOR window only, so a jump never sets its own threshold."
        ),
    ),
    FeatureSpec(
        name="jump_flag",
        version=FEATURE_VERSION,
        provider=PRIMARY_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{JUMP_WINDOW} observations",
        missing_policy=POLICY_NAN_ON_GAP,
        staleness_seconds=0,
        description=(
            f"1.0 when |jump_z| >= {JUMP_SIGMA}, else 0.0 (NaN while the "
            "robust scale is undefined). A stale flag would claim a jump that "
            "is not happening, so it is never carried forward."
        ),
    ),
    # --- premium decomposition (derived from three canonical series) --------
    FeatureSpec(
        name="dlog_18k",
        version=FEATURE_VERSION,
        provider=DERIVED_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{PREMIUM_DECOMP_LAG} step (caller's series frequency)",
        missing_policy=POLICY_FFILL,
        staleness_seconds=DECOMP_STALENESS_S,
        description="Realized log move of 18k gold — the quantity being split.",
    ),
    FeatureSpec(
        name="fx_contrib_log",
        version=FEATURE_VERSION,
        provider=DERIVED_PROVIDER,
        symbol="USD_IRT",
        lookback=f"{PREMIUM_DECOMP_LAG} step (caller's series frequency)",
        missing_policy=POLICY_FFILL,
        staleness_seconds=DECOMP_STALENESS_S,
        description="Part of the 18k move explained by the toman/USD rate.",
    ),
    FeatureSpec(
        name="gold_contrib_log",
        version=FEATURE_VERSION,
        provider=DERIVED_PROVIDER,
        symbol="XAUUSD",
        lookback=f"{PREMIUM_DECOMP_LAG} step (caller's series frequency)",
        missing_policy=POLICY_FFILL,
        staleness_seconds=DECOMP_STALENESS_S,
        description="Part of the 18k move explained by global gold.",
    ),
    FeatureSpec(
        name="premium_contrib_log",
        version=FEATURE_VERSION,
        provider=DERIVED_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{PREMIUM_DECOMP_LAG} step (caller's series frequency)",
        missing_policy=POLICY_FFILL,
        staleness_seconds=DECOMP_STALENESS_S,
        description=(
            "Local-premium residual: the part of the move neither FX nor "
            "global gold explains (Tehran demand, hoarding, policy fear)."
        ),
    ),
    FeatureSpec(
        name="fx_share",
        version=FEATURE_VERSION,
        provider=DERIVED_PROVIDER,
        symbol="USD_IRT",
        lookback=f"{PREMIUM_DECOMP_LAG} step (caller's series frequency)",
        missing_policy=POLICY_FFILL,
        staleness_seconds=DECOMP_STALENESS_S,
        description=(
            "FX contribution as a fraction of the total move (NaN when the "
            "total is zero; may exceed 1 when components offset)."
        ),
    ),
    FeatureSpec(
        name="gold_share",
        version=FEATURE_VERSION,
        provider=DERIVED_PROVIDER,
        symbol="XAUUSD",
        lookback=f"{PREMIUM_DECOMP_LAG} step (caller's series frequency)",
        missing_policy=POLICY_FFILL,
        staleness_seconds=DECOMP_STALENESS_S,
        description="Global-gold contribution as a fraction of the total move.",
    ),
    FeatureSpec(
        name="premium_share",
        version=FEATURE_VERSION,
        provider=DERIVED_PROVIDER,
        symbol=PRIMARY_SYMBOL,
        lookback=f"{PREMIUM_DECOMP_LAG} step (caller's series frequency)",
        missing_policy=POLICY_FFILL,
        staleness_seconds=DECOMP_STALENESS_S,
        description="Local-premium contribution as a fraction of the total move.",
    ),
)

FEATURE_REGISTRY: Mapping[str, FeatureSpec] = MappingProxyType(
    {spec.name: spec for spec in FEATURE_SPECS}
)

STREAM_FEATURES: tuple[str, ...] = tuple(
    spec.name for spec in FEATURE_SPECS if spec.provider != DERIVED_PROVIDER
)
DECOMPOSITION_FEATURES: tuple[str, ...] = tuple(
    spec.name for spec in FEATURE_SPECS if spec.provider == DERIVED_PROVIDER
)


def registry_table() -> pd.DataFrame:
    """The registry as a documentable table — one row per registered feature."""
    return pd.DataFrame([asdict(spec) for spec in FEATURE_SPECS])


# --- payload readers ---------------------------------------------------------


def _number(value: Any) -> Optional[float]:
    """Payload values are provider JSON: accept finite real numbers only."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    out = float(value)
    return None if math.isnan(out) or math.isinf(out) else out


def _as_payload(raw: Any) -> dict:
    """JSONB comes back as a dict, but tolerate a JSON string or NULL."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _spread_from_payload(payload: Any) -> float:
    """Dealer spread in percent from one Hamrah Gold payload, else NaN.

    Prefers the stored ``spread_pct``; when only the two sides survived it is
    recomputed with the provider's own algebra ``(sell - buy) / mid * 100``
    (the rial unit cancels, so no currency conversion is involved). A
    one-sided quote has no spread and yields NaN — the provider already
    writes ``spread_pct: null`` in that case.
    """
    if not isinstance(payload, dict):
        return math.nan
    stored = _number(payload.get(SPREAD_PCT_KEY))
    if stored is not None:
        return stored
    sell = _number(payload.get(SELL_RIAL_KEY))
    buy = _number(payload.get(BUY_RIAL_KEY))
    if sell is None or buy is None:
        return math.nan
    mid = (sell + buy) / 2.0
    return (sell - buy) / mid * 100.0 if mid > 0 else math.nan


def _dispersion_from_payload(payload: Any) -> float:
    """Cross-provider dispersion in percent from one payload, else NaN.

    The ``peer_dispersion`` key is being added by a separate change and its
    shape is not frozen, so this reads defensively: a bare number, or a small
    mapping with an obvious percent field. Anything else degrades to NaN —
    an unexpected payload must never raise inside a training run.
    """
    if not isinstance(payload, dict) or DISPERSION_KEY not in payload:
        return math.nan
    raw = payload[DISPERSION_KEY]
    direct = _number(raw)
    if direct is not None:
        return direct
    if isinstance(raw, dict):
        for key in DISPERSION_VALUE_KEYS:
            nested = _number(raw.get(key))
            if nested is not None:
                return nested
    return math.nan


# Which payload key each feature group depends on, derived from the registry
# so a new spec cannot forget to declare itself.
_PAYLOAD_EXTRACTORS = {
    SPREAD_PCT_KEY: _spread_from_payload,
    DISPERSION_KEY: _dispersion_from_payload,
}


# --- observation frame -------------------------------------------------------


def _empty_observations() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "provider_code": pd.Series(dtype=object),
            "symbol": pd.Series(dtype=object),
            "value": pd.Series(dtype=float),
            "payload": pd.Series(dtype=object),
        },
        index=pd.DatetimeIndex([], tz="UTC", name="observed_at"),
    )


def normalize_observations(
    observations: ObservationsLike, as_of: Optional[datetime] = None
) -> pd.DataFrame:
    """Canonical observation frame: UTC index, ``value``, ``payload``.

    Accepts ``raw_observations`` rows as stored (``raw_value`` plus the
    provider's ``currency``) or already-normalized ``prices``-style rows (a
    ``value`` column). Rial quotes are divided by ten exactly once — the
    toman invariant from docs/CONTRACTS.md — and the raw payload is left
    untouched.

    When ``as_of`` is given, rows observed after it are dropped and the
    result is re-checked with :func:`assert_no_future`.
    """
    frame = pd.DataFrame(observations)
    if frame.empty:
        return _empty_observations()
    if "observed_at" not in frame.columns:
        raise KeyError("observations must carry an 'observed_at' column")
    if "value" not in frame.columns and "raw_value" not in frame.columns:
        raise KeyError("observations must carry a 'value' or 'raw_value' column")

    stamps = pd.to_datetime(frame["observed_at"], utc=True)
    if "value" in frame.columns:
        value = pd.to_numeric(frame["value"], errors="coerce").astype(float)
    else:
        value = pd.to_numeric(frame["raw_value"], errors="coerce").astype(float)
        if "currency" in frame.columns:
            # rial-quoted providers store rials in raw_value (CONTRACTS.md)
            value = value.where(frame["currency"] != "IRR", rial_to_toman(value))

    payloads = (
        frame["raw_payload"].map(_as_payload)
        if "raw_payload" in frame.columns
        else pd.Series([{} for _ in range(len(frame))], index=frame.index)
    )
    blank = pd.Series(index=frame.index, dtype=object)
    out = pd.DataFrame(
        {
            "provider_code": frame.get("provider_code", blank).to_numpy(),
            "symbol": frame.get("symbol", blank).to_numpy(),
            "value": value.to_numpy(),
            "payload": payloads.to_numpy(),
        }
    )
    out.index = pd.DatetimeIndex(stamps.to_numpy(), name="observed_at")
    out = out[out.index.notna()].sort_index()

    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        if cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize("UTC")
        out = out[out.index <= cutoff]
        # belt and braces: the filter above is the guarantee, this catches a
        # timezone mistake that would silently let the future through
        assert_no_future(out.reset_index(), as_of, column="observed_at")
    return out


def _stream(observations: pd.DataFrame, provider: str, symbol: str) -> pd.DataFrame:
    """One provider/symbol observation stream, deduped on timestamp."""
    if observations.empty:
        return observations
    sub = observations[
        (observations["provider_code"] == provider) & (observations["symbol"] == symbol)
    ]
    # a re-poll inside the same second is the same quote, not a new datapoint
    return sub[~sub.index.duplicated(keep="last")]


# --- trailing (strictly backward-looking) statistics -------------------------


def trailing_z(series: pd.Series, window: int) -> pd.Series:
    """Z-score against the trailing window ending at the current row (ddof=1)."""
    mean = series.rolling(window).mean()
    std = series.rolling(window).std()
    return (series - mean) / std.replace(0.0, np.nan)


def trailing_percentile(series: pd.Series, window: int) -> pd.Series:
    """Rank of the current value inside its trailing window, in [0, 1]."""
    return series.rolling(window).rank(pct=True)


def build_microstructure_frame(
    observations: ObservationsLike,
    as_of: Optional[datetime] = None,
    provider: str = PRIMARY_PROVIDER,
    symbol: str = PRIMARY_SYMBOL,
) -> pd.DataFrame:
    """Microstructure features for one provider/symbol observation stream.

    The frame is indexed by that stream's own UTC observation timestamps:
    the row at ``t`` is computable from observations with ``observed_at <=
    t`` and from nothing else, so appending later observations can never
    change it (locked by the leakage test).

    Columns the stored payloads cannot support come back all-NaN rather than
    missing — the column set is stable across deployments; ask
    :func:`unavailable_features` which of them are structurally empty.
    """
    stream = _stream(normalize_observations(observations, as_of=as_of), provider, symbol)
    if stream.empty:
        return pd.DataFrame(
            {name: pd.Series(dtype=float) for name in STREAM_FEATURES},
            index=pd.DatetimeIndex([], tz="UTC", name="observed_at"),
        )

    frame = pd.DataFrame(index=stream.index)

    # --- dealer spread ------------------------------------------------------
    spread = stream["payload"].map(_spread_from_payload).astype(float)
    frame["dealer_spread_pct"] = spread
    frame[f"dealer_spread_pctile_{SPREAD_WINDOW}"] = trailing_percentile(
        spread, SPREAD_WINDOW
    )
    frame[f"dealer_spread_z_{SPREAD_WINDOW}"] = trailing_z(spread, SPREAD_WINDOW)
    frame[f"dealer_spread_chg_{SPREAD_CHG_LAG}"] = spread.diff(SPREAD_CHG_LAG)
    frame[f"dealer_spread_ratio_{SPREAD_MEAN_WINDOW}"] = spread / spread.rolling(
        SPREAD_MEAN_WINDOW
    ).mean().replace(0.0, np.nan)

    # --- cross-provider dispersion (all-NaN until the payload key exists) ---
    dispersion = stream["payload"].map(_dispersion_from_payload).astype(float)
    frame["peer_dispersion_pct"] = dispersion
    frame[f"peer_dispersion_z_{DISPERSION_WINDOW}"] = trailing_z(
        dispersion, DISPERSION_WINDOW
    )

    # --- update frequency / staleness ---------------------------------------
    gap = stream.index.to_series().diff().dt.total_seconds()
    frame["obs_gap_seconds"] = gap
    frame[f"obs_gap_ratio_{GAP_WINDOW}"] = gap / gap.rolling(
        GAP_WINDOW
    ).median().replace(0.0, np.nan)
    unit = pd.Series(1.0, index=stream.index)
    for hours in FREQ_WINDOW_HOURS:
        # time-based rolling closes on the right: (t - hours, t]
        frame[f"obs_per_hour_{hours}h"] = unit.rolling(f"{hours}h").sum() / float(hours)

    # --- realized volatility and jumps --------------------------------------
    price = stream["value"].astype(float)
    log_return = np.log(price.where(price > 0)).diff()
    rv_short = log_return.rolling(RV_SHORT).std()
    rv_long = log_return.rolling(RV_LONG).std()
    frame[f"realized_vol_{RV_SHORT}"] = rv_short
    frame[f"realized_vol_{RV_LONG}"] = rv_long
    frame[f"realized_vol_ratio_{RV_SHORT}_{RV_LONG}"] = rv_short / rv_long.replace(
        0.0, np.nan
    )
    # Robust scale from the PRIOR window (shift(1)): a jump that entered its
    # own scale estimate would raise the bar it has to clear and hide itself.
    scale = MAD_TO_SIGMA * log_return.abs().rolling(JUMP_WINDOW).median().shift(1)
    jump_z = log_return / scale.replace(0.0, np.nan)
    frame["jump_z"] = jump_z
    frame["jump_flag"] = (jump_z.abs() >= JUMP_SIGMA).astype(float).where(jump_z.notna())

    return frame[list(STREAM_FEATURES)]


def unavailable_features(
    observations: ObservationsLike,
    provider: str = PRIMARY_PROVIDER,
    symbol: str = PRIMARY_SYMBOL,
) -> tuple[str, ...]:
    """Registered features whose source payload key is absent from this stream.

    Availability is judged from the payloads, not from the built frame: a
    short history leaves long-window columns NaN during warm-up, which is not
    the same thing as a feature this deployment cannot produce at all.
    """
    stream = _stream(normalize_observations(observations), provider, symbol)
    missing: list[str] = []
    for key, extractor in _PAYLOAD_EXTRACTORS.items():
        names = [spec.name for spec in FEATURE_SPECS if spec.requires_payload_key == key]
        if not names:
            continue
        supported = any(
            not math.isnan(extractor(payload)) for payload in stream.get("payload", [])
        )
        if not supported:
            missing.extend(names)
    return tuple(missing)


# --- as-of join (the point-in-time boundary for callers) ---------------------


def asof_join(features: pd.DataFrame, timestamps: TimestampsLike) -> pd.DataFrame:
    """Attach ``features`` to ``timestamps`` with a strictly backward as-of join.

    This is the ONLY supported way to put microstructure features on another
    timeline (a model's daily grid, a prediction timestamp). For each query
    ``t`` the joined row is the LAST feature row indexed at or before ``t``;
    a later row can never be selected, so a caller cannot read the future
    even by accident. Each column is then aged out by its own registry
    policy:

    * ``POLICY_FFILL`` — carried forward until ``spec.staleness_seconds``,
      NaN beyond it;
    * ``POLICY_NAN_ON_GAP`` — kept only on an exact timestamp match.

    Columns with no registry entry get the strictest policy. The extra
    ``feature_age_seconds`` column is the honest age of the joined row, so a
    consumer can down-weight a value instead of trusting it blindly.
    """
    query = pd.DatetimeIndex(pd.to_datetime(pd.Index(timestamps), utc=True))
    columns = [*features.columns, AGE_COLUMN]
    if len(query) == 0 or features.empty:
        return pd.DataFrame(
            {column: pd.Series(np.nan, index=query, dtype=float) for column in columns},
            index=query,
        )
    if not isinstance(features.index, pd.DatetimeIndex):
        raise TypeError("features must be indexed by a UTC DatetimeIndex")

    order = np.asarray(query.argsort())
    left = pd.DataFrame({"_query": query[order]})
    right = features.sort_index().rename_axis("_source").reset_index()
    merged = pd.merge_asof(
        left, right, left_on="_query", right_on="_source", direction="backward"
    )
    age = (merged["_query"] - merged["_source"]).dt.total_seconds()
    # merge_asof(direction='backward') cannot pick a future row — assert it
    # anyway: this join is the leakage boundary of the whole module
    if (age.dropna() < 0).any():
        raise LeakageError("as-of join selected a feature row newer than its query")

    out = merged.drop(columns=["_query", "_source"])
    for column in out.columns:
        spec = FEATURE_REGISTRY.get(column)
        limit = (
            spec.staleness_seconds
            if spec is not None and spec.missing_policy == POLICY_FFILL
            else 0
        )
        out[column] = out[column].where(age <= limit)
    out[AGE_COLUMN] = age
    out.index = pd.DatetimeIndex(left["_query"], name="observed_at")
    # restore the caller's original row order
    return out.iloc[np.argsort(order, kind="stable")]


# --- premium decomposition ---------------------------------------------------


@dataclass(frozen=True)
class PremiumDecomposition:
    """One realized 18k move split into FX, global-gold and local premium.

    All ``dlog_*`` members are log changes and sum EXACTLY to ``dlog_total``;
    ``pct_total`` is the same move quoted the way a human reads it.
    """

    dlog_total: float
    dlog_fx: float
    dlog_gold: float
    dlog_premium: float
    pct_total: float


def decompose_move(
    k18_start: float,
    k18_end: float,
    usd_start: float,
    usd_end: float,
    xau_start: float,
    xau_end: float,
) -> PremiumDecomposition:
    """Split one 18k move into FX, global-gold and local-premium parts.

    The algebra follows directly from the parity formula in
    ``app/core/formula.py``::

        theo_18k = xau_usd / TROY_OUNCE_GRAMS * usd_irt * KARAT_18_PURITY

    Write the observed price as the theoretical price times the local premium
    factor ``m = observed_18k / theo_18k = 1 + premium_pct/100``::

        P = xau * usd * m * (KARAT_18_PURITY / TROY_OUNCE_GRAMS)

    A product becomes a sum in logs, and the constant drops out under
    differencing, leaving an EXACT additive identity for any two timestamps::

        Δlog P = Δlog xau + Δlog usd + Δlog m
                 ^global    ^FX        ^local premium residual

    No regression and no betas are involved — this is an identity, not a
    model, which is why the residual is trustworthy: it is precisely the part
    of the move that neither the dollar nor global gold can explain (Tehran
    demand, hoarding, policy fear). The residual is computed by subtraction
    so the three parts always re-sum to the total exactly.

    Non-positive or missing inputs give NaN members rather than raising.
    """
    dlog_total = _safe_dlog(k18_start, k18_end)
    dlog_fx = _safe_dlog(usd_start, usd_end)
    dlog_gold = _safe_dlog(xau_start, xau_end)
    return PremiumDecomposition(
        dlog_total=dlog_total,
        dlog_fx=dlog_fx,
        dlog_gold=dlog_gold,
        dlog_premium=dlog_total - dlog_fx - dlog_gold,
        pct_total=(
            (k18_end / k18_start - 1.0) * 100.0
            if _positive(k18_start) and _positive(k18_end)
            else math.nan
        ),
    )


def _positive(value: Any) -> bool:
    number = _number(value)
    return number is not None and number > 0.0


def _safe_dlog(start: Any, end: Any) -> float:
    if not (_positive(start) and _positive(end)):
        return math.nan
    return math.log(float(end)) - math.log(float(start))


def align_causal(series: Optional[pd.Series], index: pd.DatetimeIndex) -> pd.Series:
    """Reindex ``series`` onto ``index``, forward-filling from the PAST only."""
    if series is None or len(series) == 0:
        return pd.Series(np.nan, index=index, dtype=float)
    aux = pd.Series(
        np.asarray(series, dtype=float),
        index=pd.DatetimeIndex(pd.to_datetime(series.index, utc=True)),
    )
    aux = aux[~aux.index.duplicated(keep="last")].sort_index()
    return aux.reindex(aux.index.union(index)).ffill().reindex(index)


def decompose_premium_move(
    k18: pd.Series,
    usd_irt: Optional[pd.Series] = None,
    xau_usd: Optional[pd.Series] = None,
    lag: int = PREMIUM_DECOMP_LAG,
) -> pd.DataFrame:
    """:func:`decompose_move` over whole series, aligned onto ``k18``'s index.

    The auxiliary series are aligned by forward-fill (past only) and the move
    is measured over ``lag`` steps of the 18k index, so the row at ``t`` uses
    nothing observed after ``t``. See :func:`decompose_move` for the algebra.
    """
    index = pd.DatetimeIndex(pd.to_datetime(k18.index, utc=True))
    price = pd.Series(np.asarray(k18, dtype=float), index=index)
    usd = align_causal(usd_irt, index)
    xau = align_causal(xau_usd, index)

    out = pd.DataFrame(index=index)
    out["dlog_18k"] = np.log(price.where(price > 0)).diff(lag)
    out["fx_contrib_log"] = np.log(usd.where(usd > 0)).diff(lag)
    out["gold_contrib_log"] = np.log(xau.where(xau > 0)).diff(lag)
    # residual by subtraction: the three parts must re-sum to the total
    out["premium_contrib_log"] = (
        out["dlog_18k"] - out["fx_contrib_log"] - out["gold_contrib_log"]
    )
    total = out["dlog_18k"].replace(0.0, np.nan)
    out["fx_share"] = out["fx_contrib_log"] / total
    out["gold_share"] = out["gold_contrib_log"] / total
    out["premium_share"] = out["premium_contrib_log"] / total
    return out[list(DECOMPOSITION_FEATURES)]
