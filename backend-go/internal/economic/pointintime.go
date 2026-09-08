package economic

// The point-in-time read, exported.
//
// Everything in handlers.go serves this rule over HTTP. This file exposes the
// SAME rule -- the same statement, the same reduction -- to other Go packages,
// because the alternative is worse than the small amount of API surface it
// costs.
//
// internal/relvalue deflates a nominal return by Iran's CPI. To do that it must
// read economic_observations, and reading that table means choosing a vintage:
// for each reference period, the newest row that was already available at the
// cutoff, projections excluded. A second copy of that choice inside relvalue
// would be a second answer to "what was the 2024 CPI on this date", and the two
// would diverge the first time either was corrected -- with nothing in either
// package to say which one a published number came from.
//
// So PointInTime runs observationsSelect and applyPointInTime, the exact
// statement and the exact reduction /series/{code}/observations is served by,
// and neither is duplicated anywhere.

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// MinEpochSeconds and MaxEpochSeconds are the window of integers this system
// will read as a unix-seconds timestamp on a query parameter. They are
// exported so a second package parsing the same ?from/?to/?as_of vocabulary
// refuses exactly the same implausible integers -- a Gregorian year, a Jalali
// year, a YYYYMMDD date, JavaScript milliseconds -- instead of accumulating its
// own slightly different bounds. See parseInstant for what each bound catches.
const (
	MinEpochSeconds = minEpochSeconds
	MaxEpochSeconds = maxEpochSeconds
)

// ErrSeriesNotFound is returned by PointInTime when the code is not a
// registered economic series. It is a distinct error because a caller that
// needs a deflator must be able to tell "this deployment does not carry that
// series" (a configuration fact worth reporting to the user) from a database
// failure.
var ErrSeriesNotFound = errors.New("not a known economic series")

// SeriesProvenance is what a consumer outside this package must carry with any
// number it derives from the series. It is deliberately not optional: a real
// return computed from a CPI whose measure, base period and quality tier the
// reader cannot see is not interpretable, and this struct is what the
// downstream payload copies those fields out of -- see relvalue's
// cpiProvenanceBlock, which copies every field below onto
// /markets/performance. Fetching this and publishing only .Code was a real
// defect for a while; a field added here belongs on that block too.
type SeriesProvenance struct {
	Code               string
	NameEN             string
	NameFA             string
	Measure            string
	Unit               string
	Frequency          string
	Calendar           string
	BasePeriod         string
	ProviderCode       string
	ProviderSeriesID   string
	QualityTier        string
	SplicePolicy       string
	SeasonalAdjustment string
	// Notes is the catalog's caveat about this series -- for WB_CPI_IRN, that
	// it is the World Bank's 2010=100 rebase and NOT the Statistical Centre of
	// Iran's own 1400=100 index. It travels onto every payload that carries a
	// number derived from the series.
	Notes string
}

// Observation is one reference period as selected by the point-in-time rule.
// The vintage and availability that selected it travel with the value, so a
// consumer can state which revision it used.
type Observation struct {
	RefPeriodStart time.Time
	RefPeriodEnd   time.Time
	RefPeriodLabel string
	Value          float64
	Vintage        int
	AvailableAt    time.Time
}

// PointInTime returns the whole point-in-time view of a series at `asOf`:
// for each reference period, the newest vintage that was already available at
// the cutoff, with projections excluded. Result order is oldest first, which is
// the order a series is read, differenced and deflated in.
//
// Projections are excluded and NOT optional here. A projection is not an
// observation; deflating a measured return by a projected price level would
// produce a "real return" that is partly a forecast, which is exactly the
// boundary this codebase keeps Go on the other side of.
func PointInTime(ctx context.Context, pool *pgxpool.Pool, code string, asOf time.Time) (SeriesProvenance, []Observation, error) {
	meta, err := scanSeriesRow(pool.QueryRow(ctx, seriesByCodeSelect, code))
	if errors.Is(err, pgx.ErrNoRows) {
		return SeriesProvenance{}, nil, fmt.Errorf("%q is %w", code, ErrSeriesNotFound)
	}
	if err != nil {
		return SeriesProvenance{}, nil, err
	}

	// The same statement /series/{code}/observations runs, with every optional
	// bound left open: a deflator needs the whole covered history, not a page
	// of it. maxObservationLimit is a guard against an accident, not a window:
	// an annual series covering 1960..2025 is 66 rows.
	rows, err := pool.Query(ctx, observationsSelect,
		meta.ID, asOf.UTC(),
		(*time.Time)(nil), (*time.Time)(nil), (*time.Time)(nil),
		false, maxObservationLimit)
	if err != nil {
		return SeriesProvenance{}, nil, err
	}
	defer rows.Close()
	scanned := []observationRow{}
	for rows.Next() {
		var o observationRow
		if err := rows.Scan(&o.RefPeriodStart, &o.RefPeriodEnd, &o.RefPeriodLabel,
			&o.Value, &o.Vintage, &o.AvailableAt, &o.PublishedAt,
			&o.IsProjection, &o.IsNowcast); err != nil {
			return SeriesProvenance{}, nil, err
		}
		scanned = append(scanned, o)
	}
	if err := rows.Err(); err != nil {
		return SeriesProvenance{}, nil, err
	}

	// THE rule. Not a re-implementation of it.
	selected := applyPointInTime(scanned, asOf.UTC(), false)

	// applyPointInTime returns newest first (the page order the HTTP contract
	// pages backwards through). A consumer reading a series chronologically
	// wants the opposite, so the reversal happens once, here.
	out := make([]Observation, 0, len(selected))
	for i := len(selected) - 1; i >= 0; i-- {
		r := selected[i]
		out = append(out, Observation{
			RefPeriodStart: r.RefPeriodStart.UTC(),
			RefPeriodEnd:   r.RefPeriodEnd.UTC(),
			RefPeriodLabel: r.RefPeriodLabel,
			Value:          r.Value,
			Vintage:        r.Vintage,
			AvailableAt:    r.AvailableAt.UTC(),
		})
	}
	return SeriesProvenance{
		Code:               meta.Code,
		NameEN:             meta.NameEN,
		NameFA:             meta.NameFA,
		Measure:            meta.Measure,
		Unit:               meta.Unit,
		Frequency:          meta.Frequency,
		Calendar:           meta.Calendar,
		BasePeriod:         meta.BasePeriod,
		ProviderCode:       meta.ProviderCode,
		ProviderSeriesID:   meta.ProviderSeriesID,
		QualityTier:        meta.QualityTier,
		SplicePolicy:       meta.SplicePolicy,
		SeasonalAdjustment: meta.SeasonalAdjustment,
		Notes:              meta.Notes,
	}, out, nil
}
