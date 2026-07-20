// Package indicators implements technical-analysis math as pure functions
// over []float64 series. Positions with insufficient history are NaN so
// output slices stay aligned with their inputs.
package indicators

import "math"

// NaN is a convenience alias used across the package.
var NaN = math.NaN()

func nanSlice(n int) []float64 {
	out := make([]float64, n)
	for i := range out {
		out[i] = NaN
	}
	return out
}

// Last returns the last non-NaN value of a series (NaN if none).
func Last(s []float64) float64 {
	for i := len(s) - 1; i >= 0; i-- {
		if !math.IsNaN(s[i]) {
			return s[i]
		}
	}
	return NaN
}

// SMA computes the simple moving average with the given period.
func SMA(vals []float64, period int) []float64 {
	out := nanSlice(len(vals))
	if period <= 0 || len(vals) < period {
		return out
	}
	var sum float64
	for i, v := range vals {
		sum += v
		if i >= period {
			sum -= vals[i-period]
		}
		if i >= period-1 {
			out[i] = sum / float64(period)
		}
	}
	return out
}

// EMA computes the exponential moving average (seeded with the SMA of the
// first `period` values, multiplier 2/(period+1)).
func EMA(vals []float64, period int) []float64 {
	out := nanSlice(len(vals))
	if period <= 0 || len(vals) < period {
		return out
	}
	var sum float64
	for i := 0; i < period; i++ {
		sum += vals[i]
	}
	prev := sum / float64(period)
	out[period-1] = prev
	k := 2.0 / (float64(period) + 1.0)
	for i := period; i < len(vals); i++ {
		prev = (vals[i]-prev)*k + prev
		out[i] = prev
	}
	return out
}

// emaOverNaN runs an EMA over a series whose head may be NaN (used for the
// MACD signal line). The seed is the SMA of the first `period` valid values.
func emaOverNaN(vals []float64, period int) []float64 {
	out := nanSlice(len(vals))
	start := -1
	for i, v := range vals {
		if !math.IsNaN(v) {
			start = i
			break
		}
	}
	if start < 0 || len(vals)-start < period {
		return out
	}
	var sum float64
	for i := start; i < start+period; i++ {
		sum += vals[i]
	}
	prev := sum / float64(period)
	out[start+period-1] = prev
	k := 2.0 / (float64(period) + 1.0)
	for i := start + period; i < len(vals); i++ {
		prev = (vals[i]-prev)*k + prev
		out[i] = prev
	}
	return out
}

// RSI computes Wilder's Relative Strength Index.
func RSI(vals []float64, period int) []float64 {
	out := nanSlice(len(vals))
	if period <= 0 || len(vals) < period+1 {
		return out
	}
	var gainSum, lossSum float64
	for i := 1; i <= period; i++ {
		d := vals[i] - vals[i-1]
		if d > 0 {
			gainSum += d
		} else {
			lossSum -= d
		}
	}
	avgGain := gainSum / float64(period)
	avgLoss := lossSum / float64(period)
	out[period] = rsiValue(avgGain, avgLoss)
	for i := period + 1; i < len(vals); i++ {
		d := vals[i] - vals[i-1]
		gain, loss := 0.0, 0.0
		if d > 0 {
			gain = d
		} else {
			loss = -d
		}
		avgGain = (avgGain*float64(period-1) + gain) / float64(period)
		avgLoss = (avgLoss*float64(period-1) + loss) / float64(period)
		out[i] = rsiValue(avgGain, avgLoss)
	}
	return out
}

func rsiValue(avgGain, avgLoss float64) float64 {
	if avgLoss == 0 {
		if avgGain == 0 {
			return 50
		}
		return 100
	}
	rs := avgGain / avgLoss
	return 100 - 100/(1+rs)
}

// MACD computes the MACD line (EMA fast − EMA slow), its EMA signal line and
// the histogram (line − signal).
func MACD(vals []float64, fast, slow, signalPeriod int) (line, signal, hist []float64) {
	ef := EMA(vals, fast)
	es := EMA(vals, slow)
	line = nanSlice(len(vals))
	for i := range vals {
		if !math.IsNaN(ef[i]) && !math.IsNaN(es[i]) {
			line[i] = ef[i] - es[i]
		}
	}
	signal = emaOverNaN(line, signalPeriod)
	hist = nanSlice(len(vals))
	for i := range vals {
		if !math.IsNaN(line[i]) && !math.IsNaN(signal[i]) {
			hist[i] = line[i] - signal[i]
		}
	}
	return line, signal, hist
}

