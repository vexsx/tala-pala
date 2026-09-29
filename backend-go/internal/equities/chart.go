package equities

// One share for the Trade chart: GET /api/v1/market/candles?symbol=EQ:<code>
// is served by internal/prices, which asks this package for the bars.
//
// Everything /stocks/{symbol}/bars promises holds here, because this is the
// same read with a different cursor:
//
//   - The P3 gate. Only a VALIDATED adjustment is served, and the chart has no
//     raw mode — a back-adjusted chart that quietly fell back to raw prints
//     would draw فولاد's 21 corporate actions as crashes.
//   - The adjusted series starts at the first TRADED session, never at the par
//     value placeholders before it.
//   - Turnover is not scaled; prices are rials.
//
// What differs is what a candle needs that a bar list does not:
//
//   - The cursor is EXCLUSIVE (`trade_date < before`), because the chart pages
//     backwards with the oldest session it already holds.
//   - The close is the OFFICIAL closing price (final_close, قیمت پایانی) — the
//     one every return in this application is measured on — and the last
//     trade travels beside it. The official close is volume-weighted over the
//     session and can sit outside the traded high–low (285 of فولاد's 4,221
//     traded bars), so each bar says when it does.
//   - A halted session keeps its date and its carried reference price, and
//     loses its open/high/low: TSETMC stores zeros there, and zeros are not a
//     price. The chart draws that session as a gap and every indicator skips it.
//   - A traded bar with a zero in open/high/low is defective, not halted; it is
//     served the same way and counted, rather than drawn as a crash to zero.

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5"
)

// ErrUnknownEquity is returned for an insCode that is not in the roster.
var ErrUnknownEquity = errors.New("unknown equity")

// AdjustmentUnavailableError is the gate's refusal, carrying what the 409 of
// /stocks/{symbol}/bars carries so the chart can say the same thing.
type AdjustmentUnavailableError struct {
	Symbol        string
	Status        string
	Version       string
	RefusalReason string
}

func (e *AdjustmentUnavailableError) Error() string {
	return fmt.Sprintf(
		"no validated corporate-action adjustment exists for %q, so adjusted prices are "+
			"not served and this share cannot be charted; its raw prints are on "+
			"/api/v1/stocks/{symbol}/bars?adjusted=false", e.Symbol)
}

// Details is the error envelope's details block.
func (e *AdjustmentUnavailableError) Details() map[string]any {
	return map[string]any{
		"symbol":         e.Symbol,
		"status":         e.Status,
		"version":        e.Version,
		"refusal_reason": e.RefusalReason,
	}
}

// Adjustment and DataAge are the verdict and the age blocks exactly as the
// roster and the bars publish them, named for a caller outside this package.
type (
	Adjustment = adjustmentItem
	DataAge    = dataAgeBlock
)

// ChartMeta labels the share a chart draws.
type ChartMeta struct {
	InsCode    string     `json:"ins_code"`
	Symbol     string     `json:"symbol"`
	NameFA     string     `json:"name_fa"`
	Market     string     `json:"market"`
	Board      string     `json:"board"`
	SectorCode string     `json:"sector_code"`
	SectorFA   string     `json:"sector_fa"`
	Adjustment Adjustment `json:"adjustment"`
	// The first session of the adjusted series: history_from on the chart.
	HistoryFrom *time.Time `json:"-"`
}

// ChartBar is one session as the chart draws it. Prices are ADJUSTED rials.
type ChartBar struct {
	Day time.Time
	// Nil on a halted session and on a defective traded bar.
	Open, High, Low *float64
	// Close is final_close × factor, the official closing price.
	Close float64
	// LastTrade is close × factor; nil on a halted session, where the stored
	// "close" is the carried reference price, not a trade.
	LastTrade *float64
	Volume    int64
	Trades    int64
	// Value is turnover in rials, NOT scaled.
	Value  float64
	Traded bool
	// Factor is the back-adjustment multiplier applied to this bar.
	Factor float64
	// CloseOutsideRange: the official close lies outside [low, high].
	CloseOutsideRange bool
}

// ChartBars is a page of sessions, oldest first.
type ChartBars struct {
	Meta  ChartMeta
	Items []ChartBar
	// HasMore: an older adjusted session exists before Items[0].
	HasMore bool
	DataAge DataAge
	// Revision changes whenever the stored bars or the adjustment do, so a
	// chart holding older pages can tell they were restated.
	Revision string
	Notes    []string
	// DefectiveBars counts traded sessions served with null open/high/low
	// because one of them was zero.
	DefectiveBars int
}

