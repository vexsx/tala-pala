package relvalue

// The monthly deflator. The point of these tests is the CONTRAST with the
// annual one: the same window, the same prices, answered by one and refused by
// the other — and refused correctly, not from a bug.

import (
	"math"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
)

// jalaliMonths builds a monthly table whose periods have the SHAPE SCI
// actually publishes: Jalali months, so ~30 days each, starting in the middle
// of a Gregorian month and never on its first.
//
// Starting at 2026-01-21 and stepping 30 days reproduces the real boundary
// pattern seen in production (…2026-06-22..2026-07-22, 2026-07-23..2026-08-22),
// which is the property that makes Gregorian month arithmetic wrong here.
func jalaliMonths(start time.Time, levels ...float64) monthlyTable {
	obs := make([]economic.Observation, 0, len(levels))
	s := start
	for i, v := range levels {
		end := s.AddDate(0, 0, 29)
		obs = append(obs, economic.Observation{
			RefPeriodStart: s,
			RefPeriodEnd:   end,
			RefPeriodLabel: labelFor(i),
			Value:          v,
			Vintage:        1,
		})
		s = end.AddDate(0, 0, 1)
	}
	return buildMonthlyCPITable(obs)
}

func labelFor(i int) string {
	return "1405-" + string(rune('0'+(i+1)/10)) + string(rune('0'+(i+1)%10))
}

// dailyFrom builds a daily series of `n` days from `start`, compounding `daily`
// each day, so a known total return can be asserted.
func dailyFrom(start time.Time, n int, first, daily float64) dailySeries {
	out := make(dailySeries, 0, n)
	v := first
	for i := 0; i < n; i++ {
		out = append(out, dailyPoint{Day: start.AddDate(0, 0, i), Close: v})
		v *= daily
	}
	return out
}

func TestBuildMonthlyCPITable_DropsNonPositiveAndKeepsHoles(t *testing.T) {
	got := jalaliMonths(day(2026, time.January, 21), 100, 0, 150)
	if len(got) != 2 {
		t.Fatalf("table has %d rows, want 2 (the zero is dropped)", len(got))
	}
	// The hole must stay a hole: the surviving rows keep their own periods and
	// nothing is invented to bridge them.
	if !got[1].Start.Equal(day(2026, time.March, 22)) {
		t.Fatalf("second surviving period starts %v, want 2026-03-22 — a dropped month "+
			"must not shift the ones after it", got[1].Start)
	}
}

// THE HEADLINE. A one-year window is exactly what the annual deflator cannot
// answer and the monthly one can. Both are run over the same prices so the
// difference is the deflator and nothing else.
func TestAOneYearWindowIsRefusedAnnuallyAndAnsweredMonthly(t *testing.T) {
	start := day(2025, time.January, 21)
	// 13 monthly periods ≈ 390 days, prices flat at 100 then a step: an asset
	// that exactly doubles while prices exactly double is 0% real.
	levels := make([]float64, 13)
	for i := range levels {
		levels[i] = 100 * math.Pow(2, float64(i)/12)
	}
	monthly := jalaliMonths(start, levels...)
	series := dailyFrom(start, 400, 1000, math.Pow(2, 1.0/365))

	from, to := start, start.AddDate(0, 0, 399)

	// Annual: the World Bank table has one covered year inside this window, so
	// it cannot form a ratio. That is correct behaviour, not a failure.
	annual := annualCPI([2]float64{2025, 100})
	ann := realReturnMonthly(series, monthlyTable{}, from, to)
	if ann.Pct != nil {
		t.Error("an empty monthly table must not produce a real return")
	}
	if got := realReturn(series, annual, from, to); got.Pct != nil {
		t.Errorf("the annual deflator must refuse a single covered year, got %v", *got.Pct)
	}

	// Monthly: twelve months of coverage inside the same window.
	mon := realReturnMonthly(series, monthly, from, to)
	if mon.Pct == nil {
		t.Fatalf("the monthly deflator must answer a 1-year window: %s", mon.Note)
	}
	// Asset and prices both roughly double over the covered span, so the real
	// return is near zero. The bound is loose because the anchors land on the
	// periods' first days rather than on exact anniversaries.
	if math.Abs(*mon.Pct) > 3 {
		t.Errorf("real return = %.3f%%, want ~0%% when the asset tracks the index: %s",
			*mon.Pct, mon.Note)
	}
	if mon.From == nil || mon.To == nil {
		t.Fatal("an answered real return must report the window it used")
	}
}

