package indicators

import (
	"math"
	"testing"
)

// rampBars builds n bars of a strict uptrend: high=i+1, low=i, close=i+0.5.
func rampBars(n int) (high, low, closes []float64) {
	high = make([]float64, n)
	low = make([]float64, n)
	closes = make([]float64, n)
	for i := 0; i < n; i++ {
		high[i] = float64(i) + 1
		low[i] = float64(i)
		closes[i] = float64(i) + 0.5
	}
	return high, low, closes
}

// constBars builds n identical bars: high=11, low=9, close=10.
func constBars(n int) (high, low, closes []float64) {
	high = make([]float64, n)
	low = make([]float64, n)
	closes = make([]float64, n)
	for i := 0; i < n; i++ {
		high[i], low[i], closes[i] = 11, 9, 10
	}
	return high, low, closes
}

func TestADX_Golden(t *testing.T) {
	// Strict uptrend: every bar +DM = 1, -DM = 0, TR = 1.5
	// (TR = max(high-low=1, |high-prevClose|=1.5, |low-prevClose|=0.5)).
	// Wilder-smoothed sums keep the same ratio, so
	//   +DI = 100*14/21 = 66.67, -DI = 0, DX = 100*|66.67-0|/66.67 = 100
	// for every bar, hence ADX = 100 everywhere it is defined.
	high, low, closes := rampBars(40)
	out := ADX(high, low, closes, 14)
	for i := 0; i < 27; i++ {
		if !math.IsNaN(out[i]) {
			t.Fatalf("adx[%d] should be NaN during warm-up", i)
		}
	}
	almostEq(t, out[27], 100, 1e-9, "adx first value (index 2*14-1)")
	almostEq(t, out[39], 100, 1e-9, "adx steady state")

	// Constant bars: +DM = -DM = 0 -> DX = 0 -> ADX = 0.
	high, low, closes = constBars(40)
	out = ADX(high, low, closes, 14)
	almostEq(t, out[39], 0, 1e-12, "adx flat market")

	// Range check on a mixed series.
	mixed := []float64{5, 7, 6, 9, 8, 12, 10, 14, 11, 15, 13, 17, 12, 18,
		14, 20, 15, 19, 16, 21, 17, 23, 18, 22, 19, 24, 20, 26, 21, 25}
	h := make([]float64, len(mixed))
	l := make([]float64, len(mixed))
	for i, v := range mixed {
		h[i], l[i] = v+1, v-1
	}
	for i, v := range ADX(h, l, mixed, 14) {
		if !math.IsNaN(v) && (v < 0 || v > 100) {
			t.Fatalf("adx[%d]=%v out of [0,100]", i, v)
		}
	}
}

func TestStochastic_Golden(t *testing.T) {
	// Hand-computed with kPeriod=3, dPeriod=2:
	//   i=2: HH=max(12,22,17)=22, LL=min(8,18,13)=8
	//        %K = 100*(15-8)/(22-8) = 50
	//   i=3: HH=max(22,17,32)=32, LL=min(18,13,28)=13
	//        %K = 100*(30-13)/(32-13) = 1700/19 = 89.473684...
	//   %D[3] = (50 + 89.473684)/2 = 69.736842...
	high := []float64{12, 22, 17, 32}
	low := []float64{8, 18, 13, 28}
	closes := []float64{10, 20, 15, 30}
	k, d := Stochastic(high, low, closes, 3, 2)
	if !math.IsNaN(k[1]) || !math.IsNaN(d[2]) {
		t.Fatal("expected NaN during warm-up")
	}
	almostEq(t, k[2], 50, 1e-9, "stoch k[2]")
	almostEq(t, k[3], 1700.0/19.0, 1e-9, "stoch k[3]")
	almostEq(t, d[3], (50+1700.0/19.0)/2, 1e-9, "stoch d[3]")

	// Monotone uptrend closing on its highs: %K = %D = 100.
	_, _, c := rampBars(20)
	k, d = Stochastic(c, c, c, 14, 3)
	almostEq(t, k[19], 100, 1e-9, "stoch k uptrend")
	almostEq(t, d[19], 100, 1e-9, "stoch d uptrend")

	// Flat window: neutral 50.
	_, _, c = constBars(20)
	k, _ = Stochastic(c, c, c, 14, 3)
	almostEq(t, k[19], 50, 1e-12, "stoch k flat")
}

func TestWilliamsR_Golden(t *testing.T) {
	// Same window as the stochastic test, period 3:
	//   i=2: -100*(22-15)/(22-8) = -50
	//   i=3: -100*(32-30)/(32-13) = -200/19 = -10.526315...
	// (Williams %R = %K - 100, matching the stochastic values above.)
	high := []float64{12, 22, 17, 32}
	low := []float64{8, 18, 13, 28}
	closes := []float64{10, 20, 15, 30}
	out := WilliamsR(high, low, closes, 3)
	if !math.IsNaN(out[1]) {
		t.Fatal("expected NaN during warm-up")
	}
	almostEq(t, out[2], -50, 1e-9, "williams r[2]")
	almostEq(t, out[3], -200.0/19.0, 1e-9, "williams r[3]")

	// Close at the very top of the range: %R = 0... but never above.
	_, _, c := rampBars(20)
	out = WilliamsR(c, c, c, 14)
	almostEq(t, out[19], 0, 1e-9, "williams r at high")
	// Flat window: neutral -50.
	_, _, c = constBars(20)
	out = WilliamsR(c, c, c, 14)
	almostEq(t, out[19], -50, 1e-12, "williams r flat")
}

