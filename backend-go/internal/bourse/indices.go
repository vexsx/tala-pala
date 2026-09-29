package bourse

// GET /api/v1/bourse/indices — every enabled index with its verdict and its
// standard figures, for the page's index and sector tables.

import (
	"context"
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// checkItem is an index's verdict as a client sees it. It travels on every
// index, because "was this series corrected, and was the correction checked
// against the exchange?" is not metadata a reader should need a second
// request for.
type checkItem struct {
	Version string `json:"version,omitempty"`
	// validated | refused | never_ingested
	Status             string   `json:"status"`
	ScaleBreaks        int      `json:"scale_breaks"`
	RowsRescaled       int      `json:"rows_rescaled"`
	FirstBreak         *string  `json:"first_break"`
	LastBreak          *string  `json:"last_break"`
	DroppedNonpositive int      `json:"dropped_nonpositive"`
	LargestMovePct     *float64 `json:"largest_move_pct"`
	LargestMoveDate    *string  `json:"largest_move_date"`
	LiveValue          *float64 `json:"live_value"`
	LiveRatio          *float64 `json:"live_ratio"`
	LiveCheckedAt      *string  `json:"live_checked_at"`
	RefusalReason      string   `json:"refusal_reason,omitempty"`
	ComputedAt         *string  `json:"computed_at"`
}

const statusNeverIngested = "never_ingested"

func buildCheck(r indexRow) checkItem {
	if r.Status == nil {
		return checkItem{Status: statusNeverIngested}
	}
	c := checkItem{Status: *r.Status, FirstBreak: dayPtr(r.FirstBreak),
		LastBreak: dayPtr(r.LastBreak), LargestMoveDate: dayPtr(r.LargestMoveDate),
		LiveValue: r.LiveValue, LiveRatio: r.LiveRatio}
	if r.CheckVersion != nil {
		c.Version = *r.CheckVersion
	}
	if r.ScaleBreaks != nil {
		c.ScaleBreaks = *r.ScaleBreaks
	}
	if r.RowsRescaled != nil {
		c.RowsRescaled = *r.RowsRescaled
	}
	if r.DroppedNonpositive != nil {
		c.DroppedNonpositive = *r.DroppedNonpositive
	}
	if r.LargestMove != nil {
		c.LargestMovePct = fp(*r.LargestMove * 100)
	}
	if r.RefusalReason != nil {
		c.RefusalReason = *r.RefusalReason
	}
	if r.LiveCheckedAt != nil {
		s := r.LiveCheckedAt.UTC().Format(time.RFC3339)
		c.LiveCheckedAt = &s
	}
	if r.ComputedAt != nil {
		s := r.ComputedAt.UTC().Format(time.RFC3339)
		c.ComputedAt = &s
	}
	return c
}

// indexMeta is the registry part of an index, shared by every response.
type indexMeta struct {
	InsCode      string    `json:"ins_code"`
	NameFA       string    `json:"name_fa"`
	NameEN       string    `json:"name_en"`
	Market       string    `json:"market"`
	Kind         string    `json:"kind"`
	SectorCode   string    `json:"sector_code"`
	Weighting    string    `json:"weighting"`
	ReturnBasis  string    `json:"return_basis"`
	DisplayOrder int       `json:"display_order"`
	FirstDate    *string   `json:"first_date"`
	LastDate     *string   `json:"last_date"`
	ValueCount   int       `json:"value_count"`
	Notes        string    `json:"notes,omitempty"`
	Check        checkItem `json:"check"`
}

func buildMeta(r indexRow) indexMeta {
	return indexMeta{InsCode: r.InsCode, NameFA: r.NameFA, NameEN: r.NameEN,
		Market: r.Market, Kind: r.Kind, SectorCode: r.SectorCode, Weighting: r.Weighting,
		ReturnBasis: r.ReturnBasis, DisplayOrder: r.DisplayOrder,
		FirstDate: dayPtr(r.FirstDate), LastDate: dayPtr(r.LastDate),
		ValueCount: r.ValueCount, Notes: r.Notes, Check: buildCheck(r)}
}

type pointItem struct {
	Date  string   `json:"date"`
	Value *float64 `json:"value"`
}

func pointOf(p IndexPoint) *pointItem {
	return &pointItem{Date: dayString(p.Day), Value: fv(p.Value)}
}

type drawdownItem struct {
	Pct           *float64 `json:"pct"`
	PeakDate      *string  `json:"peak_date"`
	TroughDate    *string  `json:"trough_date"`
	RecoveredDate *string  `json:"recovered_date"`
}

func drawdownOf(d drawdown) drawdownItem {
	return drawdownItem{Pct: d.Pct, PeakDate: dayPtr(d.PeakDate),
		TroughDate: dayPtr(d.TroughDate), RecoveredDate: dayPtr(d.RecoveredDate)}
}

// returnWindows are the columns of the index table. "1w" is seven calendar
// days; the others are calendar months and years from the index's own newest
// session.
var returnWindows = []string{"1w", "1m", "3m", "6m", "1y", "3y", "5y"}

func windowStart(key string, end time.Time) time.Time {
	switch key {
	case "1w":
		return end.AddDate(0, 0, -7)
	case "1m":
		return end.AddDate(0, -1, 0)
	case "3m":
		return end.AddDate(0, -3, 0)
	case "6m":
		return end.AddDate(0, -6, 0)
	case "1y":
		return end.AddDate(-1, 0, 0)
	case "3y":
		return end.AddDate(-3, 0, 0)
	default: // "5y"
		return end.AddDate(-5, 0, 0)
	}
}

type indexItem struct {
	indexMeta
	Last *pointItem `json:"last"`
	// The newest session against the one before it. Unchanged when the two
	// are equal, which on a headline index means the market did not trade.
	Change1DPct   *float64 `json:"change_1d_pct"`
	LastUnchanged bool     `json:"last_unchanged"`

	Returns       map[string]*float64 `json:"returns"`
	ReturnReasons map[string]string   `json:"return_reasons,omitempty"`
	// SincePct is the return from the value in force on ?since= — the page
	// passes 1 Farvardin, so this is the Tehran year-to-date.
	SincePct    *float64 `json:"since_pct,omitempty"`
	SinceReason string   `json:"since_reason,omitempty"`

	// Over the year ending at the index's newest session.
	DispersionPct     *float64     `json:"dispersion_1y_pct"`
	DispersionReason  string       `json:"dispersion_1y_reason,omitempty"`
	Sessions1Y        int          `json:"sessions_1y"`
	UnchangedRows1Y   int          `json:"unchanged_rows_1y"`
	High52W           *pointItem   `json:"high_52w"`
	Low52W            *pointItem   `json:"low_52w"`
	MaxDrawdown1Y     drawdownItem `json:"max_drawdown_1y"`
	SMA200GapPct      *float64     `json:"sma200_gap_pct"`
	SMA200Reason      string       `json:"sma200_reason,omitempty"`
	AllTimeHigh       *pointItem   `json:"all_time_high"`
	FromAllTimeHigh   *float64     `json:"from_all_time_high_pct"`
	DaysSinceAllTimeH *int         `json:"days_since_all_time_high"`

	// The roster equities TSETMC classifies in this sector (bourse sector
	// indices only), so the table can link a sector to the shares behind it.
	RosterSymbols []string `json:"roster_symbols,omitempty"`
}

// buildIndexItem is every figure on one row. Pure function (unit tested).
func buildIndexItem(r indexRow, s []IndexPoint, since *time.Time, roster []string) indexItem {
	item := indexItem{indexMeta: buildMeta(r), Returns: map[string]*float64{},
		RosterSymbols: roster}
	if !r.Servable() || len(s) == 0 {
		reason := "no corrected series is served for this index"
		if r.Status == nil {
			reason = "this index has never been ingested"
		} else if r.RefusalReason != nil && *r.RefusalReason != "" {
			reason = "the index's verdict is refused: " + *r.RefusalReason
		}
		item.ReturnReasons = map[string]string{}
		for _, k := range returnWindows {
			item.Returns[k] = nil
			item.ReturnReasons[k] = reason
		}
		item.DispersionReason = reason
		item.SMA200Reason = reason
		return item
	}

	last := s[len(s)-1]
	item.Last = pointOf(last)
	if len(s) >= 2 {
		item.Change1DPct = fp((last.Value/s[len(s)-2].Value - 1) * 100)
		item.LastUnchanged = last.Unchanged
	}
	for _, k := range returnWindows {
		v, why := windowReturn(s, windowStart(k, last.Day))
		item.Returns[k] = v
		if v == nil {
			if item.ReturnReasons == nil {
				item.ReturnReasons = map[string]string{}
			}
			item.ReturnReasons[k] = why
		}
	}
	if since != nil {
		item.SincePct, item.SinceReason = windowReturn(s, *since)
	}

	yearStart := last.Day.AddDate(-1, 0, 0)
	year := withBase(s, &yearStart, last.Day)
	item.DispersionPct, item.Sessions1Y, item.DispersionReason = sessionDispersion(year)
	for _, p := range between(s, &yearStart, last.Day) {
		if p.Unchanged {
			item.UnchangedRows1Y++
		}
	}
	inYear := between(s, &yearStart, last.Day)
	if hi, ok := highest(inYear); ok {
		item.High52W = pointOf(hi)
	}
	if lo, ok := lowest(inYear); ok {
		item.Low52W = pointOf(lo)
	}
	item.MaxDrawdown1Y = drawdownOf(maxDrawdown(year))
	item.SMA200GapPct, item.SMA200Reason = smaGap(s, 200)
	if ath, ok := highest(s); ok {
		item.AllTimeHigh = pointOf(ath)
		item.FromAllTimeHigh = fp((last.Value/ath.Value - 1) * 100)
		days := int(last.Day.Sub(ath.Day).Hours() / 24)
		item.DaysSinceAllTimeH = &days
	}
	return item
}

type indicesResponse struct {
	Items []indexItem `json:"items"`
	Count int         `json:"count"`
	Since *string     `json:"since"`
	Notes []string    `json:"notes"`
	// Refused names every enabled index whose verdict withholds its series,
	// so a table with a blank row can say why without the client digging.
	Refused []string `json:"refused"`
	DataAge DataAge  `json:"data_age"`
}

var indicesNotes = []string{
	"Every figure is computed over TSETMC's own index values after one correction: " +
		"values the exchange stores off by exactly a factor of ten are multiplied back, " +
		"and each index's check says how many were and whether the corrected history " +
		"agreed with the exchange's live figure.",
	"Each index's windows end at ITS OWN newest stored session, and a return is measured " +
		"from the value in force when the window began.",
	"Dispersion is the standard deviation of session returns, not annualised. A session " +
		"whose value repeats the previous one exactly is a closed market, not a flat day, " +
		"and is left out of it.",
	"An index level is not a price and these are not forecasts: they describe what the " +
		"index did, not what it will do.",
}

// buildIndicesResponse assembles the table. Pure (unit tested).
func buildIndicesResponse(store *indexStore, since *time.Time, roster map[string][]string,
	now time.Time) indicesResponse {
	out := indicesResponse{Items: []indexItem{}, Notes: indicesNotes, Refused: []string{},
		Since: dayPtr(since)}
	for _, r := range store.rows {
		var symbols []string
		if r.Market == "bourse" && r.Kind == "sector" {
			symbols = roster[r.SectorCode]
		}
		out.Items = append(out.Items, buildIndexItem(r, store.series[r.InsCode], since, symbols))
		if r.Status != nil && !r.Servable() {
			out.Refused = append(out.Refused, r.InsCode)
		}
	}
	out.Count = len(out.Items)
	out.DataAge = buildDataAge(store.newestSession(), now)
	return out
}

// rosterBySector maps a TSETMC sector code to the enabled roster symbols in it.
func (h *Handler) rosterBySector(ctx context.Context) (map[string][]string, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT sector_code, symbol_fa FROM equity_instruments
		WHERE enabled AND sector_code <> '' ORDER BY symbol_fa`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][]string{}
	for rows.Next() {
		var code, sym string
		if err := rows.Scan(&code, &sym); err != nil {
			return nil, err
		}
		out[code] = append(out[code], sym)
	}
	for _, v := range out {
		sort.Strings(v)
	}
	return out, rows.Err()
}

// Indices implements GET /api/v1/bourse/indices?since=&kind=&market=.
func (h *Handler) Indices(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	var since *time.Time
	if raw := q.Get("since"); raw != "" {
		d, perr := parseDate("since", raw)
		if perr != nil {
			httpserver.BadRequest(w, perr.Message, perr.Details)
			return
		}
		since = &d
	}
	kind := strings.TrimSpace(q.Get("kind"))
	market := strings.TrimSpace(q.Get("market"))
	if kind != "" && !oneOf(kind, "headline", "market", "segment", "sector") {
		httpserver.BadRequest(w, fmt.Sprintf("kind must be headline, market, segment or sector, got %q", kind),
			map[string]any{"kind": kind})
		return
	}
	if market != "" && !oneOf(market, "bourse", "farabourse") {
		httpserver.BadRequest(w, fmt.Sprintf("market must be bourse or farabourse, got %q", market),
			map[string]any{"market": market})
		return
	}

	ctx := r.Context()
	store, err := h.storeFor(ctx)
	if err != nil {
		h.Log.Error("bourse_indices_store", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	roster, err := h.rosterBySector(ctx)
	if err != nil {
		h.Log.Error("bourse_indices_roster", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	resp := buildIndicesResponse(store, since, roster, time.Now())
	if kind != "" || market != "" {
		kept := resp.Items[:0]
		for _, it := range resp.Items {
			if (kind == "" || it.Kind == kind) && (market == "" || it.Market == market) {
				kept = append(kept, it)
			}
		}
		resp.Items = kept
		resp.Count = len(kept)
	}
	httpserver.JSON(w, http.StatusOK, resp)
}

func oneOf(v string, options ...string) bool {
	for _, o := range options {
		if v == o {
			return true
		}
	}
	return false
}
