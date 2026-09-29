package bourse

// The حقیقی/حقوقی money flow: who was buying and who was selling.
//
// TSETMC publishes, per share per session, how much individuals (حقیقی) and
// institutions (حقوقی) bought and sold, and how many of each did. The figures
// the Tehran market reads from it are all arithmetic on those twelve numbers:
//
//	net individual flow   buy_I_value - sell_I_value. Positive means
//	                      individuals bought more than they sold — institutions
//	                      were net sellers to them. (ورود پول حقیقی)
//	per-capita buy/sell   value / count for individuals: the average ticket.
//	buyer power           per-capita buy / per-capita sell. Above 1, the average
//	                      individual buyer spent more than the average
//	                      individual seller received. (قدرت خریدار)
//
// They DESCRIBE a session. None of them is a forecast, and the page says so:
// a strong buyer-power reading is a statement about ticket sizes, not about
// tomorrow's price.
//
// Every session is checked before it counts. Two identities must hold: what
// was bought must equal what was sold (every trade has both sides), and the
// total must equal the traded value on 0028's bar for that session — an
// independent TSETMC endpoint. On production (2026-09-29) the first fails on
// 0-3 sessions per share and the second on 1-7, out of ~3,000 each. A session
// that fails either is shown and flagged, and left out of every aggregate.

