package relvalue

// The CPI deflator, and the one thing about it that governs every line below:
// IT IS ANNUAL.
//
// The deflator this deployment carries is WB_CPI_IRN -- the World Bank's
// mirror of Iran's consumer price index, rebased to 2010=100, one observation
// per calendar year. Two consequences follow, and neither is negotiable.
//
// FIRST: the only price-level change this series can measure is between two
// calendar years. There is no defensible way to ask it what prices did between
// 2025-09-09 and 2026-09-09. Interpolating the annual index onto days would
// manufacture a daily inflation path nobody measured, and presenting a return
// deflated by it as "real" would be presenting an invention as a measurement.
// So the real return is computed over the sub-window bounded by two
// CPI-COVERED calendar-year starts, and the payload reports those bounds --
// real_return_from and real_return_to -- precisely so the reader can see it is
// a DIFFERENT window from the nominal one beside it.
//
// SECOND: the series ends where the World Bank's data ends, currently 2025,
// while today is 2026. A window ending today cannot be deflated to today. The
// answer is neither to extrapolate the index (a forecast wearing a
// measurement's clothes) nor to give up and return null (the largest
// CPI-covered sub-window is a real, useful answer). It is to shorten the
// window, say so, and let the reader compare the two spans.
//
// Two honest approximations remain, and both are stated in the note the payload
// carries rather than hidden here.
//
// CPI(y) is an ANNUAL AVERAGE price level, so CPI(y1)/CPI(y0) is an
// average-to-average ratio, while the asset leg is measured between
// observations near 1 January of y0 and of y1: the two spans are offset by
// roughly half a year. Aligning them exactly would require a within-year price
// path this series does not contain.
//
// And the two spans are NOT in general the same length. januaryAnchor takes the
// series' FIRST observation in January, which may be the 2nd in one year and
// the 29th in another, so an asset leg can run a few weeks longer or shorter
// than the whole number of calendar years the deflator covers. The note used to
// assert the legs were "the same length" and nothing checked it; it now
// measures the mismatch and reports it in days, because a claim nothing
// verifies is a claim the reader cannot use.

import (
	"fmt"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
)

// cpiYear is one calendar year of the deflator, as selected by the
// point-in-time rule in internal/economic.
type cpiYear struct {
	Year  int
	Start time.Time // 1 January, UTC -- the reference period's start
	Value float64
	// Vintage is which revision of the year this is. It travels because a real
	// return computed from vintage 1 and one computed from vintage 3 are
	// different numbers and the reader must be able to tell them apart.
	Vintage int
}

// cpiTable is ascending by year.
type cpiTable []cpiYear

// buildCPITable projects point-in-time observations onto the annual table.
//
// A non-positive index value is DROPPED rather than used: it can only be a data
// fault, and it would otherwise appear in a denominator. A year that is absent
// stays absent -- a hole in the World Bank's coverage (the UAE series has 47
// missing years, Russia 32) is never filled in.
func buildCPITable(obs []economic.Observation) cpiTable {
	out := make(cpiTable, 0, len(obs))
	for _, o := range obs {
		if o.Value <= 0 {
			continue
		}
		start := o.RefPeriodStart.UTC()
		out = append(out, cpiYear{
			Year:    start.Year(),
			Start:   time.Date(start.Year(), time.January, 1, 0, 0, 0, 0, time.UTC),
			Value:   o.Value,
			Vintage: o.Vintage,
		})
	}
	return out
}

// coverageTo is the reference-period start of the newest covered year, or nil
// for an empty table. It is what the response publishes as cpi_coverage_to, so
// a reader can see at a glance why a real return stops where it does.
func (c cpiTable) coverageTo() *time.Time {
	if len(c) == 0 {
		return nil
	}
	t := c[len(c)-1].Start
	return &t
}

// januaryAnchor is the series' first observation in JANUARY of year y, if it
// has one.
//
// January is the anchor because the deflator is annual: CPI(y1)/CPI(y0)
// measures the price level from one year to another, so each asset leg must
// stand for the turn of its own year. An observation from June cannot stand for
// the start of a year -- pairing a June-to-January asset leg with a full
// year-to-year deflator would report seven months of return against twelve
// months of inflation and call the difference "real".
//
// Requiring January is a calendar rule, not a tuned tolerance: it is simply the
// month a year starts in. A series with no January observation in a year is
// refused that year as an anchor, and the refusal is stated.
func januaryAnchor(s dailySeries, year int) (dailyPoint, bool) {
	start := time.Date(year, time.January, 1, 0, 0, 0, 0, time.UTC)
	end := time.Date(year, time.February, 1, 0, 0, 0, 0, time.UTC)
	for _, p := range s {
		if p.Day.Before(start) {
			continue
		}
		if !p.Day.Before(end) {
			return dailyPoint{}, false
		}
		return p, true
	}
	return dailyPoint{}, false
}

