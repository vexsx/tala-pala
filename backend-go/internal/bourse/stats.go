package bourse

// The arithmetic. Every function here is pure: no clock, no database, and the
// tests drive them with the corrected series of real indices.

import (
	"fmt"
	"math"
	"sort"
	"time"
)

// fp guards a percentage for JSON: six decimals, nil for a non-finite value.
// A NaN reaching encoding/json truncates the body mid-object.
func fp(v float64) *float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	r := math.Round(v*1e6) / 1e6
	return &r
}

// fv guards a LEVEL for JSON without imposing decimals on it: an index level
// or a market value in toman is not a percentage.
func fv(v float64) *float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	return &v
}

// ValueInForce is the last point at or before `day` — the value the index
// stood at when that day began or ended, whichever session it was.
func ValueInForce(s []IndexPoint, day time.Time) (IndexPoint, bool) {
	d := dayFloor(day)
	i := sort.Search(len(s), func(i int) bool { return s[i].Day.After(d) })
	if i == 0 {
		return IndexPoint{}, false
	}
	return s[i-1], true
}

// windowReturn is the percentage change from the value in force at `start` to
// the series' last point.
//
// "In force at the start" and not "first session inside the window": a 1-year
// return then measures exactly one year of change even when the market was
// closed on the anniversary, and a return "since 1 Farvardin" is measured from
// the last close of the previous year — which is how the Tehran market states
// its year-to-date. The second return is the reason when the figure is nil.
func windowReturn(s []IndexPoint, start time.Time) (*float64, string) {
	if len(s) < 2 {
		return nil, fmt.Sprintf("the index has %d stored session(s)", len(s))
	}
	base, ok := ValueInForce(s, start)
	if !ok {
		return nil, fmt.Sprintf(
			"the index's stored history begins on %s, after the window starts (%s); a "+
				"shorter window is not substituted for the one asked for",
			dayString(s[0].Day), dayString(start))
	}
	last := s[len(s)-1]
	if !base.Day.Before(last.Day) {
		return nil, "the window holds no session after its start"
	}
	return fp((last.Value/base.Value - 1) * 100), ""
}

// between is the sub-series with from <= Day <= to (from nil = unbounded).
func between(s []IndexPoint, from *time.Time, to time.Time) []IndexPoint {
	lo := 0
	if from != nil {
		f := dayFloor(*from)
		lo = sort.Search(len(s), func(i int) bool { return !s[i].Day.Before(f) })
	}
	t := dayFloor(to)
	hi := sort.Search(len(s), func(i int) bool { return s[i].Day.After(t) })
	if lo >= hi {
		return nil
	}
	return s[lo:hi]
}

// withBase is `between` plus the value in force at `from`, prepended when the
// window's first stored session is later than `from` — so a window's return
// and its drawdown are measured from where the index stood when it opened.
func withBase(s []IndexPoint, from *time.Time, to time.Time) []IndexPoint {
	w := between(s, from, to)
	if from == nil {
		return w
	}
	base, ok := ValueInForce(s, *from)
	if !ok {
		return w
	}
	if len(w) > 0 && w[0].Day.Equal(base.Day) {
		return w
	}
	out := make([]IndexPoint, 0, len(w)+1)
	out = append(out, base)
	return append(out, w...)
}

// sessionReturns are simple returns between consecutive points, SKIPPING
// every point marked Unchanged: an exact repeat is a closed market, not a
// session in which the market chose to go nowhere.
func sessionReturns(s []IndexPoint) []float64 {
	out := make([]float64, 0, len(s))
	for i := 1; i < len(s); i++ {
		if s[i].Unchanged || s[i-1].Value <= 0 {
			continue
		}
		r := s[i].Value/s[i-1].Value - 1
		if math.IsNaN(r) || math.IsInf(r, 0) {
			continue
		}
		out = append(out, r)
	}
	return out
}

