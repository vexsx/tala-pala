package relvalue

// The basket. The assertions that matter here are about what this section
// REFUSES to say, because every failure mode is a plausible-looking number.

import (
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
)

// months builds a component series with SCI's real period shape.
func months(start time.Time, levels ...float64) []economic.Observation {
	out := make([]economic.Observation, 0, len(levels))
	s := start
	for i, v := range levels {
		end := s.AddDate(0, 0, 29)
		out = append(out, economic.Observation{
			RefPeriodStart: s, RefPeriodEnd: end,
			RefPeriodLabel: labelFor(i), Value: v, Vintage: 1,
		})
		s = end.AddDate(0, 0, 1)
	}
	return out
}

func basket(headline, shelter, vehicles []economic.Observation) map[string][]economic.Observation {
	m := map[string][]economic.Observation{}
	if headline != nil {
		m[sciCPISeriesCode] = headline
	}
	if shelter != nil {
		m[sciShelterSeriesCode] = shelter
	}
	if vehicles != nil {
		m[sciVehiclesSeriesCode] = vehicles
	}
	return m
}

func itemByCode(b *costOfLivingBlock, code string) *costOfLivingItem {
	for i := range b.Items {
		if b.Items[i].Code == code {
			return &b.Items[i]
		}
	}
	return nil
}

// Rent is the same series as housing on this data. Publishing both would
// present one fact as two and double its apparent weight.
func TestRentIsNeverPublishedAsItsOwnRow(t *testing.T) {
	start := day(2026, time.January, 21)
	in := basket(months(start, 100, 110), months(start, 100, 105), months(start, 100, 130))
	// Even if a caller somehow supplied rent, it must not become a row.
	in[sciRentSeriesCode] = months(start, 100, 105)

	got := buildCostOfLiving(in, start, start.AddDate(0, 0, 59))
	if got == nil {
		t.Fatal("two headline periods are enough to build the section")
	}
	if itemByCode(got, sciRentSeriesCode) != nil {
		t.Error("SCI_CPI_RENT must never be its own row: it is SCI_CPI_HOUSING twice " +
			"(r=0.999996 over 293 months), and two rows would double its weight")
	}
	if len(got.Items) != 2 {
		t.Errorf("want exactly shelter and vehicles, got %d rows", len(got.Items))
	}
	if !strings.Contains(got.Note, "NO house PRICE index") {
		t.Errorf("the section must state it cannot price a home: %s", got.Note)
	}
}

// Real growth against the headline is EXACT here -- same publisher, same
// periods, no anchoring.
func TestRealGrowthAgainstTheHeadlineIsExact(t *testing.T) {
	start := day(2026, time.January, 21)
	// Headline 100 -> 200 (doubling). Vehicles 100 -> 300 (tripling).
	// Real = 3.0/2.0 - 1 = +50%.
	in := basket(months(start, 100, 200), nil, months(start, 100, 300))
	got := buildCostOfLiving(in, start, start.AddDate(0, 0, 59))
	v := itemByCode(got, sciVehiclesSeriesCode)
	if v == nil || v.RealGrowthPct == nil {
		t.Fatalf("vehicles must measure against the headline: %+v", v)
	}
	if math.Abs(*v.GrowthPct-200) > 5e-7 {
		t.Errorf("nominal growth = %v, want 200%%", *v.GrowthPct)
	}
	if math.Abs(*v.RealGrowthPct-50) > 5e-7 {
		t.Errorf("real growth = %v, want exactly 50%%", *v.RealGrowthPct)
	}
	// Shelter was not supplied: its row must still exist and say so.
	sh := itemByCode(got, sciShelterSeriesCode)
	if sh == nil || sh.GrowthPct != nil {
		t.Fatalf("an absent component must be a row with no number, got %+v", sh)
	}
	if !strings.Contains(strings.Join(sh.Notes, " "), "never interpolated") {
		t.Errorf("an absent component must refuse rather than estimate: %v", sh.Notes)
	}
}

