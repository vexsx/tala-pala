package relvalue

// Tests for the exported door onto the numeraire engine.
//
// What they are really guarding is that an EXTERNAL caller -- internal/equities
// screening the Tehran roster -- gets exactly the rules this package applies to
// itself, and cannot get a softer version of them by coming in through
// Converter. So every one of the four rules is asserted here again, on the
// exported surface, rather than assumed to be inherited.

import (
	"math"
	"testing"
	"time"
)

// usdQuotes is a deliberately holey USD_IRT history: it starts LATE (so days
// before it must be dropped, not back-filled) and skips days in the middle (so
// they must be carried forward, not interpolated).
func usdQuotes() []Point {
	return []Point{
		{Day: day(2026, time.March, 2), Value: 900_000},
		{Day: day(2026, time.March, 3), Value: 1_000_000},
		// 2026-03-04 and 2026-03-05 are missing on purpose.
		{Day: day(2026, time.March, 6), Value: 1_100_000},
	}
}

func goldQuotes() []Point {
	return []Point{
		{Day: day(2026, time.March, 2), Value: 8_000_000},
		{Day: day(2026, time.March, 6), Value: 8_400_000},
	}
}

func testConverter() *Converter {
	return NewConverterFromQuotes(day(2026, time.March, 10), map[string][]Point{
		SeriesUSD:     usdQuotes(),
		SeriesGold18K: goldQuotes(),
	})
}

func TestConvertTomanUsesTheQuoteOfTheSameDay(t *testing.T) {
	c := testConverter()
	got := c.ConvertToman(day(2026, time.March, 3), 2_000_000, NumeraireUSD)
	if got.Value == nil {
		t.Fatalf("expected a value, got reason %q", got.Reason)
	}
	if *got.Value != 2.0 {
		t.Fatalf("2,000,000 toman at 1,000,000/USD = 2 USD, got %v", *got.Value)
	}
	if got.CarriedForward {
		t.Error("2026-03-03 has its own quote; nothing should be carried forward")
	}
	if got.RateDay == nil || !got.RateDay.Equal(day(2026, time.March, 3)) {
		t.Errorf("rate day = %v, want 2026-03-03", got.RateDay)
	}
	if got.Unit != "USD" {
		t.Errorf("unit = %q, want USD", got.Unit)
	}
}

func TestConvertTomanCarriesForwardAndSaysSo(t *testing.T) {
	// 2026-03-05 has no USD_IRT quote. The rule is the last close AT OR BEFORE
	// the day, so the 03-03 rate applies -- and the caller is told, because a
	// figure resting on a two-day-old rate is a different kind of figure.
	c := testConverter()
	got := c.ConvertToman(day(2026, time.March, 5), 2_000_000, NumeraireUSD)
	if got.Value == nil {
		t.Fatalf("expected a value, got reason %q", got.Reason)
	}
	if *got.Value != 2.0 {
		t.Fatalf("expected the 03-03 rate to be carried forward, got %v", *got.Value)
	}
	if !got.CarriedForward {
		t.Error("CarriedForward must be true when the quote comes from an earlier day")
	}
	if got.RateDay == nil || !got.RateDay.Equal(day(2026, time.March, 3)) {
		t.Errorf("rate day = %v, want 2026-03-03", got.RateDay)
	}
}

func TestConvertTomanNeverReachesBackwardForALaterQuote(t *testing.T) {
	// THE rule this package exists to hold. 2026-03-01 is before USD_IRT's
	// first quote; the only value that could fill it is a LATER one, and using
	// it would price the day with information from its own future. So the
	// value is withheld, and the reason says exactly that.
	c := testConverter()
	got := c.ConvertToman(day(2026, time.March, 1), 2_000_000, NumeraireUSD)
	if got.Value != nil {
		t.Fatalf("a day before the numeraire's first quote must be null, got %v", *got.Value)
	}
	if got.Reason == "" {
		t.Fatal("a null value must carry a reason")
	}
	if got.RateDay != nil {
		t.Errorf("no quote was used, so RateDay must be nil, got %v", got.RateDay)
	}
	for _, want := range []string{"carried FORWARD only", "USD_IRT"} {
		if !contains(got.Reason, want) {
			t.Errorf("reason %q must mention %q", got.Reason, want)
		}
	}
}

