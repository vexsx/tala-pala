package relvalue

// The real return, and the two things it must never do: extrapolate an annual
// index past its coverage, and interpolate it onto days.

import (
	"math"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
)

// annualCPI builds the deflator table from (year, index level) pairs.
func annualCPI(pairs ...[2]float64) cpiTable {
	obs := make([]economic.Observation, 0, len(pairs))
	for _, p := range pairs {
		year := int(p[0])
		obs = append(obs, economic.Observation{
			RefPeriodStart: day(year, time.January, 1),
			RefPeriodEnd:   day(year, time.December, 31),
			RefPeriodLabel: dayString(day(year, time.January, 1))[:4],
			Value:          p[1],
			Vintage:        1,
		})
	}
	return buildCPITable(obs)
}

func TestBuildCPITable_DropsNonPositiveAndKeepsHoles(t *testing.T) {
	got := annualCPI([2]float64{2020, 100}, [2]float64{2021, 0}, [2]float64{2023, 150})
	if len(got) != 2 {
		t.Fatalf("table has %d rows, want 2 (the zero is dropped)", len(got))
	}
	if got[0].Year != 2020 || got[1].Year != 2023 {
		t.Fatalf("years = %d, %d -- a missing year must stay missing, never be filled",
			got[0].Year, got[1].Year)
	}
	if !got[1].Start.Equal(day(2023, time.January, 1)) {
		t.Fatalf("year start = %v, want 2023-01-01", got[1].Start)
	}
}

func TestCPICoverageTo(t *testing.T) {
	if cpiTable(nil).coverageTo() != nil {
		t.Error("an empty table has no coverage; it must be null")
	}
	got := annualCPI([2]float64{2024, 100}, [2]float64{2025, 120}).coverageTo()
	if got == nil || !got.Equal(day(2025, time.January, 1)) {
		t.Fatalf("coverage_to = %v, want 2025-01-01", got)
	}
}

// --- the central case: a window that outruns the CPI ----------------------------

func TestRealReturn_TruncatesToTheCPICoveredSubWindow(t *testing.T) {
	// A three-year window ending "today" (2026), against a World Bank series
	// that ends with 2025. The window CANNOT be deflated to today, so the
	// answer is the largest CPI-covered sub-window, reported as its own dates.
	s := dailySeries{
		pt(2023, time.September, 9, 50),
		pt(2024, time.January, 1, 100),
		pt(2025, time.January, 1, 200),
		pt(2026, time.September, 9, 400),
	}
	cpi := annualCPI([2]float64{2023, 80}, [2]float64{2024, 100}, [2]float64{2025, 150})

	got := realReturn(s, cpi, day(2023, time.September, 9), day(2026, time.September, 9))

	// Sub-window 2024-01-01 -> 2025-01-01: asset doubles, prices rise 50%.
	// (1 + 1.0) / 1.5 - 1 = 0.3333...
	want := (2.0/1.5 - 1) * 100
	if math.Abs(mustFloat(t, got.Pct, "real return")-want) > 1e-6 {
		t.Fatalf("real return = %v, want %v", *got.Pct, want)
	}
	if got.From == nil || !got.From.Equal(day(2024, time.January, 1)) {
		t.Fatalf("real_return_from = %v, want 2024-01-01", got.From)
	}
	if got.To == nil || !got.To.Equal(day(2025, time.January, 1)) {
		t.Fatalf("real_return_to = %v, want 2025-01-01", got.To)
	}
	// The nominal window ran to 2026-09-09. The real one must not, and the note
	// must say so rather than leaving the reader to notice the dates.
	if got.To.After(day(2025, time.January, 1)) {
		t.Fatal("the real window ran past CPI coverage -- that is extrapolation")
	}
	for _, want := range []string{
		"NOT the nominal window", "coverage ends with 2025", "never interpolated"} {
		if !strings.Contains(got.Note, want) {
			t.Errorf("note does not contain %q:\n%s", want, got.Note)
		}
	}
}