func TestCCI_Golden(t *testing.T) {
	// high=low=close so tp = {1,2,3,4,5}, period 5:
	//   SMA = 3, mean deviation = (2+1+0+1+2)/5 = 1.2
	//   CCI = (5-3) / (0.015*1.2) = 2/0.018 = 111.111111...
	tp := []float64{1, 2, 3, 4, 5}
	out := CCI(tp, tp, tp, 5)
	for i := 0; i < 4; i++ {
		if !math.IsNaN(out[i]) {
			t.Fatalf("cci[%d] should be NaN", i)
		}
	}
	almostEq(t, out[4], 2.0/0.018, 1e-9, "cci[4]")

	// Constant series: zero deviation is defined as 0.
	h, l, c := constBars(25)
	out = CCI(h, l, c, 20)
	almostEq(t, out[24], 0, 1e-12, "cci flat")
}

func TestDonchian_Golden(t *testing.T) {
	high := []float64{1, 5, 3}
	low := []float64{0, 2, 1}
	upper, lower := Donchian(high, low, 2)
	if !math.IsNaN(upper[0]) || !math.IsNaN(lower[0]) {
		t.Fatal("expected NaN during warm-up")
	}
	almostEq(t, upper[1], 5, 1e-12, "donchian upper[1]") // max(1,5)
	almostEq(t, lower[1], 0, 1e-12, "donchian lower[1]") // min(0,2)
	almostEq(t, upper[2], 5, 1e-12, "donchian upper[2]") // max(5,3)
	almostEq(t, lower[2], 1, 1e-12, "donchian lower[2]") // min(2,1)
}

func TestKeltner_Golden(t *testing.T) {
	// Constant bars high=11 low=9 close=10: EMA20 = 10 (defined from index
	// 19), ATR14 = 2 (defined from index 14) so from index 19:
	//   mid = 10, upper = 10 + 2*2 = 14, lower = 10 - 2*2 = 6.
	high, low, closes := constBars(25)
	upper, mid, lower := Keltner(high, low, closes, 20, 14, 2)
	if !math.IsNaN(upper[18]) {
		t.Fatal("keltner upper must be NaN before the EMA20 warm-up")
	}
	almostEq(t, mid[19], 10, 1e-12, "keltner mid")
	almostEq(t, upper[19], 14, 1e-12, "keltner upper")
	almostEq(t, lower[19], 6, 1e-12, "keltner lower")
	almostEq(t, upper[24], 14, 1e-12, "keltner upper end")
}

func TestCorrelationLogReturns_Golden(t *testing.T) {
	// b = 3a has identical log returns (ln(3a_i/3a_{i-1}) = ln(a_i/a_{i-1})),
	// so the Pearson correlation is exactly +1; b = 1000/a negates every log
	// return, so the correlation is exactly -1.
	a := []float64{100, 110, 105, 120, 118, 130, 125, 140}
	prop := make([]float64, len(a))
	inv := make([]float64, len(a))
	for i, v := range a {
		prop[i] = 3 * v
		inv[i] = 1000 / v
	}
	out := CorrelationLogReturns(a, prop, 5)
	for i := 0; i < 5; i++ {
		if !math.IsNaN(out[i]) {
			t.Fatalf("corr[%d] should be NaN during warm-up", i)
		}
	}
	almostEq(t, out[5], 1, 1e-12, "corr proportional")
	almostEq(t, out[7], 1, 1e-12, "corr proportional end")
	out = CorrelationLogReturns(a, inv, 5)
	almostEq(t, out[7], -1, 1e-12, "corr inverse")

	// Zero-variance window (constant series): undefined -> NaN.
	flat := []float64{5, 5, 5, 5, 5, 5, 5, 5}
	out = CorrelationLogReturns(a, flat, 5)
	if !math.IsNaN(out[7]) {
		t.Fatalf("zero variance should be NaN, got %v", out[7])
	}
}

func TestDrawdownPct_Golden(t *testing.T) {
	// High 120, last 90: (120-90)/120*100 = 25.
	almostEq(t, DrawdownPct([]float64{100, 120, 90}, 90), 25, 1e-12, "drawdown 25")
	// At the high: 0.
	almostEq(t, DrawdownPct([]float64{100, 120}, 90), 0, 1e-12, "drawdown at high")
	// Lookback trims the window: the old high 200 is outside lookback 2,
	// window {100,110} has its high at the last value -> 0.
	almostEq(t, DrawdownPct([]float64{200, 100, 110}, 2), 0, 1e-12, "drawdown lookback")
	if !math.IsNaN(DrawdownPct(nil, 90)) {
		t.Fatal("empty series should be NaN")
	}
}
