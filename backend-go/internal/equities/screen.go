package equities

// GET /api/v1/stocks/screen -- every enabled roster symbol, over one window,
// in one unit of account, with the metrics this data can actually support and
// a NAMED list of the ones it cannot.
//
// WHY THIS ENDPOINT EXISTS AT ALL. /stocks is roster metadata: symbol, sector,
// coverage, adjustment verdict, and no price of any kind. /stocks/{symbol}/bars
// is one instrument's OHLCV. A screener over twenty symbols built from those
// two is twenty bar requests and twenty client-side reimplementations of the
// same arithmetic. Everything below is arithmetic over stored bars -- the same
// class internal/prices computes for candles, internal/indicators for moving
// averages and internal/relvalue for relative value -- so it belongs on the
// server, computed once, from the one adjusted series.
//
// # THE HONESTY BOUNDARY, WHICH IS THE POINT OF THE ENDPOINT
//
// A screener is the single most tempting place in a market API to print a
// number nobody measured. The columns a reader expects -- P/E, EPS growth,
// ROE, dividend yield, market cap -- ALL require financial statements or a
// share count, and this deployment ingests neither:
//
//   - Codal publishes no XBRL. Verified false on 160 of 160 sampled filings;
//     its "Excel" export is HTML tables, and the chart of accounts differs by
//     sector (a bank has no gross-profit concept), so there is not even one
//     schema to parse into.
//   - fipiran, the one source shaped like a normalised fundamentals API,
//     returns TotalStockholderEquity 100% empty and mis-keys its symbol filter.
//   - Shares outstanding (TSETMC's zTitad) is not ingested, so market cap
//     cannot be computed and is not derived from anything else.
//
// So those columns are not here. They are also not silently absent: every row
// carries `absent_metrics`, an array of {metric, reason} naming each one with
// the REAL obstacle, so the page states its boundary from the API rather than
// from hardcoded frontend copy that nothing keeps in step with the ingest.
//
// One omission is DELIBERATE rather than forced, and it is called out in its
// own reason because a future reader will find the data and wonder why it was
// skipped: TSETMC publishes a per-instrument estimatedEPS (518 for فولاد) and
// a sectorPE. It is ONE UNDATED TRAILING SCALAR with no basis flag and no
// history, and mandatory company EPS guidance was discontinued around FY1396 --
// nine years ago. A column headed P/E beside measured metrics would read as a
// fundamental ratio. If a later increment ingests it, it needs its own basis
// label, exactly as cost_basis and confidence_basis work elsewhere in this API.
//
// # THREE RULES THIS FILE IS BUILT AROUND
//
//  1. RETURNS COME FROM ADJUSTED CLOSES. A screener that sorts on raw Tehran
//     closes ranks companies by how recently each did a capital increase:
//     فولاد's raw close is x1.52 over nineteen years against x907.86 adjusted.
//     Only `last_close` is raw, and it says so in `last_close_basis`.
//
//  2. THE CURRENCY IS RIAL, AND THE RESPONSE SAYS SO. TSETMC quotes rial; the
//     rest of this API reports Iranian amounts in TOMAN, so a client that
//     assumes the house convention is off by a factor of ten. The x10 into the
//     numeraire engine happens exactly once, in convertedCloses, where the
//     exchange that caused it is in view.
//
//  3. A METRIC THAT CANNOT BE COMPUTED IS NULL WITH A REASON, NEVER ZERO. A
//     zero return reads as "this share went nowhere"; a zero volatility reads
//     as "this share never moved". Every nullable figure here has a sibling
//     *_reason that is non-empty exactly when the figure is null.
//
// And a symbol that cannot be screened does not VANISH. کچاد is seeded
// disabled -- the validation gate refuses its adjustment, and its roster note
// records the measurement -- so it appears in the top-level `excluded` block
// with that reason attached. A screen whose universe cannot be reconstructed
// from its own response is a screen that quietly decides what a reader is
// allowed to consider.

import (
	"context"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/relvalue"
)

// --- vocabulary ---------------------------------------------------------------

// screenPeriods is CLOSED. A period outside it is refused rather than resolved
// to the nearest thing: a caller that asked for 2y and silently received 1y
// reads a number that answers a different question. It is deliberately
// SHORTER than internal/relvalue's vocabulary -- the equity roster is being
// screened, not the macro series -- and adding to it is a decision about what
// the page offers, not an accident of validation.
var screenPeriods = []string{"1m", "3m", "6m", "1y", "max"}

// defaultScreenPeriod is what an absent ?period= means, echoed on the response
// so a caller never has to infer which window it was served.
const defaultScreenPeriod = "1y"

// The screener's units of account.
//
// IRR, not IRT: this endpoint's own figures are RIALS because that is what
// TSETMC quotes, and offering a menu item spelled IRT beside a payload of
// rials would invite exactly the factor-of-ten error rule 2 exists to prevent.
// It maps onto internal/relvalue's toman hub in convertedCloses, once.
const (
	numeraireIRR  = "IRR"
	numeraireUSD  = "USD"
	numeraireGold = "GOLD"
)

var screenNumeraires = []string{numeraireIRR, numeraireUSD, numeraireGold}

// defaultScreenNumeraire is the exchange's own unit: the default view of a
// Tehran share is the number a Tehran reader already holds in their head.
const defaultScreenNumeraire = numeraireIRR

// rialsPerToman is the ONE place this package crosses between the exchange's
// unit and the house's. internal/relvalue states of itself that "IRR is a
// display-only x10 and never appears in this package at all", so the division
// belongs on this side of the door, in view of the exchange that caused it.
const rialsPerToman = 10.0

const (
	// defaultScreenLimit is above the current roster (20 instruments), so a
	// caller that sends no ?limit= receives every row rather than a silently
	// truncated page.
	defaultScreenLimit = 50
	// maxScreenLimit is a guard against accidents, not a real ceiling: the
	// whole TEDPIX is under 700 instruments and this roster is 20.
	maxScreenLimit = 200
)

// screenSortSpec is one sortable column.
//
// Desc is the NATURAL direction for that column, used when ?order= is absent.
// It is per column on purpose: "sort by return" means best first, and "sort by
// drawdown" means WORST first -- drawdowns are negative, so descending would
// rank the shares that never fell above the ones that halved.
type screenSortSpec struct {
	Key  string
	Desc bool
	// Metric extracts the sortable figure, or nil when this row could not
	// compute it. A nil metric sorts LAST in both directions: a share whose
	// volatility could not be measured is not the least volatile share.
	// Metric itself is nil for the one non-numeric column, `symbol`.
	Metric func(screenItem) *float64
	// About is published in the refusal message, so a caller that guessed a
	// column name is told what the real ones measure rather than only that its
	// guess was wrong.
	About string
}

var screenSorts = []screenSortSpec{
	{"avg_value", true, func(i screenItem) *float64 { return i.AvgValue },
		"mean rial turnover per traded session in the window (the default)"},
	{"return", true, func(i screenItem) *float64 { return i.ReturnPct },
		"period return from adjusted closes, in the selected numeraire"},
	{"volatility", true, func(i screenItem) *float64 { return i.VolatilityPct },
		"standard deviation of session returns, not annualised"},
	{"drawdown", false, func(i screenItem) *float64 { return i.MaxDrawdownPct },
		"largest peak-to-trough decline in the window; ascending by default, so the worst is first"},
	{"last_close", true, func(i screenItem) *float64 { return i.LastClose },
		"the exchange's raw official closing price, in rials"},
	{"last_volume", true, func(i screenItem) *float64 { return floatOfInt(i.LastVolume) },
		"shares traded in the last traded session"},
	{"last_value", true, func(i screenItem) *float64 { return i.LastValue },
		"rial turnover of the last traded session"},
	{"last_trade_count", true, func(i screenItem) *float64 { return floatOfInt(i.LastTradeCount) },
		"number of trades in the last traded session"},
	{"avg_volume", true, func(i screenItem) *float64 { return i.AvgVolume },
		"mean shares traded per traded session in the window"},
	{"avg_trade_count", true, func(i screenItem) *float64 { return i.AvgTradeCount },
		"mean number of trades per traded session in the window"},
	{"bar_count", true, func(i screenItem) *float64 { return floatOf(float64(i.BarCount)) },
		"stored sessions for the instrument, over its whole history"},
	{"symbol", false, nil, "the Persian trading symbol, in code-point order"},
}

