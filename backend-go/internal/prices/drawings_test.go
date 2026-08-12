package prices

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

func drawingReq(typ, points string) drawingRequest {
	return drawingRequest{
		Symbol:      "IR_GOLD_18K",
		Interval:    "1h",
		DrawingType: typ,
		Points:      json.RawMessage(points),
	}
}

// pointsFor builds a syntactically valid anchor array of length n.
func pointsFor(n int) string {
	parts := make([]string, n)
	for i := range parts {
		parts[i] = `{"t":1750000000,"price":72500000}`
	}
	return "[" + strings.Join(parts, ",") + "]"
}

// Every type in the CHECK constraint must round-trip with its own anchor
// count, and must be rejected with any other count. The counts here are the
// contract the frontend draws against; if one drifts, the drawing renders
// wrong rather than failing, so it is asserted rather than inferred.
func TestValidateDrawingRequestPointCounts(t *testing.T) {
	tests := []struct {
		typ  string
		want int
	}{
		{"trend_line", 2},
		{"horizontal_line", 1},
		{"vertical_line", 1},
		{"rectangle", 2},
		{"price_range", 2},
		{"date_range", 2},
		{"measure", 2},
		{"fib_retracement", 2},
		{"text", 1},
	}
	if len(tests) != len(drawingPointCounts) {
		t.Fatalf("table covers %d types, drawingPointCounts has %d",
			len(tests), len(drawingPointCounts))
	}
	for _, tc := range tests {
		t.Run(tc.typ, func(t *testing.T) {
			if got := drawingPointCounts[tc.typ]; got != tc.want {
				t.Fatalf("drawingPointCounts[%s] = %d, want %d", tc.typ, got, tc.want)
			}
			row, problems := ValidateDrawingRequest(drawingReq(tc.typ, pointsFor(tc.want)))
			if problems != nil {
				t.Fatalf("valid %s rejected: %v", tc.typ, problems)
			}
			if row.DrawingType != tc.typ || row.Symbol != "IR_GOLD_18K" || row.Interval != "1h" {
				t.Errorf("normalized row wrong: %+v", row)
			}
			if row.Visible != true || row.Locked != false {
				t.Errorf("defaults wrong: visible=%v locked=%v", row.Visible, row.Locked)
			}
			for _, bad := range []int{tc.want - 1, tc.want + 1} {
				if bad < 0 {
					continue
				}
				_, problems := ValidateDrawingRequest(drawingReq(tc.typ, pointsFor(bad)))
				if problems == nil || problems["points"] == nil {
					t.Errorf("%s accepted %d point(s)", tc.typ, bad)
				}
			}
		})
	}
}

func TestValidateDrawingRequestPointShapes(t *testing.T) {
	tests := []struct {
		name   string
		points string
	}{
		{"missing", ``},
		{"empty array", `[]`},
		{"json null", `null`},
		{"not an array", `{"t":1,"price":2}`},
		{"scalar element", `[1750000000, 72500000]`},
		{"missing t", `[{"price":72500000},{"t":1,"price":2}]`},
		{"missing price", `[{"t":1750000000},{"t":1,"price":2}]`},
		{"null t", `[{"t":null,"price":72500000},{"t":1,"price":2}]`},
		{"string t", `[{"t":"1750000000","price":72500000},{"t":1,"price":2}]`},
		{"bool price", `[{"t":1750000000,"price":true},{"t":1,"price":2}]`},
		// Overflows float64 and decodes to +Inf: a coordinate no axis can place.
		{"non-finite t", `[{"t":1e999,"price":72500000},{"t":1,"price":2}]`},
		{"non-finite price", `[{"t":1750000000,"price":-1e999},{"t":1,"price":2}]`},
		// Finite but absurd — milliseconds sent where seconds were meant.
		{"t out of unix-second range", `[{"t":1.75e18,"price":72500000},{"t":1,"price":2}]`},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			_, problems := ValidateDrawingRequest(drawingReq("trend_line", tc.points))
			if problems == nil || problems["points"] == nil {
				t.Fatalf("accepted %s points: %s (problems=%v)", tc.name, tc.points, problems)
			}
		})
	}
}

