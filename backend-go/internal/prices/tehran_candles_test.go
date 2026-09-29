package prices

import (
	"context"
	"encoding/json"
	"errors"
	"math"
	"net/http"
	"net/http/httptest"
	"reflect"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/bourse"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/equities"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// The Tehran chart symbols, tested without a database: the page arithmetic is
// pure, and the handler is driven through fakes of the two sources.

const tedpixSymbol = "IDX:" + bourse.TEDPIX

func tday(s string) time.Time {
	d, err := time.Parse("2006-01-02", s)
	if err != nil {
		panic(err)
	}
	return d
}

// tehranSessions is n index sessions from `from`, on the exchange's own week
// (no Thursday, no Friday), each 0.1% above the last. The gaps matter: a
// cursor that only works on consecutive days would pass a test on consecutive
// days.
func tehranSessions(n int, from time.Time) []bourse.IndexPoint {
	out := make([]bourse.IndexPoint, 0, n)
	v := 10_000.0
	for d := from; len(out) < n; d = d.AddDate(0, 0, 1) {
		if d.Weekday() == time.Thursday || d.Weekday() == time.Friday {
			continue
		}
		out = append(out, bourse.IndexPoint{Day: d, Value: v})
		v *= 1.001
	}
	return out
}

func indexQuery(t *testing.T, raw string) candleQuery {
	t.Helper()
	return mustQuery(t, "symbol="+tedpixSymbol+"&"+raw)
}

func decodeMap(t *testing.T, v any) map[string]any {
	t.Helper()
	raw, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(raw, &out); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return out
}

// --- an index session on the wire --------------------------------------------

func TestAnIndexSessionIsCloseOnlyOnTheWire(t *testing.T) {
	c := decodeMap(t, indexCandle(bourse.IndexPoint{Day: tday("2026-09-28"),
		Value: 3_713_955.9, Unchanged: true, Rescaled: true}))
	// TSETMC publishes no open and its low/high are not a traded range: all
	// three are PRESENT and null, never the close repeated.
	for _, key := range []string{"open", "high", "low", "volume"} {
		v, ok := c[key]
		if !ok || v != nil {
			t.Fatalf("%s = %v (present %v), want an explicit null", key, v, ok)
		}
	}
	// Not tick buckets: the flags that describe a bucket must not describe a
	// session.
	for _, key := range []string{"ticks", "synthetic"} {
		if _, ok := c[key]; ok {
			t.Fatalf("%s must not appear on an exchange session: %v", key, c)
		}
	}
	if c["close"] != 3_713_955.9 || c["confirmed"] != true {
		t.Fatalf("close/confirmed: %v", c)
	}
	// 00:00 UTC of the trade date — the same boundary a gold 1d bucket has, so
	// the two draw on one time axis — and a one-day session.
	if c["t"] != float64(tday("2026-09-28").Unix()) {
		t.Fatalf("t = %v", c["t"])
	}
	if c["open_time"] != "2026-09-28T00:00:00Z" || c["close_time"] != "2026-09-29T00:00:00Z" {
		t.Fatalf("session bounds %v .. %v", c["open_time"], c["close_time"])
	}
	if c["unchanged"] != true || c["rescaled"] != true {
		t.Fatalf("the store's flags must travel: %v", c)
	}
}

// --- paging ------------------------------------------------------------------

func TestIndexPagesWalkTheWholeSeriesExactlyOnce(t *testing.T) {
	// Mirrors TestPaginateCandlesCursorWalksHistoryWithoutGaps, over a real
	// size: 1,200 sessions in pages of 500 is two full pages and a short one.
	pts := tehranSessions(1200, tday("2021-03-20"))
	now := pts[len(pts)-1].Day.Add(10 * time.Hour)

	seen := map[int64]int{}
	var cursor *time.Time
	for pages := 1; ; pages++ {
		if pages > 5 {
			t.Fatal("pagination did not terminate")
		}
		raw := "limit=500"
		if cursor != nil {
			raw += "&before=" + cursor.Format(time.RFC3339)
		}
		q := indexQuery(t, raw)
		page := buildIndexCandlePage(pts, snapCandleWindow(q, q.Interval, now), q.Limit, q.Overlays)
		for _, c := range page.Candles {
			seen[c.T]++
		}
		if pages == 1 {
			if last := page.Candles[len(page.Candles)-1]; last.T != pts[len(pts)-1].Day.Unix() {
				t.Fatalf("the first page must end at the newest session, got %d", last.T)
			}
		}
		// The cursor is the oldest session the page returned.
		if page.NextBefore == nil || page.NextBefore.Unix() != page.Candles[0].T {
			t.Fatalf("page %d: next_before %v is not the oldest returned session", pages, page.NextBefore)
		}
		if pages < 3 && !page.HasMore {
			t.Fatalf("page %d: has_more is false with %d sessions unseen", pages, 1200-len(seen))
		}
		if !page.HasMore {
			if pages != 3 || len(page.Candles) != 200 {
				t.Fatalf("stopped on page %d with %d candles, want page 3 with 200", pages, len(page.Candles))
			}
			break
		}
		cursor = page.NextBefore
	}
	if len(seen) != len(pts) {
		t.Fatalf("visited %d distinct sessions, want %d", len(seen), len(pts))
	}
	for ts, n := range seen {
		if n != 1 {
			t.Fatalf("session %s returned %d times", time.Unix(ts, 0).UTC().Format("2006-01-02"), n)
		}
	}
}

func TestAnIndexPageNeverWritesTheSharedSeries(t *testing.T) {
	pts := tehranSessions(300, tday("2025-01-04"))
	before := slices.Clone(pts)
	q := indexQuery(t, "limit=50")
	_ = buildIndexCandlePage(pts, snapCandleWindow(q, q.Interval, time.Now()), q.Limit, true)
	if !reflect.DeepEqual(pts, before) {
		t.Fatal("the store's series was modified by building a page from it")
	}
}

func TestThePageKeepsTheStoresUnchangedFlagAtItsFirstSession(t *testing.T) {
	// LoadIndexPoints recomputes `unchanged` inside its window, so the first
	// point of every window loses it. The store flags the WHOLE series, and a
	// page cut from it must keep the flag on its first session.
	pts := tehranSessions(120, tday("2026-01-03"))
	for i := 60; i < 80; i++ { // a 20-session closure
		pts[i].Value = pts[59].Value
		pts[i].Unchanged = true
	}
	q := indexQuery(t, "limit=45&overlays=0&before="+pts[105].Day.Format(time.RFC3339))
	page := buildIndexCandlePage(pts, snapCandleWindow(q, q.Interval, time.Now()), q.Limit, q.Overlays)
	if len(page.Candles) != 45 || page.Candles[0].T != pts[60].Day.Unix() {
		t.Fatalf("page starts at %d with %d candles, want the closure's first session", page.Candles[0].T, len(page.Candles))
	}
	if !page.Candles[0].Unchanged {
		t.Fatal("the first session of the page lost the store's unchanged flag")
	}
	if !page.HasMore {
		t.Fatal("sixty older sessions exist")
	}
}

func TestAnIndexWindowServesOnlyItsOwnSessions(t *testing.T) {
	pts := tehranSessions(200, tday("2026-01-03"))
	from, to := pts[100].Day, pts[120].Day
	q := indexQuery(t, "from="+from.Format(time.RFC3339)+"&to="+to.Format(time.RFC3339))
	page := buildIndexCandlePage(pts, snapCandleWindow(q, q.Interval, time.Now()), q.Limit, true)
	// `to` names a boundary, so it is exclusive: sessions 100..119.
	if len(page.Candles) != 20 || page.Candles[0].T != from.Unix() {
		t.Fatalf("window served %d sessions from %d", len(page.Candles), page.Candles[0].T)
	}
	if page.HasMore {
		t.Fatal("inside a from= window, older means older inside the window")
	}

	empty := indexQuery(t, "before=2000-01-01T00:00:00Z")
	got := buildIndexCandlePage(pts, snapCandleWindow(empty, empty.Interval, time.Now()), 500, true)
	if len(got.Candles) != 0 || got.NextBefore != nil || got.HasMore {
		t.Fatalf("a window before the history must be empty with no cursor: %+v", got)
	}
	raw, _ := json.Marshal(got.Candles)
	if string(raw) != "[]" {
		t.Fatalf("an empty page is [], never null: %s", raw)
	}
}

// --- overlays and levels on a close-only series --------------------------------

func TestCloseOnlyOverlaysLeaveTheClosureOutAndRefuseTheRest(t *testing.T) {
	n := 90
	closes := make([]float64, n)
	include := make([]bool, n)
	for i := range closes {
		closes[i] = 1000 + float64(i)
		include[i] = true
	}
	for i := 40; i < 50; i++ { // a closure: repeated, excluded
		closes[i] = closes[39]
		include[i] = false
	}
	start := 30
	ov := closeOnlyOverlays(closes, include, start)

	for _, key := range []string{"supertrend", "supertrend_dir", "psar", "ichimoku_tenkan",
		"ichimoku_kijun", "ichimoku_senkou_a", "ichimoku_senkou_b"} {
		v, ok := ov[key]
		if !ok || v != nil {
			t.Fatalf("%s = %v, want null: it needs a high and a low the index does not have", key, v)
		}
	}
	sma := ov["sma_20"].([]*float64)
	if len(sma) != n-start {
		t.Fatalf("sma_20 has %d points, want %d (index-aligned with the page)", len(sma), n-start)
	}
	if sma[45-start] != nil {
		t.Fatal("a closed session must not carry an indicator value")
	}
	// Session 55: its twenty included predecessors are 55..50 and 39..26 —
	// the closure is skipped, not averaged in as ten flat days.
	var want float64
	for _, i := range []int{55, 54, 53, 52, 51, 50, 39, 38, 37, 36, 35, 34, 33, 32, 31, 30, 29, 28, 27, 26} {
		want += closes[i]
	}
	want /= 20
	if got := sma[55-start]; got == nil || math.Abs(*got-want) > 1e-6 {
		t.Fatalf("sma_20 at 55 = %v, want %v", got, want)
	}
	for _, key := range []string{"bollinger_upper", "bollinger_mid", "bollinger_lower", "sma_50"} {
		if len(ov[key].([]*float64)) != n-start {
			t.Fatalf("%s is not index-aligned", key)
		}
	}
}

func TestAnIndexHasNoPivotsAndNeedsTwentyRealSessionsForItsBand(t *testing.T) {
	pts := tehranSessions(30, tday("2026-06-01"))
	for i := 5; i < 20; i++ {
		pts[i].Value, pts[i].Unchanged = pts[4].Value, true
	}
	q := indexQuery(t, "")
	page := buildIndexCandlePage(pts, snapCandleWindow(q, q.Interval, time.Now()), q.Limit, true)
	if page.Pivots != nil {
		t.Fatal("classic pivots need a high and a low; an index has neither")
	}
	// 15 real sessions: a narrower band is a different measurement, so none.
	if page.Support != nil || page.Resistance != nil {
		t.Fatalf("support/resistance from 15 real sessions: %v %v", page.Support, page.Resistance)
	}

	full := tehranSessions(30, tday("2026-06-01"))
	page = buildIndexCandlePage(full, snapCandleWindow(q, q.Interval, time.Now()), q.Limit, true)
	if page.Support == nil || *page.Support != *fp(full[10].Value) ||
		page.Resistance == nil || *page.Resistance != *fp(full[29].Value) {
		t.Fatalf("band = %v..%v, want the last 20 closes' extremes", page.Support, page.Resistance)
	}
}

// --- a share ---------------------------------------------------------------------

func f64(v float64) *float64 { return &v }

// shareBars is n traded sessions from `from` (oldest first) with one halt at
// `halted`.
func shareBars(n, halted int, from time.Time) []equities.ChartBar {
	out := make([]equities.ChartBar, n)
	for i := range out {
		c := 2000 + 10*float64(i)
		out[i] = equities.ChartBar{Day: from.AddDate(0, 0, i), Close: c,
			Open: f64(c - 5), High: f64(c + 20), Low: f64(c - 20), LastTrade: f64(c + 1),
			Volume: 1000, Trades: 50, Value: 1000 * c, Traded: true, Factor: 1}
	}
	if halted >= 0 {
		out[halted] = equities.ChartBar{Day: out[halted].Day, Close: out[halted-1].Close,
			Traded: false, Factor: 1}
	}
	return out
}

func TestAHaltedSessionIsAGapNotAFlatDay(t *testing.T) {
	bars := shareBars(5, 3, tday("2026-08-01"))
	bars[1].Factor = 1.5
	bars[2].CloseOutsideRange = true
	page := buildEquityCandlePage(bars, false, 10, true)

	halt := decodeMap(t, page.Candles[3])
	for _, key := range []string{"open", "high", "low"} {
		if halt[key] != nil {
			t.Fatalf("a halted session's %s = %v: TSETMC's zero is not a price", key, halt[key])
		}
	}
	if halt["traded"] != false || halt["close"] != bars[2].Close {
		t.Fatalf("a halt keeps its carried close and says traded=false: %v", halt)
	}
	if _, ok := halt["last_trade"]; ok {
		t.Fatal("a halt has no last trade")
	}
	traded := decodeMap(t, page.Candles[1])
	if traded["traded"] != true || traded["last_trade"] == nil || traded["adjustment_factor"] != 1.5 {
		t.Fatalf("a traded, adjusted session: %v", traded)
	}
	if _, ok := decodeMap(t, page.Candles[0])["adjustment_factor"]; ok {
		t.Fatal("a factor of 1 is omitted")
	}
	if decodeMap(t, page.Candles[2])["close_outside_range"] != true {
		t.Fatal("an official close outside the range must say so")
	}
	if haltedSessions(page.Candles) != 1 {
		t.Fatal("one halt on the page")
	}
}

func TestShareOverlaysAreComputedOverTradedSessionsOnly(t *testing.T) {
	bars := shareBars(40, 30, tday("2026-06-01"))
	page := buildEquityCandlePage(bars, false, 40, true)
	ov := page.Overlays

	sma := ov["sma_20"].([]*float64)
	if len(sma) != 40 {
		t.Fatalf("sma_20 has %d points", len(sma))
	}
	if sma[30] != nil {
		t.Fatal("a halted session must have no indicator value")
	}
	// Session 35's twenty traded predecessors skip the halt at 30.
	var want float64
	for i := 35; i >= 15; i-- {
		if i != 30 {
			want += bars[i].Close
		}
	}
	want /= 20
	if got := sma[35]; got == nil || math.Abs(*got-want) > 1e-6 {
		t.Fatalf("sma_20 at 35 = %v, want %v", got, want)
	}
	dir := ov["supertrend_dir"].([]int)
	if len(dir) != 40 || dir[30] != 0 {
		t.Fatalf("supertrend_dir: len %d, value at the halt %d", len(dir), dir[30])
	}
	for _, key := range []string{"supertrend", "psar", "ichimoku_tenkan", "ichimoku_kijun",
		"ichimoku_senkou_a", "ichimoku_senkou_b"} {
		s := ov[key].([]*float64)
		if len(s) != 40 || s[30] != nil {
			t.Fatalf("%s: len %d, value at the halt %v", key, len(s), s[30])
		}
	}
}

func TestSharePivotsComeFromTheNewestTradedSession(t *testing.T) {
	bars := shareBars(25, 24, tday("2026-06-01")) // the newest session is a halt
	page := buildEquityCandlePage(bars, false, 25, false)
	b := bars[23]
	if page.Pivots == nil || page.Pivots.P != (*b.High+*b.Low+b.Close)/3 {
		t.Fatalf("pivots %+v, want the ones from %s", page.Pivots, b.Day.Format("2006-01-02"))
	}
	if page.Overlays != nil {
		t.Fatal("overlays=0 must not compute overlays")
	}
	if page.Support == nil {
		t.Fatal("24 traded closes are enough for the band")
	}
}

func TestSharePagingHasMore(t *testing.T) {
	bars := shareBars(70, -1, tday("2026-01-01"))
	// 60 warm-up sessions trimmed off the front prove there is more.
	if p := buildEquityCandlePage(bars, false, 10, true); !p.HasMore || len(p.Candles) != 10 ||
		!p.NextBefore.Equal(bars[60].Day) {
		t.Fatalf("trimmed page: more=%v n=%d before=%v", p.HasMore, len(p.Candles), p.NextBefore)
	}
	// Nothing trimmed: the source's own probe decides.
	if p := buildEquityCandlePage(bars, true, 500, false); !p.HasMore {
		t.Fatal("the source said older sessions exist")
	}
	if p := buildEquityCandlePage(bars, false, 500, false); p.HasMore {
		t.Fatal("the start of the history has nothing before it")
	}
	if p := buildEquityCandlePage(nil, true, 500, true); p.HasMore || p.NextBefore != nil {
		t.Fatal("no sessions means no cursor")
	}
}

// --- the handler, through fakes of its two sources ---------------------------------

type fakeIndices struct {
	info bourse.IndexInfo
	pts  []bourse.IndexPoint
	err  error
	code string
}

func (f *fakeIndices) ChartSeries(_ context.Context, code string) (bourse.IndexInfo, []bourse.IndexPoint, bourse.DataAge, string, error) {
	f.code = code
	newest := "2026-09-28"
	age := 1
	return f.info, f.pts, bourse.DataAge{NewestTradeDate: &newest, AgeDays: &age, AsOf: "2026-09-29",
		StaleAfterDays: bourse.StaleAfterDays, RefreshCommand: bourse.RefreshCommand}, "rev-1", f.err
}

type fakeEquities struct {
	bars   equities.ChartBars
	err    error
	code   string
	before *time.Time
	limit  int
}

func (f *fakeEquities) ChartBars(_ context.Context, insCode string, _, before *time.Time, limit int) (equities.ChartBars, error) {
	f.code, f.before, f.limit = insCode, before, limit
	return f.bars, f.err
}

func serveCandles(t *testing.T, h *Handler, query string) (*httptest.ResponseRecorder, map[string]any) {
	t.Helper()
	rec := httptest.NewRecorder()
	h.Candles(rec, httptest.NewRequest(http.MethodGet, "/api/v1/market/candles?"+query, nil))
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatalf("decode %s: %v", rec.Body.String(), err)
	}
	return rec, body
}

