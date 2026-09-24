package equities

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// The data_age block exists because of one measured failure. On 2026-09-24 the
// newest equity bar in production was 2026-09-09, and for fifteen days
// /api/v1/stocks/screen answered every request with a `to` of the current date
// and prices from a fortnight earlier. Each field was individually true; the
// combination read as today's market.

// --- what the roster's newest bar is -----------------------------------------

func TestNewestRosterBarTakesTheNewestAndIgnoresTheUningested(t *testing.T) {
	rows := []stockRow{
		{SymbolFA: "خودرو", LastBar: ptr(day(2026, time.August, 20))},
		{SymbolFA: "فولاد", LastBar: ptr(day(2026, time.September, 9))},
		// Registered but never collected — کهربا sat in exactly this state for
		// months. It contributes no date, and must not contribute a zero one.
		{SymbolFA: "کهربا", LastBar: nil},
	}
	got := newestRosterBar(rows)
	if got == nil {
		t.Fatal("a roster with stored bars must report a newest date")
	}
	if !got.Equal(day(2026, time.September, 9)) {
		t.Errorf("newest = %s, want 2026-09-09", got.Format(dateLayout))
	}
}

func TestNewestRosterBarIsNilWhenNothingHasEverBeenIngested(t *testing.T) {
	if got := newestRosterBar(nil); got != nil {
		t.Errorf("an empty roster must report no date, got %v", got)
	}
	rows := []stockRow{{SymbolFA: "کهربا"}, {SymbolFA: "همراه"}}
	if got := newestRosterBar(rows); got != nil {
		t.Errorf("a roster with no stored bars must report no date, got %v", got)
	}
}

// --- the age arithmetic and the bound ----------------------------------------

func TestDataAgeReportsTheFifteenDayFailureThisBlockExistsFor(t *testing.T) {
	// The exact production measurement: newest bar 2026-09-09, measured
	// 2026-09-24.
	got := buildDataAge(ptr(day(2026, time.September, 9)), day(2026, time.September, 24))

	if got.NewestTradeDate == nil || *got.NewestTradeDate != "2026-09-09" {
		t.Fatalf("newest_trade_date = %v, want 2026-09-09", got.NewestTradeDate)
	}
	if got.AgeDays == nil || *got.AgeDays != 15 {
		t.Fatalf("age_days = %v, want 15", got.AgeDays)
	}
	if !got.Stale {
		t.Error("fifteen days is beyond any Tehran market closure and must read as stale")
	}
	// "This is old" without "here is how to fix it" leaves the reader's next
	// question unanswerable from what they are holding.
	if !strings.Contains(got.Warning, equityRefreshCommand) {
		t.Errorf("the warning must name the command that fixes it: %q", got.Warning)
	}
	if got.RefreshCommand != equityRefreshCommand {
		t.Errorf("refresh_command = %q", got.RefreshCommand)
	}
	if got.AsOf != "2026-09-24" {
		t.Errorf("as_of = %q, want the day the age was measured against", got.AsOf)
	}
}

func TestDataAgeDoesNotCallTheTehranWeekendStale(t *testing.T) {
	// The mistake that makes 38 of the last 48 warnings closed-market noise.
	// The exchange trades Saturday to Wednesday, so on Saturday morning before
	// the session lands, the newest bar is Wednesday's — three days old and
	// perfectly healthy. A 24h or 48h bound pages every single weekend.
	wed := day(2026, time.September, 9) // a Wednesday
	for _, c := range []struct {
		name string
		now  time.Time
		age  int
	}{
		{"Thursday, exchange closed", day(2026, time.September, 10), 1},
		{"Friday, exchange closed", day(2026, time.September, 11), 2},
		{"Saturday before the session lands", day(2026, time.September, 12), 3},
		{"a holiday-extended closure", day(2026, time.September, 14), 5},
		// The bound itself: a weekly refresh cadence leaves the newest bar
		// around 7–8 days old immediately before a run that is still on time.
		{"one on-time weekly cycle", day(2026, time.September, 17), 8},
		{"exactly at the bound", day(2026, time.September, 19), 10},
	} {
		got := buildDataAge(&wed, c.now)
		if got.AgeDays == nil || *got.AgeDays != c.age {
			t.Errorf("%s: age_days = %v, want %d", c.name, got.AgeDays, c.age)
		}
		if got.Stale {
			t.Errorf("%s: %d days must not read as stale — the market calendar and an "+
				"on-time weekly refresh produce it between them", c.name, c.age)
		}
		if got.Warning != "" {
			t.Errorf("%s: no warning belongs on healthy data: %q", c.name, got.Warning)
		}
	}
}