// Anchors are stored canonically, not echoed: t becomes whole unix seconds and
// any key the schema does not name is dropped before it reaches JSONB.
func TestValidateDrawingRequestNormalizesPoints(t *testing.T) {
	req := drawingReq("trend_line",
		`[{"t":1750000000.75,"price":72500000.5,"label":"ignored"},{"t":1750003600,"price":72600000}]`)
	row, problems := ValidateDrawingRequest(req)
	if problems != nil {
		t.Fatalf("rejected: %v", problems)
	}
	var got []drawingPoint
	if err := json.Unmarshal(row.Points, &got); err != nil {
		t.Fatalf("stored points are not decodable: %v", err)
	}
	if len(got) != 2 || got[0].T != 1750000000 || got[0].Price != 72500000.5 {
		t.Fatalf("bad normalization: %+v", got)
	}
	if strings.Contains(string(row.Points), "label") {
		t.Errorf("unknown key survived into storage: %s", row.Points)
	}
}

func TestValidateDrawingRequestSymbolAndInterval(t *testing.T) {
	for symbol := range KnownSymbols {
		req := drawingReq("horizontal_line", pointsFor(1))
		req.Symbol = symbol
		if _, problems := ValidateDrawingRequest(req); problems != nil {
			t.Errorf("known symbol %s rejected: %v", symbol, problems)
		}
	}
	for _, interval := range drawingIntervalOrder {
		req := drawingReq("horizontal_line", pointsFor(1))
		req.Interval = interval
		if _, problems := ValidateDrawingRequest(req); problems != nil {
			t.Errorf("canonical interval %s rejected: %v", interval, problems)
		}
	}
	tests := []struct {
		name     string
		symbol   string
		interval string
		field    string
	}{
		{"unknown symbol", "IR_GOLD_24K", "1h", "symbol"},
		{"empty symbol", "", "1h", "symbol"},
		{"unknown interval", "IR_GOLD_18K", "7m", "interval"},
		{"empty interval", "IR_GOLD_18K", "", "interval"},
		// Finer than the 5-minute tick cadence: no such chart exists.
		{"sub-5m interval", "IR_GOLD_18K", "1m", "interval"},
		// The legacy candles vocabulary is not the drawing vocabulary.
		{"legacy interval word", "IR_GOLD_18K", "daily", "interval"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			req := drawingReq("horizontal_line", pointsFor(1))
			req.Symbol, req.Interval = tc.symbol, tc.interval
			_, problems := ValidateDrawingRequest(req)
			if problems == nil || problems[tc.field] == nil {
				t.Fatalf("accepted %s=%q %s=%q (problems=%v)",
					"symbol", tc.symbol, "interval", tc.interval, problems)
			}
		})
	}
}

// Case and surrounding whitespace are the client's business, not the table's.
func TestValidateDrawingRequestNormalizesIdentifiers(t *testing.T) {
	req := drawingRequest{
		Symbol:      "  ir_gold_18k ",
		Interval:    " 1H ",
		DrawingType: " Trend_Line ",
		Points:      json.RawMessage(pointsFor(2)),
	}
	row, problems := ValidateDrawingRequest(req)
	if problems != nil {
		t.Fatalf("rejected: %v", problems)
	}
	if row.Symbol != "IR_GOLD_18K" || row.Interval != "1h" || row.DrawingType != "trend_line" {
		t.Fatalf("bad normalization: %+v", row)
	}
}

func TestValidateDrawingRequestDrawingType(t *testing.T) {
	for _, typ := range []string{"", "circle", "trend line", "TREND_LINE_2", "fib"} {
		req := drawingReq(typ, pointsFor(2))
		_, problems := ValidateDrawingRequest(req)
		if problems == nil || problems["drawing_type"] == nil {
			t.Errorf("accepted drawing_type %q (problems=%v)", typ, problems)
		}
	}
}

