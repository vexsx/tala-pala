package prices

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func trendTime(hour int) time.Time {
	return time.Date(2026, 8, 12, hour, 0, 0, 0, time.UTC)
}

func timePtr(t time.Time) *time.Time { return &t }

func strPtr(s string) *string { return &s }

func TestParseTrendEventLimit(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		want    int
		wantErr bool
	}{
		{"absent uses default", "", defaultTrendEventLimit, false},
		{"minimum", "1", 1, false},
		{"explicit default", "20", 20, false},
		{"maximum", "100", 100, false},
		{"zero rejected", "0", 0, true},
		{"negative rejected", "-1", 0, true},
		{"non numeric rejected", "abc", 0, true},
		{"above maximum rejected", "101", 0, true},
		{"float rejected", "20.5", 0, true},
		{"padded rejected", " 20", 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseTrendEventLimit(tc.raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("limit %q accepted, got %d", tc.raw, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("limit %q rejected: %v", tc.raw, err)
			}
			if got != tc.want {
				t.Fatalf("limit %q: got %d, want %d", tc.raw, got, tc.want)
			}
		})
	}
}

func TestParseTrendSymbol(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		want    string
		wantErr bool
	}{
		{"absent uses the primary symbol", "", "IR_GOLD_18K", false},
		{"gold", "IR_GOLD_18K", "IR_GOLD_18K", false},
		{"spot gold", "XAUUSD", "XAUUSD", false},
		// Known elsewhere in the system, but never evaluated for alignment:
		// answering these would mean serving a trend nobody computed.
		{"silver rejected", "XAGUSD", "", true},
		{"usd rejected", "USD_IRT", "", true},
		{"fund rejected", "IR_GOLD_FUND_TALA", "", true},
		{"lowercase rejected", "ir_gold_18k", "", true},
		{"padded rejected", " XAUUSD", "", true},
		{"nonsense rejected", "'; DROP TABLE prices;--", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseTrendSymbol(tc.raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("symbol %q accepted as %q", tc.raw, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("symbol %q rejected: %v", tc.raw, err)
			}
			if got != tc.want {
				t.Fatalf("symbol %q: got %q, want %q", tc.raw, got, tc.want)
			}
		})
	}
}

// The event log differs on exactly one point: an absent symbol means every
// evaluated symbol, because each item names its own.
func TestParseTrendEventSymbol(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		want    string
		wantErr bool
	}{
		{"absent means every symbol", "", "", false},
		{"gold", "IR_GOLD_18K", "IR_GOLD_18K", false},
		{"spot gold", "XAUUSD", "XAUUSD", false},
		{"silver rejected", "XAGUSD", "", true},
		{"lowercase rejected", "xauusd", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseTrendEventSymbol(tc.raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("symbol %q accepted as %q", tc.raw, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("symbol %q rejected: %v", tc.raw, err)
			}
			if got != tc.want {
				t.Fatalf("symbol %q: got %q, want %q", tc.raw, got, tc.want)
			}
		})
	}
}

// Both handlers run on a nil pool here: that IS the assertion that validation
// happens before anything touches the database (a query would panic).
func TestTrendAlignment_UnsupportedSymbolReturns400(t *testing.T) {
	h := &Handler{Log: quietLogger()}
	for _, raw := range []string{"XAGUSD", "USD_IRT", "ir_gold_18k", "nope"} {
		rec := httptest.NewRecorder()
		h.TrendAlignment(rec, httptest.NewRequest("GET",
			"/api/v1/market/trend-alignment?symbol="+raw, nil))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("symbol=%s: got status %d, want 400", raw, rec.Code)
		}
		var body httpserver.ErrorBody
		if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.Error.Code != "bad_request" {
			t.Fatalf("symbol=%s: wrong error code %q", raw, body.Error.Code)
		}
	}
}

func TestTrendAlignmentEvents_UnsupportedSymbolReturns400(t *testing.T) {
	h := &Handler{Log: quietLogger()}
	for _, raw := range []string{"XAGUSD", "BRENT_OIL", "xauusd"} {
		rec := httptest.NewRecorder()
		h.TrendAlignmentEvents(rec, httptest.NewRequest("GET",
			"/api/v1/market/trend-alignment/events?symbol="+raw, nil))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("symbol=%s: got status %d, want 400", raw, rec.Code)
		}
	}
}