func TestConvertTomanRefusesAnUnknownNumeraire(t *testing.T) {
	c := testConverter()
	got := c.ConvertToman(day(2026, time.March, 3), 1000, "BTC")
	if got.Value != nil {
		t.Fatal("an unknown unit of account must not produce a value")
	}
	if !contains(got.Reason, "BTC") {
		t.Errorf("reason %q must name the unknown key", got.Reason)
	}
}

func TestConvertTomanIntoTomanIsTheIdentity(t *testing.T) {
	// The hub converts a toman value by doing nothing to it, and needs no
	// backing series -- so it can never become unavailable.
	c := NewConverterFromQuotes(day(2026, time.March, 10), nil)
	got := c.ConvertToman(day(2026, time.March, 1), 12_345, NumeraireToman)
	if got.Value == nil || *got.Value != 12_345 {
		t.Fatalf("identity conversion must return the input, got %v (%q)", got.Value, got.Reason)
	}
	if len(got.Chain) != 0 {
		t.Errorf("the identity conversion has no chain, got %v", got.Chain)
	}
}

func TestConvertTomanReportsAMissingSeriesAsSuch(t *testing.T) {
	// "This deployment has never collected IR_GOLD_18K" is a different failure
	// from "that day has no quote", and the message must not blur them: the
	// first is fixed by an ingest, the second cannot be fixed at all.
	c := NewConverterFromQuotes(day(2026, time.March, 10), map[string][]Point{
		SeriesUSD: usdQuotes(),
	})
	got := c.ConvertToman(day(2026, time.March, 3), 8_000_000, NumeraireGold)
	if got.Value != nil {
		t.Fatal("a numeraire with no stored series must produce no value")
	}
	if !contains(got.Reason, "IR_GOLD_18K") || !contains(got.Reason, "no stored observation") {
		t.Errorf("reason %q must name the missing series", got.Reason)
	}
}

func TestConvertTomanSeriesDropsUncoveredDaysAndCountsThem(t *testing.T) {
	c := testConverter()
	in := []Point{
		{Day: day(2026, time.March, 1), Value: 1_000_000}, // before USD_IRT starts
		{Day: day(2026, time.March, 2), Value: 1_800_000},
		{Day: day(2026, time.March, 5), Value: 2_000_000}, // carried from 03-03
		{Day: day(2026, time.March, 6), Value: 2_200_000},
	}
	got := c.ConvertTomanSeries(in, NumeraireUSD)
	if len(got.Points) != 3 {
		t.Fatalf("expected 3 converted points, got %d", len(got.Points))
	}
	if got.DroppedNoPriorQuote != 1 {
		t.Errorf("DroppedNoPriorQuote = %d, want 1", got.DroppedNoPriorQuote)
	}
	if got.CarriedForward != 1 {
		t.Errorf("CarriedForward = %d, want 1 (only 03-05 uses an earlier quote)", got.CarriedForward)
	}
	// 1,800,000 / 900,000 = 2; 2,000,000 / 1,000,000 = 2; 2,200,000/1,100,000 = 2.
	// A flat series in dollars against a rising toman price is the whole point
	// of a numeraire, and it is asserted rather than described.
	for _, p := range got.Points {
		if math.Abs(p.Value-2.0) > 1e-9 {
			t.Errorf("%s converted to %v, want 2", p.Day.Format(dateLayout), p.Value)
		}
	}
	if len(got.Chain) != 1 || got.Chain[0] != SeriesUSD {
		t.Errorf("chain = %v, want [USD_IRT]", got.Chain)
	}
}

func TestConvertTomanSeriesSortsItsInputBeforeWalking(t *testing.T) {
	// applyStep is a merge walk that assumes both sides ascend. A caller
	// handing over an unsorted series must not silently receive a conversion
	// that used the wrong day's rate.
	c := testConverter()
	in := []Point{
		{Day: day(2026, time.March, 6), Value: 2_200_000},
		{Day: day(2026, time.March, 2), Value: 1_800_000},
	}
	got := c.ConvertTomanSeries(in, NumeraireUSD)
	if len(got.Points) != 2 {
		t.Fatalf("expected 2 points, got %d", len(got.Points))
	}
	if !got.Points[0].Day.Equal(day(2026, time.March, 2)) {
		t.Errorf("first point = %v, want 2026-03-02", got.Points[0].Day)
	}
	for _, p := range got.Points {
		if math.Abs(p.Value-2.0) > 1e-9 {
			t.Errorf("%s converted to %v, want 2", p.Day.Format(dateLayout), p.Value)
		}
	}
}