func TestValidateDrawingRequestStyle(t *testing.T) {
	ok := []struct {
		name  string
		style string
	}{
		{"absent", ``},
		{"empty object", `{}`},
		{"populated", `{"color":"#e11d48","width":2,"dash":[4,2]}`},
	}
	for _, tc := range ok {
		t.Run(tc.name, func(t *testing.T) {
			req := drawingReq("trend_line", pointsFor(2))
			req.Style = json.RawMessage(tc.style)
			row, problems := ValidateDrawingRequest(req)
			if problems != nil {
				t.Fatalf("valid style rejected: %v", problems)
			}
			if len(row.Style) == 0 {
				t.Fatal("style must never be stored empty; jsonb has no such value")
			}
		})
	}
	bad := []struct {
		name  string
		style string
	}{
		{"array", `[1,2]`},
		{"json null", `null`},
		{"string", `"solid"`},
		{"number", `3`},
		{"oversized", `{"blob":"` + strings.Repeat("x", maxDrawingStyleBytes) + `"}`},
	}
	for _, tc := range bad {
		t.Run(tc.name, func(t *testing.T) {
			req := drawingReq("trend_line", pointsFor(2))
			req.Style = json.RawMessage(tc.style)
			_, problems := ValidateDrawingRequest(req)
			if problems == nil || problems["style"] == nil {
				t.Fatalf("accepted %s style (problems=%v)", tc.name, problems)
			}
		})
	}
}

// locked/visible are tri-state on the wire: absent means "use the default",
// which for visible is true — a drawing that silently vanishes on save is
// worse than one that ignores an unset flag.
func TestValidateDrawingRequestFlags(t *testing.T) {
	yes, no := true, false
	tests := []struct {
		name                    string
		locked, visible         *bool
		wantLocked, wantVisible bool
	}{
		{"absent", nil, nil, false, true},
		{"explicit true", &yes, &yes, true, true},
		{"explicit false", &no, &no, false, false},
		{"hidden but locked", &yes, &no, true, false},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			req := drawingReq("trend_line", pointsFor(2))
			req.Locked, req.Visible = tc.locked, tc.visible
			row, problems := ValidateDrawingRequest(req)
			if problems != nil {
				t.Fatalf("rejected: %v", problems)
			}
			if row.Locked != tc.wantLocked || row.Visible != tc.wantVisible {
				t.Fatalf("locked=%v visible=%v, want %v/%v",
					row.Locked, row.Visible, tc.wantLocked, tc.wantVisible)
			}
		})
	}
}

// Several bad fields at once must all be reported: a client fixing one problem
// per round trip is a client that gives up.
func TestValidateDrawingRequestReportsEveryField(t *testing.T) {
	req := drawingRequest{
		Symbol:      "NOPE",
		Interval:    "13h",
		DrawingType: "spiral",
		Points:      json.RawMessage(`"not an array"`),
		Style:       json.RawMessage(`[]`),
	}
	_, problems := ValidateDrawingRequest(req)
	for _, field := range []string{"symbol", "interval", "drawing_type", "points", "style"} {
		if problems[field] == nil {
			t.Errorf("no problem reported for %s: %v", field, problems)
		}
	}
}

// THE cross-tenant regression guard. Every statement that reads or mutates a
// drawing must be scoped to the authenticated user id. A missing predicate on
// UPDATE or DELETE lets one account overwrite or erase another's drawings by
// guessing a BIGSERIAL id — the kind of defect that must fail in CI, loudly,
// rather than in production, silently.
func TestDrawingStatementsAreUserScoped(t *testing.T) {
	tests := []struct {
		name string
		sql  string
	}{
		{"list", sqlListDrawings},
		{"insert", sqlInsertDrawing},
		{"update", sqlUpdateDrawing},
		{"chart", sqlDrawingChart},
		{"delete", sqlDeleteDrawing},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			sql := collapseSpace(tc.sql)
			if !strings.Contains(sql, "user_id = $") {
				t.Fatalf("%s statement has no user_id predicate: %s", tc.name, sql)
			}
			// Parameterized only: an interpolated id would defeat the scoping
			// even while the literal text "user_id" was still present.
			if strings.Contains(sql, "%s") || strings.Contains(sql, "%v") {
				t.Fatalf("%s statement interpolates a value: %s", tc.name, sql)
			}
		})
	}

	// The two statements that address a single row by id must AND the owner
	// in, not merely mention user_id somewhere.
	for _, tc := range []struct {
		name string
		sql  string
	}{{"update", sqlUpdateDrawing}, {"chart", sqlDrawingChart}, {"delete", sqlDeleteDrawing}} {
		t.Run(tc.name+" where clause", func(t *testing.T) {
			sql := collapseSpace(tc.sql)
			idx := strings.Index(sql, "WHERE id = $")
			if idx < 0 {
				t.Fatalf("%s does not select a row by id: %s", tc.name, sql)
			}
			where := sql[idx:]
			if !strings.Contains(where, "AND user_id = $") {
				t.Fatalf("%s WHERE clause is not owner-scoped: %s", tc.name, where)
			}
		})
	}
}

