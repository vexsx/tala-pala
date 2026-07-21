package prices

import (
	"context"
	"net/http"
	"sort"
	"strconv"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Iranian data providers frequently disagree on the same symbol (different
// bid/ask handling, update cadence, retail vs wholesale quotes). The gap
// between them is real quote uncertainty: it is surfaced here for the UI and
// factored into prediction intervals by the Python service.

const (
	gapDefaultWindowMinutes = 120
	gapMaxWindowMinutes     = 24 * 60
	gapDefaultHistoryDays   = 30
	gapMaxHistoryDays       = 180
)

type providerQuote struct {
	Provider   string    `json:"provider"`
	Value      float64   `json:"value"`
	ObservedAt time.Time `json:"observed_at"`
}

type gapHistoryPoint struct {
	Date       string  `json:"date"`
	GapAbs     float64 `json:"gap_abs"`
	GapPct     float64 `json:"gap_pct"`
	Mid        float64 `json:"mid"`
	NProviders int     `json:"n_providers"`
}

// gapStats returns (gapAbs, gapPct, mid) across values; ok=false when <2 values.
func gapStats(values []float64) (float64, float64, float64, bool) {
	if len(values) < 2 {
		return 0, 0, 0, false
	}
	sorted := append([]float64(nil), values...)
	sort.Float64s(sorted)
	minV, maxV := sorted[0], sorted[len(sorted)-1]
	mid := sorted[len(sorted)/2]
	if len(sorted)%2 == 0 {
		mid = (sorted[len(sorted)/2-1] + sorted[len(sorted)/2]) / 2
	}
	if mid == 0 {
		return maxV - minV, 0, mid, false
	}
	return maxV - minV, (maxV - minV) / mid * 100.0, mid, true
}

func (h *Handler) latestPerProvider(ctx context.Context, symbol string, since time.Time) ([]providerQuote, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT DISTINCT ON (source) source, value::float8, observed_at
		FROM prices
		WHERE symbol = $1 AND quality = 'ok' AND observed_at >= $2
		ORDER BY source, observed_at DESC`, symbol, since)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	quotes := []providerQuote{}
	for rows.Next() {
		var q providerQuote
		if err := rows.Scan(&q.Provider, &q.Value, &q.ObservedAt); err != nil {
			return nil, err
		}
		q.ObservedAt = q.ObservedAt.UTC()
		quotes = append(quotes, q)
	}
	return quotes, rows.Err()
}

func (h *Handler) gapHistory(ctx context.Context, symbol string, days int) ([]gapHistoryPoint, error) {
	since := time.Now().UTC().AddDate(0, 0, -days)
	rows, err := h.Pool.Query(ctx, `
		SELECT DISTINCT ON (date_trunc('day', observed_at), source)
			date_trunc('day', observed_at) AS day, source, value::float8
		FROM prices
		WHERE symbol = $1 AND quality = 'ok' AND observed_at >= $2
		ORDER BY date_trunc('day', observed_at), source, observed_at DESC`, symbol, since)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	byDay := map[string][]float64{}
	for rows.Next() {
		var day time.Time
		var source string
		var value float64
		if err := rows.Scan(&day, &source, &value); err != nil {
			return nil, err
		}
		key := day.UTC().Format("2006-01-02")
		byDay[key] = append(byDay[key], value)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	days_ := make([]string, 0, len(byDay))
	for d := range byDay {
		days_ = append(days_, d)
	}
	sort.Strings(days_)
	out := []gapHistoryPoint{}
	for _, d := range days_ {
		gapAbs, gapPct, mid, ok := gapStats(byDay[d])
		if !ok {
			continue // a single provider that day has no measurable gap
		}
		out = append(out, gapHistoryPoint{
			Date: d, GapAbs: gapAbs, GapPct: gapPct, Mid: mid, NProviders: len(byDay[d]),
		})
	}
	return out, nil
}

// ProviderGap implements GET /api/v1/market/provider-gap.
// Query params: symbol (default IR_GOLD_18K), window_minutes (default 120),
// history_days (default 30, 0 disables history).
func (h *Handler) ProviderGap(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	symbol := q.Get("symbol")
	if symbol == "" {
		symbol = "IR_GOLD_18K"
	}
	if !KnownSymbols[symbol] {
		httpserver.BadRequest(w, "unknown symbol", map[string]any{"symbol": symbol})
		return
	}
	window := clampQueryInt(q.Get("window_minutes"), gapDefaultWindowMinutes, 5, gapMaxWindowMinutes)
	historyDays := clampQueryInt(q.Get("history_days"), gapDefaultHistoryDays, 0, gapMaxHistoryDays)

	now := time.Now().UTC()
	quotes, err := h.latestPerProvider(r.Context(), symbol, now.Add(-time.Duration(window)*time.Minute))
	if err != nil {
		h.Log.Error("provider_gap_current", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	values := make([]float64, len(quotes))
	for i, quote := range quotes {
		values[i] = quote.Value
	}
	gapAbs, gapPct, mid, ok := gapStats(values)

	resp := map[string]any{
		"symbol":         symbol,
		"window_minutes": window,
		"providers":      quotes,
		"as_of":          now,
	}
	if ok {
		resp["gap_abs"] = gapAbs
		resp["gap_pct"] = gapPct
		resp["mid"] = mid
	} else {
		resp["gap_abs"] = nil
		resp["gap_pct"] = nil
		resp["mid"] = nil
	}
	if historyDays > 0 {
		history, err := h.gapHistory(r.Context(), symbol, historyDays)
		if err != nil {
			h.Log.Error("provider_gap_history", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		resp["history"] = history
	}
	httpserver.JSON(w, http.StatusOK, resp)
}

func clampQueryInt(s string, def, minV, maxV int) int {
	if s == "" {
		return def
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	if n < minV {
		return minV
	}
	if n > maxV {
		return maxV
	}
	return n
}