import (
	"context"
	"fmt"
	"math"
	"net/http"
	"sort"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// FlowSession is one session's flow, converted to TOMAN.
type FlowSession struct {
	Day                                              time.Time
	BuyICount, BuyNCount, SellICount, SellNCount     int64
	BuyIVolume, BuyNVolume, SellIVolume, SellNVolume float64
	BuyIValue, BuyNValue, SellIValue, SellNValue     float64
	// BarValue is equity_bars.value for the same session, in toman; nil when no
	// bar exists for it.
	BarValue *float64
}

// The two identity tolerances. Relative, because the values run from 10^6 to
// 10^13 toman. Buy and sell totals are the same trades counted from both sides
// and agree EXACTLY on 3,845 of فولاد's 3,848 sessions, so 0.1% only absorbs
// rounding; the bar is a different endpoint and 1% is its tolerance.
const (
	identityTolerance = 0.001
	barTolerance      = 0.01
)

// Check is whether a session's identities hold, and why not when they do not.
func (f FlowSession) Check() (bool, string) {
	buy := f.BuyIValue + f.BuyNValue
	sell := f.SellIValue + f.SellNValue
	if buy <= 0 && sell <= 0 {
		return false, "no traded value on either side"
	}
	if math.Abs(buy-sell) > identityTolerance*math.Max(buy, sell) {
		return false, fmt.Sprintf("buy total %.0f and sell total %.0f toman disagree; every "+
			"trade has a buyer and a seller, so one side of this row is wrong", buy, sell)
	}
	if f.BarValue == nil {
		return false, "no daily bar exists for this session to cross-check the total against"
	}
	if *f.BarValue <= 0 || math.Abs(buy/(*f.BarValue)-1) > barTolerance {
		return false, fmt.Sprintf("the flow total %.0f toman disagrees with the session's "+
			"traded value on the daily bar (%.0f toman)", buy, *f.BarValue)
	}
	return true, ""
}

func ratio(num, den float64) *float64 {
	if den <= 0 {
		return nil
	}
	return fv(num / den)
}

// FlowDay is one session as a client sees it.
type FlowDay struct {
	Date               string   `json:"date"`
	TotalValueToman    *float64 `json:"total_value_toman"`
	NetIndividualToman *float64 `json:"net_individual_toman"`
	// The individuals' share of what was bought and of what was sold.
	IndividualBuySharePct  *float64 `json:"individual_buy_share_pct"`
	IndividualSellSharePct *float64 `json:"individual_sell_share_pct"`
	PerCapitaBuyToman      *float64 `json:"per_capita_buy_toman"`
	PerCapitaSellToman     *float64 `json:"per_capita_sell_toman"`
	BuyerPower             *float64 `json:"buyer_power"`
	Consistent             bool     `json:"consistent"`
	ExcludedReason         string   `json:"excluded_reason,omitempty"`
}

// BuildFlowDays projects sessions onto the contract. Pure (unit tested).
func BuildFlowDays(sessions []FlowSession) []FlowDay {
	out := make([]FlowDay, 0, len(sessions))
	for _, f := range sessions {
		ok, why := f.Check()
		d := FlowDay{Date: dayString(f.Day), Consistent: ok, ExcludedReason: why}
		total := f.BuyIValue + f.BuyNValue
		d.TotalValueToman = fv(total)
		d.NetIndividualToman = fv(f.BuyIValue - f.SellIValue)
		if total > 0 {
			d.IndividualBuySharePct = fp(f.BuyIValue / total * 100)
		}
		if sell := f.SellIValue + f.SellNValue; sell > 0 {
			d.IndividualSellSharePct = fp(f.SellIValue / sell * 100)
		}
		pb := ratio(f.BuyIValue, float64(f.BuyICount))
		ps := ratio(f.SellIValue, float64(f.SellICount))
		d.PerCapitaBuyToman, d.PerCapitaSellToman = pb, ps
		if pb != nil && ps != nil && *ps > 0 {
			d.BuyerPower = fp(*pb / *ps)
		}
		out = append(out, d)
	}
	return out
}

// FlowSummary aggregates the CONSISTENT sessions of a window.
type FlowSummary struct {
	From       *string `json:"from"`
	To         *string `json:"to"`
	Sessions   int     `json:"sessions"`
	Consistent int     `json:"consistent"`
	Excluded   int     `json:"excluded"`
	// Sum of net individual flow, and that sum as a share of what traded.
	NetIndividualToman      *float64 `json:"net_individual_toman"`
	NetIndividualPctOfValue *float64 `json:"net_individual_pct_of_value"`
	TotalValueToman         *float64 `json:"total_value_toman"`
	IndividualBuySharePct   *float64 `json:"individual_buy_share_pct"`
	IndividualSellSharePct  *float64 `json:"individual_sell_share_pct"`
	// Buyer power over the window is the ratio of the AGGREGATE tickets —
	// total individual buying over total buyer count, against the same for
	// sellers — not an average of daily ratios, which one thin session with
	// three traders could dominate. A count is traders per SESSION, so a
	// trader active on five days is five here; the note says "per trader-session".
	BuyerPower      *float64 `json:"buyer_power"`
	InflowSessions  int      `json:"inflow_sessions"`
	OutflowSessions int      `json:"outflow_sessions"`
}

// SummarizeFlows is the window's aggregate. Pure (unit tested).
func SummarizeFlows(sessions []FlowSession) FlowSummary {
	s := FlowSummary{Sessions: len(sessions)}
	if len(sessions) > 0 {
		s.From, s.To = dayPtr(&sessions[0].Day), dayPtr(&sessions[len(sessions)-1].Day)
	}
	var net, total, buyI, sellI, sellTotal float64
	var buyCount, sellCount int64
	for _, f := range sessions {
		if ok, _ := f.Check(); !ok {
			s.Excluded++
			continue
		}
		s.Consistent++
		n := f.BuyIValue - f.SellIValue
		net += n
		switch {
		case n > 0:
			s.InflowSessions++
		case n < 0:
			s.OutflowSessions++
		}
		total += f.BuyIValue + f.BuyNValue
		sellTotal += f.SellIValue + f.SellNValue
		buyI += f.BuyIValue
		sellI += f.SellIValue
		buyCount += f.BuyICount
		sellCount += f.SellICount
	}
	if s.Consistent == 0 {
		return s
	}
	s.NetIndividualToman = fv(net)
	s.TotalValueToman = fv(total)
	if total > 0 {
		s.NetIndividualPctOfValue = fp(net / total * 100)
		s.IndividualBuySharePct = fp(buyI / total * 100)
	}
	if sellTotal > 0 {
		s.IndividualSellSharePct = fp(sellI / sellTotal * 100)
	}
	if buyCount > 0 && sellCount > 0 && sellI > 0 {
		s.BuyerPower = fp((buyI / float64(buyCount)) / (sellI / float64(sellCount)))
	}
	return s
}

// flowSelect joins each flow session to its bar's traded value. RIALS in the
// database; converted to toman on the way out of the scan.
const flowSelect = `
	SELECT f.ins_code, f.trade_date,
	       f.buy_i_count, f.buy_n_count, f.sell_i_count, f.sell_n_count,
	       f.buy_i_volume, f.buy_n_volume, f.sell_i_volume, f.sell_n_volume,
	       f.buy_i_value, f.buy_n_value, f.sell_i_value, f.sell_n_value,
	       b.value
	FROM equity_client_flows f
	LEFT JOIN equity_bars b ON b.ins_code = f.ins_code AND b.trade_date = f.trade_date
	WHERE f.ins_code = ANY($1)
	  AND ($2::date IS NULL OR f.trade_date >= $2)
	  AND ($3::date IS NULL OR f.trade_date <= $3)
	ORDER BY f.ins_code, f.trade_date`

// LoadFlows reads the flow sessions of the given instruments, ascending, in
// toman. Exported for internal/equities' per-share view.
func LoadFlows(ctx context.Context, pool *pgxpool.Pool, insCodes []string,
	from, to *time.Time) (map[string][]FlowSession, error) {
	rows, err := pool.Query(ctx, flowSelect, insCodes, from, to)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][]FlowSession{}
	for rows.Next() {
		var code string
		var f FlowSession
		var bar *float64
		if err := rows.Scan(&code, &f.Day, &f.BuyICount, &f.BuyNCount, &f.SellICount,
			&f.SellNCount, &f.BuyIVolume, &f.BuyNVolume, &f.SellIVolume, &f.SellNVolume,
			&f.BuyIValue, &f.BuyNValue, &f.SellIValue, &f.SellNValue, &bar); err != nil {
			return nil, err
		}
		f.Day = dayFloor(f.Day)
		f.BuyIValue /= rialsPerToman
		f.BuyNValue /= rialsPerToman
		f.SellIValue /= rialsPerToman
		f.SellNValue /= rialsPerToman
		if bar != nil {
			v := *bar / rialsPerToman
			f.BarValue = &v
		}
		out[code] = append(out[code], f)
	}
	return out, rows.Err()
}

