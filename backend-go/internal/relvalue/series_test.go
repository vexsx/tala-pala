package relvalue

// The numeraire rule and the four statistics. These are the places this package
// can lie, so every test below is written against a hand-built series with no
// database anywhere near it.

import (
	"math"
	"testing"
	"time"
)

// pt is one day's close.
func pt(y int, m time.Month, d int, close float64) dailyPoint {
	return dailyPoint{Day: day(y, m, d), Close: close}
}

func closes(s dailySeries) []float64 {
	out := make([]float64, len(s))
	for i, p := range s {
		out[i] = p.Close
	}
	return out
}

func days(s dailySeries) []string {
	out := make([]string, len(s))
	for i, p := range s {
		out[i] = dayString(p.Day)
	}
	return out
}

func equalFloats(a, b []float64, tol float64) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if math.Abs(a[i]-b[i]) > tol {
			return false
		}
	}
	return true
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

func mustFloat(t *testing.T, p *float64, name string) float64 {
	t.Helper()
	if p == nil {
		t.Fatalf("%s is null; a value was expected", name)
	}
	return *p
}

// --- conversion chains ------------------------------------------------------------

func TestConversionSteps(t *testing.T) {
	cases := []struct {
		name      string
		quote     string
		numeraire string
		want      []convStep
		wantOK    bool
	}{
		{"toman asset in toman needs no series at all", quoteIRT, "IRT", nil, true},
		{"toman asset in dollars divides by the fx rate", quoteIRT, "USD",
			[]convStep{{usdSeriesCode, opDivide}}, true},
		{"toman asset in gold divides by the gram price", quoteIRT, "GOLD",
			[]convStep{{goldSeriesCode, opDivide}}, true},
		{"dollar asset in toman multiplies by the fx rate", quoteUSD, "IRT",
			[]convStep{{usdSeriesCode, opMultiply}}, true},
		// A round trip through USD_IRT would be near-exact arithmetic and a real
		// coverage bug: XAUUSD in dollars would start DROPPING days on which the
		// toman rate did not quote.
		{"dollar asset in dollars is a pass-through", quoteUSD, "USD", nil, true},
		// Toman first, then grams. A USD price divided by a toman price is a
		// number with no unit at all.
		{"dollar asset in gold routes through toman", quoteUSD, "GOLD",
			[]convStep{{usdSeriesCode, opMultiply}, {goldSeriesCode, opDivide}}, true},
		{"a rate has no unit of account", quotePCT, "USD", nil, false},
		{"an index level has no unit of account", quoteINDEX, "IRT", nil, false},
		{"a ratio has no unit of account", quoteRATIO, "GOLD", nil, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, ok := conversionSteps(tc.quote, tc.numeraire)
			if ok != tc.wantOK {
				t.Fatalf("ok = %v, want %v", ok, tc.wantOK)
			}
			if len(got) != len(tc.want) {
				t.Fatalf("steps = %v, want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("step %d = %v, want %v", i, got[i], tc.want[i])
				}
			}
		})
	}
}

func TestConversionNeverMixesTomanAndDollars(t *testing.T) {
	// The gold divisor is quoted in toman per gram, so every chain that reaches
	// GOLD must put the asset into toman first. This is the structural form of
	// the "never mix IRT and IRR/USD" rule.
	steps, ok := conversionSteps(quoteUSD, "GOLD")
	if !ok || len(steps) != 2 {
		t.Fatalf("USD -> GOLD steps = %v", steps)
	}
	if steps[0].Series != usdSeriesCode || steps[0].Op != opMultiply {
		t.Errorf("first step must lift the dollar price into toman, got %v", steps[0])
	}
	if steps[1].Series != goldSeriesCode || steps[1].Op != opDivide {
		t.Errorf("second step must divide the toman value by the gram price, got %v", steps[1])
	}
}

// --- the carry-forward rule ---------------------------------------------------------