func TestDataAgeGoesStaleOneDayPastTheBound(t *testing.T) {
	wed := day(2026, time.September, 9)
	if got := buildDataAge(&wed, day(2026, time.September, 19)); got.Stale {
		t.Errorf("%d days is the bound itself and must not be stale", *got.AgeDays)
	}
	got := buildDataAge(&wed, day(2026, time.September, 20))
	if *got.AgeDays != 11 || !got.Stale {
		t.Errorf("age_days = %d stale = %v, want 11/true", *got.AgeDays, got.Stale)
	}
	if got.StaleAfterDays != equityStaleAfterDays {
		t.Errorf("stale_after_days = %d, want the published bound %d",
			got.StaleAfterDays, equityStaleAfterDays)
	}
}

func TestDataAgeOnAnEmptyRosterIsNullAndNotZero(t *testing.T) {
	// A zero age would read as "collected today", which is the exact
	// substitution this whole block exists to prevent — the same reason the
	// gauges in internal/alerts publish no series rather than a zero
	// timestamp. Nothing is stale here because nothing is being served: the
	// block says so in prose instead.
	got := buildDataAge(nil, day(2026, time.September, 24))
	if got.NewestTradeDate != nil {
		t.Errorf("newest_trade_date = %v, want null", *got.NewestTradeDate)
	}
	if got.AgeDays != nil {
		t.Errorf("age_days = %v, want null rather than 0", *got.AgeDays)
	}
	if got.Stale {
		t.Error("an empty roster serves no stale price — there is no price at all")
	}
	if !strings.Contains(got.Warning, "Tehran equity bars for the symbols in this response") {
		t.Errorf("an empty roster must still say so: %q", got.Warning)
	}
	// ...and must say it about THIS RESPONSE, not about the installation.
	// buildDataAge only ever sees the rows being returned, and Stocks()
	// applies ?enabled= and ?sector= before calling it, so an absolute claim
	// here is false whenever a filter selected only never-ingested symbols:
	// ?enabled=false&sector=13 on a fully current deployment returns just
	// کچاد. Asserted as an absence because the bug was a true-sounding
	// sentence, not a missing one — the previous wording passed the check
	// above while telling an operator the whole dataset was gone.
	for _, overclaim := range []string{"This deployment holds no", "at all"} {
		if strings.Contains(got.Warning, overclaim) {
			t.Errorf("the warning claims %q about the whole deployment from a filtered "+
				"row set it cannot see past: %q", overclaim, got.Warning)
		}
	}

	blob, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(blob), `"age_days":null`) {
		t.Errorf("age_days must serialise as null, not 0: %s", blob)
	}
}

func TestDataAgeCallsAFutureTradeDateAFaultRatherThanFreshness(t *testing.T) {
	// A bar dated after today is not freshness; migration 0028 notes that
	// TSETMC's dEven is Gregorian and reading it as Jalali would place the
	// whole series 621 years out. Clamping the age to zero would render such a
	// series as perfectly current, which is the worst possible answer.
	got := buildDataAge(ptr(day(2026, time.October, 1)), day(2026, time.September, 24))
	if got.AgeDays == nil || *got.AgeDays != -7 {
		t.Fatalf("age_days = %v, want -7 reported rather than clamped", got.AgeDays)
	}
	if !got.Stale {
		t.Error("a future-dated bar must not be presented as trustworthy")
	}
	if !strings.Contains(got.Warning, "AFTER today") {
		t.Errorf("the warning must name the fault: %q", got.Warning)
	}
}

// --- on the wire -------------------------------------------------------------

func TestStocksResponseCarriesTheAgeOfItsOwnRows(t *testing.T) {
	// Measured over the rows in THIS response: a caller filtering by ?sector=
	// is asking about that sector, and a freshness claim taken from some other
	// sector's symbols would be a true statement about the wrong thing.
	out := buildStocksResponse([]stockRow{foolad()}, day(2026, time.September, 24))
	if !out.DataAge.Stale || *out.DataAge.AgeDays != 15 {
		t.Fatalf("data_age = %+v, want the roster's own 15-day age", out.DataAge)
	}
	blob, err := json.Marshal(out)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !strings.Contains(string(blob), `"data_age"`) {
		t.Errorf("the roster must publish data_age: %s", blob)
	}
}

