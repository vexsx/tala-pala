// Package equities serves the Tehran equity roster and its daily bars.
//
// This package READS. The bars are ingested by prediction-python, which owns
// the corporate-action detection and the chaining, and nothing here re-derives
// either — a second implementation of that arithmetic would be a second,
// silently diverging answer to "what is فولاد's history?". What this package
// computes is one multiplication per bar: `final_close * cumulative_factor`,
// where the factor is a stored number on a `corporate_actions` row. That is
// deliberate and is the whole reason migration 0028 stores a factor per action
// (31 numbers for فولاد) instead of materialising an adjusted copy of 4,636
// bars per adjustment version.
//
// The gate this package enforces.
//
// docs/REDESIGN.md makes the P3 gate explicit: "adjustment must be validated
// before any return, ratio or score is computed from it." The verdict lives in
// `equity_adjustments`, one row per (instrument, adjustment_version), and an
// adjusted read is REFUSED — 409, with the reason on it — when that row does
// not say `validated`. A refused symbol still has raw bars and detected
// actions in the database, and ?adjusted=false still serves them; what is
// withheld is the adjusted number, because a missed corporate action corrupts
// every return computed from the series rather than only the day it landed on.
//
// The default is ?adjusted=true. The raw close series is not a price history —
// فولاد's raw close is x1.52 over nineteen years against x907.86 adjusted —
// so a caller who does not say gets the number that means something, and a
// caller who wants the exchange's printed prices asks for them.
package equities

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Handler serves /api/v1/stocks and /api/v1/stocks/{symbol}/bars.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger
}

const (
	// defaultBarLimit is about a year of Tehran sessions (the exchange trades
	// roughly 240 days a year), so the common "draw me a chart" read needs no
	// paging.
	defaultBarLimit = 250
	// maxBarLimit is above the longest history in the roster (خودرو, 5,874
	// bars from 2001), so a caller can ask for a whole instrument in one page
	// and the cap only stops accidents.
	maxBarLimit = 6000
)

// dateLayout matches internal/economic: trade_date is a DATE column, and
// serialising it as a timestamp would invent a time of day and a timezone the
// exchange never stated.
const dateLayout = "2006-01-02"

// statusValidated mirrors app/equities/adjust.py's STATUS_VALIDATED and the
// CHECK constraint in migration 0028. Compared as a string rather than assumed
// to be the only value: 'refused' is the other one and it must not be served.
const statusValidated = "validated"

// --- symbol folding ----------------------------------------------------------
//
// The mirror of app.equities.adjust.fold_symbol, applied to the {symbol} path
// parameter so both spellings of a Tehran symbol reach the same row.
//
// It is not cosmetic. TSETMC serves فملي with ARABIC YEH (U+064A) and كچاد
// with ARABIC KAF (U+0643); a Persian keyboard types PERSIAN YEH (U+06CC) and
// PERSIAN KEHEH (U+06A9). The two spellings render identically and compare
// unequal, so without this a user typing the symbol they can see gets a 404
// for an instrument that is right there.
//
// Deliberately NOT golang.org/x/text/unicode/norm: NFKC would make this a
// direct dependency for a fold whose whole content is four letters and five
// invisible marks, all of them listed here where they can be read. Python
// applies NFKC before the same table; the difference is unreachable for
// Tehran symbols, which are short Arabic-script tokens with no compatibility
// characters in them.
var symbolFolds = map[rune]rune{
	'\u0643': '\u06a9', // ARABIC KAF     -> PERSIAN KEHEH   (ك -> ک)
	'\u064a': '\u06cc', // ARABIC YEH     -> PERSIAN YEH     (ي -> ی)
	'\u0649': '\u06cc', // ALEF MAKSURA   -> PERSIAN YEH     (ى -> ی)
	'\u0629': '\u0647', // TEH MARBUTA    -> HEH             (ة -> ه)
}

// symbolDrops are invisible in the rendered symbol and fatal to equality. ZWNJ
// is dropped HERE, in the key, and kept in a company NAME, where it separates
// words — the same split Python makes for the same reason.
var symbolDrops = map[rune]bool{
	'\u200c': true, // ZERO WIDTH NON-JOINER
	'\u200e': true, // LEFT-TO-RIGHT MARK
	'\u200f': true, // RIGHT-TO-LEFT MARK
	'\u00a0': true, // NO-BREAK SPACE
	'\ufeff': true, // BYTE ORDER MARK
}

func foldSymbol(raw string) string {
	var b strings.Builder
	b.Grow(len(raw))
	for _, r := range raw {
		if symbolDrops[r] {
			continue
		}
		if folded, ok := symbolFolds[r]; ok {
			r = folded
		}
		b.WriteRune(r)
	}
	return strings.TrimSpace(b.String())
}

// --- refusals ----------------------------------------------------------------

