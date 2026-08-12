package prices

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Per-user chart annotations: the trend lines, boxes and labels a trader draws
// on top of the candles and expects to still be there tomorrow, on another
// device. Nothing here reads or influences a forecast — a drawing is opinion,
// stored verbatim and handed back to the same person who made it.
//
// The security property this file exists to hold: the user id in every
// statement comes from the verified token in the request context and from
// nowhere else. UPDATE and DELETE carry the same predicate, so a request that
// names another user's drawing id matches zero rows and is answered 404 —
// indistinguishable from an id that never existed.

// MaxDrawingsPerChart caps one user's annotations on one (symbol, interval)
// chart. These are unbounded, user-authored rows: without a ceiling a single
// account can grow the table forever. 200 is far past any usable chart.
const MaxDrawingsPerChart = 200

// maxDrawingsListed bounds one list response. It is deliberately NOT the same
// number as MaxDrawingsPerChart and does not depend on it being honoured: the
// cap is enforced on INSERT only, can be overshot by concurrent inserts, and
// says nothing about rows written before it existed. A response that trusted
// the cap would be unbounded in exactly the cases where a bound matters, so
// the read carries its own ceiling. The headroom above the cap means a chart
// legitimately at 200 still comes back whole.
const maxDrawingsListed = MaxDrawingsPerChart + 50

// maxDrawingStyleBytes bounds the opaque style blob. The API never interprets
// style, so a size limit is the only thing standing between a client and
// megabytes of arbitrary JSON per drawing.
const maxDrawingStyleBytes = 4096

// maxDrawingTime bounds an anchor timestamp. Unix SECONDS: a value past this
// is either a bug or milliseconds sent by mistake, and converting an
// out-of-range float to int64 is implementation-defined in Go, so the bound is
// load-bearing rather than cosmetic.
const maxDrawingTime = 1e12 // ~ year 33658

// drawingIntervalOrder is the canonical chart interval set, in duration order
// because that is how a timeframe picker lists them. Nothing finer than 5m
// appears: the tick history has no sub-5-minute resolution, so a finer chart
// cannot be drawn and therefore cannot be annotated.
var drawingIntervalOrder = []string{
	"5m", "10m", "15m", "20m", "30m", "45m",
	"1h", "2h", "3h", "4h", "6h", "8h", "12h",
	"1d", "2d", "3d", "1w",
}

var drawingIntervals = func() map[string]bool {
	m := make(map[string]bool, len(drawingIntervalOrder))
	for _, iv := range drawingIntervalOrder {
		m[iv] = true
	}
	return m
}()

// drawingPointCounts is both the accepted drawing_type set — it must mirror
// the CHECK constraint in migration 0020 — and the exact number of anchors
// each type carries. Exact, not minimum: a rectangle with three corners is a
// client bug that renders wrong, and rejecting it here is cheaper than
// discovering it on someone's chart.
var drawingPointCounts = map[string]int{
	"trend_line":      2,
	"horizontal_line": 1,
	"vertical_line":   1,
	"rectangle":       2,
	"price_range":     2,
	"date_range":      2,
	"measure":         2,
	"fib_retracement": 2,
	"text":            1,
}

const drawingColumns = `id, symbol, "interval", drawing_type, points, style, ` +
	`locked, visible, created_at, updated_at`

// The LIMIT is the response's own bound, not a restatement of
// MaxDrawingsPerChart — see maxDrawingsListed. The caller passes one more than
// it intends to return so that "there are more" can be told from "there are
// exactly this many" without a second count.
const sqlListDrawings = `
	SELECT ` + drawingColumns + `
	FROM chart_drawings
	WHERE user_id = $1 AND symbol = $2 AND "interval" = $3
	ORDER BY id ASC
	LIMIT $4`

// The per-chart cap lives inside the statement so a client cannot beat a
// check-then-insert with concurrent requests. Under READ COMMITTED two
// simultaneous inserts can both observe the pre-insert count, so the ceiling
// may be overshot by at most the number of in-flight requests — bounded,
// unlike a cap that is not enforced at all. Zero rows back means "chart full".
//
// The casts are load-bearing, not decoration: parameters in the target list of
// an INSERT ... SELECT are analysed before the assignment to the target
// columns, so without them Postgres has nothing to infer from and rejects the
// statement outright ("could not determine data type of parameter $1").
const sqlInsertDrawing = `
	INSERT INTO chart_drawings
		(user_id, symbol, "interval", drawing_type, points, style, locked, visible)
	SELECT $1::uuid, $2::text, $3::text, $4::text, $5::jsonb, $6::jsonb,
	       $7::boolean, $8::boolean
	WHERE (SELECT count(*) FROM chart_drawings
	       WHERE user_id = $1 AND symbol = $2 AND "interval" = $3) < $9::int
	RETURNING ` + drawingColumns