func TestScreenResponseStatesHowOldItsPricesAreAndWarnsInProse(t *testing.T) {
	// The failing shape, reconstructed: a window whose `to` is today over a
	// roster whose newest stored session is fifteen days earlier.
	stale := rosterRow("فولاد", "فولاد مبارکه اصفهان", "27", "فلزات اساسی", 4636)
	stale.LastBar = ptr(day(2026, time.February, 23))

	out := buildScreenResponse(screenInputs{
		Query:  mustQuery(t, "period=max"), // clock: 2026-03-10
		Roster: []stockRow{stale},
		Window: map[string][]screenBarRow{
			stale.InsCode: {
				bar(day(2026, time.February, 22), 100, 10, 1, 1000, 1.0),
				bar(day(2026, time.February, 23), 110, 10, 1, 1000, 1.0),
			},
		},
		Latest:    map[string]screenBarRow{},
		Converter: screenConverter(),
		Sectors:   testSectors(),
	})

	if out.To != "2026-03-10" {
		t.Fatalf("to = %q, want today — this is the label that misled", out.To)
	}
	if out.DataAge.AgeDays == nil || *out.DataAge.AgeDays != 15 {
		t.Fatalf("data_age.age_days = %v, want 15", out.DataAge.AgeDays)
	}
	if !out.DataAge.Stale {
		t.Error("a window labelled today over fifteen-day-old prices must say so")
	}
	// Also in `warnings`, so a client already rendering that list gets the
	// banner with no change at all.
	found := false
	for _, w := range out.Warnings {
		if strings.Contains(w, "15 days old") {
			found = true
		}
	}
	if !found {
		t.Errorf("the staleness must reach `warnings` too, got %q", out.Warnings)
	}
}

func TestScreenAgeCoversExcludedSymbolsToo(t *testing.T) {
	// An instrument the gate excludes still receives bars from the same fetch,
	// so its last_bar is evidence about whether that fetch ran. Dropping it
	// would let one refused symbol make the whole dataset look staler than it
	// is — or, worse, let a roster of nothing-but-excluded symbols report no
	// age at all.
	refused := rosterRow("شتران", "پالایش نفت تهران", "23", "فراورده های نفتی", 2371)
	refused.AdjustmentStatus = ptr("refused")
	refused.RefusalReason = ptr("the adjusted series still contains a -58.4% artefact.")
	refused.LastBar = ptr(day(2026, time.March, 9))

	out := buildScreenResponse(screenInputs{
		Query:     mustQuery(t, "period=max"),
		Roster:    []stockRow{refused},
		Window:    map[string][]screenBarRow{},
		Latest:    map[string]screenBarRow{},
		Converter: screenConverter(),
		Sectors:   testSectors(),
	})

	if out.Count != 0 || out.ExcludedCount != 1 {
		t.Fatalf("expected the symbol excluded, got count=%d excluded=%d",
			out.Count, out.ExcludedCount)
	}
	if out.DataAge.NewestTradeDate == nil || *out.DataAge.NewestTradeDate != "2026-03-09" {
		t.Fatalf("data_age must read the excluded symbol's bars too, got %+v", out.DataAge)
	}
	if out.DataAge.Stale {
		t.Error("a one-day-old dataset is not stale")
	}
}

func TestScreenAgeIsAPropertyOfTheDataNotOfTheRequestedWindow(t *testing.T) {
	// ?period= narrows what is measured; it says nothing about how far the
	// stored data reaches. A one-month screen and a max screen over the same
	// roster must report the same age, because they are over the same data —
	// the alternative is an age that a caller can change by asking a different
	// question.
	row := rosterRow("فولاد", "فولاد مبارکه اصفهان", "27", "فلزات اساسی", 4636)
	row.LastBar = ptr(day(2026, time.February, 23))

	for _, period := range []string{"max", "1m", "1y"} {
		out := buildScreenResponse(screenInputs{
			Query:     mustQuery(t, "period="+period),
			Roster:    []stockRow{row},
			Window:    map[string][]screenBarRow{},
			Latest:    map[string]screenBarRow{},
			Converter: screenConverter(),
			Sectors:   testSectors(),
		})
		if out.DataAge.AgeDays == nil || *out.DataAge.AgeDays != 15 {
			t.Errorf("period=%s: age_days = %v, want 15 measured against today (2026-03-10)",
				period, out.DataAge.AgeDays)
		}
	}
}