func TestTehranSymbolsRefuseEveryIntervalButDaily(t *testing.T) {
	for _, query := range []string{
		"symbol=" + tedpixSymbol + "&interval=4h",
		"symbol=" + tedpixSymbol + "&interval=1w",
		"symbol=" + tedpixSymbol + "&interval=2d",
		"symbol=EQ:46348559193224090&interval=15m",
	} {
		t.Run(query, func(t *testing.T) {
			// No pool and no sources: the refusal must come before either is
			// touched.
			rec, _ := serveCandles(t, &Handler{Log: quietLogger()}, query)
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400", rec.Code)
			}
			var body httpserver.ErrorBody
			_ = json.Unmarshal(rec.Body.Bytes(), &body)
			// The exact tick-feed text: the chart's fallback matches on it.
			if body.Error.Message != unsupportedIntervalMessage {
				t.Fatalf("message = %q", body.Error.Message)
			}
			got, _ := body.Error.Details["supported_intervals"].([]any)
			if len(got) != 1 || got[0] != "1d" {
				t.Fatalf("supported_intervals = %v, want [1d]", body.Error.Details["supported_intervals"])
			}
		})
	}
}

func TestAnIndexChartResponseCarriesItsWholeContract(t *testing.T) {
	src := &fakeIndices{
		info: bourse.IndexInfo{InsCode: bourse.TEDPIX, NameFA: "شاخص کل", NameEN: "TEDPIX",
			Market: "bourse", Kind: "headline", Weighting: "cap", ReturnBasis: "total",
			CheckStatus: "validated"},
		pts: tehranSessions(40, tday("2026-08-01")),
	}
	h := &Handler{Log: quietLogger(), Indices: src}
	rec, body := serveCandles(t, h, "symbol=idx:"+bourse.TEDPIX+"&limit=3")
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %v", rec.Code, body)
	}
	for _, key := range tehranResponseKeys {
		if _, ok := body[key]; !ok {
			t.Fatalf("response is missing %q", key)
		}
	}
	if src.code != bourse.TEDPIX || body["symbol"] != tedpixSymbol {
		t.Fatalf("code %q, symbol %v: the lower-case prefix must normalize", src.code, body["symbol"])
	}
	if pf := body["price_fields"].([]any); len(pf) != 1 || pf[0] != "close" {
		t.Fatalf("price_fields = %v", pf)
	}
	if body["unit"] != "index_points" || body["source"] != "TSETMC" || body["revision"] != "rev-1" {
		t.Fatalf("unit/source/revision: %v %v %v", body["unit"], body["source"], body["revision"])
	}
	if age := body["data_age"].(map[string]any); age["newest_trade_date"] != "2026-09-28" {
		t.Fatalf("data_age = %v", age)
	}
	inst := body["instrument"].(map[string]any)
	if inst["rows_rescaled"] != float64(0) || inst["check_status"] != "validated" {
		t.Fatalf("instrument = %v (rows_rescaled must travel even when zero)", inst)
	}
	cov := body["coverage"].(map[string]any)
	if cov["base_granularity_seconds"] != float64(86400) || cov["intraday_from"] != nil {
		t.Fatalf("coverage = %v", cov)
	}
	if ivs := cov["supported_intervals"].([]any); len(ivs) != 1 || ivs[0] != "1d" {
		t.Fatalf("supported_intervals = %v", ivs)
	}
	if len(body["candles"].([]any)) != 3 || body["has_more"] != true || body["pivots"] != nil {
		t.Fatalf("page: %d candles, has_more %v, pivots %v", len(body["candles"].([]any)), body["has_more"], body["pivots"])
	}
	if notes := body["notes"].([]any); len(notes) < 2 {
		t.Fatalf("notes = %v", notes)
	}
	ov := body["overlays"].(map[string]any)
	if ov["supertrend"] != nil || len(ov["sma_20"].([]any)) != 3 {
		t.Fatalf("overlays: %v", ov)
	}
}