// The type set the API accepts and the CHECK constraint in migration 0020 are
// two copies of one list; a type accepted here but absent there is a 500 at
// insert time, so they are compared directly against the migration file.
func TestDrawingTypesMatchMigrationCheck(t *testing.T) {
	sql := collapseSpace(readMigration(t, "0020_chart_drawings.up.sql"))
	allowed := checkConstraintValues(t, sql)
	for typ := range drawingPointCounts {
		if !allowed[typ] {
			t.Errorf("drawing type %q is accepted by the API but not in the 0020 CHECK", typ)
		}
	}
	// And the reverse: a type the table allows but the API never accepts is
	// dead schema that somebody will later believe.
	for typ := range allowed {
		if _, ok := drawingPointCounts[typ]; !ok {
			t.Errorf("0020 CHECK allows %q but the API never accepts it", typ)
		}
	}
}

// checkConstraintValues returns the quoted literals of the drawing_type CHECK.
func checkConstraintValues(t *testing.T, sql string) map[string]bool {
	t.Helper()
	start := strings.Index(sql, "CHECK (drawing_type IN (")
	if start < 0 {
		t.Fatal("0020 has no drawing_type CHECK constraint")
	}
	end := strings.Index(sql[start:], "))")
	if end < 0 {
		t.Fatal("0020 drawing_type CHECK is never closed")
	}
	out := map[string]bool{}
	// Odd fields of a split on the quote character are the literals.
	for i, field := range strings.Split(sql[start:start+end], "'") {
		if i%2 == 1 {
			out[field] = true
		}
	}
	if len(out) == 0 {
		t.Fatal("0020 drawing_type CHECK lists no values")
	}
	return out
}

// A drawing must not outlive the account that made it, and the one read
// pattern the API has must be indexed — both properties live in the schema, so
// the migration is asserted rather than assumed. Rolling 0020 back must leave
// nothing behind, or the next `up` fails on a fresh deploy.
func TestDrawingMigrationShape(t *testing.T) {
	up := collapseSpace(readMigration(t, "0020_chart_drawings.up.sql"))
	for _, want := range []string{
		"CREATE TABLE IF NOT EXISTS chart_drawings",
		"user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE",
		"points JSONB NOT NULL",
		"style JSONB NOT NULL DEFAULT '{}'::jsonb",
		"CREATE INDEX IF NOT EXISTS idx_chart_drawings_user_chart",
		`ON chart_drawings (user_id, symbol, "interval", id)`,
	} {
		if !strings.Contains(up, want) {
			t.Errorf("0020 up migration is missing %q", want)
		}
	}
	down := collapseSpace(readMigration(t, "0020_chart_drawings.down.sql"))
	for _, want := range []string{
		"DROP INDEX IF EXISTS idx_chart_drawings_user_chart",
		"DROP TABLE IF EXISTS chart_drawings",
	} {
		if !strings.Contains(down, want) {
			t.Errorf("0020 down migration is missing %q", want)
		}
	}
}

// updateSetClause returns the SET list of an UPDATE, without the predicate
// that follows it.
func updateSetClause(t *testing.T, sql string) string {
	t.Helper()
	collapsed := collapseSpace(sql)
	start := strings.Index(collapsed, "SET ")
	end := strings.Index(collapsed, " WHERE ")
	if start < 0 || end < 0 || end < start {
		t.Fatalf("statement has no SET ... WHERE: %s", collapsed)
	}
	return collapsed[start+len("SET ") : end]
}

