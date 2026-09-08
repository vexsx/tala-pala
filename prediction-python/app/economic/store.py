"""Bitemporal write and read layer for economic observations.

Four rules this module exists to enforce, none of which a caller can opt out
of by writing the tables directly (they should not):

*Nothing is ever updated in place.*  A source that restates a number produces
a NEW row with ``vintage = max + 1``.  The first print stays readable forever,
which is the only reason a backtest can be honest about what it knew.

*Point-in-time reads filter on ``available_at``, never ``published_at``.*  The
same rule migration 0017 states for ``news_articles``.  ``published_at`` is a
claim by the source and is NULL for both P0 providers; ``available_at`` is
evidence about this system.

*``available_at`` is the collection time, and it only ever moves forward.*
Neither the World Bank nor the IMF DataMapper stamps an individual observation
with a publication moment (the World Bank states one dataset-wide
``lastupdated``, which is emphatically not the publication date of its 1960
datapoint).  So the only defensible answer to "when could this system first
have known this value?" is "when the payload arrived", and that is what the job
stamps — one instant for a whole fetch.  The consequence is stated plainly
rather than hidden: for history that existed before this system did,
``available_at`` is the day we first collected it, not the day the world
learned it.  A backtest cutoff earlier than the first ingest therefore sees
nothing, which is correct — we knew nothing.  Because the reader picks the
winning row by ``available_at``, a new vintage that claimed to be knowable
BEFORE the print it revises would serve a revision at a cutoff where only the
first print existed — look-ahead, in the one table built to prevent it.  So
:func:`record_observation` refuses such a write instead of asserting it cannot
happen; see its docstring.

*A stored ``is_projection``/``is_nowcast`` flag is frozen with its row.*
Neither flag is stated by these sources; both are computed from the clock at
fetch time.  A flag is therefore a fact about the moment a value was written,
never a fact re-derived later — the calendar rolling over must not turn a
stored forecast into an observation.  :func:`record_observation` enforces this
by deciding "unchanged" on the VALUE alone.
"""
from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterator, Optional, Union

from sqlalchemy import Select, and_, func, select
from sqlalchemy.engine import Connection, Engine

from ..db import (
    economic_observations,
    economic_series,
    ensure_utc,
    instruments,
    utcnow,
)
from .catalog import SeriesSpec

log = logging.getLogger(__name__)

Bind = Union[Engine, Connection]

KIND_ECONOMIC_SERIES = "economic_series"

STATUS_INSERTED = "inserted"
STATUS_UNCHANGED = "unchanged"
STATUS_REVISED = "revised"
# A concurrent writer inserted this exact vintage first; see the ON CONFLICT
# comment in record_observation for the guarantee that makes this safe.
STATUS_RACED = "raced"

# --- how "the same value" is decided ----------------------------------------
#
# Comparison is numeric, never textual: the incoming number is a JSON float
# (4030.3356899712) and the stored one has round-tripped through Postgres
# NUMERIC, so comparing their string forms would call a formatting difference
# a revision and grow a vintage on every single ingest.
#
# Why a tolerance rather than ``==``: the round trip through NUMERIC and back
# is a decimal/binary conversion in each direction, and it is not contractually
# bit-identical across driver and server versions.
#
# Why 1e-15 relative, and why the window is this narrow.  The tolerance has to
# sit ABOVE representation noise and BELOW the smallest revision a source can
# actually print, and for this data those are only about two orders of
# magnitude apart:
#
#   * noise floor: 1 unit in the last place of a float64 is ~2.2e-16 relative,
#     so a couple of ULP of drift is ~5e-16.
#   * smallest printable revision: the World Bank publishes ~14 significant
#     digits (4030.3356899712), so its last digit is 1e-10 on a value of ~4030
#     — 2.5e-14 relative, roughly 110 ULP.  The IMF's one-decimal prints are
#     coarser by another twelve orders of magnitude.
#
# 1e-15 lands between them (~4.5 ULP): several times the noise it must absorb,
# and ~25x tighter than the finest revision either publisher can express, so no
# restatement they are capable of printing can hide underneath it.
VALUE_REL_TOLERANCE = 1e-15
# Absolute floor, because relative tolerance is meaningless at zero: an
# inflation print of 0.0 or -0.9 must still compare sensibly.  1e-15 is far
# below any published precision in this domain (the IMF prints one decimal).
VALUE_ABS_TOLERANCE = 1e-15