func TestAnIndexTheDeploymentCannotServeIsRefusedByName(t *testing.T) {
	rec, body := serveCandles(t, &Handler{Log: quietLogger(),
		Indices: &fakeIndices{err: bourse.ErrUnknownIndex}}, "symbol="+tedpixSymbol)
	if rec.Code != http.StatusNotFound || body["error"].(map[string]any)["code"] != "not_found" {
		t.Fatalf("unknown: %d %v", rec.Code, body)
	}

	reason := "after correction the newest value is 10.0 times the live figure"
	rec, body = serveCandles(t, &Handler{Log: quietLogger(), Indices: &fakeIndices{
		info: bourse.IndexInfo{InsCode: "1", NameFA: "x", CheckStatus: "refused", RefusalReason: reason},
		err:  bourse.ErrIndexRefused}}, "symbol="+tedpixSymbol)
	e := body["error"].(map[string]any)
	details := e["details"].(map[string]any)
	if rec.Code != http.StatusConflict || e["code"] != "index_unavailable" ||
		details["refusal_reason"] != reason || details["status"] != "refused" {
		t.Fatalf("refused: %d %v", rec.Code, body)
	}

	rec, _ = serveCandles(t, &Handler{Log: quietLogger(), Indices: &fakeIndices{err: errors.New("boom")}},
		"symbol="+tedpixSymbol)
	if rec.Code != http.StatusInternalServerError {
		t.Fatalf("a store failure is a 500, got %d", rec.Code)
	}
}

