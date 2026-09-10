"""Corporate-action detection and back-adjustment for Tehran equity bars.

WHY THIS MODULE IS THE ONE THAT MATTERS
---------------------------------------
The raw close series is not a price history.  فولاد's raw close goes from
1,900 to 2,881 across nineteen years — x1.52, which would make Iran's largest
steelmaker one of the worst investments in a country whose CPI rose by two
orders of magnitude over the same span.  Adjusted for the 31 corporate actions
TSETMC itself signals, the same series is x907.86.  Everything downstream — a
return, a ratio, a screener rank, a correlation — is computed on one of those
two numbers, and one of them is nonsense.

HOW AN ACTION IS DETECTED, AND WHY THIS SIGNAL AND NOT ANOTHER
--------------------------------------------------------------
Each bar carries ``priceYesterday``: the reference price the exchange opened
that session against.  On an ordinary session it equals the previous session's
closing price exactly.  When a capital increase, a dividend or a split falls
between two sessions, the exchange restates the reference, and the two
disagree.  The ratio is then exactly ``priceYesterday / previous_close`` — the
exchange's own arithmetic, not an estimate of it.

The alternatives were measured and rejected (2026-09-10): no source publishes a
usable adjusted series (BrsApi's "adjusted" and "unadjusted" samples are
byte-identical across 4,038 rows and round a 3,704-rial close to ``6``), and
``GetPriceAdjustList``'s event dates land on suspension days rather than on the
session that opened on the new basis.

FOUR THINGS THE NAIVE VERSION OF THIS GETS WRONG
-------------------------------------------------
Each was found by running the naive algorithm over all twenty roster symbols —
77,344 bars — rather than over the one symbol it was written against.

1. *Pre-listing placeholder bars.*  Before an instrument's first trade, TSETMC
   serves zero-volume bars at the 1,000-rial par value: 161 of them for نوری,
   which has bars from 2018-11-10 and first traded on 2019-07-13 at 31,250.
   ``priceYesterday`` agrees with them, so no action is detected — correctly,
   there was none — and the series shows a +3,025% first day.  They are prices
   of nothing, and the adjusted series starts at the first traded bar.
   (شستا +760%, فارس +650%, ذوب +262%, شتران +244% were the same defect.)

2. *The closing price is the basis, not the last trade.*  ``priceYesterday`` is
   stated against ``pClosing`` (TSE's قیمت پایانی, a base-volume-weighted
   average), not against ``pDrCotVal`` (the last trade).  Detecting on the
   wrong one manufactures an "action" on most sessions.

3. *Halted sessions still carry actions.*  فولاد's 2022 capital increase lands
   in two steps across four zero-volume halt bars: 2022-08-06 restates
   11,170 -> 9,470 on a bar with no trading at all, and 2022-08-09 restates
   9,470 -> 5,240.  Dropping untraded bars before detection loses the first
   step and leaves a 15% error in the whole pre-2022 history.  So untraded bars
   are kept in the walk, and only the LEADING never-traded run is excluded.

4. *A reopening after a suspension is not a missed action.*  فولاد's own
   history contains a -39.2% day on 2008-10-26 that survives adjustment, and it
   is real: the instrument had been suspended for 43 calendar days, TSETMC's
   ``priceYesterday`` on that bar agrees with the previous close, and the
   exchange lifts the price limit for a reopening auction.  A gate that refused
   on it would refuse the very symbol this engine is validated against.  See
   :func:`validate`.

THE GATE
--------
``docs/REDESIGN.md`` makes this phase's gate explicit: *adjustment must be
validated before any return, ratio or score is computed from it*.  So
:func:`adjust` produces a verdict, :func:`validate` decides it from measured
bounds, and nothing downstream may compute a return from a series whose verdict
is ``refused``.  A missed action does not announce itself; the only way to
notice one is that it leaves behind a day the market could not have produced.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from typing import Any, Iterable, Optional, Sequence

log = logging.getLogger(__name__)

# --- versioning --------------------------------------------------------------
#
# The version names the DETECTOR and the CHAINING together, because a change to
# either produces different numbers from the same bars.  It is stored on every
# corporate_actions row and on every verdict, and echoed on every read, so a
# number served by this system can always be traced to the arithmetic that
# produced it.  A v2 is introduced BESIDE v1 (0028 keys corporate_actions on
# the version) rather than by rewriting v1's rows.
ADJUSTMENT_VERSION = "priceYesterday-chain-v1"

# --- the gate's constants, all measured -------------------------------------
#
# Measured 2026-09-10 over the twenty roster symbols, 77,344 bars, 472 detected
# actions, on the ADJUSTED series.  Two populations, because they obey
# different rules.
#
# GENUINE SESSIONS — both bars traded, at most SESSION_GAP_DAYS apart.
# 65,802 pairs:
#
#     p99       5.00%   <- the exchange's daily price limit, visible in the data
#     p99.9     6.94%
#     p99.99   12.26%
#     max      13.64%   (فملی, 2007-09-10)
#
# The only pairs anywhere beyond 20% are کچاد's 2007-01-09 (-60.4%) and
# 2007-01-10 (+152.7%), where one 227,000,010-share block trade in two
# transactions set the official closing price to 5,576 against a market of
# ~14,090 and it recovered the next session.  That is a real defect in the
# published closing price rather than a missed action, and the gate refuses the
# symbol for it — a gate that exempts the one series it cannot explain is not a
# gate.  کچاد is seeded disabled in 0028 with that measurement on the row.
#
# 25% is therefore ~1.8x the worst legitimate session ever measured, and well
# below the -42.3% an unadjusted فولاد shows on 2022-08-09.
MAX_SESSION_RETURN = 0.25

# REOPENINGS — one or both bars halted, or a longer gap.  11,269 pairs:
#
#     p99      14.23%
#     p99.9    60.29%
#     max     242.63%   (شپنا, 2013-02-09, reopening after six halted sessions)
#
# The exchange lifts the price limit for a reopening auction, so no bound a
# session obeys applies here, and 48 of these exceed 25% across fifteen of the
# twenty symbols.  This bound is therefore NOT a claim about the market — it is
# a bound on the ARITHMETIC.  A broken factor chain (a lost action at the front
# of the series, a ratio inverted, a payload with a hole in it) produces jumps
# of ten times or more; no reopening in 77,344 measured bars exceeded 2.43x.
MAX_REOPENING_RETURN = 3.0

# What counts as "the next session".  The Tehran week trades Saturday to
# Wednesday, so consecutive sessions are one day apart inside the week and
# three across the weekend; four absorbs a single public holiday.  Beyond it
# the exchange was closed for a holiday block (Nowruz runs to about thirteen
# days) or the instrument was suspended, and in both cases the reopening
# auction is not price-limited.
SESSION_GAP_DAYS = 4

STATUS_VALIDATED = "validated"
STATUS_REFUSED = "refused"

# TSETMC's own key names.  Named here so a payload whose shape changed is a
# clear refusal rather than a KeyError three frames down.
FIELD_LIST = "closingPriceDaily"
REQUIRED_FIELDS = (
    "dEven", "insCode", "priceYesterday", "priceFirst", "priceMin", "priceMax",
    "pClosing", "pDrCotVal", "qTotTran5J", "zTotTran", "qTotCap",
)


class BarParseError(ValueError):
    """The payload is not the shape this parser was written for."""


# --- Persian text normalisation ---------------------------------------------
#
# The same confusables :func:`app.economic.sci.normalize_fa` folds, and for the
# same reason — TSETMC serves فملي with Arabic YEH (U+064A) and كچاد with
# Arabic KAF (U+0643) while a Persian keyboard types U+06CC and U+06A9 — but
# with ZWNJ handled differently on the two sides, which is why this is a pair
# of functions rather than an import.

_ARABIC_TO_PERSIAN = {
    0x0643: 0x06A9,  # ARABIC KAF   -> PERSIAN KEHEH
    0x064A: 0x06CC,  # ARABIC YEH   -> PERSIAN YEH
    0x0649: 0x06CC,  # ALEF MAKSURA -> PERSIAN YEH
    0x0629: 0x0647,  # TEH MARBUTA  -> HEH
}
# Invisible marks that break equality without changing what is rendered.
_INVISIBLE = dict.fromkeys([0x200E, 0x200F, 0x00A0, 0xFEFF])
# For a KEY, ZWNJ goes too: it is not typed reliably and a symbol is one token.
_INVISIBLE_AND_ZWNJ = dict.fromkeys([0x200C, 0x200E, 0x200F, 0x00A0, 0xFEFF])


def fold_symbol(text: object) -> str:
    """Fold a trading symbol to its lookup key.

    Used for ``equity_instruments.symbol_fa`` and for the ``{symbol}`` path
    parameter, so both spellings of فملي reach the same row.  ZWNJ is stripped
    here: a symbol is one token and nobody types the joiner reliably.
    """
    folded = unicodedata.normalize("NFKC", str(text or ""))
    folded = folded.translate(_INVISIBLE_AND_ZWNJ).translate(_ARABIC_TO_PERSIAN)
    return re.sub(r"\s+", " ", folded).strip()


def fold_name(text: object) -> str:
    """Fold a company name for display, KEEPING ZWNJ.

    In a name the joiner is a word separator: stripping it renders
    ``معدنی‌وصنعتی‌چادرملو`` as one glued-together word.  A key and a label want
    different normalisations, so they get different functions rather than one
    function with a flag nobody remembers to pass.
    """
    folded = unicodedata.normalize("NFKC", str(text or ""))
    folded = folded.translate(_INVISIBLE).translate(_ARABIC_TO_PERSIAN)
    return re.sub(r"[ \t]+", " ", folded).strip()


# --- records ----------------------------------------------------------------


@dataclass(frozen=True)
class Bar:
    """One daily bar, exactly as TSETMC served it.

    Nothing here is adjusted, and nothing here is derived: every field is one
    field of the payload, converted to a Python type.  ``final_close`` is
    ``pClosing`` and ``close`` is ``pDrCotVal``; they are different numbers and
    conflating them is defect (2) in the module docstring.
    """

    ins_code: str
    trade_date: date
    open: float
    high: float
    low: float
    close: float           # pDrCotVal, the last trade
    final_close: float     # pClosing, TSE's official قیمت پایانی
    price_yesterday: float
    volume: int
    trade_count: int
    value: float

    @property
    def traded(self) -> bool:
        """False on a halt placeholder: TSETMC emits a bar with no session in it.

        9,521 of the 77,344 measured bars are these.  Most -- but NOT every one --
        carry
        ``final_close == price_yesterday``, i.e. the previous close held
        forward, and open/high/low are frequently 0 — a zero there means "no
        trade occurred", which is why the schema does not require them
        positive.
        """
        return self.volume > 0 or self.trade_count > 0


# The two ways TSETMC signals an action.  Both were measured; the second was
# found only by running the detector over all twenty roster symbols.
#
# 'reference_restated' (443 of 475 measured actions): the session OPENED on a
#     new basis, so this bar's priceYesterday disagrees with the previous
#     session's closing price.  ratio = price_yesterday / prev_close.
# 'close_restated' (29 (in six symbols)): the action was applied to the CLOSING
#     PRICE of a bar on which nothing traded, and the next bar's
#     priceYesterday simply agrees with the new number.  شپنا 2014-03-16 is the
#     clearest case: a zero-volume bar whose priceYesterday is 27,031 and whose
#     pClosing is 11,251.  A detector that only compares across bars sees
#     nothing there and leaves a -58.4% artefact in the series.
#     ratio = restated_close / price_yesterday.
KIND_REFERENCE_RESTATED = "reference_restated"
KIND_CLOSE_RESTATED = "close_restated"


@dataclass(frozen=True)
class CorporateAction:
    """One detected action, with both numbers that imply it.

    ``ratio`` is stored rather than left to be recomputed so a row whose ratio
    disagrees with its two inputs is visible; ``cumulative_factor`` is the
    product of this ratio and every LATER one, which is what makes the
    adjustment a single multiplication at read time (see :func:`adjust`).

    ``prev_close`` and ``price_yesterday`` are the audit pair for a
    ``reference_restated`` action.  For a ``close_restated`` one the pair is
    ``price_yesterday`` and ``restated_close``, which is why that column
    exists rather than the ratio being left unexplainable on 32 rows.
    """

    effective_date: date
    prev_trade_date: date
    prev_close: float
    price_yesterday: float
    ratio: float
    cumulative_factor: float
    kind: str = KIND_REFERENCE_RESTATED
    restated_close: Optional[float] = None


@dataclass(frozen=True)
class SessionMove:
    """One adjusted return, and what kind of boundary it crossed."""

    trade_date: date
    prev_trade_date: date
    gap_days: int
    ret: float
    # False when either bar was a halt placeholder or the gap exceeds
    # SESSION_GAP_DAYS.  Those are reopenings; the price limit does not bind
    # a reopening auction, so they are reported, not used to refuse.
    is_session: bool


@dataclass(frozen=True)
class Validation:
    """The gate's verdict on one adjusted series."""

    status: str                       # validated | refused
    sessions_checked: int
    # Reopening moves beyond MAX_SESSION_RETURN.  Not failures — the exchange
    # lifts the price limit for a reopening auction — but a return computed
    # across one of these days is not a session return, and a reader is told
    # how many the series contains.
    reopenings: int
    worst_return: Optional[float]
    worst_return_date: Optional[date]
    violations: tuple[SessionMove, ...]
    max_session_return: float
    max_reopening_return: float
    session_gap_days: int
    refusal_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_VALIDATED


