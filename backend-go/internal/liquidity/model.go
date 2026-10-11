package liquidity

// The dollar model: three forecasts of the monthly dollar, scored the way the
// research scored them, and the band the scenario paths are drawn with.
//
// WHAT THE NUMBERS SAID, AND THEREFORE WHAT THIS FILE IS. Over 1394..1405 no
// money-based forecast beat the dollar's own trend. Letting the dollar grow at
// the trailing twelve-month M2 growth beats "no change", but not the trend;
// the level models (liquidity dollar, regressions, error correction) lose to
// the trend outright. So this file builds the three things that result
// licenses and nothing else:
//
//   - the backtest table (random walk, the dollar's own trend, the M2-paced
//     path), so the page can show the evidence rather than assert it;
//   - an error band for the M2-paced path, from the empirical quantiles of
//     its own backtest errors, labelled with the coverage it MEASURABLY had;
//   - the scenario paths themselves (scenarios.go), which are "if the dollar
//     kept pace with M2 growth of X%", never a prediction.
//
// THE GRID. Everything runs on a dense array of Jalali months. A month that is
// not stored is NaN and stays NaN: nothing is interpolated, and a forecast or
// error that would need one is simply absent and not counted.
//
// NO LOOK-AHEAD. A forecast made at the end of month t reads the dollar up to
// t and M2 only up to t-3: CBI publishes one to four and a half months late,
// and the research measured the backtest at a 3-month lag (the ranking is the
// same at 2 and 4). The band and coverage at origin t read only errors whose
// outcome month is at or before t. TestNoLookAhead perturbs every later month
// and asserts nothing at t moves.
//
// The backtest reads TODAY's vintage of each M2 month made usable three months
// after its end -- CBI states no release date, so true first-print
// availability is not recoverable for history. M2 is effectively never
// revised (54 months compared across editions, none beyond rounding), which is
// why this reconstruction is acceptable for M2; base money, which WAS revised,
// is not used by the model.

import (
	"math"
	"sort"
)

// The research's production parameters. Changing one changes what the
// acceptance test reproduces, and the test will say so.
const (
	// publicationLagMonths: an M2 month is usable from the end of the third
	// month after it.
	publicationLagMonths = 3
	// minErrors is the fewest past errors a quantile is taken from.
	minErrors = 12
	// maxHorizon is the longest path and band, in months.
	maxHorizon = 24
	// bandLowerQ / bandUpperQ are the error quantiles the band is drawn at.
	// They are NOT a 90% interval: the band is labelled with its measured
	// coverage, which has been lower.
	bandLowerQ = 0.05
	bandUpperQ = 0.95
	// significance is the level the verdict calls a DM p-value significant at.
	significance = 0.05
)

var (
	// forecastStart is the first origin a forecast is made at: fifteen months
	// after the first full dollar month, so the trend has a history. Errors
	// from here on feed the expanding pool the coverage is measured with.
	forecastStart = NewMonth(1392, 1)
	// evalStart is the first origin that is scored, and the first whose errors
	// form the band.
	evalStart = NewMonth(1394, 1)
	// scoredHorizons are the horizons the backtest table reports.
	scoredHorizons = []int{3, 6, 12, 24}
)

// target selects which monthly dollar figure a forecast is scored against.
type target int

const (
	targetClose target = iota // month-end close (the scenario anchor is a close)
	targetAvg                 // monthly average
)

func (t target) label() string {
	if t == targetAvg {
		return "monthly average"
	}
	return "month-end close"
}

// model names the three forecasts.
type model int

const (
	modelRandomWalk model = iota
	modelTrend
	modelM2Paced
)

func (m model) key() string {
	switch m {
	case modelTrend:
		return "dollar_trend"
	case modelM2Paced:
		return "m2_paced"
	}
	return "random_walk"
}

// panel is the monthly grid the model runs on. Index i is month first+i.
type panel struct {
	first Month
	// avg and close are the log monthly average and log month-end close of the
	// dollar; NaN where the month is not stored or not complete.
	avg, close []float64
	// m2 is log M2 (end-of-month stock); NaN where not stored.
	m2 []float64
	// iFirst and iLast are the first and last complete dollar months; -1 when
	// there is none.
	iFirst, iLast int
}

