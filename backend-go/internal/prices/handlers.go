package prices

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/markethours"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/storage"
)

// KnownSymbols is the canonical symbol set from CONTRACTS.md.
var KnownSymbols = map[string]bool{
	"IR_GOLD_18K": true, "XAUUSD": true, "XAGUSD": true, "USD_IRT": true,
	"IR_COIN_EMAMI": true, "BRENT_OIL": true, "DXY": true, "US10Y": true,
	// Tehran-exchange gold funds (Addendum 7).
	"IR_GOLD_FUND_AYAR": true, "IR_GOLD_FUND_TALA": true,
	"IR_GOLD_FUND_KAHRABA": true, "IR_GOLD_FUND_FLOW": true,
}

// Handler serves the market-data endpoints.
type Handler struct {
	Pool                *pgxpool.Pool
	Log                 *slog.Logger
	StaleMinutesDefault int
	// Tehran session bounds ("HH:MM") for market_state / freshness.
	MarketOpen  string
	MarketClose string
}

type latestPrice struct {
	Symbol     string
	Value      float64
	Currency   string
	Unit       string
	Source     string
	ObservedAt time.Time
}

func (h *Handler) latestPrices(ctx context.Context) (map[string]latestPrice, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT DISTINCT ON (symbol) symbol, value::float8, currency, unit, source, observed_at
		FROM prices
		WHERE quality = 'ok'
		ORDER BY symbol, observed_at DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]latestPrice{}
	for rows.Next() {
		var p latestPrice
		if err := rows.Scan(&p.Symbol, &p.Value, &p.Currency, &p.Unit, &p.Source, &p.ObservedAt); err != nil {
			return nil, err
		}
		out[p.Symbol] = p
	}
	return out, rows.Err()
}

