package bourse

// Where the حقیقی/حقوقی money flow went, market-wide, sector by sector.
//
//	GET /api/v1/bourse/sector-flows
//	GET /api/v1/bourse/sector-flows/{sector}?window=1|5|20|60
//
// The roster table (flows.go) covers nineteen shares. Migration 0030 stores
// the flow of every share TSETMC lists — ~1,150 instruments across the
// bourse, the Farabourse and the base market, every board — from 1 Farvardin
// 1404, and this file sums it by TSETMC's own sector classification. This
// file is the arithmetic, all of it pure; sectorflows_handler.go reads the
// database and holds the built response.
//
// WHAT NET INDIVIDUAL FLOW IS NOT. Every trade has a buyer and a seller, so
// across any set of shares the individuals' net buying is EXACTLY the
// institutions' net selling. "Money flowing into a sector" here is a transfer
// between two client types inside the market, not new money arriving from
// outside it, and the response states the institutional side as the exact
// negative so nobody reads the two as independent flows. What does not net to
// zero is a sector's SHARE of the market's traded value, and its change
// against the window before is the rotation figure.
//
// THE MARKET'S CALENDAR, NOT EACH SHARE'S. The roster table's windows are a
// share's own newest n rows (lastSessions), which for a share halted since
// spring means five sessions from spring. Here a window of n is the n newest
// MARKET sessions, the same dates for every share, and a share that did not
// trade in them contributes nothing. A date is a market session only when at
// least minMarketShares shares carry a traded row on it: the roster's full
// history lives in the same table, and a date on which only the roster traded
// is the roster, not the market.
//
// FOUR TIERS INSTEAD OF ONE CHECK. The roster's Check() (flows.go, unchanged)
// refuses a session with no daily bar, and a non-roster share has none, so the
// strict check would exclude the whole market. classifyFlow keeps every test
// Check() makes and says which one a row passed:
//
//	no_trade       nothing traded on either side (neither checked nor failed)
//	excluded       buying ≠ selling in value or in VOLUME (0.1%), or a stored
//	               session value — market_share_sessions or equity_bars —
//	               disagrees with the flow total by more than 1%
//	bar_checked    both identities hold AND an independent session value agrees
//	identity_only  both identities hold and no independent value exists
//
// Aggregates sum bar_checked and identity_only rows, and every summary carries
// all four counts, so no figure claims a cross-check it did not make. The
// volume identity is new here and separate on purpose: on فولاد's history the
// value identity fails on 1 of 3,849 traded rows and the volume identity on 3.

import (
	"fmt"
	"math"
	"sort"
	"time"
)

// The calendar's rules. Measured on the market watch of 2026-09-29: 1,152
// shares listed, 737 of them main boards; a normal session trades ~1,000 of
// them, the roster is 19. 200 sits far from both.
const (
	// marketFlowFloor is the first session every share's flow is stored
	// from (1 Farvardin 1404). Before it only the roster's is.
	marketFlowFloor = "2025-03-21"
	// minMarketShares is how many shares must carry a traded row for a date
	// to be a market session.
	minMarketShares = 200
	// A newest session carrying fewer traded shares than partialFraction of
	// the median of the partialLookback sessions before it looks like an
	// ingest that stopped halfway, and is flagged.
	partialFraction = 0.8
	partialLookback = 20
	// The heatmap: blockCount blocks of blockSessions sessions — the newest 60.
	blockSessions = 5
	blockCount    = 12
	// topCompanies per side of the top lists.
	topCompanies = 15
	// calendarSessions is the longest window and the one before it.
	calendarSessions = 120
)

// FlowTier is how far a share-session's flow was checked.
type FlowTier string

// The four tiers, spelled as the API serves them; see the file comment.
const (
	TierNoTrade      FlowTier = "no_trade"
	TierExcluded     FlowTier = "excluded"
	TierIdentityOnly FlowTier = "identity_only"
	TierBarChecked   FlowTier = "bar_checked"
)

func (t FlowTier) accepted() bool { return t == TierBarChecked || t == TierIdentityOnly }

// Tier is the four-way verdict on a session with only its daily bar to
// compare against — what a roster share has. Check() stays the roster's gate;
// this is the market's.
func (f FlowSession) Tier() (FlowTier, string) { return classifyFlow(f, nil) }

// classifyFlow applies both identities and every independent statement of the
// session's traded value that exists: f.BarValue (equity_bars) and
// sessionValue (market_share_sessions), both in toman. Pure (unit tested).
func classifyFlow(f FlowSession, sessionValue *float64) (FlowTier, string) {
	buy := f.BuyIValue + f.BuyNValue
	sell := f.SellIValue + f.SellNValue
	if buy <= 0 && sell <= 0 {
		return TierNoTrade, "no traded value on either side"
	}
	if math.Abs(buy-sell) > identityTolerance*math.Max(buy, sell) {
		return TierExcluded, fmt.Sprintf("buy total %.0f and sell total %.0f toman disagree; every "+
			"trade has a buyer and a seller, so one side of this row is wrong", buy, sell)
	}
	bvol := f.BuyIVolume + f.BuyNVolume
	svol := f.SellIVolume + f.SellNVolume
	if bvol <= 0 && svol <= 0 {
		return TierExcluded, "traded value is stated with no traded volume on either side"
	}
	if math.Abs(bvol-svol) > identityTolerance*math.Max(bvol, svol) {
		return TierExcluded, fmt.Sprintf("buy volume %.0f and sell volume %.0f shares disagree; "+
			"every share bought was sold by someone", bvol, svol)
	}
	checked := false
	for _, ref := range []struct {
		what  string
		value *float64
	}{
		{"TSETMC's session statement", sessionValue},
		{"the daily bar", f.BarValue},
	} {
		if ref.value == nil {
			continue
		}
		if *ref.value <= 0 || math.Abs(buy/(*ref.value)-1) > barTolerance {
			return TierExcluded, fmt.Sprintf("the flow total %.0f toman disagrees with the "+
				"session's traded value in %s (%.0f toman)", buy, ref.what, *ref.value)
		}
		checked = true
	}
	if checked {
		return TierBarChecked, ""
	}
	return TierIdentityOnly, ""
}

// --- the inputs ---------------------------------------------------------------

