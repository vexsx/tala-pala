package equities

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"math"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func day(y int, m time.Month, d int) time.Time {
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

func ptr[T any](v T) *T { return &v }

// foolad is the roster row plus a passing verdict, with the numbers migration
// 0028 records for it: 31 corporate actions and one reopening (2008-10-26,
// -39.2% after a 43-day suspension).
func foolad() stockRow {
	return stockRow{
		InsCode:           "46348559193224090",
		SymbolFA:          "فولاد",
		NameFA:            "فولاد مبارکه اصفهان",
		Market:            "bourse",
		SectorCode:        "27",
		SectorFA:          "فلزات اساسی",
		FirstBar:          ptr(day(2007, time.March, 11)),
		LastBar:           ptr(day(2026, time.September, 9)),
		BarCount:          4636,
		Enabled:           true,
		AdjustmentVersion: ptr("priceYesterday-chain-v1"),
		AdjustmentStatus:  ptr("validated"),
		ActionsApplied:    ptr(31),
		Reopenings:        ptr(1),
		PreListingBars:    ptr(0),
		WorstReturn:       ptr(0.1231),
		RefusalReason:     ptr(""),
		AdjustedFirstBar:  ptr(day(2007, time.March, 11)),
		ComputedAt:        ptr(time.Date(2026, time.September, 10, 9, 0, 0, 0, time.UTC)),
	}
}

// --- symbol folding ----------------------------------------------------------

func TestFoldSymbolMatchesThePythonFold(t *testing.T) {
	// The exact strings TSETMC serves, against the exact strings a Persian
	// keyboard produces. Without the fold these compare unequal and a user
	// typing the symbol they can see on screen gets a 404.
	cases := []struct{ raw, want string }{
		{"فملي", "فملی"},         // فملي (Arabic yeh) -> فملی
		{"كچاد", "کچاد"},         // كچاد (Arabic kaf) -> کچاد
		{"فولاد", "فولاد"},       // فولاد: already Persian
		{"خ\u200cگستر", "خگستر"}, // ZWNJ stripped from a key
		{"  ذوب  ", "ذوب"},       // surrounding whitespace
		{"\ufeffشستا", "شستا"},   // a BOM from a copy-paste
	}
	for _, c := range cases {
		if got := foldSymbol(c.raw); got != c.want {
			t.Errorf("foldSymbol(%q) = %q, want %q", c.raw, got, c.want)
		}
	}
}

// --- query parsing -----------------------------------------------------------

func TestBarsDefaultToAdjusted(t *testing.T) {
	// The single most consequential default in this package. Raw closes are
	// wrong by a factor of 600 on the validation symbol, so a caller who does
	// not say gets the number that means something.
	q, perr := parseBarQuery("فولاد", url.Values{})
	if perr != nil {
		t.Fatalf("unexpected refusal: %v", perr)
	}
	if !q.Adjusted {
		t.Fatal("adjusted must default to true")
	}
	if q.Limit != defaultBarLimit {
		t.Errorf("limit = %d, want %d", q.Limit, defaultBarLimit)
	}
}

func TestBarQueryRefusesRatherThanGuessing(t *testing.T) {
	cases := []struct {
		name   string
		symbol string
		query  url.Values
	}{
		{"empty symbol", "", url.Values{}},
		{"typo'd bool", "فولاد", url.Values{"adjusted": {"treu"}}},
		{"bare year as a date", "فولاد", url.Values{"from": {"2026"}}},
		{"unix seconds as a date", "فولاد", url.Values{"to": {"1757462400"}}},
		{"inverted window", "فولاد", url.Values{
			"from": {"2026-01-01"}, "to": {"2025-01-01"}}},
		{"limit zero", "فولاد", url.Values{"limit": {"0"}}},
		{"limit over the cap", "فولاد", url.Values{"limit": {"999999"}}},
		{"limit not a number", "فولاد", url.Values{"limit": {"all"}}},
	}
	for _, c := range cases {
		if _, perr := parseBarQuery(c.symbol, c.query); perr == nil {
			t.Errorf("%s: expected a refusal, got none", c.name)
		}
	}
}

func TestBarQueryFoldsTheSymbolInThePath(t *testing.T) {
	q, perr := parseBarQuery("فملي", url.Values{})
	if perr != nil {
		t.Fatalf("unexpected refusal: %v", perr)
	}
	if q.Symbol != "فملی" {
		t.Errorf("symbol = %q, want the Persian-yeh spelling", q.Symbol)
	}
}

// --- projection --------------------------------------------------------------

func bars() []barRow {
	// Newest-first, as the query orders them. The factors are the shape a real
	// chain has: 1.0 on the newest segment (back-adjustment leaves today's
	// price alone) and larger going back.
	return []barRow{
		{TradeDate: day(2026, time.September, 9), Open: 2880, High: 2887, Low: 2820,
			Close: 2887, FinalClose: 2881, Volume: 3464453616, TradeCount: 15381,
			Value: 9982417051844, Factor: 1.0},
		{TradeDate: day(2026, time.September, 8), Open: 2803, High: 2803, Low: 2803,
			Close: 2803, FinalClose: 2803, Volume: 100, TradeCount: 5,
			Value: 280300, Factor: 1.0},
		{TradeDate: day(2026, time.September, 7), Open: 0, High: 0, Low: 0,
			Close: 2722, FinalClose: 2722, Volume: 0, TradeCount: 0,
			Value: 0, Factor: 2.0},
	}
}

func TestAdjustedResponseMultipliesPricesAndNotTurnover(t *testing.T) {
	q := barQuery{Symbol: "فولاد", Adjusted: true, Limit: 10}
	out := buildBarsResponse(foolad(), bars(), q)

	if len(out.Items) != 3 {
		t.Fatalf("items = %d, want 3", len(out.Items))
	}
	// Oldest-first on the wire, so a chart draws it without reversing.
	if out.Items[0].Date != "2026-09-07" || out.Items[2].Date != "2026-09-09" {
		t.Fatalf("items are not oldest-first: %s .. %s",
			out.Items[0].Date, out.Items[2].Date)
	}
	oldest := out.Items[0]
	if oldest.FinalClose != 5444 { // 2722 * 2.0
		t.Errorf("final_close = %v, want 5444", oldest.FinalClose)
	}
	// Volume and turnover are counts of things that actually happened. Scaling
	// the rial turnover by a back-adjustment factor would claim money changed
	// hands that never did, and the volume it is the counterpart of is not
	// scaled either.
	if oldest.Volume != 0 || oldest.Value != 0 {
		t.Errorf("volume/value were scaled: %d / %v", oldest.Volume, oldest.Value)
	}
	if oldest.Factor != 2.0 {
		t.Errorf("adjustment_factor = %v, want 2.0 on an adjusted bar", oldest.Factor)
	}
	if oldest.Traded {
		t.Error("a zero-volume bar must not report traded=true")
	}
	// A factor of 1 is omitted rather than serialised as noise on every row.
	if out.Items[2].Factor != 0 {
		t.Errorf("a factor of 1 should be omitted, got %v", out.Items[2].Factor)
	}
}

func TestUnadjustedResponseServesExactlyWhatTheExchangePrinted(t *testing.T) {
	q := barQuery{Symbol: "فولاد", Adjusted: false, Limit: 10}
	out := buildBarsResponse(foolad(), bars(), q)

	oldest := out.Items[0]
	if oldest.FinalClose != 2722 || oldest.Close != 2722 {
		t.Errorf("an unadjusted read must not apply the factor: %v", oldest.FinalClose)
	}
	if oldest.Factor != 0 {
		t.Error("adjustment_factor must not appear on an unadjusted response")
	}
	if out.Adjusted {
		t.Error("the response must state that it is unadjusted")
	}
	// Even an unadjusted response states which arithmetic exists, because a
	// caller comparing the two needs to know what they are comparing against.
	if out.Adjustment.Version != "priceYesterday-chain-v1" || out.Adjustment.ActionsApplied != 31 {
		t.Errorf("the adjustment provenance must travel on every response: %+v",
			out.Adjustment)
	}
}

func TestEveryResponseStatesItsVersionAndActionCount(t *testing.T) {
	for _, adjusted := range []bool{true, false} {
		out := buildBarsResponse(foolad(), bars(),
			barQuery{Symbol: "فولاد", Adjusted: adjusted, Limit: 10})
		if out.Adjustment.Version == "" {
			t.Errorf("adjusted=%v: no adjustment_version on the response", adjusted)
		}
		if out.Adjustment.ActionsApplied != 31 {
			t.Errorf("adjusted=%v: actions_applied = %d, want 31",
				adjusted, out.Adjustment.ActionsApplied)
		}
		if out.Currency != currencyRial {
			t.Errorf("adjusted=%v: currency = %q, want IRR — the rest of this API "+
				"reports Iranian amounts in toman and equities are quoted in rial",
				adjusted, out.Currency)
		}
	}
}

func TestAdjustedResponseCarriesTheReopeningCaveat(t *testing.T) {
	out := buildBarsResponse(foolad(), bars(),
		barQuery{Symbol: "فولاد", Adjusted: true, Limit: 10})
	if out.Note == "" {
		t.Fatal("a series with a reopening must say so: a return spanning one is " +
			"not a session return")
	}
	if out.Adjustment.Reopenings != 1 {
		t.Errorf("reopenings = %d, want 1", out.Adjustment.Reopenings)
	}
}

func TestPagingDropsTheOldestBarNotTheNewest(t *testing.T) {
	q := barQuery{Symbol: "فولاد", Adjusted: false, Limit: 2}
	out := buildBarsResponse(foolad(), bars(), q)

	if !out.HasMore {
		t.Fatal("has_more must be true when the query returned limit+1 rows")
	}
	if len(out.Items) != 2 {
		t.Fatalf("items = %d, want 2", len(out.Items))
	}
	// The newest bar is the one a caller always wants; the extra row that
	// proves has_more is the oldest.
	if out.Items[1].Date != "2026-09-09" {
		t.Errorf("newest served bar = %s, want 2026-09-09", out.Items[1].Date)
	}
	if out.Items[0].Date != "2026-09-08" {
		t.Errorf("oldest served bar = %s, want 2026-09-08", out.Items[0].Date)
	}
}

func TestNeverIngestedIsDistinctFromRefused(t *testing.T) {
	fresh := stockRow{InsCode: "1", SymbolFA: "x"}
	if got := buildAdjustment(fresh); got.Status != statusNeverIngested || got.Servable {
		t.Errorf("an instrument with no verdict must read as %q and be unservable, got %+v",
			statusNeverIngested, got)
	}

	refused := foolad()
	refused.AdjustmentStatus = ptr("refused")
	refused.RefusalReason = ptr("2 single-session move(s) beyond 25% survive adjustment")
	got := buildAdjustment(refused)
	if got.Servable {
		t.Error("a refused adjustment must never be servable")
	}
	if got.RefusalReason == "" {
		t.Error("a refused verdict must carry its reason to the client")
	}
}

func TestStocksResponseIsAListNotNull(t *testing.T) {
	out := buildStocksResponse(nil)
	if out.Items == nil {
		t.Fatal("an empty roster must serialise as [] so no client special-cases null")
	}
	blob, err := json.Marshal(out)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if string(blob) != `{"items":[],"count":0}` {
		t.Errorf("unexpected empty payload: %s", blob)
	}
}

func TestStocksResponseCarriesCoverageAndVerdict(t *testing.T) {
	out := buildStocksResponse([]stockRow{foolad()})
	item := out.Items[0]
	if item.FirstBar != "2007-03-11" || item.LastBar != "2026-09-09" || item.BarCount != 4636 {
		t.Errorf("coverage is wrong: %+v", item)
	}
	if !item.Adjustment.Servable || item.Adjustment.ActionsApplied != 31 {
		t.Errorf("verdict is wrong: %+v", item.Adjustment)
	}
	if item.InstrumentCode != "" {
		t.Error("instrument_code must be omitted when the equity is not a modelled instrument")
	}
	if math.Abs(*item.Adjustment.WorstReturn-0.1231) > 1e-9 {
		t.Errorf("worst_session_return = %v", *item.Adjustment.WorstReturn)
	}
}

// --- the gate at the read edge -----------------------------------------------
//
// These drive the real handler with a nil pool, which is safe only because the
// refusals below are decided BEFORE any query runs. That is the point being
// tested: an unknown symbol and an unvalidated adjustment must be refused
// without the database being consulted at all.

func request(t *testing.T, symbol, query string) *http.Request {
	t.Helper()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/stocks/x/bars?"+query, nil)
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("symbol", symbol)
	return req.WithContext(context.WithValue(req.Context(), chi.RouteCtxKey, rctx))
}

func TestBarsRefusesABadParameterBeforeTouchingTheDatabase(t *testing.T) {
	h := &Handler{Pool: nil, Log: quietLogger()}
	rec := httptest.NewRecorder()
	h.Bars(rec, request(t, "فولاد", "limit=0"))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestUnknownSymbolIsRefusedAndNeverSubstituted(t *testing.T) {
	rec := httptest.NewRecorder()
	unknownSymbol(rec, "فولادد")

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	var body struct {
		Error struct {
			Code    string         `json:"code"`
			Message string         `json:"message"`
			Details map[string]any `json:"details"`
		} `json:"error"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Error.Code != "not_found" {
		t.Errorf("code = %q, want not_found", body.Error.Code)
	}
	if body.Error.Details["symbol"] != "فولادد" {
		t.Errorf("the refused symbol must be echoed, got %v", body.Error.Details)
	}
}

func TestConflictBodyShape(t *testing.T) {
	// The 409 an adjusted read of a refused symbol produces, checked for the
	// two things a caller acts on: the reason, and the way out.
	rec := httptest.NewRecorder()
	httpserver.Error(rec, http.StatusConflict, "adjustment_unavailable",
		`no validated corporate-action adjustment exists for "کچاد", so adjusted `+
			`prices are not served; retry with ?adjusted=false for the exchange's raw prints`,
		map[string]any{"symbol": "کچاد", "status": "refused", "refusal_reason": "..."})

	if rec.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409", rec.Code)
	}
	var body struct {
		Error struct {
			Code    string         `json:"code"`
			Message string         `json:"message"`
			Details map[string]any `json:"details"`
		} `json:"error"`
	}
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("decode: %v", err)
	}
	if body.Error.Code != "adjustment_unavailable" {
		t.Errorf("code = %q", body.Error.Code)
	}
	if body.Error.Details["status"] != "refused" {
		t.Errorf("the verdict must be on the refusal: %v", body.Error.Details)
	}
}