// AND user_id = $7 is the whole point: an id owned by somebody else matches
// zero rows, the handler answers 404, and the caller learns neither that the
// drawing exists nor anything about it.
//
// symbol and "interval" are absent from SET and present in the predicate on
// purpose. A drawing belongs to the chart it was drawn on; moving one is not a
// use case, and while it was writable the per-chart cap was decorative —
// nothing counts rows on UPDATE, so 200 drawings could be created on one chart
// and then walked onto another, repeatedly, for unbounded rows per account.
// Leaving the two columns out of SET makes a move impossible rather than
// merely unimplemented; matching them in WHERE turns a body that names a
// different chart into zero rows, which the handler then explains as a 400.
const sqlUpdateDrawing = `
	UPDATE chart_drawings
	SET drawing_type = $1, points = $2, style = $3,
	    locked = $4, visible = $5, updated_at = now()
	WHERE id = $6 AND user_id = $7 AND symbol = $8 AND "interval" = $9
	RETURNING ` + drawingColumns

// sqlDrawingChart answers the one question the UPDATE predicate cannot: zero
// rows means either "no such drawing for this user" or "that drawing is on a
// different chart", and only the second is the client's fault. Read solely on
// that error path, with the same owner predicate, so another user's id is
// still indistinguishable from an id that never existed.
const sqlDrawingChart = `
	SELECT symbol, "interval"
	FROM chart_drawings
	WHERE id = $1 AND user_id = $2`

const sqlDeleteDrawing = `
	DELETE FROM chart_drawings
	WHERE id = $1 AND user_id = $2`

type drawingDTO struct {
	ID          int64           `json:"id"`
	Symbol      string          `json:"symbol"`
	Interval    string          `json:"interval"`
	DrawingType string          `json:"drawing_type"`
	Points      json.RawMessage `json:"points"`
	Style       json.RawMessage `json:"style"`
	Locked      bool            `json:"locked"`
	Visible     bool            `json:"visible"`
	CreatedAt   time.Time       `json:"created_at"`
	UpdatedAt   time.Time       `json:"updated_at"`
}

type drawingRequest struct {
	Symbol      string          `json:"symbol"`
	Interval    string          `json:"interval"`
	DrawingType string          `json:"drawing_type"`
	Points      json.RawMessage `json:"points"`
	Style       json.RawMessage `json:"style"`
	Locked      *bool           `json:"locked"`
	Visible     *bool           `json:"visible"`

	// Server-owned fields, accepted and thrown away. httpserver.DecodeJSON
	// sets DisallowUnknownFields, and a GET hands back id, created_at and
	// updated_at — so without somewhere for those three to land, PUTting back
	// a drawing that was just fetched is a 400 on the field names the server
	// itself chose. That round trip is the first thing an edit-then-save does.
	//
	// They stay server-owned: json.RawMessage parks any JSON value without
	// interpreting it, nothing reads these fields, and ValidateDrawingRequest
	// builds drawingRow out of the named columns only — so there is no path
	// from a request body to id or to either timestamp. id comes from the URL,
	// created_at from the table default, updated_at from now().
	IgnoredID        json.RawMessage `json:"id"`
	IgnoredCreatedAt json.RawMessage `json:"created_at"`
	IgnoredUpdatedAt json.RawMessage `json:"updated_at"`
}

// drawingPointInput is one anchor as it arrives. The coordinates stay raw
// rather than decoding into float64 so that absent, non-numeric and
// out-of-range can each be told apart and reported for what they are — a
// float64 field would turn all three into an indistinguishable zero.
type drawingPointInput struct {
	T     json.RawMessage `json:"t"`
	Price json.RawMessage `json:"price"`
}

// drawingPoint is the stored form: t truncated to whole unix seconds (UTC),
// price as given.
type drawingPoint struct {
	T     int64   `json:"t"`
	Price float64 `json:"price"`
}

// drawingRow is a request after validation: exactly the values that go into
// the table, nothing a caller sent that the schema does not name.
type drawingRow struct {
	Symbol      string
	Interval    string
	DrawingType string
	Points      json.RawMessage
	Style       json.RawMessage
	Locked      bool
	Visible     bool
}