func TestAShareChartAsksForTheWarmUpAndTodaysSession(t *testing.T) {
	src := &fakeEquities{bars: equities.ChartBars{
		Meta:     equities.ChartMeta{InsCode: "46348559193224090", Symbol: "فولاد"},
		Items:    shareBars(5, 3, tday("2026-09-20")),
		Revision: "2026-09-24|v1|x",
		Notes:    []string{"rials"},
	}}
	rec, body := serveCandles(t, &Handler{Log: quietLogger(), Equities: src},
		"symbol=EQ:46348559193224090&limit=2")
	if rec.Code != http.StatusOK {
		t.Fatalf("status %d: %v", rec.Code, body)
	}
	if src.code != "46348559193224090" || src.limit != 2+candleWarmupBuckets {
		t.Fatalf("asked the source for %q × %d", src.code, src.limit)
	}
	// The exclusive upper bound is a session boundary AFTER now, so a session
	// settled today is inside it.
	now := time.Now().UTC()
	if src.before == nil || !src.before.After(now) || src.before.Sub(now) > 24*time.Hour ||
		src.before.Hour() != 0 || src.before.Minute() != 0 {
		t.Fatalf("before = %v, want the next UTC midnight", src.before)
	}
	for _, key := range tehranResponseKeys {
		if _, ok := body[key]; !ok {
			t.Fatalf("response is missing %q", key)
		}
	}
	if body["unit"] != "IRR" || len(body["price_fields"].([]any)) != 4 || body["revision"] != "2026-09-24|v1|x" {
		t.Fatalf("unit %v fields %v revision %v", body["unit"], body["price_fields"], body["revision"])
	}
	if inst := body["instrument"].(map[string]any); inst["symbol"] != "فولاد" {
		t.Fatalf("instrument = %v", inst)
	}
	notes := body["notes"].([]any)
	if len(notes) != 2 || notes[1] != "1 of the 2 sessions on this page had no trade." {
		t.Fatalf("notes = %v", notes)
	}
}