func TestPreListingExclusionIsVisibleOnTheWire(t *testing.T) {
	// نوری: 1,876 stored bars, 161 of them placeholders at the 1,000-rial par
	// value before its first trade on 2019-07-13. An adjusted read is 161 bars
	// shorter than bar_count, and the response has to say why — otherwise the
	// discrepancy looks like missing data.
	nouri := stockRow{
		InsCode:           "19040514831923530",
		SymbolFA:          "نوری",
		Market:            "bourse",
		BarCount:          1876,
		FirstBar:          ptr(day(2018, time.November, 10)),
		LastBar:           ptr(day(2026, time.September, 9)),
		Enabled:           true,
		AdjustmentVersion: ptr("priceYesterday-chain-v1"),
		AdjustmentStatus:  ptr("validated"),
		ActionsApplied:    ptr(14),
		Reopenings:        ptr(1),
		PreListingBars:    ptr(161),
		AdjustedFirstBar:  ptr(day(2019, time.July, 13)),
	}
	item := buildStocksResponse([]stockRow{nouri}).Items[0]

	if item.BarCount != 1876 {
		t.Errorf("bar_count = %d, want the stored total 1876", item.BarCount)
	}
	if item.Adjustment.PreListingBars != 161 {
		t.Errorf("pre_listing_bars = %d, want 161", item.Adjustment.PreListingBars)
	}
	if item.Adjustment.AdjustedFirstBar != "2019-07-13" {
		t.Errorf("adjusted_first_bar = %q, want the first traded session",
			item.Adjustment.AdjustedFirstBar)
	}
	// The raw series still starts where TSETMC's does: the placeholders are
	// excluded from the ADJUSTED series, not deleted.
	if item.FirstBar != "2018-11-10" {
		t.Errorf("first_bar = %q, want the first stored bar", item.FirstBar)
	}
}