func (h *Handler) pricesAsOf(ctx context.Context, cutoff time.Time) (map[string]float64, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT DISTINCT ON (symbol) symbol, value::float8
		FROM prices
		WHERE quality = 'ok' AND observed_at <= $1
		ORDER BY symbol, observed_at DESC`, cutoff)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]float64{}
	for rows.Next() {
		var sym string
		var v float64
		if err := rows.Scan(&sym, &v); err != nil {
			return nil, err
		}
		out[sym] = v
	}
	return out, rows.Err()
}

// Current implements GET /api/v1/prices/current.
func (h *Handler) Current(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	now := time.Now().UTC()
	latest, err := h.latestPrices(ctx)
	if err != nil {
		h.Log.Error("prices_current_query", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	prev, err := h.pricesAsOf(ctx, now.Add(-24*time.Hour))
	if err != nil {
		h.Log.Error("prices_current_prev", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	staleMin := storage.GetStaleMinutes(ctx, h.Pool, h.StaleMinutesDefault)

	out := map[string]any{}
	for sym, p := range latest {
		entry := map[string]any{
			"value":        p.Value,
			"currency":     p.Currency,
			"unit":         p.Unit,
			"source":       p.Source,
			"observed_at":  p.ObservedAt.UTC(),
			"stale":        !markethours.AcceptablyFresh(sym, p.ObservedAt, now, staleMin, h.MarketOpen, h.MarketClose),
			"market_state": MarketState(sym, now, h.MarketOpen, h.MarketClose),
		}
		if pv, ok := prev[sym]; ok {
			entry["change_24h_pct"] = fp(ChangePct(p.Value, pv))
		} else {
			entry["change_24h_pct"] = nil
		}
		out[sym] = entry
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{"prices": out, "as_of": now})
}

// History implements GET /api/v1/prices/history.
func (h *Handler) History(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	symbol := q.Get("symbol")
	if !KnownSymbols[symbol] {
		httpserver.BadRequest(w, "unknown or missing symbol", map[string]any{"symbol": symbol})
		return
	}
	interval := q.Get("interval")
	if interval == "" {
		interval = "raw"
	}
	if interval != "raw" && interval != "hourly" && interval != "daily" {
		httpserver.BadRequest(w, "interval must be raw, hourly or daily", nil)
		return
	}
	now := time.Now().UTC()
	from, to := now.Add(-30*24*time.Hour), now
	if v := q.Get("from"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			httpserver.BadRequest(w, "from must be RFC3339", nil)
			return
		}
		from = t.UTC()
	}
	if v := q.Get("to"); v != "" {
		t, err := time.Parse(time.RFC3339, v)
		if err != nil {
			httpserver.BadRequest(w, "to must be RFC3339", nil)
			return
		}
		to = t.UTC()
	}
	page := intParam(q.Get("page"), 1, 1, 1_000_000)
	pageSize := intParam(q.Get("page_size"), 500, 1, 1000)
	offset := (page - 1) * pageSize

	ctx := r.Context()
	type item struct {
		ObservedAt time.Time `json:"observed_at"`
		Value      float64   `json:"value"`
		Source     string    `json:"source"`
	}
	items := []item{}
	var total int

	if interval == "raw" {
		err := h.Pool.QueryRow(ctx, `
			SELECT count(*) FROM prices
			WHERE symbol=$1 AND quality='ok' AND observed_at BETWEEN $2 AND $3`,
			symbol, from, to).Scan(&total)
		if err == nil {
			var rows pgx.Rows
			rows, err = h.Pool.Query(ctx, `
				SELECT observed_at, value::float8, source FROM prices
				WHERE symbol=$1 AND quality='ok' AND observed_at BETWEEN $2 AND $3
				ORDER BY observed_at ASC LIMIT $4 OFFSET $5`,
				symbol, from, to, pageSize, offset)
			if err == nil {
				defer rows.Close()
				for rows.Next() {
					var it item
					if err = rows.Scan(&it.ObservedAt, &it.Value, &it.Source); err != nil {
						break
					}
					it.ObservedAt = it.ObservedAt.UTC()
					items = append(items, it)
				}
				if err == nil {
					err = rows.Err()
				}
			}
		}
		if err != nil {
			h.Log.Error("prices_history_raw", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
	} else {
		trunc := "hour"
		if interval == "daily" {
			trunc = "day"
		}
		err := h.Pool.QueryRow(ctx, `
			SELECT count(DISTINCT date_trunc($4, observed_at)) FROM prices
			WHERE symbol=$1 AND quality='ok' AND observed_at BETWEEN $2 AND $3`,
			symbol, from, to, trunc).Scan(&total)
		if err == nil {
			var rows pgx.Rows
			rows, err = h.Pool.Query(ctx, `
				SELECT date_trunc($4, observed_at) AS bucket,
				       (array_agg(value ORDER BY observed_at DESC))[1]::float8 AS value
				FROM prices
				WHERE symbol=$1 AND quality='ok' AND observed_at BETWEEN $2 AND $3
				GROUP BY bucket ORDER BY bucket ASC LIMIT $5 OFFSET $6`,
				symbol, from, to, trunc, pageSize, offset)
			if err == nil {
				defer rows.Close()
				for rows.Next() {
					var it item
					if err = rows.Scan(&it.ObservedAt, &it.Value); err != nil {
						break
					}
					it.ObservedAt = it.ObservedAt.UTC()
					it.Source = "aggregate"
					items = append(items, it)
				}
				if err == nil {
					err = rows.Err()
				}
			}
		}
		if err != nil {
			h.Log.Error("prices_history_bucketed", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
	}

	httpserver.JSON(w, http.StatusOK, map[string]any{
		"items": items, "page": page, "page_size": pageSize, "total": total,
	})
}

type dailyPoint struct {
	Date  time.Time
	Value float64
}

func (h *Handler) dailySeries(ctx context.Context, symbol string, since time.Time) ([]dailyPoint, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT date_trunc('day', observed_at) AS d,
		       (array_agg(value ORDER BY observed_at DESC))[1]::float8 AS v
		FROM prices
		WHERE symbol=$1 AND quality='ok' AND observed_at >= $2
		GROUP BY d ORDER BY d ASC`, symbol, since)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []dailyPoint
	for rows.Next() {
		var p dailyPoint
		if err := rows.Scan(&p.Date, &p.Value); err != nil {
			return nil, err
		}
		p.Date = p.Date.UTC()
		out = append(out, p)
	}
	return out, rows.Err()
}

