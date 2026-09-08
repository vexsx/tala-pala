package relvalue

// Daily series: how a stored tick history becomes one close per UTC day, how
// that series is carried into a numeraire, and the four statistics this package
// reports about it.
//
// Everything below the SQL is a pure function over a slice. That is deliberate:
// the numeraire rule, the drop rule and the four statistics are where this
// endpoint can lie, and none of them should need a database to be tested.

import (
	"context"
	"math"
	"sort"
	"time"
)

// dailyPoint is one UTC day's close.
type dailyPoint struct {
	Day   time.Time
	Close float64
}

// dailySeries is ascending by Day with at most one point per day.
type dailySeries []dailyPoint

// dailyCloseSelect is THE daily series, and it is deliberately the same shape
// as candleBuckets in internal/prices/candles.go at the 1d interval:
//
//   - quality = 'ok' only
//   - buckets floored in UTC epoch space, never date_trunc in a session
//     timezone (Tehran flooring would move roughly a seventh of the closes onto
//     the wrong day)
//   - close = the last observation of the bucket by (observed_at DESC, id DESC),
//     the same tiebreak _load_candles uses in
//     prediction-python/app/jobs/trend_alignment.py
//
// A chart drawn from /market/candles and a return computed here must not be
// able to disagree about what a day's close was, and the only way to guarantee
// that is to derive both from the same expression.
//
// Every requested symbol is aggregated in ONE statement. A per-symbol query
// would be eleven round trips for the performance table, and -- worse -- would
// invite the numeraire to be read over a different window than the assets it
// converts.
//
// There is no lower bound. It is not an oversight: the numeraire's
// carry-forward rule needs observations from BEFORE the window (the last quote
// at or before the window's first day may be older than the window), and the
// percentile basis needs the entire paired history rather than the window. The
// window is applied in Go, after the series are built, where it can be applied
// to each purpose separately.
const dailyCloseSelect = `
	SELECT symbol, bucket, close FROM (
	    SELECT symbol,
	           to_timestamp((floor(extract(epoch from observed_at) / 86400) * 86400)::float8) AS bucket,
	           (array_agg(value ORDER BY observed_at DESC, id DESC))[1]::float8 AS close
	    FROM prices
	    WHERE quality = 'ok'
	      AND symbol = ANY($1)
	      AND observed_at < $2
	    GROUP BY symbol, bucket
	) d
	ORDER BY symbol ASC, bucket ASC`

// loadDailySeries reads one close per UTC day for each requested symbol, up to
// (and excluding) `asOf`. A symbol with no stored observation is absent from
// the map rather than present and empty, so "this deployment has never
// collected it" is distinguishable from "it has no observation in your window".
func (h *Handler) loadDailySeries(ctx context.Context, symbols []string, asOf time.Time) (map[string]dailySeries, error) {
	rows, err := h.Pool.Query(ctx, dailyCloseSelect, symbols, asOf.UTC())
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]dailySeries{}
	for rows.Next() {
		var symbol string
		var p dailyPoint
		if err := rows.Scan(&symbol, &p.Day, &p.Close); err != nil {
			return nil, err
		}
		p.Day = p.Day.UTC()
		out[symbol] = append(out[symbol], p)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// The statement already returns each symbol ascending by bucket. Re-sorting
	// makes the ordering the rest of this file relies on a property of the Go,
	// not a promise from an ORDER BY no unit test can execute -- the same
	// reason sortTrendEventRows exists in internal/prices.
	for symbol := range out {
		s := out[symbol]
		sort.SliceStable(s, func(i, j int) bool { return s[i].Day.Before(s[j].Day) })
		out[symbol] = s
	}
	return out, nil
}

// --- windowing ----------------------------------------------------------------

// slice returns the points inside [from, to], where `from` is floored onto its
// UTC day and a nil `from` means "from the beginning of this series".
//
// The upper bound is INCLUSIVE of the day containing `to`, because `to` is an
// instant and the day it falls in is a real, if unfinished, bucket. That day's
// close is the latest observation so far, not a settled daily close, and the
// responses say so once at the top level rather than on every item.
func (s dailySeries) slice(from *time.Time, to time.Time) dailySeries {
	out := make(dailySeries, 0, len(s))
	var lo time.Time
	if from != nil {
		lo = floorDay(*from)
	}
	for _, p := range s {
		if from != nil && p.Day.Before(lo) {
			continue
		}
		if p.Day.After(to) {
			continue
		}
		out = append(out, p)
	}
	return out
}