// marketRow is one share's stored flow on one date, in toman.
type marketRow struct {
	InsCode string
	// Flow.BarValue is equity_bars.value for the same session (roster shares).
	Flow FlowSession
	// SessionValue is market_share_sessions.value for the same session.
	SessionValue *float64
}

// priceRow is one share's official close against the reference price the
// exchange opened that session with — both in the same unit, so the ratio is
// all that is used.
type priceRow struct {
	InsCode        string
	Day            time.Time
	Close          float64
	PriceYesterday float64
}

// shareMeta is a market_shares row, joined to the roster.
type shareMeta struct {
	InsCode, Symbol, NameFA, Market, Board, CompanyCode, SectorCode string
	Listed                                                          bool
	// InRoster is an ENABLED equity_instruments row; RosterSymbol is its
	// symbol there, the one /stocks/{symbol} resolves.
	InRoster     bool
	RosterSymbol string
}

type sectorName struct {
	Code, NameFA, NameEN string
}

// sessionCount is one stored date: how many shares carry a row on it, and how
// many of those rows show a trade.
type sessionCount struct {
	Day          time.Time
	Rows, Traded int
}

// --- the market's session calendar ------------------------------------------------

type marketCalendar struct {
	// Sessions are the market sessions, NEWEST FIRST, at most calendarSessions.
	Sessions []time.Time
	Traded   []int // traded shares per session, aligned with Sessions
	Rows     []int
	// NewestStored is the newest date carrying any flow row at all, market
	// session or not, and NewestStoredTraded its traded shares.
	NewestStored       *time.Time
	NewestStoredTraded int
	// ThinAfterNewest counts stored dates newer than the newest market session
	// with too few traded shares to be one; ThinSkipped the ones between
	// market sessions inside the calendar.
	ThinAfterNewest int
	ThinSkipped     int
	MedianPrev      *float64
	NewestPartial   bool
}

func (c marketCalendar) marketWide() bool { return len(c.Sessions) > 0 }

// buildMarketCalendar applies the calendar rule to the stored dates. Pure
// (unit tested).
func buildMarketCalendar(counts []sessionCount, keep int) marketCalendar {
	s := append([]sessionCount(nil), counts...)
	sort.Slice(s, func(i, j int) bool { return s[i].Day.After(s[j].Day) })
	var c marketCalendar
	if len(s) > 0 {
		d := dayFloor(s[0].Day)
		c.NewestStored, c.NewestStoredTraded = &d, s[0].Traded
	}
	// A thin date counts as skipped only once an older market session shows it
	// lay BETWEEN two: the roster's dates before the market-wide history began
	// are older than every session, not gaps in them.
	pending := 0
	for _, x := range s {
		if len(c.Sessions) >= keep {
			break
		}
		if x.Traded < minMarketShares {
			if len(c.Sessions) == 0 {
				c.ThinAfterNewest++
			} else {
				pending++
			}
			continue
		}
		c.ThinSkipped += pending
		pending = 0
		c.Sessions = append(c.Sessions, dayFloor(x.Day))
		c.Traded = append(c.Traded, x.Traded)
		c.Rows = append(c.Rows, x.Rows)
	}
	if len(c.Sessions) >= 2 {
		end := 1 + partialLookback
		if end > len(c.Sessions) {
			end = len(c.Sessions)
		}
		med := median(c.Traded[1:end])
		c.MedianPrev = &med
		c.NewestPartial = float64(c.Traded[0]) < partialFraction*med
	}
	return c
}

func median(v []int) float64 {
	s := append([]int(nil), v...)
	sort.Ints(s)
	n := len(s)
	if n%2 == 1 {
		return float64(s[n/2])
	}
	return float64(s[n/2-1]+s[n/2]) / 2
}

// sessionWindow is window n on the calendar: sessions [0, n), and the one of
// equal length before it, [n, 2n).
type sessionWindow struct {
	N             int
	Available     bool
	From, To      time.Time
	PrevAvailable bool
	PrevFrom      time.Time
	PrevTo        time.Time
	// Base is the market session before the window's first — where a sector
	// index's return over the window is measured from.
	Base *time.Time
}

func windowOn(c marketCalendar, n int) sessionWindow {
	w := sessionWindow{N: n}
	k := len(c.Sessions)
	if k < n {
		return w
	}
	w.Available, w.To, w.From = true, c.Sessions[0], c.Sessions[n-1]
	if k > n {
		b := c.Sessions[n]
		w.Base = &b
	}
	if k >= 2*n {
		w.PrevAvailable, w.PrevTo, w.PrevFrom = true, c.Sessions[n], c.Sessions[2*n-1]
	}
	return w
}

type sessionWindowItem struct {
	Sessions     int     `json:"sessions"`
	Available    bool    `json:"available"`
	From         *string `json:"from"`
	To           *string `json:"to"`
	PreviousFrom *string `json:"previous_from"`
	PreviousTo   *string `json:"previous_to"`
	Reason       string  `json:"reason,omitempty"`
}

func (w sessionWindow) item(stored int) sessionWindowItem {
	it := sessionWindowItem{Sessions: w.N, Available: w.Available}
	if !w.Available {
		it.Reason = fmt.Sprintf("%d market session(s) are stored and this window needs %d; a "+
			"shorter window is not substituted for the one asked for", stored, w.N)
		return it
	}
	it.From, it.To = dayPtr(&w.From), dayPtr(&w.To)
	if w.PrevAvailable {
		it.PreviousFrom, it.PreviousTo = dayPtr(&w.PrevFrom), dayPtr(&w.PrevTo)
	}
	return it
}

// --- summing ------------------------------------------------------------------------

// flowAcc sums share-session rows. Only accepted rows enter the figures; every
// row enters a tier count.
type flowAcc struct {
	rows, barChecked, identityOnly, excluded, noTrade int
	inflowRows, outflowRows                           int
	net, value, sellValue, buyI, sellI                float64
	buyICount, sellICount                             int64
}

func (a *flowAcc) add(f FlowSession, tier FlowTier) {
	a.rows++
	switch tier {
	case TierNoTrade:
		a.noTrade++
		return
	case TierExcluded:
		a.excluded++
		return
	case TierBarChecked:
		a.barChecked++
	case TierIdentityOnly:
		a.identityOnly++
	}
	n := f.BuyIValue - f.SellIValue
	a.net += n
	switch {
	case n > 0:
		a.inflowRows++
	case n < 0:
		a.outflowRows++
	}
	a.value += f.BuyIValue + f.BuyNValue
	a.sellValue += f.SellIValue + f.SellNValue
	a.buyI += f.BuyIValue
	a.sellI += f.SellIValue
	a.buyICount += f.BuyICount
	a.sellICount += f.SellICount
}