type paramError struct {
	Message string
	Details map[string]any
}

func (e *paramError) Error() string { return e.Message }

func badParam(message string, details map[string]any) *paramError {
	return &paramError{Message: message, Details: details}
}

// parseBoolParam refuses an unknown value rather than reading it as false. An
// ?adjusted=treu that quietly meant false would serve raw prices under a
// contract that promises adjusted ones, and raw prices are wrong by a factor
// of 600 on the symbol this system was validated against.
func parseBoolParam(name, raw string) (bool, *paramError) {
	switch strings.ToLower(raw) {
	case "1", "true", "yes":
		return true, nil
	case "0", "false", "no":
		return false, nil
	}
	return false, badParam(
		fmt.Sprintf("%s must be 0 or 1, got %q", name, raw),
		map[string]any{name: raw})
}

// parseDate reads a ?from/?to bound. A calendar date is the primary form; an
// RFC3339 timestamp is accepted (its UTC date is used) so a caller holding one
// is not stuck. Bare integers are refused for the same reason
// internal/economic refuses them: in a date field an integer is far more
// likely a typo'd year than an epoch, and guessing is the silent substitution
// this codebase forbids.
func parseDate(name, raw string) (time.Time, *paramError) {
	if d, err := time.Parse(dateLayout, raw); err == nil {
		return d.UTC(), nil
	}
	if t, err := time.Parse(time.RFC3339, raw); err == nil {
		u := t.UTC()
		return time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC), nil
	}
	return time.Time{}, badParam(
		fmt.Sprintf("%s must be a date (YYYY-MM-DD) or RFC3339, got %q", name, raw),
		map[string]any{name: raw})
}

// unknownSymbol writes the 404, with no substitution of any kind.
//
// There is deliberately no nearest-match, no case-folded second attempt beyond
// the orthographic fold above, and no fallback to another instrument. Two
// Tehran symbols can differ by one letter and be different companies, and a
// chart drawn from the wrong company's bars is indistinguishable from a chart
// drawn from the right one's.
func unknownSymbol(w http.ResponseWriter, symbol string) {
	httpserver.Error(w, http.StatusNotFound, "not_found",
		fmt.Sprintf("%q is not a known Tehran equity in this deployment", symbol),
		map[string]any{
			"symbol": symbol,
			"hint":   "GET /api/v1/stocks lists every symbol this deployment carries",
		})
}

// --- the roster --------------------------------------------------------------

// stockRow is equity_instruments left-joined to its adjustment verdict.
type stockRow struct {
	InsCode        string
	SymbolFA       string
	NameFA         string
	Market         string
	Board          string
	SectorCode     string
	SectorFA       string
	ISIN           string
	InstrumentCode *string
	FirstBar       *time.Time
	LastBar        *time.Time
	BarCount       int
	Enabled        bool
	Notes          string

	// From equity_adjustments. All nil when the instrument has never been
	// ingested, which is a different state from "ingested and refused" and is
	// reported as such.
	AdjustmentVersion *string
	AdjustmentStatus  *string
	ActionsApplied    *int
	Reopenings        *int
	PreListingBars    *int
	WorstReturn       *float64
	RefusalReason     *string
	ComputedAt        *time.Time
	// The first bar of the ADJUSTED series, which is the instrument's first
	// TRADED session and not necessarily its first stored bar. نوری has 1,876
	// bars from 2018-11-10 and first traded on 2019-07-13; the 161 in between
	// are placeholders at the 1,000-rial par value.
	AdjustedFirstBar *time.Time
}

// adjustmentItem is the verdict as a client sees it. It travels on the roster
// AND on every bar response, because "which arithmetic produced this number"
// is not metadata a caller should have to make a second request for.
type adjustmentItem struct {
	Version string `json:"version"`
	// validated | refused | never_ingested
	Status         string   `json:"status"`
	ActionsApplied int      `json:"actions_applied"`
	Reopenings     int      `json:"reopenings"`
	WorstReturn    *float64 `json:"worst_session_return,omitempty"`
	// Bars TSETMC served before the instrument's first trade, at the par
	// value, which the adjusted series excludes. Reported because otherwise
	// an adjusted read of نوری is 161 bars shorter than the roster's
	// bar_count with nothing on the wire explaining the difference.
	PreListingBars   int    `json:"pre_listing_bars,omitempty"`
	AdjustedFirstBar string `json:"adjusted_first_bar,omitempty"`
	RefusalReason    string `json:"refusal_reason,omitempty"`
	ComputedAt       string `json:"computed_at,omitempty"`
	// Whether an adjusted read is available for this instrument at all. False
	// means ?adjusted=true is a 409, never a quietly-raw series.
	Servable bool `json:"adjusted_servable"`
}

const statusNeverIngested = "never_ingested"

