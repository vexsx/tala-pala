package prices

// Multi-timeframe trend alignment (Addendum 20) — the READ side.
//
// This file serves a TECHNICAL INDICATOR and nothing else. Go computes none of
// it: no moving averages, no candle synthesis, no resampling, no trend calls.
// The quant lives in prediction-python/app/models/trend_alignment.py, which
// owns the definitions (EMA 26/48/220 over CLOSED candles only) and the
// idempotent event log; re-deriving any of it here would create a second,
// silently diverging answer to the same question. What follows is a projection
// of two persisted tables — trend_alignment_states and trend_alignment_events —
// onto the wire contract.
//
// Nothing here touches model input, model selection, prediction confidence,
// intervals or the buy/sell decision policy. It is an overlay the user reads,
// served from its own tables, and it must stay that way.

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// trendAlignmentSymbols is the symbol set the evaluator covers, in report
// order. It is deliberately narrower than KnownSymbols: the 4H and 1H legs
// need an hourly candle history that only these two symbols have, so a request
// for anything else is a client bug rather than an empty result.
var trendAlignmentSymbols = []string{"IR_GOLD_18K", "XAUUSD"}

// trendTimeframes are the three legs of the alignment, slowest first. The keys
// match AlignmentResult.candle_identity() on the Python side.
var trendTimeframes = []string{"1d", "4h", "1h"}

const (
	defaultTrendEventLimit = 20
	maxTrendEventLimit     = 100
)

// Configuration echoed when a symbol has never been evaluated. These mirror
// TrendConfig in prediction-python/app/models/trend_alignment.py and the column
// defaults of migration 0019: they describe the settings that WOULD be used,
// and are never presented as a measurement.
const (
	defaultTrendMaType = "ema"
	defaultTrendFast   = 26
	defaultTrendMid    = 48
	defaultTrendSlow   = 220
)

const (
	alignmentNotAligned  = "not_aligned"
	alignmentFullBullish = "full_bullish"
	alignmentFullBearish = "full_bearish"
)

// Reasons attached to a timeframe the stored state does not describe. They are
// distinguishable on purpose: "never evaluated at all" and "evaluated, but this
// leg is missing from the row" are different operational problems.
const (
	reasonNeverEvaluated = "never_evaluated"
	reasonNotEvaluated   = "not_evaluated"
	reasonUnreadable     = "unreadable_state"
)

func trendSymbolSupported(symbol string) bool {
	for _, s := range trendAlignmentSymbols {
		if s == symbol {
			return true
		}
	}
	return false
}

func unsupportedTrendSymbol(raw string) error {
	return fmt.Errorf("symbol must be one of %s, got %q",
		strings.Join(trendAlignmentSymbols, ", "), raw)
}

// ParseTrendSymbol resolves ?symbol= for the state read. An absent symbol means
// the primary one, as on /market/candles and /market/provider-gap. An
// unsupported symbol is refused rather than silently substituted: a caller
// asking about USD_IRT must never be handed gold's answer under its own name.
func ParseTrendSymbol(raw string) (string, error) {
	if raw == "" {
		return trendAlignmentSymbols[0], nil
	}
	if !trendSymbolSupported(raw) {
		return "", unsupportedTrendSymbol(raw)
	}
	return raw, nil
}

// ParseTrendEventSymbol resolves ?symbol= for the event log, where an absent
// symbol means "every evaluated symbol" (returned as ""): the log is a feed of
// transitions and each item names its own symbol, so an unfiltered read is
// meaningful in a way an unfiltered state read is not.
func ParseTrendEventSymbol(raw string) (string, error) {
	if raw == "" {
		return "", nil
	}
	if !trendSymbolSupported(raw) {
		return "", unsupportedTrendSymbol(raw)
	}
	return raw, nil
}

// ParseTrendEventLimit resolves ?limit=: empty means the default, and anything
// non-numeric or outside [1, maxTrendEventLimit] is an error.
//
// Like the news endpoint (and unlike the older market reads, which clamp
// silently), this one refuses: a caller that asked for 500 events and got 100
// without being told cannot tell "that is all there is" from "you were cut off",
// and a transition log is exactly where that difference matters.
func ParseTrendEventLimit(raw string) (int, error) {
	if raw == "" {
		return defaultTrendEventLimit, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("limit must be an integer, got %q", raw)
	}
	if n < 1 || n > maxTrendEventLimit {
		return 0, fmt.Errorf("limit must be between 1 and %d, got %d", maxTrendEventLimit, n)
	}
	return n, nil
}