// ValidateDrawingRequest is the pure validation for create/update payloads. It
// returns the storage-ready row alongside the per-field problems map, so a
// handler can only ever write a normalized shape: points and style are
// re-derived from the parsed form rather than passed through, which keeps
// stray keys and non-canonical numbers out of JSONB.
func ValidateDrawingRequest(req drawingRequest) (drawingRow, map[string]any) {
	problems := map[string]any{}
	row := drawingRow{
		Symbol:      strings.ToUpper(strings.TrimSpace(req.Symbol)),
		Interval:    strings.ToLower(strings.TrimSpace(req.Interval)),
		DrawingType: strings.ToLower(strings.TrimSpace(req.DrawingType)),
		Locked:      req.Locked != nil && *req.Locked,
		Visible:     req.Visible == nil || *req.Visible,
	}
	if !KnownSymbols[row.Symbol] {
		problems["symbol"] = "unknown or missing symbol"
	}
	if !drawingIntervals[row.Interval] {
		problems["interval"] = "must be one of " + strings.Join(drawingIntervalOrder, ", ")
	}
	want, knownType := drawingPointCounts[row.DrawingType]
	if !knownType {
		problems["drawing_type"] = "unknown drawing type"
	}
	points, pointProblem := parseDrawingPoints(req.Points, want, knownType)
	if pointProblem != "" {
		problems["points"] = pointProblem
	}
	row.Points = points
	style, styleProblem := parseDrawingStyle(req.Style)
	if styleProblem != "" {
		problems["style"] = styleProblem
	}
	row.Style = style

	if len(problems) == 0 {
		return row, nil
	}
	return row, problems
}

// drawingChartMismatch names the fields whose values would move a drawing to a
// different chart than the one it was drawn on. nil means the body agrees with
// the stored row — which is what a client PUTting back what it fetched sends,
// so the ordinary edit-and-save never sees this. Both sides are compared after
// normalization, so " ir_gold_18k " and "IR_GOLD_18K" are the same chart.
func drawingChartMismatch(storedSymbol, storedInterval string, row drawingRow) map[string]any {
	problems := map[string]any{}
	if row.Symbol != storedSymbol {
		problems["symbol"] = fmt.Sprintf(
			"drawing belongs to %s and cannot be moved; delete it and draw it on %s",
			storedSymbol, row.Symbol)
	}
	if row.Interval != storedInterval {
		problems["interval"] = fmt.Sprintf(
			"drawing belongs to the %s chart and cannot be moved; delete it and draw it on %s",
			storedInterval, row.Interval)
	}
	if len(problems) == 0 {
		return nil
	}
	return problems
}

// parseDrawingPoints validates the anchor array and returns it in canonical
// form. want is only meaningful when the type is known; an unknown type has
// already been reported as its own problem, so the count is not second-guessed
// and the caller is not told two things about one mistake.
func parseDrawingPoints(raw json.RawMessage, want int, knownType bool) (json.RawMessage, string) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return nil, "required"
	}
	var in []drawingPointInput
	// A JSON null decodes into a nil slice without error, which is not an
	// array of anchors however permissive one feels.
	if err := json.Unmarshal(raw, &in); err != nil || in == nil {
		return nil, "must be an array of {t, price} objects"
	}
	if knownType && len(in) != want {
		return nil, fmt.Sprintf("must contain exactly %d point(s), got %d", want, len(in))
	}
	out := make([]drawingPoint, len(in))
	for i, p := range in {
		if p.T == nil || p.Price == nil {
			return nil, fmt.Sprintf("point %d must have both t and price", i)
		}
		t, tOK := finiteDrawingCoord(p.T)
		price, priceOK := finiteDrawingCoord(p.Price)
		if !tOK || !priceOK {
			return nil, fmt.Sprintf("point %d must have finite numeric t and price", i)
		}
		if math.Abs(t) > maxDrawingTime {
			return nil, fmt.Sprintf("point %d t must be unix seconds", i)
		}
		out[i] = drawingPoint{T: int64(t), Price: price}
	}
	encoded, err := json.Marshal(out)
	if err != nil {
		return nil, "must be an array of {t, price} objects"
	}
	return encoded, ""
}

// finiteDrawingCoord reads one raw coordinate. It insists on a bare JSON
// number: a quoted "1750000000" is a client bug worth naming rather than
// coercing, and coercion here is how a chart ends up silently anchored to
// whatever a string happened to parse as. A literal that overflows float64
// decodes to ±Inf, and a non-finite coordinate poisons every pixel derived
// from it, so those are refused too.
func finiteDrawingCoord(raw json.RawMessage) (float64, bool) {
	s := string(bytes.TrimSpace(raw))
	// The decoder has already validated the token, so the first byte alone
	// separates a number from a string, bool, null, object or array.
	if s == "" || (s[0] != '-' && (s[0] < '0' || s[0] > '9')) {
		return 0, false
	}
	v, err := strconv.ParseFloat(s, 64)
	if err != nil || math.IsNaN(v) || math.IsInf(v, 0) {
		return 0, false
	}
	return v, true
}