const defaultScreenSort = "avg_value"

func lookupScreenSort(key string) (screenSortSpec, bool) {
	for _, s := range screenSorts {
		if s.Key == key {
			return s, true
		}
	}
	return screenSortSpec{}, false
}

func screenSortKeys() []string {
	out := make([]string, 0, len(screenSorts))
	for _, s := range screenSorts {
		out = append(out, s.Key)
	}
	return out
}

func screenSortCatalog() []map[string]string {
	out := make([]map[string]string, 0, len(screenSorts))
	for _, s := range screenSorts {
		direction := "asc"
		if s.Desc {
			direction = "desc"
		}
		out = append(out, map[string]string{
			"key": s.Key, "default_order": direction, "measures": s.About,
		})
	}
	return out
}

// --- the boundary, on the wire --------------------------------------------------

// absentMetric is one thing a reader will look for and not find, with the real
// obstacle rather than "not available".
type absentMetric struct {
	Metric string `json:"metric"`
	// Label is what a page would have headed the column, so the frontend does
	// not have to hold its own English for a boundary the API defines.
	Label  string `json:"label"`
	Reason string `json:"reason"`
	// Requires names what would have to be ingested first. It is separate from
	// Reason so a client can group the five entries by their common blocker
	// without parsing prose.
	Requires string `json:"requires"`
}

// screenAbsentMetrics is the fixed boundary of this dataset. It is published
// per row rather than once at the top level because the obstacle is a property
// of the INSTRUMENT -- a later increment that ingests statements for some
// symbols and not others must be able to shorten this list per row without
// changing the response's shape.
var screenAbsentMetrics = []absentMetric{
	{
		Metric: "pe_ratio", Label: "P/E",
		Requires: "audited earnings per share, with a stated basis and a period",
		Reason: "A price/earnings ratio needs earnings, and this deployment ingests no " +
			"financial statements: Codal publishes no XBRL (verified false on 160 of 160 " +
			"sampled filings) and its \"Excel\" export is HTML tables, while fipiran — the " +
			"one source shaped like a normalised fundamentals API — returns " +
			"TotalStockholderEquity 100% empty and mis-keys its symbol filter. TSETMC does " +
			"publish a per-instrument estimatedEPS (518 for فولاد) and a sectorPE, and this " +
			"endpoint deliberately does NOT surface them: it is one undated trailing scalar " +
			"with no basis flag and no history, and mandatory company EPS guidance was " +
			"discontinued around FY1396, nine years ago. A column headed P/E beside measured " +
			"metrics would read as a fundamental ratio. If a later increment ingests it, it " +
			"needs its own basis label, exactly as cost_basis and confidence_basis do " +
			"elsewhere in this API.",
	},
	{
		Metric: "eps_growth", Label: "EPS growth",
		Requires: "a per-period history of audited earnings",
		Reason: "Earnings growth needs a HISTORY of earnings, which needs statements parsed " +
			"per period. Codal publishes no XBRL, and its chart of accounts differs by sector " +
			"— a bank has no gross-profit concept — so there is not even one schema to parse " +
			"into. That is a three-to-six-month ingestion project, not a column.",
	},
	{
		Metric: "roe", Label: "ROE",
		Requires: "net income and shareholders' equity, per period",
		Reason: "Return on equity needs net income and shareholders' equity. fipiran returns " +
			"TotalStockholderEquity 100% empty across the sampled roster, so the one " +
			"normalised-looking source supplies neither leg, and Codal's filings would have " +
			"to be parsed out of HTML tables per sector-specific chart of accounts.",
	},
	{
		Metric: "dividend_yield", Label: "Dividend yield",
		Requires: "the DPS approved at each annual general meeting",
		Reason: "A dividend yield needs the dividend per share approved at each annual " +
			"general meeting. Those live in Codal filings as HTML tables with no XBRL, and no " +
			"dividend history is ingested here. corporate_actions records the PRICE effect of " +
			"a distribution as an adjustment factor, which is a different number and cannot " +
			"be divided by a price to produce a yield.",
	},
	{
		Metric: "market_cap", Label: "Market cap",
		Requires: "shares outstanding (TSETMC's zTitad)",
		Reason: "Market capitalisation needs shares outstanding, which this increment does " +
			"not ingest. It is deliberately not derived from anything else, and the column is " +
			"absent with this reason rather than present and empty — an empty column with no " +
			"reason is indistinguishable from a company that has no market cap.",
	},
}

// --- query ----------------------------------------------------------------------

// sectorOption is one sector the roster actually contains. The vocabulary is
// built from the roster rather than hard-coded, so a refusal names the sectors
// this deployment carries TODAY and a new listing needs no code change.
type sectorOption struct {
	Code   string `json:"sector_code"`
	NameFA string `json:"sector_fa"`
	Count  int    `json:"instrument_count"`
}

// screenQuery is a validated /stocks/screen request.
type screenQuery struct {
	Period    string
	From      *time.Time // nil for period=max: as far back as each series goes
	To        time.Time
	Numeraire string
	Sector    string // "" means every sector
	Sort      screenSortSpec
	Desc      bool
	Limit     int
	AsOf      time.Time
}

// dayFloor drops the time of day. Bars are DATE rows, so every bound this
// endpoint applies is a calendar date and never an instant -- serialising one
// with a time of day would invent a precision the exchange never stated.
func dayFloor(t time.Time) time.Time {
	u := t.UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC)
}

// screenPeriodStart counts back in CALENDAR months and years rather than fixed
// day counts, matching internal/relvalue's periodStart: "1y" from 2026-09-09
// means 2025-09-09, not 2026-09-09 minus 365 days, and the two differ across a
// leap year. "max" returns nil -- how far back the data goes is a property of
// each instrument, not of the request, so it is reported per row as
// metrics_from.
func screenPeriodStart(period string, to time.Time) (*time.Time, bool) {
	var from time.Time
	switch period {
	case "1m":
		from = to.AddDate(0, -1, 0)
	case "3m":
		from = to.AddDate(0, -3, 0)
	case "6m":
		from = to.AddDate(0, -6, 0)
	case "1y":
		from = to.AddDate(-1, 0, 0)
	case "max":
		return nil, true
	default:
		return nil, false
	}
	u := dayFloor(from)
	return &u, true
}