// newPanel lays the dollar months and M2 onto one grid. Only COMPLETE dollar
// months enter the model: the month still running and a first month the
// history does not cover from its start are partial, and a partial month's
// average is not that month's average.
func newPanel(usd []usdMonth, m2 []moneyPoint) *panel {
	var lo, hi Month
	set := false
	extend := func(m Month) {
		if !set {
			lo, hi, set = m, m, true
			return
		}
		if m < lo {
			lo = m
		}
		if m > hi {
			hi = m
		}
	}
	for _, u := range usd {
		extend(u.Month)
	}
	for _, p := range m2 {
		extend(p.Month)
	}
	p := &panel{first: lo, iFirst: -1, iLast: -1}
	if !set {
		return p
	}
	n := int(hi-lo) + 1
	p.avg, p.close, p.m2 = nanSlice(n), nanSlice(n), nanSlice(n)
	for _, u := range usd {
		if !u.Complete || u.Avg <= 0 || u.Close <= 0 {
			continue
		}
		i := p.idx(u.Month)
		p.avg[i], p.close[i] = math.Log(u.Avg), math.Log(u.Close)
		if p.iFirst < 0 || i < p.iFirst {
			p.iFirst = i
		}
		if i > p.iLast {
			p.iLast = i
		}
	}
	for _, q := range m2 {
		if q.Value > 0 {
			p.m2[p.idx(q.Month)] = math.Log(q.Value)
		}
	}
	return p
}

func nanSlice(n int) []float64 {
	s := make([]float64, n)
	for i := range s {
		s[i] = math.NaN()
	}
	return s
}

func (p *panel) idx(m Month) int { return int(m - p.first) }

func (p *panel) month(i int) Month { return p.first.Add(i) }

func (p *panel) at(s []float64, i int) float64 {
	if i < 0 || i >= len(s) {
		return math.NaN()
	}
	return s[i]
}

func (p *panel) series(t target) []float64 {
	if t == targetAvg {
		return p.avg
	}
	return p.close
}

// usableM2 is the newest M2 month a forecaster at the end of month t could
// have read: at or before t-3, stepping back over months that are not stored
// (the quarter-end-only history before 1397-04). -1 when there is none.
func (p *panel) usableM2(t int) int {
	k := t - publicationLagMonths
	if k >= len(p.m2) {
		k = len(p.m2) - 1
	}
	for k >= 0 && math.IsNaN(p.m2[k]) {
		k--
	}
	return k
}

// m2Growth is the trailing twelve-month M2 growth ending at k, as a mean
// monthly log change: [ln M2(k) - ln M2(k-12)]/12. In the quarterly history a
// twelve-months-earlier month may not be stored, so up to six months further
// back are tried and the divisor is the actual span. NaN when nothing in
// k-18..k-12 is stored.
func (p *panel) m2Growth(k int) float64 {
	if k < 0 || k >= len(p.m2) || math.IsNaN(p.m2[k]) {
		return math.NaN()
	}
	j := k - 12
	for j >= 0 && j > k-18 && math.IsNaN(p.m2[j]) {
		j--
	}
	if j < 0 || math.IsNaN(p.m2[j]) {
		return math.NaN()
	}
	return (p.m2[k] - p.m2[j]) / float64(k-j)
}

// trendSlope is the dollar's own trend at origin t: the mean monthly change of
// the log monthly AVERAGE from the first complete month to t. The average is
// used for either target (as the research did): it is the less noisy of the
// two, and the slope, not the level, is what is borrowed.
func (p *panel) trendSlope(t int) float64 {
	if p.iFirst < 0 || t <= p.iFirst {
		return math.NaN()
	}
	return (p.at(p.avg, t) - p.avg[p.iFirst]) / float64(t-p.iFirst)
}

// forecast is model m's h-month-ahead log forecast of series y from origin t.
func (p *panel) forecast(m model, y []float64, t, h int) float64 {
	yt := p.at(y, t)
	switch m {
	case modelTrend:
		return yt + float64(h)*p.trendSlope(t)
	case modelM2Paced:
		return yt + float64(h)*p.m2Growth(p.usableM2(t))
	}
	return yt
}

// originError is one scored forecast: the log error at origin t.
type originError struct {
	T   int
	Err float64
}

// errors are model m's h-month log errors (actual minus forecast) for origins
// fromT..toT whose outcome month is known: at or before the last complete
// month. Ascending by origin. Origins with no forecast or no outcome are
// absent, never zero.
func (p *panel) errors(m model, y []float64, h, fromT, toT int) []originError {
	out := []originError{}
	if p.iLast < 0 {
		return out
	}
	if fromT < 0 {
		fromT = 0
	}
	for t := fromT; t <= toT && t+h <= p.iLast; t++ {
		f := p.forecast(m, y, t, h)
		a := p.at(y, t+h)
		if math.IsNaN(f) || math.IsNaN(a) {
			continue
		}
		out = append(out, originError{T: t, Err: a - f})
	}
	return out
}

// --- the backtest table -------------------------------------------------------

// scoreRow is one model at one horizon.
type scoreRow struct {
	N    int
	RMSE float64
	// VsRandomWalk and VsTrend are one-sided DM tests that THIS model's
	// squared error is smaller; not OK where not applicable.
	VsRandomWalk dmResult
	VsTrend      dmResult
}