func TestTrendAlignmentEvents_InvalidLimitReturns400(t *testing.T) {
	h := &Handler{Log: quietLogger()}
	for _, raw := range []string{"0", "-1", "abc", "101", "1000"} {
		rec := httptest.NewRecorder()
		h.TrendAlignmentEvents(rec, httptest.NewRequest("GET",
			"/api/v1/market/trend-alignment/events?symbol=XAUUSD&limit="+raw, nil))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("limit=%s: got status %d, want 400", raw, rec.Code)
		}
		var body httpserver.ErrorBody
		if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.Error.Code != "bad_request" {
			t.Fatalf("limit=%s: wrong error code %q", raw, body.Error.Code)
		}
	}
}

// storedTimeframes is what the Python evaluator persists into the JSONB column,
// including one key that is NOT part of the wire contract.
func storedTimeframes() []byte {
	return []byte(`{
		"1d": {"timeframe":"1d","trend":"bullish","price":91234567.0,
		       "ma26":90000000.0,"ma48":88000000.0,"ma220":80000000.0,
		       "candle_open_time":"2026-08-11T00:00:00+00:00",
		       "candle_close_time":"2026-08-12T00:00:00+00:00",
		       "confirmed":true,"data_fresh":true,"ma_type":"ema",
		       "history_points":400,"reason":"","debug_window":[1,2,3]},
		"4h": {"timeframe":"4h","trend":"bullish","price":91234567.0,
		       "ma26":91000000.0,"ma48":90500000.0,"ma220":85000000.0,
		       "candle_open_time":"2026-08-12T04:00:00+00:00",
		       "candle_close_time":"2026-08-12T08:00:00+00:00",
		       "confirmed":true,"data_fresh":true,"ma_type":"ema",
		       "history_points":300,"reason":""},
		"1h": {"timeframe":"1h","trend":"bullish","price":91234567.0,
		       "ma26":91100000.0,"ma48":91000000.0,"ma220":89000000.0,
		       "candle_open_time":"2026-08-12T09:00:00+00:00",
		       "candle_close_time":"2026-08-12T10:00:00+00:00",
		       "confirmed":true,"data_fresh":true,"ma_type":"ema",
		       "history_points":900,"reason":""}
	}`)
}

func alignedStateRow() *trendStateRow {
	return &trendStateRow{
		Symbol:            "IR_GOLD_18K",
		Alignment:         alignmentFullBullish,
		PreviousAlignment: strPtr(alignmentNotAligned),
		Timeframes:        storedTimeframes(),
		MaType:            "ema",
		FastPeriod:        26,
		MidPeriod:         48,
		SlowPeriod:        220,
		DataFresh:         true,
		LastBullishAlert:  timePtr(trendTime(10)),
		LastBearishAlert:  timePtr(trendTime(3)),
		CalculatedAt:      trendTime(11),
	}
}

// The envelope is a fixed contract shared with the Python writer and the UI:
// pin the exact key set, in both directions, so neither a missing key nor an
// extra one can ship unnoticed.
func TestBuildTrendAlignmentResponse_EnvelopeShape(t *testing.T) {
	resp := buildTrendAlignmentResponse("IR_GOLD_18K", alignedStateRow(), timePtr(trendTime(9)))
	b, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(b, &envelope); err != nil {
		t.Fatal(err)
	}
	wantKeys := []string{"symbol", "alignment", "previous_alignment", "timeframes",
		"ma_type", "periods", "data_fresh", "calculated_at", "last_transition_at",
		"last_alert_at", "note"}
	for _, key := range wantKeys {
		if _, ok := envelope[key]; !ok {
			t.Fatalf("response is missing %q: %s", key, b)
		}
	}
	if len(envelope) != len(wantKeys) {
		t.Fatalf("unexpected envelope keys: %s", b)
	}
	if string(envelope["alignment"]) != `"full_bullish"` {
		t.Fatalf("alignment = %s", envelope["alignment"])
	}
	if string(envelope["periods"]) != `{"fast":26,"mid":48,"slow":220}` {
		t.Fatalf("periods = %s", envelope["periods"])
	}
	if string(envelope["note"]) != "null" {
		t.Fatalf("an evaluated state carries no note, got %s", envelope["note"])
	}
	// Every timestamp is serialized UTC with a Z suffix, whatever the row's
	// location was.
	if string(envelope["calculated_at"]) != `"2026-08-12T11:00:00Z"` {
		t.Fatalf("calculated_at not RFC3339 UTC: %s", envelope["calculated_at"])
	}
	if string(envelope["last_transition_at"]) != `"2026-08-12T09:00:00Z"` {
		t.Fatalf("last_transition_at = %s", envelope["last_transition_at"])
	}
}