// parseScreenQuery validates the query string. Pure function (unit tested):
// `now` and the sector vocabulary both arrive from the caller, so there is no
// hidden clock and no hidden database.
//
// EVERY refusal here is a 400 with a specific message and the accepted
// vocabulary in `details`. Nothing is clamped and nothing is substituted --
// the pattern candles.go established and internal/economic and
// internal/relvalue both follow. A caller that asked for limit=5000 and
// silently received 200 cannot tell that its "whole market" screen is a page.
func parseScreenQuery(q url.Values, sectors []sectorOption, now time.Time) (screenQuery, *paramError) {
	asOf := dayFloor(now)
	out := screenQuery{
		Period:    defaultScreenPeriod,
		To:        asOf,
		AsOf:      asOf,
		Numeraire: defaultScreenNumeraire,
		Limit:     defaultScreenLimit,
	}

	if raw := q.Get("period"); raw != "" {
		out.Period = raw
	}
	from, ok := screenPeriodStart(out.Period, asOf)
	if !ok {
		return out, badParam(
			fmt.Sprintf("period must be one of %s, got %q",
				strings.Join(screenPeriods, ", "), q.Get("period")),
			map[string]any{"period": q.Get("period"), "supported": screenPeriods})
	}
	out.From = from

	if raw := q.Get("numeraire"); raw != "" {
		key := strings.ToUpper(raw)
		if !containsString(screenNumeraires, key) {
			return out, badParam(
				fmt.Sprintf("numeraire must be one of %s, got %q",
					strings.Join(screenNumeraires, ", "), raw),
				map[string]any{
					"numeraire": raw,
					"supported": screenNumeraires,
					"hint": "GET /api/v1/markets/numeraires reports which units of account " +
						"this deployment can actually back",
				})
		}
		out.Numeraire = key
	}

	if raw := q.Get("sector"); raw != "" {
		found := false
		for _, s := range sectors {
			if s.Code == raw {
				found = true
				break
			}
		}
		if !found {
			// Refused, not answered with an empty list. /stocks?sector= may
			// legitimately return nothing (it is a filter over a roster that
			// grows), but a SCREEN whose every row was filtered away is
			// indistinguishable from a sector in which nothing is listed, and
			// the caller cannot tell which it got.
			return out, badParam(
				fmt.Sprintf("sector %q is not a sector code in this deployment's roster", raw),
				map[string]any{
					"sector":    raw,
					"supported": sectors,
					"hint":      "sector is the TSETMC sector CODE (cSecVal), e.g. 27, not the Persian name",
				})
		}
		out.Sector = raw
	}

	sortKey := defaultScreenSort
	if raw := q.Get("sort"); raw != "" {
		sortKey = raw
	}
	spec, ok := lookupScreenSort(sortKey)
	if !ok {
		return out, badParam(
			fmt.Sprintf("sort must be one of %s, got %q",
				strings.Join(screenSortKeys(), ", "), q.Get("sort")),
			map[string]any{
				"sort": q.Get("sort"), "supported": screenSortKeys(),
				"columns": screenSortCatalog(),
			})
	}
	out.Sort = spec
	out.Desc = spec.Desc

	if raw := q.Get("order"); raw != "" {
		switch strings.ToLower(raw) {
		case "asc":
			out.Desc = false
		case "desc":
			out.Desc = true
		default:
			return out, badParam(
				fmt.Sprintf("order must be asc or desc, got %q", raw),
				map[string]any{"order": raw, "supported": []string{"asc", "desc"}})
		}
	}

	if raw := q.Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil || n < 1 || n > maxScreenLimit {
			return out, badParam(
				fmt.Sprintf("limit must be between 1 and %d, got %q", maxScreenLimit, raw),
				map[string]any{"limit": raw, "min": 1, "max": maxScreenLimit})
		}
		out.Limit = n
	}
	return out, nil
}

func containsString(haystack []string, needle string) bool {
	for _, h := range haystack {
		if h == needle {
			return true
		}
	}
	return false
}

// --- rows -------------------------------------------------------------------------

// screenBarRow is one stored bar plus the back-adjustment factor that applies
// to it, resolved by the SAME rule barsSelect uses: the cumulative factor of
// the first action effective strictly after this bar, and 1.0 when none
// follows.
type screenBarRow struct {
	InsCode    string
	TradeDate  time.Time
	FinalClose float64
	Volume     int64
	TradeCount int64
	Value      float64
	Factor     float64
}

// Traded reports whether the session actually had trading. TSETMC serves a
// halted session as a stored bar with volume and trade_count at zero, and its
// final_close is the carried reference price rather than a print.
func (b screenBarRow) Traded() bool { return b.Volume > 0 || b.TradeCount > 0 }

// AdjustedClose is the ONLY arithmetic this package performs on a stored
// price: one multiplication, by a factor stored on a corporate_actions row.
// It is computed from final_close (قیمت پایانی) and not from close, because
// final_close is the basis price_yesterday is stated on and therefore the
// basis the whole corporate-action chain was detected and validated against.
func (b screenBarRow) AdjustedClose() float64 { return b.FinalClose * b.Factor }

// conversionBlock accounts for what the numeraire could not carry. It is null
// for numeraire=IRR, where nothing is converted at all.
type conversionBlock struct {
	Chain []string `json:"chain"`
	// CarriedForwardDays counts sessions priced with a numeraire quote from an
	// EARLIER day. Nothing is wrong with them; a window where most days are
	// carried is a window whose figures rest on a stale rate, and the reader is
	// entitled to know that.
	CarriedForwardDays int `json:"carried_forward_days"`
	// DroppedNoPriorQuote counts sessions removed because the numeraire had
	// never quoted at or before them. They are not interpolated: the only value
	// that could fill them is a LATER quote, and using it would price a 2021
	// session with a 2022 exchange rate.
	DroppedNoPriorQuote int `json:"dropped_no_prior_quote"`
	// DroppedNonPositiveQuote counts sessions removed because the quote in
	// force was zero or negative -- a stored fault, not a rate.
	DroppedNonPositiveQuote int `json:"dropped_non_positive_quote"`
}

// screenItem is one instrument over the window.
//
// EVERY nullable figure has a sibling *_reason that is non-empty exactly when
// the figure is null. That pairing is the contract: a null with no reason is a
// defect in this file, and a zero standing in for a null is the defect this
// pairing exists to make impossible.
type screenItem struct {
	Symbol     string `json:"symbol"`
	InsCode    string `json:"ins_code"`
	NameFA     string `json:"name_fa"`
	SectorCode string `json:"sector_code"`
	SectorFA   string `json:"sector_fa"`
	Market     string `json:"market"`
	Board      string `json:"board"`

	// LastClose is the exchange's RAW official closing price on
	// LastTradeDate, in RIALS, unadjusted -- see LastCloseBasis. At the newest
	// end of a back-adjusted series the factor is 1.0 by construction (no
	// corporate action follows the last bar), so this equals the adjusted close
	// on that day; the field is still labelled raw, because that stops being
	// true the moment a new action is detected.
	LastClose      *float64 `json:"last_close"`
	LastCloseBasis string   `json:"last_close_basis"`
	// LastTradeDate is the last session in which this instrument actually
	// traded, which is NOT necessarily LastBar: a halted session is stored.
	LastTradeDate *string `json:"last_trade_date"`
	LastBar       *string `json:"last_bar"`
	FirstBar      *string `json:"first_bar"`
	BarCount      int     `json:"bar_count"`

	// --- period metrics, from ADJUSTED closes, in MetricsNumeraire ---
	ReturnPct         *float64 `json:"return_pct"`
	ReturnReason      *string  `json:"return_reason"`
	VolatilityPct     *float64 `json:"volatility_pct"`
	VolatilityReason  *string  `json:"volatility_reason"`
	MaxDrawdownPct    *float64 `json:"max_drawdown_pct"`
	MaxDrawdownReason *string  `json:"max_drawdown_reason"`
	// The window the three figures above were ACTUALLY measured over, which is
	// shorter than the requested window whenever the instrument listed later
	// than it or the numeraire had not yet quoted. A percentage whose window is
	// not published beside it answers a different question from the one it
	// appears to answer.
	MetricsFrom         *string `json:"metrics_from"`
	MetricsTo           *string `json:"metrics_to"`
	MetricsObservations int     `json:"metrics_observations"`
	MetricsNumeraire    string  `json:"metrics_numeraire"`

	// --- coverage of the window ---
	SessionsInPeriod int `json:"sessions_in_period"`
	TradedSessions   int `json:"traded_sessions"`
	HaltedSessions   int `json:"halted_sessions"`

	// --- liquidity, in shares / trades / RIALS ---
	LastVolume      *int64   `json:"last_volume"`
	LastTradeCount  *int64   `json:"last_trade_count"`
	LastValue       *float64 `json:"last_value"`
	AvgVolume       *float64 `json:"avg_volume"`
	AvgTradeCount   *float64 `json:"avg_trade_count"`
	AvgValue        *float64 `json:"avg_value"`
	LiquidityReason *string  `json:"liquidity_reason"`

	// --- the last price, re-expressed ---
	// Both are computed from LastClose on LastTradeDate through
	// internal/relvalue, whatever ?numeraire= asked for: they are columns, not
	// the basis, and a screener reader comparing a share to gold wants both
	// beside each other.
	PriceUSD                     *float64 `json:"price_usd"`
	PriceUSDRateDate             *string  `json:"price_usd_rate_date"`
	PriceUSDCarriedForward       bool     `json:"price_usd_carried_forward"`
	PriceUSDReason               *string  `json:"price_usd_reason"`
	PriceGoldGrams               *float64 `json:"price_gold_grams"`
	PriceGoldGramsRateDate       *string  `json:"price_gold_grams_rate_date"`
	PriceGoldGramsCarriedForward bool     `json:"price_gold_grams_carried_forward"`
	PriceGoldGramsReason         *string  `json:"price_gold_grams_reason"`

	// Adjustment is the SAME block /stocks and /stocks/{symbol}/bars publish,
	// deliberately: "which arithmetic produced this number" must not have three
	// spellings across three endpoints. The action count is `actions_applied`.
	Adjustment adjustmentItem `json:"adjustment"`
	// Conversion is null for numeraire=IRR, where nothing is converted.
	Conversion    *conversionBlock `json:"conversion"`
	AbsentMetrics []absentMetric   `json:"absent_metrics"`
	Notes         []string         `json:"notes"`
}