def values_equal(left: float, right: float) -> bool:
    """True when two values are the same number to within the tolerance above."""
    return math.isclose(
        float(left), float(right),
        rel_tol=VALUE_REL_TOLERANCE, abs_tol=VALUE_ABS_TOLERANCE,
    )


# --- records ----------------------------------------------------------------


@dataclass(frozen=True)
class ObservationWrite:
    """What :func:`record_observation` did, and to which vintage."""

    status: str                       # inserted | unchanged | revised | raced
    vintage: int
    previous_value: Optional[float] = None
    previous_vintage: Optional[int] = None


@dataclass(frozen=True)
class EconomicObservation:
    """One stored observation, as a point-in-time read returns it."""

    ref_period_start: date
    ref_period_end: date
    ref_period_label: str
    value: float
    vintage: int
    available_at: datetime
    published_at: Optional[datetime]
    is_projection: bool
    is_nowcast: bool
    source_document_id: Optional[int]
    collected_at: Optional[datetime]


@dataclass(frozen=True)
class PointInTimeSeries:
    """A series as of one instant, plus which vintages that answer came from."""

    code: str
    series_id: int
    as_of: datetime
    observations: tuple[EconomicObservation, ...]
    # The vintages actually used to answer.  {1} means "nothing here has ever
    # been revised as far as this cutoff can see"; {1, 2} means the answer
    # mixes a first print with a revision, which is exactly what a reader
    # needs to be told before trusting the series.
    vintages: frozenset[int]
    # Echoed back because it changes what the answer MEANS: with projections
    # excluded (the default) a period the source has only forecast is absent,
    # which is not the same statement as "the source has no value for it".
    include_projections: bool = False

    @property
    def is_revised(self) -> bool:
        return any(vintage > 1 for vintage in self.vintages)


# --- connection plumbing ----------------------------------------------------


@contextmanager
def _write(bind: Bind) -> Iterator[Connection]:
    """A connection in a transaction.

    Passing an :class:`Engine` opens and commits one transaction; passing a
    :class:`Connection` joins the caller's, which is what lets the job wrap a
    whole series in one transaction while these functions stay callable on
    their own.
    """
    if isinstance(bind, Engine):
        with bind.begin() as conn:
            yield conn
    else:
        yield bind


@contextmanager
def _read(bind: Bind) -> Iterator[Connection]:
    if isinstance(bind, Engine):
        with bind.connect() as conn:
            yield conn
    else:
        yield bind


def _dialect_insert(conn: Connection):
    """The dialect's INSERT construct (the one that speaks ON CONFLICT)."""
    name = conn.dialect.name
    if name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    elif name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as dialect_insert
    else:  # pragma: no cover - production is PostgreSQL, tests are SQLite
        raise NotImplementedError(
            f"economic store: no upsert support for dialect {name!r}"
        )
    return dialect_insert


# --- writes -----------------------------------------------------------------

# Columns the catalog owns and re-asserts on every ingest.  ``enabled`` is
# NOT among them, in either table: the catalog declares what a series IS, the
# operator decides whether it runs, and an ingest must not silently re-enable
# a series somebody turned off.
_INSTRUMENT_MANAGED = (
    "kind", "name_en", "name_fa", "domain", "quote_currency", "unit",
    "decimals", "calendar_class", "quality_tier", "notes",
)
_SERIES_MANAGED = (
    "frequency", "calendar", "measure", "seasonal_adjustment", "base_period",
    "provider_code", "provider_series_id", "publication_lag_days", "revisable",
    "splice_policy",
)