// A timestamp stored in a non-UTC location must still leave as UTC.
func TestBuildTrendAlignmentResponse_NormalizesLocationToUTC(t *testing.T) {
	tehran := time.FixedZone("+0330", 3*3600+1800)
	row := alignedStateRow()
	row.CalculatedAt = trendTime(11).In(tehran)
	row.LastBullishAlert = timePtr(trendTime(10).In(tehran))
	transition := trendTime(9).In(tehran)

	b, err := json.Marshal(buildTrendAlignmentResponse("IR_GOLD_18K", row, &transition))
	if err != nil {
		t.Fatal(err)
	}
	got := string(b)
	for _, want := range []string{
		`"calculated_at":"2026-08-12T11:00:00Z"`,
		`"last_alert_at":"2026-08-12T10:00:00Z"`,
		`"last_transition_at":"2026-08-12T09:00:00Z"`,
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("missing %s in %s", want, got)
		}
	}
}

func TestBuildTrendAlignmentResponse_TimeframeProjection(t *testing.T) {
	resp := buildTrendAlignmentResponse("IR_GOLD_18K", alignedStateRow(), nil)
	b, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	var envelope struct {
		Timeframes map[string]map[string]json.RawMessage `json:"timeframes"`
	}
	if err := json.Unmarshal(b, &envelope); err != nil {
		t.Fatal(err)
	}
	if len(envelope.Timeframes) != 3 {
		t.Fatalf("want exactly the three legs, got %v", envelope.Timeframes)
	}
	wantKeys := []string{"timeframe", "trend", "price", "ma26", "ma48", "ma220",
		"candle_open_time", "candle_close_time", "confirmed", "data_fresh",
		"ma_type", "history_points", "reason"}
	for _, tf := range []string{"1d", "4h", "1h"} {
		leg, ok := envelope.Timeframes[tf]
		if !ok {
			t.Fatalf("timeframe %q missing: %s", tf, b)
		}
		for _, key := range wantKeys {
			if _, ok := leg[key]; !ok {
				t.Fatalf("timeframe %q missing key %q: %s", tf, key, b)
			}
		}
		if len(leg) != len(wantKeys) {
			t.Fatalf("timeframe %q has unexpected keys: %v", tf, leg)
		}
	}
	// A key the evaluator stashes in the JSONB is not part of the contract and
	// must not leak.
	if strings.Contains(string(b), "debug_window") {
		t.Fatalf("stored-but-uncontracted key leaked: %s", b)
	}
	// Measurements pass through verbatim: Go reads the quant, it never rounds,
	// rescales or recomputes it.
	if string(envelope.Timeframes["1h"]["ma26"]) != "91100000.0" {
		t.Fatalf("ma26 was not passed through verbatim: %s", envelope.Timeframes["1h"]["ma26"])
	}
	if string(envelope.Timeframes["1d"]["candle_close_time"]) != `"2026-08-12T00:00:00+00:00"` {
		t.Fatalf("candle_close_time was rewritten: %s", envelope.Timeframes["1d"]["candle_close_time"])
	}
}

// A leg the evaluator did not write is reported as unavailable with null
// measurements — never as a plausible-looking number.
func TestBuildTrendAlignmentResponse_MissingLegIsUnavailable(t *testing.T) {
	row := alignedStateRow()
	row.Timeframes = []byte(`{"1h": {"timeframe":"1h","trend":"bullish","price":91234567.0}}`)

	b, err := json.Marshal(buildTrendAlignmentResponse("IR_GOLD_18K", row, nil))
	if err != nil {
		t.Fatal(err)
	}
	var envelope struct {
		Timeframes map[string]map[string]json.RawMessage `json:"timeframes"`
	}
	if err := json.Unmarshal(b, &envelope); err != nil {
		t.Fatal(err)
	}
	daily := envelope.Timeframes["1d"]
	if string(daily["trend"]) != `"unavailable"` {
		t.Fatalf("missing leg trend = %s, want \"unavailable\"", daily["trend"])
	}
	for _, key := range []string{"price", "ma26", "ma48", "ma220", "candle_close_time"} {
		if string(daily[key]) != "null" {
			t.Fatalf("missing leg %q = %s, want null", key, daily[key])
		}
	}
	if string(daily["reason"]) != `"not_evaluated"` {
		t.Fatalf("missing leg reason = %s", daily["reason"])
	}
	// A leg that IS present but partial keeps what was measured and nulls the
	// rest, rather than being discarded.
	hourly := envelope.Timeframes["1h"]
	if string(hourly["price"]) != "91234567.0" || string(hourly["ma220"]) != "null" {
		t.Fatalf("partial leg mishandled: %v", hourly)
	}
}

func TestBuildTrendAlignmentResponse_UnreadableTimeframesJSON(t *testing.T) {
	row := alignedStateRow()
	row.Timeframes = []byte(`not json`)
	b, err := json.Marshal(buildTrendAlignmentResponse("IR_GOLD_18K", row, nil))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), `"unreadable_state"`) {
		t.Fatalf("corrupt timeframes must be reported, got %s", b)
	}
	if strings.Contains(string(b), "not json") {
		t.Fatalf("raw stored bytes leaked: %s", b)
	}
}

