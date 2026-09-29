package equities

// A share against its market: its حقیقی/حقوقی money flow, and its adjusted
// return beside TEDPIX and beside its own sector's index.
//
//	GET /api/v1/stocks/{symbol}/flows?period=
//	GET /api/v1/stocks/{symbol}/relative?period=
//
// The index series and the flow arithmetic are internal/bourse's; this file
// resolves the symbol, applies this package's gate (an adjusted series only
// when its verdict is validated) and pairs the share's sessions with the
// index's.
//
// HOW A HALT IS PAIRED. A halted session carries the reference price in
// final_close and no trade, so it is not a return (see screen.go). A return is
// therefore taken from one TRADED session to the next, and the index return
// it is compared with is taken over EXACTLY the same span — the index value in
// force on each of the two days. A share halted for three weeks contributes
// one observation spanning three weeks on both sides, never twenty zeros on
// its own side against twenty real moves on the market's. That is the whole
// difference between a beta and a number that merely looks like one for a
// market where halts are routine (شپنا carries 879 of them).

import (
	"errors"
	"fmt"
	"math"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/bourse"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// linkPeriods is the window vocabulary for both routes.
var linkPeriods = []string{"1m", "3m", "6m", "1y", "3y", "5y", "max"}

func linkPeriodStart(period string, end time.Time) (*time.Time, bool) {
	var t time.Time
	switch period {
	case "1m":
		t = end.AddDate(0, -1, 0)
	case "3m":
		t = end.AddDate(0, -3, 0)
	case "6m":
		t = end.AddDate(0, -6, 0)
	case "1y":
		t = end.AddDate(-1, 0, 0)
	case "3y":
		t = end.AddDate(-3, 0, 0)
	case "5y":
		t = end.AddDate(-5, 0, 0)
	case "max":
		return nil, true
	default:
		return nil, false
	}
	return &t, true
}

type linkWindow struct {
	Period string  `json:"period"`
	From   *string `json:"from"`
	To     string  `json:"to"`
}

func parseLinkPeriod(q string, def string) (string, *paramError) {
	p := strings.ToLower(strings.TrimSpace(q))
	if p == "" {
		p = def
	}
	if !containsString(linkPeriods, p) {
		return "", badParam(fmt.Sprintf("period must be one of %v, got %q", linkPeriods, p),
			map[string]any{"period": p, "supported": linkPeriods})
	}
	return p, nil
}

// --- money flow --------------------------------------------------------------

type cumulativePoint struct {
	Date  string   `json:"date"`
	Value *float64 `json:"value"`
}

type stockFlowsResponse struct {
	Symbol  string                        `json:"symbol"`
	InsCode string                        `json:"ins_code"`
	NameFA  string                        `json:"name_fa"`
	Window  linkWindow                    `json:"window"`
	Days    []bourse.FlowDay              `json:"days"`
	Summary bourse.FlowSummary            `json:"summary"`
	Windows map[string]bourse.FlowSummary `json:"windows"`
	// CumulativeNetIndividual is the running sum of net individual flow over
	// the CONSISTENT sessions, in toman — the line the page draws.
	CumulativeNetIndividual []cumulativePoint `json:"cumulative_net_individual"`
	Notes                   []string          `json:"notes"`
	DataAge                 dataAgeBlock      `json:"data_age"`
}

// buildStockFlows is the per-share view. Pure (unit tested).
func buildStockFlows(meta stockRow, all []bourse.FlowSession, from *time.Time,
	period string, now time.Time) stockFlowsResponse {
	out := stockFlowsResponse{Symbol: meta.SymbolFA, InsCode: meta.InsCode, NameFA: meta.NameFA,
		Days: []bourse.FlowDay{}, Windows: map[string]bourse.FlowSummary{},
		CumulativeNetIndividual: []cumulativePoint{}}
	var newest *time.Time
	if len(all) > 0 {
		d := all[len(all)-1].Day
		newest = &d
	}
	out.DataAge = buildDataAge(newest, now)
	if newest == nil {
		out.Window = linkWindow{Period: period}
		out.Notes = []string{"No money-flow session is stored for this share yet."}
		return out
	}
	lo := 0
	if from != nil {
		lo = sort.Search(len(all), func(i int) bool { return !all[i].Day.Before(*from) })
	}
	win := all[lo:]
	out.Window = linkWindow{Period: period, From: datePtr2(from), To: dateString(*newest)}
	out.Days = bourse.BuildFlowDays(win)
	out.Summary = bourse.SummarizeFlows(win)
	for _, n := range []int{5, 20, 60} {
		w := all
		if len(w) > n {
			w = w[len(w)-n:]
		}
		out.Windows[fmt.Sprint(n)] = bourse.SummarizeFlows(w)
	}
	running := 0.0
	for _, d := range out.Days {
		if !d.Consistent || d.NetIndividualToman == nil {
			continue
		}
		running += *d.NetIndividualToman
		v := running
		out.CumulativeNetIndividual = append(out.CumulativeNetIndividual,
			cumulativePoint{Date: d.Date, Value: &v})
	}
	out.Notes = []string{
		"Net individual flow is individuals' buying minus their selling, in toman: positive " +
			"means institutions were net sellers to individuals. Buyer power is the average " +
			"individual buy ticket over the average sell ticket. Both describe who traded; " +
			"neither is a forecast.",
		"Each session is checked against two identities — buying equals selling, and the " +
			"total equals the traded value on that session's daily bar — and a session failing " +
			"either is shown with its reason and left out of every sum.",
	}
	if out.Summary.Excluded > 0 {
		out.Notes = append(out.Notes, fmt.Sprintf(
			"%d session(s) in this window failed a check and are excluded.", out.Summary.Excluded))
	}
	return out
}

func datePtr2(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := dateString(*t)
	return &s
}

// Flows implements GET /api/v1/stocks/{symbol}/flows?period=.
func (h *Handler) Flows(w http.ResponseWriter, r *http.Request) {
	symbol := foldSymbol(chi.URLParam(r, "symbol"))
	period, perr := parseLinkPeriod(r.URL.Query().Get("period"), "6m")
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	ctx := r.Context()
	meta, err := scanStock(h.Pool.QueryRow(ctx, stockBySymbolSelect, symbol))
	if errors.Is(err, pgx.ErrNoRows) {
		unknownSymbol(w, symbol)
		return
	}
	if err != nil {
		h.Log.Error("flows_symbol", "error", err, "symbol", symbol)
		httpserver.Internal(w, "database error")
		return
	}
	flows, err := bourse.LoadFlows(ctx, h.Pool, []string{meta.InsCode}, nil, nil)
	if err != nil {
		h.Log.Error("flows_load", "error", err, "symbol", symbol)
		httpserver.Internal(w, "database error")
		return
	}
	all := flows[meta.InsCode]
	var from *time.Time
	if len(all) > 0 {
		from, _ = linkPeriodStart(period, all[len(all)-1].Day)
	}
	httpserver.JSON(w, http.StatusOK, buildStockFlows(meta, all, from, period, time.Now()))
}

// --- relative performance ----------------------------------------------------

// relSession is one stored bar reduced to what the comparison needs.
type relSession struct {
	Day      time.Time
	Adjusted float64
	Traded   bool
}

type relPoint struct {
	Date string `json:"date"`
	// Stock is null on a session the share did not trade: a halt is a gap.
	Stock  *float64 `json:"stock"`
	Market *float64 `json:"market"`
	Sector *float64 `json:"sector"`
}

type betaBlock struct {
	Beta        *float64 `json:"beta"`
	Correlation *float64 `json:"correlation"`
	// Pairs is how many traded-to-traded spans were compared; LongSpans how
	// many of them crossed more than seven calendar days (a halt), each of
	// which is ONE observation on both sides.
	Pairs     int    `json:"pairs"`
	LongSpans int    `json:"long_spans"`
	Reason    string `json:"reason,omitempty"`
}

type benchmarkItem struct {
	Role   string            `json:"role"` // market | sector
	Index  *bourse.IndexInfo `json:"index"`
	Reason string            `json:"reason,omitempty"`
}

type relativeSummary struct {
	From             *string    `json:"from"`
	To               *string    `json:"to"`
	StockReturnPct   *float64   `json:"stock_return_pct"`
	MarketReturnPct  *float64   `json:"market_return_pct"`
	SectorReturnPct  *float64   `json:"sector_return_pct"`
	ExcessVsMarketPP *float64   `json:"excess_vs_market_pp"`
	ExcessVsSectorPP *float64   `json:"excess_vs_sector_pp"`
	VsMarket         betaBlock  `json:"vs_market"`
	VsSector         *betaBlock `json:"vs_sector"`
	TradedSessions   int        `json:"traded_sessions"`
	HaltedSessions   int        `json:"halted_sessions"`
}

type relativeResponse struct {
	Symbol     string          `json:"symbol"`
	InsCode    string          `json:"ins_code"`
	NameFA     string          `json:"name_fa"`
	SectorCode string          `json:"sector_code"`
	SectorFA   string          `json:"sector_fa"`
	Window     linkWindow      `json:"window"`
	Benchmarks []benchmarkItem `json:"benchmarks"`
	Points     []relPoint      `json:"points"`
	Summary    relativeSummary `json:"summary"`
	Notes      []string        `json:"notes"`
	DataAge    dataAgeBlock    `json:"data_age"`
}

// minBetaPairs: below twenty matched spans a regression slope is noise with a
// decimal point, and it is withheld with the count rather than printed.
const minBetaPairs = 20

// matchedBeta regresses the share's traded-to-traded returns on the index's
// returns over the SAME spans. Pure (unit tested).
func matchedBeta(traded []relSession, index []bourse.IndexPoint) betaBlock {
	var rs, rm []float64
	out := betaBlock{}
	for i := 1; i < len(traded); i++ {
		a, b := traded[i-1], traded[i]
		ia, okA := bourse.ValueInForce(index, a.Day)
		ib, okB := bourse.ValueInForce(index, b.Day)
		if !okA || !okB || ia.Value <= 0 || a.Adjusted <= 0 {
			continue
		}
		rs = append(rs, b.Adjusted/a.Adjusted-1)
		rm = append(rm, ib.Value/ia.Value-1)
		if b.Day.Sub(a.Day) > 7*24*time.Hour {
			out.LongSpans++
		}
	}
	out.Pairs = len(rs)
	if len(rs) < minBetaPairs {
		out.Reason = fmt.Sprintf(
			"a beta needs at least %d traded-to-traded spans and this window has %d",
			minBetaPairs, len(rs))
		return out
	}
	mm, ms := 0.0, 0.0
	for i := range rs {
		mm += rm[i]
		ms += rs[i]
	}
	mm /= float64(len(rm))
	ms /= float64(len(rs))
	var cov, vm float64
	for i := range rs {
		cov += (rs[i] - ms) * (rm[i] - mm)
		vm += (rm[i] - mm) * (rm[i] - mm)
	}
	if vm == 0 {
		out.Reason = "the index did not move over these spans, so there is nothing to regress on"
		return out
	}
	b := cov / vm
	if !math.IsNaN(b) && !math.IsInf(b, 0) {
		r := math.Round(b*1e4) / 1e4
		out.Beta = &r
	}
	out.Correlation = bourse.Pearson(rs, rm)
	return out
}

// buildRelative pairs a share's sessions with its benchmarks. `sessions` is
// every stored bar ascending, `from` the window start (nil = all), `to` its
// end. Pure (unit tested).
func buildRelative(sessions []relSession, market, sector []bourse.IndexPoint,
	from *time.Time, to time.Time) ([]relPoint, relativeSummary) {
	sum := relativeSummary{}
	var traded []relSession
	baseIdx := -1
	for i, s := range sessions {
		if s.Day.After(to) {
			break
		}
		if !s.Traded || s.Adjusted <= 0 {
			continue
		}
		if from != nil && s.Day.Before(*from) {
			baseIdx = i // the traded session in force when the window opened
			continue
		}
		traded = append(traded, s)
	}
	if baseIdx >= 0 && (len(traded) == 0 || traded[0].Day.After(*from)) {
		traded = append([]relSession{sessions[baseIdx]}, traded...)
	}
	points := []relPoint{}
	if len(traded) == 0 {
		return points, sum
	}
	t0, tn := traded[0], traded[len(traded)-1]
	sum.From, sum.To = datePtr2(&t0.Day), datePtr2(&tn.Day)
	for _, s := range sessions {
		if s.Day.Before(t0.Day) || s.Day.After(tn.Day) {
			continue
		}
		if s.Traded {
			sum.TradedSessions++
		} else {
			sum.HaltedSessions++
		}
	}

	m0, okM := bourse.ValueInForce(market, t0.Day)
	var s0 bourse.IndexPoint
	okS := false
	if len(sector) > 0 {
		s0, okS = bourse.ValueInForce(sector, t0.Day)
	}

	// The x-axis is every market session in the span plus every traded
	// session of the share, so the market line is continuous and the share's
	// halts show as gaps.
	adjByDay := map[time.Time]float64{}
	for _, s := range traded {
		adjByDay[s.Day] = s.Adjusted
	}
	days := map[time.Time]bool{}
	for _, p := range market {
		if !p.Day.Before(t0.Day) && !p.Day.After(tn.Day) {
			days[p.Day] = true
		}
	}
	for d := range adjByDay {
		days[d] = true
	}
	ordered := make([]time.Time, 0, len(days))
	for d := range days {
		ordered = append(ordered, d)
	}
	sort.Slice(ordered, func(i, j int) bool { return ordered[i].Before(ordered[j]) })
	for _, d := range ordered {
		p := relPoint{Date: dateString(d)}
		if v, ok := adjByDay[d]; ok {
			p.Stock = fp(v / t0.Adjusted * 100)
		}
		if okM {
			if m, ok := bourse.ValueInForce(market, d); ok && m0.Value > 0 {
				p.Market = fp(m.Value / m0.Value * 100)
			}
		}
		if okS {
			if s, ok := bourse.ValueInForce(sector, d); ok && s0.Value > 0 {
				p.Sector = fp(s.Value / s0.Value * 100)
			}
		}
		points = append(points, p)
	}

	// Returns over the share's own traded span, on every side.
	if len(traded) >= 2 {
		sr := (tn.Adjusted/t0.Adjusted - 1) * 100
		sum.StockReturnPct = fp(sr)
		if okM {
			if mn, ok := bourse.ValueInForce(market, tn.Day); ok && m0.Value > 0 {
				mr := (mn.Value/m0.Value - 1) * 100
				sum.MarketReturnPct = fp(mr)
				sum.ExcessVsMarketPP = fp(sr - mr)
			}
		}
		if okS {
			if sn, ok := bourse.ValueInForce(sector, tn.Day); ok && s0.Value > 0 {
				secr := (sn.Value/s0.Value - 1) * 100
				sum.SectorReturnPct = fp(secr)
				sum.ExcessVsSectorPP = fp(sr - secr)
			}
		}
	}
	sum.VsMarket = matchedBeta(traded, market)
	if len(sector) > 0 {
		b := matchedBeta(traded, sector)
		sum.VsSector = &b
	}
	return points, sum
}

// relativeBarsSelect is barsSelect ascending and unpaged: the comparison needs
// every session of the window in order, and the longest history in the roster
// is 5,876 bars.
const relativeBarsSelect = `
	SELECT b.trade_date, b.final_close, b.volume, b.trade_count,
	       COALESCE((
	           SELECT c.cumulative_factor
	           FROM corporate_actions c
	           WHERE c.ins_code = b.ins_code
	             AND c.adjustment_version = $2
	             AND c.effective_date > b.trade_date
	           ORDER BY c.effective_date
	           LIMIT 1
	       ), 1.0) AS factor
	FROM equity_bars b
	WHERE b.ins_code = $1
	  AND ($3::date IS NULL OR b.trade_date >= $3)
	ORDER BY b.trade_date`

// Relative implements GET /api/v1/stocks/{symbol}/relative?period=.
func (h *Handler) Relative(w http.ResponseWriter, r *http.Request) {
	symbol := foldSymbol(chi.URLParam(r, "symbol"))
	period, perr := parseLinkPeriod(r.URL.Query().Get("period"), "1y")
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	ctx := r.Context()
	meta, err := scanStock(h.Pool.QueryRow(ctx, stockBySymbolSelect, symbol))
	if errors.Is(err, pgx.ErrNoRows) {
		unknownSymbol(w, symbol)
		return
	}
	if err != nil {
		h.Log.Error("relative_symbol", "error", err, "symbol", symbol)
		httpserver.Internal(w, "database error")
		return
	}
	adj := buildAdjustment(meta)
	if !adj.Servable {
		httpserver.Error(w, http.StatusConflict, "adjustment_unavailable",
			fmt.Sprintf("no validated corporate-action adjustment exists for %q, so no "+
				"return can be compared", meta.SymbolFA),
			map[string]any{"symbol": meta.SymbolFA, "status": adj.Status,
				"refusal_reason": adj.RefusalReason})
		return
	}
	if meta.LastBar == nil {
		unknownSymbol(w, symbol)
		return
	}
	to := dayFloor(*meta.LastBar)
	from, _ := linkPeriodStart(period, to)
	// A traded session BEFORE the window is needed as the base, so the read
	// starts a quarter earlier than the window (a halt longer than that leaves
	// the window starting at its first traded session, which is stated).
	var readFrom *time.Time
	if from != nil {
		rf := from.AddDate(0, -3, 0)
		if meta.AdjustedFirstBar != nil && rf.Before(*meta.AdjustedFirstBar) {
			rf = *meta.AdjustedFirstBar
		}
		readFrom = &rf
	} else if meta.AdjustedFirstBar != nil {
		readFrom = meta.AdjustedFirstBar
	}
	version := ""
	if meta.AdjustmentVersion != nil {
		version = *meta.AdjustmentVersion
	}
	rows, err := h.Pool.Query(ctx, relativeBarsSelect, meta.InsCode, version, readFrom)
	if err != nil {
		h.Log.Error("relative_bars", "error", err, "symbol", symbol)
		httpserver.Internal(w, "database error")
		return
	}
	var sessions []relSession
	for rows.Next() {
		var d time.Time
		var fc, factor float64
		var vol, cnt int64
		if err := rows.Scan(&d, &fc, &vol, &cnt, &factor); err != nil {
			rows.Close()
			h.Log.Error("relative_scan", "error", err, "symbol", symbol)
			httpserver.Internal(w, "database error")
			return
		}
		sessions = append(sessions, relSession{Day: dayFloor(d), Adjusted: fc * factor,
			Traded: vol > 0 || cnt > 0})
	}
	rows.Close()

	benchmarks := []benchmarkItem{}
	marketInfo, market, err := bourse.LoadIndexPoints(ctx, h.Pool, bourse.TEDPIX, readFrom, &to)
	if err != nil && !errors.Is(err, bourse.ErrUnknownIndex) && !errors.Is(err, bourse.ErrIndexRefused) {
		h.Log.Error("relative_market", "error", err, "symbol", symbol)
		httpserver.Internal(w, "database error")
		return
	}
	mb := benchmarkItem{Role: "market", Index: &marketInfo}
	if err != nil {
		mb.Reason = "TEDPIX is not served in this deployment: " + err.Error()
	}
	benchmarks = append(benchmarks, mb)

	var sector []bourse.IndexPoint
	secCode, err := bourse.SectorIndexFor(ctx, h.Pool, meta.SectorCode)
	if err != nil {
		h.Log.Error("relative_sector_lookup", "error", err, "symbol", symbol)
		httpserver.Internal(w, "database error")
		return
	}
	sb := benchmarkItem{Role: "sector"}
	if secCode == "" {
		sb.Reason = fmt.Sprintf("no bourse sector index carries sector code %q", meta.SectorCode)
	} else {
		info, pts, err := bourse.LoadIndexPoints(ctx, h.Pool, secCode, readFrom, &to)
		switch {
		case err == nil:
			sector = pts
			sb.Index = &info
		case errors.Is(err, bourse.ErrIndexRefused):
			sb.Index = &info
			sb.Reason = "the sector index's verdict is refused: " + info.RefusalReason
		default:
			h.Log.Error("relative_sector", "error", err, "symbol", symbol)
			httpserver.Internal(w, "database error")
			return
		}
	}
	benchmarks = append(benchmarks, sb)

	points, sum := buildRelative(sessions, market, sector, from, to)
	resp := relativeResponse{Symbol: meta.SymbolFA, InsCode: meta.InsCode, NameFA: meta.NameFA,
		SectorCode: meta.SectorCode, SectorFA: meta.SectorFA,
		Window:     linkWindow{Period: period, From: datePtr2(from), To: dateString(to)},
		Benchmarks: benchmarks, Points: points, Summary: sum,
		DataAge: buildDataAge(meta.LastBar, time.Now()),
		Notes: []string{
			"The share is its corporate-action-adjusted close; the market is TEDPIX and the " +
				"sector is the bourse index for the share's own TSETMC sector. All three are " +
				"rebased to 100 at the share's first traded session in the window, and a " +
				"session the share did not trade is a gap in its line.",
			"Beta and correlation compare the share's return from one traded session to the " +
				"next with the index's return over exactly the same span, so a halt is one " +
				"observation on both sides and never a run of zeros on one. They describe how " +
				"the share moved with its market in this window, not a cause and not a forecast.",
		},
	}
	if from != nil && sum.From != nil && *sum.From != dateString(*from) {
		resp.Notes = append(resp.Notes, fmt.Sprintf(
			"The comparison starts on %s, the share's traded session in force when the "+
				"window opened on %s.", *sum.From, dateString(*from)))
	}
	httpserver.JSON(w, http.StatusOK, resp)
}