// count shipped as 0 beside a fully populated 19-year series: the field was
// declared and never assigned, while the sibling /stocks response set its own
// correctly. A client that renders on count>0, or pages on it, sees no data for
// فولاد. Asserted on both a truncated and a complete page, because the two
// differ by the sentinel row the query deliberately over-fetches.
func TestBuildBarsResponse_CountMatchesTheItemsServed(t *testing.T) {
	base := bars()
	rows := make([]barRow, 0, 5)
	for i := 0; i < 5; i++ {
		rows = append(rows, base[i%len(base)])
	}

	full := buildBarsResponse(foolad(), rows, barQuery{Adjusted: true, Limit: 10})
	if full.Count != len(full.Items) {
		t.Fatalf("complete page: count=%d but %d items served", full.Count, len(full.Items))
	}
	if full.Count != 5 {
		t.Fatalf("complete page: count=%d, want 5", full.Count)
	}

	// Limit 3 against 5 rows: the query hands over one extra as the has_more
	// sentinel, so counting `rows` would report 4 for a 3-item page.
	page := buildBarsResponse(foolad(), rows[:4], barQuery{Adjusted: true, Limit: 3})
	if !page.HasMore {
		t.Fatal("truncated page must report has_more")
	}
	if page.Count != len(page.Items) {
		t.Fatalf("truncated page: count=%d but %d items served", page.Count, len(page.Items))
	}
	if page.Count != 3 {
		t.Fatalf("truncated page: count=%d, want 3 (not the 4 rows handed in)", page.Count)
	}
}