func TestApplyStep_UsesTheSameDayQuoteWhenThereIsOne(t *testing.T) {
	asset := dailySeries{pt(2026, time.January, 1, 100), pt(2026, time.January, 2, 200)}
	num := dailySeries{pt(2026, time.January, 1, 10), pt(2026, time.January, 2, 20)}
	got := applyStep(asset, num, opDivide)
	if !equalFloats(closes(got.Points), []float64{10, 10}, 1e-12) {
		t.Fatalf("closes = %v, want [10 10]", closes(got.Points))
	}
	if got.CarriedForward != 0 || got.NoPriorQuote != 0 {
		t.Errorf("nothing should have been carried or dropped: %+v", got)
	}
}

func TestApplyStep_CarriesTheLastQuoteForward(t *testing.T) {
	// The numeraire quoted on the 1st and then went quiet. The 2nd and 3rd are
	// converted at the 1st's rate -- the last observation AT OR BEFORE the day.
	asset := dailySeries{
		pt(2026, time.January, 1, 100),
		pt(2026, time.January, 2, 120),
		pt(2026, time.January, 3, 140),
	}
	num := dailySeries{pt(2026, time.January, 1, 10)}
	got := applyStep(asset, num, opDivide)
	if !equalFloats(closes(got.Points), []float64{10, 12, 14}, 1e-12) {
		t.Fatalf("closes = %v, want [10 12 14]", closes(got.Points))
	}
	if got.CarriedForward != 2 {
		t.Errorf("carried forward = %d, want 2 (the 2nd and the 3rd)", got.CarriedForward)
	}
	if got.NoPriorQuote != 0 {
		t.Errorf("nothing should have been dropped: %+v", got)
	}
}

func TestApplyStep_NeverCarriesBackward(t *testing.T) {
	// THE look-ahead test. The numeraire's first quote is the 5th. The asset's
	// days before that CANNOT be converted: the only value available is from
	// their future, and using it would price 2026-01-01 with a rate that did not
	// exist until 2026-01-05.
	asset := dailySeries{
		pt(2026, time.January, 1, 100),
		pt(2026, time.January, 2, 100),
		pt(2026, time.January, 5, 100),
		pt(2026, time.January, 6, 100),
	}
	num := dailySeries{pt(2026, time.January, 5, 4), pt(2026, time.January, 6, 5)}
	got := applyStep(asset, num, opDivide)

	if !equalStrings(days(got.Points), []string{"2026-01-05", "2026-01-06"}) {
		t.Fatalf("days = %v, want only the 5th and the 6th", days(got.Points))
	}
	if !equalFloats(closes(got.Points), []float64{25, 20}, 1e-12) {
		t.Fatalf("closes = %v, want [25 20]", closes(got.Points))
	}
	if got.NoPriorQuote != 2 {
		t.Fatalf("dropped = %d, want 2 -- the days with no PRIOR quote", got.NoPriorQuote)
	}
	// And they are dropped, not guessed: had the 5th's rate been carried
	// backward, the first close would be 25 on 2026-01-01.
	for _, p := range got.Points {
		if p.Day.Before(day(2026, time.January, 5)) {
			t.Fatalf("a day before the numeraire's first quote survived: %v", p)
		}
	}
}

func TestApplyStep_DropsNonPositiveQuotes(t *testing.T) {
	asset := dailySeries{pt(2026, time.January, 1, 100), pt(2026, time.January, 2, 100)}
	num := dailySeries{pt(2026, time.January, 1, 0), pt(2026, time.January, 2, 5)}
	got := applyStep(asset, num, opDivide)
	if len(got.Points) != 1 || got.NonPositiveQuote != 1 {
		t.Fatalf("a zero rate must remove the day rather than divide by it: %+v", got)
	}
	if !equalFloats(closes(got.Points), []float64{20}, 1e-12) {
		t.Fatalf("closes = %v, want [20]", closes(got.Points))
	}
}

