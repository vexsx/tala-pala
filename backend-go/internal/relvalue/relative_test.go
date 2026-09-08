package relvalue

// Pairing, the gap, and the evidence gate that decides whether a percentile is
// allowed to be published at all.

import (
	"math"
	"strings"
	"testing"
	"time"
)

func pairedDays(p pairedSeries) []string {
	out := make([]string, len(p))
	for i, pt := range p {
		out[i] = dayString(pt.Day)
	}
	return out
}

// --- pairing ---------------------------------------------------------------------

func TestPairSeries_IntersectsAndCountsWhatItDropped(t *testing.T) {
	a := dailySeries{
		pt(2026, time.January, 1, 10),
		pt(2026, time.January, 2, 20),
		pt(2026, time.January, 3, 30),
		pt(2026, time.January, 5, 50),
	}
	b := dailySeries{
		pt(2026, time.January, 2, 2),
		pt(2026, time.January, 3, 3),
		pt(2026, time.January, 4, 4),
		pt(2026, time.January, 5, 5),
	}
	got := pairSeries(a, b)
	if !equalStrings(pairedDays(got.Points), []string{"2026-01-02", "2026-01-03", "2026-01-05"}) {
		t.Fatalf("paired days = %v", pairedDays(got.Points))
	}
	if got.UnpairedA != 1 || got.UnpairedB != 1 {
		t.Fatalf("unpaired = a:%d b:%d, want 1 and 1", got.UnpairedA, got.UnpairedB)
	}
}

func TestPairSeries_NeverManufacturesAPair(t *testing.T) {
	// The failure this guards: carrying b's stale close onto a day it did not
	// quote puts a flat leg beside a moving one, which reads as relative
	// performance that never happened.
	a := dailySeries{pt(2026, time.January, 1, 10), pt(2026, time.January, 2, 20)}
	b := dailySeries{pt(2026, time.January, 1, 1)}
	got := pairSeries(a, b)
	if len(got.Points) != 1 {
		t.Fatalf("paired %d days, want 1 -- the 2nd has no partner", len(got.Points))
	}
	if got.UnpairedA != 1 {
		t.Errorf("the dropped day must be counted, got %d", got.UnpairedA)
	}
}

func TestPairSeries_DropsNonPositiveCloses(t *testing.T) {
	a := dailySeries{pt(2026, time.January, 1, 0), pt(2026, time.January, 2, 20)}
	b := dailySeries{pt(2026, time.January, 1, 1), pt(2026, time.January, 2, 2)}
	if got := pairSeries(a, b); len(got.Points) != 1 {
		t.Fatalf("a zero close cannot be indexed; paired %d days, want 1", len(got.Points))
	}
}

func TestPairSeries_DisjointHistories(t *testing.T) {
	a := dailySeries{pt(2020, time.January, 1, 1)}
	b := dailySeries{pt(2026, time.January, 1, 1)}
	got := pairSeries(a, b)
	if len(got.Points) != 0 {
		t.Fatal("disjoint histories must not pair")
	}
	if got.UnpairedA != 1 || got.UnpairedB != 1 {
		t.Fatalf("unpaired = a:%d b:%d, want 1 and 1", got.UnpairedA, got.UnpairedB)
	}
}

// --- the evidence gate ---------------------------------------------------------------

// dailyPairs builds n consecutive daily pairs from 2026-01-01 with the given
// per-index values.
func dailyPairs(n int, av, bv func(i int) float64) pairedSeries {
	out := make(pairedSeries, 0, n)
	start := day(2026, time.January, 1)
	for i := 0; i < n; i++ {
		out = append(out, pairedPoint{Day: start.AddDate(0, 0, i), A: av(i), B: bv(i)})
	}
	return out
}

func TestGapDistribution_WithheldBelowTheIndependenceBar(t *testing.T) {
	// 30 daily observations is 29 days of history. At a 10-day window that is
	// TWO independent windows -- and 20 overlapping ones, which is the number
	// that would flatter the reader if it were published alone.
	full := dailyPairs(30, func(i int) float64 { return float64(100 + i) }, func(int) float64 { return 100 })

	pct, basis := gapDistribution(full, 10, 5)
	if pct != nil {
		t.Fatalf("gap_percentile = %v, want null below the evidence bar", *pct)
	}
	if basis.Sufficient {
		t.Fatal("sufficient must be false")
	}
	if basis.IndependentWindows != 2 {
		t.Fatalf("independent windows = %d, want 2 (29 days / 10)", basis.IndependentWindows)
	}
	if basis.OverlappingWindows != 20 {
		t.Fatalf("overlapping windows = %d, want 20", basis.OverlappingWindows)
	}
	if basis.MinIndependentWindows != minIndependentWindows {
		t.Errorf("the bar must be published: %d", basis.MinIndependentWindows)
	}
	// Both counts are reported precisely so the large one cannot be mistaken
	// for the sample size, and the note has to say which is which.
	for _, want := range []string{"not independent", "WITHHELD", "noise wearing a statistic"} {
		if !strings.Contains(basis.Note, want) {
			t.Errorf("note does not contain %q:\n%s", want, basis.Note)
		}
	}
}