func TestRealReturn_NeverExtrapolatesPastCoverage(t *testing.T) {
	// However far the window runs, y1 can never exceed the last covered year.
	s := dailySeries{
		pt(2020, time.January, 1, 100),
		pt(2021, time.January, 1, 100),
		pt(2022, time.January, 1, 100),
		pt(2026, time.September, 9, 100),
	}
	cpi := annualCPI([2]float64{2020, 100}, [2]float64{2021, 110}, [2]float64{2022, 120})
	got := realReturn(s, cpi, day(2020, time.January, 1), day(2026, time.September, 9))
	if got.To == nil || !got.To.Equal(day(2022, time.January, 1)) {
		t.Fatalf("real_return_to = %v, want the last covered year 2022-01-01", got.To)
	}
	// Flat asset, +20% prices: the real return is 1/1.2 - 1.
	want := (1/1.2 - 1) * 100
	if math.Abs(mustFloat(t, got.Pct, "real return")-want) > 1e-6 {
		t.Fatalf("real return = %v, want %v", *got.Pct, want)
	}
}

func TestRealReturn_WithholdsWhenTheWindowSpansNoTwoCoveredYears(t *testing.T) {
	// The exact shape of a 1y window ending today while CPI stops at last year:
	// there is only ONE covered calendar-year start inside it, so no annual
	// ratio can be formed. Interpolating the index onto days to rescue a number
	// would manufacture an inflation path nobody measured.
	s := dailySeries{
		pt(2025, time.September, 9, 100),
		pt(2026, time.September, 9, 200),
	}
	cpi := annualCPI([2]float64{2024, 100}, [2]float64{2025, 150})

	got := realReturn(s, cpi, day(2025, time.September, 9), day(2026, time.September, 9))
	if got.Pct != nil {
		t.Fatalf("real return = %v, want null -- there is no deflator for this window", *got.Pct)
	}
	if got.From != nil || got.To != nil {
		t.Error("a withheld real return must not report a window it did not measure")
	}
	for _, want := range []string{"ANNUAL", "NOT interpolated onto days"} {
		if !strings.Contains(got.Note, want) {
			t.Errorf("note does not contain %q:\n%s", want, got.Note)
		}
	}
}

func TestRealReturn_WithholdsWithoutADeflator(t *testing.T) {
	s := dailySeries{pt(2024, time.January, 1, 100), pt(2025, time.January, 1, 200)}
	got := realReturn(s, nil, day(2024, time.January, 1), day(2025, time.January, 1))
	if got.Pct != nil {
		t.Fatal("no CPI must mean no real return")
	}
	if !strings.Contains(got.Note, cpiSeriesCode) {
		t.Errorf("note does not name the missing series:\n%s", got.Note)
	}
}

func TestRealReturn_RefusesAYearItCannotAnchor(t *testing.T) {
	// CPI covers 2020 and 2025, but every stored observation falls in June and
	// July of 2025. Neither covered year has a January observation to stand for
	// its start, so no annual ratio can be paired with an asset leg -- and a
	// mid-year leg matched to a full year-to-year deflator would report seven
	// months of return against twelve months of inflation.
	s := dailySeries{pt(2025, time.June, 1, 100), pt(2025, time.July, 1, 200)}
	cpi := annualCPI([2]float64{2020, 100}, [2]float64{2025, 500})
	got := realReturn(s, cpi, day(2020, time.January, 1), day(2025, time.December, 31))
	if got.Pct != nil {
		t.Fatalf("real return = %v, want null", *got.Pct)
	}
	if !strings.Contains(got.Note, "January observation") {
		t.Errorf("note does not explain the missing anchor:\n%s", got.Note)
	}
}

func TestRealReturn_RefusesAWindowShorterThanTheDeflator(t *testing.T) {
	// A three-month window inside one calendar year. An ANNUAL index has
	// nothing to say about it, and the only way to produce a number would be to
	// interpolate the index onto days.
	s := dailySeries{pt(2025, time.January, 2, 100), pt(2025, time.March, 31, 130)}
	cpi := annualCPI([2]float64{2024, 100}, [2]float64{2025, 150})
	got := realReturn(s, cpi, day(2025, time.January, 1), day(2025, time.March, 31))
	if got.Pct != nil {
		t.Fatalf("real return = %v, want null for a sub-annual window", *got.Pct)
	}
	if !strings.Contains(got.Note, "NOT interpolated onto days") {
		t.Errorf("note does not state the refusal:\n%s", got.Note)
	}
}