def ensure_series(bind: Bind, entry: SeriesSpec) -> int:
    """Upsert the ``instruments`` + ``economic_series`` rows; return series id.

    Idempotent, and rows only — Python never issues DDL (see :mod:`app.db`).
    Re-running it after a catalog edit updates the descriptive columns, which
    is the point: the catalog is the source of truth and the rows are its
    projection.

    Refuses to redefine an instrument that already exists with another kind.
    A code collision with, say, a market price is a catalog bug, and quietly
    rewriting ``IR_GOLD_18K`` into an economic series would corrupt every
    reader that projects this vocabulary.
    """
    with _write(bind) as conn:
        insert = _dialect_insert(conn)

        existing_kind = conn.execute(
            select(instruments.c.kind).where(instruments.c.code == entry.code)
        ).scalar()
        if existing_kind is not None and existing_kind != KIND_ECONOMIC_SERIES:
            raise ValueError(
                f"instrument {entry.code} already exists with kind="
                f"{existing_kind!r}; refusing to redefine it as an economic series"
            )

        now = utcnow()
        instrument_values = {
            "code": entry.code,
            "kind": entry.kind,
            "name_en": entry.name_en,
            "name_fa": entry.name_fa,
            "domain": entry.domain,
            "quote_currency": entry.quote_currency,
            "unit": entry.unit,
            "decimals": int(entry.decimals),
            "calendar_class": entry.calendar_class,
            "quality_tier": entry.quality_tier,
            "is_proxy": False,
            "is_derived": False,
            "enabled": bool(entry.enabled),
            "notes": entry.notes,
            "created_at": now,
            "updated_at": now,
        }
        stmt = insert(instruments).values(**instrument_values)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[instruments.c.code],
                set_={
                    name: stmt.excluded[name] for name in _INSTRUMENT_MANAGED
                } | {"updated_at": now},
            )
        )

        series_values = {
            "code": entry.code,
            "frequency": entry.frequency,
            "calendar": entry.calendar,
            "measure": entry.measure,
            "seasonal_adjustment": entry.seasonal_adjustment,
            "base_period": entry.base_period,
            "provider_code": entry.provider_code,
            "provider_series_id": entry.provider_series_id,
            "publication_lag_days": entry.publication_lag_days,
            "revisable": bool(entry.revisable),
            "splice_policy": entry.splice_policy,
            "enabled": bool(entry.enabled),
            "created_at": now,
            "updated_at": now,
        }
        stmt = insert(economic_series).values(**series_values)
        conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[economic_series.c.code],
                set_={name: stmt.excluded[name] for name in _SERIES_MANAGED}
                | {"updated_at": now},
            )
        )

        # Read the id back rather than relying on RETURNING through an upsert:
        # one cheap indexed lookup, identical on both dialects.
        series_id = conn.execute(
            select(economic_series.c.id).where(economic_series.c.code == entry.code)
        ).scalar_one()
    return int(series_id)