func TestQuoteAtOrBeforeAgreesWithApplyStep(t *testing.T) {
	// The pin. ConvertToman reports WHICH quote was used with a binary search,
	// while the value itself comes from applyStep's merge walk. Two spellings
	// of one rule is exactly the drift the export file exists to prevent, so
	// the two are asserted to agree on every day of a series with holes at both
	// ends and in the middle.
	num := toDailySeries(usdQuotes())
	asset := dailySeries{}
	for d := 1; d <= 8; d++ {
		asset = append(asset, pt(2026, time.March, d, 2_000_000))
	}
	stepped := applyStep(asset, num, opDivide)

	converted := map[string]float64{}
	for _, p := range stepped.Points {
		converted[p.Day.Format(dateLayout)] = p.Close
	}
	for _, a := range asset {
		q, found := num.quoteAtOrBefore(a.Day)
		key := a.Day.Format(dateLayout)
		value, kept := converted[key]
		if found != kept {
			t.Fatalf("%s: quoteAtOrBefore says found=%v but applyStep kept=%v", key, found, kept)
		}
		if !found {
			continue
		}
		if want := a.Close / q.Close; math.Abs(value-want) > 1e-9 {
			t.Errorf("%s: applyStep produced %v, quoteAtOrBefore implies %v", key, value, want)
		}
	}
}

func TestNumeraireInfoRefusesAnUnbackedUnit(t *testing.T) {
	c := NewConverterFromQuotes(day(2026, time.March, 10), map[string][]Point{
		SeriesUSD: usdQuotes(),
	})
	gold, ok := c.NumeraireInfo(NumeraireGold)
	if !ok {
		t.Fatal("GOLD is in the vocabulary and must be described even when unavailable")
	}
	if gold.Available {
		t.Error("GOLD has no stored series here and must not be offered")
	}
	if gold.UnavailableReason == nil || !contains(*gold.UnavailableReason, "IR_GOLD_18K") {
		t.Errorf("the reason must name the missing series, got %v", gold.UnavailableReason)
	}

	usd, _ := c.NumeraireInfo(NumeraireUSD)
	if !usd.Available {
		t.Error("USD is backed by a stored series here and must be available")
	}
	if usd.Observations != 3 {
		t.Errorf("observations = %d, want 3", usd.Observations)
	}
	if usd.CoverageFrom == nil || *usd.CoverageFrom != "2026-03-02" {
		t.Errorf("coverage_from = %v, want 2026-03-02", usd.CoverageFrom)
	}

	toman, _ := c.NumeraireInfo(NumeraireToman)
	if !toman.Available || toman.Series != nil {
		t.Error("the toman hub needs no backing series and can never be unavailable")
	}

	if _, ok := c.NumeraireInfo("BTC"); ok {
		t.Error("an unknown key must not be described")
	}
}

func TestFpUnitKeepsSmallPricesIntact(t *testing.T) {
	// fp's six decimals would round 0.0000625 grams to 0.000063 -- a 0.8%
	// error introduced by a display convention, in the column whose whole
	// purpose is to be small.
	got := fpUnit(0.0000625)
	if got == nil {
		t.Fatal("a finite value must survive")
	}
	if math.Abs(*got-0.0000625) > 1e-15 {
		t.Errorf("fpUnit(0.0000625) = %v, want 0.0000625", *got)
	}
	if fpUnit(math.NaN()) != nil || fpUnit(math.Inf(1)) != nil {
		t.Error("a non-finite value must be nil: encoding/json refuses to encode one")
	}
	if z := fpUnit(0); z == nil || *z != 0 {
		t.Errorf("fpUnit(0) = %v, want 0", z)
	}
}

func contains(haystack, needle string) bool {
	return len(needle) == 0 || (len(haystack) >= len(needle) && indexOf(haystack, needle) >= 0)
}

func indexOf(haystack, needle string) int {
	for i := 0; i+len(needle) <= len(haystack); i++ {
		if haystack[i:i+len(needle)] == needle {
			return i
		}
	}
	return -1
}