// updateWhereClause returns the predicate of an UPDATE, without its RETURNING.
func updateWhereClause(t *testing.T, sql string) string {
	t.Helper()
	collapsed := collapseSpace(sql)
	start := strings.Index(collapsed, "WHERE ")
	if start < 0 {
		t.Fatalf("statement has no WHERE: %s", collapsed)
	}
	where := collapsed[start:]
	if end := strings.Index(where, " RETURNING "); end >= 0 {
		where = where[:end]
	}
	return where
}

// THE per-chart-cap regression guard. The cap is counted on INSERT and nowhere
// else, so while UPDATE could write symbol and "interval" it was bypassable
// with no exploit beyond the documented API: fill a chart to 200, PUT each
// drawing with a different interval, and the source chart is empty again and
// ready to refill. 200·N rows per account, style blobs included.
//
// The fix is structural rather than procedural — the two columns are not in
// SET at all — so this asserts the statement, which is the thing that cannot
// be got around by a later handler edit.
func TestUpdateStatementCannotMoveADrawingBetweenCharts(t *testing.T) {
	set := updateSetClause(t, sqlUpdateDrawing)
	for _, col := range []string{"symbol", "interval"} {
		if strings.Contains(set, col) {
			t.Errorf("UPDATE assigns %s (%q): a drawing must not change chart, "+
				"and an assignable chart makes the per-chart cap bypassable", col, set)
		}
	}
	// And the chart is pinned in the predicate, so a body naming another chart
	// matches zero rows instead of silently editing the drawing in place.
	where := updateWhereClause(t, sqlUpdateDrawing)
	for _, want := range []string{"symbol = $", `"interval" = $`} {
		if !strings.Contains(where, want) {
			t.Errorf("UPDATE predicate is missing %q: %s", want, where)
		}
	}
}

// The list response must be bounded by the query, not by an assumption about
// how many rows the table holds. The cap is an INSERT-time check: it can be
// overshot by concurrent inserts, it did not bind at all while UPDATE could
// move drawings, and it never applied to rows written before it existed.
func TestListStatementBoundsTheResponse(t *testing.T) {
	sql := collapseSpace(sqlListDrawings)
	if !strings.Contains(sql, "LIMIT $") {
		t.Fatalf("list statement has no parameterized LIMIT: %s", sql)
	}
	// Ordering plus a limit is a prefix, which is only meaningful if the order
	// is total and stable — id is both.
	if !strings.Contains(sql, "ORDER BY id ASC LIMIT $") {
		t.Errorf("LIMIT must follow a stable ORDER BY or the prefix is arbitrary: %s", sql)
	}
	if maxDrawingsListed < MaxDrawingsPerChart {
		t.Errorf("maxDrawingsListed (%d) < MaxDrawingsPerChart (%d): a chart at the cap "+
			"would come back truncated", maxDrawingsListed, MaxDrawingsPerChart)
	}
}

func TestDrawingChartMismatch(t *testing.T) {
	base := func() drawingRow {
		row, problems := ValidateDrawingRequest(drawingReq("trend_line", pointsFor(2)))
		if problems != nil {
			t.Fatalf("fixture is invalid: %v", problems)
		}
		return row
	}
	// drawingReq builds IR_GOLD_18K / 1h.
	t.Run("same chart", func(t *testing.T) {
		if p := drawingChartMismatch("IR_GOLD_18K", "1h", base()); p != nil {
			t.Fatalf("a body restating its own chart was refused: %v", p)
		}
	})
	t.Run("case and whitespace only", func(t *testing.T) {
		req := drawingReq("trend_line", pointsFor(2))
		req.Symbol, req.Interval = "  ir_gold_18k ", " 1H "
		row, problems := ValidateDrawingRequest(req)
		if problems != nil {
			t.Fatalf("rejected: %v", problems)
		}
		if p := drawingChartMismatch("IR_GOLD_18K", "1h", row); p != nil {
			t.Fatalf("normalization difference read as a chart change: %v", p)
		}
	})
	t.Run("interval changed", func(t *testing.T) {
		// The exact bypass: same drawing, different timeframe.
		p := drawingChartMismatch("IR_GOLD_18K", "5m", base())
		if p == nil || p["interval"] == nil {
			t.Fatalf("interval move accepted: %v", p)
		}
		if p["symbol"] != nil {
			t.Errorf("unchanged symbol reported as a problem: %v", p)
		}
	})
	t.Run("symbol changed", func(t *testing.T) {
		p := drawingChartMismatch("XAUUSD", "1h", base())
		if p == nil || p["symbol"] == nil {
			t.Fatalf("symbol move accepted: %v", p)
		}
		if p["interval"] != nil {
			t.Errorf("unchanged interval reported as a problem: %v", p)
		}
	})
	t.Run("both changed", func(t *testing.T) {
		p := drawingChartMismatch("XAUUSD", "1d", base())
		if p == nil || p["symbol"] == nil || p["interval"] == nil {
			t.Fatalf("both fields must be reported at once: %v", p)
		}
	})
}