func (h *Handler) dailyBars(ctx context.Context, symbol string, since time.Time) ([]DayBar, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT date_trunc('day', observed_at) AS d,
		       max(value)::float8, min(value)::float8,
		       (array_agg(value ORDER BY observed_at DESC))[1]::float8
		FROM prices
		WHERE symbol=$1 AND quality='ok' AND observed_at >= $2
		GROUP BY d ORDER BY d ASC`, symbol, since)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []DayBar
	for rows.Next() {
		var b DayBar
		if err := rows.Scan(&b.Date, &b.High, &b.Low, &b.Close); err != nil {
			return nil, err
		}
		b.Date = b.Date.UTC()
		out = append(out, b)
	}
	return out, rows.Err()
}

// MarketSummary implements GET /api/v1/market/summary.
func (h *Handler) MarketSummary(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	now := time.Now().UTC()

	latest, err := h.latestPrices(ctx)
	if err != nil {
		h.Log.Error("summary_latest", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	prev, err := h.pricesAsOf(ctx, now.Add(-24*time.Hour))
	if err != nil {
		h.Log.Error("summary_prev", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	staleMin := storage.GetStaleMinutes(ctx, h.Pool, h.StaleMinutesDefault)

	out := map[string]any{"as_of": now}

	var lastUpdate *time.Time
	addSymbol := func(key, sym string) *latestPrice {
		p, ok := latest[sym]
		if !ok {
			out[key] = nil
			return nil
		}
		e := map[string]any{
			"value": p.Value, "currency": p.Currency, "unit": p.Unit,
			"observed_at": p.ObservedAt.UTC(),
			"source":      p.Source,
			"stale":       !markethours.AcceptablyFresh(sym, p.ObservedAt, now, staleMin, h.MarketOpen, h.MarketClose),
			"market_state": MarketState(sym, now, h.MarketOpen, h.MarketClose),
		}
		if pv, ok := prev[sym]; ok {
			e["change_24h_pct"] = fp(ChangePct(p.Value, pv))
		} else {
			e["change_24h_pct"] = nil
		}
		out[key] = e
		if lastUpdate == nil || p.ObservedAt.After(*lastUpdate) {
			t := p.ObservedAt.UTC()
			lastUpdate = &t
		}
		return &p
	}

	gold := addSymbol("current_18k", "IR_GOLD_18K")
	xau := addSymbol("xau_usd", "XAUUSD")
	usd := addSymbol("usd_irt", "USD_IRT")
	out["last_update"] = lastUpdate

	if xau != nil && usd != nil {
		theo := Theoretical18kIRT(xau.Value, usd.Value)
		out["theoretical_18k"] = fp(theo)
		if gold != nil {
			out["premium_pct"] = fp(PremiumPct(gold.Value, theo))
		} else {
			out["premium_pct"] = nil
		}
	} else {
		out["theoretical_18k"] = nil
		out["premium_pct"] = nil
	}

	// 30-day average premium from daily joined series.
	series, err := h.premiumSeries(ctx, 30)
	if err != nil {
		h.Log.Error("summary_premium30", "error", err)
	} else {
		var sum float64
		var n int
		for _, p := range series {
			if p.PremiumPct != nil {
				sum += *p.PremiumPct
				n++
			}
		}
		if n > 0 {
			out["premium_avg_30d"] = fp(sum / float64(n))
		} else {
			out["premium_avg_30d"] = nil
		}
	}

	// Live round-trip trading cost: the primary dealer's observed buy/sell
	// spread (Hamrah Gold reports both sides). Buying at the dealer's sell
	// price and later selling at their buy price costs exactly the spread,
	// so this is the honest cost basis for net-of-cost tilts. Null when the
	// spread has not been observed recently; the UI then falls back to a
	// conservative fixed assumption.
	var costPct *float64
	err = h.Pool.QueryRow(ctx, `
		SELECT (raw_payload->>'spread_pct')::float8
		FROM raw_observations
		WHERE provider_code = 'hamrahgold' AND raw_payload ? 'spread_pct'
		  AND observed_at > now() - interval '3 days'
		ORDER BY observed_at DESC LIMIT 1`).Scan(&costPct)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		h.Log.Error("summary_trading_cost", "error", err)
	}
	out["trading_cost_pct"] = costPct

	// Provider health.
	provRows, err := h.Pool.Query(ctx, `
		SELECT code, name, category, enabled, priority, last_success_at,
		       consecutive_failures, COALESCE(last_error, '')
		FROM data_providers ORDER BY category, priority`)
	if err != nil {
		h.Log.Error("summary_providers", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	providers := []map[string]any{}
	for provRows.Next() {
		var code, name, category, lastErr string
		var enabled bool
		var priority, fails int
		var lastSuccess *time.Time
		if err := provRows.Scan(&code, &name, &category, &enabled, &priority, &lastSuccess, &fails, &lastErr); err != nil {
			provRows.Close()
			h.Log.Error("summary_providers_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		providers = append(providers, map[string]any{
			"code": code, "name": name, "category": category, "enabled": enabled,
			"priority": priority, "last_success_at": lastSuccess,
			"consecutive_failures": fails, "last_error": lastErr,
			"healthy": enabled && fails == 0,
		})
	}
	provRows.Close()
	out["providers"] = providers

	// Latest signal row (full shape, matching /api/v1/signals/current).
	var sig struct {
		ID           int64
		GeneratedAt  time.Time
		Signal       string
		Score        int
		Confidence   float64
		Explanation  string
		Supporting   []byte
		Conflicting  []byte
		Risks        []byte
		Invalidation string
		ReviewAt     *time.Time
		DataFresh    bool
	}
	err = h.Pool.QueryRow(ctx, `
		SELECT id, generated_at, signal, score, confidence, explanation,
		       supporting, conflicting, risks, invalidation, review_at, data_fresh
		FROM signals ORDER BY generated_at DESC LIMIT 1`).
		Scan(&sig.ID, &sig.GeneratedAt, &sig.Signal, &sig.Score, &sig.Confidence,
			&sig.Explanation, &sig.Supporting, &sig.Conflicting, &sig.Risks,
			&sig.Invalidation, &sig.ReviewAt, &sig.DataFresh)
	switch {
	case errors.Is(err, pgx.ErrNoRows):
		out["signal"] = nil
	case err != nil:
		h.Log.Error("summary_signal", "error", err)
		out["signal"] = nil
	default:
		out["signal"] = map[string]any{
			"id": sig.ID, "generated_at": sig.GeneratedAt.UTC(), "signal": sig.Signal,
			"score": sig.Score, "confidence": sig.Confidence, "explanation": sig.Explanation,
			"supporting": json.RawMessage(sig.Supporting), "conflicting": json.RawMessage(sig.Conflicting),
			"risks": json.RawMessage(sig.Risks), "invalidation": sig.Invalidation,
			"review_at": sig.ReviewAt, "data_fresh": sig.DataFresh,
		}
	}

	httpserver.JSON(w, http.StatusOK, out)
}

type premiumPoint struct {
	Date           string   `json:"date"`
	Observed18k    *float64 `json:"observed_18k"`
	Theoretical18k *float64 `json:"theoretical_18k"`
	PremiumPct     *float64 `json:"premium_pct"`
}

func (h *Handler) premiumSeries(ctx context.Context, days int) ([]premiumPoint, error) {
	since := time.Now().UTC().AddDate(0, 0, -days).Truncate(24 * time.Hour)
	gold, err := h.dailySeries(ctx, "IR_GOLD_18K", since)
	if err != nil {
		return nil, err
	}
	xau, err := h.dailySeries(ctx, "XAUUSD", since)
	if err != nil {
		return nil, err
	}
	usd, err := h.dailySeries(ctx, "USD_IRT", since)
	if err != nil {
		return nil, err
	}
	return JoinPremiumSeries(gold, xau, usd), nil
}

// JoinPremiumSeries joins three daily series by date and computes theoretical
// price + premium per day. Pure function (unit tested).
func JoinPremiumSeries(gold, xau, usd []dailyPoint) []premiumPoint {
	xauBy := map[string]float64{}
	for _, p := range xau {
		xauBy[p.Date.Format("2006-01-02")] = p.Value
	}
	usdBy := map[string]float64{}
	for _, p := range usd {
		usdBy[p.Date.Format("2006-01-02")] = p.Value
	}
	out := []premiumPoint{}
	for _, g := range gold {
		d := g.Date.Format("2006-01-02")
		pt := premiumPoint{Date: d, Observed18k: fp(g.Value)}
		if xv, ok := xauBy[d]; ok {
			if uv, ok := usdBy[d]; ok {
				theo := Theoretical18kIRT(xv, uv)
				pt.Theoretical18k = fp(theo)
				pt.PremiumPct = fp(PremiumPct(g.Value, theo))
			}
		}
		out = append(out, pt)
	}
	return out
}

// Premium implements GET /api/v1/market/premium.
func (h *Handler) Premium(w http.ResponseWriter, r *http.Request) {
	days := intParam(r.URL.Query().Get("days"), 90, 1, 3650)
	series, err := h.premiumSeries(r.Context(), days)
	if err != nil {
		h.Log.Error("premium_series", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, map[string]any{"days": days, "items": series})
}

// Indicators implements GET /api/v1/market/indicators.
func (h *Handler) Indicators(w http.ResponseWriter, r *http.Request) {
	days := intParam(r.URL.Query().Get("days"), 90, 1, 3650)
	// Fetch extra history as indicator warm-up: SMA50/EMA26/ADX(2x14) need
	// lead-in, drawdown_pct looks back 90 days and corr_xau_20 needs 21
	// paired closes, so 110 extra days covers every window.
	since := time.Now().UTC().AddDate(0, 0, -(days + 110))
	ctx := r.Context()
	bars, err := h.dailyBars(ctx, "IR_GOLD_18K", since)
	if err != nil {
		h.Log.Error("indicators_bars", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	// XAUUSD daily closes for corr_xau_20 (rolling correlation of log returns).
	xau, err := h.dailySeries(ctx, "XAUUSD", since)
	if err != nil {
		h.Log.Error("indicators_xau", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, ComputeIndicators(bars, xau, days))
}

func intParam(s string, def, minV, maxV int) int {
	if s == "" {
		return def
	}
	n, err := strconv.Atoi(s)
	if err != nil {
		return def
	}
	return int(math.Min(float64(maxV), math.Max(float64(minV), float64(n))))
}
