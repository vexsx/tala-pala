package liquidity

// The scenario paths: where the dollar would be if, from today's close, it
// grew as fast as M2 did over a stated span. Three spans, chosen to bracket
// the record rather than to pick one: the latest twelve months, the latest
// three years, and the slowest three Esfand-to-Esfand years since 1390.
//
// They are SCENARIOS. The backtest is why: pacing the dollar to M2 beats "no
// change" but not the dollar's own trend, so no M2 path is a forecast and the
// response never calls one that. The spread between the three is also far
// smaller than the band around any of them -- the band, not the choice of
// span, is the honest measure of what is unknown.

import (
	"fmt"
	"math"
	"time"
)

// scenarioRate is one annual M2 growth rate and where it came from.
type scenarioRate struct {
	Key     string
	LabelEN string
	LabelFA string
	// Growth is the annual rate as a fraction (0.533 for 53.3%).
	Growth   float64
	From, To Month
	Basis    string
}

// slowestSince is the first Esfand a "slowest three years" window may start
// at: the research compared three-year spans from 1390 on.
var slowestSince = NewMonth(1390, 12)

// scenarioRates derives the three rates from M2 as known now. k* is the newest
// stored month -- what CBI has published, not what the backtest's 3-month lag
// would allow: the lag reconstructs history, and today the real availability
// is known. A rate whose months are not stored is absent and said to be.
func scenarioRates(m2 moneySeries) ([]scenarioRate, []string) {
	notes := []string{}
	k, ok := m2.newest()
	if !ok {
		return nil, []string{"No liquidity (M2) month is stored, so no scenario rate exists."}
	}
	all := m2.byMonth()
	out := []scenarioRate{}

	if b, ok := all[k.Month.Add(-12)]; ok && b.Value > 0 {
		out = append(out, scenarioRate{
			Key: "last_12m", LabelEN: "M2 growth over the latest 12 months",
			LabelFA: "رشد نقدینگی در ۱۲ ماه اخیر",
			Growth:  k.Value/b.Value - 1, From: b.Month, To: k.Month,
			Basis: fmt.Sprintf("M2 growth %s→%s", b.Month, k.Month),
		})
	} else {
		notes = append(notes, fmt.Sprintf("%s is not stored, so the latest-12-months scenario is not drawn.",
			k.Month.Add(-12)))
	}

	if b, ok := all[k.Month.Add(-36)]; ok && b.Value > 0 {
		out = append(out, scenarioRate{
			Key: "three_year", LabelEN: "Average M2 growth over the latest 3 years",
			LabelFA: "میانگین رشد سالانه نقدینگی در سه سال اخیر",
			Growth:  math.Pow(k.Value/b.Value, 1.0/3) - 1, From: b.Month, To: k.Month,
			Basis: fmt.Sprintf("M2 average annual growth %s→%s", b.Month, k.Month),
		})
	} else {
		notes = append(notes, fmt.Sprintf("%s is not stored, so the three-year scenario is not drawn.",
			k.Month.Add(-36)))
	}

	var slow *scenarioRate
	for start := slowestSince; start.Add(36) <= k.Month; start = start.Add(12) {
		a, okA := all[start]
		b, okB := all[start.Add(36)]
		if !okA || !okB || a.Value <= 0 {
			continue
		}
		g := math.Pow(b.Value/a.Value, 1.0/3) - 1
		if slow == nil || g < slow.Growth {
			slow = &scenarioRate{
				Key: "slowest_three_year", LabelEN: "Slowest 3-year M2 growth since 1390",
				LabelFA: "کندترین رشد سه‌ساله نقدینگی از ۱۳۹۰",
				Growth:  g, From: start, To: start.Add(36),
				Basis: fmt.Sprintf("M2 average annual growth %s→%s, the slowest Esfand-to-Esfand "+
					"three years since %d", start, start.Add(36), slowestSince.Year()),
			}
		}
	}
	if slow != nil {
		out = append(out, *slow)
	} else {
		notes = append(notes, "No two Esfands three years apart since 1390 are both stored, so the "+
			"slowest-three-years scenario is not drawn.")
	}
	return out, notes
}

// pathPoint is one month of a path on the wire.
type pathPoint struct {
	H      int     `json:"h"`
	Period string  `json:"period"`
	Date   string  `json:"date"`
	Value  float64 `json:"value"`
	// Lo and Hi are the band edges around this path at h; null beyond the
	// horizons the backtest can support.
	Lo *float64 `json:"lo"`
	Hi *float64 `json:"hi"`
}

// jalaliDate is the anchor's civil date as a Jalali triple.
type jalaliDate struct{ Y, M, D int }

// addMonths steps a Jalali date by n months, keeping the day of the month
// (clamped to the target month's length): 1405/07/18 + 1 is 1405/08/18.
func (j jalaliDate) addMonths(n int) (Month, time.Time) {
	m := NewMonth(j.Y, j.M).Add(n)
	d := j.D
	if d > m.Days() {
		d = m.Days()
	}
	t, _ := JalaliToGregorian(m.Year(), m.Of(), d)
	return m, t
}

// buildPath is P(h) = P0 * exp(h*g) for h = 1..horizon, g a monthly log rate,
// with the band applied where it exists. Pure function.
func buildPath(anchor jalaliDate, p0, g float64, horizon int, band []bandStep) []pathPoint {
	out := make([]pathPoint, 0, horizon)
	for h := 1; h <= horizon; h++ {
		m, d := anchor.addMonths(h)
		v := p0 * math.Exp(float64(h)*g)
		pp := pathPoint{H: h, Period: m.String(), Date: d.Format(dateLayout), Value: roundTo(v, 2)}
		if h <= len(band) {
			pp.Lo = fptr(v*math.Exp(band[h-1].Lo), 2)
			pp.Hi = fptr(v*math.Exp(band[h-1].Hi), 2)
		}
		out = append(out, pp)
	}
	return out
}

// monthlyRate is the monthly log rate of an annual growth G: ln(1+G)/12.
func monthlyRate(annual float64) float64 { return math.Log1p(annual) / 12 }
