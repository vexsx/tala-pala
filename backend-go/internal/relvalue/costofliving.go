package relvalue

// WHAT THINGS COST, which is a different question from what anything returned,
// and is kept in a different array of the response for exactly that reason.
//
// This platform holds four monthly series from the Statistical Centre of Iran:
// the urban headline index and its housing, rent and vehicle components. They
// are consumer PRICE INDICES. Putting them in `items` beside gold and the
// dollar would invite three specific errors, and separating them structurally
// is how this file prevents all three rather than warning about them.
//
//  1. THEY ARE NOT INVESTABLE. There is no instrument that pays the housing
//     index. A reader cannot buy it, and a "return" they could not have earned
//     does not belong in a ranked table of returns. Nothing here is sorted with
//     the assets and nothing here can win "best performer".
//
//  2. HOUSING IS NOT HOUSE PRICES. In the Iranian urban basket the housing
//     division is dominated by rent, actual and imputed. Measured on this
//     deployment's own data, SCI_CPI_HOUSING and SCI_CPI_RENT correlate at
//     0.999996 across all 293 months and never diverge by more than 1.11%:
//     they are the same fact twice. So ONE shelter row is published, not two,
//     and it is labelled as the cost of shelter rather than as the price of a
//     house. This deployment carries NO house price index, and a reader asking
//     "should I have bought a flat" cannot be answered from it -- inventing an
//     answer would be manufacturing a series nobody published.
//
//  3. A PRICE INDEX HAS NO NUMERAIRE. Gold measured in dollars is a price;
//     the vehicle index measured in dollars is nothing at all, because the
//     index is already a ratio to its own base period. These rows are
//     therefore never converted, whatever numeraire the request asked for.
//
// WHAT IS BETTER HERE THAN ANYWHERE ELSE IN THIS PACKAGE: a component and the
// headline come from the SAME publisher with the SAME reference periods, so
// the two legs of a real comparison are identical by construction. The asset
// rows in performance.go must anchor a daily series inside a monthly period and
// report the residual mismatch in days; here there is no mismatch to report.

import (
	"fmt"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
)

const (
	sciShelterSeriesCode  = "SCI_CPI_HOUSING"
	sciVehiclesSeriesCode = "SCI_CPI_VEHICLES"
	// sciRentSeriesCode is deliberately NOT published as its own row. It is
	// named here so the reason travels with the code rather than living only
	// in a commit message: see (2) above.
	sciRentSeriesCode = "SCI_CPI_RENT"
)

// costOfLivingItem is one component of the consumer basket over the window.
type costOfLivingItem struct {
	Code  string `json:"code"`
	Label string `json:"label"`
	// GrowthPct is the index's own change over the covered window: what this
	// part of the basket cost at the end against what it cost at the start.
	GrowthPct *float64 `json:"growth_pct"`
	// RealGrowthPct is that change measured against the HEADLINE index, so a
	// positive number means this component outpaced the basket as a whole and
	// a negative one means it became relatively cheaper. It is exact: both
	// legs are the same publisher's same reference periods.
	RealGrowthPct *float64 `json:"real_growth_pct"`
	From          *string  `json:"from"`
	To            *string  `json:"to"`
	// Periods is how many reference months the comparison actually spans,
	// which can be fewer than the request asked for.
	Periods int      `json:"periods"`
	Notes   []string `json:"notes"`
}

// costOfLivingBlock is the whole section, including the reason it exists.
type costOfLivingBlock struct {
	Deflator string             `json:"deflator"`
	Items    []costOfLivingItem `json:"items"`
	Note     string             `json:"note"`
}

// windowed returns the observations whose reference period STARTS inside
// [from, to], oldest first.
//
// The period's start and not its end: an index value describes the whole period
// it is stamped with, and admitting a period that merely overlaps the window
// would let a month that mostly precedes the window set its first leg.
func windowed(obs []economic.Observation, from, to time.Time) []economic.Observation {
	lo, hi := floorDay(from), floorDay(to)
	out := make([]economic.Observation, 0, len(obs))
	for _, o := range obs {
		d := floorDay(o.RefPeriodStart.UTC())
		if d.Before(lo) || d.After(hi) || o.Value <= 0 {
			continue
		}
		out = append(out, o)
	}
	return out
}