func TestMonthlyRealReturnArithmetic(t *testing.T) {
	start := day(2026, time.January, 21)
	// Index 100 -> 110 (10% inflation). Asset 1000 -> 1320 (32% nominal).
	// Real = 1.32/1.10 - 1 = 20%.
	monthly := jalaliMonths(start, 100, 110)
	series := dailySeries{
		{Day: start, Close: 1000},
		{Day: start.AddDate(0, 0, 30), Close: 1320},
	}
	got := realReturnMonthly(series, monthly, start, start.AddDate(0, 0, 35))
	if got.Pct == nil {
		t.Fatalf("no real return: %s", got.Note)
	}
	if math.Abs(*got.Pct-20) > 5e-7 {
		t.Errorf("real return = %.9f%%, want 20%% (within fp()'s six-decimal rounding)",
			*got.Pct)
	}
	if !strings.Contains(got.Note, sciCPISeriesCode) {
		t.Errorf("the note must name the deflator it used: %s", got.Note)
	}
	if !strings.Contains(got.Note, "MONTHLY") {
		t.Errorf("the note must say the deflator is monthly, so a reader knows the "+
			"resolution of the claim: %s", got.Note)
	}
}

func TestMonthlyDeflatorRefusesFewerThanTwoAnchorsAndSaysWhy(t *testing.T) {
	start := day(2026, time.January, 21)
	monthly := jalaliMonths(start, 100, 110, 120)
	// One observation only: an anchor cannot be formed for a second period.
	series := dailySeries{{Day: start, Close: 1000}}

	got := realReturnMonthly(series, monthly, start, start.AddDate(0, 0, 89))
	if got.Pct != nil {
		t.Fatalf("one anchor must not produce a real return, got %v", *got.Pct)
	}
	for _, want := range []string{"NOT interpolated", "MONTHLY"} {
		if !strings.Contains(got.Note, want) {
			t.Errorf("the refusal must contain %q so the reader knows it is a refusal "+
				"and not a gap: %s", want, got.Note)
		}
	}
}

// A missing month is never bridged. With a hole in the middle the deflator
// still answers — from the months it HAS — and the ratio must be the one that
// skips the hole, not an interpolation across it.
func TestAMissingMonthIsSkippedNeverInterpolated(t *testing.T) {
	start := day(2026, time.January, 21)
	monthly := jalaliMonths(start, 100, 0, 121) // middle month dropped
	series := dailyFrom(start, 95, 1000, 1.0)   // flat asset

	got := realReturnMonthly(series, monthly, start, start.AddDate(0, 0, 94))
	if got.Pct == nil {
		t.Fatalf("two surviving months must still deflate: %s", got.Note)
	}
	// Flat asset, index 100 -> 121, so real = 1/1.21 - 1 = -17.3554%.
	// The tolerance is 5e-7 and not tighter because fp() rounds every derived
	// number in this package to six decimals before it leaves.
	want := (1/1.21 - 1) * 100
	if math.Abs(*got.Pct-want) > 5e-7 {
		t.Errorf("real return = %.9f%%, want %.9f%% (the ratio must skip the hole, "+
			"not interpolate across it)", *got.Pct, want)
	}
	// The span is TWO months even though only two anchors formed: the dropped
	// month sits between them and the deflator ratio really does cover it.
	// The deflator spans 1405-01 to 1405-03 — 60 days — even though only two
	// rows survive: the dropped month leaves no row, so counting rows would
	// report half the true span. Days between period starts cannot be fooled
	// that way.
	if !strings.Contains(got.Note, "1405-01 to 1405-03, 60 days") {
		t.Errorf("the span must be measured across the hole, not between surviving "+
			"rows: %s", got.Note)
	}
}

// The anchors are chosen inside each period's own Gregorian span. A period
// beginning 2026-07-23 must not be anchored by Gregorian month arithmetic,
// which would look at 2026-07-01 and find a price from the PREVIOUS period.
func TestAnchorsRespectJalaliBoundariesNotGregorianOnes(t *testing.T) {
	p := cpiMonth{
		Start: day(2026, time.July, 23),
		End:   day(2026, time.August, 22),
		Label: "1405-05", Value: 683.59, Vintage: 1,
	}
	series := dailySeries{
		{Day: day(2026, time.July, 1), Close: 111},  // inside Gregorian July, BEFORE the period
		{Day: day(2026, time.July, 25), Close: 222}, // the first point actually inside it
	}
	got, off, ok := periodAnchor(series, p)
	if !ok {
		t.Fatal("the period contains an observation, so it must anchor")
	}
	if got.Close != 222 {
		t.Errorf("anchored on close %v; a price from before the reference period "+
			"must never stand for it", got.Close)
	}
	if off != 2 {
		t.Errorf("offset = %d days, want 2 (2026-07-25 is 2 days into the period)", off)
	}
}

