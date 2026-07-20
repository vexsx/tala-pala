package indicators

import "math"

// This file implements the Addendum 2 expanded indicators. All functions are
// pure over aligned slices; positions with insufficient history are NaN.

// ADX computes Wilder's Average Directional Index. high/low/closes must have
// equal length. Directional movement and true range start at index 1, the
// Wilder-smoothed sums (and thus DI/DX) become available at index `period`,
// and the first ADX value — the average of the first `period` DX values —
// lands at index 2*period-1.
func ADX(high, low, closes []float64, period int) []float64 {
	n := len(closes)
	out := nanSlice(n)
	if period <= 0 || n < 2*period || len(high) != n || len(low) != n {
		return out
	}
	tr := make([]float64, n)
	pdm := make([]float64, n)
	mdm := make([]float64, n)
	for i := 1; i < n; i++ {
		hl := high[i] - low[i]
		hc := math.Abs(high[i] - closes[i-1])
		lc := math.Abs(low[i] - closes[i-1])
		tr[i] = math.Max(hl, math.Max(hc, lc))
		up := high[i] - high[i-1]
		down := low[i-1] - low[i]
		if up > down && up > 0 {
			pdm[i] = up
		}
		if down > up && down > 0 {
			mdm[i] = down
		}
	}
	// Wilder smoothing: seed with plain sums of the first `period` values,
	// then s = s - s/period + x each bar.
	var str, spdm, smdm float64
	for i := 1; i <= period; i++ {
		str += tr[i]
		spdm += pdm[i]
		smdm += mdm[i]
	}
	dx := nanSlice(n)
	dxVal := func() float64 {
		if str == 0 {
			return 0
		}
		pdi := 100 * spdm / str
		mdi := 100 * smdm / str
		if pdi+mdi == 0 {
			return 0
		}
		return 100 * math.Abs(pdi-mdi) / (pdi + mdi)
	}
	dx[period] = dxVal()
	for i := period + 1; i < n; i++ {
		str = str - str/float64(period) + tr[i]
		spdm = spdm - spdm/float64(period) + pdm[i]
		smdm = smdm - smdm/float64(period) + mdm[i]
		dx[i] = dxVal()
	}
	var sum float64
	for i := period; i < 2*period; i++ {
		sum += dx[i]
	}
	prev := sum / float64(period)
	out[2*period-1] = prev
	for i := 2 * period; i < n; i++ {
		prev = (prev*float64(period-1) + dx[i]) / float64(period)
		out[i] = prev
	}
	return out
}

// Stochastic computes the fast stochastic oscillator:
// %K = 100·(close − LL) / (HH − LL) over kPeriod bars, %D = SMA(%K, dPeriod).
// A flat window (HH == LL) yields the neutral value 50.
func Stochastic(high, low, closes []float64, kPeriod, dPeriod int) (k, d []float64) {
	n := len(closes)
	k = nanSlice(n)
	d = nanSlice(n)
	if kPeriod <= 0 || dPeriod <= 0 || n < kPeriod || len(high) != n || len(low) != n {
		return k, d
	}
	for i := kPeriod - 1; i < n; i++ {
		hh, ll := high[i], low[i]
		for j := i - kPeriod + 1; j <= i; j++ {
			hh = math.Max(hh, high[j])
			ll = math.Min(ll, low[j])
		}
		if hh == ll {
			k[i] = 50
		} else {
			k[i] = 100 * (closes[i] - ll) / (hh - ll)
		}
	}
	for i := kPeriod - 1 + dPeriod - 1; i < n; i++ {
		var s float64
		for j := i - dPeriod + 1; j <= i; j++ {
			s += k[j]
		}
		d[i] = s / float64(dPeriod)
	}
	return k, d
}

// WilliamsR computes Williams %R = −100·(HH − close) / (HH − LL) over
// `period` bars, ranging from 0 (at the high) to −100 (at the low).
// A flat window yields the neutral value −50.
func WilliamsR(high, low, closes []float64, period int) []float64 {
	n := len(closes)
	out := nanSlice(n)
	if period <= 0 || n < period || len(high) != n || len(low) != n {
		return out
	}
	for i := period - 1; i < n; i++ {
		hh, ll := high[i], low[i]
		for j := i - period + 1; j <= i; j++ {
			hh = math.Max(hh, high[j])
			ll = math.Min(ll, low[j])
		}
		if hh == ll {
			out[i] = -50
		} else {
			out[i] = -100 * (hh - closes[i]) / (hh - ll)
		}
	}
	return out
}