func TestGapDistribution_PublishedAboveTheBar(t *testing.T) {
	// 61 daily observations = 60 days; at a 10-day window that is six
	// independent windows, which clears the bar of five.
	//
	// b is flat. a is flat for the first 51 days and then rises by one a day,
	// so the 51 rolling windows produce 41 gaps of 0 and then 1..10.
	full := dailyPairs(61,
		func(i int) float64 {
			if i <= 50 {
				return 100
			}
			return float64(100 + (i - 50))
		},
		func(int) float64 { return 100 })

	// 5.5 rather than 5.0: the gap of the i=45 window is 5.000000000000004 in
	// binary floating point, and a test that straddles it would be asserting
	// against an artefact of the arithmetic rather than against the rule.
	pct, basis := gapDistribution(full, 10, 5.5)
	if !basis.Sufficient {
		t.Fatalf("sufficient = false with %d independent windows", basis.IndependentWindows)
	}
	if basis.IndependentWindows != 6 {
		t.Fatalf("independent windows = %d, want 6", basis.IndependentWindows)
	}
	if basis.OverlappingWindows != 51 {
		t.Fatalf("overlapping windows = %d, want 51", basis.OverlappingWindows)
	}
	// 41 zero gaps plus the gaps of 1..5 are at or below 5.5: 46 of 51.
	want := 46.0 / 51.0 * 100
	if got := mustFloat(t, pct, "gap percentile"); math.Abs(got-want) > 1e-4 {
		t.Fatalf("gap_percentile = %v, want %v", got, want)
	}
	if !strings.Contains(basis.Note, "NOT independent") {
		t.Errorf("even a published percentile must carry its caveat:\n%s", basis.Note)
	}
}

func TestGapDistribution_ExactlyAtTheBarPublishes(t *testing.T) {
	// 51 observations = 50 days = exactly five 10-day windows.
	full := dailyPairs(51, func(int) float64 { return 100 }, func(int) float64 { return 100 })
	pct, basis := gapDistribution(full, 10, 0)
	if basis.IndependentWindows != minIndependentWindows {
		t.Fatalf("independent windows = %d, want exactly %d", basis.IndependentWindows, minIndependentWindows)
	}
	if !basis.Sufficient || pct == nil {
		t.Fatal("the gate is >=, so exactly the minimum must publish")
	}
}

func TestGapDistribution_ZeroLengthWindow(t *testing.T) {
	full := dailyPairs(10, func(int) float64 { return 100 }, func(int) float64 { return 100 })
	pct, basis := gapDistribution(full, 0, 0)
	if pct != nil {
		t.Fatal("a window of less than a day has no comparison length")
	}
	if basis.Sufficient {
		t.Fatal("sufficient must be false")
	}
}

func TestGapDistribution_NoHistory(t *testing.T) {
	pct, basis := gapDistribution(nil, 30, 0)
	if pct != nil || basis.Sufficient {
		t.Fatal("an empty history cannot produce a percentile")
	}
}

// --- decimation ------------------------------------------------------------------------

func TestPickIndices(t *testing.T) {
	cases := []struct {
		n, points int
		want      []int
	}{
		{5, 10, []int{0, 1, 2, 3, 4}}, // fewer points than asked for: all of them
		{5, 5, []int{0, 1, 2, 3, 4}},
		{10, 3, []int{0, 5, 9}},
		{10, 2, []int{0, 9}},
		// points<2 cannot honour "first and last, always", so it collapses to
		// both ends rather than to the last index alone. The HTTP layer refuses
		// it outright; this is the library's floor.
		{10, 1, []int{0, 9}},
		{10, 0, []int{0, 9}},
		{1, 0, []int{0}},
		{0, 5, nil},
	}
	for _, tc := range cases {
		got := pickIndices(tc.n, tc.points)
		if len(got) != len(tc.want) {
			t.Fatalf("pickIndices(%d,%d) = %v, want %v", tc.n, tc.points, got, tc.want)
		}
		for i := range got {
			if got[i] != tc.want[i] {
				t.Fatalf("pickIndices(%d,%d) = %v, want %v", tc.n, tc.points, got, tc.want)
			}
		}
	}
}

