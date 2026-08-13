"""Unit/currency normalization helpers.

Canonical storage rules (docs/CONTRACTS.md):

* Iranian values are stored in **IRT (toman)**; providers quoting rials (TGJU)
  are divided by 10 and the raw rial value is preserved in ``raw_observations``.
* ``^TNX`` (Yahoo's US 10-year note ticker) is published under TWO conventions
  and Yahoo switches between them — see :func:`tnx_to_pct`, which reads the
  convention off the quote instead of assuming one.
* Global metals are USD per troy ounce; sanity constants below let validation
  catch gram/ounce mix-ups.
"""
from __future__ import annotations

from typing import Sequence

from .formula import TROY_OUNCE_GRAMS  # re-export for convenience
from .validation import SANITY_RANGES

__all__ = [
    "TROY_OUNCE_GRAMS",
    "SYMBOL_META",
    "TNX_MAX_YIELD_PCT",
    "rial_to_toman",
    "toman_to_rial",
    "tnx_to_pct",
    "tnx_series_to_pct",
    "tnx_quote_meta",
]

# symbol -> (currency, unit) for normalized `prices` rows.
SYMBOL_META: dict[str, tuple[str, str]] = {
    "IR_GOLD_18K": ("IRT", "gram"),
    "IR_COIN_EMAMI": ("IRT", "coin"),
    "USD_IRT": ("IRT", "usd"),
    "XAUUSD": ("USD", "ozt"),
    "XAGUSD": ("USD", "ozt"),
    "BRENT_OIL": ("USD", "bbl"),
    "DXY": ("INDEX", "index"),
    "US10Y": ("PCT", "pct"),
}


def rial_to_toman(value_irr: float) -> float:
    """1 toman = 10 rials."""
    return value_irr / 10.0


def toman_to_rial(value_irt: float) -> float:
    return value_irt * 10.0


# Upper bound of a plausible US 10-year yield IN PERCENT.  Imported from the
# validation table rather than restated, so the number that decides how a
# quote is read and the number that decides whether the result is storable are
# provably the same one.
TNX_MAX_YIELD_PCT: float = SANITY_RANGES["US10Y"][1]


def tnx_to_pct(tnx_quote: float) -> float:
    """Read a ``^TNX`` quote as a US 10-year yield in percent.

    Yahoo publishes this ticker under BOTH conventions and has switched
    between them mid-series:

    * the legacy CBOE index, ten times the yield — ``46.82`` means 4.682%;
    * the yield itself — ``4.697`` means 4.697%.

    Until 2026-08-11 the feed carried the second form while this function
    divided unconditionally by ten, so five years of US10Y (2021-07-26 to
    2026-08-11, 1663 rows) were stored a factor of ten too small — 0.117 to
    0.499 for real yields of 1.17% to 4.99%.  Migration 0022 repairs the rows;
    this function stops the assumption from being made again.

    And it has to, because the flip is not one-way: by 2026-08-13 the feed was
    back on the plain yield (live ``regularMarketPrice`` 4.627, previous close
    4.682).  There is no "current convention" to hardcode — which is exactly
    why migration 0022 must not be applied ahead of this code, see its header.

    THE RULE, and why it is this one.  The two readings are only
    distinguishable where they disagree about validity, so ``÷10`` is treated
    as a REPAIR and applied only to a quote that cannot be a yield at all:
    above :data:`TNX_MAX_YIELD_PCT` (25%, the documented ceiling in
    ``validation.SANITY_RANGES``) the index reading is the only one left.  At
    or below it the quote is taken at face value, because dividing a number
    that is already a valid yield is precisely the failure being fixed, and
    because the alternative default — always divide — is the one that produced
    five silent years of wrong data.

    The residual ambiguity is stated rather than hidden: a quote of, say, 11.7
    is 11.7% under the direct convention and 1.17% under the index one, and no
    single number can settle that.  What is left to catch it is the series,
    not the quote — a convention flip inside the ambiguous band is a tenfold
    step, which ``validation.classify_observation`` holds as suspect, and
    which the collect job now both alerts on and refuses to accept until the
    new level has repeated (see ``validation.SUSTAIN_MIN_OBSERVATIONS``).
    """
    quote = float(tnx_quote)
    return quote / 10.0 if quote > TNX_MAX_YIELD_PCT else quote


def tnx_series_to_pct(tnx_quotes: Sequence[float]) -> list[float]:
    """Read a whole ``^TNX`` SERIES as yields in percent, under one convention.

    :func:`tnx_to_pct` judges a lone quote, because a lone quote is all it is
    given.  A history payload is not that: it is one download, published under
    one convention, so the convention is a property of the SERIES and the
    series is the evidence for it.  Deciding bar by bar throws that evidence
    away and gets the ambiguous band wrong — measured on a five-year
    index-form history through ``providers.yahoo.parse_chart_history``:

        true    quote    per-bar    series
        1.28%   12.80    12.80  X   1.28  ok
        1.51%   15.10    15.10  X   1.51  ok
        2.38%   23.80    23.80  X   2.38  ok
        3.48%   34.80     3.48  ok  3.48  ok
        4.68%   46.80     4.68  ok  4.68  ok

    The three wrong rows are not caught downstream either: 12.8, 15.1 and 23.8
    all sit inside ``SANITY_RANGES["US10Y"]`` = (0.0, 25.0), so they are stored
    as ordinary values.  A single history backfill therefore writes a series
    that is partly yields and partly index values — and that job,
    ``POST /internal/backfill/history``, is exactly what migration 0022
    recommends for recovering the days the freeze cost.

    THE RULE is the same repair as :func:`tnx_to_pct`, asked once of the whole
    series instead of once per bar: if ANY bar is above
    :data:`TNX_MAX_YIELD_PCT` it cannot be a yield, so the download is in the
    index convention and EVERY bar is divided by ten.  Otherwise no bar
    contradicts the plain-yield reading and none is touched.  Maximum, not
    median or majority: one impossible value is proof about the convention,
    whereas most values being possible is proof of nothing.

    What this still cannot settle, stated rather than hidden: a window in
    which the true yield never exceeded 2.5% is below the ceiling under BOTH
    readings (0.5%-1.5% yields are an index of 5-15), and no arithmetic on
    those numbers alone can tell them apart.  The ranges this is called with
    do not have that shape — a 3y or 5y range ending in 2026 contains the
    2023-2025 highs near 5%, i.e. an index near 50 — but a short range over a
    low-rate era would, and it would be read as plain yields.
    """
    quotes = [float(q) for q in tnx_quotes]
    if not quotes:
        return []
    divisor = 10.0 if max(quotes) > TNX_MAX_YIELD_PCT else 1.0
    return [q / divisor for q in quotes]


def tnx_quote_meta(tnx_quote: float) -> tuple[str, str]:
    """``(unit, currency)`` labelling the convention :func:`tnx_to_pct` read.

    ``raw_observations`` exists so a normalization is auditable, which it is
    not when every row claims the same unit regardless of what the provider
    actually sent.  Labelling the branch makes a convention change a visible
    fact in the table (``SELECT DISTINCT unit ... WHERE symbol = 'US10Y'``)
    instead of something only a re-derivation of the numbers would reveal.
    """
    if float(tnx_quote) > TNX_MAX_YIELD_PCT:
        return "TNX_index", "INDEX"
    return "pct", "PCT"