// storedDrawing is one row as the list endpoint serializes it.
func storedDrawing() drawingDTO {
	at := time.Date(2026, 8, 12, 9, 30, 0, 0, time.UTC)
	return drawingDTO{
		ID:          4242,
		Symbol:      "IR_GOLD_18K",
		Interval:    "5m",
		DrawingType: "trend_line",
		Points: json.RawMessage(
			`[{"t":1750000000,"price":72500000},{"t":1750003600,"price":72600000}]`),
		Style:     json.RawMessage(`{"color":"#e11d48","width":2}`),
		Locked:    false,
		Visible:   true,
		CreatedAt: at,
		UpdatedAt: at,
	}
}

// putDrawingBody decodes a body exactly as UpdateDrawing does — same
// DisallowUnknownFields decoder — and reports what the client would have seen.
func putDrawingBody(t *testing.T, body []byte) (drawingRequest, *httptest.ResponseRecorder, bool) {
	t.Helper()
	r := httptest.NewRequest(http.MethodPut, "/api/v1/chart/drawings/4242", bytes.NewReader(body))
	rec := httptest.NewRecorder()
	var req drawingRequest
	ok := httpserver.DecodeJSON(rec, r, &req)
	return req, rec, ok
}

// THE round-trip regression guard. A client edits a drawing it fetched and
// saves it: the body it has is the object the server just handed it, id and
// timestamps included. The decoder refuses unknown fields, so before the
// request struct had somewhere to put them this was a 400 on the server's own
// field names — every edit, immediately, for the whole drawing engine.
//
// The list response is built here by the same function the handler calls, so
// this is the real shape, not a hand-written approximation of it.
func TestListedDrawingCanBePutBackUnchanged(t *testing.T) {
	stored := storedDrawing()
	envelope, err := json.Marshal(drawingListResponse([]drawingDTO{stored}, false))
	if err != nil {
		t.Fatalf("marshal list response: %v", err)
	}
	var listed struct {
		Items []json.RawMessage `json:"items"`
	}
	if err := json.Unmarshal(envelope, &listed); err != nil {
		t.Fatalf("unmarshal list response: %v", err)
	}
	if len(listed.Items) != 1 {
		t.Fatalf("list response carried %d items", len(listed.Items))
	}

	// The bytes of the listed drawing, verbatim, as the PUT body.
	req, rec, ok := putDrawingBody(t, listed.Items[0])
	if !ok {
		t.Fatalf("a drawing straight from the list response was rejected: %d %s",
			rec.Code, strings.TrimSpace(rec.Body.String()))
	}
	row, problems := ValidateDrawingRequest(req)
	if problems != nil {
		t.Fatalf("round-tripped drawing failed validation: %v", problems)
	}
	// Same chart, so the update predicate matches and the handler answers 200
	// rather than the 400 a chart change now earns.
	if p := drawingChartMismatch(stored.Symbol, stored.Interval, row); p != nil {
		t.Fatalf("unchanged round trip read as a chart move: %v", p)
	}
	if row.Symbol != stored.Symbol || row.Interval != stored.Interval ||
		row.DrawingType != stored.DrawingType || row.Locked != stored.Locked ||
		row.Visible != stored.Visible {
		t.Errorf("round trip changed the drawing: %+v", row)
	}
	var want, got []drawingPoint
	if err := json.Unmarshal(stored.Points, &want); err != nil {
		t.Fatalf("fixture points: %v", err)
	}
	if err := json.Unmarshal(row.Points, &got); err != nil {
		t.Fatalf("round-tripped points: %v", err)
	}
	if !reflect.DeepEqual(want, got) {
		t.Errorf("anchors changed on round trip: %v -> %v", want, got)
	}
	if string(row.Style) != string(stored.Style) {
		t.Errorf("style changed on round trip: %s -> %s", stored.Style, row.Style)
	}
}

