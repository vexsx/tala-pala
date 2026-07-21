package prices

import (
	"context"
	"net/http"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/indicators"
)

// The trading panel's data feed: OHLC candles synthesized from tick history
// (open/high/low/close per bucket) plus chart-ready overlay series computed
// on the same buckets, all aligned by index. One request paints the panel.

type candle struct {
	T     int64   `json:"t"` // unix seconds (bucket start, UTC)
	Open  float64 `json:"open"`
	High  float64 `json:"high"`
	Low   float64 `json:"low"`
	Close float64 `json:"close"`
}

type candleBar struct {
	date                   time.Time
	open, high, low, close float64
}

func (h *Handler) ohlcBars(ctx context.Context, symbol, interval string, since time.Time) ([]candleBar, error) {
	trunc := "day"
	if interval == "hourly" {
		trunc = "hour"
	}
	rows, err := h.Pool.Query(ctx, `
		SELECT date_trunc('`+trunc+`', observed_at) AS bucket,
		       (array_agg(value ORDER BY observed_at ASC))[1]::float8  AS open,
		       max(value)::float8                                       AS high,
		       min(value)::float8                                       AS low,
		       (array_agg(value ORDER BY observed_at DESC))[1]::float8 AS close
		FROM prices
		WHERE symbol=$1 AND quality='ok' AND observed_at >= $2
		GROUP BY bucket ORDER BY bucket ASC`, symbol, since)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []candleBar
	for rows.Next() {
		var b candleBar
		if err := rows.Scan(&b.date, &b.open, &b.high, &b.low, &b.close); err != nil {
			return nil, err
		}
		b.date = b.date.UTC()
		out = append(out, b)
	}
	return out, rows.Err()
}

func fpSlice(vals []float64) []*float64 {
	out := make([]*float64, len(vals))
	for i, v := range vals {
		out[i] = fp(v)
	}
	return out
}

// Candles implements GET /api/v1/market/candles.
// Query: symbol (default IR_GOLD_18K), interval=daily|hourly (default daily),
// days (default 120 daily / 14 hourly). Overlay arrays are index-aligned
// with the candles; nulls during indicator warm-up.
func (h *Handler) Candles(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	symbol := q.Get("symbol")
	if symbol == "" {
		symbol = "IR_GOLD_18K"
	}
	if !KnownSymbols[symbol] {
		httpserver.BadRequest(w, "unknown symbol", map[string]any{"symbol": symbol})
		return
	}
	interval := q.Get("interval")
	if interval == "" {
		interval = "daily"
	}
	if interval != "daily" && interval != "hourly" {
		httpserver.BadRequest(w, "interval must be daily or hourly", nil)
		return
	}
	defDays := 120
	if interval == "hourly" {
		defDays = 14
	}
	days := intParam(q.Get("days"), defDays, 1, 3650)
	// warm-up lead-in so overlays are defined from the first visible candle
	// (ichimoku senkou B needs 52 buckets, SMA50 needs 50)
	warmup := 60
	if interval == "daily" {
		warmup = 60
	}
	sinceDays := days + warmup
	if interval == "hourly" {
		sinceDays = days + 3 // 3 days ≈ 60+ trading hours of warm-up
	}
	since := time.Now().UTC().AddDate(0, 0, -sinceDays)

	bars, err := h.ohlcBars(r.Context(), symbol, interval, since)
	if err != nil {
		h.Log.Error("candles_query", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	n := len(bars)
	closes := make([]float64, n)
	highs := make([]float64, n)
	lows := make([]float64, n)
	for i, b := range bars {
		closes[i], highs[i], lows[i] = b.close, b.high, b.low
	}

	sma20 := indicators.SMA(closes, 20)
	sma50 := indicators.SMA(closes, 50)
	bbU, bbM, bbL := indicators.Bollinger(closes, 20, 2)
	stLine, stDir := indicators.SuperTrend(highs, lows, closes, 10, 3)
	psar := indicators.ParabolicSAR(highs, lows, 0.02, 0.02, 0.2)
	tenkan, kijun, senkouA, senkouB := indicators.Ichimoku(highs, lows)

	// visible window: last `days` buckets (hourly buckets: days*24 cap)
	visible := days
	if interval == "hourly" {
		visible = days * 24
	}
	start := n - visible
	if start < 0 {
		start = 0
	}

	candles := make([]candle, 0, n-start)
	for i := start; i < n; i++ {
		candles = append(candles, candle{
			T:    bars[i].date.Unix(),
			Open: bars[i].open, High: bars[i].high,
			Low: bars[i].low, Close: bars[i].close,
		})
	}
	window := func(vals []float64) []*float64 { return fpSlice(vals[start:]) }

	resp := map[string]any{
		"symbol":   symbol,
		"interval": interval,
		"candles":  candles,
		"overlays": map[string]any{
			"sma_20":          window(sma20),
			"sma_50":          window(sma50),
			"bollinger_upper": window(bbU),
			"bollinger_mid":   window(bbM),
			"bollinger_lower": window(bbL),
			"supertrend":      window(stLine),
			"supertrend_dir":  stDir[start:],
			"psar":            window(psar),
			"ichimoku_tenkan": window(tenkan),
			"ichimoku_kijun":  window(kijun),
			"ichimoku_senkou_a": window(senkouA),
			"ichimoku_senkou_b": window(senkouB),
		},
		"as_of": time.Now().UTC(),
	}
	if n >= 2 {
		piv := indicators.Pivots(bars[n-2].high, bars[n-2].low, bars[n-2].close)
		resp["pivots"] = piv
	}
	if support, resistance := indicators.SupportResistance(closes, 20); true {
		resp["support"] = fp(support)
		resp["resistance"] = fp(resistance)
	}
	httpserver.JSON(w, http.StatusOK, resp)
}
