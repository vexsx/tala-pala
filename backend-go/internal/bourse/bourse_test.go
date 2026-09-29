package bourse

// The arithmetic, tested against the corrected series production actually
// stores. testdata/index_values.json.gz is market_index_values for TEDPIX, the
// equal-weighted all-share and the second-market index, exported from
// production on 2026-09-29 right after the first 0029 ingest — the raw close
// AS SERVED and its scale_exp, so these tests run the same correct() and
// markUnchanged() the store runs. Every expected number below was measured
// from that export with an independent script before these tests were written.

import (
	"compress/gzip"
	"encoding/json"
	"math"
	"os"
	"strconv"
	"testing"
	"time"
)

type exported struct {
	Series map[string][][3]json.RawMessage `json:"series"`
}

func loadExported(t *testing.T, code string) []IndexPoint {
	t.Helper()
	f, err := os.Open("testdata/index_values.json.gz")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	gz, err := gzip.NewReader(f)
	if err != nil {
		t.Fatal(err)
	}
	var e exported
	if err := json.NewDecoder(gz).Decode(&e); err != nil {
		t.Fatal(err)
	}
	rows := e.Series[code]
	if len(rows) == 0 {
		t.Fatalf("no rows for %s", code)
	}
	out := make([]IndexPoint, 0, len(rows))
	for _, r := range rows {
		var day, raw string
		var exp int
		_ = json.Unmarshal(r[0], &day)
		_ = json.Unmarshal(r[1], &raw)
		_ = json.Unmarshal(r[2], &exp)
		d, err := time.Parse(dateLayout, day)
		if err != nil {
			t.Fatal(err)
		}
		v, err := strconv.ParseFloat(raw, 64)
		if err != nil {
			t.Fatal(err)
		}
		out = append(out, IndexPoint{Day: d, Value: correct(v, exp), Rescaled: exp != 0})
	}
	markUnchanged(out)
	return out
}

func day(s string) time.Time {
	d, err := time.Parse(dateLayout, s)
	if err != nil {
		panic(err)
	}
	return d
}

func approx(t *testing.T, name string, got *float64, want, tol float64) {
	t.Helper()
	if got == nil {
		t.Fatalf("%s: got nil, want %v", name, want)
	}
	if math.Abs(*got-want) > tol {
		t.Fatalf("%s: got %v, want %v (±%v)", name, *got, want, tol)
	}
}

// --- the store's own arithmetic ------------------------------------------------

func TestTheSecondMarketIsServedCorrected(t *testing.T) {
	s := loadExported(t, "71704845530629737")
	last := s[len(s)-1]
	// Stored raw as 1,315,840 with scale_exp 1; the exchange's live figure the
	// next morning was 13,310,356.3, +1.15% on this.
	if last.Value != 13158400 || !last.Rescaled {
		t.Fatalf("last = %+v, want 13158400 rescaled", last)
	}
	rescaled := 0
	for _, p := range s {
		if p.Rescaled {
			rescaled++
		}
	}
	if rescaled != 31 {
		t.Fatalf("rescaled = %d, want 31", rescaled)
	}
	// No step in the corrected series is a factor of ten any more.
	for i := 1; i < len(s); i++ {
		r := s[i].Value / s[i-1].Value
		if r > 7.9 || r < 1/7.9 {
			t.Fatalf("%s: corrected step x%.3f survives", dayString(s[i].Day), r)
		}
	}
}

func TestCorrectIsExactForTheStoredExponents(t *testing.T) {
	if correct(1315840, 1) != 13158400 || correct(154466, 1) != 1544660 || correct(12926, -1) != 1292.6 {
		t.Fatal("a power-of-ten correction is not exact")
	}
}

func TestAClosedMarketIsMarkedUnchanged(t *testing.T) {
	s := loadExported(t, TEDPIX)
	total, closure := 0, 0
	for _, p := range s {
		if p.Unchanged {
			total++
			if !p.Day.Before(day("2026-02-25")) && !p.Day.After(day("2026-05-18")) {
				closure++
			}
		}
	}
	if total != 72 {
		t.Fatalf("unchanged rows = %d, want 72", total)
	}
	// 3,713,955.9 held for 50 sessions after 2026-02-25.
	if closure != 50 {
		t.Fatalf("unchanged rows in the 2026 closure = %d, want 50", closure)
	}
}

// --- returns, dispersion, drawdown --------------------------------------------

func TestTheOneYearReturnIsMeasuredFromTheValueInForce(t *testing.T) {
	s := loadExported(t, TEDPIX)
	got, why := windowReturn(s, day("2025-09-28"))
	if why != "" {
		t.Fatal(why)
	}
	// 2,639,373.0 on 2025-09-28 -> 7,459,337.7 on 2026-09-28.
	approx(t, "1y", got, 182.617792, 1e-5)
}