// CCI computes the Commodity Channel Index over typical prices
// tp = (high+low+close)/3: (tp − SMA(tp)) / (0.015 · mean absolute deviation).
// A zero-deviation window yields 0.
func CCI(high, low, closes []float64, period int) []float64 {
	n := len(closes)
	out := nanSlice(n)
	if period <= 0 || n < period || len(high) != n || len(low) != n {
		return out
	}
	tp := make([]float64, n)
	for i := range tp {
		tp[i] = (high[i] + low[i] + closes[i]) / 3
	}
	for i := period - 1; i < n; i++ {
		var mean float64
		for j := i - period + 1; j <= i; j++ {
			mean += tp[j]
		}
		mean /= float64(period)
		var dev float64
		for j := i - period + 1; j <= i; j++ {
			dev += math.Abs(tp[j] - mean)
		}
		dev /= float64(period)
		if dev == 0 {
			out[i] = 0
		} else {
			out[i] = (tp[i] - mean) / (0.015 * dev)
		}
	}
	return out
}

// Donchian computes Donchian Channels: upper = highest high and
// lower = lowest low over the trailing `period` bars.
func Donchian(high, low []float64, period int) (upper, lower []float64) {
	n := len(high)
	upper = nanSlice(n)
	lower = nanSlice(n)
	if period <= 0 || n < period || len(low) != n {
		return upper, lower
	}
	for i := period - 1; i < n; i++ {
		u, l := high[i], low[i]
		for j := i - period + 1; j <= i; j++ {
			u = math.Max(u, high[j])
			l = math.Min(l, low[j])
		}
		upper[i] = u
		lower[i] = l
	}
	return upper, lower
}

// Keltner computes Keltner Channels: mid = EMA(closes, emaPeriod),
// upper/lower = mid ± mult·ATR(atrPeriod). Values are defined once both the
// EMA and the ATR have warmed up.
func Keltner(high, low, closes []float64, emaPeriod, atrPeriod int, mult float64) (upper, mid, lower []float64) {
	mid = EMA(closes, emaPeriod)
	atr := ATR(high, low, closes, atrPeriod)
	n := len(closes)
	upper = nanSlice(n)
	lower = nanSlice(n)
	for i := 0; i < n; i++ {
		if !math.IsNaN(mid[i]) && !math.IsNaN(atr[i]) {
			upper[i] = mid[i] + mult*atr[i]
			lower[i] = mid[i] - mult*atr[i]
		}
	}
	return upper, mid, lower
}

// logReturns returns r[i] = ln(v[i]/v[i-1]) (NaN at index 0 and for
// non-positive prices).
func logReturns(vals []float64) []float64 {
	out := nanSlice(len(vals))
	for i := 1; i < len(vals); i++ {
		if vals[i-1] > 0 && vals[i] > 0 {
			out[i] = math.Log(vals[i] / vals[i-1])
		}
	}
	return out
}

// CorrelationLogReturns computes the rolling Pearson correlation of the log
// returns of two aligned price series over a window of `window` returns.
// out[i] uses returns i-window+1..i (i.e. prices i-window..i); it is NaN when
// either return series has a gap in the window or a zero variance.
func CorrelationLogReturns(a, b []float64, window int) []float64 {
	n := len(a)
	out := nanSlice(n)
	if window <= 1 || len(b) != n || n < window+1 {
		return out
	}
	ra := logReturns(a)
	rb := logReturns(b)
	for i := window; i < n; i++ {
		var ma, mb float64
		ok := true
		for j := i - window + 1; j <= i; j++ {
			if math.IsNaN(ra[j]) || math.IsNaN(rb[j]) {
				ok = false
				break
			}
			ma += ra[j]
			mb += rb[j]
		}
		if !ok {
			continue
		}
		ma /= float64(window)
		mb /= float64(window)
		var saa, sbb, sab float64
		for j := i - window + 1; j <= i; j++ {
			da := ra[j] - ma
			db := rb[j] - mb
			saa += da * da
			sbb += db * db
			sab += da * db
		}
		if saa > 0 && sbb > 0 {
			out[i] = sab / math.Sqrt(saa*sbb)
		}
	}
	return out
}

// DrawdownPct returns the percent decline of the last value from the highest
// value over the trailing `lookback` points: 0 when the series sits at its
// high, positive when below it. NaN for an empty series or non-positive high.
func DrawdownPct(vals []float64, lookback int) float64 {
	if len(vals) == 0 {
		return NaN
	}
	start := len(vals) - lookback
	if start < 0 {
		start = 0
	}
	high := vals[start]
	for _, v := range vals[start:] {
		if v > high {
			high = v
		}
	}
	if high <= 0 {
		return NaN
	}
	return (high - vals[len(vals)-1]) / high * 100
}