// When the two anchors sit at different offsets the legs are NOT the same
// length, and the note must say so rather than quietly assert alignment.
func TestUnequalAnchorOffsetsAreReportedNotAssertedAway(t *testing.T) {
	start := day(2026, time.January, 21)
	monthly := jalaliMonths(start, 100, 110)
	series := dailySeries{
		{Day: start, Close: 1000},                   // offset 0
		{Day: start.AddDate(0, 0, 34), Close: 1320}, // offset 4 into the second period
	}
	got := realReturnMonthly(series, monthly, start, start.AddDate(0, 0, 40))
	if got.Pct == nil {
		t.Fatalf("no real return: %s", got.Note)
	}
	// The exact mismatch, in days, not a vague admission of one.
	for _, want := range []string{"0 and 4 day(s)", "+4 day(s)", "no inflation behind them"} {
		if !strings.Contains(got.Note, want) {
			t.Errorf("a 4-day leg mismatch must be reported exactly, missing %q: %s",
				want, got.Note)
		}
	}
}

func TestMonthlyCoverageTo(t *testing.T) {
	if monthlyTable(nil).coverageTo() != nil {
		t.Error("an empty table has no coverage")
	}
	m := jalaliMonths(day(2026, time.January, 21), 100, 110, 120)
	got := m.coverageTo()
	if got == nil || !got.Equal(day(2026, time.March, 22)) {
		t.Errorf("coverageTo = %v, want the START of the newest period (2026-03-22)", got)
	}
}

// --- the selector -----------------------------------------------------------

// The monthly index wins wherever it reaches. This is the case that covers
// every asset in `prices`, all of which begin in 2010 or later.
func TestDeflatePrefersTheMonthlyIndex(t *testing.T) {
	start := day(2025, time.January, 21)
	levels := make([]float64, 14)
	for i := range levels {
		levels[i] = 100 * math.Pow(1.5, float64(i)/12)
	}
	in := performanceInputs{
		CPIMonthly: jalaliMonths(start, levels...),
		// An annual table that also covers the window, so the choice is a
		// preference and not the only option available.
		CPI: annualCPI([2]float64{2025, 100}, [2]float64{2026, 150}),
	}
	series := dailyFrom(start, 420, 1000, math.Pow(1.5, 1.0/365))

	got, code := in.deflate(series, start, start.AddDate(0, 0, 419))
	if got.Pct == nil {
		t.Fatalf("the monthly index covers this window: %s", got.Note)
	}
	if code != sciCPISeriesCode {
		t.Errorf("deflator = %q, want %q — the monthly index must be preferred "+
			"wherever it reaches", code, sciCPISeriesCode)
	}
	if strings.Contains(got.Note, "rather than the monthly") {
		t.Errorf("a monthly answer must not carry the fallback preamble: %s", got.Note)
	}
}

// A window that begins before SCI starts falls back to the annual index, and
// the note must say that is what happened — not silently produce a coarser
// number that looks identical to a fine one.
func TestDeflateFallsBackToAnnualBeforeSciStarts(t *testing.T) {
	// The monthly table covers 2025 only; the window is 2019..2023.
	in := performanceInputs{
		CPIMonthly: jalaliMonths(day(2025, time.January, 21), 100, 110),
		CPI: annualCPI(
			[2]float64{2019, 100}, [2]float64{2021, 140}, [2]float64{2023, 200}),
	}
	series := dailySeries{}
	for y := 2019; y <= 2023; y++ {
		series = append(series, dailyPoint{
			Day:   day(y, time.January, 3),
			Close: 1000 * math.Pow(1.3, float64(y-2019)),
		})
	}

	got, code := in.deflate(series, day(2019, time.January, 1), day(2023, time.December, 31))
	if got.Pct == nil {
		t.Fatalf("the annual index covers this window: %s", got.Note)
	}
	if code != cpiSeriesCode {
		t.Errorf("deflator = %q, want %q for a pre-2002-style window", code, cpiSeriesCode)
	}
	for _, want := range []string{"ANNUAL", "rather than the monthly"} {
		if !strings.Contains(got.Note, want) {
			t.Errorf("a fallback must announce itself, missing %q: %s", want, got.Note)
		}
	}
}

// When neither index can answer, the reported reason is the MONTHLY one --
// the preferred deflator's -- and the note shows the fallback was tried.
func TestDeflateReportsBothRefusals(t *testing.T) {
	in := performanceInputs{
		CPIMonthly: jalaliMonths(day(2026, time.January, 21), 100, 110),
		CPI:        annualCPI([2]float64{2026, 100}),
	}
	// A series with one observation: no second anchor for either deflator.
	series := dailySeries{{Day: day(2026, time.January, 21), Close: 1000}}

	got, code := in.deflate(series, day(2026, time.January, 1), day(2026, time.February, 1))
	if got.Pct != nil {
		t.Fatalf("neither index can deflate a single observation, got %v", *got.Pct)
	}
	if code != "" {
		t.Errorf("no deflator produced a number, so none may be named; got %q", code)
	}
	if !strings.Contains(got.Note, sciCPISeriesCode) {
		t.Errorf("the preferred deflator's refusal must lead: %s", got.Note)
	}
	if !strings.Contains(got.Note, "also declined") {
		t.Errorf("the note must show the annual fallback was tried, not forgotten: %s",
			got.Note)
	}
}