// realReturnResult is a CPI-deflated return together with the window it was
// actually measured over. Pct is nil whenever the deflator cannot honestly
// produce one, and Note then says why -- never silently.
type realReturnResult struct {
	Pct  *float64
	From *time.Time
	To   *time.Time
	// Note is never empty. On success it names the years, the deflator and the
	// approximation; on failure it says what was missing. Both go to the
	// reader.
	Note string
}

// yearAnchor pairs a covered CPI year with the real observation that stands for
// its start.
type yearAnchor struct {
	Year  cpiYear
	Point dailyPoint
}

// realReturn deflates the nominal return of `s` by the CPI ratio over the
// largest CPI-covered sub-window of [from, to]. Pure function (unit tested):
// no clock, no database.
//
//	real_return = (1 + nominal_return_over_cpi_window) / (CPI(y1)/CPI(y0)) - 1
//
// The sub-window is built from ANCHORS, not from the requested dates: a covered
// CPI year is usable only when the series actually has a January observation
// inside the requested window to stand for it. y0 is the earliest such year and
// y1 the latest, and the two ACTUAL observation dates -- not 1 January, which
// the market may not have quoted -- are what the payload reports as
// real_return_from and real_return_to.
//
// Fewer than two anchors means no annual ratio can be formed, and the answer is
// then null with the reason. That is the correct answer for a 3-month window,
// for a window that ends after CPI coverage does with only one covered year
// inside it, and for a series whose history does not reach two Januaries. None
// of them is rescued by inventing a CPI value for a period nobody measured.
func realReturn(s dailySeries, cpi cpiTable, from, to time.Time) realReturnResult {
	if len(cpi) == 0 {
		return realReturnResult{Note: "no real return: this deployment carries no " +
			cpiSeriesCode + " observation at this cutoff, so there is no deflator."}
	}
	coverage := cpi[len(cpi)-1]
	lo := floorDay(from)

	anchors := make([]yearAnchor, 0, len(cpi))
	for _, y := range cpi {
		p, ok := januaryAnchor(s, y.Year)
		if !ok || p.Day.Before(lo) || p.Day.After(to) || p.Close <= 0 || y.Value <= 0 {
			continue
		}
		anchors = append(anchors, yearAnchor{Year: y, Point: p})
	}
	if len(anchors) < 2 {
		return realReturnResult{Note: fmt.Sprintf(
			"no real return: %s is ANNUAL (World Bank, 2010=100) and its coverage ends "+
				"with %d. Deflating needs two CPI-covered calendar years that the series "+
				"also has a January observation for inside %s..%s, and this request has "+
				"%d. The annual index is NOT interpolated onto days to manufacture one.",
			cpiSeriesCode, coverage.Year, dayString(from), dayString(to), len(anchors))}
	}

	a := anchors[0]
	b := anchors[len(anchors)-1]
	nominal := b.Point.Close/a.Point.Close - 1
	deflator := b.Year.Value / a.Year.Value
	pct := fp(((1+nominal)/deflator - 1) * 100)
	if pct == nil {
		return realReturnResult{Note: "no real return: the deflated result was not a finite number."}
	}

	// How far the asset leg departs from a whole number of calendar years. The
	// anchors are each year's FIRST January observation, so one may sit on the
	// 2nd and the other on the 29th; the deflator spans exactly y1-y0 years
	// whatever they do. The note reports the difference instead of asserting it
	// away.
	spanYears := b.Year.Year - a.Year.Year
	skewDays := daysBetween(a.Point.Day.AddDate(spanYears, 0, 0), b.Point.Day)
	lengthClaim := fmt.Sprintf(
		"Both anchors fall on the same day of January, so the asset leg is exactly "+
			"%d calendar year(s) -- the same length as the deflator's span.", spanYears)
	if skewDays != 0 {
		lengthClaim = fmt.Sprintf(
			"The asset leg is %d calendar year(s) %+d day(s) long and the deflator spans "+
				"exactly %d, so THE TWO LEGS ARE NOT THE SAME LENGTH: each anchor is its "+
				"year's first January observation and the market does not quote every "+
				"1 January. %d day(s) of asset return therefore have no inflation behind "+
				"them.",
			spanYears, skewDays, spanYears, max(skewDays, -skewDays))
	}

	note := fmt.Sprintf(
		"Real return is measured over %s..%s -- the CPI-covered sub-window, NOT the "+
			"nominal window -- because %s is annual and its coverage ends with %d. Each "+
			"leg is the series' first January observation of its year. Deflator "+
			"CPI(%d)/CPI(%d) = %.4f, from vintages %d and %d. %s The CPI legs are annual "+
			"AVERAGES while the asset legs sit at the turn of the year, so they are "+
			"additionally offset by about half a year; the annual index is never "+
			"interpolated onto days.",
		dayString(a.Point.Day), dayString(b.Point.Day), cpiSeriesCode, coverage.Year,
		b.Year.Year, a.Year.Year, deflator, a.Year.Vintage, b.Year.Vintage, lengthClaim)

	fromDay, toDay := a.Point.Day, b.Point.Day
	return realReturnResult{Pct: pct, From: &fromDay, To: &toDay, Note: note}
}