// parseDrawingStyle accepts an absent style as {} and otherwise insists on a
// JSON object. Passed through verbatim once validated — it is presentation the
// API has no business rewriting.
func parseDrawingStyle(raw json.RawMessage) (json.RawMessage, string) {
	if len(bytes.TrimSpace(raw)) == 0 {
		return json.RawMessage(`{}`), ""
	}
	if len(raw) > maxDrawingStyleBytes {
		return nil, fmt.Sprintf("must be at most %d bytes", maxDrawingStyleBytes)
	}
	var m map[string]any
	// A JSON null unmarshals into a nil map without error; an array or scalar
	// fails outright. Both are "not an object".
	if err := json.Unmarshal(raw, &m); err != nil || m == nil {
		return nil, "must be a JSON object"
	}
	return raw, ""
}

// drawingUser resolves the caller from the request context. Every drawing
// statement takes its user id from here — never from the body, never from a
// query parameter.
func drawingUser(w http.ResponseWriter, r *http.Request) (httpserver.AuthUser, bool) {
	u, ok := httpserver.UserFromContext(r.Context())
	if !ok {
		httpserver.Unauthorized(w, "not authenticated")
	}
	return u, ok
}

// pgx.Rows satisfies pgx.Row, so one scan serves both the list query and the
// single-row RETURNING clauses.
func scanDrawing(row pgx.Row) (drawingDTO, error) {
	var d drawingDTO
	if err := row.Scan(&d.ID, &d.Symbol, &d.Interval, &d.DrawingType, &d.Points,
		&d.Style, &d.Locked, &d.Visible, &d.CreatedAt, &d.UpdatedAt); err != nil {
		return d, err
	}
	d.CreatedAt = d.CreatedAt.UTC()
	d.UpdatedAt = d.UpdatedAt.UTC()
	return d, nil
}

func drawingIDParam(w http.ResponseWriter, r *http.Request) (int64, bool) {
	id, err := strconv.ParseInt(chi.URLParam(r, "id"), 10, 64)
	if err != nil {
		httpserver.BadRequest(w, "invalid drawing id", nil)
		return 0, false
	}
	return id, true
}

// drawingListResponse is the list envelope. It exists as a function so the
// shape is written once and can be asserted by a test that round-trips a real
// response back through the update path.
//
// truncated is the honest half of the bound: a client that hits it has more
// drawings on this chart than one response carries, and is being shown a
// prefix in id order rather than everything.
func drawingListResponse(items []drawingDTO, truncated bool) map[string]any {
	return map[string]any{
		"items":     items,
		"count":     len(items),
		"limit":     maxDrawingsListed,
		"truncated": truncated,
	}
}

