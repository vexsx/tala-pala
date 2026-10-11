package liquidity

import (
	"math"
	"strings"
	"testing"
)

func esfands(values map[int]float64) moneySeries {
	s := moneySeries{Code: CodeLiquidity}
	for y := 1385; y <= 1410; y++ {
		if v, ok := values[y]; ok {
			s.Points = append(s.Points, moneyPoint{Month: NewMonth(y, 12), Value: v})
		}
	}
	return s
}

func TestScenarioRates(t *testing.T) {
	// Esfand levels growing 30% a year, except 1394..1397 at 10%, with the
	// newest month 1404-12.
	v := map[int]float64{1389: 100}
	for y := 1390; y <= 1404; y++ {
		g := 0.30
		if y >= 1395 && y <= 1397 {
			g = 0.10
		}
		v[y] = v[y-1] * (1 + g)
	}
	rates, notes := scenarioRates(esfands(v))
	if len(rates) != 3 || len(notes) != 0 {
		t.Fatalf("rates %+v notes %v", rates, notes)
	}
	near(t, "last 12 months", rates[0].Growth, 0.30, 1e-12)
	near(t, "three years", rates[1].Growth, 0.30, 1e-12)
	near(t, "slowest three years", rates[2].Growth, 0.10, 1e-12)
	if rates[0].From.String() != "1403-12" || rates[0].To.String() != "1404-12" {
		t.Errorf("last 12m span %s..%s", rates[0].From, rates[0].To)
	}
	if rates[1].From.String() != "1401-12" {
		t.Errorf("three-year span starts %s", rates[1].From)
	}
	if rates[2].From.String() != "1394-12" || rates[2].To.String() != "1397-12" ||
		!strings.Contains(rates[2].Basis, "1394-12→1397-12") {
		t.Errorf("slowest = %+v", rates[2])
	}
}

func TestScenarioRatesSayWhatIsMissing(t *testing.T) {
	// Only the newest Esfand and the one a year before: no three-year span.
	rates, notes := scenarioRates(esfands(map[int]float64{1403: 100, 1404: 150}))
	if len(rates) != 1 || rates[0].Key != "last_12m" || len(notes) != 2 {
		t.Fatalf("rates %+v notes %v", rates, notes)
	}
	if _, notes := scenarioRates(moneySeries{Code: CodeLiquidity}); len(notes) != 1 {
		t.Fatalf("no M2 at all: %v", notes)
	}
}

func TestBuildPath(t *testing.T) {
	band := []bandStep{{H: 1, Lo: -0.1, Hi: 0.1}, {H: 2, Lo: -0.2, Hi: 0.3}}
	g := monthlyRate(0.5)
	path := buildPath(jalaliDate{1405, 6, 31}, 1000, g, 3, band)
	if len(path) != 3 {
		t.Fatalf("%d points", len(path))
	}
	// P(12 months) would be exactly 1.5x; P(h) = P0*(1.5)^(h/12).
	near(t, "P(1)", path[0].Value, roundTo(1000*math.Pow(1.5, 1.0/12), 2), 1e-9)
	near(t, "P(3)", path[2].Value, roundTo(1000*math.Pow(1.5, 3.0/12), 2), 1e-9)
	// 31 Shahrivar + 1 month is the last day of Mehr (30 days), not 1 Aban.
	if path[0].Period != "1405-07" || path[0].Date != "2026-10-22" {
		t.Errorf("h=1 period %s date %s, want 1405-07 on 2026-10-22", path[0].Period, path[0].Date)
	}
	near(t, "band lo h=2", *path[1].Lo, 1000*math.Pow(1.5, 2.0/12)*math.Exp(-0.2), 0.006)
	if path[2].Lo != nil || path[2].Hi != nil {
		t.Error("beyond the band the edges are null, not extrapolated")
	}
	full := buildPath(jalaliDate{1405, 7, 18}, 265100, monthlyRate(0.532687), 12, nil)
	near(t, "the research's S1 at 12 months", full[11].Value, 265100*1.532687, 0.01)
}