// Bollinger computes Bollinger Bands: mid = SMA(period), upper/lower =
// mid ± k·σ where σ is the population standard deviation over the window.
func Bollinger(vals []float64, period int, k float64) (upper, mid, lower []float64) {
	mid = SMA(vals, period)
	upper = nanSlice(len(vals))
	lower = nanSlice(len(vals))
	if period <= 0 || len(vals) < period {
		return upper, mid, lower
	}
	for i := period - 1; i < len(vals); i++ {
		m := mid[i]
		var ss float64
		for j := i - period + 1; j <= i; j++ {
			d := vals[j] - m
			ss += d * d
		}
		sd := math.Sqrt(ss / float64(period))
		upper[i] = m + k*sd
		lower[i] = m - k*sd
	}
	return upper, mid, lower
}

// ATR computes Wilder's Average True Range from high/low/close series.
// All three slices must have equal length.
func ATR(high, low, closes []float64, period int) []float64 {
	n := len(closes)
	out := nanSlice(n)
	if period <= 0 || n < period+1 || len(high) != n || len(low) != n {
		return out
	}
	tr := make([]float64, n)
	tr[0] = high[0] - low[0]
	for i := 1; i < n; i++ {
		hl := high[i] - low[i]
		hc := math.Abs(high[i] - closes[i-1])
		lc := math.Abs(low[i] - closes[i-1])
		tr[i] = math.Max(hl, math.Max(hc, lc))
	}
	var sum float64
	for i := 1; i <= period; i++ {
		sum += tr[i]
	}
	prev := sum / float64(period)
	out[period] = prev
	for i := period + 1; i < n; i++ {
		prev = (prev*float64(period-1) + tr[i]) / float64(period)
		out[i] = prev
	}
	return out
}

// Momentum computes vals[i] − vals[i−n].
func Momentum(vals []float64, n int) []float64 {
	out := nanSlice(len(vals))
	for i := n; i < len(vals); i++ {
		out[i] = vals[i] - vals[i-n]
	}
	return out
}

// ROC computes the rate of change in percent: (v[i]−v[i−n])/v[i−n]·100.
func ROC(vals []float64, n int) []float64 {
	out := nanSlice(len(vals))
	for i := n; i < len(vals); i++ {
		if vals[i-n] != 0 {
			out[i] = (vals[i] - vals[i-n]) / vals[i-n] * 100
		}
	}
	return out
}

// Volatility computes the rolling annualized volatility: the sample standard
// deviation of log returns over `window` returns, scaled by sqrt(365).
func Volatility(vals []float64, window int) []float64 {
	n := len(vals)
	out := nanSlice(n)
	if window <= 1 || n < window+1 {
		return out
	}
	rets := make([]float64, n) // rets[i] = ln(v[i]/v[i-1]), rets[0] unused
	for i := 1; i < n; i++ {
		if vals[i-1] > 0 && vals[i] > 0 {
			rets[i] = math.Log(vals[i] / vals[i-1])
		}
	}
	for i := window; i < n; i++ {
		var mean float64
		for j := i - window + 1; j <= i; j++ {
			mean += rets[j]
		}
		mean /= float64(window)
		var ss float64
		for j := i - window + 1; j <= i; j++ {
			d := rets[j] - mean
			ss += d * d
		}
		sd := math.Sqrt(ss / float64(window-1))
		out[i] = sd * math.Sqrt(365)
	}
	return out
}

// SupportResistance returns the swing low (support) and swing high
// (resistance) over the trailing `lookback` values.
func SupportResistance(vals []float64, lookback int) (support, resistance float64) {
	if len(vals) == 0 {
		return NaN, NaN
	}
	start := len(vals) - lookback
	if start < 0 {
		start = 0
	}
	support, resistance = vals[start], vals[start]
	for _, v := range vals[start:] {
		if v < support {
			support = v
		}
		if v > resistance {
			resistance = v
		}
	}
	return support, resistance
}