def record_observation(
    bind: Bind,
    series_id: int,
    *,
    ref_period_start: date,
    ref_period_end: date,
    value: float,
    available_at: datetime,
    ref_period_label: str = "",
    published_at: Optional[datetime] = None,
    is_nowcast: bool = False,
    is_projection: bool = False,
    source_document_id: Optional[int] = None,
    collected_at: Optional[datetime] = None,
) -> ObservationWrite:
    """Store one observation under the vintage rule.

    * no row for this (series, period) yet -> insert ``vintage = 1``
    * the current row states the same VALUE -> write nothing, ``unchanged``
    * it states a different value -> insert ``vintage = max + 1``, ``revised``
    * a concurrent writer inserted that vintage first -> ``raced``, nothing
      written by us (see the ON CONFLICT comment below)

    An existing row is never UPDATEd, so a revision costs a row and preserves
    the print it replaced.

    **"The same value" is decided on the value alone.**  ``is_projection`` and
    ``is_nowcast`` are deliberately NOT part of that comparison, and this is the
    load-bearing decision of the module.  Neither P0 source states either flag:
    both are computed at fetch time from the clock
    (:func:`app.economic.is_incomplete_annual_period`), so the first ingest
    after a New Year re-delivers last year's unchanged figure with
    ``is_projection`` flipped from True to False.  Comparing the flags would
    store that as a new vintage carrying an IDENTICAL value — manufacturing a
    revision the source never made and reporting the series as revised.
    Comparing the value alone writes nothing, which is the truth: the source
    said nothing new.

    The flags are therefore FROZEN with the row that carries them.  A period
    first stored as a projection stays flagged as one until the source actually
    restates its value; the new vintage that restatement creates carries the
    flags as they are computed at THAT moment, honestly.  What a stored
    ``is_projection`` can never do is quietly become False because the calendar
    rolled over — which is the only way this system could have served a
    forecast as a measurement.

    ``available_at`` must not precede the newest stored vintage's
    ``available_at``: the reader picks the winning row by availability, so a
    row that claims to have been knowable earlier than the print it revises is
    look-ahead.  Such a write is REFUSED, naming both timestamps, rather than
    clamped — a clock that went backwards, or two writers with skewed clocks,
    is an operational fault, and quietly rewriting its timestamp would hide it.
    """
    if value is None:  # pragma: no cover - guarded by the callers too
        raise ValueError(
            "record_observation: value is None; a missing observation is "
            "skipped by the caller, never stored"
        )
    if ref_period_end < ref_period_start:
        raise ValueError(
            f"record_observation: ref_period_end {ref_period_end} precedes "
            f"ref_period_start {ref_period_start}"
        )

    if available_at is None:
        raise ValueError(
            "record_observation: available_at is required; it is when THIS "
            "system could first have known the value, and every point-in-time "
            "read filters on it"
        )

    numeric_value = float(value)
    stamped_available_at = ensure_utc(available_at)
    period_rows = and_(
        economic_observations.c.series_id == series_id,
        economic_observations.c.ref_period_start == ref_period_start,
    )
    with _write(bind) as conn:
        # Which row is "current" is the READER's question, so it gets the
        # reader's answer: :func:`point_in_time` ranks by availability first and
        # breaks ties on vintage, and a writer that ordered by vintage alone
        # would compare the incoming value against a row nobody is being served.
        current = conn.execute(
            select(
                economic_observations.c.vintage,
                economic_observations.c.value,
            )
            .where(period_rows)
            .order_by(
                economic_observations.c.available_at.desc(),
                economic_observations.c.vintage.desc(),
            )
            .limit(1)
        ).first()
        # Numbering and the availability invariant are properties of the whole
        # period, not of the current row: ``economic_obs_unique`` counts
        # max(vintage), and no new row may precede max(available_at).
        bounds = conn.execute(
            select(
                func.max(economic_observations.c.vintage),
                func.max(economic_observations.c.available_at),
            ).where(period_rows)
        ).first()
        max_vintage = int(bounds[0]) if bounds is not None and bounds[0] is not None else 0
        newest_available_at = (
            ensure_utc(bounds[1]) if bounds is not None and bounds[1] is not None else None
        )

        if current is None:
            status, vintage = STATUS_INSERTED, 1
            previous_value = previous_vintage = None
        else:
            previous_vintage = int(current.vintage)
            previous_value = float(current.value)
            if values_equal(previous_value, numeric_value):
                # Nothing to store, so nothing to check: an ingest that writes
                # no row cannot introduce look-ahead, whatever its clock says.
                return ObservationWrite(
                    status=STATUS_UNCHANGED,
                    vintage=previous_vintage,
                    previous_value=previous_value,
                    previous_vintage=previous_vintage,
                )
            status, vintage = STATUS_REVISED, max_vintage + 1

        if newest_available_at is not None and stamped_available_at < newest_available_at:
            raise ValueError(
                "record_observation: refusing to store series "
                f"{series_id} period {ref_period_start} with available_at "
                f"{stamped_available_at.isoformat()}, which is EARLIER than the "
                "newest stored vintage's available_at "
                f"{newest_available_at.isoformat()}. A vintage that claims to "
                "have been knowable before the print it revises would be served "
                "by a point-in-time read at a cutoff where only the earlier "
                "print existed. Fix the clock (or the caller's timestamp); this "
                "is not clamped, because a backwards clock is a fault worth "
                "seeing."
            )

        insert_stmt = _dialect_insert(conn)(economic_observations).values(
            series_id=series_id,
            ref_period_start=ref_period_start,
            ref_period_end=ref_period_end,
            ref_period_label=ref_period_label,
            value=numeric_value,
            published_at=ensure_utc(published_at),
            available_at=stamped_available_at,
            vintage=vintage,
            is_nowcast=bool(is_nowcast),
            is_projection=bool(is_projection),
            source_document_id=source_document_id,
            collected_at=ensure_utc(collected_at) or utcnow(),
        )
        # The guarantee chosen here: AT MOST ONE row per
        # (series_id, ref_period_start, vintage) — exactly the key
        # ``economic_obs_unique`` already enforces — and the loser of a race
        # writes NOTHING instead of raising, which on PostgreSQL would abort the
        # transaction carrying the rest of the series.
        #
        # Two overlapping passes (the cron and a manual POST) read the same
        # max vintage and compute the same number, so the row that lands second
        # is a duplicate of the row that landed first: same period, same value,
        # from the same payload. Dropping it loses nothing. If the two passes
        # genuinely disagreed — different payloads, one mid-revision — the loser
        # is only DEFERRED: the next pass compares its value against the stored
        # winner and writes it as a real revision at vintage+1.
        #
        # DO NOTHING, never DO UPDATE: this table is append-only, and an UPDATE
        # here would overwrite a print somebody may already have been served.
        result = conn.execute(
            insert_stmt.on_conflict_do_nothing(
                index_elements=[
                    economic_observations.c.series_id,
                    economic_observations.c.ref_period_start,
                    economic_observations.c.vintage,
                ]
            )
        )
        if result.rowcount == 0:
            log.warning(
                "economic store: series %s period %s vintage %d was already "
                "written by a concurrent pass; this write was dropped",
                series_id, ref_period_start, vintage,
            )
            return ObservationWrite(
                status=STATUS_RACED,
                vintage=vintage,
                previous_value=previous_value,
                previous_vintage=previous_vintage,
            )
    return ObservationWrite(
        status=status,
        vintage=vintage,
        previous_value=previous_value,
        previous_vintage=previous_vintage,
    )