func (s dailySeries) first() (dailyPoint, bool) {
	if len(s) == 0 {
		return dailyPoint{}, false
	}
	return s[0], true
}

func (s dailySeries) last() (dailyPoint, bool) {
	if len(s) == 0 {
		return dailyPoint{}, false
	}
	return s[len(s)-1], true
}

// --- numeraire conversion -----------------------------------------------------

// conversion is a converted series plus an account of everything the conversion
// could not carry. The counts are not diagnostics: they are the reason a
// coverage figure can be trusted, and they are reported.
type conversion struct {
	Points dailySeries
	// NoPriorQuote counts asset days DROPPED because the numeraire had never
	// quoted at or before that day. Those days are not guessable: the only
	// value that could fill them is a LATER numeraire quote, and using it would
	// be look-ahead -- pricing a 2021 gold close with a 2022 exchange rate.
	NoPriorQuote int
	// NonPositiveQuote counts asset days dropped because the numeraire's
	// carried-forward close was zero or negative. A stored zero is a data
	// fault, not a rate; dividing by it yields an infinity that would either
	// truncate the JSON body or, rounded, print as a plausible number.
	NonPositiveQuote int
	// CarriedForward counts days whose numeraire value came from an EARLIER
	// day rather than the same day. Nothing is wrong with those days, but a
	// window where most of them are carried is a window whose conversion rests
	// on a stale rate, and the reader is entitled to know that.
	CarriedForward int
	// MissingSeries is the first conversion series that this deployment does
	// not carry at all, or "" when every step resolved.
	MissingSeries string
}

// applyStep converts `asset` by `num` under `op`.
//
// THE RULE, and it is the one thing in this package most worth getting right:
// for each asset day t the numeraire's value is its LAST CLOSE AT OR BEFORE t.
// Carried FORWARD only. Never backward -- a numeraire quote from after t did
// not exist at t, and using it would price history with information from its
// own future. An asset day with no numeraire quote at or before it is DROPPED,
// because there is nothing honest to put there.
//
// Both series are ascending, so this is a single merge walk. Pure function
// (unit tested).
func applyStep(asset, num dailySeries, op convOp) conversion {
	out := conversion{Points: make(dailySeries, 0, len(asset))}
	j := -1 // index of the last numeraire point at or before the current day
	for _, a := range asset {
		for j+1 < len(num) && !num[j+1].Day.After(a.Day) {
			j++
		}
		if j < 0 {
			out.NoPriorQuote++
			continue
		}
		rate := num[j]
		if rate.Close <= 0 {
			out.NonPositiveQuote++
			continue
		}
		if !rate.Day.Equal(a.Day) {
			out.CarriedForward++
		}
		var v float64
		if op == opDivide {
			v = a.Close / rate.Close
		} else {
			v = a.Close * rate.Close
		}
		if math.IsNaN(v) || math.IsInf(v, 0) {
			// Unreachable with a positive rate and a stored NUMERIC close, and
			// still checked: a non-finite value that reached the payload would
			// truncate the JSON body rather than announce itself.
			out.NonPositiveQuote++
			continue
		}
		out.Points = append(out.Points, dailyPoint{Day: a.Day, Close: v})
	}
	return out
}

// convertSeries walks a whole conversion chain, accumulating what each step
// could not carry. An empty chain returns the asset untouched -- see
// conversionSteps for why that is a real case and not an optimization.
// Pure function (unit tested).
func convertSeries(asset dailySeries, steps []convStep, sources map[string]dailySeries) conversion {
	out := conversion{Points: asset}
	for _, step := range steps {
		num, ok := sources[step.Series]
		if !ok || len(num) == 0 {
			return conversion{
				Points:           dailySeries{},
				NoPriorQuote:     out.NoPriorQuote + len(out.Points),
				NonPositiveQuote: out.NonPositiveQuote,
				CarriedForward:   out.CarriedForward,
				MissingSeries:    step.Series,
			}
		}
		stepped := applyStep(out.Points, num, step.Op)
		out = conversion{
			Points:           stepped.Points,
			NoPriorQuote:     out.NoPriorQuote + stepped.NoPriorQuote,
			NonPositiveQuote: out.NonPositiveQuote + stepped.NonPositiveQuote,
			CarriedForward:   out.CarriedForward + stepped.CarriedForward,
		}
	}
	return out
}

