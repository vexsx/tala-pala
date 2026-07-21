// Trading-desk indicators (Addendum 4): Ichimoku, SuperTrend, Parabolic SAR
// and classic floor-trader pivot points. Same conventions as the rest of the
// package: causal rolling windows, NaN until warm-up, plain float64 slices
// aligned with the input.
package indicators

import "math"

// Ichimoku periods (standard 9/26/52).
const (
	IchimokuTenkan = 9
	IchimokuKijun  = 26
	IchimokuSenkou = 52
)

// rollingMid returns (max+min)/2 of the trailing `period` highs/lows.
func rollingMid(high, low []float64, period int) []float64 {
	n := len(high)
	out := nanSlice(n)
	for i := period - 1; i < n; i++ {
		hi := math.Inf(-1)
		lo := math.Inf(1)
		for j := i - period + 1; j <= i; j++ {
			hi = math.Max(hi, high[j])
			lo = math.Min(lo, low[j])
		}
		out[i] = (hi + lo) / 2
	}
	return out
}

// Ichimoku computes the four Ichimoku lines WITHOUT the forward/backward
// displacement: every value is aligned to the bar it was computed on. The
// standard chart shifts senkou A/B forward 26 bars (the cloud) and chikou
// (just the close) back 26 bars — plotting concerns left to the client.
func Ichimoku(high, low []float64) (tenkan, kijun, senkouA, senkouB []float64) {
	n := len(high)
	tenkan = rollingMid(high, low, IchimokuTenkan)
	kijun = rollingMid(high, low, IchimokuKijun)
	senkouA = nanSlice(n)
	for i := 0; i < n; i++ {
		if !math.IsNaN(tenkan[i]) && !math.IsNaN(kijun[i]) {
			senkouA[i] = (tenkan[i] + kijun[i]) / 2
		}
	}
	senkouB = rollingMid(high, low, IchimokuSenkou)
	return tenkan, kijun, senkouA, senkouB
}

// SuperTrend computes the ATR trailing stop-and-reverse line. Returns the
// line and its direction per bar (+1 = price above the line, bullish;
// -1 = bearish; 0 = warm-up). period/mult of 10/3 are the common defaults.
func SuperTrend(high, low, closes []float64, period int, mult float64) (line []float64, dir []int) {
	n := len(closes)
	line = nanSlice(n)
	dir = make([]int, n)
	atr := ATR(high, low, closes, period)

	// running (final) upper/lower bands per the standard recurrence
	finalUpper, finalLower := nanSlice(n), nanSlice(n)
	for i := 0; i < n; i++ {
		if math.IsNaN(atr[i]) {
			continue
		}
		mid := (high[i] + low[i]) / 2
		upper := mid + mult*atr[i]
		lower := mid - mult*atr[i]
		if i > 0 && !math.IsNaN(finalUpper[i-1]) {
			// bands only ratchet toward price, never away from it
			if upper < finalUpper[i-1] || closes[i-1] > finalUpper[i-1] {
				finalUpper[i] = upper
			} else {
				finalUpper[i] = finalUpper[i-1]
			}
			if lower > finalLower[i-1] || closes[i-1] < finalLower[i-1] {
				finalLower[i] = lower
			} else {
				finalLower[i] = finalLower[i-1]
			}
		} else {
			finalUpper[i] = upper
			finalLower[i] = lower
		}

		prevDir := 0
		if i > 0 {
			prevDir = dir[i-1]
		}
		switch {
		case prevDir == 0:
			if closes[i] >= mid {
				dir[i] = 1
			} else {
				dir[i] = -1
			}
		case prevDir == 1 && closes[i] < finalLower[i]:
			dir[i] = -1
		case prevDir == -1 && closes[i] > finalUpper[i]:
			dir[i] = 1
		default:
			dir[i] = prevDir
		}
		if dir[i] == 1 {
			line[i] = finalLower[i]
		} else {
			line[i] = finalUpper[i]
		}
	}
	return line, dir
}

// ParabolicSAR computes Wilder's parabolic stop-and-reverse.
// afStart/afStep/afMax of 0.02/0.02/0.2 are the classic defaults.
func ParabolicSAR(high, low []float64, afStart, afStep, afMax float64) []float64 {
	n := len(high)
	out := nanSlice(n)
	if n < 2 {
		return out
	}
	uptrend := high[1]+low[1] >= high[0]+low[0]
	af := afStart
	var sar, ep float64
	if uptrend {
		sar, ep = low[0], high[1]
	} else {
		sar, ep = high[0], low[1]
	}
	out[1] = sar
	for i := 2; i < n; i++ {
		sar = sar + af*(ep-sar)
		if uptrend {
			// SAR may never rise into the prior two bars' lows
			sar = math.Min(sar, math.Min(low[i-1], low[i-2]))
			if low[i] < sar { // reversal
				uptrend = false
				sar = ep
				ep = low[i]
				af = afStart
			} else if high[i] > ep {
				ep = high[i]
				af = math.Min(af+afStep, afMax)
			}
		} else {
			sar = math.Max(sar, math.Max(high[i-1], high[i-2]))
			if high[i] > sar { // reversal
				uptrend = true
				sar = ep
				ep = high[i]
				af = afStart
			} else if low[i] < ep {
				ep = low[i]
				af = math.Min(af+afStep, afMax)
			}
		}
		out[i] = sar
	}
	return out
}

// PivotPoints returns the classic floor-trader levels computed from the last
// COMPLETED period's high/low/close.
type PivotPoints struct {
	P  float64 `json:"p"`
	R1 float64 `json:"r1"`
	R2 float64 `json:"r2"`
	R3 float64 `json:"r3"`
	S1 float64 `json:"s1"`
	S2 float64 `json:"s2"`
	S3 float64 `json:"s3"`
}

// Pivots computes classic pivots from one completed bar.
func Pivots(high, low, closePrice float64) PivotPoints {
	p := (high + low + closePrice) / 3
	return PivotPoints{
		P:  p,
		R1: 2*p - low,
		R2: p + (high - low),
		R3: high + 2*(p-low),
		S1: 2*p - high,
		S2: p - (high - low),
		S3: low - 2*(high-p),
	}
}