func buildAdjustment(row stockRow) adjustmentItem {
	if row.AdjustmentStatus == nil {
		return adjustmentItem{Status: statusNeverIngested}
	}
	item := adjustmentItem{
		Status:   *row.AdjustmentStatus,
		Servable: *row.AdjustmentStatus == statusValidated,
	}
	if row.AdjustmentVersion != nil {
		item.Version = *row.AdjustmentVersion
	}
	if row.ActionsApplied != nil {
		item.ActionsApplied = *row.ActionsApplied
	}
	if row.Reopenings != nil {
		item.Reopenings = *row.Reopenings
	}
	if row.PreListingBars != nil {
		item.PreListingBars = *row.PreListingBars
	}
	item.AdjustedFirstBar = formatDate(row.AdjustedFirstBar)
	item.WorstReturn = row.WorstReturn
	if row.RefusalReason != nil {
		item.RefusalReason = *row.RefusalReason
	}
	if row.ComputedAt != nil {
		item.ComputedAt = row.ComputedAt.UTC().Format(time.RFC3339)
	}
	return item
}

type stockItem struct {
	InsCode        string         `json:"ins_code"`
	Symbol         string         `json:"symbol"`
	Name           string         `json:"name_fa"`
	Market         string         `json:"market"`
	Board          string         `json:"board"`
	SectorCode     string         `json:"sector_code"`
	Sector         string         `json:"sector_fa"`
	ISIN           string         `json:"isin"`
	InstrumentCode string         `json:"instrument_code,omitempty"`
	FirstBar       string         `json:"first_bar,omitempty"`
	LastBar        string         `json:"last_bar,omitempty"`
	BarCount       int            `json:"bar_count"`
	Enabled        bool           `json:"enabled"`
	Notes          string         `json:"notes,omitempty"`
	Adjustment     adjustmentItem `json:"adjustment"`
}

// --- how old the data is -----------------------------------------------------
//
// Tehran equity bars are a MANUALLY REFRESHED dataset. cdn.tsetmc.com is not
// reachable from the production host (TCP 443 fails outright, http=000 —
// measured, see scripts/tsetmc_fetch.py), so nothing on the server can fetch
// them; they arrive when a human runs `make refresh-equities` from a network
// that can reach TSETMC. Nothing schedules that, so nothing stops it either.
//
// On 2026-09-24 the newest bar in production was 2026-09-09. For fifteen days
// this endpoint answered every request with a `to` of the current date, a
// `period` of the last month, and prices from a fortnight earlier — each field
// individually true, and the combination reading as today's market. That is
// the failure this block exists to make impossible, and it is why the AGE
// travels on the payload instead of being left for a client to derive from
// `last_trade_date`: a client that never does the subtraction is exactly the
// client that renders the stale number under today's heading.
const (
	// equityStaleAfterDays is the age at which the roster's newest bar stops
	// being explicable by the calendar.
	//
	// Not 1 day, and not 3. The Tehran exchange trades Saturday to Wednesday,
	// so the newest bar is routinely 2 days old on Friday and 3 on Saturday
	// morning before the session lands, and a public holiday against a weekend
	// stretches that to about 5. On top of that the refresh is manual and its
	// documented cadence is weekly (docs/deployment.md): a run on Friday can
	// only ingest through the Wednesday session, so it stores a bar that is
	// already 2 days old and the newest bar reaches 9 days immediately before
	// the next on-time run. Ten days is the first age the market calendar and
	// an on-time weekly refresh cannot produce between them — and it is below
	// the fifteen measured on 2026-09-24, so it would have caught that.
	//
	// Measured on production the same day, this also clears the market by
	// construction: across 2,571 gaps between distinct sessions since 2016 the
	// longest closure is exactly 10 days (Nowruz 2016 and 2021) and nothing
	// exceeds it, so no closure on record makes data "stale" under this bound.
	//
	// EquityBarsStale in observability/alerts.yml holds the same BOUNDARY,
	// deliberately: the page a reader looks at and the alert an operator gets
	// must not disagree about when this data has gone bad. Note it cannot hold
	// the same NUMBER, and that is the point — Stale is `days > 10`, so the
	// first stale age is eleven days, while the rule compares a gauge in
	// seconds and must therefore say `>= 11*86400`. Written as `> 10*86400` it
	// fired one second after midnight on day ten and paged an operator a full
	// day before this block would admit anything was wrong.
	equityStaleAfterDays = 10

	// equityRefreshCommand is named in the payload because "this data is old"
	// without "here is how to fix it" makes the reader's next question
	// unanswerable from what they are holding.
	equityRefreshCommand = "make refresh-equities"
)