func TestAWindowBeforeTheHistoryIsRefusedNotShortened(t *testing.T) {
	s := loadExported(t, EqualWeighted) // begins 2014-03-19
	got, why := windowReturn(s, day("2010-01-01"))
	if got != nil || why == "" {
		t.Fatalf("got %v %q, want nil with a reason", got, why)
	}
}

func TestDispersionLeavesTheClosureOut(t *testing.T) {
	s := loadExported(t, TEDPIX)
	from := day("2025-09-28")
	year := withBase(s, &from, day("2026-09-28"))
	got, n, why := sessionDispersion(year)
	if why != "" {
		t.Fatal(why)
	}
	if n != 190 {
		t.Fatalf("sessions = %d, want 190 (fifty closure rows excluded)", n)
	}
	approx(t, "dispersion", got, 1.4552091, 1e-6)
	// Counting the closure's repeats as sessions would report a calmer market
	// than the one that traded: 1.3139% instead of 1.4552%.
	naive := 0.0
	var rets []float64
	for i := 1; i < len(year); i++ {
		rets = append(rets, year[i].Value/year[i-1].Value-1)
	}
	m := 0.0
	for _, r := range rets {
		m += r
	}
	m /= float64(len(rets))
	for _, r := range rets {
		naive += (r - m) * (r - m)
	}
	naive = math.Sqrt(naive/float64(len(rets)-1)) * 100
	if math.Abs(naive-1.3139007) > 1e-6 || *got <= naive {
		t.Fatalf("naive dispersion %v should be the smaller 1.3139", naive)
	}
}

func TestTheWorstFallIsMeasuredFromItsOwnPeak(t *testing.T) {
	s := loadExported(t, TEDPIX)
	from := day("2025-09-28")
	dd := maxDrawdown(withBase(s, &from, day("2026-09-28")))
	approx(t, "drawdown", dd.Pct, -19.441938, 1e-5)
	if dayString(*dd.PeakDate) != "2026-01-19" || dayString(*dd.TroughDate) != "2026-02-22" {
		t.Fatalf("peak %s trough %s", dayString(*dd.PeakDate), dayString(*dd.TroughDate))
	}
	if dd.RecoveredDate == nil {
		t.Fatal("TEDPIX regained its January peak inside the window")
	}
}

func TestMaxDrawdownAgreesWithBruteForce(t *testing.T) {
	s := loadExported(t, EqualWeighted)
	worst := 0.0
	for i := range s {
		for j := i + 1; j < len(s); j++ {
			if d := s[j].Value/s[i].Value - 1; d < worst {
				worst = d
			}
		}
	}
	approx(t, "drawdown", maxDrawdown(s).Pct, worst*100, 1e-5)
}

func TestSmaGapCountsSessionsNotRows(t *testing.T) {
	s := []IndexPoint{{Value: 100}, {Value: 100}, {Value: 110}, {Value: 110}, {Value: 120}}
	markUnchanged(s)
	// Sessions counting back: 120, 110, 100 (the repeats are skipped).
	got, why := smaGap(s, 3)
	if why != "" {
		t.Fatal(why)
	}
	approx(t, "gap", got, (120.0/110.0-1)*100, 1e-6)
	if v, why := smaGap(s, 4); v != nil || why == "" {
		t.Fatal("four sessions do not exist here")
	}
}

// --- one index row --------------------------------------------------------------

func validatedRow(code string) indexRow {
	st := statusValidated
	return indexRow{InsCode: code, NameFA: "شاخص کل", Market: "bourse", Kind: "headline", Status: &st}
}

func TestAnIndexRowCarriesEveryFigure(t *testing.T) {
	s := loadExported(t, TEDPIX)
	item := buildIndexItem(validatedRow(TEDPIX), s, nil, nil)
	approx(t, "1y", item.Returns["1y"], 182.617792, 1e-5)
	for _, k := range returnWindows {
		if _, ok := item.Returns[k]; !ok {
			t.Fatalf("return window %s missing", k)
		}
	}
	if item.AllTimeHigh == nil || item.AllTimeHigh.Date != "2026-09-16" {
		t.Fatalf("all-time high = %+v, want 2026-09-16", item.AllTimeHigh)
	}
	if item.UnchangedRows1Y != 50 || item.Sessions1Y != 190 {
		t.Fatalf("1y rows: unchanged %d sessions %d", item.UnchangedRows1Y, item.Sessions1Y)
	}
}