func TestPickIndicesAlwaysKeepsBothEnds(t *testing.T) {
	// A decimated chart that silently dropped its last point would show a stale
	// end value beside a fresh headline number.
	for _, points := range []int{2, 7, 100, 499} {
		got := pickIndices(1000, points)
		if got[0] != 0 || got[len(got)-1] != 999 {
			t.Fatalf("points=%d kept %d..%d, want 0..999", points, got[0], got[len(got)-1])
		}
	}
}

// --- assembled response --------------------------------------------------------------------

func relativeFixture(t *testing.T, a, b dailySeries, points int) relativeResponse {
	t.Helper()
	from := day(2026, time.January, 1)
	return buildRelativeResponse(relativeInputs{
		Query: relativeQuery{
			A: "IR_GOLD_18K", B: "USD_IRT", Points: points,
			Window: window{From: &from, To: day(2026, time.January, 31)},
		},
		A:       instrumentRow{Code: "IR_GOLD_18K", Domain: "gold", QuoteCurrency: quoteIRT, Unit: "gram", QualityTier: "official_mirror"},
		B:       instrumentRow{Code: "USD_IRT", Domain: "fx", QuoteCurrency: quoteIRT, Unit: "usd", QualityTier: "proxy", IsProxy: true},
		SeriesA: a,
		SeriesB: b,
		AsOf:    day(2026, time.January, 31),
	})
}

func TestBuildRelativeResponse_IndexesBothLegsAtTheBase(t *testing.T) {
	a := dailySeries{
		pt(2026, time.January, 1, 100),
		pt(2026, time.January, 2, 200),
		pt(2026, time.January, 3, 150),
	}
	b := dailySeries{
		pt(2026, time.January, 1, 100),
		pt(2026, time.January, 2, 100),
		pt(2026, time.January, 3, 100),
	}
	got := relativeFixture(t, a, b, 500)

	if got.BaseDate == nil || !got.BaseDate.Equal(day(2026, time.January, 1)) {
		t.Fatalf("base date = %v, want the first paired day", got.BaseDate)
	}
	if len(got.Series) != 3 {
		t.Fatalf("series has %d points, want 3", len(got.Series))
	}
	if got.Series[0].AIndexed != 100 || got.Series[0].BIndexed != 100 {
		t.Errorf("both legs must be 100 at the base: %+v", got.Series[0])
	}
	if got.Series[1].AIndexed != 200 || got.Series[1].Ratio != 2 {
		t.Errorf("point 1 = %+v, want a_indexed 200 and ratio 2", got.Series[1])
	}
	if math.Abs(mustFloat(t, got.AGrowthPct, "a growth")-50) > 1e-9 {
		t.Errorf("a_growth_pct = %v, want 50", *got.AGrowthPct)
	}
	if math.Abs(mustFloat(t, got.BGrowthPct, "b growth")) > 1e-9 {
		t.Errorf("b_growth_pct = %v, want 0", *got.BGrowthPct)
	}
	if math.Abs(mustFloat(t, got.GapPct, "gap")-50) > 1e-9 {
		t.Errorf("gap_pct = %v, want 50", *got.GapPct)
	}
	// The ratio peaked at 2 and closed at 1.5.
	if math.Abs(mustFloat(t, got.RatioDrawdownPct, "ratio drawdown")-(-25)) > 1e-9 {
		t.Errorf("ratio_drawdown_pct = %v, want -25", *got.RatioDrawdownPct)
	}
	// Three days of history cannot support a percentile of a two-day window.
	if got.GapPercentile != nil {
		t.Errorf("gap_percentile = %v, want null on three days of history", *got.GapPercentile)
	}
	if got.PercentileBasis.Sufficient {
		t.Error("sufficient must be false")
	}
}