// excludedItem is one roster symbol that is NOT in `items`, with why.
type excludedItem struct {
	Symbol     string `json:"symbol"`
	InsCode    string `json:"ins_code"`
	NameFA     string `json:"name_fa"`
	SectorCode string `json:"sector_code"`
	SectorFA   string `json:"sector_fa"`
	// ReasonCode is the machine-readable form: disabled | adjustment_refused |
	// adjustment_never_ingested | no_bars.
	ReasonCode string `json:"reason_code"`
	Reason     string `json:"reason"`
	Enabled    bool   `json:"enabled"`
	BarCount   int    `json:"bar_count"`
	// Notes is the roster's OWN recorded note for this instrument. It is the
	// field that carries کچاد's measurement -- the block trade of 227,000,010
	// shares that set a closing price of 5,576 against a market of ~14,090 --
	// so the exclusion arrives with the evidence behind it rather than with a
	// status word.
	Notes      string         `json:"notes"`
	Adjustment adjustmentItem `json:"adjustment"`
}

const (
	reasonDisabled       = "disabled"
	reasonRefused        = "adjustment_refused"
	reasonNeverIngested  = "adjustment_never_ingested"
	reasonNoBars         = "no_bars"
	reasonNoValidVerdict = "adjustment_unavailable"
)

// priceBasisBlock states, once, what every number on this response is made of.
// It is not documentation moved onto the wire for its own sake: `last_close`
// and `return_pct` are computed from different series, and a client that
// assumes otherwise draws a chart that disagrees with its own table.
type priceBasisBlock struct {
	Currency          string `json:"currency"`
	CloseField        string `json:"close_field"`
	LastCloseAdjusted bool   `json:"last_close_adjusted"`
	ReturnsAdjusted   bool   `json:"returns_adjusted"`
	ReturnBasis       string `json:"return_basis"`
	VolatilityBasis   string `json:"volatility_basis"`
	DrawdownBasis     string `json:"drawdown_basis"`
	LiquidityBasis    string `json:"liquidity_basis"`
	SessionBasis      string `json:"session_basis"`
}

type screenResponse struct {
	AsOf   time.Time `json:"as_of"`
	Period string    `json:"period"`
	// From is null for period=max, where the window's start is a property of
	// each instrument and is reported per row as metrics_from.
	From *string `json:"from"`
	To   string  `json:"to"`

	Numeraire string `json:"numeraire"`
	// NumeraireSeries is the backing series' identity and reach, in the same
	// shape /api/v1/markets/performance publishes. Null for IRR, which is the
	// exchange's own unit and needs no series.
	NumeraireSeries *relvalue.NumeraireInfo `json:"numeraire_series"`

	// Currency is IRR, and it is stated on EVERY response because the rest of
	// this API reports Iranian amounts in TOMAN. A client that assumes the
	// house convention is off by a factor of ten.
	Currency     string          `json:"currency"`
	CurrencyNote string          `json:"currency_note"`
	PriceBasis   priceBasisBlock `json:"price_basis"`

	Sort   string  `json:"sort"`
	Order  string  `json:"order"`
	Limit  int     `json:"limit"`
	Sector *string `json:"sector"`
	// Sectors is the roster's sector vocabulary, so a client can build its
	// filter from what exists rather than from a hard-coded list that a new
	// listing would silently invalidate.
	Sectors []sectorOption `json:"sectors"`

	Items []screenItem `json:"items"`
	Count int          `json:"count"`
	// Matched is how many rows the filters produced BEFORE ?limit=, so a
	// truncated page cannot be mistaken for a complete one.
	Matched   int  `json:"matched"`
	Truncated bool `json:"truncated"`

	// Excluded names every roster symbol in the requested universe that is NOT
	// in items, with why. items + excluded is the whole universe the caller
	// asked for; a screen whose omissions cannot be reconstructed from its own
	// response quietly decides what a reader may consider.
	Excluded      []excludedItem `json:"excluded"`
	ExcludedCount int            `json:"excluded_count"`
	// ExcludedNote explains what the block is, because a client that ignores
	// it is not showing a shorter list -- it is showing a different market.
	ExcludedNote string `json:"excluded_note"`

	Warnings []string `json:"warnings"`
}

// --- statistics ----------------------------------------------------------------

// fp converts a possibly non-finite float to a JSON-friendly *float64,
// rounding to six decimals. Same helper, same rounding, as
// internal/prices/calc.go and internal/relvalue. It matters more than it
// looks: encoding/json REFUSES to encode a NaN and httpserver.JSON discards the
// encoder's error, so one NaN reaching the payload truncates the response body
// mid-object.
func fp(v float64) *float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	r := math.Round(v*1e6) / 1e6
	return &r
}

func floatOf(v float64) *float64 { return &v }

func floatOfInt(v *int64) *float64 {
	if v == nil {
		return nil
	}
	f := float64(*v)
	return &f
}

func strPtr(s string) *string {
	if s == "" {
		return nil
	}
	v := s
	return &v
}

// sessionReturns differences a converted close series session to session.
//
// A pair whose base is non-positive is SKIPPED rather than producing an
// infinity: final_close carries a CHECK (> 0) in migration 0028, so this is
// unreachable on stored data and is handled anyway, because a single infinity
// in the payload truncates the JSON body.
func sessionReturns(points []relvalue.Point) []float64 {
	out := make([]float64, 0, len(points))
	for i := 1; i < len(points); i++ {
		prev, cur := points[i-1].Value, points[i].Value
		if prev <= 0 {
			continue
		}
		r := cur/prev - 1
		if math.IsNaN(r) || math.IsInf(r, 0) {
			continue
		}
		out = append(out, r)
	}
	return out
}

// screenReturnPct is the simple return from the first covered session to the
// last, in percent. The second return is the REASON it could not be computed,
// non-empty exactly when the figure is nil.
func screenReturnPct(points []relvalue.Point) (*float64, string) {
	if len(points) < 2 {
		return nil, fmt.Sprintf(
			"a return needs a start and an end, and this window covers %d session(s) of "+
				"this instrument", len(points))
	}
	start, end := points[0].Value, points[len(points)-1].Value
	if start <= 0 {
		return nil, fmt.Sprintf(
			"the first covered session (%s) has a non-positive value, so there is no base to "+
				"measure a return from", dateString(points[0].Day))
	}
	return fp((end/start - 1) * 100), ""
}