const stockByInsCodeSelect = `
	SELECT` + stockColumns + `
	FROM equity_instruments i` + verdictJoin + `
	WHERE i.ins_code = $1`

// chartBarsSelect is barsSelect with an EXCLUSIVE upper bound: $4 is the
// oldest session the chart already holds (or the day after the newest one).
const chartBarsSelect = `
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
	  AND ($4::date IS NULL OR b.trade_date < $4)
	ORDER BY b.trade_date DESC
	LIMIT $5`

// ChartBars reads up to `limit` adjusted sessions for insCode in
// [from, before), newest first from the database and returned oldest first.
// `from` and `before` are dates (UTC midnight); nil is unbounded.
func (h *Handler) ChartBars(ctx context.Context, insCode string, from, before *time.Time, limit int) (ChartBars, error) {
	meta, err := scanStock(h.Pool.QueryRow(ctx, stockByInsCodeSelect, insCode))
	if errors.Is(err, pgx.ErrNoRows) {
		return ChartBars{}, ErrUnknownEquity
	}
	if err != nil {
		return ChartBars{}, fmt.Errorf("chart symbol: %w", err)
	}
	if gate := chartGate(meta); gate != nil {
		return ChartBars{Meta: chartMeta(meta)}, gate
	}

	version := ""
	if meta.AdjustmentVersion != nil {
		version = *meta.AdjustmentVersion
	}
	lower := from
	if meta.AdjustedFirstBar != nil && (lower == nil || lower.Before(*meta.AdjustedFirstBar)) {
		lower = meta.AdjustedFirstBar
	}
	// One more than asked for: the extra row is what proves has_more.
	rows, err := h.Pool.Query(ctx, chartBarsSelect, meta.InsCode, version, lower, before, limit+1)
	if err != nil {
		return ChartBars{}, fmt.Errorf("chart bars: %w", err)
	}
	defer rows.Close()
	scanned := []barRow{}
	for rows.Next() {
		var b barRow
		if err := rows.Scan(&b.TradeDate, &b.Open, &b.High, &b.Low, &b.Close,
			&b.FinalClose, &b.PriceYesterday, &b.Volume, &b.TradeCount,
			&b.Value, &b.Factor); err != nil {
			return ChartBars{}, fmt.Errorf("chart bar scan: %w", err)
		}
		scanned = append(scanned, b)
	}
	if err := rows.Err(); err != nil {
		return ChartBars{}, fmt.Errorf("chart bar rows: %w", err)
	}
	return buildChartBars(meta, scanned, limit, time.Now().UTC()), nil
}

// chartGate is the P3 gate: nil when an adjusted series may be served.
func chartGate(meta stockRow) error {
	adj := buildAdjustment(meta)
	if adj.Servable {
		return nil
	}
	return &AdjustmentUnavailableError{Symbol: meta.SymbolFA, Status: adj.Status,
		Version: adj.Version, RefusalReason: adj.RefusalReason}
}

func chartMeta(meta stockRow) ChartMeta {
	return ChartMeta{InsCode: meta.InsCode, Symbol: meta.SymbolFA, NameFA: meta.NameFA,
		Market: meta.Market, Board: meta.Board, SectorCode: meta.SectorCode,
		SectorFA: meta.SectorFA, Adjustment: buildAdjustment(meta),
		HistoryFrom: meta.AdjustedFirstBar}
}

// chartRevision is what a chart compares between polls. The newest stored
// session moves on every ingest, and computed_at moves whenever the
// adjustment is recomputed — which re-scales every bar before the newest
// action, i.e. restates pages a chart may already hold.
func chartRevision(meta stockRow) string {
	rev := formatDate(meta.LastBar) + "|"
	if meta.AdjustmentVersion != nil {
		rev += *meta.AdjustmentVersion
	}
	rev += "|"
	if meta.ComputedAt != nil {
		rev += meta.ComputedAt.UTC().Format(time.RFC3339Nano)
	}
	return rev
}