// No state yet is a 200 that says so: not a 404, and not invented numbers.
func TestBuildTrendAlignmentResponse_NeverEvaluated(t *testing.T) {
	b, err := json.Marshal(buildTrendAlignmentResponse("XAUUSD", nil, nil))
	if err != nil {
		t.Fatal(err)
	}
	got := string(b)
	for _, want := range []string{
		`"symbol":"XAUUSD"`,
		`"alignment":"not_aligned"`,
		`"previous_alignment":null`,
		`"note":"never_evaluated"`,
		`"calculated_at":null`,
		`"last_transition_at":null`,
		`"last_alert_at":null`,
		`"data_fresh":false`,
		`"periods":{"fast":26,"mid":48,"slow":220}`,
		`"ma_type":"ema"`,
	} {
		if !strings.Contains(got, want) {
			t.Fatalf("missing %s in %s", want, got)
		}
	}
	var envelope struct {
		Timeframes map[string]map[string]json.RawMessage `json:"timeframes"`
	}
	if err := json.Unmarshal(b, &envelope); err != nil {
		t.Fatal(err)
	}
	if len(envelope.Timeframes) != 3 {
		t.Fatalf("all three legs must be present and unavailable: %s", got)
	}
	for tf, leg := range envelope.Timeframes {
		if string(leg["trend"]) != `"unavailable"` {
			t.Fatalf("%s trend = %s, want \"unavailable\"", tf, leg["trend"])
		}
		if string(leg["reason"]) != `"never_evaluated"` {
			t.Fatalf("%s reason = %s", tf, leg["reason"])
		}
		for _, key := range []string{"price", "ma26", "ma48", "ma220"} {
			if string(leg[key]) != "null" {
				t.Fatalf("%s %q = %s, want null (nothing was measured)", tf, key, leg[key])
			}
		}
	}
}

func TestResolveLastAlertAt(t *testing.T) {
	bull, bear := trendTime(10), trendTime(3)
	cases := []struct {
		name      string
		alignment string
		bullish   *time.Time
		bearish   *time.Time
		want      *time.Time
	}{
		{"bullish reports the bullish alert", alignmentFullBullish, &bull, &bear, &bull},
		{"bearish reports the bearish alert", alignmentFullBearish, &bull, &bear, &bear},
		{"bullish without an alert is null", alignmentFullBullish, nil, &bear, nil},
		{"not aligned reports the most recent alert", alignmentNotAligned, &bear, &bull, &bull},
		{"not aligned with one side only", alignmentNotAligned, &bull, nil, &bull},
		{"not aligned with no alerts", alignmentNotAligned, nil, nil, nil},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := resolveLastAlertAt(tc.alignment, tc.bullish, tc.bearish)
			switch {
			case tc.want == nil && got != nil:
				t.Fatalf("got %v, want nil", got)
			case tc.want != nil && got == nil:
				t.Fatalf("got nil, want %v", tc.want)
			case tc.want != nil && !got.Equal(*tc.want):
				t.Fatalf("got %v, want %v", got, tc.want)
			}
		})
	}
}

// Rows as they would come back from a scan, in a deliberately wrong order.
func unorderedTrendEventRows() []trendEventRow {
	tf := []byte(`{"1h":{"timeframe":"1h","trend":"bullish"}}`)
	return []trendEventRow{
		{ID: 1, Symbol: "IR_GOLD_18K", Alignment: alignmentFullBullish,
			PreviousAlignment: strPtr(alignmentNotAligned), OccurredAt: trendTime(3),
			Candle1h: trendTime(3), Candle4h: trendTime(0), Candle1d: trendTime(0),
			Timeframes: tf, MaType: "ema"},
		{ID: 4, Symbol: "XAUUSD", Alignment: alignmentFullBearish,
			PreviousAlignment: strPtr(alignmentNotAligned), OccurredAt: trendTime(9),
			Candle1h: trendTime(9), Candle4h: trendTime(8), Candle1d: trendTime(0),
			Timeframes: tf, MaType: "ema"},
		// Same second as id 4: the tiebreak must be deterministic, because two
		// symbols can enter an alignment off the same closed candle.
		{ID: 5, Symbol: "IR_GOLD_18K", Alignment: alignmentFullBearish,
			PreviousAlignment: strPtr(alignmentFullBullish), OccurredAt: trendTime(9),
			Candle1h: trendTime(9), Candle4h: trendTime(8), Candle1d: trendTime(0),
			Timeframes: tf, MaType: "ema"},
		{ID: 2, Symbol: "IR_GOLD_18K", Alignment: alignmentFullBullish,
			PreviousAlignment: strPtr(alignmentNotAligned), OccurredAt: trendTime(6),
			Candle1h: trendTime(6), Candle4h: trendTime(4), Candle1d: trendTime(0),
			Timeframes: tf, MaType: "ema"},
	}
}