// The roster table's windows, in SESSIONS rather than calendar days — the
// Tehran market reads flow over the last 1, 5, 20 and 60 sessions.
var flowWindows = []int{1, 5, 20, 60}

// lastSessions is the newest n sessions of a series.
func lastSessions(s []FlowSession, n int) []FlowSession {
	if len(s) <= n {
		return s
	}
	return s[len(s)-n:]
}

type flowRosterItem struct {
	InsCode    string  `json:"ins_code"`
	Symbol     string  `json:"symbol"`
	NameFA     string  `json:"name_fa"`
	SectorCode string  `json:"sector_code"`
	SectorFA   string  `json:"sector_fa"`
	LastDate   *string `json:"last_date"`
	// Windows keyed "1", "5", "20", "60": the newest n stored sessions.
	Windows map[string]FlowSummary `json:"windows"`
}

type flowRosterResponse struct {
	Items []flowRosterItem `json:"items"`
	Count int              `json:"count"`
	// Roster is the same windows summed across every roster share. It is the
	// ROSTER, not the market: nineteen shares of some seven hundred listed.
	Roster  map[string]FlowSummary `json:"roster"`
	Notes   []string               `json:"notes"`
	DataAge DataAge                `json:"data_age"`
}

type rosterMember struct {
	InsCode, Symbol, NameFA, SectorCode, SectorFA string
}

// buildFlowRoster is the table. Pure (unit tested).
func buildFlowRoster(members []rosterMember, flows map[string][]FlowSession, now time.Time) flowRosterResponse {
	out := flowRosterResponse{Items: []flowRosterItem{}, Roster: map[string]FlowSummary{}}
	pooled := map[int][]FlowSession{}
	var newest *time.Time
	for _, m := range members {
		s := flows[m.InsCode]
		item := flowRosterItem{InsCode: m.InsCode, Symbol: m.Symbol, NameFA: m.NameFA,
			SectorCode: m.SectorCode, SectorFA: m.SectorFA, Windows: map[string]FlowSummary{}}
		if len(s) > 0 {
			d := s[len(s)-1].Day
			item.LastDate = dayPtr(&d)
			if newest == nil || d.After(*newest) {
				newest = &d
			}
		}
		for _, n := range flowWindows {
			w := lastSessions(s, n)
			item.Windows[fmt.Sprint(n)] = SummarizeFlows(w)
			pooled[n] = append(pooled[n], w...)
		}
		out.Items = append(out.Items, item)
	}
	for _, n := range flowWindows {
		p := pooled[n]
		sort.SliceStable(p, func(i, j int) bool { return p[i].Day.Before(p[j].Day) })
		sum := SummarizeFlows(p)
		// A pooled sum of sessions from different shares is not "sessions"; the
		// counts are share-sessions and are labelled by the note.
		out.Roster[fmt.Sprint(n)] = sum
	}
	out.Count = len(out.Items)
	out.DataAge = buildDataAge(newest, now)
	out.Notes = []string{
		"Net individual flow is individuals' buying minus their selling, in toman: positive " +
			"means institutions were net sellers to individuals. It describes who traded, not " +
			"what the price will do.",
		"Buyer power is the average individual buy ticket over the average individual sell " +
			"ticket, per trader-session. Above 1, buyers spent more per head than sellers " +
			"received. A ratio of ticket sizes, not a forecast.",
		"Every session is checked first: buying must equal selling, and the total must equal " +
			"the traded value on that session's daily bar. A session failing either is left " +
			"out of every sum and counted in `excluded`.",
		"The roster row sums the roster's shares — nineteen of some seven hundred listed — " +
			"and is not the market's flow. The whole market's, where its ingest has run, is " +
			"summed by sector at /api/v1/bourse/sector-flows.",
	}
	return out
}

// Flows implements GET /api/v1/bourse/flows — the money-flow table for the roster.
func (h *Handler) Flows(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	rows, err := h.Pool.Query(ctx, `
		SELECT ins_code, symbol_fa, name_fa, sector_code, sector_fa
		FROM equity_instruments WHERE enabled ORDER BY symbol_fa`)
	if err != nil {
		h.Log.Error("bourse_flows_roster", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	var members []rosterMember
	var codes []string
	for rows.Next() {
		var m rosterMember
		if err := rows.Scan(&m.InsCode, &m.Symbol, &m.NameFA, &m.SectorCode, &m.SectorFA); err != nil {
			rows.Close()
			h.Log.Error("bourse_flows_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		members = append(members, m)
		codes = append(codes, m.InsCode)
	}
	rows.Close()
	// Sixty sessions is about three months; 200 calendar days covers them
	// through a long closure (the 2026 one was twelve weeks) with room left.
	from := dayFloor(time.Now()).AddDate(0, 0, -200)
	flows, err := LoadFlows(ctx, h.Pool, codes, &from, nil)
	if err != nil {
		h.Log.Error("bourse_flows_load", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, buildFlowRoster(members, flows, time.Now()))
}