func (a *flowAcc) merge(b flowAcc) {
	a.rows += b.rows
	a.barChecked += b.barChecked
	a.identityOnly += b.identityOnly
	a.excluded += b.excluded
	a.noTrade += b.noTrade
	a.inflowRows += b.inflowRows
	a.outflowRows += b.outflowRows
	a.net += b.net
	a.value += b.value
	a.sellValue += b.sellValue
	a.buyI += b.buyI
	a.sellI += b.sellI
	a.buyICount += b.buyICount
	a.sellICount += b.sellICount
}

func (a flowAcc) consistent() int { return a.barChecked + a.identityOnly }

// TieredFlowSummary is a window's flow for a share, a company, a sector or the
// market, with the check tiers it rests on.
type TieredFlowSummary struct {
	From *string `json:"from"`
	To   *string `json:"to"`
	// Sessions is market sessions in the window; Rows the share-session rows
	// stored in it, each in exactly one of the four tiers.
	Sessions     int `json:"sessions"`
	Rows         int `json:"rows"`
	Consistent   int `json:"consistent"`
	BarChecked   int `json:"bar_checked"`
	IdentityOnly int `json:"identity_only"`
	Excluded     int `json:"excluded"`
	NoTrade      int `json:"no_trade"`
	// Net individual flow, toman. NetInstitutionalToman is STATED as its
	// negative rather than summed separately: on an accepted row buying equals
	// selling to within the 0.1% identity, so buy_N − sell_N is −(buy_I −
	// sell_I) up to that rounding, and two numbers would invite reading the
	// two sides of one set of trades as two flows.
	NetIndividualToman      *float64 `json:"net_individual_toman"`
	NetInstitutionalToman   *float64 `json:"net_institutional_toman"`
	NetIndividualPctOfValue *float64 `json:"net_individual_pct_of_value"`
	TotalValueToman         *float64 `json:"total_value_toman"`
	IndividualBuySharePct   *float64 `json:"individual_buy_share_pct"`
	IndividualSellSharePct  *float64 `json:"individual_sell_share_pct"`
	// Buyer power over aggregate tickets, as SummarizeFlows states it.
	BuyerPower *float64 `json:"buyer_power"`
	// Share-sessions with net individual inflow / outflow.
	InflowSessions  int `json:"inflow_sessions"`
	OutflowSessions int `json:"outflow_sessions"`
	// Shares with an accepted row in the window, and those whose window net
	// individual flow is positive / negative.
	InstrumentsTraded  int `json:"instruments_traded"`
	InflowInstruments  int `json:"inflow_instruments"`
	OutflowInstruments int `json:"outflow_instruments"`
	// The share of the enclosing total's traded value (a sector's of the
	// market's, a share's of its sector's or, in the top lists, of the
	// market's), the same in the window before, and the change in points.
	ValueSharePct         *float64 `json:"value_share_pct"`
	PreviousValueSharePct *float64 `json:"previous_value_share_pct"`
	ValueShareChangePP    *float64 `json:"value_share_change_pp"`
}

func summarizeAcc(a flowAcc, w sessionWindow) TieredFlowSummary {
	s := TieredFlowSummary{Sessions: w.N, Rows: a.rows, Consistent: a.consistent(),
		BarChecked: a.barChecked, IdentityOnly: a.identityOnly, Excluded: a.excluded,
		NoTrade: a.noTrade, InflowSessions: a.inflowRows, OutflowSessions: a.outflowRows}
	if w.Available {
		s.From, s.To = dayPtr(&w.From), dayPtr(&w.To)
	}
	if s.Consistent == 0 {
		return s
	}
	s.NetIndividualToman = fv(a.net)
	s.NetInstitutionalToman = fv(-a.net)
	s.TotalValueToman = fv(a.value)
	if a.value > 0 {
		s.NetIndividualPctOfValue = fp(a.net / a.value * 100)
		s.IndividualBuySharePct = fp(a.buyI / a.value * 100)
	}
	if a.sellValue > 0 {
		s.IndividualSellSharePct = fp(a.sellI / a.sellValue * 100)
	}
	if a.buyICount > 0 && a.sellICount > 0 && a.sellI > 0 {
		s.BuyerPower = fp((a.buyI / float64(a.buyICount)) / (a.sellI / float64(a.sellICount)))
	}
	return s
}

// countInstrument counts one share into a summary's instrument tallies.
func (s *TieredFlowSummary) countInstrument(a flowAcc) {
	if a.consistent() == 0 {
		return
	}
	s.InstrumentsTraded++
	switch {
	case a.net > 0:
		s.InflowInstruments++
	case a.net < 0:
		s.OutflowInstruments++
	}
}

// valueShare is part/whole in percent; nil when the whole is empty.
func valueShare(part, whole float64) *float64 {
	if whole <= 0 {
		return nil
	}
	return fp(part / whole * 100)
}

func (s *TieredFlowSummary) setValueShare(cur, curWhole float64, prev *[2]float64) {
	s.ValueSharePct = valueShare(cur, curWhole)
	if prev == nil {
		return
	}
	s.PreviousValueSharePct = valueShare(prev[0], prev[1])
	if s.ValueSharePct != nil && s.PreviousValueSharePct != nil {
		s.ValueShareChangePP = fp(*s.ValueSharePct - *s.PreviousValueSharePct)
	}
}

// --- a share's price over a window ---------------------------------------------------