func TestSortTrendEventRows_NewestFirstThenID(t *testing.T) {
	rows := unorderedTrendEventRows()
	sortTrendEventRows(rows)

	var got []int64
	for _, r := range rows {
		got = append(got, r.ID)
	}
	want := []int64{5, 4, 2, 1}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order = %v, want %v", got, want)
		}
	}
}

func TestBuildTrendEventsResponse_OrderingAndPagination(t *testing.T) {
	rows := unorderedTrendEventRows()
	resp := buildTrendEventsResponse(rows, 2)

	if resp.Count != 2 || len(resp.Items) != 2 {
		t.Fatalf("count = %d, items = %d, want 2 and 2", resp.Count, len(resp.Items))
	}
	// The page holds the NEWEST rows, not the first two scanned.
	if resp.Items[0].ID != 5 || resp.Items[1].ID != 4 {
		t.Fatalf("page = %d,%d, want 5,4", resp.Items[0].ID, resp.Items[1].ID)
	}
	// A limit larger than the result set returns everything, still ordered.
	full := buildTrendEventsResponse(unorderedTrendEventRows(), 100)
	if full.Count != 4 {
		t.Fatalf("count = %d, want 4", full.Count)
	}
	var got []int64
	for _, it := range full.Items {
		got = append(got, it.ID)
	}
	for i, want := range []int64{5, 4, 2, 1} {
		if got[i] != want {
			t.Fatalf("order = %v, want [5 4 2 1]", got)
		}
	}
}

func TestBuildTrendEventsResponse_ItemShape(t *testing.T) {
	b, err := json.Marshal(buildTrendEventsResponse(unorderedTrendEventRows(), 1))
	if err != nil {
		t.Fatal(err)
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(b, &envelope); err != nil {
		t.Fatal(err)
	}
	if len(envelope) != 2 || envelope["items"] == nil || envelope["count"] == nil {
		t.Fatalf("envelope must be exactly {items,count}: %s", b)
	}
	var items []map[string]json.RawMessage
	if err := json.Unmarshal(envelope["items"], &items); err != nil {
		t.Fatal(err)
	}
	wantKeys := []string{"id", "symbol", "alignment", "previous_alignment",
		"occurred_at", "candles", "timeframes", "ma_type"}
	for _, key := range wantKeys {
		if _, ok := items[0][key]; !ok {
			t.Fatalf("item is missing %q: %s", key, b)
		}
	}
	if len(items[0]) != len(wantKeys) {
		t.Fatalf("item has unexpected keys: %s", b)
	}
	// The writer's crash-recovery link is not part of the client contract.
	if strings.Contains(string(b), "alert_event_id") {
		t.Fatalf("internal column leaked: %s", b)
	}
	if string(items[0]["occurred_at"]) != `"2026-08-12T09:00:00Z"` {
		t.Fatalf("occurred_at not RFC3339 UTC: %s", items[0]["occurred_at"])
	}
	// The closed-candle triple is the idempotency key made visible: it is what
	// answers "why did this fire now?".
	var candles map[string]string
	if err := json.Unmarshal(items[0]["candles"], &candles); err != nil {
		t.Fatal(err)
	}
	for _, tf := range []string{"1d", "4h", "1h"} {
		if _, ok := candles[tf]; !ok {
			t.Fatalf("candles missing %q: %s", tf, items[0]["candles"])
		}
	}
	if candles["1h"] != "2026-08-12T09:00:00Z" {
		t.Fatalf("candles[1h] = %s", candles["1h"])
	}
}

func TestBuildTrendEventsResponse_EmptyEncodesAsList(t *testing.T) {
	b, err := json.Marshal(buildTrendEventsResponse(nil, defaultTrendEventLimit))
	if err != nil {
		t.Fatal(err)
	}
	got := string(b)
	if !strings.Contains(got, `"items":[]`) {
		t.Fatalf("empty items must encode as [], got %s", got)
	}
	if !strings.Contains(got, `"count":0`) {
		t.Fatalf("count must be 0, got %s", got)
	}
}

// --- track record (Addendum 22) ----------------------------------------------

func float64Ptr(f float64) *float64 { return &f }

func TestTrendAlignmentPerformance_UnsupportedSymbolReturns400(t *testing.T) {
	// Nil pool again: validation must happen before the query, or this panics.
	h := &Handler{Log: quietLogger()}
	for _, raw := range []string{"XAGUSD", "USD_IRT", "ir_gold_18k", "IR_GOLD_FUND_TALA"} {
		rec := httptest.NewRecorder()
		h.TrendAlignmentPerformance(rec, httptest.NewRequest("GET",
			"/api/v1/market/trend-alignment/performance?symbol="+raw, nil))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("symbol=%s: got status %d, want 400", raw, rec.Code)
		}
		var body httpserver.ErrorBody
		if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.Error.Code != "bad_request" {
			t.Fatalf("symbol=%s: wrong error code %q", raw, body.Error.Code)
		}
	}
}

