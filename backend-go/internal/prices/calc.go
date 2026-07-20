// Package prices serves market-data endpoints: current prices, history,
// market summary, premium and technical indicators.
package prices

import (
	"math"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/indicators"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/markethours"
)

// TroyOunceGrams is the gram weight of one troy ounce.
const TroyOunceGrams = 31.1034768

// Purity18k is the fine-gold fraction of 18-karat gold.
const Purity18k = 0.750

// Theoretical18kIRT computes the theoretical 18k price in IRT per gram from
// the global gold price (USD/ozt) and the free-market USD rate (IRT/USD).
// Mirrors Python core/formula.py.
func Theoretical18kIRT(xauUSD, usdIRT float64) float64 {
	pureGramUSD := xauUSD / TroyOunceGrams
	pureGramIRT := pureGramUSD * usdIRT
	return pureGramIRT * Purity18k
}

// PremiumPct computes the observed-vs-theoretical premium in percent.
func PremiumPct(observed18k, theoretical18k float64) float64 {
	if theoretical18k == 0 {
		return math.NaN()
	}
	return (observed18k - theoretical18k) / theoretical18k * 100
}

// ChangePct computes the percent change from prev to cur.
func ChangePct(cur, prev float64) float64 {
	if prev == 0 {
		return math.NaN()
	}
	return (cur - prev) / prev * 100
}

// DayBar is one daily bucket of a price series (min/max/last approximate
// low/high/close for ATR purposes, per contract).
type DayBar struct {
	Date  time.Time
	High  float64
	Low   float64
	Close float64
}

// IndicatorPoint is one day of the indicator series returned to clients.
type IndicatorPoint struct {
	Date       string   `json:"date"`
	Close      float64  `json:"close"`
	SMA20      *float64 `json:"sma_20"`
	SMA50      *float64 `json:"sma_50"`
	EMA12      *float64 `json:"ema_12"`
	EMA26      *float64 `json:"ema_26"`
	RSI14      *float64 `json:"rsi_14"`
	MACD       *float64 `json:"macd"`
	MACDSignal *float64 `json:"macd_signal"`
	MACDHist   *float64 `json:"macd_hist"`
	BollUpper  *float64 `json:"bollinger_upper"`
	BollMid    *float64 `json:"bollinger_mid"`
	BollLower  *float64 `json:"bollinger_lower"`
	ATR14      *float64 `json:"atr_14"`
	ADX14      *float64 `json:"adx_14"`
	StochK     *float64 `json:"stoch_k"`
	StochD     *float64 `json:"stoch_d"`
}

// IndicatorsResult is the response of GET /api/v1/market/indicators.
type IndicatorsResult struct {
	Symbol     string           `json:"symbol"`
	AsOf       *string          `json:"as_of"`
	Days       int              `json:"days"`
	SMA20      *float64         `json:"sma_20"`
	SMA50      *float64         `json:"sma_50"`
	EMA12      *float64         `json:"ema_12"`
	EMA26      *float64         `json:"ema_26"`
	RSI14      *float64         `json:"rsi_14"`
	MACD       macdOut          `json:"macd"`
	Bollinger  bollingerOut     `json:"bollinger"`
	ATR14      *float64         `json:"atr_14"`
	Momentum10 *float64         `json:"momentum_10"`
	ROC10      *float64         `json:"roc_10"`
	Volatility *float64         `json:"volatility_20"`
	Support    *float64         `json:"support"`
	Resistance *float64         `json:"resistance"`
	ADX14      *float64         `json:"adx_14"`
	StochK     *float64         `json:"stoch_k"`
	StochD     *float64         `json:"stoch_d"`
	WilliamsR  *float64         `json:"williams_r_14"`
	CCI20      *float64         `json:"cci_20"`
	Donchian   donchianOut      `json:"donchian"`
	Keltner    keltnerOut       `json:"keltner"`
	CorrXAU20  *float64         `json:"corr_xau_20"`
	Drawdown   *float64         `json:"drawdown_pct"`
	Series     []IndicatorPoint `json:"series"`
}

type macdOut struct {
	Line   *float64 `json:"line"`
	Signal *float64 `json:"signal"`
	Hist   *float64 `json:"hist"`
}

type bollingerOut struct {
	Upper *float64 `json:"upper"`
	Mid   *float64 `json:"mid"`
	Lower *float64 `json:"lower"`
}

type donchianOut struct {
	Upper *float64 `json:"upper"`
	Lower *float64 `json:"lower"`
}

type keltnerOut struct {
	Upper *float64 `json:"upper"`
	Mid   *float64 `json:"mid"`
	Lower *float64 `json:"lower"`
}

// fp converts a possibly-NaN float to a JSON-friendly *float64 (nil for NaN).
func fp(v float64) *float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	r := math.Round(v*1e6) / 1e6
	return &r
}