# --- reads ------------------------------------------------------------------

_PIT_COLUMNS = (
    "ref_period_start", "ref_period_end", "ref_period_label", "value",
    "vintage", "available_at", "published_at", "is_projection", "is_nowcast",
    "source_document_id", "collected_at",
)


def series_id_for_code(bind: Bind, code: str) -> int:
    """The series id for ``code``.

    Raises :class:`ValueError` when the code is not a registered series: an
    unknown series is refused with a message, never answered with an empty
    result that a caller would read as "no data".
    """
    with _read(bind) as conn:
        series_id = conn.execute(
            select(economic_series.c.id).where(economic_series.c.code == code)
        ).scalar()
    if series_id is None:
        raise ValueError(f"This series is not registered: {code}")
    return int(series_id)


def point_in_time_statement(
    dialect_name: str,
    series_id: int,
    cutoff: datetime,
    start: Optional[date] = None,
    end: Optional[date] = None,
    include_projections: bool = False,
) -> Select:
    """The single statement :func:`point_in_time` runs, per dialect.

    Extracted so the PostgreSQL spelling — the one production runs and the one
    ``idx_econ_obs_pit`` was built for — can be compiled and asserted in a test
    suite that has only SQLite to execute against.

    The projection filter sits in the WHERE, i.e. BEFORE the per-period winner
    is chosen, so a period whose only rows at this cutoff are projections
    disappears rather than being answered by a forecast.  Where a projection
    was later restated as a value for a finished period, the restatement is a
    separate row and is picked normally.
    """
    obs = economic_observations
    columns = [obs.c[name] for name in _PIT_COLUMNS]
    conditions = [obs.c.series_id == series_id, obs.c.available_at <= cutoff]
    if not include_projections:
        conditions.append(~obs.c.is_projection)
    if start is not None:
        conditions.append(obs.c.ref_period_start >= start)
    if end is not None:
        conditions.append(obs.c.ref_period_start <= end)
    # The winner within a period: latest availability, then latest vintage.
    within_period = (obs.c.available_at.desc(), obs.c.vintage.desc())

    if dialect_name == "postgresql":
        newest = (
            select(*columns)
            .where(and_(*conditions))
            .order_by(obs.c.ref_period_start.desc(), *within_period)
            .distinct(obs.c.ref_period_start)
            .subquery()
        )
    else:
        ranked = (
            select(
                *columns,
                func.row_number()
                .over(partition_by=obs.c.ref_period_start, order_by=within_period)
                .label("rank"),
            )
            .where(and_(*conditions))
            .subquery()
        )
        newest = (
            select(*[ranked.c[name] for name in _PIT_COLUMNS])
            .where(ranked.c.rank == 1)
            .subquery()
        )
    return select(*[newest.c[name] for name in _PIT_COLUMNS]).order_by(
        newest.c.ref_period_start
    )