func TestAShareTheGateRefusesIsA409WithItsVerdict(t *testing.T) {
	rec, body := serveCandles(t, &Handler{Log: quietLogger(), Equities: &fakeEquities{
		err: &equities.AdjustmentUnavailableError{Symbol: "کچاد", Status: "refused", RefusalReason: "gap"}}},
		"symbol=EQ:46348559193224090")
	e := body["error"].(map[string]any)
	d := e["details"].(map[string]any)
	if rec.Code != http.StatusConflict || e["code"] != "adjustment_unavailable" ||
		d["status"] != "refused" || d["chart_symbol"] != "EQ:46348559193224090" {
		t.Fatalf("%d %v", rec.Code, body)
	}

	rec, body = serveCandles(t, &Handler{Log: quietLogger(), Equities: &fakeEquities{
		err: equities.ErrUnknownEquity}}, "symbol=EQ:46348559193224090")
	if rec.Code != http.StatusNotFound || body["error"].(map[string]any)["code"] != "not_found" {
		t.Fatalf("unknown share: %d %v", rec.Code, body)
	}
}

func TestAnUnwiredTehranSourceIsAServerFault(t *testing.T) {
	for _, sym := range []string{tedpixSymbol, "EQ:46348559193224090"} {
		rec, _ := serveCandles(t, &Handler{Log: quietLogger()}, "symbol="+sym)
		if rec.Code != http.StatusInternalServerError {
			t.Fatalf("%s: status %d, want 500", sym, rec.Code)
		}
	}
}

func TestIndexNotesStateWhatTheLineIs(t *testing.T) {
	info := bourse.IndexInfo{Weighting: "equal", ReturnBasis: "price", RowsRescaled: 31,
		Notes: "registry note"}
	page := tehranPage{Candles: []seriesCandle{{Unchanged: true}, {}, {Unchanged: true}}}
	notes := indexNotes(info, page)
	joined := strings.Join(notes, "\n")
	for _, want := range []string{"31 of this index's stored closes", "a price index (dividends excluded), equal-weighted",
		"2 session(s) on this page repeat", "registry note", "not a forecast"} {
		if !strings.Contains(joined, want) {
			t.Fatalf("notes do not say %q:\n%s", want, joined)
		}
	}
}