// A component whose coverage is SHORTER than the headline's must be deflated
// over its own periods, not over the headline's longer span.
func TestAComponentIsDeflatedOverItsOwnPeriodsNotTheHeadlines(t *testing.T) {
	start := day(2026, time.January, 21)
	// Headline runs four months and triples; vehicles run only the first two
	// and double. Deflating the 2-month vehicle leg by the 4-month headline
	// ratio would report a large fake decline.
	headline := months(start, 100, 150, 220, 300)
	vehicles := months(start, 100, 200)
	got := buildCostOfLiving(basket(headline, nil, vehicles), start, start.AddDate(0, 0, 119))

	v := itemByCode(got, sciVehiclesSeriesCode)
	if v == nil || v.RealGrowthPct == nil {
		t.Fatalf("vehicles cover two periods and must measure: %+v", v)
	}
	// Correct: 2.0 / 1.5 - 1 = +33.333%. Wrong (headline's full span): 2.0/3.0-1 = -33%.
	want := (2.0/1.5 - 1) * 100
	if math.Abs(*v.RealGrowthPct-want) > 5e-7 {
		t.Errorf("real growth = %.6f%%, want %.6f%% -- the headline leg must span the "+
			"component's OWN periods, or coverage differences read as price moves",
			*v.RealGrowthPct, want)
	}
	if v.Periods != 1 {
		t.Errorf("periods = %d, want 1", v.Periods)
	}
}

func TestTooFewPeriodsIsARefusalNotAZero(t *testing.T) {
	start := day(2026, time.January, 21)
	in := basket(months(start, 100, 110, 120), months(start, 100), nil)
	got := buildCostOfLiving(in, start, start.AddDate(0, 0, 89))
	sh := itemByCode(got, sciShelterSeriesCode)
	if sh == nil {
		t.Fatal("the row must exist even when it cannot be measured")
	}
	if sh.GrowthPct != nil || sh.RealGrowthPct != nil {
		t.Errorf("one period cannot produce growth, got %v / %v", sh.GrowthPct, sh.RealGrowthPct)
	}
	if sh.Periods != 0 {
		t.Errorf("periods = %d, want 0", sh.Periods)
	}
}

// Without the headline there is nothing to measure against, and bare index
// growth beside deflated asset returns would invite exactly the wrong reading.
func TestNoHeadlineMeansNoSection(t *testing.T) {
	start := day(2026, time.January, 21)
	in := basket(nil, months(start, 100, 150), months(start, 100, 200))
	if got := buildCostOfLiving(in, start, start.AddDate(0, 0, 59)); got != nil {
		t.Errorf("with no headline the whole section must be withheld, got %d rows",
			len(got.Items))
	}
}

// Structural, not advisory: the section carries no numeraire and no rank, so a
// price index cannot be converted into grams of gold or win "best performer".
func TestCostOfLivingCarriesNoNumeraireAndNoRank(t *testing.T) {
	start := day(2026, time.January, 21)
	got := buildCostOfLiving(
		basket(months(start, 100, 110), months(start, 100, 105), months(start, 100, 130)),
		start, start.AddDate(0, 0, 59))
	blob, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	// KEYS, not the whole body: the note deliberately uses the words "ranked"
	// and "bought" to explain why this section is separate, and matching those
	// as substrings would fail on the explanation instead of on the structure.
	var decoded struct {
		Items []map[string]json.RawMessage `json:"items"`
	}
	if err := json.Unmarshal(blob, &decoded); err != nil {
		t.Fatal(err)
	}
	if len(decoded.Items) == 0 {
		t.Fatal("expected rows to inspect")
	}
	for _, row := range decoded.Items {
		for key := range row {
			switch key {
			case "numeraire", "conversion", "rank", "usd_growth_pct", "gold_grams":
				t.Errorf("row carries %q: a price index is already a ratio to its own "+
					"base period, so converting or ranking it produces nothing", key)
			}
		}
	}
}