// ComputeIndicators runs the full indicator suite over daily bars. The last
// `days` points are returned as a series; the full history is used as
// warm-up so leading values are well-defined. `xau` is the XAUUSD daily
// close series used for corr_xau_20 (rolling Pearson correlation of daily
// log returns, joined by date). Pure function (unit tested).
func ComputeIndicators(bars []DayBar, xau []dailyPoint, days int) IndicatorsResult {
	res := IndicatorsResult{Symbol: "IR_GOLD_18K", Days: days, Series: []IndicatorPoint{}}
	n := len(bars)
	if n == 0 {
		return res
	}

	closes := make([]float64, n)
	highs := make([]float64, n)
	lows := make([]float64, n)
	for i, b := range bars {
		closes[i] = b.Close
		highs[i] = b.High
		lows[i] = b.Low
	}

	sma20 := indicators.SMA(closes, 20)
	sma50 := indicators.SMA(closes, 50)
	ema12 := indicators.EMA(closes, 12)
	ema26 := indicators.EMA(closes, 26)
	rsi14 := indicators.RSI(closes, 14)
	macdLine, macdSig, macdHist := indicators.MACD(closes, 12, 26, 9)
	bbU, bbM, bbL := indicators.Bollinger(closes, 20, 2)
	atr14 := indicators.ATR(highs, lows, closes, 14)
	mom10 := indicators.Momentum(closes, 10)
	roc10 := indicators.ROC(closes, 10)
	vol20 := indicators.Volatility(closes, 20)
	support, resistance := indicators.SupportResistance(closes, 20)
	adx14 := indicators.ADX(highs, lows, closes, 14)
	stochK, stochD := indicators.Stochastic(highs, lows, closes, 14, 3)
	wr14 := indicators.WilliamsR(highs, lows, closes, 14)
	cci20 := indicators.CCI(highs, lows, closes, 20)
	donU, donL := indicators.Donchian(highs, lows, 20)
	kelU, kelM, kelL := indicators.Keltner(highs, lows, closes, 20, 14, 2)

	last := n - 1
	asOf := bars[last].Date.UTC().Format(time.RFC3339)
	res.AsOf = &asOf
	res.SMA20 = fp(sma20[last])
	res.SMA50 = fp(sma50[last])
	res.EMA12 = fp(ema12[last])
	res.EMA26 = fp(ema26[last])
	res.RSI14 = fp(rsi14[last])
	res.MACD = macdOut{Line: fp(macdLine[last]), Signal: fp(macdSig[last]), Hist: fp(macdHist[last])}
	res.Bollinger = bollingerOut{Upper: fp(bbU[last]), Mid: fp(bbM[last]), Lower: fp(bbL[last])}
	res.ATR14 = fp(atr14[last])
	res.Momentum10 = fp(mom10[last])
	res.ROC10 = fp(roc10[last])
	res.Volatility = fp(vol20[last])
	res.Support = fp(support)
	res.Resistance = fp(resistance)
	res.ADX14 = fp(adx14[last])
	res.StochK = fp(stochK[last])
	res.StochD = fp(stochD[last])
	res.WilliamsR = fp(wr14[last])
	res.CCI20 = fp(cci20[last])
	res.Donchian = donchianOut{Upper: fp(donU[last]), Lower: fp(donL[last])}
	res.Keltner = keltnerOut{Upper: fp(kelU[last]), Mid: fp(kelM[last]), Lower: fp(kelL[last])}
	res.Drawdown = fp(indicators.DrawdownPct(closes, 90))

	// corr_xau_20: pair the two daily series by date, then take the latest
	// rolling correlation of log returns over 20 return observations.
	xauBy := map[string]float64{}
	for _, p := range xau {
		xauBy[p.Date.Format("2006-01-02")] = p.Value
	}
	var goldAligned, xauAligned []float64
	for i, b := range bars {
		if v, ok := xauBy[b.Date.Format("2006-01-02")]; ok {
			goldAligned = append(goldAligned, closes[i])
			xauAligned = append(xauAligned, v)
		}
	}
	if corr := indicators.CorrelationLogReturns(goldAligned, xauAligned, 20); len(corr) > 0 {
		res.CorrXAU20 = fp(corr[len(corr)-1])
	}

	start := n - days
	if start < 0 {
		start = 0
	}
	for i := start; i < n; i++ {
		res.Series = append(res.Series, IndicatorPoint{
			Date:       bars[i].Date.UTC().Format("2006-01-02"),
			Close:      closes[i],
			SMA20:      fp(sma20[i]),
			SMA50:      fp(sma50[i]),
			EMA12:      fp(ema12[i]),
			EMA26:      fp(ema26[i]),
			RSI14:      fp(rsi14[i]),
			MACD:       fp(macdLine[i]),
			MACDSignal: fp(macdSig[i]),
			MACDHist:   fp(macdHist[i]),
			BollUpper:  fp(bbU[i]),
			BollMid:    fp(bbM[i]),
			BollLower:  fp(bbL[i]),
			ATR14:      fp(atr14[i]),
			ADX14:      fp(adx14[i]),
			StochK:     fp(stochK[i]),
			StochD:     fp(stochD[i]),
		})
	}
	return res
}

// IsStale reports whether an observation is older than staleMinutes
// (the fixed-age check; endpoints use markethours.AcceptablyFresh so that
// last-session data does not read as stale while the market is closed).
func IsStale(observedAt, now time.Time, staleMinutes int) bool {
	return now.Sub(observedAt) > time.Duration(staleMinutes)*time.Minute
}

// MarketState returns "open" or "closed" for a symbol at `now` under the
// Addendum 1 market-calendar rules.
func MarketState(symbol string, now time.Time, open, close string) string {
	if markethours.IsOpen(symbol, now, open, close) {
		return "open"
	}
	return "closed"
}