// headlineRatio is the headline index's growth over the same two reference
// periods a component is measured across.
//
// It is looked up BY PERIOD rather than taken from the headline's own first and
// last observation in the window. A component with a shorter history would
// otherwise be deflated by a longer span of inflation and report a real change
// that is mostly the difference in coverage.
func headlineRatio(headline []economic.Observation, start, end time.Time) (float64, bool) {
	var v0, v1 float64
	for _, o := range headline {
		d := floorDay(o.RefPeriodStart.UTC())
		if d.Equal(floorDay(start)) {
			v0 = o.Value
		}
		if d.Equal(floorDay(end)) {
			v1 = o.Value
		}
	}
	if v0 <= 0 || v1 <= 0 {
		return 0, false
	}
	return v1 / v0, true
}

// buildCostOfLiving measures each component over the requested window. Pure
// function (unit tested): no clock, no database.
func buildCostOfLiving(components map[string][]economic.Observation, from, to time.Time) *costOfLivingBlock {
	headline := windowed(components[sciCPISeriesCode], from, to)
	if len(headline) < 2 {
		return nil
	}

	block := &costOfLivingBlock{
		Deflator: sciCPISeriesCode,
		Items:    []costOfLivingItem{},
		Note: fmt.Sprintf(
			"These are CONSUMER PRICE INDICES, not assets: they say what part of the "+
				"basket cost, not what anything returned, and none of them can be bought. "+
				"They are kept out of the ranked table above for that reason. `real_growth_pct` "+
				"measures each component against the headline %s over the SAME reference "+
				"periods, so unlike the asset rows there is no leg mismatch to report. "+
				"Shelter is published once: %s and %s are the same fact twice on this "+
				"data (correlation 0.999996 over 293 months, never more than 1.11%% apart), "+
				"because the Iranian housing division is rent-dominated. This deployment "+
				"carries NO house PRICE index, so it cannot say what a home was worth -- "+
				"only what shelter cost.",
			sciCPISeriesCode, sciShelterSeriesCode, sciRentSeriesCode),
	}

	for _, spec := range []struct{ code, label, note string }{
		{sciShelterSeriesCode, "Shelter (rent-dominated)",
			"The housing division of the urban CPI. It is dominated by rent, actual and " +
				"imputed, so it is the cost of OCCUPYING a home and not the price of buying " +
				"one. Do not read it as a housing-market return."},
		{sciVehiclesSeriesCode, "Vehicles",
			"The vehicle component of the urban CPI: what cars cost a consumer. Closer to " +
				"a transactable price than the shelter row, but still an index of consumer " +
				"prices rather than the resale value of a particular car."},
	} {
		obs := windowed(components[spec.code], from, to)
		item := costOfLivingItem{Code: spec.code, Label: spec.label, Notes: []string{spec.note}}
		if len(obs) < 2 {
			item.Notes = append(item.Notes, fmt.Sprintf(
				"No measurement: this window contains %d reference period(s) of %s and two "+
					"are needed. The index is never interpolated to fill a window.",
				len(obs), spec.code))
			block.Items = append(block.Items, item)
			continue
		}

		first, last := obs[0], obs[len(obs)-1]
		item.Periods = len(obs) - 1
		f, t := dayString(first.RefPeriodStart.UTC()), dayString(last.RefPeriodStart.UTC())
		item.From, item.To = &f, &t
		item.GrowthPct = fp((last.Value/first.Value - 1) * 100)

		ratio, ok := headlineRatio(headline, first.RefPeriodStart, last.RefPeriodStart)
		if !ok {
			item.Notes = append(item.Notes, fmt.Sprintf(
				"No real change: the headline %s does not cover both of this component's "+
					"reference periods, and deflating across a different span would report "+
					"the coverage difference as a price movement.", sciCPISeriesCode))
		} else {
			item.RealGrowthPct = fp(((last.Value / first.Value / ratio) - 1) * 100)
			item.Notes = append(item.Notes, fmt.Sprintf(
				"Measured over %s..%s (%d reference month%s). Against the headline index, "+
					"which rose %.2f%% over the identical periods.",
				first.RefPeriodLabel, last.RefPeriodLabel, item.Periods,
				plural(item.Periods), (ratio-1)*100))
		}
		block.Items = append(block.Items, item)
	}
	return block
}

// plural is the "s" on a count, so a one-month window does not read
// "1 reference months" in text a user is meant to trust with arithmetic.
func plural(n int) string {
	if n == 1 {
		return ""
	}
	return "s"
}