// chainLinkedChange is a share's price change over [from, to] in percent:
// Π close ÷ price_yesterday over its sessions there, less one.
//
// Chained through the exchange's own reference price rather than taken from
// the first close to the last, because TSETMC lowers the reference on an
// ex-date — a capital increase, a dividend — and a naive ratio would read the
// mechanical drop as a fall. `traded` is the window's dates on which the
// share's flow shows a trade: if any of them has no stored price the product
// would silently skip that session's move, so the change is nil instead.
// Pure (unit tested).
func chainLinkedChange(prices []priceRow, traded []time.Time, from, to time.Time) (*float64, string) {
	have := map[time.Time]bool{}
	prod, priced := 1.0, 0
	for _, p := range prices {
		d := dayFloor(p.Day)
		if d.Before(from) || d.After(to) || p.Close <= 0 || p.PriceYesterday <= 0 {
			continue
		}
		have[d] = true
		prod *= p.Close / p.PriceYesterday
		priced++
	}
	missing := 0
	for _, d := range traded {
		if !have[dayFloor(d)] {
			missing++
		}
	}
	switch {
	case missing > 0:
		return nil, fmt.Sprintf("no closing price is stored for %d of the %d session(s) this "+
			"share traded in this window, and a change that skipped them would be wrong",
			missing, len(traded))
	case priced == 0 && len(traded) == 0:
		return nil, "the share did not trade in this window"
	case priced == 0:
		return nil, "no closing price is stored for this share in this window"
	}
	return fp((prod - 1) * 100), ""
}

// --- the response ---------------------------------------------------------------------

type sectorWindowItem struct {
	TieredFlowSummary
	// The sector's bourse index over the same sessions: from its value in
	// force at the market session before the window's first to its value in
	// force at the window's last.
	IndexReturnPct    *float64 `json:"index_return_pct"`
	IndexReturnReason string   `json:"index_return_reason,omitempty"`
}

// flowBlock is one five-session block of the heatmap.
type flowBlock struct {
	From                    string   `json:"from"`
	To                      string   `json:"to"`
	Rows                    int      `json:"rows"`
	Consistent              int      `json:"consistent"`
	Excluded                int      `json:"excluded"`
	NetIndividualToman      *float64 `json:"net_individual_toman"`
	NetIndividualPctOfValue *float64 `json:"net_individual_pct_of_value"`
	TotalValueToman         *float64 `json:"total_value_toman"`
	ValueSharePct           *float64 `json:"value_share_pct"`
}

type blockSpan struct {
	From     string `json:"from"`
	To       string `json:"to"`
	Sessions int    `json:"sessions"`
}

type sectorFlowItem struct {
	SectorCode   string `json:"sector_code"`
	NameFA       string `json:"name_fa"`
	NameEN       string `json:"name_en"`
	IndexInsCode string `json:"index_ins_code,omitempty"`
	// Instruments carry a stored row in the loaded sessions; Listed are in
	// TSETMC's newest market watch.
	Instruments int                         `json:"instruments"`
	Listed      int                         `json:"listed"`
	Windows     map[string]sectorWindowItem `json:"windows"`
	// Blocks align with the response's `blocks`, oldest first.
	Blocks []flowBlock `json:"blocks"`
}

// shareFlowItem is one instrument over one window.
type shareFlowItem struct {
	InsCode           string            `json:"ins_code"`
	Symbol            string            `json:"symbol"`
	NameFA            string            `json:"name_fa"`
	Market            string            `json:"market"`
	Board             string            `json:"board"`
	CompanyCode       string            `json:"company_code"`
	SectorCode        string            `json:"sector_code"`
	Listed            bool              `json:"listed"`
	InRoster          bool              `json:"in_roster"`
	RosterSymbol      string            `json:"roster_symbol,omitempty"`
	Summary           TieredFlowSummary `json:"summary"`
	PriceChangePct    *float64          `json:"price_change_pct"`
	PriceChangeReason string            `json:"price_change_reason,omitempty"`
}

// companyFlowItem is a company — every board of it summed — over one window.
type companyFlowItem struct {
	CompanyCode  string `json:"company_code"`
	InsCode      string `json:"ins_code"`
	Symbol       string `json:"symbol"`
	NameFA       string `json:"name_fa"`
	Board        string `json:"board"`
	SectorCode   string `json:"sector_code"`
	SectorNameFA string `json:"sector_name_fa"`
	SectorNameEN string `json:"sector_name_en"`
	// Boards whose rows were summed, e.g. ["main","block"].
	Boards       []string          `json:"boards"`
	InRoster     bool              `json:"in_roster"`
	RosterSymbol string            `json:"roster_symbol,omitempty"`
	Summary      TieredFlowSummary `json:"summary"`
	// Measured on the board shown (`board`), whose symbol is shown.
	PriceChangePct    *float64 `json:"price_change_pct"`
	PriceChangeReason string   `json:"price_change_reason,omitempty"`
}

type topFlows struct {
	Inflow  []companyFlowItem `json:"inflow"`
	Outflow []companyFlowItem `json:"outflow"`
}

type sectorFlowCoverage struct {
	MarketWide                bool     `json:"market_wide"`
	Floor                     string   `json:"floor"`
	MinTradedShares           int      `json:"min_traded_shares"`
	NewestSession             *string  `json:"newest_session"`
	NewestTradedShares        int      `json:"newest_traded_shares"`
	InstrumentsWithRowsNewest int      `json:"instruments_with_rows_newest"`
	MedianTradedPrev20        *float64 `json:"median_traded_prev20"`
	PartialThresholdPct       float64  `json:"partial_threshold_pct"`
	NewestPartial             bool     `json:"newest_partial"`
	SessionsAvailable         int      `json:"sessions_available"`
	InstrumentsKnown          int      `json:"instruments_known"`
	InstrumentsListed         int      `json:"instruments_listed"`
	RosterInstruments         int      `json:"roster_instruments"`
	NewestStoredDate          *string  `json:"newest_stored_date"`
	NewestStoredTradedShares  int      `json:"newest_stored_traded_shares"`
	ThinDatesAfterNewest      int      `json:"thin_dates_after_newest"`
	ThinDatesSkipped          int      `json:"thin_dates_skipped"`
}

// flowChecks publishes the tolerances so a client never restates them.
type flowChecks struct {
	IdentityTolerancePct     float64 `json:"identity_tolerance_pct"`
	SessionValueTolerancePct float64 `json:"session_value_tolerance_pct"`
}

type sectorFlowsResponse struct {
	Sessions map[string]sessionWindowItem `json:"sessions"`
	Market   map[string]TieredFlowSummary `json:"market"`
	Sectors  []sectorFlowItem             `json:"sectors"`
	// Blocks are the heatmap's columns, oldest first; MarketBlocks the market
	// over each.
	Blocks       []blockSpan         `json:"blocks"`
	MarketBlocks []flowBlock         `json:"market_blocks"`
	Top          map[string]topFlows `json:"top"`
	Coverage     sectorFlowCoverage  `json:"coverage"`
	Checks       flowChecks          `json:"checks"`
	Notes        []string            `json:"notes"`
	DataAge      DataAge             `json:"data_age"`
}