// storedPerformanceRows are rows as the evaluator writes them, in a deliberately
// wrong order. The 90-day row is daily_only with every conditional statistic
// NULL: that is the real shape of the longest window, because the 4H and 1H legs
// have no history that far back and the daily leg was never directionally
// aligned inside it.
func storedPerformanceRows() []trendPerformanceRow {
	return []trendPerformanceRow{
		{WindowDays: 14, Basis: "daily_only", Samples: 13,
			BullishEpisodes: 1, BearishEpisodes: 0,
			BullishBars: 9, BearishBars: 0, UnalignedBars: 4,
			FwdReturnBullishPct: float64Ptr(0.42), FwdReturnBearishPct: nil,
			FwdReturnBaselinePct: float64Ptr(0.31),
			HitRateBullish:       float64Ptr(0.5555555555555556), HitRateBearish: nil,
			EvaluatedFrom: timePtr(trendTime(1)), EvaluatedTo: timePtr(trendTime(9)),
			ComputedAt: trendTime(10), Note: "Daily leg only"},
		{WindowDays: 90, Basis: "daily_only", Samples: 61,
			BullishEpisodes: 0, BearishEpisodes: 0,
			BullishBars: 0, BearishBars: 0, UnalignedBars: 61,
			FwdReturnBullishPct: nil, FwdReturnBearishPct: nil,
			FwdReturnBaselinePct: float64Ptr(0.11),
			HitRateBullish:       nil, HitRateBearish: nil,
			EvaluatedFrom: timePtr(trendTime(0)), EvaluatedTo: timePtr(trendTime(9)),
			ComputedAt: trendTime(10), Note: "the 4H and 1H legs have no history that far back"},
		{WindowDays: 30, Basis: "daily_only", Samples: 29,
			BullishEpisodes: 2, BearishEpisodes: 1,
			BullishBars: 12, BearishBars: 5, UnalignedBars: 12,
			FwdReturnBullishPct: float64Ptr(0.9), FwdReturnBearishPct: float64Ptr(-0.4),
			FwdReturnBaselinePct: float64Ptr(0.2),
			HitRateBullish:       float64Ptr(0.75), HitRateBearish: float64Ptr(0.6),
			EvaluatedFrom: timePtr(trendTime(0)), EvaluatedTo: timePtr(trendTime(9)),
			ComputedAt: trendTime(10), Note: "Daily leg only"},
		{WindowDays: 60, Basis: "full_mtf", Samples: 3,
			BullishEpisodes: 1, BearishEpisodes: 0,
			BullishBars: 3, BearishBars: 0, UnalignedBars: 0,
			FwdReturnBullishPct: float64Ptr(1.5), FwdReturnBearishPct: nil,
			FwdReturnBaselinePct: float64Ptr(1.5),
			HitRateBullish:       float64Ptr(1), HitRateBearish: nil,
			EvaluatedFrom: timePtr(trendTime(2)), EvaluatedTo: timePtr(trendTime(9)),
			ComputedAt: trendTime(10), Note: "three bars is not a track record"},
	}
}

func TestSortTrendPerformanceRows_LongestWindowFirst(t *testing.T) {
	rows := storedPerformanceRows()
	sortTrendPerformanceRows(rows)

	var got []int
	for _, r := range rows {
		got = append(got, r.WindowDays)
	}
	for i, want := range []int{90, 60, 30, 14} {
		if got[i] != want {
			t.Fatalf("order = %v, want [90 60 30 14]", got)
		}
	}
}