// screenVolatilityPct is the SAMPLE standard deviation of simple
// session-over-session returns, in percent, and it is NOT ANNUALISED.
//
// Not annualising is a correction this repository has already made once: a
// card that multiplied a per-step figure by sqrt(252) implied a trading-day
// calendar that none of these symbols keep. Tehran trades SATURDAY TO
// WEDNESDAY, which is not 252 days; شپنا alone carries 879 halted sessions, so
// even the count of real observations differs per instrument. There is no
// single N for which sqrt(N) is right here, so no N is applied and the field
// says what it is.
//
// One observation is one TRADED session. Halted sessions are excluded upstream
// because their final_close is the carried reference price, not a print, and
// including them would inject 0% returns that dampen the figure by however
// often the instrument was suspended.
//
// The sample (n-1) denominator means a single return yields nil rather than
// 0.0. A stored zero would read as "this share did not move".
func screenVolatilityPct(points []relvalue.Point) (*float64, string) {
	rets := sessionReturns(points)
	if len(rets) < 2 {
		return nil, fmt.Sprintf(
			"a sample standard deviation needs at least two session returns and this window "+
				"has %d; a single return carries no information about dispersion at all, and "+
				"0.00 would read as \"this share did not move\"", len(rets))
	}
	mean := 0.0
	for _, r := range rets {
		mean += r
	}
	mean /= float64(len(rets))
	ss := 0.0
	for _, r := range rets {
		d := r - mean
		ss += d * d
	}
	return fp(math.Sqrt(ss/float64(len(rets)-1)) * 100), ""
}

// screenMaxDrawdownPct is the largest peak-to-trough decline over the window,
// in percent, as a NEGATIVE number -- 0 for a series that never closed below a
// previous peak, which is a real measurement and not a missing one.
//
// Deliberately not indicators.DrawdownPct, which answers a different question:
// how far below its trailing high the series sits RIGHT NOW. A share that fell
// 40% and fully recovered has a max drawdown of -40% and a current drawdown of
// 0%, and this field is named for the first of those.
func screenMaxDrawdownPct(points []relvalue.Point) (*float64, string) {
	if len(points) < 2 {
		return nil, fmt.Sprintf(
			"a drawdown needs a peak and a trough, and this window covers %d session(s) of "+
				"this instrument", len(points))
	}
	peak := math.Inf(-1)
	worst := 0.0
	for _, p := range points {
		if p.Value > peak {
			peak = p.Value
		}
		if peak <= 0 {
			continue
		}
		if d := (p.Value/peak - 1) * 100; d < worst {
			worst = d
		}
	}
	if math.IsInf(peak, -1) {
		return nil, "no covered session carries a usable close"
	}
	return fp(worst), ""
}

func dateString(t time.Time) string { return t.UTC().Format(dateLayout) }

func datePtr(t time.Time) *string {
	s := dateString(t)
	return &s
}

// --- pure assembly ----------------------------------------------------------------

// screenInputs is everything the response is built from, already fetched.
// Separating it from the handler is what makes the adjusted-return arithmetic,
// the not-annualised volatility, every refusal path and the excluded block
// testable without a database.
type screenInputs struct {
	Query screenQuery
	// Roster is the WHOLE roster, disabled instruments included. The excluded
	// block cannot be built from a pre-filtered list: a symbol that was
	// filtered out before this function ran is a symbol it cannot explain.
	Roster []stockRow
	// Window holds the bars inside the requested window, keyed by ins_code and
	// ascending by trade_date.
	Window map[string][]screenBarRow
	// Latest holds the last TRADED bar per ins_code, whatever the window. It is
	// read separately so `last_close` is the instrument's real last print even
	// when a short window contains no session at all.
	Latest map[string]screenBarRow
	// Converter is nil only when this deployment could not load the numeraire
	// engine; every converted figure is then null with that reason.
	Converter *relvalue.Converter
	Sectors   []sectorOption
}

// screenable reports whether an instrument may appear in `items`, and the
// exclusion when it may not.
//
// The gate is docs/REDESIGN.md's P3 rule at the read edge: "adjustment must be
// validated before any return, ratio or score is computed from it". Every
// figure in `items` except last_close IS such a number, so an instrument
// without a validated verdict is excluded from the screen entirely rather than
// listed with nulls -- a row of nulls in a sortable table is a row a reader
// scrolls past, and the reason never reaches them.
func screenable(r stockRow) (excludedItem, bool) {
	adj := buildAdjustment(r)
	out := excludedItem{
		Symbol: r.SymbolFA, InsCode: r.InsCode, NameFA: r.NameFA,
		SectorCode: r.SectorCode, SectorFA: r.SectorFA,
		Enabled: r.Enabled, BarCount: r.BarCount, Notes: r.Notes,
		Adjustment: adj,
	}
	switch {
	case !r.Enabled:
		out.ReasonCode = reasonDisabled
		out.Reason = fmt.Sprintf(
			"%s is disabled in the roster, so it is not screened. The instrument's own "+
				"`notes` record the measurement behind that decision.", r.SymbolFA)
		if adj.Status != "" && adj.Status != statusValidated {
			out.Reason += fmt.Sprintf(" Its adjustment verdict is %q.", adj.Status)
		}
		if adj.RefusalReason != "" {
			out.Reason += " " + adj.RefusalReason
		}
		return out, false
	case r.BarCount == 0:
		out.ReasonCode = reasonNoBars
		out.Reason = fmt.Sprintf(
			"%s is enabled but carries no stored bar, so there is nothing to measure. "+
				"\"Registered but never collected\" is a real state and is reported as one "+
				"rather than as an absence.", r.SymbolFA)
		return out, false
	case adj.Status == statusNeverIngested:
		out.ReasonCode = reasonNeverIngested
		out.Reason = fmt.Sprintf(
			"no corporate-action adjustment has ever been computed for %s, and every return "+
				"in this table is computed from adjusted closes. Raw Tehran closes would rank "+
				"this instrument by how recently it did a capital increase, so it is excluded "+
				"rather than ranked wrongly.", r.SymbolFA)
		return out, false
	case !adj.Servable:
		out.ReasonCode = reasonRefused
		reason := fmt.Sprintf(
			"the adjustment validation gate refused %s (status %q), and this table computes "+
				"every return from adjusted closes. A missed corporate action corrupts every "+
				"return drawn from the series, not only the day it landed on.",
			r.SymbolFA, adj.Status)
		if adj.RefusalReason != "" {
			reason += " " + adj.RefusalReason
		}
		out.ReasonCode = reasonRefused
		out.Reason = reason
		if adj.Status == "" {
			out.ReasonCode = reasonNoValidVerdict
		}
		return out, false
	}
	return excludedItem{}, true
}

// convertedCloses turns a window of bars into the series the period metrics
// are measured on: TRADED sessions only, adjusted, and re-expressed in the
// requested unit of account.
//
// THE x10 LIVES HERE, and nowhere else in this package. TSETMC quotes RIAL;
// internal/relvalue is a toman engine that states of itself that rial "never
// appears in this package at all". So the division happens on this side of the
// door, once, in view of the exchange that caused it. It does not touch
// return_pct, volatility_pct or max_drawdown_pct -- all three are ratios and a
// constant factor cancels out of every one -- but it is the whole difference
// between a correct price_usd and one that is wrong by an order of magnitude.
func convertedCloses(bars []screenBarRow, numeraire string,
	conv *relvalue.Converter) ([]relvalue.Point, *conversionBlock, string) {
	rial := make([]relvalue.Point, 0, len(bars))
	for _, b := range bars {
		if !b.Traded() {
			continue
		}
		rial = append(rial, relvalue.Point{Day: b.TradeDate, Value: b.AdjustedClose()})
	}
	if numeraire == numeraireIRR {
		return rial, nil, ""
	}
	if conv == nil {
		return nil, nil, "the numeraire engine is not available in this deployment, so no " +
			"figure can be expressed in a unit other than rial"
	}
	toman := make([]relvalue.Point, len(rial))
	for i, p := range rial {
		toman[i] = relvalue.Point{Day: p.Day, Value: p.Value / rialsPerToman}
	}
	sc := conv.ConvertTomanSeries(toman, relvalueKey(numeraire))
	block := &conversionBlock{
		Chain:                   sc.Chain,
		CarriedForwardDays:      sc.CarriedForward,
		DroppedNoPriorQuote:     sc.DroppedNoPriorQuote,
		DroppedNonPositiveQuote: sc.DroppedNonPositiveQuote,
	}
	if sc.Chain == nil {
		block.Chain = []string{}
	}
	return sc.Points, block, sc.Reason
}

