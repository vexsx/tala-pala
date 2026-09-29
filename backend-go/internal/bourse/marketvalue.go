package bourse

// GET /api/v1/bourse/market-value — the total value of the bourse and the
// Farabourse per session, in toman and in dollars.
//
// NOT A RETURN SERIES. A market's total value moves when prices move AND when
// a company lists, delists or issues shares, and TSETMC's series shows both:
// on 2025-03-26 the bourse's value rose 9.14% while TEDPIX rose 0.93%. The
// index is the answer to "what did the market return?"; this is the answer to
// "how big is it?", and the page must not let one be read as the other.

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/relvalue"
)

// rialsPerToman is the one place this package divides by ten: TSETMC serves
// market value in RIALS, and IRT is canonical everywhere else in the system.
const rialsPerToman = 10.0

type marketValueRow struct {
	Market string
	Day    time.Time
	Rials  float64
}

type marketValuePoint struct {
	Date            string   `json:"date"`
	BourseToman     *float64 `json:"bourse_toman"`
	FarabourseToman *float64 `json:"farabourse_toman"`
	// Total only on a session BOTH markets carry: a sum with one side missing
	// would read as the market halving.
	TotalToman    *float64 `json:"total_toman"`
	BourseUSD     *float64 `json:"bourse_usd"`
	FarabourseUSD *float64 `json:"farabourse_usd"`
	TotalUSD      *float64 `json:"total_usd"`
}

type marketValueSummary struct {
	From *string `json:"from"`
	To   *string `json:"to"`
	// Changes over the window for the TOTAL, where both ends carry one.
	TotalTomanChangePct *float64          `json:"total_toman_change_pct"`
	TotalUSDChangePct   *float64          `json:"total_usd_change_pct"`
	Latest              *marketValuePoint `json:"latest"`
}

type marketValueResponse struct {
	Window     windowItem         `json:"window"`
	Points     []marketValuePoint `json:"points"`
	Summary    marketValueSummary `json:"summary"`
	Conversion *conversionItem    `json:"conversion,omitempty"`
	Notes      []string           `json:"notes"`
	DataAge    DataAge            `json:"data_age"`
}

// buildMarketValue joins the two markets by session and attaches the dollar
// figures. `usd` maps a day to one toman's worth in dollars' — i.e. the result
// of converting 1 toman — so the dollar value is value_toman * usd[day]. Pure
// (unit tested).
func buildMarketValue(rows []marketValueRow, usd map[time.Time]float64) ([]marketValuePoint, marketValueSummary) {
	byDay := map[time.Time]*marketValuePoint{}
	var days []time.Time
	for _, r := range rows {
		p, ok := byDay[r.Day]
		if !ok {
			p = &marketValuePoint{Date: dayString(r.Day)}
			byDay[r.Day] = p
			days = append(days, r.Day)
		}
		toman := r.Rials / rialsPerToman
		rate, hasRate := usd[r.Day]
		switch r.Market {
		case "bourse":
			p.BourseToman = fv(toman)
			if hasRate {
				p.BourseUSD = fv(toman * rate)
			}
		case "farabourse":
			p.FarabourseToman = fv(toman)
			if hasRate {
				p.FarabourseUSD = fv(toman * rate)
			}
		}
	}
	sort.Slice(days, func(i, j int) bool { return days[i].Before(days[j]) })
	out := make([]marketValuePoint, 0, len(days))
	for _, d := range days {
		p := byDay[d]
		if p.BourseToman != nil && p.FarabourseToman != nil {
			p.TotalToman = fv(*p.BourseToman + *p.FarabourseToman)
			if p.BourseUSD != nil && p.FarabourseUSD != nil {
				p.TotalUSD = fv(*p.BourseUSD + *p.FarabourseUSD)
			}
		}
		out = append(out, *p)
	}
	sum := marketValueSummary{}
	if len(out) == 0 {
		return out, sum
	}
	sum.From, sum.To = &out[0].Date, &out[len(out)-1].Date
	latest := out[len(out)-1]
	sum.Latest = &latest
	first := firstWith(out, func(p marketValuePoint) *float64 { return p.TotalToman })
	last := lastWith(out, func(p marketValuePoint) *float64 { return p.TotalToman })
	if first != nil && last != nil && first != last {
		sum.TotalTomanChangePct = fp((*last.TotalToman / *first.TotalToman - 1) * 100)
	}
	fu := firstWith(out, func(p marketValuePoint) *float64 { return p.TotalUSD })
	lu := lastWith(out, func(p marketValuePoint) *float64 { return p.TotalUSD })
	if fu != nil && lu != nil && fu != lu {
		sum.TotalUSDChangePct = fp((*lu.TotalUSD / *fu.TotalUSD - 1) * 100)
	}
	return out, sum
}