@dataclass(frozen=True)
class AdjustedSeries:
    """Raw bars, the actions found in them, and the verdict on the result."""

    ins_code: str
    version: str
    bars: tuple[Bar, ...]             # RAW, ascending, exactly as served
    actions: tuple[CorporateAction, ...]
    # Index of the first traded bar.  ``bars[:pre_listing_bars]`` are the
    # placeholder bars that precede an instrument's first trade; they are
    # stored raw and excluded from everything computed here.
    pre_listing_bars: int
    adjusted_closes: tuple[float, ...]  # aligned with bars[pre_listing_bars:]
    validation: Validation

    @property
    def traded_bars(self) -> tuple[Bar, ...]:
        return self.bars[self.pre_listing_bars:]

    @property
    def raw_ratio(self) -> float:
        """Last raw close over first, on the traded span.  The nonsense number."""
        span = self.traded_bars
        return span[-1].final_close / span[0].final_close if span else 0.0

    @property
    def adjusted_ratio(self) -> float:
        """Last adjusted close over first.  The one worth reporting."""
        closes = self.adjusted_closes
        return closes[-1] / closes[0] if closes else 0.0


# --- parsing ----------------------------------------------------------------


def parse_deven(value: object) -> date:
    """Parse TSETMC's ``dEven`` (an integer ``YYYYMMDD``) to a date.

    It is GREGORIAN, despite every human-facing date on TSETMC being Jalali.
    Verified on the roster payloads: فولاد's newest bar is 20260909 and the
    exchange traded on 2026-09-09.  Reading it as Jalali would place the whole
    series 621 years early and every date filter would silently return nothing.

    The bound is not decoration.  A ``dEven`` of 0 appears in TSETMC's
    ``sector`` and ``staticThreshold`` sub-objects as "not stated", and a bare
    ``int()`` on it yields year 0 — which ``datetime.date`` accepts on some
    inputs and rejects on others, so the failure would be inconsistent rather
    than absent.
    """
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise BarParseError(f"dEven {value!r} is not an integer date") from exc
    if not (19000101 <= n <= 21001231):
        raise BarParseError(
            f"dEven {n} is outside 19000101..21001231. TSETMC's dEven is a "
            "GREGORIAN YYYYMMDD; a value outside this band means the field "
            "changed meaning or a non-bar object is being parsed as one."
        )
    try:
        return date(n // 10000, (n // 100) % 100, n % 100)
    except ValueError as exc:
        raise BarParseError(f"dEven {n} is not a real calendar date") from exc


def _number(row: dict, key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BarParseError(f"{key}={value!r} is not a number")
    return float(value)


def parse_daily_list(payload: Any, ins_code: str = "") -> list[Bar]:
    """Parse a ``GetClosingPriceDailyList`` payload into ascending bars.

    ``ins_code``, when given, is the instrument the CALLER believes this
    payload is for, and a mismatch is refused rather than reconciled: the
    payload states its own ``insCode`` on every row, so ingesting فولاد's bars
    under خودرو's code is a mistake this can catch for free and nothing
    downstream ever could.

    Refuses, rather than repairs: a missing field, a non-numeric price, a
    ``dEven`` that is not a real date, two rows for the same day, or rows for
    more than one instrument.  Everything else — zeros on a halted session, a
    closing price outside the day's range, a ``priceYesterday`` of 0 on the
    very first bar — is real and is passed through untouched.
    """
    if not isinstance(payload, dict):
        raise BarParseError(f"payload is {type(payload).__name__}, expected an object")
    rows = payload.get(FIELD_LIST)
    if not isinstance(rows, list):
        raise BarParseError(
            f"payload has no {FIELD_LIST!r} list; TSETMC's daily-bar response "
            f"carries one. Keys present: {sorted(payload)[:8]}"
        )
    if not rows:
        raise BarParseError(
            f"{FIELD_LIST} is empty: TSETMC returned no bars for this instrument"
        )

    bars: list[Bar] = []
    seen: dict[date, int] = {}
    codes: set[str] = set()
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BarParseError(f"row {index} is {type(row).__name__}, expected an object")
        missing = [f for f in REQUIRED_FIELDS if f not in row]
        if missing:
            raise BarParseError(
                f"row {index} is missing {', '.join(missing)}; the payload shape changed"
            )
        # insCode arrives as a string on this endpoint and as a number on
        # others, so it is compared as text on both sides rather than coerced
        # to an int that would lose nothing but could overflow a JS caller.
        codes.add(str(row["insCode"]).strip())
        trade_date = parse_deven(row["dEven"])
        if trade_date in seen:
            raise BarParseError(
                f"two bars for {trade_date.isoformat()} (rows {seen[trade_date]} and "
                f"{index}). A duplicated session would be counted twice by the "
                "corporate-action walk and shift every factor after it."
            )
        seen[trade_date] = index
        final_close = _number(row, "pClosing")
        if final_close <= 0:
            raise BarParseError(
                f"{trade_date.isoformat()}: pClosing is {final_close}. Every return "
                "and every action ratio divides by it, so a non-positive closing "
                "price is refused rather than propagated as an infinity."
            )
        bars.append(
            Bar(
                ins_code=str(row["insCode"]).strip(),
                trade_date=trade_date,
                open=_number(row, "priceFirst"),
                high=_number(row, "priceMax"),
                low=_number(row, "priceMin"),
                close=_number(row, "pDrCotVal"),
                final_close=final_close,
                price_yesterday=_number(row, "priceYesterday"),
                volume=int(_number(row, "qTotTran5J")),
                trade_count=int(_number(row, "zTotTran")),
                value=_number(row, "qTotCap"),
            )
        )

    if len(codes) != 1:
        raise BarParseError(
            f"payload mixes {len(codes)} instruments ({', '.join(sorted(codes)[:4])}); "
            "one file is one instrument"
        )
    payload_code = codes.pop()
    if ins_code and payload_code != str(ins_code).strip():
        raise BarParseError(
            f"payload carries insCode {payload_code} but was offered as {ins_code}. "
            "Refusing rather than trusting the label: storing one company's bars "
            "under another's code is invisible afterwards."
        )
    # TSETMC serves newest-first; the walk below needs oldest-first and says so
    # rather than assuming the order it happens to receive.
    bars.sort(key=lambda bar: bar.trade_date)
    return bars


# --- detection and chaining --------------------------------------------------


def first_traded_index(bars: Sequence[Bar]) -> int:
    """Index of the first bar with a trade in it.

    Only the LEADING run is skipped.  An interior zero-volume bar is a halt,
    and فولاد's 2022 capital increase is restated across four of them, so
    dropping them wholesale loses a real corporate action (defect 3 in the
    module docstring).
    """
    for index, bar in enumerate(bars):
        if bar.traded:
            return index
    return len(bars)


def detect_actions(bars: Sequence[Bar]) -> list[CorporateAction]:
    """Find every restatement of the price basis, oldest first.

    TSETMC signals an action in TWO ways, and a detector that knows only the
    first leaves a large artefact in one symbol in three:

    1. ``reference_restated`` — the bar's ``price_yesterday`` disagrees with
       the previous bar's ``final_close``.  The exchange restated the opening
       reference, and the ratio is those two numbers.  443 of the 472 actions
       measured across the roster.

    2. ``close_restated`` — a bar on which NOTHING TRADED whose own
       ``final_close`` disagrees with its own ``price_yesterday``.  No session
       happened, so that difference is not a price move; the exchange applied
       the action to the closing price in place.  32 such bars exist; 29 are
       detected, the other 3 being each instrument's FIRST bar where
       price_yesterday is 0. In six symbols,
       and every one of them is invisible to rule 1: شپنا 2014-03-16
       (27,031 -> 11,251) and وبملت 2012-09-26 (1,533 -> 926) are both real
       capital increases that a priceYesterday-only detector leaves as -58%
       and -40% artefacts.

    The two never co-occur on one bar in the measured set, which is why an
    action is keyed by date alone.  Nothing is inferred, thresholded or
    rounded: a one-rial disagreement is an action, because the exchange does
    not restate a reference price by accident.

    ``cumulative_factor`` is filled in by :func:`chain_factors`; the actions
    returned here carry a placeholder of 1.0, because the cumulative value is
    only defined once the whole series is known.
    """
    actions: list[CorporateAction] = []
    for index in range(1, len(bars)):
        current, previous = bars[index], bars[index - 1]

        if current.price_yesterday != previous.final_close:
            if current.price_yesterday <= 0 or previous.final_close <= 0:
                # Only an instrument's very first bar legitimately carries
                # price_yesterday = 0 (there is no yesterday), and that bar is
                # never the `current` of a pair.  Anywhere else a zero makes
                # the ratio 0 or infinite and collapses the whole chain before
                # it, silently, to zero.
                raise BarParseError(
                    f"{current.trade_date.isoformat()}: cannot derive an action "
                    f"ratio from priceYesterday={current.price_yesterday} and "
                    f"previous close={previous.final_close}. A non-positive "
                    "reference price mid-series would zero every adjusted close "
                    "before this date."
                )
            actions.append(
                CorporateAction(
                    effective_date=current.trade_date,
                    prev_trade_date=previous.trade_date,
                    prev_close=previous.final_close,
                    price_yesterday=current.price_yesterday,
                    ratio=current.price_yesterday / previous.final_close,
                    cumulative_factor=1.0,
                    kind=KIND_REFERENCE_RESTATED,
                )
            )
            continue

        if not current.traded and current.final_close != current.price_yesterday:
            if current.price_yesterday <= 0:
                raise BarParseError(
                    f"{current.trade_date.isoformat()}: a halted bar restates its "
                    f"close to {current.final_close} against a reference of "
                    f"{current.price_yesterday}, which cannot be divided by."
                )
            actions.append(
                CorporateAction(
                    effective_date=current.trade_date,
                    prev_trade_date=previous.trade_date,
                    prev_close=previous.final_close,
                    price_yesterday=current.price_yesterday,
                    ratio=current.final_close / current.price_yesterday,
                    cumulative_factor=1.0,
                    kind=KIND_CLOSE_RESTATED,
                    restated_close=current.final_close,
                )
            )
    return actions


def chain_factors(actions: Sequence[CorporateAction]) -> list[CorporateAction]:
    """Fill in each action's cumulative factor, chaining from the newest.

    ``cumulative_factor(a_k) = product of ratio(a_j) for every j >= k``.  A bar
    is then adjusted by the cumulative factor of the FIRST action whose
    effective date is strictly after it, and by 1.0 when no action follows —
    the newest segment of the series is left at its raw prices, which is what
    "back-adjusted" means and what makes today's adjusted close equal today's
    real one.

    This is the same arithmetic as walking the bars newest-first and
    multiplying a running factor at each action; the equality was checked
    numerically over فولاد's 4,636 bars (maximum relative difference 0.0).
    Storing it per action rather than per bar is why 0028 has no
    ``equity_bars_adjusted`` table: فولاد's adjustment is 31 numbers, not
    4,636, and it does not need rewriting in full whenever a new action lands.
    """
    chained: list[CorporateAction] = []
    running = 1.0
    for action in reversed(actions):
        running *= action.ratio
        chained.append(
            CorporateAction(
                effective_date=action.effective_date,
                prev_trade_date=action.prev_trade_date,
                prev_close=action.prev_close,
                price_yesterday=action.price_yesterday,
                ratio=action.ratio,
                cumulative_factor=running,
                kind=action.kind,
                restated_close=action.restated_close,
            )
        )
    chained.reverse()
    return chained


def apply_factors(
    bars: Sequence[Bar], actions: Sequence[CorporateAction]
) -> list[float]:
    """Back-adjusted closing prices for ``bars``, in the same order.

    Walks both sequences once (both ascending), so this is linear rather than a
    binary search per bar.  ``bars`` is expected to be the TRADED span — the
    caller drops the pre-listing placeholders first — but nothing here depends
    on that beyond the factors being right for the dates it is given.
    """
    factors = [action.cumulative_factor for action in actions]
    dates = [action.effective_date for action in actions]
    out: list[float] = []
    cursor = 0
    for bar in bars:
        while cursor < len(dates) and dates[cursor] <= bar.trade_date:
            cursor += 1
        out.append(bar.final_close * (factors[cursor] if cursor < len(dates) else 1.0))
    return out


# --- the gate ----------------------------------------------------------------


def session_moves(
    bars: Sequence[Bar],
    adjusted: Sequence[float],
    session_gap_days: int = SESSION_GAP_DAYS,
) -> list[SessionMove]:
    """Every consecutive adjusted return, labelled session or reopening.

    A pair is a SESSION when both bars traded and they are at most
    ``session_gap_days`` apart.  Otherwise it spans a halt or a holiday block,
    the reopening auction is not price-limited, and no bound a normal session
    obeys applies to it.
    """
    moves: list[SessionMove] = []
    for index in range(1, len(bars)):
        current, previous = bars[index], bars[index - 1]
        gap = (current.trade_date - previous.trade_date).days
        moves.append(
            SessionMove(
                trade_date=current.trade_date,
                prev_trade_date=previous.trade_date,
                gap_days=gap,
                ret=adjusted[index] / adjusted[index - 1] - 1.0,
                is_session=(
                    current.traded and previous.traded and gap <= session_gap_days
                ),
            )
        )
    return moves


def validate(
    moves: Sequence[SessionMove],
    max_session_return: float = MAX_SESSION_RETURN,
    max_reopening_return: float = MAX_REOPENING_RETURN,
    session_gap_days: int = SESSION_GAP_DAYS,
) -> Validation:
    """Decide whether returns may be computed from this adjusted series.

    TWO TIERS, because the two populations obey different rules.

    *A genuine session* — both bars traded, at most ``session_gap_days`` apart —
    cannot move beyond ``max_session_return`` under the exchange's price limit.
    One that does means an action was missed, and a missed action silently
    corrupts every return computed from the series, not only the day it landed
    on.  The whole symbol is refused.

    *A reopening* — either bar halted, or a longer gap — is auctioned without a
    price limit, and 48 of the 11,269 measured reopenings exceed 25% across
    fifteen of the twenty roster symbols.  فولاد's own -39.2% on 2008-10-26
    follows a 43-day suspension and TSETMC's ``priceYesterday`` on that bar
    agrees with the previous close: refusing on it would refuse the symbol this
    engine is validated against.  So these are held to ``max_reopening_return``
    instead, which is a bound on the ARITHMETIC rather than on the market — it
    catches a chain that broke, not a market that moved — and every reopening
    beyond the session bound is COUNTED so the caveat travels with the series.

    WHAT THIS GATE CANNOT DO, stated rather than left to be discovered.  An
    action is detected from what TSETMC states about the price basis.  If
    TSETMC states nothing for an action that falls on a reopening — and 30 of
    فولاد's 31 actions do fall on one, because Iranian capital increases happen
    during the AGM suspension — the resulting move is indistinguishable from a
    genuine unlimited auction, and nothing in this payload separates them.  The
    defence against that is the DETECTOR, not the gate: :func:`detect_actions`
    reads both of the ways TSETMC signals a restatement, and the second of them
    (a close restated on a halted bar) accounted for 32 actions and, in one
    symbol, a factor of 5.5 in the whole history.
    """
    sessions = [move for move in moves if move.is_session]
    reopenings = [move for move in moves if not move.is_session]
    over_session = [
        move for move in sessions if abs(move.ret) > max_session_return
    ]
    over_reopening = [
        move for move in reopenings if abs(move.ret) > max_reopening_return
    ]
    violations = tuple(over_session + over_reopening)
    worst = max(sessions, key=lambda move: abs(move.ret), default=None)

    reasons: list[str] = []
    if over_session:
        reasons.append(
            f"{len(over_session)} single-session move(s) beyond "
            f"{max_session_return:.0%} survive adjustment: "
            f"{_describe(over_session)}. A session cannot move that far under "
            "the exchange's price limit, so a corporate action was missed — and "
            "a missed action corrupts every return computed from this series, "
            "not only these days."
        )
    if over_reopening:
        reasons.append(
            f"{len(over_reopening)} reopening move(s) beyond "
            f"{max_reopening_return:.0%}: {_describe(over_reopening)}. A "
            "reopening auction has no price limit, but no reopening in 77,344 "
            "measured bars exceeded 2.43x; a jump this large means the factor "
            "chain itself is wrong, not that the market moved."
        )
    reason = ""
    if reasons:
        reason = (
            " ".join(reasons)
            + " Refusing the symbol rather than serving adjusted prices that "
            "cannot be right."
        )
    return Validation(
        status=STATUS_REFUSED if violations else STATUS_VALIDATED,
        sessions_checked=len(sessions),
        reopenings=sum(
            1 for move in reopenings if abs(move.ret) > max_session_return
        ),
        worst_return=worst.ret if worst else None,
        worst_return_date=worst.trade_date if worst else None,
        violations=violations,
        max_session_return=max_session_return,
        max_reopening_return=max_reopening_return,
        session_gap_days=session_gap_days,
        refusal_reason=reason,
    )


def _describe(moves: Sequence[SessionMove], limit: int = 5) -> str:
    """The first few offending moves, with the dates that make them findable."""
    shown = ", ".join(
        f"{move.trade_date.isoformat()} {move.ret:+.1%} "
        f"(from {move.prev_trade_date.isoformat()}, {move.gap_days}d)"
        for move in moves[:limit]
    )
    return shown if len(moves) <= limit else f"{shown} and {len(moves) - limit} more"


def adjust(
    bars: Sequence[Bar],
    max_session_return: float = MAX_SESSION_RETURN,
    max_reopening_return: float = MAX_REOPENING_RETURN,
    session_gap_days: int = SESSION_GAP_DAYS,
) -> AdjustedSeries:
    """Detect, chain, adjust and validate one instrument's bars.

    Returns the verdict rather than raising on a refusal: the caller stores the
    raw bars and the detected actions either way — they are what TSETMC served
    and what it implied, and both stay true — and only the PROMOTION of the
    series to "returns may be computed from this" is withheld.  Throwing away
    the evidence because the conclusion failed would leave nothing to diagnose
    the refusal with.
    """
    if not bars:
        raise BarParseError("no bars to adjust")
    ordered = sorted(bars, key=lambda bar: bar.trade_date)

    # Detection runs over ALL bars, including halt placeholders: فولاد's 2022
    # capital increase is restated in two steps across four zero-volume bars.
    actions = chain_factors(detect_actions(ordered))

    # Adjustment and validation run over the TRADED span only: the placeholder
    # bars before an instrument's first trade are at the 1,000-rial par value
    # and are prices of nothing.
    lead = first_traded_index(ordered)
    if lead >= len(ordered):
        raise BarParseError(
            f"{ordered[0].ins_code}: none of the {len(ordered)} bars carries a "
            "trade. TSETMC has registered the instrument but it has never "
            "traded, so there is no price series to adjust."
        )
    traded = ordered[lead:]
    adjusted = apply_factors(traded, actions)
    validation = validate(
        session_moves(traded, adjusted, session_gap_days),
        max_session_return=max_session_return,
        max_reopening_return=max_reopening_return,
        session_gap_days=session_gap_days,
    )
    if validation.violations:
        log.warning(
            "equity adjustment refused for %s (%s): %s",
            ordered[0].ins_code, ADJUSTMENT_VERSION, validation.refusal_reason,
        )
    return AdjustedSeries(
        ins_code=ordered[0].ins_code,
        version=ADJUSTMENT_VERSION,
        bars=tuple(ordered),
        actions=tuple(actions),
        pre_listing_bars=lead,
        adjusted_closes=tuple(adjusted),
        validation=validation,
    )


def adjust_payload(
    payload: Any,
    ins_code: str = "",
    max_session_return: float = MAX_SESSION_RETURN,
    max_reopening_return: float = MAX_REOPENING_RETURN,
    session_gap_days: int = SESSION_GAP_DAYS,
) -> AdjustedSeries:
    """:func:`parse_daily_list` then :func:`adjust`, for a raw TSETMC payload."""
    return adjust(
        parse_daily_list(payload, ins_code=ins_code),
        max_session_return=max_session_return,
        max_reopening_return=max_reopening_return,
        session_gap_days=session_gap_days,
    )


def adjusted_pairs(series: AdjustedSeries) -> Iterable[tuple[Bar, float]]:
    """``(raw bar, adjusted close)`` over the traded span."""
    return zip(series.traded_bars, series.adjusted_closes)