func TestARefusedIndexServesNoFigure(t *testing.T) {
	st, reason := "refused", "after correction the newest value is 10.0 times the live figure"
	r := indexRow{InsCode: "1", Status: &st, RefusalReason: &reason}
	item := buildIndexItem(r, loadExported(t, TEDPIX), nil, nil)
	if item.Last != nil || item.Returns["1y"] != nil || item.AllTimeHigh != nil {
		t.Fatal("a refused index must not serve a corrected figure")
	}
	if item.ReturnReasons["1y"] == "" || item.Check.Status != "refused" {
		t.Fatal("the refusal must be stated")
	}
}

func TestANeverIngestedIndexSaysSo(t *testing.T) {
	item := buildIndexItem(indexRow{InsCode: "1"}, nil, nil, nil)
	if item.Check.Status != statusNeverIngested || item.ReturnReasons["1m"] == "" {
		t.Fatalf("%+v", item.Check)
	}
}

// --- breadth ---------------------------------------------------------------------

func TestBreadthOverTheYear(t *testing.T) {
	pairs := pairSeries(loadExported(t, TEDPIX), loadExported(t, EqualWeighted))
	from := day("2025-09-28")
	points, sum := buildBreadth(pairsInWindow(pairs, &from, day("2026-09-28")))
	approx(t, "cap", sum.CapReturnPct, 182.617792, 1e-5)
	approx(t, "equal", sum.EqualReturnPct, 152.349622, 1e-5)
	approx(t, "spread", sum.SpreadPP, -30.26817, 1e-4)
	if sum.SessionsCompared != 190 || sum.EqualBeatCap != 87 || sum.CapBeatEqual != 103 {
		t.Fatalf("sessions %d eq %d cap %d", sum.SessionsCompared, sum.EqualBeatCap, sum.CapBeatEqual)
	}
	if points[0].Ratio == nil || *points[0].Ratio != 100 {
		t.Fatal("the ratio starts at 100")
	}
	// Large caps carried the year: the ratio ends well below 100.
	if last := points[len(points)-1]; *last.Ratio >= 100 {
		t.Fatalf("ratio ends at %v", *last.Ratio)
	}
}

func TestPairsInWindowTakesThePairInForce(t *testing.T) {
	p := []pairedPoint{{Day: day("2024-01-06")}, {Day: day("2024-01-08")}, {Day: day("2024-01-09")}}
	from := day("2024-01-07")
	got := pairsInWindow(p, &from, day("2024-01-09"))
	if len(got) != 3 || !got[0].Day.Equal(day("2024-01-06")) {
		t.Fatalf("got %d pairs starting %v", len(got), got[0].Day)
	}
}

// --- market value ------------------------------------------------------------------

func TestATotalNeedsBothMarkets(t *testing.T) {
	rows := []marketValueRow{
		{Market: "bourse", Day: day("2026-09-27"), Rials: 2e17},
		{Market: "farabourse", Day: day("2026-09-27"), Rials: 3e16},
		{Market: "bourse", Day: day("2026-09-28"), Rials: 2.2e17},
	}
	usd := map[time.Time]float64{day("2026-09-27"): 1.0 / 100000}
	points, sum := buildMarketValue(rows, usd)
	if points[0].TotalToman == nil || *points[0].TotalToman != 2.3e16 {
		t.Fatalf("total %v", points[0].TotalToman)
	}
	if points[0].TotalUSD == nil || math.Abs(*points[0].TotalUSD-2.3e11) > 1 {
		t.Fatalf("usd %v", points[0].TotalUSD)
	}
	if points[1].TotalToman != nil || points[1].BourseUSD != nil {
		t.Fatal("a one-sided session has no total, and a day with no rate has no dollars")
	}
	if sum.TotalTomanChangePct != nil {
		t.Fatal("one total is not a change")
	}
}

// --- money flow ----------------------------------------------------------------------

func flow(d string, buyI, buyN, sellI, sellN float64, bar *float64) FlowSession {
	return FlowSession{Day: day(d), BuyIValue: buyI, BuyNValue: buyN, SellIValue: sellI,
		SellNValue: sellN, BuyICount: 10, SellICount: 20, BarValue: bar}
}

func ptr(v float64) *float64 { return &v }

func TestAFlowSessionMustSatisfyBothIdentities(t *testing.T) {
	ok, _ := flow("2026-09-28", 70, 30, 40, 60, ptr(100)).Check()
	if !ok {
		t.Fatal("a consistent session was refused")
	}
	if ok, why := flow("2026-09-28", 70, 30, 40, 50, ptr(100)).Check(); ok || why == "" {
		t.Fatal("buy 100 against sell 90 must fail")
	}
	if ok, _ := flow("2026-09-28", 70, 30, 40, 60, ptr(120)).Check(); ok {
		t.Fatal("a total 17% off the bar must fail")
	}
	if ok, _ := flow("2026-09-28", 70, 30, 40, 60, nil).Check(); ok {
		t.Fatal("a session with no bar cannot be cross-checked")
	}
}