func TestBuildTrendPerformanceResponse_EnvelopeAndItemShape(t *testing.T) {
	resp := buildTrendPerformanceResponse("IR_GOLD_18K", storedPerformanceRows())
	b, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(b, &envelope); err != nil {
		t.Fatal(err)
	}
	if len(envelope) != 3 {
		t.Fatalf("envelope must be exactly {symbol,items,count}: %s", b)
	}
	for _, key := range []string{"symbol", "items", "count"} {
		if _, ok := envelope[key]; !ok {
			t.Fatalf("envelope is missing %q: %s", key, b)
		}
	}
	if string(envelope["symbol"]) != `"IR_GOLD_18K"` || string(envelope["count"]) != "4" {
		t.Fatalf("symbol/count wrong: %s", b)
	}

	var items []map[string]json.RawMessage
	if err := json.Unmarshal(envelope["items"], &items); err != nil {
		t.Fatal(err)
	}
	wantKeys := []string{
		"window_days", "basis", "samples", "bullish_episodes", "bearish_episodes",
		"bullish_bars", "bearish_bars", "unaligned_bars",
		"fwd_return_bullish_pct", "fwd_return_bearish_pct", "fwd_return_baseline_pct",
		"hit_rate_bullish", "hit_rate_bearish",
		"evaluated_from", "evaluated_to", "computed_at", "note",
	}
	for _, key := range wantKeys {
		if _, ok := items[0][key]; !ok {
			t.Fatalf("item is missing %q: %s", key, b)
		}
	}
	if len(items[0]) != len(wantKeys) {
		t.Fatalf("item has unexpected keys: %s", b)
	}
	// The reader has to be able to say "that rate is noise", which needs the
	// denominator in the same object as the rate.
	if string(items[0]["samples"]) != "61" {
		t.Fatalf("samples not projected: %s", items[0]["samples"])
	}
	// Timestamps serialize UTC with Z.
	if string(items[0]["computed_at"]) != `"2026-08-12T10:00:00Z"` {
		t.Fatalf("computed_at not RFC3339 UTC: %s", items[0]["computed_at"])
	}
	if string(items[0]["evaluated_from"]) != `"2026-08-12T00:00:00Z"` {
		t.Fatalf("evaluated_from not RFC3339 UTC: %s", items[0]["evaluated_from"])
	}
	// No significance decoration may appear on this contract: with these sample
	// sizes a Sharpe or a p-value would be a claim the data cannot support.
	for _, forbidden := range []string{"sharpe", "p_value", "pvalue", "significan", "confidence"} {
		if strings.Contains(strings.ToLower(string(b)), forbidden) {
			t.Fatalf("statistic %q must not appear on this contract: %s", forbidden, b)
		}
	}
}

func TestBuildTrendPerformanceResponse_OrderedLongestWindowFirst(t *testing.T) {
	resp := buildTrendPerformanceResponse("IR_GOLD_18K", storedPerformanceRows())
	if resp.Count != 4 || len(resp.Items) != 4 {
		t.Fatalf("count = %d, items = %d, want 4 and 4", resp.Count, len(resp.Items))
	}
	for i, want := range []int{90, 60, 30, 14} {
		if resp.Items[i].WindowDays != want {
			t.Fatalf("item %d window_days = %d, want %d", i, resp.Items[i].WindowDays, want)
		}
	}
}

// The 90-day row is daily_only and must stay daily_only: relabelling it as the
// multi-timeframe alignment would be the single most misleading thing this
// endpoint could do, because the 4H and 1H legs have no history that far back.
func TestBuildTrendPerformanceResponse_BasisIsNeverRewritten(t *testing.T) {
	resp := buildTrendPerformanceResponse("IR_GOLD_18K", storedPerformanceRows())
	want := map[int]string{90: "daily_only", 60: "full_mtf", 30: "daily_only", 14: "daily_only"}
	for _, item := range resp.Items {
		if item.Basis != want[item.WindowDays] {
			t.Fatalf("window %d basis = %q, want %q",
				item.WindowDays, item.Basis, want[item.WindowDays])
		}
		if item.Note == "" {
			t.Fatalf("window %d lost its note", item.WindowDays)
		}
	}
}

// "Never happened" and "happened and returned nothing" are different facts.
// A null that arrives as 0.0 would credit the indicator with a measurement it
// never made.
func TestBuildTrendPerformanceResponse_NullStatisticsStayNull(t *testing.T) {
	b, err := json.Marshal(buildTrendPerformanceResponse("IR_GOLD_18K", storedPerformanceRows()))
	if err != nil {
		t.Fatal(err)
	}
	var envelope struct {
		Items []map[string]json.RawMessage `json:"items"`
	}
	if err := json.Unmarshal(b, &envelope); err != nil {
		t.Fatal(err)
	}
	ninety := envelope.Items[0] // window_days DESC, so the 90-day row
	for _, key := range []string{
		"fwd_return_bullish_pct", "fwd_return_bearish_pct",
		"hit_rate_bullish", "hit_rate_bearish",
	} {
		if string(ninety[key]) != "null" {
			t.Fatalf("%s = %s, want null (it was never measured)", key, ninety[key])
		}
	}
	// The baseline IS measured over the same window and must survive beside
	// them: without it a reader cannot tell the indicator from the market.
	if string(ninety["fwd_return_baseline_pct"]) != "0.11" {
		t.Fatalf("baseline = %s, want 0.11", ninety["fwd_return_baseline_pct"])
	}
	// A count of zero, by contrast, IS the measurement.
	if string(ninety["bullish_bars"]) != "0" || string(ninety["unaligned_bars"]) != "61" {
		t.Fatalf("counts not projected: %s", b)
	}
}

