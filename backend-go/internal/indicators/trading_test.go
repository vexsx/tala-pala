package indicators

import (
	"math"
	"testing"
)

func closeTo(a, b, tol float64) bool { return math.Abs(a-b) <= tol }

func trendBars(n int) (high, low, closes []float64) {
	high = make([]float64, n)
	low = make([]float64, n)
	closes = make([]float64, n)
	for i := 0; i < n; i++ {
		c := 100.0 + float64(i)
		closes[i] = c
		high[i] = c + 1
		low[i] = c - 1
	}
	return
}

func TestIchimokuWarmupAndValues(t *testing.T) {
	high, low, _ := trendBars(60)
	tenkan, kijun, senkouA, senkouB := Ichimoku(high, low)

	if !math.IsNaN(tenkan[7]) {
		t.Fatal("tenkan should be NaN before 9 bars")
	}
	// tenkan at i: midpoint of the last 9 highs/lows; on the ramp
	// high[i]=c+1, low[i-8]=c-8-1 -> mid = c - 4
	i := 30
	c := 100.0 + float64(i)
	if !closeTo(tenkan[i], c-4, 1e-9) {
		t.Fatalf("tenkan[%d] = %f, want %f", i, tenkan[i], c-4)
	}
	if !closeTo(kijun[i], c-12.5, 1e-9) { // 26-bar midpoint on the ramp
		t.Fatalf("kijun[%d] = %f, want %f", i, kijun[i], c-12.5)
	}
	if !closeTo(senkouA[i], (tenkan[i]+kijun[i])/2, 1e-9) {
		t.Fatal("senkouA must be the tenkan/kijun midpoint")
	}
	if math.IsNaN(senkouB[52 - 1]) || !math.IsNaN(senkouB[50]) {
		t.Fatal("senkouB warm-up must be exactly 52 bars")
	}
}

func TestSuperTrendFollowsTrend(t *testing.T) {
	high, low, closes := trendBars(80)
	line, dir := SuperTrend(high, low, closes, 10, 3)
	last := len(closes) - 1
	if dir[last] != 1 {
		t.Fatalf("uptrend must give direction +1, got %d", dir[last])
	}
	if math.IsNaN(line[last]) || line[last] >= closes[last] {
		t.Fatalf("bullish supertrend line must sit below price (line=%f close=%f)",
			line[last], closes[last])
	}

	// reverse the ramp -> direction must flip to -1 and line above price
	for i := range closes {
		c := 200.0 - float64(i)
		closes[i], high[i], low[i] = c, c+1, c-1
	}
	line, dir = SuperTrend(high, low, closes, 10, 3)
	if dir[last] != -1 {
		t.Fatalf("downtrend must give direction -1, got %d", dir[last])
	}
	if line[last] <= closes[last] {
		t.Fatal("bearish supertrend line must sit above price")
	}
}

func TestParabolicSARBounds(t *testing.T) {
	high, low, closes := trendBars(50)
	_ = closes
	sar := ParabolicSAR(high, low, 0.02, 0.02, 0.2)
	last := len(high) - 1
	if math.IsNaN(sar[last]) {
		t.Fatal("SAR must be defined after warm-up")
	}
	// in a clean uptrend the SAR trails below the lows
	if sar[last] >= low[last] {
		t.Fatalf("uptrend SAR must stay below the low (sar=%f low=%f)", sar[last], low[last])
	}
}

func TestPivotsClassicFormulas(t *testing.T) {
	piv := Pivots(110, 90, 100)
	if !closeTo(piv.P, 100, 1e-9) {
		t.Fatalf("P = %f, want 100", piv.P)
	}
	if !closeTo(piv.R1, 110, 1e-9) || !closeTo(piv.S1, 90, 1e-9) {
		t.Fatalf("R1/S1 = %f/%f, want 110/90", piv.R1, piv.S1)
	}
	if !closeTo(piv.R2, 120, 1e-9) || !closeTo(piv.S2, 80, 1e-9) {
		t.Fatalf("R2/S2 = %f/%f, want 120/80", piv.R2, piv.S2)
	}
	if piv.R3 <= piv.R2 || piv.S3 >= piv.S2 {
		t.Fatal("R3 must exceed R2 and S3 sit below S2")
	}
}