func TestApplyStep_Multiplies(t *testing.T) {
	asset := dailySeries{pt(2026, time.January, 2, 3)}
	num := dailySeries{pt(2026, time.January, 1, 1000)}
	got := applyStep(asset, num, opMultiply)
	if !equalFloats(closes(got.Points), []float64{3000}, 1e-12) {
		t.Fatalf("closes = %v, want [3000]", closes(got.Points))
	}
	if got.CarriedForward != 1 {
		t.Errorf("the rate came from an earlier day and should be counted: %+v", got)
	}
}

func TestConvertSeries_EmptyChainIsIdentity(t *testing.T) {
	asset := dailySeries{pt(2026, time.January, 1, 100), pt(2026, time.January, 2, 110)}
	got := convertSeries(asset, nil, map[string]dailySeries{})
	if !equalFloats(closes(got.Points), []float64{100, 110}, 0) {
		t.Fatalf("an empty chain must return the asset untouched, got %v", closes(got.Points))
	}
	if got.NoPriorQuote != 0 || got.MissingSeries != "" {
		t.Errorf("an identity conversion cannot fail: %+v", got)
	}
}

func TestConvertSeries_TwoStepChain(t *testing.T) {
	// XAUUSD 2 USD/unit, USD_IRT 1000 toman/USD, IR_GOLD_18K 100 toman/gram
	// => 2 * 1000 / 100 = 20 grams of 18k gold.
	asset := dailySeries{pt(2026, time.January, 1, 2)}
	sources := map[string]dailySeries{
		usdSeriesCode:  {pt(2026, time.January, 1, 1000)},
		goldSeriesCode: {pt(2026, time.January, 1, 100)},
	}
	steps, _ := conversionSteps(quoteUSD, "GOLD")
	got := convertSeries(asset, steps, sources)
	if !equalFloats(closes(got.Points), []float64{20}, 1e-12) {
		t.Fatalf("closes = %v, want [20]", closes(got.Points))
	}
}

func TestConvertSeries_MissingSeriesIsNamedNotGuessed(t *testing.T) {
	asset := dailySeries{pt(2026, time.January, 1, 100)}
	got := convertSeries(asset, []convStep{{goldSeriesCode, opDivide}}, map[string]dailySeries{})
	if got.MissingSeries != goldSeriesCode {
		t.Fatalf("missing series = %q, want %q", got.MissingSeries, goldSeriesCode)
	}
	if len(got.Points) != 0 {
		t.Fatalf("nothing may survive a conversion whose series does not exist: %v", got.Points)
	}
}

func TestConvertSeries_DropCountsAccumulateAcrossSteps(t *testing.T) {
	asset := dailySeries{
		pt(2026, time.January, 1, 1),
		pt(2026, time.January, 2, 1),
		pt(2026, time.January, 3, 1),
	}
	sources := map[string]dailySeries{
		// No fx quote until the 2nd: the 1st is dropped here.
		usdSeriesCode: {pt(2026, time.January, 2, 10)},
		// No gram price until the 3rd: the 2nd is dropped there.
		goldSeriesCode: {pt(2026, time.January, 3, 5)},
	}
	steps, _ := conversionSteps(quoteUSD, "GOLD")
	got := convertSeries(asset, steps, sources)
	if !equalStrings(days(got.Points), []string{"2026-01-03"}) {
		t.Fatalf("days = %v, want only the 3rd", days(got.Points))
	}
	if got.NoPriorQuote != 2 {
		t.Fatalf("accumulated drops = %d, want 2 (one per step)", got.NoPriorQuote)
	}
}

// --- windowing -----------------------------------------------------------------------

func TestSliceHonoursBothBounds(t *testing.T) {
	s := dailySeries{
		pt(2026, time.January, 1, 1),
		pt(2026, time.January, 5, 2),
		pt(2026, time.January, 9, 3),
	}
	from := day(2026, time.January, 3)
	got := s.slice(&from, day(2026, time.January, 9))
	if !equalStrings(days(got), []string{"2026-01-05", "2026-01-09"}) {
		t.Fatalf("days = %v", days(got))
	}
	// A nil `from` means the whole history, not "today".
	if all := s.slice(nil, day(2026, time.January, 9)); len(all) != 3 {
		t.Fatalf("nil from kept %d points, want 3", len(all))
	}
	// The upper bound includes the day containing an intraday instant.
	if inc := s.slice(nil, time.Date(2026, time.January, 9, 13, 0, 0, 0, time.UTC)); len(inc) != 3 {
		t.Fatalf("the day containing `to` must be kept, got %d points", len(inc))
	}
}