// trendStateRow is trend_alignment_states as scanned. Columns that exist for
// the writer's benefit (state_version, updated_at, the candle-close idempotency
// triple) are not selected: the client is told what is true, not how the
// evaluator keeps itself honest.
type trendStateRow struct {
	Symbol            string
	Alignment         string
	PreviousAlignment *string
	Timeframes        []byte
	MaType            string
	FastPeriod        int
	MidPeriod         int
	SlowPeriod        int
	DataFresh         bool
	LastBullishAlert  *time.Time
	LastBearishAlert  *time.Time
	CalculatedAt      time.Time
}

const trendStateSelect = `
	SELECT symbol, alignment, previous_alignment, timeframes, ma_type,
	       fast_period, mid_period, slow_period, data_fresh,
	       last_bullish_alert_at, last_bearish_alert_at, calculated_at
	FROM trend_alignment_states
	WHERE symbol = $1`

// trendLastTransitionSelect reads when this symbol last ENTERED a full
// alignment. The event log records entries only — losing an alignment is not an
// event — so this is the newest transition the system committed to, and max()
// over zero rows yields NULL rather than an error.
const trendLastTransitionSelect = `
	SELECT max(occurred_at) FROM trend_alignment_events WHERE symbol = $1`

// unavailableTimeframe is the placeholder for a leg the stored state does not
// describe. Every measurement is null and the trend is "unavailable": a missing
// evaluation is reported as missing, never filled in with a plausible number.
func unavailableTimeframe(timeframe, maType, reason string) map[string]any {
	return map[string]any{
		"timeframe":         timeframe,
		"trend":             "unavailable",
		"price":             nil,
		"ma26":              nil,
		"ma48":              nil,
		"ma220":             nil,
		"candle_open_time":  nil,
		"candle_close_time": nil,
		"confirmed":         false,
		"data_fresh":        false,
		"ma_type":           maType,
		"history_points":    0,
		"reason":            reason,
	}
}

// projectTimeframe copies the stored leg onto the contract's key set.
//
// Values are passed through verbatim (as raw JSON) — Go must not round, rescale
// or recompute what Python measured — but the key set is ours: a key the
// evaluator adds to the JSONB later cannot leak to a client, and a key it omits
// becomes an explicit null instead of a hole.
func projectTimeframe(timeframe, maType string, raw json.RawMessage) map[string]any {
	stored := map[string]json.RawMessage{}
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &stored); err != nil {
			return unavailableTimeframe(timeframe, maType, reasonUnreadable)
		}
	}
	out := unavailableTimeframe(timeframe, maType, reasonNotEvaluated)
	for key := range out {
		if key == "timeframe" {
			continue // always the leg we asked for, never the row's label
		}
		if v, ok := stored[key]; ok {
			out[key] = v
		}
	}
	return out
}

// normalizeTimeframes turns the stored timeframes JSONB into exactly the three
// legs of the contract. missingReason explains the legs that are absent.
func normalizeTimeframes(raw []byte, maType, missingReason string) map[string]any {
	stored := map[string]json.RawMessage{}
	readable := true
	if len(raw) > 0 {
		if err := json.Unmarshal(raw, &stored); err != nil {
			readable = false
		}
	}
	out := make(map[string]any, len(trendTimeframes))
	for _, tf := range trendTimeframes {
		if !readable {
			out[tf] = unavailableTimeframe(tf, maType, reasonUnreadable)
			continue
		}
		leg, ok := stored[tf]
		if !ok {
			out[tf] = unavailableTimeframe(tf, maType, missingReason)
			continue
		}
		out[tf] = projectTimeframe(tf, maType, leg)
	}
	return out
}

// resolveLastAlertAt picks the alert timestamp that belongs to the alignment
// being reported: the bullish column while bullish, the bearish column while
// bearish. While NOT aligned there is no current alert, so the later of the two
// is reported — the most recent alert this symbol raised, about the alignment
// that has since broken.
func resolveLastAlertAt(alignment string, bullish, bearish *time.Time) *time.Time {
	switch alignment {
	case alignmentFullBullish:
		return utcPtr(bullish)
	case alignmentFullBearish:
		return utcPtr(bearish)
	}
	if bullish != nil && bearish != nil {
		if bearish.After(*bullish) {
			return utcPtr(bearish)
		}
		return utcPtr(bullish)
	}
	if bullish != nil {
		return utcPtr(bullish)
	}
	return utcPtr(bearish)
}