func TestBuildRelativeResponse_DecimationIsAnnounced(t *testing.T) {
	a := make(dailySeries, 0, 40)
	b := make(dailySeries, 0, 40)
	start := day(2026, time.January, 1)
	for i := 0; i < 31; i++ {
		a = append(a, dailyPoint{Day: start.AddDate(0, 0, i), Close: float64(100 + i)})
		b = append(b, dailyPoint{Day: start.AddDate(0, 0, i), Close: 100})
	}
	got := relativeFixture(t, a, b, 5)
	if len(got.Series) != 5 {
		t.Fatalf("series has %d points, want 5", len(got.Series))
	}
	if got.Observations != 31 {
		t.Fatalf("observations = %d, want the full 31 paired days", got.Observations)
	}
	found := false
	for _, w := range got.Warnings {
		if strings.Contains(w, "decimated") && strings.Contains(w, "nothing was averaged or interpolated") {
			found = true
		}
	}
	if !found {
		t.Errorf("a thinned chart must say so: %v", got.Warnings)
	}
}

func TestBuildRelativeResponse_NoSharedDay(t *testing.T) {
	a := dailySeries{pt(2026, time.January, 1, 100)}
	b := dailySeries{pt(2026, time.January, 2, 100)}
	got := relativeFixture(t, a, b, 500)
	if got.BaseDate != nil || got.GapPct != nil || len(got.Series) != 0 {
		t.Fatal("with no shared day there is nothing to report, and nothing may be invented")
	}
	if len(got.Warnings) == 0 {
		t.Fatal("the reason must be stated")
	}
	if !strings.Contains(got.Warnings[0], "no stored day in common") {
		t.Errorf("unexpected warning: %q", got.Warnings[0])
	}
}

func TestBuildRelativeResponse_UnpairedDaysAreDeclared(t *testing.T) {
	a := dailySeries{
		pt(2026, time.January, 1, 100),
		pt(2026, time.January, 2, 100),
		pt(2026, time.January, 3, 100),
	}
	b := dailySeries{pt(2026, time.January, 1, 100), pt(2026, time.January, 3, 100)}
	got := relativeFixture(t, a, b, 500)
	if got.Observations != 2 {
		t.Fatalf("observations = %d, want 2", got.Observations)
	}
	found := false
	for _, w := range got.Warnings {
		if strings.Contains(w, "had no partner") {
			found = true
		}
	}
	if !found {
		t.Errorf("dropped days must be declared: %v", got.Warnings)
	}
}

func TestBuildRelativeResponse_WindowOutsideCoverage(t *testing.T) {
	a := dailySeries{pt(2020, time.January, 1, 100), pt(2020, time.January, 2, 100)}
	b := dailySeries{pt(2020, time.January, 1, 100), pt(2020, time.January, 2, 100)}
	got := relativeFixture(t, a, b, 500) // window is January 2026
	if got.BaseDate != nil || got.GapPct != nil {
		t.Fatal("a window with no paired day must produce nothing")
	}
	found := false
	for _, w := range got.Warnings {
		if strings.Contains(w, "shared coverage is") || strings.Contains(w, "Their shared coverage") {
			found = true
		}
	}
	if !found {
		t.Errorf("the real coverage must be named: %v", got.Warnings)
	}
}

func TestBuildRelativeResponse_CarriesProvenance(t *testing.T) {
	a := dailySeries{pt(2026, time.January, 1, 100), pt(2026, time.January, 2, 110)}
	b := dailySeries{pt(2026, time.January, 1, 100), pt(2026, time.January, 2, 100)}
	got := relativeFixture(t, a, b, 500)
	if got.B.QualityTier != "proxy" || !got.B.IsProxy {
		t.Errorf("USD_IRT's proxy status must travel with the comparison: %+v", got.B)
	}
	if got.A.Code != "IR_GOLD_18K" || got.A.Unit != "gram" {
		t.Errorf("identity block is incomplete: %+v", got.A)
	}
}

// --- windows that are not really window_days long --------------------------------------

// The reproduction, exactly: three two-day clusters spread over 2001 calendar
// days. IndependentWindows was HistoryDays/windowDays = 2001/365 = 5, which
// cleared the bar of five and PUBLISHED a percentile -- computed from two
// comparison windows that each spanned a single day, under a note asserting
// they were 365-day windows. It also reported five independent windows against
// two overlapping ones, which cannot be true of any record.
func sparseClusters() pairedSeries {
	out := pairedSeries{}
	start := day(2020, time.January, 1)
	for i, offset := range []int{0, 1000, 2000} {
		for k := 0; k < 2; k++ {
			out = append(out, pairedPoint{
				Day: start.AddDate(0, 0, offset+k),
				A:   float64(100 + 10*i + k),
				B:   100,
			})
		}
	}
	return out
}