// --- statistics ---------------------------------------------------------------

// totalReturnPct is the simple return from the first point to the last, in
// percent. nil for a series that cannot express one: fewer than two points, or
// a non-positive base. Pure function (unit tested).
func totalReturnPct(s dailySeries) *float64 {
	if len(s) < 2 {
		return nil
	}
	start, end := s[0].Close, s[len(s)-1].Close
	if start <= 0 {
		return nil
	}
	return fp((end/start - 1) * 100)
}

// observationVolatilityPct is the sample standard deviation of the log returns
// between CONSECUTIVE OBSERVATIONS, in percent. Pure function (unit tested).
//
// The name is the fix to a real defect: this was published as
// `daily_volatility_pct`, and it is not a per-day figure. It differences the
// series point by point, and these series have holes -- IR_GOLD_18K quotes 24/7,
// the Tehran gold funds trade a Saturday-to-Wednesday session, XAUUSD follows a
// global futures calendar -- so a "daily" sigma computed from steps that are
// often three days long overstates the per-day number by roughly sqrt(3) while
// claiming a unit it does not have. Nothing is interpolated to close the holes
// (that would manufacture a price path), so the honest correction is the name:
// per OBSERVATION. nonAdjacentSteps counts how many steps span more than a day,
// and the payload publishes that count in a note.
//
// It is NOT annualised, and this is a correction the repository has already
// made once: a card that multiplied a per-step figure by sqrt(252) implied a
// trading-day calendar that none of these symbols keep. There is no single N
// for which sqrt(N) is right for all of them.
//
// The sample (n-1) denominator is used, so a single return -- which carries no
// information about dispersion at all -- yields nil rather than 0.0. A stored
// zero would read as "this asset did not move".
func observationVolatilityPct(s dailySeries) *float64 {
	rets := make([]float64, 0, len(s))
	for i := 1; i < len(s); i++ {
		prev, cur := s[i-1].Close, s[i].Close
		if prev <= 0 || cur <= 0 {
			continue
		}
		rets = append(rets, math.Log(cur/prev))
	}
	if len(rets) < 2 {
		return nil
	}
	mean := 0.0
	for _, r := range rets {
		mean += r
	}
	mean /= float64(len(rets))
	ss := 0.0
	for _, r := range rets {
		d := r - mean
		ss += d * d
	}
	return fp(math.Sqrt(ss/float64(len(rets)-1)) * 100)
}

// nonAdjacentSteps counts consecutive pairs more than one calendar day apart.
//
// It exists because "daily volatility" over a series with holes is really
// per-OBSERVATION volatility, and the two differ by however much the market
// moved over the weekend. Nothing is interpolated to close those holes; the
// count is published in a note instead so the label stays honest.
func nonAdjacentSteps(s dailySeries) int {
	n := 0
	for i := 1; i < len(s); i++ {
		if daysBetween(s[i-1].Day, s[i].Day) > 1 {
			n++
		}
	}
	return n
}

// maxDrawdownPct is the largest peak-to-trough decline of the series, in
// percent, as a NEGATIVE number (0 for a series that never closed below a
// previous peak). Pure function (unit tested).
//
// Deliberately not indicators.DrawdownPct, which answers a different question:
// how far below its trailing high the series sits RIGHT NOW. An asset that fell
// 40% and fully recovered has a max drawdown of -40% and a current drawdown of
// 0%, and the field here is named for the first of those.
func maxDrawdownPct(s dailySeries) *float64 {
	if len(s) < 2 {
		return nil
	}
	peak := math.Inf(-1)
	worst := 0.0
	for _, p := range s {
		if p.Close > peak {
			peak = p.Close
		}
		if peak <= 0 {
			continue
		}
		if d := (p.Close/peak - 1) * 100; d < worst {
			worst = d
		}
	}
	if math.IsInf(peak, -1) {
		return nil
	}
	return fp(worst)
}

// gapPct is the cumulative relative performance of a against b:
//
//	((1 + a_growth) / (1 + b_growth) - 1) * 100
//
// which is exactly the ratio of the two series indexed to 100 at the same base
// date, minus one. Pure function (unit tested).
//
// It is a description of what HAS happened between two dates. It is not a
// spread with a mean to revert to, and nothing in this package projects it
// forward.
func gapPct(aGrowth, bGrowth float64) float64 {
	return ((1+aGrowth)/(1+bGrowth) - 1) * 100
}