// utcPtr normalizes a nullable timestamp to UTC without aliasing the caller's
// value (every timestamp on this contract is serialized UTC).
func utcPtr(t *time.Time) *time.Time {
	if t == nil {
		return nil
	}
	u := t.UTC()
	return &u
}

// buildTrendAlignmentResponse renders the state contract. A nil row means the
// evaluator has never run for this symbol: that is a 200 saying so, not a 404
// and not a fabricated "not_aligned" measurement — the note is the difference.
// Pure function (unit tested): no clock, no database.
func buildTrendAlignmentResponse(symbol string, row *trendStateRow, lastTransitionAt *time.Time) map[string]any {
	if row == nil {
		return map[string]any{
			"symbol":             symbol,
			"alignment":          alignmentNotAligned,
			"previous_alignment": nil,
			"timeframes":         normalizeTimeframes(nil, defaultTrendMaType, reasonNeverEvaluated),
			"ma_type":            defaultTrendMaType,
			"periods": map[string]int{
				"fast": defaultTrendFast, "mid": defaultTrendMid, "slow": defaultTrendSlow,
			},
			"data_fresh":         false,
			"calculated_at":      nil,
			"last_transition_at": nil,
			"last_alert_at":      nil,
			"note":               reasonNeverEvaluated,
		}
	}
	return map[string]any{
		"symbol":             row.Symbol,
		"alignment":          row.Alignment,
		"previous_alignment": row.PreviousAlignment,
		"timeframes":         normalizeTimeframes(row.Timeframes, row.MaType, reasonNotEvaluated),
		"ma_type":            row.MaType,
		"periods": map[string]int{
			"fast": row.FastPeriod, "mid": row.MidPeriod, "slow": row.SlowPeriod,
		},
		"data_fresh":         row.DataFresh,
		"calculated_at":      row.CalculatedAt.UTC(),
		"last_transition_at": utcPtr(lastTransitionAt),
		"last_alert_at":      resolveLastAlertAt(row.Alignment, row.LastBullishAlert, row.LastBearishAlert),
		"note":               nil,
	}
}

// TrendAlignment implements GET /api/v1/market/trend-alignment?symbol=.
func (h *Handler) TrendAlignment(w http.ResponseWriter, r *http.Request) {
	raw := r.URL.Query().Get("symbol")
	symbol, err := ParseTrendSymbol(raw)
	if err != nil {
		httpserver.BadRequest(w, err.Error(), map[string]any{
			"symbol": raw, "supported": trendAlignmentSymbols})
		return
	}

	ctx := r.Context()
	var row trendStateRow
	err = h.Pool.QueryRow(ctx, trendStateSelect, symbol).Scan(
		&row.Symbol, &row.Alignment, &row.PreviousAlignment, &row.Timeframes,
		&row.MaType, &row.FastPeriod, &row.MidPeriod, &row.SlowPeriod,
		&row.DataFresh, &row.LastBullishAlert, &row.LastBearishAlert, &row.CalculatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		httpserver.JSON(w, http.StatusOK, buildTrendAlignmentResponse(symbol, nil, nil))
		return
	}
	if err != nil {
		h.Log.Error("trend_alignment_state", "error", err, "symbol", symbol)
		httpserver.Internal(w, "database error")
		return
	}

	var lastTransitionAt *time.Time
	if err := h.Pool.QueryRow(ctx, trendLastTransitionSelect, symbol).Scan(&lastTransitionAt); err != nil {
		// The state itself is intact; a missing transition time is worth a log
		// line, not a 500 that hides the alignment the user asked for.
		h.Log.Error("trend_alignment_last_transition", "error", err, "symbol", symbol)
		lastTransitionAt = nil
	}

	httpserver.JSON(w, http.StatusOK, buildTrendAlignmentResponse(symbol, &row, lastTransitionAt))
}

// trendEventRow is trend_alignment_events as scanned.
type trendEventRow struct {
	ID                int64
	Symbol            string
	Alignment         string
	PreviousAlignment *string
	OccurredAt        time.Time
	Candle1h          time.Time
	Candle4h          time.Time
	Candle1d          time.Time
	Timeframes        []byte
	MaType            string
}