// --- statistics -------------------------------------------------------------------------

func TestTotalReturnPct(t *testing.T) {
	s := dailySeries{pt(2026, time.January, 1, 100), pt(2026, time.January, 2, 190)}
	if got := mustFloat(t, totalReturnPct(s), "return"); math.Abs(got-90) > 1e-9 {
		t.Fatalf("return = %v, want 90", got)
	}
	if totalReturnPct(dailySeries{pt(2026, time.January, 1, 100)}) != nil {
		t.Error("one point cannot express a return; it must be null")
	}
	if totalReturnPct(nil) != nil {
		t.Error("an empty series must be null")
	}
	if totalReturnPct(dailySeries{pt(2026, time.January, 1, 0), pt(2026, time.January, 2, 5)}) != nil {
		t.Error("a zero base must be null, not an infinite return")
	}
}

func TestObservationVolatilityIsNotAnnualised(t *testing.T) {
	// Two log returns, +ln2 and -ln2. Their sample standard deviation is
	// |a-b|/sqrt(2) = sqrt(2)*ln2 = 0.980258..., i.e. 98.0258% per DAY.
	s := dailySeries{
		pt(2026, time.January, 1, 100),
		pt(2026, time.January, 2, 200),
		pt(2026, time.January, 3, 100),
	}
	got := mustFloat(t, observationVolatilityPct(s), "observation volatility")
	want := math.Sqrt(2) * math.Log(2) * 100
	if math.Abs(got-want) > 1e-4 {
		t.Fatalf("observation volatility = %v, want %v", got, want)
	}
	// The specific failure this guards: multiplying by sqrt(252) would report
	// roughly 1556%. These symbols keep three different calendars and no single
	// sqrt(N) is right for any of them.
	if math.Abs(got-want*math.Sqrt(252)) < 1 {
		t.Fatal("the figure looks annualised")
	}
}

func TestObservationVolatilityNeedsTwoReturns(t *testing.T) {
	// One return carries no information about dispersion at all. A sample
	// standard deviation of it is 0/0, and reporting 0.0 would read as "this
	// asset did not move".
	one := dailySeries{pt(2026, time.January, 1, 100), pt(2026, time.January, 2, 150)}
	if observationVolatilityPct(one) != nil {
		t.Error("a single return must yield null, never 0.0")
	}
	if observationVolatilityPct(nil) != nil {
		t.Error("an empty series must yield null")
	}
}

func TestObservationVolatilitySkipsNonPositiveCloses(t *testing.T) {
	s := dailySeries{
		pt(2026, time.January, 1, 100),
		pt(2026, time.January, 2, 0),
		pt(2026, time.January, 3, 100),
		pt(2026, time.January, 4, 200),
		pt(2026, time.January, 5, 100),
	}
	if v := observationVolatilityPct(s); v == nil || math.IsNaN(*v) {
		t.Fatalf("a zero close must be skipped, not poison the result: %v", v)
	}
}

func TestNonAdjacentSteps(t *testing.T) {
	s := dailySeries{
		pt(2026, time.January, 1, 1),
		pt(2026, time.January, 2, 1), // adjacent
		pt(2026, time.January, 5, 1), // three-day hole
		pt(2026, time.January, 6, 1), // adjacent
	}
	if got := nonAdjacentSteps(s); got != 1 {
		t.Fatalf("non-adjacent steps = %d, want 1", got)
	}
}