// sessionDispersion is the SAMPLE standard deviation of session returns, in
// percent, NOT ANNUALISED — the same statement internal/equities' screener
// makes and for the same reason: the Tehran calendar has no single N for which
// sqrt(N) would be right, and a closure of fifty sessions makes any assumed N
// wrong by a fifth. The second return is the reason when the figure is nil.
func sessionDispersion(s []IndexPoint) (*float64, int, string) {
	rets := sessionReturns(s)
	if len(rets) < 2 {
		return nil, len(rets), fmt.Sprintf(
			"a sample standard deviation needs two session returns and this window has %d",
			len(rets))
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
	return fp(math.Sqrt(ss/float64(len(rets)-1)) * 100), len(rets), ""
}

// drawdown is the worst peak-to-trough fall inside a series.
type drawdown struct {
	Pct        *float64 // negative, or 0 when the series never fell below a peak
	PeakDate   *time.Time
	TroughDate *time.Time
	// RecoveredDate is the first point back at or above the peak, nil when the
	// series never got back there inside the window.
	RecoveredDate *time.Time
}

// maxDrawdown walks the series once. The worst drawdown is the worst FALL,
// measured from the peak that preceded it — not the lowest close.
func maxDrawdown(s []IndexPoint) drawdown {
	if len(s) < 2 {
		return drawdown{}
	}
	peak := s[0]
	worst := 0.0
	var wPeak, wTrough IndexPoint
	found := false
	for _, p := range s {
		if p.Value > peak.Value {
			peak = p
		}
		if peak.Value <= 0 {
			continue
		}
		if d := p.Value/peak.Value - 1; d < worst {
			worst, wPeak, wTrough, found = d, peak, p, true
		}
	}
	out := drawdown{Pct: fp(worst * 100)}
	if !found {
		return out
	}
	pd, td := wPeak.Day, wTrough.Day
	out.PeakDate, out.TroughDate = &pd, &td
	for _, p := range s {
		if p.Day.After(wTrough.Day) && p.Value >= wPeak.Value {
			rd := p.Day
			out.RecoveredDate = &rd
			break
		}
	}
	return out
}

// highest is the maximum point (the earliest, on a tie).
func highest(s []IndexPoint) (IndexPoint, bool) {
	if len(s) == 0 {
		return IndexPoint{}, false
	}
	best := s[0]
	for _, p := range s[1:] {
		if p.Value > best.Value {
			best = p
		}
	}
	return best, true
}

func lowest(s []IndexPoint) (IndexPoint, bool) {
	if len(s) == 0 {
		return IndexPoint{}, false
	}
	best := s[0]
	for _, p := range s[1:] {
		if p.Value < best.Value {
			best = p
		}
	}
	return best, true
}

// smaGap is last / (mean of the last n SESSIONS) - 1, in percent. Unchanged
// points are not sessions and are skipped when counting back, so a closure
// does not drag the average toward the level the market was shut at.
func smaGap(s []IndexPoint, n int) (*float64, string) {
	if len(s) == 0 {
		return nil, "no stored session"
	}
	sum, count := 0.0, 0
	for i := len(s) - 1; i >= 0 && count < n; i-- {
		if s[i].Unchanged {
			continue
		}
		sum += s[i].Value
		count++
	}
	if count < n {
		return nil, fmt.Sprintf("a %d-session average needs %d sessions and the index has %d",
			n, n, count)
	}
	mean := sum / float64(count)
	return fp((s[len(s)-1].Value/mean - 1) * 100), ""
}

// rebase maps a series onto 100 at its first point.
func rebase(s []IndexPoint) []float64 {
	out := make([]float64, len(s))
	if len(s) == 0 || s[0].Value <= 0 {
		return out
	}
	for i, p := range s {
		out[i] = p.Value / s[0].Value * 100
	}
	return out
}

// Pearson is the correlation of two equal-length samples, nil below three.
func Pearson(a, b []float64) *float64 {
	n := len(a)
	if n != len(b) || n < 3 {
		return nil
	}
	ma, mb := 0.0, 0.0
	for i := range a {
		ma += a[i]
		mb += b[i]
	}
	ma /= float64(n)
	mb /= float64(n)
	var sab, saa, sbb float64
	for i := range a {
		da, db := a[i]-ma, b[i]-mb
		sab += da * db
		saa += da * da
		sbb += db * db
	}
	if saa == 0 || sbb == 0 {
		return nil
	}
	return fp(sab / math.Sqrt(saa*sbb))
}