// dataAgeBlock states how old the stored equity data is, on every response
// that serves a price derived from it.
type dataAgeBlock struct {
	// NewestTradeDate is the newest session stored across the roster in this
	// response's universe. Null when the roster holds no bars at all, which is
	// a different state from "old" and is reported as such rather than as an
	// age of zero.
	NewestTradeDate *string `json:"newest_trade_date"`
	// AgeDays is whole days from NewestTradeDate to today, in UTC. It is
	// measured against TODAY and not against this response's `to` bound: it
	// describes the DATASET, so a caller asking for a historical window still
	// learns that the underlying data stops where it stops.
	AgeDays *int   `json:"age_days"`
	AsOf    string `json:"as_of"`
	// Stale is AgeDays > StaleAfterDays. A boolean, so a client renders a
	// banner without reimplementing the bound and drifting from it.
	Stale          bool   `json:"stale"`
	StaleAfterDays int    `json:"stale_after_days"`
	RefreshCommand string `json:"refresh_command"`
	// Warning is non-empty whenever these prices must not be read as current,
	// and says in prose what is wrong and what to run. It is set in three
	// cases and Stale is true in only two of them: an empty roster warns
	// without being stale, because there is no old price being served — there
	// is no price at all, and a client that renders "N days old" for it would
	// be describing data that does not exist. Stale is never true without a
	// Warning, which is the pairing rule the screener's nullable figures
	// follow.
	Warning string `json:"warning,omitempty"`
	Note    string `json:"note"`
}

const dataAgeNote = "Tehran equity bars do not refresh themselves: TSETMC is unreachable " +
	"from the production host, so bars arrive only when an operator runs the fetch script " +
	"from a network that can reach it. This block is the age of the newest stored session " +
	"across the roster, measured against today rather than against this response's window."

// newestRosterBar is the newest stored session across the given roster rows,
// or nil when none of them has one.
//
// Read from equity_instruments.last_bar — the registry's own coverage column,
// maintained by the ingest — so it costs no extra query, covers instruments
// the screener excludes (a refused adjustment still receives bars from the
// same fetch, so it is evidence about whether the fetch ran), and reports the
// same number the rows themselves publish as `last_bar`.
//
// Pure function (unit tested).
func newestRosterBar(rows []stockRow) *time.Time {
	var newest *time.Time
	for _, r := range rows {
		if r.LastBar == nil {
			continue
		}
		d := r.LastBar.UTC()
		if newest == nil || d.After(*newest) {
			newest = &d
		}
	}
	return newest
}

// buildDataAge is the age arithmetic and the bound. Pure function (unit
// tested): the clock arrives from the caller, so there is no hidden `now`.
func buildDataAge(newest *time.Time, now time.Time) dataAgeBlock {
	today := dayFloor(now)
	out := dataAgeBlock{
		AsOf:           today.Format(dateLayout),
		StaleAfterDays: equityStaleAfterDays,
		RefreshCommand: equityRefreshCommand,
		Note:           dataAgeNote,
	}
	if newest == nil {
		// No bars for the rows in THIS response. Null age and null date, never
		// a zero: a zero would read as "current" and is the substitution this
		// whole block exists to prevent.
		//
		// The wording is scoped to the response on purpose, because that is
		// the only thing this function can see. buildDataAge is measured over
		// the returned rows, and Stocks() applies ?enabled= and ?sector=
		// BEFORE building this block -- so a claim about "this deployment"
		// would be made from a filtered universe and can be flatly false:
		// GET /api/v1/stocks?enabled=false&sector=13 on a fully current
		// deployment returns only کچاد, which has never been ingested, and an
		// absolute sentence there would tell an operator the whole dataset is
		// missing and send them off to run an unnecessary off-server refresh.
		out.Warning = "No stored Tehran equity bars for the symbols in this response, so " +
			"every price column here is empty rather than old. If a filter is applied " +
			"(sector, enabled), other symbols may still hold data — check /api/v1/stocks " +
			"unfiltered before concluding the dataset is missing. If it genuinely is, run `" +
			equityRefreshCommand + "` from a host that can reach TSETMC."
		return out
	}

	date := dayFloor(*newest)
	days := int(today.Sub(date).Hours() / 24)
	label := date.Format(dateLayout)
	out.NewestTradeDate = &label
	out.AgeDays = &days

	switch {
	case days < 0:
		// A bar dated after today is not freshness, it is a fault — a bad
		// dEven parse would put the whole series 621 years out (migration
		// 0028's note on trade_date). Said plainly rather than clamped to
		// zero, because a clamp would render it as perfectly current.
		out.Stale = true
		out.Warning = fmt.Sprintf(
			"The newest stored trade date is %s, which is %d day(s) AFTER today (%s). A bar "+
				"cannot be in the future, so this is a stored or parsed date fault, not a "+
				"fresh dataset — do not read these prices as current.",
			label, -days, out.AsOf)
	case days > equityStaleAfterDays:
		out.Stale = true
		out.Warning = fmt.Sprintf(
			"These prices are %d days old: the newest stored Tehran session is %s and today "+
				"is %s. The Tehran exchange trades Saturday to Wednesday, so a gap of up to "+
				"about 5 days is the calendar; beyond %d days it is a refresh that has "+
				"stopped. Tehran equity bars are fetched by hand because TSETMC is "+
				"unreachable from the server — run `%s` from a network that can reach it.",
			days, label, out.AsOf, equityStaleAfterDays, equityRefreshCommand)
	}
	return out
}