// backtest is the scored comparison over the evaluation window.
type backtest struct {
	Target   target
	From, To Month // first and last scored origin
	Scores   map[model]map[int]scoreRow
	// NOrigins is the number of origins scored per horizon (the random walk's,
	// which every comparison is paired against).
	NOrigins map[int]int
}

// runBacktest scores the three models at the reported horizons, origins from
// evalStart to the month before the last complete one -- the window the
// research reported (1394-01..1405-05 at its cutoff).
func (p *panel) runBacktest(tg target) (backtest, bool) {
	out := backtest{Target: tg, Scores: map[model]map[int]scoreRow{}, NOrigins: map[int]int{}}
	if p.iLast < 0 {
		return out, false
	}
	from, to := p.idx(evalStart), p.iLast-1
	if to < from {
		return out, false
	}
	y := p.series(tg)
	scored := false
	first := math.MaxInt32
	for _, h := range scoredHorizons {
		errs := map[model]map[int]float64{}
		for _, m := range []model{modelRandomWalk, modelTrend, modelM2Paced} {
			errs[m] = map[int]float64{}
			for _, e := range p.errors(m, y, h, from, to) {
				errs[m][e.T] = e.Err
				// On a history that starts later than evalStart, the first
				// scored origin is later too, and the window says so.
				first = min(first, e.T)
			}
		}
		out.NOrigins[h] = len(errs[modelRandomWalk])
		for _, m := range []model{modelRandomWalk, modelTrend, modelM2Paced} {
			row, ok := scoreAgainst(errs[m], errs[modelRandomWalk])
			if !ok {
				continue
			}
			if m != modelRandomWalk {
				row.VsRandomWalk = lossDifferential(errs[m], errs[modelRandomWalk], h)
			}
			if m == modelM2Paced {
				row.VsTrend = lossDifferential(errs[m], errs[modelTrend], h)
			}
			if out.Scores[m] == nil {
				out.Scores[m] = map[int]scoreRow{}
			}
			out.Scores[m][h] = row
			scored = true
		}
	}
	if scored {
		// From is the first origin actually scored; To is the end of the
		// origin window (the month before the last complete one), the way the
		// research stated it -- each horizon scores the origins in it whose
		// outcome is known, which n_origins counts.
		out.From, out.To = p.month(first), p.month(to)
	}
	return out, scored
}

// commonOrigins are the origins both error sets have, ascending -- the order
// the DM autocovariances need, and a fixed summation order for the RMSE.
func commonOrigins(e, bench map[int]float64) []int {
	ts := make([]int, 0, len(e))
	for t := range e {
		if _, ok := bench[t]; ok {
			ts = append(ts, t)
		}
	}
	sort.Ints(ts)
	return ts
}

// scoreAgainst is the RMSE of a model's errors over the origins where both it
// and the benchmark have one -- the pairing every comparison in the table
// shares, so the RMSEs in one column are over the same origins.
func scoreAgainst(e, bench map[int]float64) (scoreRow, bool) {
	ts := commonOrigins(e, bench)
	if len(ts) < minErrors {
		return scoreRow{}, false
	}
	var ss float64
	for _, t := range ts {
		ss += e[t] * e[t]
	}
	return scoreRow{N: len(ts), RMSE: math.Sqrt(ss / float64(len(ts)))}, true
}

// lossDifferential runs the DM test on squared-error differentials over the
// common origins, in origin order.
func lossDifferential(e, bench map[int]float64, h int) dmResult {
	ts := commonOrigins(e, bench)
	if len(ts) < minErrors {
		return dmResult{N: len(ts)}
	}
	d := make([]float64, len(ts))
	for i, t := range ts {
		d[i] = e[t]*e[t] - bench[t]*bench[t]
	}
	return dieboldMariano(d, h)
}

// --- the band -------------------------------------------------------------------

// bandStep is the band at one horizon, in log units around the path.
type bandStep struct {
	H int
	// Lo and Hi are the monotone edges (running min / running max over
	// horizons up to H), so the band never narrows as the horizon grows.
	Lo, Hi float64
	// RawLo and RawHi are this horizon's own quantiles before that.
	RawLo, RawHi float64
	// Median is the median error: positive when the dollar more often ended
	// above the path than below it.
	Median float64
	N      int
}

