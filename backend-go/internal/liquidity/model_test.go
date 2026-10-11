package liquidity

// The model's rules, one test each, on small synthetic grids where the right
// answer can be worked out by hand -- and the no-look-ahead property on the
// research's real grid, where it matters.

import (
	"math"
	"math/rand"
	"testing"
)

// synthUSD is n complete dollar months from start, average and close both
// f(i).
func synthUSD(start Month, n int, f func(i int) float64) []usdMonth {
	out := make([]usdMonth, n)
	for i := range out {
		v := f(i)
		out[i] = usdMonth{Month: start.Add(i), Avg: v, Close: v, Days: 20, Complete: true}
	}
	return out
}

func synthM2(start Month, n, step int, f func(i int) float64) []moneyPoint {
	out := []moneyPoint{}
	for i := 0; i < n; i += step {
		out = append(out, moneyPoint{Month: start.Add(i), Value: f(i)})
	}
	return out
}

func TestUsableM2HonoursThePublicationLag(t *testing.T) {
	start := NewMonth(1395, 1)
	p := newPanel(synthUSD(start, 40, func(int) float64 { return 100 }),
		synthM2(start, 40, 1, func(i int) float64 { return 1000 + float64(i) }))
	tIdx := 30
	if k := p.usableM2(tIdx); k != tIdx-3 {
		t.Fatalf("usable M2 at t=%d is %d, want t-3=%d even though t-1 and t-2 are stored", tIdx, k, tIdx-3)
	}
	// A hole at t-3 steps back, never forward.
	p.m2[tIdx-3] = math.NaN()
	if k := p.usableM2(tIdx); k != tIdx-4 {
		t.Fatalf("with t-3 missing, usable M2 = %d, want %d", k, tIdx-4)
	}
}

func TestM2GrowthQuarterlyFallback(t *testing.T) {
	start := NewMonth(1390, 3)
	// Quarter-end history: only every third month stored.
	p := newPanel(nil, synthM2(start, 40, 3, func(i int) float64 { return math.Exp(0.02 * float64(i)) }))
	k := p.idx(start.Add(36))
	if g := p.m2Growth(k); math.Abs(g-0.02) > 1e-12 {
		t.Fatalf("growth = %v, want 0.02 a month", g)
	}
	// k-12 missing but k-15 stored: the span is 15 months, not 12.
	p.m2[k-12] = math.NaN()
	if g := p.m2Growth(k); math.Abs(g-0.02) > 1e-12 {
		t.Fatalf("growth over the 15-month fallback = %v, want 0.02", g)
	}
	// Nothing within k-18..k-12: no growth, not a guess.
	for j := k - 18; j <= k-12; j++ {
		p.m2[j] = math.NaN()
	}
	if g := p.m2Growth(k); !math.IsNaN(g) {
		t.Fatalf("growth with no year-earlier month = %v, want NaN", g)
	}
}

func TestForecastsArithmetic(t *testing.T) {
	start := NewMonth(1390, 10)
	// The dollar grows 3% a month (log), M2 2% a month.
	p := newPanel(synthUSD(start, 60, func(i int) float64 { return 1000 * math.Exp(0.03*float64(i)) }),
		synthM2(start.Add(-24), 84, 1, func(i int) float64 { return 50 * math.Exp(0.02*float64(i)) }))
	tIdx := p.idx(start.Add(40))
	y := p.close
	if f := p.forecast(modelRandomWalk, y, tIdx, 6); f != y[tIdx] {
		t.Errorf("random walk = %v, want today's log close %v", f, y[tIdx])
	}
	if f := p.forecast(modelTrend, y, tIdx, 6); math.Abs(f-(y[tIdx]+6*0.03)) > 1e-12 {
		t.Errorf("trend forecast = %v, want log close + 6*0.03", f)
	}
	if f := p.forecast(modelM2Paced, y, tIdx, 6); math.Abs(f-(y[tIdx]+6*0.02)) > 1e-12 {
		t.Errorf("M2-paced forecast = %v, want log close + 6*0.02", f)
	}
	// Errors exist only where the outcome is known.
	errs := p.errors(modelM2Paced, y, 12, 0, p.iLast)
	for _, e := range errs {
		if e.T+12 > p.iLast {
			t.Fatalf("an error at origin %d needs month %d, past the last complete %d", e.T, e.T+12, p.iLast)
		}
		if math.Abs(e.Err-12*0.01) > 1e-9 {
			t.Fatalf("error = %v, want 12*(0.03-0.02)", e.Err)
		}
	}
}

func TestPartialMonthsDoNotEnterTheModel(t *testing.T) {
	start := NewMonth(1400, 1)
	usd := synthUSD(start, 5, func(int) float64 { return 100 })
	usd[4].Complete = false
	p := newPanel(usd, nil)
	if p.iLast != 3 || !math.IsNaN(p.close[4]) {
		t.Fatalf("iLast = %d, close[4] = %v; a running month must not be a model month", p.iLast, p.close[4])
	}
}