// trendEventItem is one entry INTO a full alignment. Candles is the closed-
// candle triple the entry was drawn from — the same triple the unique index
// uses as the idempotency key, which is what makes "why did this fire now?"
// answerable from the response alone. alert_event_id stays internal: it is the
// writer's crash-recovery link, and the client has /api/v1/alerts/events.
type trendEventItem struct {
	ID                int64                `json:"id"`
	Symbol            string               `json:"symbol"`
	Alignment         string               `json:"alignment"`
	PreviousAlignment *string              `json:"previous_alignment"`
	OccurredAt        time.Time            `json:"occurred_at"`
	Candles           map[string]time.Time `json:"candles"`
	Timeframes        map[string]any       `json:"timeframes"`
	MaType            string               `json:"ma_type"`
}

type trendEventsResponse struct {
	Items []trendEventItem `json:"items"`
	Count int              `json:"count"`
}

// trendEventsSelect reads the transition log for the requested symbols. The
// symbol filter is an array so one statement serves both "this symbol" and
// "every evaluated symbol", and a row left behind by a symbol that is no longer
// evaluated can never appear in either.
const trendEventsSelect = `
	SELECT id, symbol, alignment, previous_alignment, occurred_at,
	       latest_1h_candle_close, latest_4h_candle_close, latest_1d_candle_close,
	       timeframes, ma_type
	FROM trend_alignment_events
	WHERE symbol = ANY($1)
	ORDER BY occurred_at DESC, id DESC
	LIMIT $2`

// sortTrendEventRows applies the documented order: newest transition first,
// ties broken by descending id. trendEventsSelect already returns rows this
// way; the comparator exists so the ordering is expressible (and testable)
// without a database, and re-applying it costs nothing at these page sizes.
func sortTrendEventRows(rows []trendEventRow) {
	sort.SliceStable(rows, func(i, j int) bool {
		a, b := rows[i], rows[j]
		if !a.OccurredAt.Equal(b.OccurredAt) {
			return a.OccurredAt.After(b.OccurredAt)
		}
		return a.ID > b.ID
	})
}

// buildTrendEventsResponse orders, truncates to limit and projects the rows.
// Pure function (unit tested). The truncation is repeated here rather than
// trusted to the SQL LIMIT so the page size holds whatever the caller of this
// function passed in.
func buildTrendEventsResponse(rows []trendEventRow, limit int) trendEventsResponse {
	sortTrendEventRows(rows)
	if limit >= 0 && len(rows) > limit {
		rows = rows[:limit]
	}
	out := trendEventsResponse{Items: make([]trendEventItem, 0, len(rows))}
	for _, r := range rows {
		out.Items = append(out.Items, trendEventItem{
			ID:                r.ID,
			Symbol:            r.Symbol,
			Alignment:         r.Alignment,
			PreviousAlignment: r.PreviousAlignment,
			OccurredAt:        r.OccurredAt.UTC(),
			Candles: map[string]time.Time{
				"1d": r.Candle1d.UTC(), "4h": r.Candle4h.UTC(), "1h": r.Candle1h.UTC(),
			},
			Timeframes: normalizeTimeframes(r.Timeframes, r.MaType, reasonNotEvaluated),
			MaType:     r.MaType,
		})
	}
	out.Count = len(out.Items)
	return out
}

// TrendAlignmentEvents implements
// GET /api/v1/market/trend-alignment/events?symbol=&limit=20.
func (h *Handler) TrendAlignmentEvents(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	rawSymbol := q.Get("symbol")
	symbol, err := ParseTrendEventSymbol(rawSymbol)
	if err != nil {
		httpserver.BadRequest(w, err.Error(), map[string]any{
			"symbol": rawSymbol, "supported": trendAlignmentSymbols})
		return
	}
	rawLimit := q.Get("limit")
	limit, err := ParseTrendEventLimit(rawLimit)
	if err != nil {
		httpserver.BadRequest(w, err.Error(), map[string]any{"limit": rawLimit})
		return
	}

	symbols := trendAlignmentSymbols
	if symbol != "" {
		symbols = []string{symbol}
	}
	rows, err := h.Pool.Query(r.Context(), trendEventsSelect, symbols, limit)
	if err != nil {
		h.Log.Error("trend_alignment_events", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	scanned := []trendEventRow{}
	for rows.Next() {
		var e trendEventRow
		if err := rows.Scan(&e.ID, &e.Symbol, &e.Alignment, &e.PreviousAlignment,
			&e.OccurredAt, &e.Candle1h, &e.Candle4h, &e.Candle1d,
			&e.Timeframes, &e.MaType); err != nil {
			h.Log.Error("trend_alignment_events_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		scanned = append(scanned, e)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("trend_alignment_events_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	httpserver.JSON(w, http.StatusOK, buildTrendEventsResponse(scanned, limit))
}