// buildChartBars projects rows onto chart sessions. Pure function (unit
// tested). `rows` arrives NEWEST-FIRST and up to one longer than `limit`,
// exactly as the query returns it; the extra row proves has_more and is the
// oldest, so dropping it never loses the newest session.
func buildChartBars(meta stockRow, rows []barRow, limit int, now time.Time) ChartBars {
	out := ChartBars{
		Meta:     chartMeta(meta),
		DataAge:  buildDataAge(newestRosterBar([]stockRow{meta}), now),
		Revision: chartRevision(meta),
		Items:    make([]ChartBar, 0, min(len(rows), limit)),
	}
	// The block is measured over this ONE share, so the roster's sentence —
	// "the newest stored session across the roster" — would misstate what it
	// measured.
	out.DataAge.Note = chartDataAgeNote
	limited := rows
	if len(limited) > limit {
		limited = limited[:limit]
		out.HasMore = true
	}
	for i := len(limited) - 1; i >= 0; i-- {
		bar := chartBarOf(limited[i])
		if bar.Traded && bar.Open == nil {
			out.DefectiveBars++
		}
		out.Items = append(out.Items, bar)
	}
	out.Notes = chartNotes(out.Meta.Adjustment, out.DefectiveBars)
	return out
}

// chartDataAgeNote is dataAgeNote for a block measured over one share.
const chartDataAgeNote = "Tehran equity bars do not refresh themselves: TSETMC is unreachable " +
	"from the production host, so bars arrive only when an operator runs the fetch script " +
	"from a network that can reach it. This block is the age of THIS share's newest stored " +
	"session (a halted session is stored too, so it moves with every fetch), measured " +
	"against today rather than against the page on screen."

// chartBarOf is one row, adjusted. The factor multiplies every PRICE and
// nothing that counts what happened: volume, trades and turnover stay as the
// exchange recorded them (see buildBarsResponse).
func chartBarOf(r barRow) ChartBar {
	f := r.Factor
	bar := ChartBar{
		Day:    dayFloor(r.TradeDate),
		Close:  r.FinalClose * f,
		Volume: r.Volume, Trades: r.TradeCount, Value: r.Value,
		Traded: r.Volume > 0 || r.TradeCount > 0,
		Factor: f,
	}
	if !bar.Traded {
		// A halt: TSETMC's zeros and its carried reference price. The close
		// stays (it is what the next session's limit is measured from) and is
		// labelled by traded=false; no range and no last trade are invented.
		return bar
	}
	last := r.Close * f
	bar.LastTrade = &last
	if r.Open <= 0 || r.High <= 0 || r.Low <= 0 {
		// A traded session with a zero in its range is a defective row. Its
		// official close is still the exchange's; its range is not drawable.
		return bar
	}
	o, hi, lo := r.Open*f, r.High*f, r.Low*f
	bar.Open, bar.High, bar.Low = &o, &hi, &lo
	// Compared on the RAW prints, where the exchange's numbers are exact; a
	// comparison after multiplying by a factor could flip on rounding alone.
	bar.CloseOutsideRange = r.FinalClose < r.Low || r.FinalClose > r.High
	return bar
}

// chartNotes are the sentences a reader needs to read an equity chart right.
func chartNotes(adj Adjustment, defective int) []string {
	notes := []string{
		fmt.Sprintf("Prices are rials (IRR), back-adjusted for corporate actions "+
			"(%s, %d action(s) applied): every session before an action is multiplied by "+
			"its cumulative factor so the series is continuous. Volume and turnover are "+
			"not scaled.", adj.Version, adj.ActionsApplied),
		"The close is the official closing price (قیمت پایانی), which every return in " +
			"this application is measured on. It is volume-weighted over the session and " +
			"can sit outside the traded high–low; the last trade travels beside it.",
		"A halted session has no trade: TSETMC carries the previous reference price, so " +
			"it is drawn as a gap and left out of every indicator rather than drawn as a " +
			"flat day.",
	}
	if adj.Reopenings > 0 {
		notes = append(notes, fmt.Sprintf(
			"This series contains %d reopening auction(s) whose move exceeds a normal "+
				"session's price limit; the exchange lifts the limit when an instrument "+
				"resumes after a suspension, so a move spanning one is not a session move.",
			adj.Reopenings))
	}
	if adj.PreListingBars > 0 {
		notes = append(notes, fmt.Sprintf(
			"%d stored bar(s) before the first trade (%s) are par-value placeholders and "+
				"are not part of the adjusted series.", adj.PreListingBars, adj.AdjustedFirstBar))
	}
	if defective > 0 {
		notes = append(notes, fmt.Sprintf(
			"%d traded session(s) on this page carry a zero open, high or low; their "+
				"range is not drawn, their official close is.", defective))
	}
	return notes
}
