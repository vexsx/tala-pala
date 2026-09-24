package relvalue

// The MONTHLY deflator, and why it exists beside the annual one.
//
// cpi.go deflates with WB_CPI_IRN: the World Bank's mirror of Iran's CPI, one
// observation per calendar year, coverage ending 2025. Everything that file
// apologises for follows from that single fact. A real return needs two
// CPI-covered calendar years, so a 3-month or a 1-year window gets no real
// return at all. The deflator's legs are annual AVERAGES while the asset's legs
// sit at the turn of the year, so the two are offset by roughly half a year. A
// window ending today is cut back to the end of coverage, losing over a year.
//
// This deployment now also carries SCI_CPI_URBAN: Iran's own statistical
// centre, 293 MONTHLY observations from 2002-03 to 2026-07. Every one of those
// three limits shrinks by roughly a factor of twelve, and the third nearly
// vanishes:
//
//	resolution   1 month rather than 1 calendar year
//	offset       up to half a MONTH rather than half a year
//	coverage     to 2026-07 rather than to 2025-01
//
// So this is the preferred deflator wherever it reaches, and it reaches every
// asset in `prices` — the earliest of those is IR_COIN_EMAMI in 2010, eight
// years inside SCI's start. WB_CPI_IRN remains the fallback for windows that
// begin before 2002-03, which today means only deep Tehran equity history.
//
// THE TWO ARE NEVER CHAINED. A ratio taken across the 2002 boundary would
// splice two different baskets, two methodologies and two rebasings into one
// number that neither publisher would endorse. A window picks one deflator and
// the payload says which.
//
// WHAT THIS IS STILL NOT. SCI's index is a monthly AVERAGE price level while an
// asset leg is a point observation, so a residual offset remains — it is just
// bounded by the length of a Jalali month instead of a year. And the periods
// are JALALI months, which do not align to Gregorian ones: the Mordad 1405
// print covers 2026-07-23..2026-08-22. The anchors are therefore chosen inside
// each period's own Gregorian span, never by Gregorian month arithmetic.

import (
	"fmt"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
)

// sciCPISeriesCode is the monthly deflator: the urban-household consumer price
// index published by the Statistical Centre of Iran.
//
// URBAN and not rural or national, because it is the series whose housing,
// rent and vehicle components this platform also ingests, and mixing
// populations inside one comparison would make the cost-of-living rows
// incomparable with the deflator applied to the assets beside them.
const sciCPISeriesCode = "SCI_CPI_URBAN"

// cpiMonth is one reference period of the monthly deflator.
//
// Start and End are the period's own Gregorian span as the publisher defines
// it — a Jalali month, so 28, 29, 30 or 31 days, beginning mid-Gregorian-month.
// Label is the publisher's own name for it, carried so the note can say
// "1405-05" rather than a Gregorian approximation of it.
type cpiMonth struct {
	Start   time.Time
	End     time.Time
	Label   string
	Value   float64
	Vintage int
}

// monthlyTable is ascending by reference period.
type monthlyTable []cpiMonth

// buildMonthlyCPITable projects point-in-time observations onto the monthly
// table.
//
// Same rule as the annual table: a non-positive index value is DROPPED rather
// than used, because it can only be a data fault and it would otherwise appear
// in a denominator. A missing month stays missing — it is never interpolated,
// which is the whole reason this file can claim to measure rather than model.
func buildMonthlyCPITable(obs []economic.Observation) monthlyTable {
	out := make(monthlyTable, 0, len(obs))
	for _, o := range obs {
		if o.Value <= 0 {
			continue
		}
		out = append(out, cpiMonth{
			Start:   o.RefPeriodStart.UTC(),
			End:     o.RefPeriodEnd.UTC(),
			Label:   o.RefPeriodLabel,
			Value:   o.Value,
			Vintage: o.Vintage,
		})
	}
	return out
}

// coverageTo is the reference-period START of the newest covered month, or nil
// for an empty table.
//
// The START and not the end, to match what the annual table publishes and what
// the payload's cpi_coverage_to has always meant: the beginning of the last
// period the deflator can speak for.
func (m monthlyTable) coverageTo() *time.Time {
	if len(m) == 0 {
		return nil
	}
	t := m[len(m)-1].Start
	return &t
}

// periodAnchor is the series' FIRST observation inside a reference period's own
// span.
//
// Inside the period rather than on a fixed day of it: these are Jalali months,
// so there is no Gregorian day-of-month that reliably falls in one, and the
// market does not quote every day anyway. Taking the first observation in the
// span is the monthly counterpart of januaryAnchor's rule for the annual table
// — a calendar rule, not a tuned tolerance.
//
// The offset of that anchor from its period's start is returned so the caller
// can report how far the two legs of the comparison actually differ, rather
// than assuming they align.
func periodAnchor(s dailySeries, p cpiMonth) (dailyPoint, int, bool) {
	for _, q := range s {
		if q.Day.Before(p.Start) {
			continue
		}
		if q.Day.After(p.End) {
			return dailyPoint{}, 0, false
		}
		return q, daysBetween(p.Start, q.Day), true
	}
	return dailyPoint{}, 0, false
}