// relvalueKey maps this endpoint's numeraire vocabulary onto
// internal/relvalue's. Only the Iranian unit differs, and it differs for a
// reason worth keeping: this endpoint's figures are RIALS because TSETMC
// quotes rial, and that package's hub is TOMAN because every other Iranian
// value in this system is stored in toman.
func relvalueKey(numeraire string) string {
	switch numeraire {
	case numeraireUSD:
		return relvalue.NumeraireUSD
	case numeraireGold:
		return relvalue.NumeraireGold
	default:
		return relvalue.NumeraireToman
	}
}

// priceIn re-expresses one RAW rial close, on the session it printed, in a
// numeraire. The reason is non-empty exactly when the value is nil.
func priceIn(rial float64, day time.Time, key string,
	conv *relvalue.Converter) (*float64, *string, *string, bool) {
	if conv == nil {
		return nil, nil, strPtr("the numeraire engine is not available in this deployment"), false
	}
	got := conv.ConvertToman(day, rial/rialsPerToman, key)
	if got.Value == nil {
		return nil, nil, strPtr(got.Reason), false
	}
	var rateDate *string
	if got.RateDay != nil {
		rateDate = datePtr(*got.RateDay)
	}
	return got.Value, rateDate, nil, got.CarriedForward
}

// buildScreenItem projects one instrument onto the contract. Pure function
// (unit tested): no clock, no database.
func buildScreenItem(meta stockRow, bars []screenBarRow, latest *screenBarRow,
	q screenQuery, conv *relvalue.Converter) screenItem {
	item := screenItem{
		Symbol: meta.SymbolFA, InsCode: meta.InsCode, NameFA: meta.NameFA,
		SectorCode: meta.SectorCode, SectorFA: meta.SectorFA,
		Market: meta.Market, Board: meta.Board,
		LastCloseBasis:   "raw final_close (TSE's official قیمت پایانی), in rials, unadjusted",
		BarCount:         meta.BarCount,
		FirstBar:         strPtr(formatDate(meta.FirstBar)),
		LastBar:          strPtr(formatDate(meta.LastBar)),
		MetricsNumeraire: q.Numeraire,
		Adjustment:       buildAdjustment(meta),
		AbsentMetrics:    screenAbsentMetrics,
		Notes:            []string{},
	}

	// --- the last print --------------------------------------------------
	if latest != nil {
		item.LastClose = fp(latest.FinalClose)
		item.LastTradeDate = datePtr(latest.TradeDate)
		v, tc := latest.Volume, latest.TradeCount
		item.LastVolume, item.LastTradeCount = &v, &tc
		item.LastValue = fp(latest.Value)

		usd, usdDate, usdReason, usdCarried := priceIn(
			latest.FinalClose, latest.TradeDate, relvalue.NumeraireUSD, conv)
		item.PriceUSD, item.PriceUSDRateDate = usd, usdDate
		item.PriceUSDReason, item.PriceUSDCarriedForward = usdReason, usdCarried

		gold, goldDate, goldReason, goldCarried := priceIn(
			latest.FinalClose, latest.TradeDate, relvalue.NumeraireGold, conv)
		item.PriceGoldGrams, item.PriceGoldGramsRateDate = gold, goldDate
		item.PriceGoldGramsReason, item.PriceGoldGramsCarriedForward = goldReason, goldCarried

		if meta.LastBar != nil && meta.LastBar.After(latest.TradeDate) {
			item.Notes = append(item.Notes, fmt.Sprintf(
				"the last stored session (%s) is later than the last TRADED session (%s): "+
					"this instrument is currently halted, and last_close is the last real "+
					"print rather than the carried reference price.",
				formatDate(meta.LastBar), dateString(latest.TradeDate)))
		}
	} else {
		reason := strPtr("this instrument has no traded session in the store: every stored " +
			"bar is a halt or a pre-listing placeholder, so there is no print to report")
		item.LastCloseBasis = "no traded session"
		item.PriceUSDReason, item.PriceGoldGramsReason = reason, reason
	}

	// --- the window ------------------------------------------------------
	item.SessionsInPeriod = len(bars)
	for _, b := range bars {
		if b.Traded() {
			item.TradedSessions++
		}
	}
	item.HaltedSessions = item.SessionsInPeriod - item.TradedSessions

	points, block, convReason := convertedCloses(bars, q.Numeraire, conv)
	item.Conversion = block

	if convReason != "" {
		r := strPtr(convReason)
		item.ReturnReason, item.VolatilityReason, item.MaxDrawdownReason = r, r, r
	} else {
		item.MetricsObservations = len(points)
		if len(points) > 0 {
			item.MetricsFrom = datePtr(points[0].Day)
			item.MetricsTo = datePtr(points[len(points)-1].Day)
		}
		ret, retReason := screenReturnPct(points)
		item.ReturnPct, item.ReturnReason = ret, strPtr(retReason)
		vol, volReason := screenVolatilityPct(points)
		item.VolatilityPct, item.VolatilityReason = vol, strPtr(volReason)
		dd, ddReason := screenMaxDrawdownPct(points)
		item.MaxDrawdownPct, item.MaxDrawdownReason = dd, strPtr(ddReason)
	}

	// --- liquidity over the window ---------------------------------------
	//
	// Averaged over TRADED sessions, not over stored ones. A halted session
	// contributes a zero to every liquidity column, and averaging those in
	// would report a lower turnover for a share that was suspended than for one
	// that simply traded thinly -- two different facts collapsed into one
	// number. Both denominators are published (sessions_in_period and
	// traded_sessions) so the reader can see which was used.
	if item.TradedSessions > 0 {
		var vol, trades, value float64
		for _, b := range bars {
			if !b.Traded() {
				continue
			}
			vol += float64(b.Volume)
			trades += float64(b.TradeCount)
			value += b.Value
		}
		n := float64(item.TradedSessions)
		item.AvgVolume, item.AvgTradeCount, item.AvgValue = fp(vol/n), fp(trades/n), fp(value/n)
	} else {
		item.LiquidityReason = strPtr(fmt.Sprintf(
			"no traded session in this window (%d stored session(s), all halted), so there is "+
				"nothing to average", item.SessionsInPeriod))
	}

	// --- notes -----------------------------------------------------------
	if item.HaltedSessions > 0 {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"%d of %d stored sessions in this window were halted (no trade). They are "+
				"excluded from the return, volatility, drawdown and liquidity figures, "+
				"because a halted bar's final_close is the carried reference price and not a "+
				"print.", item.HaltedSessions, item.SessionsInPeriod))
	}
	if block != nil && (block.DroppedNoPriorQuote > 0 || block.DroppedNonPositiveQuote > 0) {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"the %s conversion dropped %d session(s) with no quote at or before them and %d "+
				"with a non-positive quote. Nothing was interpolated: the only value that "+
				"could fill those days is a LATER quote, and using it would price a session "+
				"with information from its own future.",
			q.Numeraire, block.DroppedNoPriorQuote, block.DroppedNonPositiveQuote))
	}
	if block != nil && block.CarriedForwardDays > 0 {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"%d of %d converted sessions were priced with a %s quote carried forward from an "+
				"earlier day.", block.CarriedForwardDays, item.MetricsObservations, q.Numeraire))
	}
	if item.Adjustment.Reopenings > 0 {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"this instrument's adjusted series contains %d reopening auction(s) whose move "+
				"exceeds a normal session's price limit; the exchange lifts the limit when an "+
				"instrument resumes after a suspension, so a session return spanning one of "+
				"those days is not a session return.", item.Adjustment.Reopenings))
	}
	return item
}

