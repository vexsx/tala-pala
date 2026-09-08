"""The bitemporal store: vintages, and reading the past as it was.

These tests are the specification of migration 0024's central claim — that a
number this system published can still be read after the source restates it.
Everything runs against the in-memory SQLite engine built from the SQLAlchemy
mirror (tests/conftest.py); nothing here touches a production database, and
the PostgreSQL-only spelling of the point-in-time read is asserted by
compiling it rather than by running it.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.dialects import postgresql

from app.db import economic_observations, economic_series, instruments
from app.economic.catalog import spec_for_code
from app.economic.store import (
    STATUS_INSERTED,
    STATUS_RACED,
    STATUS_REVISED,
    STATUS_UNCHANGED,
    ensure_series,
    point_in_time,
    point_in_time_statement,
    record_observation,
    values_equal,
    vintages_for_period,
)

# The real World Bank print for Iran's 2025 CPI, recorded 2026-09-08.
IRAN_CPI_2025 = 4030.3356899712
# A hypothetical later restatement. The World Bank revises this series; the
# specific replacement value is invented for the test and is never stored
# anywhere but the in-memory database this test creates.
IRAN_CPI_2025_REVISED = 4111.2

PERIOD_2025 = (date(2025, 1, 1), date(2025, 12, 31), "2025")
PERIOD_2024 = (date(2024, 1, 1), date(2024, 12, 31), "2024")

# Every one of these is in the past, and stays in the past however long this
# suite outlives today: an available_at in the future would mean "not yet
# knowable", which is exactly what the reads below are testing for.
FIRST_INGEST = datetime(2026, 3, 2, 12, 0, tzinfo=timezone.utc)
BEFORE_REVISION = datetime(2026, 4, 1, tzinfo=timezone.utc)
REVISION_INGEST = datetime(2026, 6, 1, 9, 30, tzinfo=timezone.utc)
AFTER_REVISION = datetime(2026, 8, 1, tzinfo=timezone.utc)


def _spec():
    return spec_for_code("WB_CPI_IRN")


def _write(engine, series_id, period, value, available_at, **kwargs):
    start, end, label = period
    return record_observation(
        engine,
        series_id,
        ref_period_start=start,
        ref_period_end=end,
        ref_period_label=label,
        value=value,
        available_at=available_at,
        **kwargs,
    )


@pytest.fixture()
def series(engine):
    """A registered series, exactly as the ingest job registers one."""
    return ensure_series(engine, _spec())


# --- ensure_series ----------------------------------------------------------


def test_ensure_series_writes_both_rows_from_the_catalog(engine, series):
    spec = _spec()
    with engine.connect() as conn:
        instrument = conn.execute(
            select(instruments).where(instruments.c.code == spec.code)
        ).one()._mapping
        row = conn.execute(
            select(economic_series).where(economic_series.c.code == spec.code)
        ).one()._mapping

    assert instrument["kind"] == "economic_series"
    assert instrument["quality_tier"] == spec.quality_tier
    assert instrument["calendar_class"] == "none"  # a series is never open or closed
    assert instrument["name_fa"] == spec.name_fa
    assert row["id"] == series
    assert row["frequency"] == "A"
    assert row["measure"] == "index"
    assert row["base_period"] == "2010=100"
    assert row["provider_code"] == "worldbank"
    assert row["provider_series_id"] == "FP.CPI.TOTL/IRN"
    # NULL means "not characterised" — it is not the same fact as zero.
    assert row["publication_lag_days"] is None


def test_ensure_series_is_idempotent(engine):
    spec = _spec()
    first = ensure_series(engine, spec)
    second = ensure_series(engine, spec)
    assert first == second
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(economic_series)).scalar() == 1
        assert conn.execute(select(func.count()).select_from(instruments)).scalar() == 1


def test_ensure_series_does_not_re_enable_what_an_operator_disabled(engine, series):
    """The catalog declares what a series is; the operator decides if it runs."""
    spec = _spec()
    with engine.begin() as conn:
        conn.execute(
            instruments.update()
            .where(instruments.c.code == spec.code)
            .values(enabled=False)
        )
        conn.execute(
            economic_series.update()
            .where(economic_series.c.code == spec.code)
            .values(enabled=False)
        )

    ensure_series(engine, spec)

    with engine.connect() as conn:
        assert conn.execute(
            select(instruments.c.enabled).where(instruments.c.code == spec.code)
        ).scalar() is False
        assert conn.execute(
            select(economic_series.c.enabled).where(economic_series.c.code == spec.code)
        ).scalar() is False


def test_ensure_series_refuses_to_redefine_a_market_instrument(engine):
    """A code collision must not rewrite a price symbol into a macro series."""
    spec = _spec()
    with engine.begin() as conn:
        conn.execute(
            instruments.insert().values(
                code=spec.code, kind="market_price", name_en="not a series",
                name_fa="", domain="gold", quote_currency="IRT", unit="gram",
                decimals=0, calendar_class="always_open",
                quality_tier="official_mirror", is_proxy=False, is_derived=False,
                enabled=True, notes="",
            )
        )
    with pytest.raises(ValueError, match="refusing to redefine"):
        ensure_series(engine, spec)


# --- the vintage rule -------------------------------------------------------


def test_first_ingest_inserts_vintage_1(engine, series):
    written = _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    assert written.status == STATUS_INSERTED
    assert written.vintage == 1

    stored = vintages_for_period(engine, _spec().code, PERIOD_2025[0])
    assert [item["vintage"] for item in stored] == [1]
    assert stored[0]["value"] == pytest.approx(IRAN_CPI_2025)
    assert stored[0]["available_at"] == FIRST_INGEST


def test_reingesting_the_same_value_writes_nothing(engine, series):
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    again = _write(engine, series, PERIOD_2025, IRAN_CPI_2025, REVISION_INGEST)

    assert again.status == STATUS_UNCHANGED
    assert again.vintage == 1
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(economic_observations)
        ).scalar() == 1
    # ...and the row that is there still carries the FIRST ingest's stamp: an
    # unchanged value did not become newly available.
    assert vintages_for_period(engine, _spec().code, PERIOD_2025[0])[0][
        "available_at"
    ] == FIRST_INGEST


def test_a_changed_value_inserts_vintage_2_and_leaves_vintage_1_intact(engine, series):
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    revised = _write(
        engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, REVISION_INGEST
    )

    assert revised.status == STATUS_REVISED
    assert revised.vintage == 2
    assert revised.previous_value == pytest.approx(IRAN_CPI_2025)

    stored = vintages_for_period(engine, _spec().code, PERIOD_2025[0])
    assert [item["vintage"] for item in stored] == [1, 2]
    # The first print is still readable, with the availability it always had.
    assert stored[0]["value"] == pytest.approx(IRAN_CPI_2025)
    assert stored[0]["available_at"] == FIRST_INGEST
    assert stored[1]["value"] == pytest.approx(IRAN_CPI_2025_REVISED)
    assert stored[1]["available_at"] == REVISION_INGEST


def test_a_third_print_becomes_vintage_3(engine, series):
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, REVISION_INGEST)
    third = _write(engine, series, PERIOD_2025, 4222.0, AFTER_REVISION)
    assert (third.status, third.vintage) == (STATUS_REVISED, 3)
    assert [item["vintage"] for item in
            vintages_for_period(engine, _spec().code, PERIOD_2025[0])] == [1, 2, 3]


def test_an_unchanged_value_with_a_flipped_flag_writes_nothing(engine, series):
    """The flags are NOT part of "the same value", and this is why.

    Neither source states is_projection; it is computed from the clock at fetch
    time. So the first ingest of a new year re-delivers last year's UNCHANGED
    figure with the flag flipped. Storing that as a vintage would manufacture a
    revision the source never made — an identical number, filed as a
    restatement, flipping the series to is_revised. The source said nothing
    new, so nothing is written.
    """
    _write(engine, series, PERIOD_2025, 50.9, FIRST_INGEST, is_projection=True)
    settled = _write(
        engine, series, PERIOD_2025, 50.9, AFTER_REVISION, is_projection=False
    )

    assert settled.status == STATUS_UNCHANGED
    assert settled.vintage == 1
    stored = vintages_for_period(engine, _spec().code, PERIOD_2025[0])
    assert len(stored) == 1
    # The flag is frozen with the row that carries it.
    assert stored[0]["is_projection"] is True
    assert stored[0]["available_at"] == FIRST_INGEST


def test_a_stored_projection_never_becomes_an_observation_by_the_clock(engine, series):
    """THE calendar-rollover test, with the IMF's real numbers.

    68.9 is what the WEO prints for Iran in 2026 while 2026 is still running:
    a forecast. Re-fetched on 2027-01-02 the SAME 68.9 arrives with the period
    now complete, so the adapter computes is_projection=False. If that were
    stored, GET /series/.../observations — which excludes projections — would
    serve a forecast as a measured 2026 inflation rate, and the IMF publishes
    no reported 2026 figure before the April 2027 WEO. It is not stored.
    """
    imf_period = (date(2026, 1, 1), date(2026, 12, 31), "2026")
    during_2026 = datetime(2026, 4, 20, tzinfo=timezone.utc)
    new_years_ingest = datetime(2027, 1, 2, 6, 0, tzinfo=timezone.utc)

    _write(engine, series, imf_period, 68.9, during_2026, is_projection=True)
    rollover = _write(engine, series, imf_period, 68.9, new_years_ingest)

    assert rollover.status == STATUS_UNCHANGED
    stored = vintages_for_period(engine, _spec().code, imf_period[0])
    assert [item["is_projection"] for item in stored] == [True]

    # And the read layer still refuses to serve it as an observation.
    served = point_in_time(engine, _spec().code, as_of=new_years_ingest)
    assert served.observations == ()
    assert served.is_revised is False
    forecasts = point_in_time(
        engine, _spec().code, as_of=new_years_ingest, include_projections=True
    )
    assert [item.value for item in forecasts.observations] == [pytest.approx(68.9)]
    assert forecasts.observations[0].is_projection is True


def test_a_real_restatement_recomputes_the_flag_honestly(engine, series):
    """A changed value DOES write a row, and that row's flag is computed now.

    The frozen-flag rule is not "the flag can never change" — it is "only the
    source changing its number can change it".
    """
    imf_period = (date(2026, 1, 1), date(2026, 12, 31), "2026")
    during_2026 = datetime(2026, 4, 20, tzinfo=timezone.utc)
    after_2026 = datetime(2027, 4, 15, tzinfo=timezone.utc)

    _write(engine, series, imf_period, 68.9, during_2026, is_projection=True)
    restated = _write(engine, series, imf_period, 61.2, after_2026, is_projection=False)

    assert (restated.status, restated.vintage) == (STATUS_REVISED, 2)
    stored = vintages_for_period(engine, _spec().code, imf_period[0])
    assert [item["is_projection"] for item in stored] == [True, False]
    assert [item["value"] for item in stored] == [pytest.approx(68.9), pytest.approx(61.2)]


def test_value_comparison_is_numeric_not_textual(engine, series):
    """'4030.3356899712' != Decimal('4030.3356899712') as strings; equal here."""
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    from decimal import Decimal

    same = _write(
        engine, series, PERIOD_2025,
        float(Decimal("4030.3356899712")), REVISION_INGEST,
    )
    assert same.status == STATUS_UNCHANGED


def test_tolerance_absorbs_representation_noise_but_not_a_real_revision():
    """The tolerance must sit between the two magnitudes that bracket it."""
    # Two units in the last place: the representation drift it exists to absorb.
    noisy = math.nextafter(math.nextafter(IRAN_CPI_2025, math.inf), math.inf)
    assert noisy != IRAN_CPI_2025
    assert values_equal(IRAN_CPI_2025, noisy)

    # The smallest revision the World Bank can express at its published
    # precision: its last printed digit, 1e-10 on a value near 4030.
    assert not values_equal(IRAN_CPI_2025, IRAN_CPI_2025 + 1e-10)
    # ...and the smallest the IMF can, at one decimal place.
    assert not values_equal(50.9, 51.0)

    # Zero and near-zero compare through the absolute floor, not relatively.
    assert values_equal(0.0, 0.0)
    assert not values_equal(0.0, -0.9)


def test_a_missing_value_is_never_stored(engine, series):
    with pytest.raises(ValueError, match="never stored"):
        _write(engine, series, PERIOD_2025, None, FIRST_INGEST)


def test_a_period_that_ends_before_it_starts_is_refused(engine, series):
    with pytest.raises(ValueError, match="precedes"):
        record_observation(
            engine, series,
            ref_period_start=date(2025, 12, 31),
            ref_period_end=date(2025, 1, 1),
            value=1.0, available_at=FIRST_INGEST,
        )


# --- the availability invariant ---------------------------------------------


def _raw_insert(engine, series_id, period, value, available_at, vintage, **flags):
    """A row written straight to the table, bypassing record_observation.

    Used to construct states the writer now refuses to create — legacy rows, or
    a competing pass — so the reader and writer can be tested against them.
    """
    start, end, label = period
    with engine.begin() as conn:
        conn.execute(
            economic_observations.insert().values(
                series_id=series_id,
                ref_period_start=start,
                ref_period_end=end,
                ref_period_label=label,
                value=value,
                available_at=available_at,
                collected_at=available_at,
                vintage=vintage,
                is_projection=flags.get("is_projection", False),
                is_nowcast=flags.get("is_nowcast", False),
            )
        )


def test_a_vintage_that_predates_the_print_it_revises_is_refused(engine, series):
    """The look-ahead this table exists to prevent, coming in through the writer.

    available_at is a caller-supplied argument, and the reader picks the winning
    row by it. A vintage 2 stamped EARLIER than vintage 1 would therefore be
    served at a cutoff before the first print existed — the revised number,
    readable before anybody could have known it.
    """
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, REVISION_INGEST)

    with pytest.raises(ValueError) as excinfo:
        _write(engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, FIRST_INGEST)

    message = str(excinfo.value)
    # Both timestamps are named: an operator has to see which clock went where.
    assert FIRST_INGEST.isoformat() in message
    assert REVISION_INGEST.isoformat() in message
    assert "EARLIER" in message

    # Nothing was written, and no cutoff can see the revision.
    stored = vintages_for_period(engine, _spec().code, PERIOD_2025[0])
    assert [item["vintage"] for item in stored] == [1]
    assert point_in_time(engine, _spec().code, as_of=BEFORE_REVISION).observations == ()


def test_the_refusal_is_not_a_silent_clamp(engine, series):
    """A backwards clock is an operational fault; rewriting its timestamp to
    'now' would store the row anyway and hide the fault that produced it."""
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, REVISION_INGEST)
    with pytest.raises(ValueError):
        _write(engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, FIRST_INGEST)
    with engine.connect() as conn:
        assert conn.execute(
            select(func.count()).select_from(economic_observations)
        ).scalar() == 1


def test_an_unchanged_value_with_an_older_stamp_is_still_a_no_op(engine, series):
    """Nothing written, nothing to refuse: a no-op cannot introduce look-ahead,
    so a lagging replica re-reporting the same number is not an error."""
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, REVISION_INGEST)
    again = _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    assert again.status == STATUS_UNCHANGED
    assert vintages_for_period(engine, _spec().code, PERIOD_2025[0])[0][
        "available_at"
    ] == REVISION_INGEST


def test_the_writer_compares_against_the_row_the_reader_serves(engine, series):
    """Writer and reader must agree on which vintage is current.

    This state — vintage 2 stamped BEFORE vintage 1 — is one the writer now
    refuses to create, but rows written before that check existed can still be
    in the table. The reader serves vintage 1 (latest available_at), so the
    writer must compare against vintage 1 too. Ordering by vintage alone would
    compare 4030.34 against vintage 2's number and write a phantom "revision"
    back to the value already being served.
    """
    _raw_insert(engine, series, PERIOD_2025, IRAN_CPI_2025, REVISION_INGEST, 1)
    _raw_insert(engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, FIRST_INGEST, 2)

    assert point_in_time(engine, _spec().code).observations[0].value == pytest.approx(
        IRAN_CPI_2025
    )

    again = _write(engine, series, PERIOD_2025, IRAN_CPI_2025, AFTER_REVISION)

    assert again.status == STATUS_UNCHANGED
    assert again.previous_vintage == 1
    assert len(vintages_for_period(engine, _spec().code, PERIOD_2025[0])) == 2


def test_a_concurrent_writer_does_not_cost_the_series(engine, series):
    """Two overlapping passes (the cron plus a manual POST) compute the same
    vintage. The loser must write nothing and carry on — not abort the
    transaction holding the rest of the series."""
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)

    other_pass_wrote = []

    @event.listens_for(engine, "before_cursor_execute")
    def _other_pass_gets_there_first(conn, cursor, statement, params, context, many):
        # Fire once, on OUR insert: the competing row lands after our SELECT
        # picked the vintage and before our INSERT can use it.
        if other_pass_wrote or "INSERT INTO economic_observations" not in statement:
            return
        other_pass_wrote.append(True)
        other = cursor.connection.cursor()
        other.execute(
            "INSERT INTO economic_observations (series_id, ref_period_start, "
            "ref_period_end, ref_period_label, value, available_at, vintage, "
            "is_nowcast, is_projection, collected_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?)",
            (
                series, "2025-01-01", "2025-12-31", "2025", IRAN_CPI_2025_REVISED,
                "2026-06-01 09:30:00.000000", 2, "2026-06-01 09:30:00.000000",
            ),
        )
        other.close()

    try:
        raced = _write(
            engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, REVISION_INGEST
        )
    finally:
        event.remove(engine, "before_cursor_execute", _other_pass_gets_there_first)

    assert other_pass_wrote == [True]
    assert raced.status == STATUS_RACED
    # One row per (series, period, vintage): the winner's, not two of them.
    stored = vintages_for_period(engine, _spec().code, PERIOD_2025[0])
    assert [item["vintage"] for item in stored] == [1, 2]
    assert stored[1]["value"] == pytest.approx(IRAN_CPI_2025_REVISED)
    # ...and the connection is still usable, which is the whole point: a raised
    # IntegrityError here would have rolled back the other 65 periods.
    third = _write(engine, series, PERIOD_2024, 2834.84629484158, AFTER_REVISION)
    assert third.status == STATUS_INSERTED


# --- point-in-time reads ----------------------------------------------------


def test_point_in_time_before_a_revision_returns_the_original_number(engine, series):
    """THE test. A cutoff before a revision must return the number that stood
    then — not the number that stands now. Everything else in this subsystem
    exists to make this one assertion true."""
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, REVISION_INGEST)

    as_it_was = point_in_time(engine, _spec().code, as_of=BEFORE_REVISION)

    assert len(as_it_was.observations) == 1
    observation = as_it_was.observations[0]
    assert observation.value == pytest.approx(IRAN_CPI_2025)
    assert observation.value != pytest.approx(IRAN_CPI_2025_REVISED)
    assert observation.vintage == 1
    assert as_it_was.vintages == frozenset({1})
    assert as_it_was.is_revised is False


def test_point_in_time_after_the_revision_returns_the_revised_number(engine, series):
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, REVISION_INGEST)

    now = point_in_time(engine, _spec().code, as_of=AFTER_REVISION)

    assert now.observations[0].value == pytest.approx(IRAN_CPI_2025_REVISED)
    assert now.observations[0].vintage == 2
    assert now.vintages == frozenset({2})
    assert now.is_revised is True


def test_point_in_time_defaults_to_the_latest_known_values(engine, series):
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, REVISION_INGEST)
    latest = point_in_time(engine, _spec().code)
    assert latest.observations[0].value == pytest.approx(IRAN_CPI_2025_REVISED)


def test_point_in_time_before_the_first_ingest_knows_nothing(engine, series):
    """We knew nothing before we collected anything, and say so."""
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    empty = point_in_time(
        engine, _spec().code, as_of=datetime(2026, 2, 1, tzinfo=timezone.utc)
    )
    assert empty.observations == ()
    assert empty.vintages == frozenset()


def test_point_in_time_mixes_vintages_across_periods_and_reports_which(engine, series):
    _write(engine, series, PERIOD_2024, 2834.84629484158, FIRST_INGEST)
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST)
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025_REVISED, REVISION_INGEST)

    answer = point_in_time(engine, _spec().code, as_of=AFTER_REVISION)

    assert [item.ref_period_label for item in answer.observations] == ["2024", "2025"]
    assert [item.vintage for item in answer.observations] == [1, 2]
    # The caller is told the answer is not all first prints.
    assert answer.vintages == frozenset({1, 2})
    assert answer.is_revised is True


def test_point_in_time_bounds_periods_inclusively(engine, series):
    for year, value in ((2023, 2140.2), (2024, 2834.8), (2025, IRAN_CPI_2025)):
        _write(
            engine, series,
            (date(year, 1, 1), date(year, 12, 31), str(year)),
            value, FIRST_INGEST,
        )
    window = point_in_time(
        engine, _spec().code, start=date(2024, 1, 1), end=date(2025, 1, 1)
    )
    assert [item.ref_period_label for item in window.observations] == ["2024", "2025"]


def test_point_in_time_refuses_an_unregistered_series(engine):
    with pytest.raises(ValueError, match="not registered"):
        point_in_time(engine, "WB_CPI_NOWHERE")


def test_point_in_time_refuses_an_empty_range(engine, series):
    with pytest.raises(ValueError, match="empty"):
        point_in_time(
            engine, _spec().code, start=date(2025, 1, 1), end=date(2024, 1, 1)
        )


def test_point_in_time_excludes_projections_by_default(engine, series):
    """The Python and Go read layers answer the same question the same way.

    handlers.go documents "A projection is not an observation" and defaults
    include_projections to false. This reader feeds the P1 numeraire/CPI-real
    engine; if it defaulted the other way, a real return would be computed
    against an IMF forecast depending only on which door the caller came in by.
    """
    _write(engine, series, PERIOD_2024, 2834.84629484158, FIRST_INGEST)
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST,
           is_projection=True)

    served = point_in_time(engine, _spec().code)

    assert [item.ref_period_label for item in served.observations] == ["2024"]
    assert served.include_projections is False
    assert all(item.is_projection is False for item in served.observations)


def test_point_in_time_returns_projections_when_asked(engine, series):
    _write(engine, series, PERIOD_2024, 2834.84629484158, FIRST_INGEST)
    _write(engine, series, PERIOD_2025, IRAN_CPI_2025, FIRST_INGEST,
           is_projection=True)

    served = point_in_time(engine, _spec().code, include_projections=True)

    assert [item.ref_period_label for item in served.observations] == ["2024", "2025"]
    assert served.include_projections is True
    assert [item.is_projection for item in served.observations] == [False, True]


def test_a_period_known_only_as_a_projection_is_absent_not_zero(engine, series):
    """Excluding the projection drops the PERIOD, rather than answering it with
    an older vintage of a different kind or with nothing where a value existed."""
    _write(engine, series, PERIOD_2025, 68.9, FIRST_INGEST, is_projection=True)
    assert point_in_time(engine, _spec().code).observations == ()
    assert point_in_time(engine, _spec().code).vintages == frozenset()


def test_a_projection_later_restated_for_a_finished_period_is_served(engine, series):
    """Once the source restates it as a value for a completed year, the newer
    vintage is an observation and the default read serves it — while a cutoff
    before that restatement still sees nothing."""
    _write(engine, series, PERIOD_2025, 68.9, FIRST_INGEST, is_projection=True)
    _write(engine, series, PERIOD_2025, 61.2, REVISION_INGEST, is_projection=False)

    before = point_in_time(engine, _spec().code, as_of=BEFORE_REVISION)
    after = point_in_time(engine, _spec().code, as_of=AFTER_REVISION)

    assert before.observations == ()
    assert [item.value for item in after.observations] == [pytest.approx(61.2)]
    assert after.observations[0].vintage == 2


def _compiled(dialect_name: str, **kwargs) -> str:
    """The statement as that dialect would send it."""
    from sqlalchemy.dialects import sqlite

    dialect = postgresql.dialect() if dialect_name == "postgresql" else sqlite.dialect()
    statement = point_in_time_statement(
        dialect_name, 1, datetime(2026, 10, 1, tzinfo=timezone.utc), **kwargs
    )
    return str(statement.compile(dialect=dialect))


def test_both_dialects_filter_projections_in_the_same_place():
    """The filter must sit in the WHERE, before the per-period winner is chosen:
    ranking first would let a projection win its period and then be dropped,
    leaving the period unanswered even where an observation vintage exists.

    Both spellings are asserted because production runs the PostgreSQL one and
    the tests can only execute the SQLite one; a filter that reached only one of
    them would be a read layer that disagrees with itself.
    """
    # The same predicate, spelled the way each dialect spells a negated boolean.
    predicate = {
        "postgresql": "NOT economic_observations.is_projection",
        "sqlite": "economic_observations.is_projection = 0",
    }
    for dialect, filter_sql in predicate.items():
        excluded = _compiled(dialect)
        included = _compiled(dialect, include_projections=True)
        _, _, after_where = excluded.partition("WHERE")
        # In the row-selection WHERE, alongside the availability cutoff — not
        # in a later filter applied after the winner was picked.
        assert filter_sql in after_where, dialect
        assert "economic_observations.available_at <=" in after_where, dialect
        # Asked for, and the filter is gone entirely — not inverted, not
        # re-applied somewhere the ranking cannot see.
        assert filter_sql not in included, dialect


def test_postgres_reads_with_distinct_on_in_the_index_order():
    """Production runs PostgreSQL; the test database cannot. Compile it.

    The ORDER BY must be column-for-column ``idx_econ_obs_pit``
    (series_id, ref_period_start DESC, available_at DESC, vintage DESC) with
    the leading series_id fixed by the WHERE, or the index stops answering
    this query and the read degrades into a sort of the whole series.
    """
    sql = str(
        point_in_time_statement(
            "postgresql", 1, datetime(2026, 10, 1, tzinfo=timezone.utc)
        ).compile(dialect=postgresql.dialect())
    )
    assert "DISTINCT ON (economic_observations.ref_period_start)" in sql
    assert (
        "ORDER BY economic_observations.ref_period_start DESC, "
        "economic_observations.available_at DESC, "
        "economic_observations.vintage DESC" in sql
    )
    assert "available_at <=" in sql
    # published_at is never a filter: 0017's rule, restated here so a change
    # to this query has to argue with a test.
    assert "published_at <=" not in sql


def test_sqlite_read_is_one_statement_not_a_row_loop(engine, series):
    """The SQLite spelling is a window function, not a fallback scan."""
    sql = str(
        point_in_time_statement(
            "sqlite", series, datetime(2026, 10, 1, tzinfo=timezone.utc)
        ).compile(engine)
    )
    assert "row_number() OVER" in sql
    assert "PARTITION BY economic_observations.ref_period_start" in sql
    assert "ORDER BY economic_observations.available_at DESC" in sql