func TestMaxDrawdownIsPeakToTroughNotCurrent(t *testing.T) {
	// Rises to 200, collapses to 100 (-50% from the peak), recovers to 250.
	// The CURRENT drawdown is 0; the MAX drawdown is -50.
	s := dailySeries{
		pt(2026, time.January, 1, 100),
		pt(2026, time.January, 2, 200),
		pt(2026, time.January, 3, 100),
		pt(2026, time.January, 4, 250),
	}
	got := mustFloat(t, maxDrawdownPct(s), "max drawdown")
	if math.Abs(got-(-50)) > 1e-9 {
		t.Fatalf("max drawdown = %v, want -50", got)
	}
}

func TestMaxDrawdownOfAMonotoneRiseIsZero(t *testing.T) {
	s := dailySeries{
		pt(2026, time.January, 1, 100),
		pt(2026, time.January, 2, 110),
		pt(2026, time.January, 3, 120),
	}
	got := mustFloat(t, maxDrawdownPct(s), "max drawdown")
	if got != 0 {
		t.Fatalf("max drawdown = %v, want 0", got)
	}
}

func TestMaxDrawdownNeedsTwoPoints(t *testing.T) {
	if maxDrawdownPct(dailySeries{pt(2026, time.January, 1, 100)}) != nil {
		t.Error("one point has no drawdown; it must be null")
	}
	if maxDrawdownPct(nil) != nil {
		t.Error("an empty series must be null")
	}
}

// --- the gap formula ---------------------------------------------------------------------

func TestGapPct(t *testing.T) {
	cases := []struct {
		name           string
		aGrowth, bGrow float64
		want           float64
	}{
		// The contract's own worked example: +620% against +780%.
		{"a lags b", 6.20, 7.80, (7.20/8.80 - 1) * 100},
		{"equal growth is no gap", 0.5, 0.5, 0},
		{"a leads b", 1.0, 0.0, 100},
		{"b leads a", 0.0, 1.0, -50},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := gapPct(tc.aGrowth, tc.bGrow)
			if math.Abs(got-tc.want) > 1e-9 {
				t.Fatalf("gap = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestGapPctIsTheRatioOfIndexedSeries(t *testing.T) {
	// Indexing both legs to 100 and taking the ratio must give the same answer
	// as the growth formula -- that identity is why the formula is defensible.
	aBase, aEnd := 100.0, 620.0
	bBase, bEnd := 50.0, 200.0
	fromGrowth := gapPct(aEnd/aBase-1, bEnd/bBase-1)
	fromIndex := ((aEnd/aBase*100)/(bEnd/bBase*100) - 1) * 100
	if math.Abs(fromGrowth-fromIndex) > 1e-9 {
		t.Fatalf("growth form %v and indexed form %v disagree", fromGrowth, fromIndex)
	}
}

// --- day arithmetic -------------------------------------------------------------------------

func TestFloorDayIgnoresTheDisplayTimezone(t *testing.T) {
	tehran := time.FixedZone("Asia/Tehran", 3*3600+1800)
	// 2026-01-02 02:00 Tehran is 2026-01-01 22:30 UTC and belongs to the 1st.
	local := time.Date(2026, time.January, 2, 2, 0, 0, 0, tehran)
	if got := floorDay(local); !got.Equal(day(2026, time.January, 1)) {
		t.Fatalf("floorDay = %v, want the UTC day 2026-01-01", got)
	}
}

func TestDaysBetween(t *testing.T) {
	if got := daysBetween(day(2026, time.January, 1), day(2026, time.January, 31)); got != 30 {
		t.Fatalf("daysBetween = %d, want 30", got)
	}
	if got := daysBetween(day(2024, time.February, 28), day(2024, time.March, 1)); got != 2 {
		t.Fatalf("daysBetween across the leap day = %d, want 2", got)
	}
}

func TestFpWithholdsNonFiniteNumbers(t *testing.T) {
	// encoding/json refuses to encode a NaN and httpserver.JSON discards the
	// encoder's error, so a NaN reaching a payload truncates the body.
	if fp(math.NaN()) != nil || fp(math.Inf(1)) != nil || fp(math.Inf(-1)) != nil {
		t.Fatal("a non-finite value must become null, never reach the payload")
	}
}