func TestRealReturn_UsesRealObservationDatesNotNewYearsDay(t *testing.T) {
	// The market did not quote on 1 January. Nothing is carried onto that day:
	// the legs are the FIRST observation at or after it and the LAST at or
	// before the closing year's start, and those actual dates are reported.
	s := dailySeries{
		pt(2024, time.January, 3, 100), // the market's first quote of 2024
		pt(2024, time.December, 28, 300),
		pt(2025, time.January, 5, 800), // and of 2025
		pt(2025, time.March, 1, 900),
	}
	cpi := annualCPI([2]float64{2024, 100}, [2]float64{2025, 200})
	got := realReturn(s, cpi, day(2024, time.January, 1), day(2025, time.March, 1))
	if got.From == nil || !got.From.Equal(day(2024, time.January, 3)) {
		t.Fatalf("real_return_from = %v, want the first REAL observation 2024-01-03", got.From)
	}
	if got.To == nil || !got.To.Equal(day(2025, time.January, 5)) {
		t.Fatalf("real_return_to = %v, want the first REAL observation of 2025", got.To)
	}
	// 100 -> 800 nominal against a doubling of prices: (1+7)/2 - 1 = 300%.
	if math.Abs(mustFloat(t, got.Pct, "real return")-300) > 1e-6 {
		t.Fatalf("real return = %v, want 300", *got.Pct)
	}
}

func TestRealReturn_NoteAlwaysExplainsItself(t *testing.T) {
	// Every outcome carries a note. A null with no explanation is the failure
	// mode this contract exists to prevent.
	cases := []realReturnResult{
		realReturn(dailySeries{pt(2024, time.January, 1, 1), pt(2025, time.January, 1, 2)},
			annualCPI([2]float64{2024, 100}, [2]float64{2025, 110}),
			day(2024, time.January, 1), day(2025, time.January, 1)),
		realReturn(nil, nil, day(2024, time.January, 1), day(2025, time.January, 1)),
		realReturn(dailySeries{pt(2026, time.January, 1, 1)}, annualCPI([2]float64{2025, 100}),
			day(2026, time.January, 1), day(2026, time.June, 1)),
		realReturn(dailySeries{pt(2025, time.June, 1, 1)}, annualCPI([2]float64{2024, 100}, [2]float64{2025, 120}),
			day(2025, time.January, 1), day(2025, time.December, 31)),
	}
	for i, c := range cases {
		if strings.TrimSpace(c.Note) == "" {
			t.Errorf("case %d produced no note", i)
		}
	}
}

// --- the note's claim about the two legs' lengths ------------------------------------

// januaryAnchor takes each year's FIRST January observation, which can be the
// 2nd in one year and the 29th in the next. Nothing checks the two offsets
// match, so the note's old claim that the legs were "the same length" was
// simply untrue whenever they were not. It now measures the difference.
func TestRealReturn_NoteDoesNotClaimEqualLegsWhenTheyDiffer(t *testing.T) {
	s := dailySeries{
		pt(2024, time.January, 2, 100),
		pt(2025, time.January, 29, 200),
	}
	cpi := annualCPI([2]float64{2024, 100}, [2]float64{2025, 125})
	got := realReturn(s, cpi, day(2024, time.January, 1), day(2025, time.February, 1))

	if got.Pct == nil {
		t.Fatal("two anchors are present; the real return should be computed")
	}
	if strings.Contains(got.Note, "the same length as the deflator") {
		t.Fatalf("the legs are 27 days apart in length; the note must not claim "+
			"otherwise:\n%s", got.Note)
	}
	for _, want := range []string{"NOT THE SAME LENGTH", "27 day(s)"} {
		if !strings.Contains(got.Note, want) {
			t.Errorf("note does not contain %q:\n%s", want, got.Note)
		}
	}
}

func TestRealReturn_NoteMayClaimEqualLegsWhenTheyAre(t *testing.T) {
	s := dailySeries{
		pt(2024, time.January, 3, 100),
		pt(2025, time.January, 3, 200),
	}
	cpi := annualCPI([2]float64{2024, 100}, [2]float64{2025, 125})
	got := realReturn(s, cpi, day(2024, time.January, 1), day(2025, time.February, 1))
	if !strings.Contains(got.Note, "the same length as the deflator") {
		t.Fatalf("both anchors sit on 3 January, so the claim is true and should be "+
			"made:\n%s", got.Note)
	}
	if strings.Contains(got.Note, "NOT THE SAME LENGTH") {
		t.Errorf("the legs are the same length here:\n%s", got.Note)
	}
}