// sortScreenItems applies the requested column and direction.
//
// A row whose metric is NULL sorts LAST in both directions, never as a zero.
// That is the sort-order face of the same rule the payload follows: a share
// whose volatility could not be measured is not the least volatile share, and
// a share with no return is not the worst performer.
func sortScreenItems(items []screenItem, spec screenSortSpec, desc bool) {
	sort.SliceStable(items, func(a, b int) bool {
		x, y := items[a], items[b]
		if spec.Metric == nil {
			if x.Symbol == y.Symbol {
				return false
			}
			if desc {
				return x.Symbol > y.Symbol
			}
			return x.Symbol < y.Symbol
		}
		xv, yv := spec.Metric(x), spec.Metric(y)
		switch {
		case xv == nil && yv == nil:
			return x.Symbol < y.Symbol
		case xv == nil:
			return false
		case yv == nil:
			return true
		}
		if *xv == *yv {
			return x.Symbol < y.Symbol
		}
		if desc {
			return *xv > *yv
		}
		return *xv < *yv
	})
}

// screenNumeraireRefusal is the 400 for a numeraire this deployment cannot
// BACK. Pure function (unit tested), given a converter.
//
// It is a refusal and not an empty column: a client that asked for gold terms
// and received a page of nulls cannot tell "gold did nothing" from "this
// server has never collected a gold price". Only the requested BASIS is
// refused this way -- the price_usd and price_gold_grams columns keep
// reporting their own per-row reason, because they are columns rather than the
// unit the table is measured in.
func screenNumeraireRefusal(numeraire string, conv *relvalue.Converter) *paramError {
	if numeraire == numeraireIRR || conv == nil {
		return nil
	}
	info, ok := conv.NumeraireInfo(relvalueKey(numeraire))
	if !ok || info.Available {
		return nil
	}
	reason := ""
	if info.UnavailableReason != nil {
		reason = *info.UnavailableReason
	}
	series := ""
	if info.Series != nil {
		series = *info.Series
	}
	return badParam(
		fmt.Sprintf("numeraire %s cannot be served by this deployment: %s", numeraire, reason),
		map[string]any{
			"numeraire": numeraire,
			"series":    series,
			"hint": "GET /api/v1/markets/numeraires reports which units of account this " +
				"deployment can back; ?numeraire=IRR always can",
		})
}

// buildScreenResponse assembles the whole payload. Pure function (unit
// tested): every input arrives already fetched, so the arithmetic, the
// refusals, the null-with-a-reason rule and the excluded block can all be
// asserted without a database.
func buildScreenResponse(in screenInputs) screenResponse {
	q := in.Query
	order := "asc"
	if q.Desc {
		order = "desc"
	}
	out := screenResponse{
		AsOf:      q.AsOf,
		Period:    q.Period,
		From:      nil,
		To:        dateString(q.To),
		Numeraire: q.Numeraire,
		Currency:  currencyRial,
		CurrencyNote: "Every amount on this response — last_close, last_value, avg_value — is " +
			"in RIALS (IRR), the unit TSETMC quotes. The rest of this API reports Iranian " +
			"amounts in TOMAN, so a client that assumes the house convention is off by a " +
			"factor of ten. price_usd and price_gold_grams are converted through the toman " +
			"hub, and the divide-by-ten is applied exactly once, on the way in.",
		PriceBasis: priceBasisBlock{
			Currency:          currencyRial,
			CloseField:        "final_close",
			LastCloseAdjusted: false,
			ReturnsAdjusted:   true,
			ReturnBasis: "simple return from the first to the last covered session, computed " +
				"from ADJUSTED closes (stored final_close multiplied by the cumulative " +
				"corporate-action factor). Raw Tehran closes would rank by how recently each " +
				"company did a capital increase: فولاد is x1.52 raw against x907.86 adjusted.",
			VolatilityBasis: "sample standard deviation of simple session-over-session returns, " +
				"in percent, NOT ANNUALISED. Tehran trades Saturday to Wednesday and halts are " +
				"common, so there is no single N for which sqrt(N) would be right.",
			DrawdownBasis: "largest peak-to-trough decline over the covered sessions, as a " +
				"negative percentage; 0 means the series never closed below a previous peak.",
			LiquidityBasis: "averaged over TRADED sessions only. A halted session contributes " +
				"zero to every liquidity column, and averaging those in would report lower " +
				"turnover for a suspended share than for a thinly traded one.",
			SessionBasis: "one observation is one traded Tehran session; halted sessions are " +
				"excluded because their final_close is the carried reference price, not a print.",
		},
		Sort:          q.Sort.Key,
		Order:         order,
		Limit:         q.Limit,
		Sector:        strPtr(q.Sector),
		Sectors:       in.Sectors,
		Items:         []screenItem{},
		Excluded:      []excludedItem{},
		ExcludedCount: 0,
		ExcludedNote: "Roster symbols in the requested universe that could NOT be screened, " +
			"with why. items + excluded is the whole universe: a screen whose omissions " +
			"cannot be reconstructed from its own response quietly decides what a reader may " +
			"consider.",
		Warnings: []string{},
	}
	if in.Sectors == nil {
		out.Sectors = []sectorOption{}
	}
	if q.From != nil {
		out.From = datePtr(*q.From)
	}

	if q.Numeraire != numeraireIRR && in.Converter != nil {
		if info, ok := in.Converter.NumeraireInfo(relvalueKey(q.Numeraire)); ok {
			out.NumeraireSeries = &info
		}
	}
	if q.Numeraire != numeraireIRR && in.Converter == nil {
		out.Warnings = append(out.Warnings,
			"the numeraire engine is not available in this deployment, so every figure that "+
				"would be expressed in "+q.Numeraire+" is null with that reason.")
	}

	for _, r := range in.Roster {
		if q.Sector != "" && r.SectorCode != q.Sector {
			// Not an exclusion. The caller narrowed the universe on purpose,
			// and reporting its own filter back as an omission would bury the
			// symbols that genuinely could not be screened.
			continue
		}
		if ex, ok := screenable(r); !ok {
			out.Excluded = append(out.Excluded, ex)
			continue
		}
		bars := in.Window[r.InsCode]
		var latest *screenBarRow
		if b, ok := in.Latest[r.InsCode]; ok {
			l := b
			latest = &l
		}
		out.Items = append(out.Items, buildScreenItem(r, bars, latest, q, in.Converter))
	}

	out.Matched = len(out.Items)
	sortScreenItems(out.Items, q.Sort, q.Desc)
	if len(out.Items) > q.Limit {
		out.Items = out.Items[:q.Limit]
		out.Truncated = true
	}
	out.Count = len(out.Items)
	out.ExcludedCount = len(out.Excluded)

	if out.Truncated {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"%d instruments matched and %d are shown: this page is truncated by ?limit=%d.",
			out.Matched, out.Count, q.Limit))
	}
	if out.ExcludedCount > 0 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"%d roster symbol(s) are not screened; `excluded` names each one and why.",
			out.ExcludedCount))
	}
	out.Warnings = append(out.Warnings,
		"P/E, EPS growth, ROE, dividend yield and market cap are NOT computable from what "+
			"this deployment ingests. Every row's `absent_metrics` names each one with the "+
			"real obstacle, so a page can state its boundary from this API rather than from "+
			"its own copy.")
	return out
}

// --- SQL and handler ----------------------------------------------------------------