func TestGapDistribution_SparseHistoryCannotFakeAYearOfWindows(t *testing.T) {
	full := sparseClusters()
	if got := daysBetween(full[0].Day, full[len(full)-1].Day); got != 2001 {
		t.Fatalf("fixture spans %d days, want 2001", got)
	}

	pct, basis := gapDistribution(full, 365, 5)
	if pct != nil {
		t.Fatalf("gap_percentile = %v, want null: the only comparison windows this "+
			"history offers span ONE DAY, not 365", *pct)
	}
	if basis.Sufficient {
		t.Fatal("sufficient must be false")
	}
	if basis.OverlappingWindows != 0 {
		t.Fatalf("overlapping windows = %d, want 0 -- a one-day window is not a "+
			"365-day window", basis.OverlappingWindows)
	}
	if basis.RejectedShortWindows == 0 {
		t.Fatal("the rejected candidates must be counted, not silently skipped")
	}
	if basis.WindowDaysTolerance != 36 {
		t.Errorf("window_days_tolerance = %d, want 36 (10%% of 365)", basis.WindowDaysTolerance)
	}
	// The calendar span is still reported -- it is a true fact about the
	// history -- but it no longer manufactures windows out of it.
	if basis.HistoryDays != 2001 {
		t.Errorf("history_days = %d, want 2001", basis.HistoryDays)
	}
}

// independent_windows > overlapping_windows is incoherent on its face: a
// non-overlapping window is one of the overlapping ones.
func TestGapDistribution_IndependentNeverExceedsOverlapping(t *testing.T) {
	cases := []struct {
		name       string
		full       pairedSeries
		windowDays int
	}{
		{"sparse clusters", sparseClusters(), 365},
		{"dense daily", dailyPairs(30, func(i int) float64 { return float64(100 + i) },
			func(int) float64 { return 100 }), 10},
		{"window longer than history", dailyPairs(30, func(int) float64 { return 100 },
			func(int) float64 { return 100 }), 400},
		{"two observations", dailyPairs(2, func(int) float64 { return 100 },
			func(int) float64 { return 100 }), 1},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, basis := gapDistribution(tc.full, tc.windowDays, 0)
			if basis.IndependentWindows > basis.OverlappingWindows {
				t.Fatalf("independent %d > overlapping %d",
					basis.IndependentWindows, basis.OverlappingWindows)
			}
		})
	}
}

// A window that ends a few days early because the market did not quote on the
// horizon date is still a window of that length. The tolerance exists so the
// fix above does not throw away real evidence.
func TestGapDistribution_ToleratesAWindowEndingJustShort(t *testing.T) {
	// Weekly observations: a 70-day window's horizon lands on an observation
	// every time, but drop one day from each so the ends are 69 days apart.
	full := pairedSeries{}
	start := day(2020, time.January, 1)
	for i := 0; i < 60; i++ {
		full = append(full, pairedPoint{Day: start.AddDate(0, 0, i*7), A: float64(100 + i), B: 100})
	}
	_, basis := gapDistribution(full, 71, 0)
	if basis.OverlappingWindows == 0 {
		t.Fatalf("a 70-day window against a 71-day request (tolerance %d) must count",
			basis.WindowDaysTolerance)
	}
	if !basis.Sufficient {
		t.Fatalf("this history holds %d independent windows and should publish",
			basis.IndependentWindows)
	}
}

// --- the same instruments both endpoints refuse -----------------------------------------

// The defect: /relative-value never looked at instruments.enabled, so a symbol
// /markets/performance excludes from its table with a named warning was
// silently comparable here.
func TestLegRefusal_MatchesWhatThePerformanceTableExcludes(t *testing.T) {
	for _, inst := range testInstruments() {
		got := legRefusal(inst)
		switch inst.Code {
		case "RETIRED_SYMBOL":
			if got != refusalDisabled {
				t.Errorf("%s: legRefusal = %q, want %q", inst.Code, got, refusalDisabled)
			}
		case "US10Y":
			if got != refusalRate {
				t.Errorf("%s: legRefusal = %q, want %q", inst.Code, got, refusalRate)
			}
		case "DXY":
			// An index level has no currency to be carried into, so the
			// performance table cannot hold it -- but indexing both legs to 100
			// cancels units, so it is a legitimate comparison HERE.
			if got != "" {
				t.Errorf("DXY must stay comparable on /relative-value: %q", got)
			}
		default:
			if got != "" {
				t.Errorf("%s: legRefusal = %q, want it comparable", inst.Code, got)
			}
		}
	}
}