// monthAnchor pairs a covered month with the observation that stands for it.
type monthAnchor struct {
	Month  cpiMonth
	Point  dailyPoint
	Offset int // days from the period's start to the anchoring observation
}

// realReturnMonthly deflates the nominal return of `s` by the monthly CPI ratio
// over the largest covered sub-window of [from, to]. Pure function (unit
// tested): no clock, no database.
//
//	real_return = (1 + nominal_return_over_cpi_window) / (CPI(m1)/CPI(m0)) - 1
//
// The structure deliberately mirrors realReturn in cpi.go — anchors built from
// coverage rather than from the requested dates, fewer than two anchors
// answered with null and a reason, never an interpolation — so that the two
// deflators differ in resolution and in nothing else.
func realReturnMonthly(s dailySeries, m monthlyTable, from, to time.Time) realReturnResult {
	if len(m) == 0 {
		return realReturnResult{Note: "no real return: this deployment carries no " +
			sciCPISeriesCode + " observation at this cutoff, so there is no deflator."}
	}
	coverage := m[len(m)-1]
	lo := floorDay(from)

	anchors := make([]monthAnchor, 0, len(m))
	for _, p := range m {
		q, off, ok := periodAnchor(s, p)
		if !ok || q.Day.Before(lo) || q.Day.After(to) || q.Close <= 0 {
			continue
		}
		anchors = append(anchors, monthAnchor{Month: p, Point: q, Offset: off})
	}
	if len(anchors) < 2 {
		return realReturnResult{Note: fmt.Sprintf(
			"no real return: %s is MONTHLY and its coverage ends with %s (period starting "+
				"%s). Deflating needs two CPI-covered months that the series also has an "+
				"observation inside, within %s..%s, and this request has %d. The index is "+
				"NOT interpolated onto days to manufacture one.",
			sciCPISeriesCode, coverage.Label, dayString(coverage.Start),
			dayString(from), dayString(to), len(anchors))}
	}

	a := anchors[0]
	b := anchors[len(anchors)-1]
	nominal := b.Point.Close/a.Point.Close - 1
	deflator := b.Month.Value / a.Month.Value
	pct := fp(((1+nominal)/deflator - 1) * 100)
	if pct == nil {
		return realReturnResult{Note: "no real return: the deflated result was not a finite number."}
	}

	// How far the asset leg departs from the deflator's span. Each anchor is
	// its period's first observation, so one may sit on the period's first day
	// and the other on its third; the deflator spans period-start to
	// period-start whatever they do. The difference of the two offsets IS the
	// mismatch, exactly, and it is reported rather than asserted away.
	skewDays := b.Offset - a.Offset
	// The deflator's span is measured in DAYS between the two reference
	// periods' starts, not in months. Months cannot be counted from this table:
	// buildMonthlyCPITable drops a faulty month rather than keeping a
	// placeholder for it, so two adjacent ROWS are not necessarily two adjacent
	// MONTHS, and counting rows would silently understate a span that crosses a
	// hole. Days between period starts is exact and needs no calendar
	// arithmetic over a Jalali month of unknown length.
	deflatorDays := daysBetween(a.Month.Start, b.Month.Start)
	lengthClaim := fmt.Sprintf(
		"Both anchors sit %d day(s) into their own reference period, so the asset leg "+
			"is exactly as long as the deflator's span (%s to %s, %d days).",
		a.Offset, a.Month.Label, b.Month.Label, deflatorDays)
	if skewDays != 0 {
		lengthClaim = fmt.Sprintf(
			"The anchors sit %d and %d day(s) into their own reference periods, so the "+
				"asset leg is %+d day(s) against the deflator's span (%s to %s, %d days): "+
				"the market does not quote every day, and a Jalali month does not begin "+
				"on a fixed Gregorian one. %d day(s) of asset return therefore have no "+
				"inflation behind them.",
			a.Offset, b.Offset, skewDays, a.Month.Label, b.Month.Label, deflatorDays,
			max(skewDays, -skewDays))
	}

	note := fmt.Sprintf(
		"Real return is measured over %s..%s using %s, which is MONTHLY (Statistical "+
			"Centre of Iran, urban households) and covers through %s. Deflator "+
			"CPI(%s)/CPI(%s) = %.4f, from vintages %d and %d. %s The CPI legs are monthly "+
			"AVERAGE price levels while the asset legs are point observations, so they "+
			"are additionally offset by up to about half a month; the index is never "+
			"interpolated onto days.",
		dayString(a.Point.Day), dayString(b.Point.Day), sciCPISeriesCode,
		coverage.Label, b.Month.Label, a.Month.Label, deflator,
		a.Month.Vintage, b.Month.Vintage, lengthClaim)

	fromDay, toDay := a.Point.Day, b.Point.Day
	return realReturnResult{Pct: pct, From: &fromDay, To: &toDay, Note: note}
}