func firstWith(s []marketValuePoint, f func(marketValuePoint) *float64) *marketValuePoint {
	for i := range s {
		if f(s[i]) != nil {
			return &s[i]
		}
	}
	return nil
}

func lastWith(s []marketValuePoint, f func(marketValuePoint) *float64) *marketValuePoint {
	for i := len(s) - 1; i >= 0; i-- {
		if f(s[i]) != nil {
			return &s[i]
		}
	}
	return nil
}

func (h *Handler) loadMarketValues(ctx context.Context, from *time.Time, to time.Time) ([]marketValueRow, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT market, trade_date, market_cap FROM market_values
		WHERE ($1::date IS NULL OR trade_date >= $1) AND trade_date <= $2
		ORDER BY trade_date, market`, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []marketValueRow
	for rows.Next() {
		var r marketValueRow
		if err := rows.Scan(&r.Market, &r.Day, &r.Rials); err != nil {
			return nil, err
		}
		r.Day = dayFloor(r.Day)
		out = append(out, r)
	}
	return out, rows.Err()
}

// MarketValue implements GET /api/v1/bourse/market-value?period=&from=&to=.
func (h *Handler) MarketValue(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var newest *time.Time
	if err := h.Pool.QueryRow(ctx, `SELECT max(trade_date) FROM market_values`).Scan(&newest); err != nil {
		h.Log.Error("bourse_mv_newest", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	now := time.Now()
	if newest == nil {
		httpserver.JSON(w, http.StatusOK, marketValueResponse{Points: []marketValuePoint{},
			Notes: []string{"No market value is stored yet."}, DataAge: buildDataAge(nil, now)})
		return
	}
	win, perr := parseWindow(r.URL.Query(), *newest)
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	rows, err := h.loadMarketValues(ctx, win.From, win.To)
	if err != nil {
		h.Log.Error("bourse_mv_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	conv, err := relvalue.NewConverter(ctx, h.Pool, now)
	if err != nil {
		h.Log.Error("bourse_mv_converter", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	// One toman converted per session day gives the rate as a multiplier; the
	// converter applies the carry-forward rule and drops a day with no prior
	// quote, so a missing multiplier is a missing dollar figure, never a zero.
	seen := map[time.Time]bool{}
	var unit []relvalue.Point
	for _, row := range rows {
		if !seen[row.Day] {
			seen[row.Day] = true
			unit = append(unit, relvalue.Point{Day: row.Day, Value: 1})
		}
	}
	res := conv.ConvertTomanSeries(unit, relvalue.NumeraireUSD)
	usd := make(map[time.Time]float64, len(res.Points))
	for _, p := range res.Points {
		usd[dayFloor(p.Day)] = p.Value
	}
	points, sum := buildMarketValue(rows, usd)
	notes := []string{
		"Total market value as TSETMC publishes it, converted from rials to toman. It is " +
			"not a return: it rises and falls with listings, delistings and share issues as " +
			"well as with prices — on 2025-03-26 the bourse's value rose 9.14% while TEDPIX " +
			"rose 0.93%. Use the index for what the market returned.",
		"The dollar figure divides each session's value by the free-market dollar rate in " +
			"force that day (USD_IRT, carried forward, never backward).",
	}
	resp := marketValueResponse{Window: win.item(), Points: points, Summary: sum, Notes: notes,
		Conversion: &conversionItem{Chain: res.Chain, Unit: res.Unit,
			CarriedForward: res.CarriedForward, DroppedNoPriorQuote: res.DroppedNoPriorQuote,
			DroppedNonPositiveQuote: res.DroppedNonPositiveQuote},
		DataAge: buildDataAge(newest, now)}
	if res.MissingSeries != "" {
		resp.Notes = append(resp.Notes, fmt.Sprintf(
			"No dollar figures: %s is not stored in this deployment.", res.MissingSeries))
	}
	httpserver.JSON(w, http.StatusOK, resp)
}
