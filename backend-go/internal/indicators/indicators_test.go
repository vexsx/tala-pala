package indicators

import (
	"math"
	"testing"
)

func almostEq(t *testing.T, got, want, tol float64, name string) {
	t.Helper()
	if math.IsNaN(got) {
		t.Fatalf("%s: got NaN, want %v", name, want)
	}
	if math.Abs(got-want) > tol {
		t.Fatalf("%s: got %v, want %v (tol %v)", name, got, want, tol)
	}
}

func TestSMA_Golden(t *testing.T) {
	vals := []float64{1, 2, 3, 4, 5, 6}
	out := SMA(vals, 3)
	if !math.IsNaN(out[0]) || !math.IsNaN(out[1]) {
		t.Fatal("expected NaN before period-1")
	}
	almostEq(t, out[2], 2, 1e-12, "sma[2]")
	almostEq(t, out[3], 3, 1e-12, "sma[3]")
	almostEq(t, out[5], 5, 1e-12, "sma[5]")
}

func TestEMA_Golden(t *testing.T) {
	// Hand-computed: seed = SMA(1,2,3) = 2; k = 0.5 for period 3.
	// ema[3] = (4-2)*0.5+2 = 3; ema[4] = (5-3)*0.5+3 = 4.
	vals := []float64{1, 2, 3, 4, 5}
	out := EMA(vals, 3)
	almostEq(t, out[2], 2, 1e-12, "ema seed")
	almostEq(t, out[3], 3, 1e-12, "ema[3]")
	almostEq(t, out[4], 4, 1e-12, "ema[4]")
}

// StockCharts RSI reference dataset. First Wilder RSI (unrounded
// intermediates) is 70.4645; the next value is 66.2497.
var rsiCloses = []float64{
	44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08,
	45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64,
	46.21, 46.25, 45.71, 46.45, 45.78, 45.35, 44.03, 44.18, 44.22, 44.57,
	43.42, 42.66, 43.13,
}

func TestRSI_Golden(t *testing.T) {
	out := RSI(rsiCloses, 14)
	for i := 0; i < 14; i++ {
		if !math.IsNaN(out[i]) {
			t.Fatalf("rsi[%d] should be NaN", i)
		}
	}
	almostEq(t, out[14], 70.4645, 0.01, "rsi[14]")
	almostEq(t, out[15], 66.2497, 0.01, "rsi[15]")
	// All values must stay within [0,100].
	for i, v := range out {
		if !math.IsNaN(v) && (v < 0 || v > 100) {
			t.Fatalf("rsi[%d]=%v out of range", i, v)
		}
	}
}

func TestRSI_AllGains(t *testing.T) {
	vals := []float64{1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16}
	out := RSI(vals, 14)
	almostEq(t, out[15], 100, 1e-9, "rsi all gains")
}

func TestMACD_Golden(t *testing.T) {
	// On a constant series every EMA equals the constant, so MACD = 0.
	vals := make([]float64, 60)
	for i := range vals {
		vals[i] = 42
	}
	line, signal, hist := MACD(vals, 12, 26, 9)
	almostEq(t, line[59], 0, 1e-12, "macd line const")
	almostEq(t, signal[59], 0, 1e-12, "macd signal const")
	almostEq(t, hist[59], 0, 1e-12, "macd hist const")

	// On a linear ramp v[i]=i, EMA(fast) converges above EMA(slow) offset:
	// steady-state EMA lag is (period-1)/2, so line -> (26-12)/2 = 7.
	ramp := make([]float64, 400)
	for i := range ramp {
		ramp[i] = float64(i)
	}
	line, signal, hist = MACD(ramp, 12, 26, 9)
	almostEq(t, line[399], 7, 0.01, "macd line ramp")
	almostEq(t, signal[399], 7, 0.01, "macd signal ramp")
	almostEq(t, hist[399], 0, 0.01, "macd hist ramp")
}

func TestBollinger_Golden(t *testing.T) {
	// Window of the last 4 values {2,4,4,4,5,5,7,9}: classic stdev example,
	// population stdev = 2, mean = 5.
	vals := []float64{2, 4, 4, 4, 5, 5, 7, 9}
	upper, mid, lower := Bollinger(vals, 8, 2)
	almostEq(t, mid[7], 5, 1e-12, "boll mid")
	almostEq(t, upper[7], 9, 1e-12, "boll upper")
	almostEq(t, lower[7], 1, 1e-12, "boll lower")
}

func TestATR_Golden(t *testing.T) {
	// Constant range bars: TR is always 2 (high-low dominates), ATR = 2.
	n := 20
	high := make([]float64, n)
	low := make([]float64, n)
	closes := make([]float64, n)
	for i := 0; i < n; i++ {
		high[i], low[i], closes[i] = 11, 9, 10
	}
	out := ATR(high, low, closes, 14)
	almostEq(t, out[14], 2, 1e-12, "atr const")
	almostEq(t, out[19], 2, 1e-12, "atr const end")
}

func TestMomentumROC(t *testing.T) {
	vals := []float64{100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110}
	mom := Momentum(vals, 10)
	almostEq(t, mom[10], 10, 1e-12, "momentum")
	roc := ROC(vals, 10)
	almostEq(t, roc[10], 10, 1e-12, "roc")
}

func TestVolatility(t *testing.T) {
	// Constant series: log returns are all zero, volatility = 0.
	vals := make([]float64, 30)
	for i := range vals {
		vals[i] = 500
	}
	out := Volatility(vals, 20)
	almostEq(t, out[29], 0, 1e-12, "volatility const")

	// Alternating +1%/-1%-ish series must have positive volatility.
	alt := make([]float64, 30)
	alt[0] = 100
	for i := 1; i < 30; i++ {
		if i%2 == 0 {
			alt[i] = alt[i-1] * 1.01
		} else {
			alt[i] = alt[i-1] * 0.99
		}
	}
	out = Volatility(alt, 20)
	if v := out[29]; !(v > 0) {
		t.Fatalf("expected positive volatility, got %v", v)
	}
}

func TestSupportResistance(t *testing.T) {
	vals := []float64{5, 1, 9, 3, 7}
	s, r := SupportResistance(vals, 20)
	almostEq(t, s, 1, 1e-12, "support")
	almostEq(t, r, 9, 1e-12, "resistance")

	// Lookback smaller than the series: only last 2 considered.
	s, r = SupportResistance(vals, 2)
	almostEq(t, s, 3, 1e-12, "support lookback")
	almostEq(t, r, 7, 1e-12, "resistance lookback")
}

func TestLast(t *testing.T) {
	if !math.IsNaN(Last([]float64{NaN, NaN})) {
		t.Fatal("expected NaN")
	}
	almostEq(t, Last([]float64{1, 2, NaN}), 2, 1e-12, "last non-NaN")
}