// screenBarsSelect reads the window for EVERY requested instrument in ONE
// statement, resolving each bar's back-adjustment factor by the same
// correlated subquery barsSelect uses -- the rule from app/equities/adjust.py,
// stated in SQL: the cumulative factor of the first action effective strictly
// AFTER this bar, and 1.0 when none follows.
//
// One statement, not one per symbol. Twenty round trips for a twenty-row table
// is the reason this endpoint exists, and it would also invite the numeraire to
// be read over a different window than the bars it converts.
//
// The unnest carries each instrument's OWN adjustment_version and its adjusted
// first bar. The version matters because equity_adjustments is keyed on
// (ins_code, adjustment_version) precisely so a v2 can be computed beside v1;
// the first bar matters because an adjusted read must start at the
// instrument's first TRADED session -- before it, TSETMC serves placeholder
// bars at the 1,000-rial par value (161 of them for نوری), and a series that
// begins at par shows a +3,025% first day.
const screenBarsSelect = `
	SELECT b.ins_code, b.trade_date, b.final_close, b.volume, b.trade_count, b.value,
	       COALESCE((
	           SELECT c.cumulative_factor
	           FROM corporate_actions c
	           WHERE c.ins_code = b.ins_code
	             AND c.adjustment_version = v.adjustment_version
	             AND c.effective_date > b.trade_date
	           ORDER BY c.effective_date
	           LIMIT 1
	       ), 1.0) AS factor
	FROM equity_bars b
	JOIN unnest($1::text[], $2::text[], $3::date[])
	     AS v(ins_code, adjustment_version, adjusted_first_bar)
	  ON v.ins_code = b.ins_code
	WHERE ($4::date IS NULL OR b.trade_date >= $4)
	  AND (v.adjusted_first_bar IS NULL OR b.trade_date >= v.adjusted_first_bar)
	  AND b.trade_date <= $5::date
	ORDER BY b.ins_code, b.trade_date`

// screenLatestSelect is the last TRADED session per instrument, whatever the
// window.
//
// It is a second statement rather than a corner of the first because
// `last_close` answers a different question from the period metrics: a
// one-month screen run during a long suspension has no session in its window,
// and a screener whose price column empties out because the WINDOW was short
// is reporting on its own parameters rather than on the market. The halt
// filter is the same one barItem.traded publishes: volume or trade_count above
// zero.
const screenLatestSelect = `
	SELECT DISTINCT ON (b.ins_code)
	       b.ins_code, b.trade_date, b.final_close, b.volume, b.trade_count, b.value
	FROM equity_bars b
	WHERE b.ins_code = ANY($1)
	  AND (b.volume > 0 OR b.trade_count > 0)
	ORDER BY b.ins_code, b.trade_date DESC`

// rosterSectors builds the sector vocabulary from the roster itself, in
// publication order (code). Pure function (unit tested): hard-coding the list
// would make a new listing a code change, and would make a refusal message
// name sectors this deployment does not carry.
func rosterSectors(rows []stockRow) []sectorOption {
	index := map[string]*sectorOption{}
	order := []string{}
	for _, r := range rows {
		if r.SectorCode == "" {
			continue
		}
		if _, ok := index[r.SectorCode]; !ok {
			index[r.SectorCode] = &sectorOption{Code: r.SectorCode, NameFA: r.SectorFA}
			order = append(order, r.SectorCode)
		}
		index[r.SectorCode].Count++
	}
	sort.Strings(order)
	out := make([]sectorOption, 0, len(order))
	for _, code := range order {
		out = append(out, *index[code])
	}
	return out
}

// loadScreenRoster reads the WHOLE roster, disabled instruments included.
// Filtering happens in Go so the refusal messages can name what exists and the
// excluded block can explain what was left out -- the same reason
// internal/relvalue loads its instrument registry unfiltered.
func (h *Handler) loadScreenRoster(ctx context.Context) ([]stockRow, error) {
	var noEnabled *bool
	var noSector *string
	rows, err := h.Pool.Query(ctx, stocksSelect, noEnabled, noSector)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []stockRow{}
	for rows.Next() {
		s, err := scanStock(rows)
		if err != nil {
			return nil, err
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// Screen implements
// GET /api/v1/stocks/screen?period=&numeraire=&sector=&sort=&order=&limit=.
func (h *Handler) Screen(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	now := time.Now().UTC()

	roster, err := h.loadScreenRoster(ctx)
	if err != nil {
		h.Log.Error("screen_roster", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	sectors := rosterSectors(roster)

	q, perr := parseScreenQuery(r.URL.Query(), sectors, now)
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}

	// The instruments that will actually be screened, so the bar query reads
	// no bars for a symbol the gate excludes. screenable is consulted here and
	// again inside buildScreenResponse -- it is a pure predicate over a roster
	// row, and calling it twice is what lets the response builder stay a pure
	// function over the WHOLE roster and still explain every omission.
	insCodes := []string{}
	versions := []string{}
	firstBars := []*time.Time{}
	for _, row := range roster {
		if q.Sector != "" && row.SectorCode != q.Sector {
			continue
		}
		if _, ok := screenable(row); !ok {
			continue
		}
		version := ""
		if row.AdjustmentVersion != nil {
			version = *row.AdjustmentVersion
		}
		insCodes = append(insCodes, row.InsCode)
		versions = append(versions, version)
		firstBars = append(firstBars, row.AdjustedFirstBar)
	}

	window := map[string][]screenBarRow{}
	latest := map[string]screenBarRow{}
	if len(insCodes) > 0 {
		window, err = h.loadScreenWindow(ctx, insCodes, versions, firstBars, q)
		if err != nil {
			h.Log.Error("screen_bars", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		latest, err = h.loadScreenLatest(ctx, insCodes)
		if err != nil {
			h.Log.Error("screen_latest", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
	}

	// The numeraire engine is loaded ONCE for the whole screen: twenty symbols
	// share one read of USD_IRT and IR_GOLD_18K.
	conv, err := relvalue.NewConverter(ctx, h.Pool, now)
	if err != nil {
		h.Log.Error("screen_numeraire", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if perr := screenNumeraireRefusal(q.Numeraire, conv); perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}

	httpserver.JSON(w, http.StatusOK, buildScreenResponse(screenInputs{
		Query: q, Roster: roster, Window: window, Latest: latest,
		Converter: conv, Sectors: sectors,
	}))
}

func (h *Handler) loadScreenWindow(ctx context.Context, insCodes, versions []string,
	firstBars []*time.Time, q screenQuery) (map[string][]screenBarRow, error) {
	rows, err := h.Pool.Query(ctx, screenBarsSelect,
		insCodes, versions, firstBars, q.From, q.To)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][]screenBarRow{}
	for rows.Next() {
		var b screenBarRow
		if err := rows.Scan(&b.InsCode, &b.TradeDate, &b.FinalClose, &b.Volume,
			&b.TradeCount, &b.Value, &b.Factor); err != nil {
			return nil, err
		}
		b.TradeDate = b.TradeDate.UTC()
		out[b.InsCode] = append(out[b.InsCode], b)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// The statement already returns each instrument ascending by trade_date.
	// Re-sorting makes the ordering the arithmetic relies on a property of the
	// Go rather than a promise from an ORDER BY no unit test can execute --
	// the same reason loadDailySeries re-sorts in internal/relvalue.
	for code := range out {
		bars := out[code]
		sort.SliceStable(bars, func(i, j int) bool {
			return bars[i].TradeDate.Before(bars[j].TradeDate)
		})
		out[code] = bars
	}
	return out, nil
}

func (h *Handler) loadScreenLatest(ctx context.Context, insCodes []string) (map[string]screenBarRow, error) {
	rows, err := h.Pool.Query(ctx, screenLatestSelect, insCodes)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string]screenBarRow{}
	for rows.Next() {
		var b screenBarRow
		if err := rows.Scan(&b.InsCode, &b.TradeDate, &b.FinalClose, &b.Volume,
			&b.TradeCount, &b.Value); err != nil {
			return nil, err
		}
		b.TradeDate = b.TradeDate.UTC()
		// The latest bar carries no adjustment factor and needs none: no
		// corporate action follows the newest bar, so its factor is 1.0 by
		// construction, which is what "back-adjusted" means.
		b.Factor = 1.0
		out[b.InsCode] = b
	}
	return out, rows.Err()
}