def point_in_time(
    bind: Bind,
    code: str,
    as_of: Optional[datetime] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
    include_projections: bool = False,
) -> PointInTimeSeries:
    """The series as it was knowable at ``as_of``.

    For each reference period this returns the newest vintage among the rows
    whose ``available_at`` is at or before the cutoff — so a cutoff before a
    revision returns the number that stood then, not the number that stands
    now.  ``as_of=None`` means now, i.e. the latest known values.

    ``start``/``end`` bound ``ref_period_start`` inclusively.

    **Projections are EXCLUDED unless asked for**, which is the same default
    the Go read layer applies to ``GET /api/v1/series/{code}/observations``
    ("a projection is not an observation").  The two read layers answer the
    same question about the same table, so they must answer it the same way: a
    caller that got IMF WEO figures for 2027..2031 from one and not the other
    would compute a different number depending on which door it came in by.
    Pass ``include_projections=True`` to see forecasts, and expect
    ``is_projection`` on every row to be checked before anything is scored.

    One statement, not a Python filter over every row.  On PostgreSQL that is
    ``DISTINCT ON (ref_period_start)`` with ``ORDER BY ref_period_start DESC,
    available_at DESC, vintage DESC``, which is column-for-column the order of
    ``idx_econ_obs_pit`` (the leading ``series_id`` is fixed by the WHERE), so
    the index answers the whole query.  SQLite — which the tests run on — has
    no ``DISTINCT ON``, so it gets the ``ROW_NUMBER()`` spelling of the same
    partition and the same ordering; still one statement, still no row loop.

    Why availability leads the ordering and vintage only breaks ties:
    :func:`record_observation` assigns ``max + 1`` at insert time and REFUSES a
    row whose ``available_at`` precedes the newest stored vintage, so vintage
    is monotone in ``available_at`` — enforced at the write, not assumed at the
    read, because ``available_at`` is a caller-supplied argument and an
    unchecked assumption here is a look-ahead bug.  The ``vintage DESC`` tie-
    break covers rows that share an instant (one fetch writing a correction in
    the same batch).  The writer compares against the row THIS ordering picks,
    so writer and reader cannot disagree about which value is current.
    """
    if start is not None and end is not None and start > end:
        raise ValueError(
            f"This period range is empty: start {start} is after end {end}"
        )
    cutoff = ensure_utc(as_of) if as_of is not None else utcnow()
    series_id = series_id_for_code(bind, code)

    with _read(bind) as conn:
        statement = point_in_time_statement(
            conn.dialect.name, series_id, cutoff, start=start, end=end,
            include_projections=include_projections,
        )
        rows = conn.execute(statement).all()

    observations = tuple(_to_observation(row._mapping) for row in rows)
    return PointInTimeSeries(
        code=code,
        series_id=series_id,
        as_of=cutoff,
        observations=observations,
        vintages=frozenset(item.vintage for item in observations),
        include_projections=bool(include_projections),
    )


def _to_observation(row: Any) -> EconomicObservation:
    return EconomicObservation(
        ref_period_start=row["ref_period_start"],
        ref_period_end=row["ref_period_end"],
        ref_period_label=row["ref_period_label"],
        value=float(row["value"]),
        vintage=int(row["vintage"]),
        available_at=ensure_utc(row["available_at"]),
        published_at=ensure_utc(row["published_at"]),
        is_projection=bool(row["is_projection"]),
        is_nowcast=bool(row["is_nowcast"]),
        source_document_id=(
            int(row["source_document_id"])
            if row["source_document_id"] is not None
            else None
        ),
        collected_at=ensure_utc(row["collected_at"]),
    )


def vintages_for_period(
    bind: Bind, code: str, ref_period_start: date
) -> list[dict[str, Any]]:
    """Every stored vintage of one period, oldest first.

    The audit view: it is what proves a first print survived a revision, and
    what a reader is shown when they ask "what did this number used to say?".
    """
    series_id = series_id_for_code(bind, code)
    with _read(bind) as conn:
        rows = conn.execute(
            select(
                economic_observations.c.vintage,
                economic_observations.c.value,
                economic_observations.c.available_at,
                economic_observations.c.is_projection,
                economic_observations.c.is_nowcast,
            )
            .where(
                and_(
                    economic_observations.c.series_id == series_id,
                    economic_observations.c.ref_period_start == ref_period_start,
                )
            )
            .order_by(economic_observations.c.vintage)
        ).all()
    return [
        {
            "vintage": int(row.vintage),
            "value": float(row.value),
            "available_at": ensure_utc(row.available_at),
            "is_projection": bool(row.is_projection),
            "is_nowcast": bool(row.is_nowcast),
        }
        for row in rows
    ]