// No rows yet is a 200 with an empty list, never a 404 and never a null items.
func TestBuildTrendPerformanceResponse_EmptyEncodesAsList(t *testing.T) {
	b, err := json.Marshal(buildTrendPerformanceResponse("XAUUSD", nil))
	if err != nil {
		t.Fatal(err)
	}
	got := string(b)
	for _, want := range []string{`"symbol":"XAUUSD"`, `"items":[]`, `"count":0`} {
		if !strings.Contains(got, want) {
			t.Fatalf("missing %s in %s", want, got)
		}
	}
}

// Go must not touch the numbers it was handed: no rounding, no rescaling, and
// no rate recomputed from the counts beside it.
func TestBuildTrendPerformanceResponse_StatisticsArePassedThroughVerbatim(t *testing.T) {
	rows := storedPerformanceRows()
	resp := buildTrendPerformanceResponse("IR_GOLD_18K", rows)
	sortTrendPerformanceRows(rows)
	sameFloat := func(got, want *float64) bool {
		if got == nil || want == nil {
			return got == nil && want == nil
		}
		return *got == *want
	}
	for i, item := range resp.Items {
		src := rows[i]
		if !sameFloat(item.HitRateBullish, src.HitRateBullish) ||
			!sameFloat(item.HitRateBearish, src.HitRateBearish) {
			t.Fatalf("window %d hit rates rewritten", item.WindowDays)
		}
		if !sameFloat(item.FwdReturnBullishPct, src.FwdReturnBullishPct) ||
			!sameFloat(item.FwdReturnBearishPct, src.FwdReturnBearishPct) ||
			!sameFloat(item.FwdReturnBaselinePct, src.FwdReturnBaselinePct) {
			t.Fatalf("window %d returns rewritten", item.WindowDays)
		}
		if item.Samples != src.Samples || item.BullishBars != src.BullishBars ||
			item.BearishBars != src.BearishBars || item.UnalignedBars != src.UnalignedBars ||
			item.BullishEpisodes != src.BullishEpisodes ||
			item.BearishEpisodes != src.BearishEpisodes {
			t.Fatalf("window %d counts rewritten", item.WindowDays)
		}
	}
}

// A location-carrying timestamp from the driver must still serialize as UTC Z.
func TestBuildTrendPerformanceResponse_NormalizesLocationToUTC(t *testing.T) {
	tehran := time.FixedZone("Asia/Tehran", int((3*time.Hour + 30*time.Minute).Seconds()))
	local := trendTime(6).In(tehran)
	rows := []trendPerformanceRow{{
		WindowDays: 30, Basis: "daily_only", Samples: 1,
		EvaluatedFrom: timePtr(local), EvaluatedTo: timePtr(local), ComputedAt: local,
		Note: "n/a",
	}}
	b, err := json.Marshal(buildTrendPerformanceResponse("IR_GOLD_18K", rows))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{
		`"computed_at":"2026-08-12T06:00:00Z"`,
		`"evaluated_from":"2026-08-12T06:00:00Z"`,
		`"evaluated_to":"2026-08-12T06:00:00Z"`,
	} {
		if !strings.Contains(string(b), want) {
			t.Fatalf("missing %s in %s", want, b)
		}
	}
}

// hit_rate_bullish over 3 bars is arithmetically valid and epistemically noise.
// The contract's job is to hand the client both numbers so it can say so; this
// test is the guard that the counts never get dropped as "redundant".
func TestBuildTrendPerformanceResponse_SmallSampleKeepsItsDenominator(t *testing.T) {
	resp := buildTrendPerformanceResponse("IR_GOLD_18K", storedPerformanceRows())
	var sixty trendPerformanceItem
	for _, item := range resp.Items {
		if item.WindowDays == 60 {
			sixty = item
		}
	}
	if sixty.Samples != 3 || sixty.BullishBars != 3 || sixty.BullishEpisodes != 1 {
		t.Fatalf("60-day denominators lost: %+v", sixty)
	}
	if sixty.HitRateBullish == nil || *sixty.HitRateBullish != 1 {
		t.Fatalf("60-day hit rate = %v, want 1", sixty.HitRateBullish)
	}
}