func TestBandIsMonotoneAndNeedsTwelveErrors(t *testing.T) {
	p := researchPanel(t)
	band := p.band(targetClose, maxHorizon)
	if len(band) != maxHorizon {
		t.Fatalf("band covers %d horizons", len(band))
	}
	for i := 1; i < len(band); i++ {
		if band[i].Lo > band[i-1].Lo || band[i].Hi < band[i-1].Hi {
			t.Fatalf("band narrows at h=%d: %+v after %+v", band[i].H, band[i], band[i-1])
		}
		if band[i].Lo > band[i].RawLo || band[i].Hi < band[i].RawHi {
			t.Fatalf("monotone edge inside the raw quantile at h=%d", band[i].H)
		}
	}
	// Cut the history so long horizons have fewer than 12 known errors: the
	// band stops there rather than being drawn from a handful.
	short := researchPanel(t)
	cut := short.idx(NewMonth(1395, 6))
	for i := cut + 1; i < len(short.close); i++ {
		short.close[i], short.avg[i] = math.NaN(), math.NaN()
	}
	short.iLast = cut
	b := short.band(targetClose, maxHorizon)
	if len(b) == 0 || len(b) >= maxHorizon {
		t.Fatalf("short history band covers %d horizons", len(b))
	}
	for _, s := range b {
		if s.N < minErrors {
			t.Fatalf("h=%d drawn from %d errors", s.H, s.N)
		}
	}
}

// TestNoLookAhead: everything a forecaster at the end of month t is credited
// with -- the three forecasts at every horizon, and the band built from the
// errors known at t -- must be unchanged when every later dollar month and
// every M2 month after t-3 is replaced by something else.
func TestNoLookAhead(t *testing.T) {
	base := researchPanel(t)
	rng := rand.New(rand.NewSource(7))
	for _, origin := range []Month{NewMonth(1395, 3), NewMonth(1399, 11), NewMonth(1403, 6)} {
		ti := base.idx(origin)
		moved := researchPanel(t)
		for i := ti + 1; i < len(moved.close); i++ {
			if !math.IsNaN(moved.close[i]) {
				moved.close[i] += rng.NormFloat64()
				moved.avg[i] += rng.NormFloat64()
			}
		}
		for i := ti - publicationLagMonths + 1; i < len(moved.m2); i++ {
			if !math.IsNaN(moved.m2[i]) {
				moved.m2[i] += rng.NormFloat64()
			}
		}
		for _, tg := range []target{targetClose, targetAvg} {
			for _, m := range []model{modelRandomWalk, modelTrend, modelM2Paced} {
				for h := 1; h <= maxHorizon; h++ {
					a := base.forecast(m, base.series(tg), ti, h)
					b := moved.forecast(m, moved.series(tg), ti, h)
					if a != b {
						t.Fatalf("%s forecast from %s at h=%d moved from %v to %v when only later data changed",
							m.key(), origin, h, a, b)
					}
				}
			}
			for _, mono := range []bool{true, false} {
				a := knownBand(base.m2Errors(tg), ti, bandLowerQ, bandUpperQ, mono)
				b := knownBand(moved.m2Errors(tg), ti, bandLowerQ, bandUpperQ, mono)
				if len(a) != len(b) {
					t.Fatalf("band known at %s covers %d vs %d horizons", origin, len(a), len(b))
				}
				for h, v := range a {
					if b[h] != v {
						t.Fatalf("band known at %s, h=%d moved from %v to %v", origin, h, v, b[h])
					}
				}
			}
		}
		// And the change was real: something after the origin did move.
		if base.forecast(modelRandomWalk, base.close, ti+1, 1) == moved.forecast(modelRandomWalk, moved.close, ti+1, 1) {
			t.Fatal("the perturbation did not change anything; the test proves nothing")
		}
	}
}

func TestCoverageCountsOnlyScoredOrigins(t *testing.T) {
	p := researchPanel(t)
	cov := p.coverage(targetClose, bandLowerQ, bandUpperQ, true)
	for h, c := range cov {
		if c.Hits > c.N || c.N == 0 {
			t.Fatalf("h=%d: %d hits of %d", h, c.Hits, c.N)
		}
	}
	// Monotone widening can only add hits relative to the research's raw band.
	raw := p.coverage(targetClose, bandLowerQ, bandUpperQ, false)
	for h := 1; h <= maxHorizon; h++ {
		if cov[h].N != raw[h].N || cov[h].Hits < raw[h].Hits {
			t.Fatalf("h=%d monotone %+v vs raw %+v", h, cov[h], raw[h])
		}
	}
	pooled := pooledCoverage(cov, 1, 12)
	sum := 0
	for h := 1; h <= 12; h++ {
		sum += cov[h].N
	}
	if pooled.N != sum {
		t.Fatalf("pooled n %d, want %d", pooled.N, sum)
	}
}

// A deployment whose dollar history starts after 1394-01 must not label its
// backtest as if it ran from 1394-01.
func TestBacktestWindowIsWhatWasScored(t *testing.T) {
	months, _ := bucketUSD(researchUSD(t), tehranToday(researchNow))
	late := []usdMonth{}
	for _, u := range months {
		if u.Month >= NewMonth(1396, 1) {
			late = append(late, u)
		}
	}
	p := newPanel(late, researchMoney(t)[CodeLiquidity].Points)
	bt, ok := p.runBacktest(targetClose)
	if !ok || bt.From.String() != "1396-01" || bt.To.String() != "1405-05" {
		t.Fatalf("window %s..%s (ok %v), want 1396-01..1405-05", bt.From, bt.To, ok)
	}
}

func TestBacktestNeedsData(t *testing.T) {
	if _, ok := newPanel(nil, nil).runBacktest(targetClose); ok {
		t.Fatal("an empty panel has no backtest")
	}
	if b := newPanel(nil, nil).band(targetClose, 12); len(b) != 0 {
		t.Fatal("an empty panel has no band")
	}
}