// band is the M2-paced model's error band: for each horizon, the 5% and 95%
// quantiles of every known error from origins evalStart onward. A horizon
// with fewer than minErrors known errors has no band, and neither does any
// longer one -- a band that stopped and then resumed could not be monotone.
func (p *panel) band(tg target, horizon int) []bandStep {
	out := []bandStep{}
	if p.iLast < 0 {
		return out
	}
	y := p.series(tg)
	from := p.idx(evalStart)
	lo, hi := math.Inf(1), math.Inf(-1)
	for h := 1; h <= horizon; h++ {
		errs := p.errors(modelM2Paced, y, h, from, p.iLast-h)
		if len(errs) < minErrors {
			break
		}
		vals := make([]float64, len(errs))
		for i, e := range errs {
			vals[i] = e.Err
		}
		sort.Float64s(vals)
		rl, rh := sortedQuantile(vals, bandLowerQ), sortedQuantile(vals, bandUpperQ)
		lo, hi = math.Min(lo, rl), math.Max(hi, rh)
		out = append(out, bandStep{H: h, Lo: lo, Hi: hi, RawLo: rl, RawHi: rh,
			Median: sortedQuantile(vals, 0.5), N: len(errs)})
	}
	return out
}

// coverageCell is how often a band built at each origin from that origin's
// own past errors held the outcome.
type coverageCell struct {
	Hits, N int
}

func (c coverageCell) rate() float64 {
	if c.N == 0 {
		return math.NaN()
	}
	return float64(c.Hits) / float64(c.N)
}

// m2Errors are the M2-paced model's errors at every horizon 1..maxHorizon,
// from forecastStart, ascending by origin: the pool every origin's band is
// drawn from.
func (p *panel) m2Errors(tg target) [][]originError {
	all := make([][]originError, maxHorizon+1)
	if p.iLast < 0 {
		return all
	}
	y := p.series(tg)
	for h := 1; h <= maxHorizon; h++ {
		all[h] = p.errors(modelM2Paced, y, h, p.idx(forecastStart), p.iLast-h)
	}
	return all
}

// knownBand is the band a forecaster at the end of month t could have built,
// by horizon: from the errors KNOWN AT t -- origin s with outcome s+h at or
// before t, at least minErrors of them -- at the given quantiles, and (when
// monotone) widened across horizons exactly as the published band is. A
// horizon with too few known errors has no band; under the monotone rule
// neither does any longer one. Nothing about t's own outcome or any later
// month can enter it, which TestNoLookAhead asserts.
func knownBand(all [][]originError, t int, lowerQ, upperQ float64, monotone bool) map[int][2]float64 {
	out := map[int][2]float64{}
	lo, hi := math.Inf(1), math.Inf(-1)
	for h := 1; h < len(all); h++ {
		// all[h] ascends by origin, so the known errors are a prefix.
		n := 0
		for n < len(all[h]) && all[h][n].T+h <= t {
			n++
		}
		if n < minErrors {
			if monotone {
				break // a longer horizon could not be widened from this one
			}
			continue
		}
		past := make([]float64, n)
		for i := 0; i < n; i++ {
			past[i] = all[h][i].Err
		}
		sort.Float64s(past)
		rl, rh := sortedQuantile(past, lowerQ), sortedQuantile(past, upperQ)
		if monotone {
			lo, hi = math.Min(lo, rl), math.Max(hi, rh)
		} else {
			lo, hi = rl, rh
		}
		out[h] = [2]float64{lo, hi}
	}
	return out
}

// coverage measures the band the way it is used: at each scored origin t
// (evalStart onward), the band known at t (knownBand) is checked against t's
// own outcome at every horizon whose outcome is known. A hit is an outcome
// inside it.
//
// The research measured the non-monotone version (interval_calibration.csv);
// the acceptance test reproduces that, and the API publishes the monotone one
// because that is the band it draws.
func (p *panel) coverage(tg target, lowerQ, upperQ float64, monotone bool) map[int]coverageCell {
	out := map[int]coverageCell{}
	if p.iLast < 0 {
		return out
	}
	all := p.m2Errors(tg)
	own := make([]map[int]float64, len(all))
	for h := 1; h < len(all); h++ {
		own[h] = map[int]float64{}
		for _, e := range all[h] {
			own[h][e.T] = e.Err
		}
	}
	for t := p.idx(evalStart); t <= p.iLast-1; t++ {
		band := knownBand(all, t, lowerQ, upperQ, monotone)
		for h := 1; h < len(all) && t+h <= p.iLast; h++ {
			b, okB := band[h]
			e, okE := own[h][t]
			if !okB || !okE {
				continue
			}
			c := out[h]
			c.N++
			if e >= b[0] && e <= b[1] {
				c.Hits++
			}
			out[h] = c
		}
	}
	return out
}

// pooledCoverage is the hit rate over a range of horizons, every scored
// (origin, horizon) pair counted once.
func pooledCoverage(cells map[int]coverageCell, fromH, toH int) coverageCell {
	var c coverageCell
	for h := fromH; h <= toH; h++ {
		c.Hits += cells[h].Hits
		c.N += cells[h].N
	}
	return c
}