type stocksResponse struct {
	Items []stockItem `json:"items"`
	Count int         `json:"count"`
	// DataAge travels on the roster as well as on the screener. /api/v1/stocks
	// publishes first_bar, last_bar and bar_count per row, and a caller can
	// therefore build a symbol picker or a coverage table from it without ever
	// touching the screener — so it needs the same one-line answer to "is any
	// of this current?".
	DataAge dataAgeBlock `json:"data_age"`
}

func formatDate(t *time.Time) string {
	if t == nil {
		return ""
	}
	return t.UTC().Format(dateLayout)
}

// buildStocksResponse projects the stored rows onto the contract. Pure
// function (unit tested): it copies and formats, and the only thing it
// computes is the age of the newest bar, from the `now` the caller passes.
//
// The age is measured over the rows in THIS response, not over the whole
// stored roster: a caller filtering by ?sector= is asking about that sector,
// and a top-level freshness claim taken from some other sector's symbols would
// be a true statement about the wrong thing.
func buildStocksResponse(rows []stockRow, now time.Time) stocksResponse {
	// Never nil: an empty roster is a 200 with an empty list, not a missing
	// key a client has to special-case.
	out := stocksResponse{Items: make([]stockItem, 0, len(rows))}
	for _, r := range rows {
		item := stockItem{
			InsCode:    r.InsCode,
			Symbol:     r.SymbolFA,
			Name:       r.NameFA,
			Market:     r.Market,
			Board:      r.Board,
			SectorCode: r.SectorCode,
			Sector:     r.SectorFA,
			ISIN:       r.ISIN,
			FirstBar:   formatDate(r.FirstBar),
			LastBar:    formatDate(r.LastBar),
			BarCount:   r.BarCount,
			Enabled:    r.Enabled,
			Notes:      r.Notes,
			Adjustment: buildAdjustment(r),
		}
		if r.InstrumentCode != nil {
			item.InstrumentCode = *r.InstrumentCode
		}
		out.Items = append(out.Items, item)
	}
	out.Count = len(out.Items)
	out.DataAge = buildDataAge(newestRosterBar(rows), now)
	return out
}

const stockColumns = `
	       i.ins_code, i.symbol_fa, i.name_fa, i.market, i.board,
	       i.sector_code, i.sector_fa, i.isin, i.instrument_code,
	       i.first_bar, i.last_bar, i.bar_count, i.enabled, i.notes,
	       a.adjustment_version, a.status, a.actions_applied, a.reopenings,
	       a.pre_listing_bars, a.worst_return, a.refusal_reason, a.computed_at,
	       a.first_bar AS adjusted_first_bar`

// verdictJoin picks ONE verdict per instrument: the most recently computed.
//
// A plain LEFT JOIN on ins_code would be right today and wrong the moment a
// second adjustment_version exists — 0028 keys equity_adjustments on
// (ins_code, adjustment_version) precisely so a v2 can be computed beside v1 —
// and it would then return an instrument TWICE, once per version, with no
// indication which row a client should believe. The lateral takes the newest,
// which is the version this deployment most recently ran.
//
// LEFT, so an instrument that has never been ingested is listed with a null
// verdict rather than vanishing. "Registered but never collected" is a real
// and useful state — کهربا sat in TSETMC_FUNDS for months in exactly it — and
// a client can only act on it if the row is there to see.
const verdictJoin = `
	LEFT JOIN LATERAL (
	    SELECT adjustment_version, status, actions_applied, reopenings,
	           pre_listing_bars, worst_return, refusal_reason, computed_at,
	           first_bar
	    FROM equity_adjustments
	    WHERE ins_code = i.ins_code
	    ORDER BY computed_at DESC
	    LIMIT 1
	) a ON TRUE`

const stocksSelect = `
	SELECT` + stockColumns + `
	FROM equity_instruments i` + verdictJoin + `
	WHERE ($1::bool IS NULL OR i.enabled = $1)
	  AND ($2::text IS NULL OR i.sector_code = $2)
	ORDER BY i.bar_count DESC, i.symbol_fa`

const stockBySymbolSelect = `
	SELECT` + stockColumns + `
	FROM equity_instruments i` + verdictJoin + `
	WHERE i.symbol_fa = $1`