// Accepted and ignored, not accepted and used: whatever a body says about id
// or the timestamps must reach nothing. drawingRow is the only value a
// statement is fed from, so its field set is the proof.
func TestDrawingRequestIgnoresServerOwnedFields(t *testing.T) {
	body := []byte(`{"id":999999,"created_at":"1999-01-01T00:00:00Z",` +
		`"updated_at":"1999-01-01T00:00:00Z","symbol":"IR_GOLD_18K","interval":"5m",` +
		`"drawing_type":"trend_line","points":` + pointsFor(2) + `,"style":{}}`)
	req, rec, ok := putDrawingBody(t, body)
	if !ok {
		t.Fatalf("server-owned fields were not accepted: %d %s",
			rec.Code, strings.TrimSpace(rec.Body.String()))
	}
	row, problems := ValidateDrawingRequest(req)
	if problems != nil {
		t.Fatalf("rejected: %v", problems)
	}
	schema := map[string]bool{
		"Symbol": true, "Interval": true, "DrawingType": true,
		"Points": true, "Style": true, "Locked": true, "Visible": true,
	}
	for _, f := range reflect.VisibleFields(reflect.TypeOf(row)) {
		if !schema[f.Name] {
			t.Errorf("drawingRow gained field %s: the storage-ready row must carry "+
				"nothing a request body can set beyond the drawing's own columns", f.Name)
		}
	}
	// And no statement takes an id or a creation time from anywhere but the
	// database and the URL path.
	insert := collapseSpace(sqlInsertDrawing)
	start := strings.Index(insert, "INSERT INTO chart_drawings (")
	end := strings.Index(insert, ") SELECT ")
	if start < 0 || end < 0 {
		t.Fatalf("insert statement is not the shape this test reads: %s", insert)
	}
	// Compared name by name, not as substrings: user_id contains "id".
	written := map[string]bool{}
	for _, col := range strings.Split(insert[start+len("INSERT INTO chart_drawings ("):end], ",") {
		written[strings.Trim(strings.TrimSpace(col), `"`)] = true
	}
	for _, col := range []string{"id", "created_at", "updated_at"} {
		if written[col] {
			t.Errorf("INSERT writes server-owned column %s: %v", col, written)
		}
	}
	set := updateSetClause(t, sqlUpdateDrawing)
	if strings.Contains(set, "created_at") {
		t.Errorf("UPDATE rewrites created_at: %s", set)
	}
	if !strings.Contains(set, "updated_at = now()") {
		t.Errorf("UPDATE must stamp updated_at from the database clock: %s", set)
	}
}

// Widened by exactly three field names. Anything else unknown is still a 400 —
// a body full of typos must not be accepted and silently half-applied.
func TestDrawingRequestStillRefusesUnknownFields(t *testing.T) {
	body := []byte(`{"symbol":"IR_GOLD_18K","interval":"5m","drawing_type":"trend_line",` +
		`"points":` + pointsFor(2) + `,"colour":"#fff"}`)
	_, rec, ok := putDrawingBody(t, body)
	if ok {
		t.Fatal(`unknown field "colour" was accepted`)
	}
	if rec.Code != http.StatusBadRequest {
		t.Errorf("unknown field answered %d, want 400", rec.Code)
	}
}

func readMigration(t *testing.T, name string) string {
	t.Helper()
	path := filepath.Join("..", "..", "..", "database", "migrations", name)
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	return string(b)
}

func collapseSpace(s string) string {
	return strings.Join(strings.Fields(s), " ")
}