func TestFlowSummaryExcludesWhatFailsAndAggregatesTickets(t *testing.T) {
	s := []FlowSession{
		flow("2026-09-27", 70, 30, 40, 60, ptr(100)),
		flow("2026-09-28", 50, 50, 80, 20, ptr(100)),
		flow("2026-09-29", 999, 0, 0, 0, ptr(999)), // buy 999 against sell 0
	}
	sum := SummarizeFlows(s)
	if sum.Consistent != 2 || sum.Excluded != 1 {
		t.Fatalf("consistent %d excluded %d", sum.Consistent, sum.Excluded)
	}
	approx(t, "net", sum.NetIndividualToman, (70-40)+(50-80), 1e-9)
	approx(t, "net share", sum.NetIndividualPctOfValue, 0, 1e-9)
	// Aggregate tickets: buys 120 over 20 traders, sells 120 over 40 traders.
	approx(t, "buyer power", sum.BuyerPower, (120.0/20)/(120.0/40), 1e-9)
	if sum.InflowSessions != 1 || sum.OutflowSessions != 1 {
		t.Fatalf("in %d out %d", sum.InflowSessions, sum.OutflowSessions)
	}
}

func TestTheRosterRowIsTheRosterNotTheMarket(t *testing.T) {
	members := []rosterMember{{InsCode: "a", Symbol: "الف"}, {InsCode: "b", Symbol: "ب"}}
	flows := map[string][]FlowSession{
		"a": {flow("2026-09-28", 70, 30, 40, 60, ptr(100))},
		"b": {flow("2026-09-28", 10, 90, 60, 40, ptr(100))},
	}
	out := buildFlowRoster(members, flows, day("2026-09-29"))
	if out.Count != 2 || out.Roster["1"].Consistent != 2 {
		t.Fatalf("%+v", out.Roster["1"])
	}
	approx(t, "pooled net", out.Roster["1"].NetIndividualToman, 30-50, 1e-9)
	found := false
	for _, n := range out.Notes {
		if len(n) > 0 && contains(n, "not the market") {
			found = true
		}
	}
	if !found {
		t.Fatal("the roster row must say it is not the market")
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}

// --- age, overview ---------------------------------------------------------------------

func TestDataAgeUsesTheSameBoundaryAsTheBars(t *testing.T) {
	newest := day("2026-09-18")
	if buildDataAge(&newest, day("2026-09-28")).Stale {
		t.Fatal("ten days is not stale")
	}
	if !buildDataAge(&newest, day("2026-09-29")).Stale {
		t.Fatal("eleven days is stale")
	}
	if a := buildDataAge(nil, day("2026-09-29")); a.AgeDays != nil || a.Warning == "" {
		t.Fatal("no data is empty, not zero days old")
	}
}

func TestSectorBreadthTotals(t *testing.T) {
	items, tot := buildSectorBreadth([]sectorBreadthItem{
		{SectorCode: "27", DownOver2: 41, DownUnder2: 31, UpUnder2: 26, UpOver2: 80},
		{SectorCode: "01", DownOver2: 0, DownUnder2: 9, UpUnder2: 3, UpOver2: 20},
	})
	if items[0].Total != 178 || tot.Total != 210 {
		t.Fatalf("totals %d %d", items[0].Total, tot.Total)
	}
	approx(t, "basic metals up share", items[0].UpSharePct, float64(106)/178*100, 1e-5)
}

func TestChangePctIsAgainstThePreviousClose(t *testing.T) {
	v, c := 7591547.65, 132209.91
	approx(t, "pct", changePct(&v, &c), 132209.91/(7591547.65-132209.91)*100, 1e-6)
	if changePct(nil, &c) != nil {
		t.Fatal("no value, no percentage")
	}
}

func TestDataAgeFollowsTEDPIXNotADormantIndex(t *testing.T) {
	st := &indexStore{series: map[string][]IndexPoint{
		TEDPIX:              {{Day: day("2026-09-28"), Value: 1}},
		"61848754958448778": {{Day: day("2026-09-29"), Value: 1}},
	}}
	if got := dayString(*st.newestSession()); got != "2026-09-28" {
		t.Fatalf("newest %s, want TEDPIX's 2026-09-28", got)
	}
	delete(st.series, TEDPIX)
	if got := dayString(*st.newestSession()); got != "2026-09-29" {
		t.Fatalf("without TEDPIX the newest series decides, got %s", got)
	}
}