func scanStock(row pgx.Row) (stockRow, error) {
	var s stockRow
	err := row.Scan(&s.InsCode, &s.SymbolFA, &s.NameFA, &s.Market, &s.Board,
		&s.SectorCode, &s.SectorFA, &s.ISIN, &s.InstrumentCode,
		&s.FirstBar, &s.LastBar, &s.BarCount, &s.Enabled, &s.Notes,
		&s.AdjustmentVersion, &s.AdjustmentStatus, &s.ActionsApplied,
		&s.Reopenings, &s.PreListingBars, &s.WorstReturn, &s.RefusalReason,
		&s.ComputedAt, &s.AdjustedFirstBar)
	return s, err
}

func optional(raw string) *string {
	if raw == "" {
		return nil
	}
	v := raw
	return &v
}

// Stocks implements GET /api/v1/stocks?enabled=&sector=.
//
// An absent ?enabled= applies NO filter, matching /api/v1/instruments: every
// row carries its own flag, so returning the whole roster is lossless, while
// defaulting to enabled-only would make a deliberately disabled instrument
// (کچاد, whose adjustment the gate refuses) look like one that does not exist.
func (h *Handler) Stocks(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	var enabled *bool
	if raw := q.Get("enabled"); raw != "" {
		on, perr := parseBoolParam("enabled", raw)
		if perr != nil {
			httpserver.BadRequest(w, perr.Message, perr.Details)
			return
		}
		enabled = &on
	}

	rows, err := h.Pool.Query(r.Context(), stocksSelect, enabled, optional(q.Get("sector")))
	if err != nil {
		h.Log.Error("stocks_query", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	scanned := []stockRow{}
	for rows.Next() {
		s, err := scanStock(rows)
		if err != nil {
			h.Log.Error("stocks_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		scanned = append(scanned, s)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("stocks_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	httpserver.JSON(w, http.StatusOK, buildStocksResponse(scanned, time.Now().UTC()))
}

// --- bars --------------------------------------------------------------------

// barQuery is a validated /stocks/{symbol}/bars request.
type barQuery struct {
	Symbol   string
	Adjusted bool
	From     *time.Time
	To       *time.Time
	Limit    int
}

// parseBarQuery validates the query string. Pure function (unit tested): no
// clock, no database.
//
// ?adjusted defaults to TRUE. The raw close series is not a price history —
// فولاد's raw close is x1.52 over nineteen years against x907.86 adjusted, and
// four separate raw days drop more than 37% on corporate actions that never
// moved the market — so the default is the number that means something.
func parseBarQuery(symbol string, q url.Values) (barQuery, *paramError) {
	out := barQuery{Symbol: foldSymbol(symbol), Adjusted: true, Limit: defaultBarLimit}
	if out.Symbol == "" {
		return out, badParam("symbol is required", map[string]any{"symbol": symbol})
	}

	if raw := q.Get("adjusted"); raw != "" {
		on, perr := parseBoolParam("adjusted", raw)
		if perr != nil {
			return out, perr
		}
		out.Adjusted = on
	}
	if raw := q.Get("from"); raw != "" {
		d, perr := parseDate("from", raw)
		if perr != nil {
			return out, perr
		}
		out.From = &d
	}
	if raw := q.Get("to"); raw != "" {
		d, perr := parseDate("to", raw)
		if perr != nil {
			return out, perr
		}
		out.To = &d
	}
	if out.From != nil && out.To != nil && out.To.Before(*out.From) {
		return out, badParam(
			fmt.Sprintf("to (%s) is before from (%s)",
				out.To.Format(dateLayout), out.From.Format(dateLayout)),
			map[string]any{
				"from": out.From.Format(dateLayout),
				"to":   out.To.Format(dateLayout),
			})
	}
	if raw := q.Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		// Refused, never clamped. A caller who asks for 50,000 bars has a
		// wrong idea of the data, and quietly serving 6,000 lets them believe
		// the instrument only has that many.
		if err != nil || n < 1 || n > maxBarLimit {
			return out, badParam(
				fmt.Sprintf("limit must be between 1 and %d, got %q", maxBarLimit, raw),
				map[string]any{"limit": raw, "max": maxBarLimit})
		}
		out.Limit = n
	}
	return out, nil
}

// barRow is one equity_bars row plus the adjustment factor that applies to it.
//
// The factor is resolved in SQL, by the same rule the Python engine uses: the
// cumulative factor of the FIRST action effective strictly after this bar, and
// 1.0 when no action follows. Keeping the rule in one statement rather than in
// a loop here is what makes "this package computes nothing" true enough to be
// worth saying — the only arithmetic is the multiplication below.
type barRow struct {
	TradeDate      time.Time
	Open           float64
	High           float64
	Low            float64
	Close          float64
	FinalClose     float64
	PriceYesterday float64
	Volume         int64
	TradeCount     int64
	Value          float64
	Factor         float64
}

type barItem struct {
	Date string `json:"date"`
	// On a halted session TSETMC serves zeros for open/high/low, which is what
	// is stored and what is served: a zero here means no trade occurred, and
	// `traded` below says so without the caller having to infer it.
	Open       float64 `json:"open"`
	High       float64 `json:"high"`
	Low        float64 `json:"low"`
	Close      float64 `json:"close"`
	FinalClose float64 `json:"final_close"`
	Volume     int64   `json:"volume"`
	TradeCount int64   `json:"trade_count"`
	Value      float64 `json:"value"`
	Traded     bool    `json:"traded"`
	// Only on an adjusted response, and only when it is not 1: the factor this
	// bar's prices were multiplied by. It is what makes an adjusted number
	// checkable against the raw one without a second request.
	Factor float64 `json:"adjustment_factor,omitempty"`
}

type barsResponse struct {
	Symbol   string `json:"symbol"`
	InsCode  string `json:"ins_code"`
	Name     string `json:"name_fa"`
	Adjusted bool   `json:"adjusted"`
	// Present on every response, adjusted or not, because a caller comparing
	// the two needs to know which arithmetic the adjusted one used — and a
	// caller who asked for raw still needs to know that 21 actions exist in
	// the window they are looking at.
	Adjustment adjustmentItem `json:"adjustment"`
	Currency   string         `json:"currency"`
	Count      int            `json:"count"`
	Items      []barItem      `json:"items"`
	// The window actually served, echoed rather than left to be inferred from
	// the rows: an empty page and a page that stopped at the limit look the
	// same otherwise.
	From    string `json:"from,omitempty"`
	To      string `json:"to,omitempty"`
	Limit   int    `json:"limit"`
	HasMore bool   `json:"has_more"`
	Note    string `json:"note,omitempty"`
}

// currencyRial is the unit TSETMC quotes equities in. Stated on every response
// because the rest of this API reports Iranian amounts in TOMAN (docs/api.md),
// and a client that assumes the house convention is off by a factor of ten.
const currencyRial = "IRR"

// buildBarsResponse projects rows onto the contract, applying the stored
// factor when the caller asked for adjusted prices. Pure function (unit
// tested): the only computation in this package.
//
// `rows` arrives NEWEST-FIRST, as the query orders it, and one row longer than
// the limit when there is more. Both matter: trimming a newest-first slice
// drops the OLDEST bar, which is the one a caller paging backwards has not
// asked for yet, and the items go out oldest-first so a chart can draw them
// without reversing.
func buildBarsResponse(meta stockRow, rows []barRow, q barQuery) barsResponse {
	limited := rows
	hasMore := false
	if len(limited) > q.Limit {
		limited = limited[:q.Limit]
		hasMore = true
	}

	out := barsResponse{
		Symbol:     meta.SymbolFA,
		InsCode:    meta.InsCode,
		Name:       meta.NameFA,
		Adjusted:   q.Adjusted,
		Adjustment: buildAdjustment(meta),
		Currency:   currencyRial,
		Limit:      q.Limit,
		HasMore:    hasMore,
		Items:      make([]barItem, 0, len(limited)),
	}
	if q.From != nil {
		out.From = q.From.Format(dateLayout)
	}
	if q.To != nil {
		out.To = q.To.Format(dateLayout)
	}

	for i := len(limited) - 1; i >= 0; i-- {
		r := limited[i]
		factor := 1.0
		if q.Adjusted {
			factor = r.Factor
		}
		item := barItem{
			Date:       r.TradeDate.UTC().Format(dateLayout),
			Open:       r.Open * factor,
			High:       r.High * factor,
			Low:        r.Low * factor,
			Close:      r.Close * factor,
			FinalClose: r.FinalClose * factor,
			Volume:     r.Volume,
			TradeCount: r.TradeCount,
			// Turnover is a rial amount that was actually exchanged, so it is
			// NOT scaled. Multiplying it by a back-adjustment factor would
			// claim a trade happened for money that never changed hands, and
			// the volume it is the counterpart of is not scaled either.
			Value:  r.Value,
			Traded: r.Volume > 0 || r.TradeCount > 0,
		}
		if q.Adjusted && factor != 1.0 {
			item.Factor = factor
		}
		out.Items = append(out.Items, item)
	}
	// Set from the slice that was actually built, never from len(rows): rows
	// arrives one longer than the limit when there is more, so counting it
	// would over-report by exactly one on every truncated page. Leaving it
	// unset was worse -- it shipped 0 beside 4,636 items, and a client that
	// renders on count>0 draws an empty chart for a full 19-year series.
	out.Count = len(out.Items)

	if q.Adjusted && out.Adjustment.Reopenings > 0 {
		out.Note = fmt.Sprintf(
			"this series contains %d reopening auction(s) whose move exceeds a "+
				"normal session's price limit; the exchange lifts the limit when "+
				"an instrument resumes after a suspension, so a return spanning "+
				"one of those days is not a session return",
			out.Adjustment.Reopenings)
	}
	return out
}

// barsSelect resolves the back-adjustment factor per bar in one statement.
//
// The correlated subquery is the rule from app/equities/adjust.py, stated in
// SQL: the cumulative factor of the first action effective strictly AFTER this
// bar, and 1.0 when none follows — which is what leaves the newest segment of
// the series at its real prices, as "back-adjusted" means.
//
// Bars are selected newest-first (that is what a chart and a "latest" read
// both want) and reversed below, so the page is the most recent `limit`
// sessions in the window rather than the oldest.
const barsSelect = `
	SELECT b.trade_date, b.open, b.high, b.low, b.close, b.final_close,
	       b.price_yesterday, b.volume, b.trade_count, b.value,
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
	  AND ($4::date IS NULL OR b.trade_date <= $4)
	ORDER BY b.trade_date DESC
	LIMIT $5`

// Bars implements GET /api/v1/stocks/{symbol}/bars?adjusted=&from=&to=&limit=.
func (h *Handler) Bars(w http.ResponseWriter, r *http.Request) {
	q, perr := parseBarQuery(chi.URLParam(r, "symbol"), r.URL.Query())
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}

	ctx := r.Context()
	// The instrument is resolved FIRST: it supplies the insCode the bars are
	// keyed by and the verdict the response is required to carry, and an
	// unknown symbol is refused here rather than answered with an empty list.
	meta, err := scanStock(h.Pool.QueryRow(ctx, stockBySymbolSelect, q.Symbol))
	if errors.Is(err, pgx.ErrNoRows) {
		unknownSymbol(w, q.Symbol)
		return
	}
	if err != nil {
		h.Log.Error("bars_symbol", "error", err, "symbol", q.Symbol)
		httpserver.Internal(w, "database error")
		return
	}

	adjustment := buildAdjustment(meta)
	if q.Adjusted && !adjustment.Servable {
		// The P3 gate, at the read edge. Serving raw prices under a contract
		// that promised adjusted ones would be worse than refusing, and
		// silently is how a corrupted series ends up in a backtest.
		httpserver.Error(w, http.StatusConflict, "adjustment_unavailable",
			fmt.Sprintf(
				"no validated corporate-action adjustment exists for %q, so "+
					"adjusted prices are not served; retry with ?adjusted=false "+
					"for the exchange's raw prints",
				meta.SymbolFA),
			map[string]any{
				"symbol":         meta.SymbolFA,
				"status":         adjustment.Status,
				"version":        adjustment.Version,
				"refusal_reason": adjustment.RefusalReason,
			})
		return
	}

	version := ""
	if meta.AdjustmentVersion != nil {
		version = *meta.AdjustmentVersion
	}

	// An ADJUSTED read starts at the instrument's first TRADED session, never
	// at its first stored bar. Before its first trade TSETMC serves
	// placeholder bars at the 1,000-rial par value — 161 of them for نوری,
	// which listed on 2019-07-13 with bars from 2018-11-10 — and they are
	// prices of nothing. Serving them adjusted would put a +3,025% first day
	// at the front of the series, which is exactly the artefact the engine
	// excludes them to avoid. ?adjusted=false still shows them: they ARE what
	// TSETMC served, and the raw contract promises nothing more than that.
	from := q.From
	if q.Adjusted && meta.AdjustedFirstBar != nil {
		if from == nil || from.Before(*meta.AdjustedFirstBar) {
			from = meta.AdjustedFirstBar
		}
	}
	// fetchLimit: one extra row is what proves has_more without a count query.
	rows, err := h.Pool.Query(ctx, barsSelect,
		meta.InsCode, version, from, q.To, q.Limit+1)
	if err != nil {
		h.Log.Error("bars_query", "error", err, "symbol", q.Symbol)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	scanned := []barRow{}
	for rows.Next() {
		var b barRow
		if err := rows.Scan(&b.TradeDate, &b.Open, &b.High, &b.Low, &b.Close,
			&b.FinalClose, &b.PriceYesterday, &b.Volume, &b.TradeCount,
			&b.Value, &b.Factor); err != nil {
			h.Log.Error("bars_scan", "error", err, "symbol", q.Symbol)
			httpserver.Internal(w, "database error")
			return
		}
		scanned = append(scanned, b)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("bars_rows", "error", err, "symbol", q.Symbol)
		httpserver.Internal(w, "database error")
		return
	}

	// buildBarsResponse takes the rows newest-first, exactly as the query
	// ordered them, and reverses on the way out — so the extra row that proves
	// has_more is the oldest one and dropping it never loses the newest bar.
	httpserver.JSON(w, http.StatusOK, buildBarsResponse(meta, scanned, q))
}