// sectorFlowsBuilt is what the handler caches: the response (data_age is
// stamped per request, because it is measured against today) and every
// share's summary per sector per window for /sector-flows/{sector}.
type sectorFlowsBuilt struct {
	key     string
	resp    sectorFlowsResponse
	newest  *time.Time
	windows map[int]sessionWindow
	sectors map[string]sectorFlowItem
	shares  map[string]map[int][]shareFlowItem
}

// sectorFlowInput is everything the build reads.
type sectorFlowInput struct {
	Calendar marketCalendar
	// Rows are flow rows on the calendar's dates; any other date is ignored.
	Rows []marketRow
	// Prices over the calendar's span, thin dates included.
	Prices  []priceRow
	Shares  []shareMeta
	Sectors []sectorName
	// SectorIndex maps a sector code to its bourse sector index (SectorIndexFor);
	// IndexSeries holds the validated series, as the index store does.
	SectorIndex map[string]string
	IndexSeries map[string][]IndexPoint
}

// shareWindows is one share's accumulator per window, and the days in each
// window on which its flow shows a trade.
type shareWindows struct {
	acc    map[int]*flowAcc
	traded map[int][]time.Time
	// thin are the share's traded days on stored dates too thin to be market
	// sessions but lying between them. They are in no sum; they ARE inside a
	// window's span of prices, so a price change must see them too.
	thin []time.Time
}

// tradedIn is every day inside window w on which the share's flow shows a
// trade: its market sessions there and any thin date between them. A price
// change over the window needs a stored close for each of them.
func (sw *shareWindows) tradedIn(w sessionWindow) []time.Time {
	if sw == nil {
		return nil
	}
	out := append([]time.Time(nil), sw.traded[w.N]...)
	for _, d := range sw.thin {
		if !d.Before(w.From) && !d.After(w.To) {
			out = append(out, d)
		}
	}
	return out
}

// tieredRow is a row placed on the calendar.
type tieredRow struct {
	idx  int // 0 = the newest session
	day  time.Time
	flow FlowSession
	tier FlowTier
}

