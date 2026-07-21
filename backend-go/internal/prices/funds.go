package prices

import (
	"context"
	"encoding/json"
	"net/http"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// The TSE gold-fund stats feed (Addendum 8): every tse_funds collection
// round stores the raw TSETMC payload (volume, value, retail/institutional
// buyer-seller split) in raw_observations.raw_payload. This endpoint
// aggregates them into the dashboard's funds panel: latest snapshot per
// fund, today's averages, and the composite retail-flow history.

type fundSnapshot struct {
	Symbol     string    `json:"symbol"`
	Ticker     string    `json:"ticker"`
	Price      float64   `json:"price"` // IRT per unit
	Change24h  *float64  `json:"change_24h_pct"`
	ObservedAt time.Time `json:"observed_at"`

	Volume float64 `json:"volume"`
	Value  float64 `json:"value"` // rial, as quoted

	RetailBuyPct  *float64 `json:"retail_buy_pct"`  // Buy_I_Volume / tvol * 100
	RetailSellPct *float64 `json:"retail_sell_pct"` // Sell_I_Volume / tvol * 100
	// BuyerPower is the classic قدرت خریدار حقیقی: per-capita retail buy
	// volume over per-capita retail sell volume (> 1 = buyers more eager).
	BuyerPower *float64 `json:"buyer_power"`

	TodayAvgRetailBuyPct  *float64 `json:"today_avg_retail_buy_pct"`
	TodayAvgRetailSellPct *float64 `json:"today_avg_retail_sell_pct"`
	SnapshotsToday        int      `json:"snapshots_today"`
}

type fundPayload struct {
	L18        string  `json:"l18"`
	Tvol       float64 `json:"tvol"`
	Tval       float64 `json:"tval"`
	BuyIVol    float64 `json:"Buy_I_Volume"`
	SellIVol   float64 `json:"Sell_I_Volume"`
	BuyCountI  float64 `json:"Buy_CountI"`
	SellCountI float64 `json:"Sell_CountI"`
}

func pctOf(part, total float64) *float64 {
	if total <= 0 {
		return nil
	}
	v := part / total * 100.0
	return &v
}

// tehranMidnightUTC returns today's 00:00 Asia/Tehran expressed in UTC.
func tehranMidnightUTC(now time.Time) time.Time {
	tehran := time.FixedZone("Asia/Tehran", 3*3600+30*60)
	lt := now.In(tehran)
	midnight := time.Date(lt.Year(), lt.Month(), lt.Day(), 0, 0, 0, 0, tehran)
	return midnight.UTC()
}

func (h *Handler) fundSnapshots(ctx context.Context, now time.Time) ([]fundSnapshot, error) {
	dayStart := tehranMidnightUTC(now)
	since := dayStart.AddDate(0, 0, -7) // lead-in for the previous-close change
	rows, err := h.Pool.Query(ctx, `
		SELECT symbol, raw_value::float8, observed_at, raw_payload
		FROM raw_observations
		WHERE provider_code = 'tse_funds'
		  AND symbol LIKE 'IR_GOLD_FUND%' AND symbol <> 'IR_GOLD_FUND_FLOW'
		  AND observed_at >= $1
		ORDER BY symbol, observed_at ASC`, since)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	type snap struct {
		rial       float64
		observedAt time.Time
		payload    fundPayload
	}
	bySymbol := map[string][]snap{}
	for rows.Next() {
		var symbol string
		var rial float64
		var observedAt time.Time
		var raw []byte
		if err := rows.Scan(&symbol, &rial, &observedAt, &raw); err != nil {
			return nil, err
		}
		var payload fundPayload
		_ = json.Unmarshal(raw, &payload) // partial payloads leave zero fields
		bySymbol[symbol] = append(bySymbol[symbol], snap{rial, observedAt.UTC(), payload})
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}

	out := []fundSnapshot{}
	for symbol, snaps := range bySymbol {
		last := snaps[len(snaps)-1]
		fs := fundSnapshot{
			Symbol:     symbol,
			Ticker:     last.payload.L18,
			Price:      last.rial / 10.0, // rial -> toman
			ObservedAt: last.observedAt,
			Volume:     last.payload.Tvol,
			Value:      last.payload.Tval,
		}
		fs.RetailBuyPct = pctOf(last.payload.BuyIVol, last.payload.Tvol)
		fs.RetailSellPct = pctOf(last.payload.SellIVol, last.payload.Tvol)
		if last.payload.BuyCountI > 0 && last.payload.SellCountI > 0 && last.payload.SellIVol > 0 {
			power := (last.payload.BuyIVol / last.payload.BuyCountI) /
				(last.payload.SellIVol / last.payload.SellCountI)
			fs.BuyerPower = &power
		}

		// today's averages across the session's snapshots (quota-spaced)
		var buySum, sellSum float64
		var buyN, sellN, todayCount int
		var prevDayClose *float64
		for _, s := range snaps {
			if s.observedAt.Before(dayStart) {
				v := s.rial / 10.0
				prevDayClose = &v // last pre-today observation = previous close
				continue
			}
			todayCount++
			if p := pctOf(s.payload.BuyIVol, s.payload.Tvol); p != nil {
				buySum += *p
				buyN++
			}
			if p := pctOf(s.payload.SellIVol, s.payload.Tvol); p != nil {
				sellSum += *p
				sellN++
			}
		}
		fs.SnapshotsToday = todayCount
		if buyN > 0 {
			avg := buySum / float64(buyN)
			fs.TodayAvgRetailBuyPct = &avg
		}
		if sellN > 0 {
			avg := sellSum / float64(sellN)
			fs.TodayAvgRetailSellPct = &avg
		}
		if prevDayClose != nil && *prevDayClose > 0 {
			change := (fs.Price / *prevDayClose - 1.0) * 100.0
			fs.Change24h = &change
		}
		out = append(out, fs)
	}
	// stable order: by symbol
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j].Symbol < out[i].Symbol {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out, nil
}

// Funds implements GET /api/v1/market/funds — the gold-fund stats panel.
func (h *Handler) Funds(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	funds, err := h.fundSnapshots(r.Context(), now)
	if err != nil {
		h.Log.Error("funds_snapshots", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	// composite retail net flow: latest value + daily history for the chart
	var flowCurrent *float64
	var flowValue float64
	var flowObserved time.Time
	if err := h.Pool.QueryRow(r.Context(), `
		SELECT value::float8, observed_at FROM prices
		WHERE symbol = 'IR_GOLD_FUND_FLOW' AND quality = 'ok'
		ORDER BY observed_at DESC LIMIT 1`).Scan(&flowValue, &flowObserved); err == nil {
		flowCurrent = &flowValue
	}
	flowHistory := []map[string]any{}
	frows, err := h.Pool.Query(r.Context(), `
		SELECT date_trunc('day', observed_at) AS d,
		       (array_agg(value ORDER BY observed_at DESC))[1]::float8
		FROM prices
		WHERE symbol = 'IR_GOLD_FUND_FLOW' AND quality = 'ok'
		  AND observed_at >= $1
		GROUP BY d ORDER BY d ASC`, now.AddDate(0, 0, -30))
	if err == nil {
		defer frows.Close()
		for frows.Next() {
			var day time.Time
			var value float64
			if frows.Scan(&day, &value) != nil {
				break
			}
			flowHistory = append(flowHistory, map[string]any{
				"date":     day.UTC().Format("2006-01-02"),
				"flow_pct": value,
			})
		}
	}

	// the TSE calendar is keyed off the symbol prefix inside markethours;
	// the Tehran open/close args are ignored for fund symbols
	marketState := MarketState("IR_GOLD_FUND_AYAR", now, h.MarketOpen, h.MarketClose)
	httpserver.JSON(w, http.StatusOK, map[string]any{
		"funds":        funds,
		"flow_pct":     flowCurrent,
		"flow_history": flowHistory,
		"market_state": marketState,
		"as_of":        now,
	})
}