// ListDrawings implements GET /api/v1/chart/drawings?symbol=&interval=.
// Both filters are required: they are the index key.
//
// The response is bounded by the statement's own LIMIT. It is not bounded by
// MaxDrawingsPerChart — that cap is checked on INSERT and nowhere else, so the
// number of rows behind this query is not a number this handler gets to assume.
func (h *Handler) ListDrawings(w http.ResponseWriter, r *http.Request) {
	u, ok := drawingUser(w, r)
	if !ok {
		return
	}
	q := r.URL.Query()
	symbol := strings.ToUpper(strings.TrimSpace(q.Get("symbol")))
	interval := strings.ToLower(strings.TrimSpace(q.Get("interval")))
	problems := map[string]any{}
	if !KnownSymbols[symbol] {
		problems["symbol"] = "unknown or missing symbol"
	}
	if !drawingIntervals[interval] {
		problems["interval"] = "must be one of " + strings.Join(drawingIntervalOrder, ", ")
	}
	if len(problems) > 0 {
		httpserver.BadRequest(w, "invalid drawing query", problems)
		return
	}
	// One past the ceiling: the extra row, if it comes back, is the proof that
	// the chart holds more than a response carries. It is dropped, never sent.
	rows, err := h.Pool.Query(r.Context(), sqlListDrawings, u.ID, symbol, interval,
		maxDrawingsListed+1)
	if err != nil {
		h.Log.Error("chart_drawings_list", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	items := []drawingDTO{}
	for rows.Next() {
		d, err := scanDrawing(rows)
		if err != nil {
			h.Log.Error("chart_drawings_list_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		items = append(items, d)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("chart_drawings_list_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	truncated := len(items) > maxDrawingsListed
	if truncated {
		items = items[:maxDrawingsListed]
		h.Log.Warn("chart_drawings_list_truncated", "user_id", u.ID,
			"symbol", symbol, "interval", interval, "limit", maxDrawingsListed)
	}
	httpserver.JSON(w, http.StatusOK, drawingListResponse(items, truncated))
}

// CreateDrawing implements POST /api/v1/chart/drawings.
func (h *Handler) CreateDrawing(w http.ResponseWriter, r *http.Request) {
	u, ok := drawingUser(w, r)
	if !ok {
		return
	}
	var req drawingRequest
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	row, problems := ValidateDrawingRequest(req)
	if problems != nil {
		httpserver.BadRequest(w, "invalid drawing", problems)
		return
	}
	d, err := scanDrawing(h.Pool.QueryRow(r.Context(), sqlInsertDrawing,
		u.ID, row.Symbol, row.Interval, row.DrawingType, row.Points, row.Style,
		row.Locked, row.Visible, MaxDrawingsPerChart))
	if errors.Is(err, pgx.ErrNoRows) {
		httpserver.Conflict(w, fmt.Sprintf(
			"drawing limit reached (%d per symbol and interval)", MaxDrawingsPerChart))
		return
	}
	if err != nil {
		h.Log.Error("chart_drawings_create", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusCreated, d)
}

// UpdateDrawing implements PUT /api/v1/chart/drawings/{id}. A full replacement
// of everything a drawing is except which chart it is on, so dragging an
// anchor is one statement rather than a patch merge.
//
// The body still carries symbol and interval — a full replacement that omitted
// them would be a different, stranger shape, and the client has them anyway
// from the GET — but they may only restate where the drawing already is.
func (h *Handler) UpdateDrawing(w http.ResponseWriter, r *http.Request) {
	u, ok := drawingUser(w, r)
	if !ok {
		return
	}
	id, ok := drawingIDParam(w, r)
	if !ok {
		return
	}
	var req drawingRequest
	if !httpserver.DecodeJSON(w, r, &req) {
		return
	}
	row, problems := ValidateDrawingRequest(req)
	if problems != nil {
		httpserver.BadRequest(w, "invalid drawing", problems)
		return
	}
	d, err := scanDrawing(h.Pool.QueryRow(r.Context(), sqlUpdateDrawing,
		row.DrawingType, row.Points, row.Style, row.Locked, row.Visible,
		id, u.ID, row.Symbol, row.Interval))
	if errors.Is(err, pgx.ErrNoRows) {
		h.updateDrawingNoRows(w, r, id, u.ID, row)
		return
	}
	if err != nil {
		h.Log.Error("chart_drawings_update", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, d)
}

// updateDrawingNoRows explains an update that matched nothing. Three of the
// four predicates are "this drawing, owned by you" and the fourth is "on this
// chart", so one extra read splits a client error from a missing drawing.
// Somebody else's id and a nonexistent id stay the same answer on purpose: the
// read is owner-scoped too, so a cross-tenant guess falls into the 404 branch
// having revealed nothing.
func (h *Handler) updateDrawingNoRows(
	w http.ResponseWriter, r *http.Request, id int64, userID string, row drawingRow,
) {
	var storedSymbol, storedInterval string
	err := h.Pool.QueryRow(r.Context(), sqlDrawingChart, id, userID).
		Scan(&storedSymbol, &storedInterval)
	if errors.Is(err, pgx.ErrNoRows) {
		httpserver.NotFound(w, "drawing not found")
		return
	}
	if err != nil {
		h.Log.Error("chart_drawings_update_chart", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if problems := drawingChartMismatch(storedSymbol, storedInterval, row); problems != nil {
		httpserver.BadRequest(w, "a drawing cannot be moved to another chart", problems)
		return
	}
	// The row exists on exactly the chart the body named, yet the UPDATE
	// matched nothing: it was deleted between the two statements. That is a
	// 404 by the same reasoning as any other id that is no longer there.
	httpserver.NotFound(w, "drawing not found")
}

// DeleteDrawing implements DELETE /api/v1/chart/drawings/{id}.
func (h *Handler) DeleteDrawing(w http.ResponseWriter, r *http.Request) {
	u, ok := drawingUser(w, r)
	if !ok {
		return
	}
	id, ok := drawingIDParam(w, r)
	if !ok {
		return
	}
	tag, err := h.Pool.Exec(r.Context(), sqlDeleteDrawing, id, u.ID)
	if err != nil {
		h.Log.Error("chart_drawings_delete", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if tag.RowsAffected() == 0 {
		httpserver.NotFound(w, "drawing not found")
		return
	}
	w.WriteHeader(http.StatusNoContent)
}