// buildSectorFlows is the whole computation. Pure (unit tested).
func buildSectorFlows(in sectorFlowInput) sectorFlowsBuilt {
	cal := in.Calendar
	out := sectorFlowsBuilt{windows: map[int]sessionWindow{}, sectors: map[string]sectorFlowItem{},
		shares: map[string]map[int][]shareFlowItem{}}
	resp := sectorFlowsResponse{Sessions: map[string]sessionWindowItem{},
		Market: map[string]TieredFlowSummary{}, Sectors: []sectorFlowItem{}, Blocks: []blockSpan{},
		MarketBlocks: []flowBlock{}, Top: map[string]topFlows{},
		Checks: flowChecks{IdentityTolerancePct: identityTolerance * 100,
			SessionValueTolerancePct: barTolerance * 100}}
	for _, n := range flowWindows {
		w := windowOn(cal, n)
		out.windows[n] = w
		resp.Sessions[fmt.Sprint(n)] = w.item(len(cal.Sessions))
	}

	meta := map[string]shareMeta{}
	for _, m := range in.Shares {
		meta[m.InsCode] = m
	}
	names := map[string]sectorName{}
	for _, s := range in.Sectors {
		names[s.Code] = s
	}
	resp.Coverage = buildCoverage(cal, in.Shares)
	if !cal.marketWide() {
		out.newest = cal.NewestStored
		resp.Notes = notMarketWideNotes(cal)
		out.resp = resp
		return out
	}
	out.newest = &cal.Sessions[0]

	// Place every row on the calendar.
	pos := make(map[time.Time]int, len(cal.Sessions))
	for i, d := range cal.Sessions {
		pos[d] = i
	}
	byShare := map[string][]tieredRow{}
	thinTraded := map[string][]time.Time{}
	oldest := cal.Sessions[len(cal.Sessions)-1]
	for _, r := range in.Rows {
		d := dayFloor(r.Flow.Day)
		i, ok := pos[d]
		if !ok {
			// A thin date between two market sessions enters no figure, but a
			// share that traded on it moved its price inside the window, and
			// a chained change that silently skipped the day would be wrong.
			f := r.Flow
			if !d.Before(oldest) && !d.After(cal.Sessions[0]) &&
				f.BuyIValue+f.BuyNValue+f.SellIValue+f.SellNValue > 0 {
				thinTraded[r.InsCode] = append(thinTraded[r.InsCode], d)
			}
			continue
		}
		if _, known := meta[r.InsCode]; !known {
			// The foreign key makes this impossible; kept honest rather than dropped.
			meta[r.InsCode] = shareMeta{InsCode: r.InsCode, Symbol: r.InsCode}
		}
		tier, _ := classifyFlow(r.Flow, r.SessionValue)
		byShare[r.InsCode] = append(byShare[r.InsCode], tieredRow{idx: i, day: d, flow: r.Flow, tier: tier})
	}
	// A listed share with no row in these sessions is still in its sector's
	// list, stated as not having traded; it adds nothing to any sum.
	withRows := map[string]bool{}
	for code := range byShare {
		withRows[code] = true
	}
	for _, m := range in.Shares {
		if _, ok := byShare[m.InsCode]; !ok && m.Listed {
			byShare[m.InsCode] = nil
		}
	}
	prices := map[string][]priceRow{}
	for _, p := range in.Prices {
		prices[p.InsCode] = append(prices[p.InsCode], p)
	}

	nBlocks := len(cal.Sessions) / blockSessions
	if nBlocks > blockCount {
		nBlocks = blockCount
	}
	// Block b (0 = newest) covers sessions [5b, 5b+5); output is oldest first.
	for b := nBlocks - 1; b >= 0; b-- {
		resp.Blocks = append(resp.Blocks, blockSpan{
			From: dayString(cal.Sessions[b*blockSessions+blockSessions-1]),
			To:   dayString(cal.Sessions[b*blockSessions]), Sessions: blockSessions})
	}

	perShare := map[string]*shareWindows{}
	sectorPrev := map[string]map[int]float64{}
	marketPrev := map[int]float64{}
	sectorBlocks := map[string][]flowAcc{}
	marketBlocks := make([]flowAcc, nBlocks)
	sectorMembers := map[string][]string{}
	// In insCode order, not map order: a float sum's last bit depends on the
	// order of its terms, and the same data must build the same response.
	codes := make([]string, 0, len(byShare))
	for code := range byShare {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	for _, code := range codes {
		rows := byShare[code]
		m := meta[code]
		sec := m.SectorCode
		sw := &shareWindows{acc: map[int]*flowAcc{}, traded: map[int][]time.Time{},
			thin: thinTraded[code]}
		perShare[code] = sw
		sectorMembers[sec] = append(sectorMembers[sec], code)
		if sectorPrev[sec] == nil {
			sectorPrev[sec] = map[int]float64{}
			sectorBlocks[sec] = make([]flowAcc, nBlocks)
		}
		for _, n := range flowWindows {
			sw.acc[n] = &flowAcc{}
		}
		for _, r := range rows {
			for _, n := range flowWindows {
				if !out.windows[n].Available {
					continue
				}
				switch {
				case r.idx < n:
					sw.acc[n].add(r.flow, r.tier)
					if r.tier != TierNoTrade {
						sw.traded[n] = append(sw.traded[n], r.day)
					}
				case r.idx < 2*n && out.windows[n].PrevAvailable && r.tier.accepted():
					v := r.flow.BuyIValue + r.flow.BuyNValue
					sectorPrev[sec][n] += v
					marketPrev[n] += v
				}
			}
			if b := r.idx / blockSessions; b < nBlocks {
				sectorBlocks[sec][b].add(r.flow, r.tier)
				marketBlocks[b].add(r.flow, r.tier)
			}
		}
	}

	// Sectors, then the market as the sum of its sectors.
	secCodes := make([]string, 0, len(sectorMembers))
	for sec := range sectorMembers {
		sort.Strings(sectorMembers[sec])
		secCodes = append(secCodes, sec)
	}
	sort.Strings(secCodes)
	sectorAcc := map[string]map[int]flowAcc{}
	marketAcc := map[int]flowAcc{}
	for _, sec := range secCodes {
		sectorAcc[sec] = map[int]flowAcc{}
		for _, n := range flowWindows {
			var a flowAcc
			for _, code := range sectorMembers[sec] {
				a.merge(*perShare[code].acc[n])
			}
			sectorAcc[sec][n] = a
			m := marketAcc[n]
			m.merge(a)
			marketAcc[n] = m
		}
	}
	for _, n := range flowWindows {
		w := out.windows[n]
		if !w.Available {
			continue
		}
		s := summarizeAcc(marketAcc[n], w)
		for _, sw := range perShare {
			s.countInstrument(*sw.acc[n])
		}
		if marketAcc[n].value > 0 {
			s.ValueSharePct = fp(100)
		}
		resp.Market[fmt.Sprint(n)] = s
	}

	listedBySector := map[string]int{}
	for _, m := range in.Shares {
		if m.Listed {
			listedBySector[m.SectorCode]++
		}
	}
	for _, sec := range secCodes {
		nm := names[sec]
		item := sectorFlowItem{SectorCode: sec, NameFA: nm.NameFA, NameEN: nm.NameEN,
			IndexInsCode: in.SectorIndex[sec], Listed: listedBySector[sec],
			Windows: map[string]sectorWindowItem{}, Blocks: []flowBlock{}}
		for _, code := range sectorMembers[sec] {
			if withRows[code] {
				item.Instruments++
			}
		}
		for _, n := range flowWindows {
			w := out.windows[n]
			if !w.Available {
				continue
			}
			s := summarizeAcc(sectorAcc[sec][n], w)
			for _, code := range sectorMembers[sec] {
				s.countInstrument(*perShare[code].acc[n])
			}
			var prev *[2]float64
			if w.PrevAvailable {
				prev = &[2]float64{sectorPrev[sec][n], marketPrev[n]}
			}
			s.setValueShare(sectorAcc[sec][n].value, marketAcc[n].value, prev)
			wi := sectorWindowItem{TieredFlowSummary: s}
			wi.IndexReturnPct, wi.IndexReturnReason = sectorIndexReturn(item.IndexInsCode, in.IndexSeries, w)
			item.Windows[fmt.Sprint(n)] = wi
		}
		for b := nBlocks - 1; b >= 0; b-- {
			item.Blocks = append(item.Blocks, blockOf(sectorBlocks[sec][b], marketBlocks[b], resp.Blocks[nBlocks-1-b]))
		}
		resp.Sectors = append(resp.Sectors, item)
		out.sectors[sec] = item
	}
	for b := nBlocks - 1; b >= 0; b-- {
		resp.MarketBlocks = append(resp.MarketBlocks, blockOf(marketBlocks[b], marketBlocks[b], resp.Blocks[nBlocks-1-b]))
	}

	// Every share per sector per window, for /sector-flows/{sector}.
	shareItem := func(code string, n int) shareFlowItem {
		m := meta[code]
		w := out.windows[n]
		a := *perShare[code].acc[n]
		s := summarizeAcc(a, w)
		s.countInstrument(a)
		it := shareFlowItem{InsCode: code, Symbol: m.Symbol, NameFA: m.NameFA, Market: m.Market,
			Board: m.Board, CompanyCode: m.CompanyCode, SectorCode: m.SectorCode, Listed: m.Listed,
			InRoster: m.InRoster, Summary: s}
		if m.InRoster {
			it.RosterSymbol = m.RosterSymbol
		}
		it.PriceChangePct, it.PriceChangeReason = chainLinkedChange(prices[code], perShare[code].tradedIn(w), w.From, w.To)
		return it
	}
	for _, sec := range secCodes {
		out.shares[sec] = map[int][]shareFlowItem{}
		for _, n := range flowWindows {
			if !out.windows[n].Available {
				continue
			}
			items := make([]shareFlowItem, 0, len(sectorMembers[sec]))
			for _, code := range sectorMembers[sec] {
				it := shareItem(code, n)
				it.Summary.ValueSharePct = valueShare(perShare[code].acc[n].value, sectorAcc[sec][n].value)
				items = append(items, it)
			}
			sort.SliceStable(items, func(i, j int) bool {
				vi, vj := perShare[items[i].InsCode].acc[n].value, perShare[items[j].InsCode].acc[n].value
				if vi != vj {
					return vi > vj
				}
				return items[i].Symbol < items[j].Symbol
			})
			out.shares[sec][n] = items
		}
	}

	// The top lists: companies, every board summed.
	for _, n := range flowWindows {
		if !out.windows[n].Available {
			continue
		}
		companies := buildCompanies(meta, names, perShare, prices, n, out.windows[n], marketAcc[n].value)
		resp.Top[fmt.Sprint(n)] = topOf(companies, topCompanies)
	}

	resp.Notes = marketWideNotes(cal)
	out.resp = resp
	return out
}

// sectorIndexReturn is the sector index over the window's sessions, measured
// with windowReturn from the value in force at the market session before the
// window to the value in force at its last session.
func sectorIndexReturn(code string, series map[string][]IndexPoint, w sessionWindow) (*float64, string) {
	if code == "" {
		return nil, "no bourse sector index carries this sector's code"
	}
	s := series[code]
	if len(s) == 0 {
		return nil, "the sector index has no validated series"
	}
	if w.Base == nil {
		return nil, "the market session before this window is not among the stored sessions"
	}
	return windowReturn(between(s, nil, w.To), *w.Base)
}

// blockOf is one heatmap block, its value share taken of `market`.
func blockOf(a, market flowAcc, span blockSpan) flowBlock {
	b := flowBlock{From: span.From, To: span.To, Rows: a.rows, Consistent: a.consistent(),
		Excluded: a.excluded}
	if a.consistent() == 0 {
		return b
	}
	b.NetIndividualToman = fv(a.net)
	b.TotalValueToman = fv(a.value)
	if a.value > 0 {
		b.NetIndividualPctOfValue = fp(a.net / a.value * 100)
	}
	b.ValueSharePct = valueShare(a.value, market.value)
	return b
}

// companyKey rolls a company's boards up: TSETMC's insID[:8]. A row with no
// company code — a roster share seeded before its first market-watch ingest —
// stands alone.
func companyKey(m shareMeta) string {
	if m.CompanyCode != "" {
		return m.CompanyCode
	}
	return "ins:" + m.InsCode
}

var boardOrder = map[string]int{"main": 0, "block": 1, "secondary": 2, "other": 3}

// buildCompanies sums every board of each company over window n. The board
// shown — its symbol and its price change — is the main board wherever the
// company has one, because a block board's prices are negotiated blocks and a
// second board's history is weeks old; otherwise the board that traded the
// most. Only companies with an accepted row are returned. Pure (unit tested).
//
// A company can carry more than one main-board insCode: market_shares never
// deletes, so an instrument TSETMC re-issued under the same insID keeps its
// old row, delisted. The one shown is then the listed one, then the one that
// traded more in the window — never an arbitrary old code whose symbol and
// "did not trade" price change would stand for the company.
func buildCompanies(meta map[string]shareMeta, names map[string]sectorName,
	perShare map[string]*shareWindows, prices map[string][]priceRow, n int,
	w sessionWindow, marketValue float64) []companyFlowItem {
	valueOf := func(code string) float64 {
		if sw := perShare[code]; sw != nil {
			return sw.acc[n].value
		}
		return 0
	}
	better := func(a, b string) bool { // a is a better main board to show than b
		ma, mb := meta[a], meta[b]
		if ma.Listed != mb.Listed {
			return ma.Listed
		}
		if va, vb := valueOf(a), valueOf(b); va != vb {
			return va > vb
		}
		return a < b
	}
	mainBoard := map[string]string{}
	for code, m := range meta {
		if m.Board == "main" {
			k := companyKey(m)
			if cur, ok := mainBoard[k]; !ok || better(code, cur) {
				mainBoard[k] = code
			}
		}
	}
	members := map[string][]string{}
	for code := range perShare {
		k := companyKey(meta[code])
		members[k] = append(members[k], code)
	}
	out := []companyFlowItem{}
	for k, codes := range members {
		sort.Strings(codes)
		var acc flowAcc
		var boards []string
		seen := map[string]bool{}
		for _, code := range codes {
			a := *perShare[code].acc[n]
			acc.merge(a)
			// A board is named only when something of it was summed: one whose
			// rows were all excluded or idle added nothing to the figures.
			if b := meta[code].Board; a.consistent() > 0 && !seen[b] {
				seen[b] = true
				boards = append(boards, b)
			}
		}
		if acc.consistent() == 0 {
			continue
		}
		sort.Slice(boards, func(i, j int) bool { return boardOrder[boards[i]] < boardOrder[boards[j]] })
		shown, ok := mainBoard[k]
		if !ok {
			shown = codes[0]
			for _, code := range codes[1:] {
				if valueOf(code) > valueOf(shown) {
					shown = code
				}
			}
		}
		m := meta[shown]
		s := summarizeAcc(acc, w)
		for _, code := range codes {
			s.countInstrument(*perShare[code].acc[n])
		}
		s.ValueSharePct = valueShare(acc.value, marketValue)
		it := companyFlowItem{CompanyCode: m.CompanyCode, InsCode: shown, Symbol: m.Symbol,
			NameFA: m.NameFA, Board: m.Board, SectorCode: m.SectorCode,
			SectorNameFA: names[m.SectorCode].NameFA, SectorNameEN: names[m.SectorCode].NameEN,
			Boards: boards, InRoster: m.InRoster, Summary: s}
		if m.InRoster {
			it.RosterSymbol = m.RosterSymbol
		}
		// The main board may not have traded in the window at all while a
		// block did; its change is then stated as such, not borrowed.
		it.PriceChangePct, it.PriceChangeReason = chainLinkedChange(prices[shown], perShare[shown].tradedIn(w), w.From, w.To)
		out = append(out, it)
	}
	return out
}

// topOf is the k companies with the largest net individual inflow and the k
// with the largest outflow. Ties go to the larger traded value, then the
// symbol, so the lists are stable. Pure (unit tested).
func topOf(companies []companyFlowItem, k int) topFlows {
	t := topFlows{Inflow: []companyFlowItem{}, Outflow: []companyFlowItem{}}
	net := func(c companyFlowItem) float64 { return *c.Summary.NetIndividualToman }
	value := func(c companyFlowItem) float64 { return *c.Summary.TotalValueToman }
	for _, c := range companies {
		switch {
		case net(c) > 0:
			t.Inflow = append(t.Inflow, c)
		case net(c) < 0:
			t.Outflow = append(t.Outflow, c)
		}
	}
	order := func(list []companyFlowItem, before func(a, b float64) bool) {
		sort.SliceStable(list, func(i, j int) bool {
			a, b := list[i], list[j]
			if net(a) != net(b) {
				return before(net(a), net(b))
			}
			if value(a) != value(b) {
				return value(a) > value(b)
			}
			if a.Symbol != b.Symbol {
				return a.Symbol < b.Symbol
			}
			return a.InsCode < b.InsCode // symbols are reused across listings
		})
	}
	order(t.Inflow, func(a, b float64) bool { return a > b })
	order(t.Outflow, func(a, b float64) bool { return a < b })
	if len(t.Inflow) > k {
		t.Inflow = t.Inflow[:k]
	}
	if len(t.Outflow) > k {
		t.Outflow = t.Outflow[:k]
	}
	return t
}

func buildCoverage(cal marketCalendar, shares []shareMeta) sectorFlowCoverage {
	c := sectorFlowCoverage{MarketWide: cal.marketWide(), Floor: marketFlowFloor,
		MinTradedShares: minMarketShares, PartialThresholdPct: partialFraction * 100,
		NewestPartial: cal.NewestPartial, SessionsAvailable: len(cal.Sessions),
		MedianTradedPrev20: cal.MedianPrev, NewestStoredDate: dayPtr(cal.NewestStored),
		NewestStoredTradedShares: cal.NewestStoredTraded, ThinDatesAfterNewest: cal.ThinAfterNewest,
		ThinDatesSkipped: cal.ThinSkipped, InstrumentsKnown: len(shares)}
	for _, m := range shares {
		if m.Listed {
			c.InstrumentsListed++
		}
		if m.InRoster {
			c.RosterInstruments++
		}
	}
	if cal.marketWide() {
		c.NewestSession = dayPtr(&cal.Sessions[0])
		c.NewestTradedShares = cal.Traded[0]
		c.InstrumentsWithRowsNewest = cal.Rows[0]
	}
	return c
}

// --- what the figures are and are not ---------------------------------------------------

const zeroSumNote = "Net individual flow is individuals' buying minus their selling, in toman. " +
	"Every trade has a buyer and a seller, so institutions' net flow is exactly its negative: " +
	"\"money into a sector\" here is a transfer between individuals and institutions inside the " +
	"market, not new money entering it. A sector's share of the market's traded value, and its " +
	"change against the window before, is the measure of rotation between sectors that does not " +
	"net to zero."

func notMarketWideNotes(cal marketCalendar) []string {
	head := "Market-wide flows not ingested yet: no money-flow row is stored at all."
	if cal.NewestStored != nil {
		head = fmt.Sprintf("Market-wide flows not ingested yet: the newest stored date with money "+
			"flow, %s, carries %d traded share(s), and a market session needs at least %d. The "+
			"roster alone is not the market, so nothing is summed here; the Tehran market page "+
			"shows the roster's own table, labelled as the roster.",
			dayString(*cal.NewestStored), cal.NewestStoredTraded, minMarketShares)
	}
	return []string{head, zeroSumNote}
}

func marketWideNotes(cal marketCalendar) []string {
	notes := []string{
		zeroSumNote,
		fmt.Sprintf("Every share-session is checked before it counts. Buying must equal selling, "+
			"in value and in volume, to within %.1f%%; where TSETMC's session statement or the "+
			"daily bar gives the session's traded value, the flow total must agree with it to "+
			"within %.0f%%. bar_checked rows passed all of that; identity_only rows passed the "+
			"identities with no independent value to compare against; excluded rows failed a "+
			"check and are left out of every figure; no_trade rows had nothing traded. Every "+
			"summary carries the four counts.", identityTolerance*100, barTolerance*100),
		fmt.Sprintf("Windows count market sessions: a date is one only when at least %d shares "+
			"carry a traded row, and a window of n is the n newest such sessions for every share, "+
			"so a share halted through a window contributes nothing to it.", minMarketShares),
		"Sector membership is TSETMC's classification at the last fetch, applied to every " +
			"session: a share the exchange has since moved counts in its new sector for its " +
			"whole history.",
		"Only shares in TSETMC's market watch when a fetch ran are stored. A share delisted " +
			"before the first fetch has no stored flow, so older windows can miss it.",
		"A company's boards — main, block, second — are separate instruments and all of them " +
			"count in its sector. The top lists sum a company's boards and show its main-board " +
			"symbol; block-board trades are negotiated blocks and can dominate a thin session.",
		"Price change is chained from TSETMC's official close against the reference price of " +
			"each session the share traded (Π close ÷ price yesterday), so a capital increase or " +
			"a dividend, which lowers the reference, is not read as a fall. It is empty when a " +
			"traded session has no stored close.",
		"These figures describe who bought and who sold. None of them is a forecast, and a " +
			"sector's net individual inflow says nothing about its next price.",
	}
	if cal.NewestPartial && cal.MedianPrev != nil {
		lookback := len(cal.Sessions) - 1
		if lookback > partialLookback {
			lookback = partialLookback
		}
		notes = append(notes, fmt.Sprintf("The newest session, %s, carries %d traded shares "+
			"against a median of %.0f over the %d sessions before it — under %.0f%% of it — so "+
			"it looks partly ingested: its 1-session figures and its heatmap block undercount "+
			"the market.", dayString(cal.Sessions[0]), cal.Traded[0], *cal.MedianPrev, lookback,
			partialFraction*100))
	}
	if cal.ThinAfterNewest > 0 {
		notes = append(notes, fmt.Sprintf("%d stored date(s) after %s carry flow for fewer than "+
			"%d traded shares — the roster alone, or an ingest in progress — and are not market "+
			"sessions; nothing here includes them.", cal.ThinAfterNewest,
			dayString(cal.Sessions[0]), minMarketShares))
	}
	if cal.ThinSkipped > 0 {
		notes = append(notes, fmt.Sprintf("%d stored date(s) between the market sessions used "+
			"here carry fewer than %d traded shares and are left out of every window.",
			cal.ThinSkipped, minMarketShares))
	}
	return notes
}
