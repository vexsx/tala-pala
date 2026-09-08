package relvalue

// The performance table: what it includes, what it refuses to include, and what
// it withholds rather than estimate.

import (
	"bytes"
	"encoding/json"
	"math"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
)

// The registry fixture mirrors the shape migration 0024 seeds: two Iranian
// prices in toman, a dollar-quoted global proxy, a yield in percent, a
// dimensionless index, a fund with no collected history, and a disabled row.
func testInstruments() []instrumentRow {
	return []instrumentRow{
		{Code: "USD_IRT", Kind: "fx", Domain: "fx", QuoteCurrency: quoteIRT, Unit: "usd",
			QualityTier: "proxy", IsProxy: true, Enabled: true, NameEN: "US dollar, free market",
			Notes: "Free-market USDT/toman, NOT an official rate."},
		{Code: "DXY", Kind: "index", Domain: "global", QuoteCurrency: quoteINDEX, Unit: "index",
			QualityTier: "official_mirror", Enabled: true},
		{Code: "US10Y", Kind: "market_price", Domain: "global", QuoteCurrency: quotePCT, Unit: "pct",
			QualityTier: "official_mirror", Enabled: true},
		{Code: "XAUUSD", Kind: "market_price", Domain: "global", QuoteCurrency: quoteUSD, Unit: "ozt",
			QualityTier: "proxy", IsProxy: true, Enabled: true,
			Notes: "COMEX front-month future, NOT the London spot fix."},
		{Code: "IR_GOLD_18K", Kind: "market_price", Domain: "gold", QuoteCurrency: quoteIRT, Unit: "gram",
			QualityTier: "official_mirror", Enabled: true},
		{Code: "IR_GOLD_FUND_KAHRABA", Kind: "market_price", Domain: "fund", QuoteCurrency: quoteIRT,
			Unit: "unit", QualityTier: "official_mirror", Enabled: true},
		{Code: "RETIRED_SYMBOL", Kind: "market_price", Domain: "gold", QuoteCurrency: quoteIRT,
			Unit: "gram", QualityTier: "official_mirror", Enabled: false},
	}
}

// Three annual snapshots, chosen so every derived figure below is exact.
func testSeries() map[string]dailySeries {
	return map[string]dailySeries{
		"IR_GOLD_18K": {
			pt(2024, time.January, 1, 500),
			pt(2025, time.January, 1, 1000),
			pt(2026, time.January, 1, 2000),
		},
		"USD_IRT": {
			pt(2024, time.January, 1, 40),
			pt(2025, time.January, 1, 50),
			pt(2026, time.January, 1, 80),
		},
		"XAUUSD": {
			pt(2024, time.January, 1, 1),
			pt(2025, time.January, 1, 2),
			pt(2026, time.January, 1, 3),
		},
		// DXY and US10Y have history; the table excludes them for what they
		// ARE, not for what they lack.
		"DXY":   {pt(2024, time.January, 1, 100), pt(2026, time.January, 1, 110)},
		"US10Y": {pt(2024, time.January, 1, 4), pt(2026, time.January, 1, 4.3)},
	}
}

func testCPI() cpiTable {
	// Coverage stops at 2025 while the window runs to 2026 -- the real
	// situation the World Bank series puts this endpoint in.
	return annualCPI([2]float64{2024, 100}, [2]float64{2025, 125})
}

func performanceFixture(t *testing.T, numeraire string) performanceResponse {
	t.Helper()
	spec, perr := parseNumeraire(numeraire)
	if perr != nil {
		t.Fatalf("fixture numeraire %q: %v", numeraire, perr)
	}
	from := day(2024, time.January, 1)
	prov := testCPIProvenance()
	return buildPerformanceResponse(performanceInputs{
		Query: performanceQuery{
			Numeraire: spec,
			Window:    window{From: &from, To: day(2026, time.January, 1), Period: ptr("max")},
		},
		Instruments:   testInstruments(),
		Series:        testSeries(),
		CPI:           testCPI(),
		CPIProvenance: &prov,
		AsOf:          day(2026, time.January, 1),
	})
}

// The deflator's identity as the catalog carries it. Every field here is one a
// reader needs to know WHICH consumer price index deflated the number beside it.
func testCPIProvenance() economic.SeriesProvenance {
	return economic.SeriesProvenance{
		Code: cpiSeriesCode, NameEN: "Iran consumer price index (World Bank)",
		Measure: "index", Unit: "index", Frequency: "annual", Calendar: "gregorian",
		BasePeriod: "2010=100", ProviderCode: "worldbank", ProviderSeriesID: "FP.CPI.TOTL",
		QualityTier: "official_mirror", SplicePolicy: "none", SeasonalAdjustment: "nsa",
		Notes: "World Bank rebase to 2010=100, NOT the Statistical Centre of Iran's 1400=100 index.",
	}
}

func itemFor(t *testing.T, resp performanceResponse, code string) performanceItem {
	t.Helper()
	for _, it := range resp.Items {
		if it.Code == code {
			return it
		}
	}
	t.Fatalf("%s is not in the table", code)
	return performanceItem{}
}

func absentFrom(t *testing.T, resp performanceResponse, code string) {
	t.Helper()
	for _, it := range resp.Items {
		if it.Code == code {
			t.Fatalf("%s must not appear in the performance table", code)
		}
	}
}

func warningsMentioning(resp performanceResponse, needle string) bool {
	for _, w := range resp.Warnings {
		if strings.Contains(w, needle) {
			return true
		}
	}
	return false
}

func notesMentioning(item performanceItem, needle string) bool {
	for _, n := range item.Notes {
		if strings.Contains(n, needle) {
			return true
		}
	}
	return false
}

// --- what is excluded, and said out loud ---------------------------------------

func TestPerformance_ExcludesRatesAndNamesThem(t *testing.T) {
	got := performanceFixture(t, "IRT")
	absentFrom(t, got, "US10Y")
	if !warningsMentioning(got, "US10Y") {
		t.Fatalf("US10Y was dropped without a word: %v", got.Warnings)
	}
	if !warningsMentioning(got, "RATES rather than prices") {
		t.Errorf("the reason must be stated: %v", got.Warnings)
	}
}

func TestPerformance_ExcludesIndexLevelsAndNamesThem(t *testing.T) {
	got := performanceFixture(t, "USD")
	absentFrom(t, got, "DXY")
	if !warningsMentioning(got, "DXY") {
		t.Fatalf("DXY was dropped without a word: %v", got.Warnings)
	}
	if !warningsMentioning(got, "not denominated in any currency") {
		t.Errorf("the reason must be stated: %v", got.Warnings)
	}
}

func TestPerformance_ExcludesDisabledInstrumentsAndNamesThem(t *testing.T) {
	got := performanceFixture(t, "IRT")
	absentFrom(t, got, "RETIRED_SYMBOL")
	if !warningsMentioning(got, "RETIRED_SYMBOL") {
		t.Fatalf("a disabled instrument was dropped silently: %v", got.Warnings)
	}
}

// --- the numbers ------------------------------------------------------------------

func TestPerformance_NominalAndCrossNumeraireReturns(t *testing.T) {
	got := performanceFixture(t, "IRT")

	gold := itemFor(t, got, "IR_GOLD_18K")
	// 500 -> 2000 toman.
	if v := mustFloat(t, gold.NominalReturnPct, "gold nominal"); math.Abs(v-300) > 1e-6 {
		t.Errorf("IR_GOLD_18K nominal = %v, want 300", v)
	}
	// In dollars: 500/40 = 12.5 -> 2000/80 = 25.
	if v := mustFloat(t, gold.USDReturnPct, "gold usd"); math.Abs(v-100) > 1e-6 {
		t.Errorf("IR_GOLD_18K usd = %v, want 100", v)
	}
	// Measured in itself, gold is 1.000 every day -- a property of the unit, not
	// a finding, so the cell is null and says why rather than printing 0.00%
	// beside genuinely measured zeros.
	if gold.GoldReturnPct != nil {
		t.Errorf("IR_GOLD_18K in grams of gold = %v, want null by construction", *gold.GoldReturnPct)
	}
	if !notesMentioning(gold, "DEFINES this unit") {
		t.Errorf("the definitional cell must explain itself: %v", gold.Notes)
	}
	if mustFloat(t, gold.StartValue, "start") != 500 || mustFloat(t, gold.EndValue, "end") != 2000 {
		t.Errorf("start/end = %v/%v, want 500/2000", *gold.StartValue, *gold.EndValue)
	}
	if gold.Observations != 3 {
		t.Errorf("observations = %d, want 3", gold.Observations)
	}

	xau := itemFor(t, got, "XAUUSD")
	// A dollar-quoted asset in toman: 1*40 = 40 -> 3*80 = 240.
	if v := mustFloat(t, xau.NominalReturnPct, "xau nominal in IRT"); math.Abs(v-500) > 1e-6 {
		t.Errorf("XAUUSD nominal in toman = %v, want 500", v)
	}
	// In its own currency it is untouched: 1 -> 3.
	if v := mustFloat(t, xau.USDReturnPct, "xau usd"); math.Abs(v-200) > 1e-6 {
		t.Errorf("XAUUSD usd = %v, want 200", v)
	}
	// In grams: 1*40/500 = 0.08 -> 3*80/2000 = 0.12.
	if v := mustFloat(t, xau.GoldReturnPct, "xau gold"); math.Abs(v-50) > 1e-6 {
		t.Errorf("XAUUSD in grams of gold = %v, want 50", v)
	}

	usd := itemFor(t, got, "USD_IRT")
	// 40/500 = 0.08 grams -> 80/2000 = 0.04 grams: the dollar halved against
	// gold while quadrupling against the toman, which is the whole reason a
	// numeraire column exists.
	if v := mustFloat(t, usd.GoldReturnPct, "usd in gold"); math.Abs(v-(-50)) > 1e-6 {
		t.Errorf("USD_IRT in grams of gold = %v, want -50", v)
	}
}

func TestPerformance_DollarAssetInDollarsNeedsNoFxSeries(t *testing.T) {
	// XAUUSD in USD must not depend on USD_IRT having quoted: a round trip
	// through the toman rate would drop every day the rate is missing.
	series := testSeries()
	delete(series, "USD_IRT")
	spec, _ := parseNumeraire("USD")
	from := day(2024, time.January, 1)
	got := buildPerformanceResponse(performanceInputs{
		Query:       performanceQuery{Numeraire: spec, Window: window{From: &from, To: day(2026, time.January, 1)}},
		Instruments: testInstruments(),
		Series:      series,
		AsOf:        day(2026, time.January, 1),
	})
	xau := itemFor(t, got, "XAUUSD")
	if v := mustFloat(t, xau.NominalReturnPct, "xau in usd"); math.Abs(v-200) > 1e-6 {
		t.Fatalf("XAUUSD in USD = %v, want 200 with no fx series present", v)
	}
	// The toman-quoted rows, by contrast, genuinely cannot be converted.
	gold := itemFor(t, got, "IR_GOLD_18K")
	if gold.NominalReturnPct != nil {
		t.Errorf("IR_GOLD_18K in USD = %v, want null without USD_IRT", *gold.NominalReturnPct)
	}
	if !notesMentioning(gold, usdSeriesCode) {
		t.Errorf("the missing series must be named: %v", gold.Notes)
	}
}

func TestPerformance_VolatilityAndDrawdown(t *testing.T) {
	got := performanceFixture(t, "IRT")
	gold := itemFor(t, got, "IR_GOLD_18K")
	// 500 -> 1000 -> 2000 is ln2 twice: zero dispersion, and the sample
	// standard deviation of two identical returns is exactly 0.
	if v := mustFloat(t, gold.ObservationVolatilityPct, "volatility"); math.Abs(v) > 1e-9 {
		t.Errorf("observation volatility = %v, want 0 for two identical log returns", v)
	}
	if v := mustFloat(t, gold.MaxDrawdownPct, "drawdown"); v != 0 {
		t.Errorf("max drawdown = %v, want 0 for a monotone rise", v)
	}
	// The series steps a year at a time, so the figure is emphatically not
	// per-day and the field must not be called daily.
	if !notesMentioning(gold, "between consecutive OBSERVATIONS") ||
		!notesMentioning(gold, "not a per-day figure") {
		t.Errorf("a gappy series must say the sigma is per observation: %v", gold.Notes)
	}
	if !notesMentioning(gold, "not annualised") && !notesMentioning(gold, "NOT annualised") {
		t.Errorf("the non-annualised label must be stated: %v", gold.Notes)
	}
}

func TestPerformance_RealReturnUsesTheCPICoveredSubWindow(t *testing.T) {
	got := performanceFixture(t, "IRT")
	if got.CPISeries == nil || *got.CPISeries != cpiSeriesCode {
		t.Fatalf("cpi_series = %v, want %s", got.CPISeries, cpiSeriesCode)
	}
	if got.CPICoverageTo == nil || *got.CPICoverageTo != "2025-01-01" {
		t.Fatalf("cpi_coverage_to = %v, want 2025-01-01", got.CPICoverageTo)
	}

	gold := itemFor(t, got, "IR_GOLD_18K")
	// Sub-window 2024-01-01 -> 2025-01-01: 500 -> 1000 nominal against +25%
	// prices, so (1 + 1.0) / 1.25 - 1 = 60%.
	if v := mustFloat(t, gold.RealReturnPct, "real return"); math.Abs(v-60) > 1e-6 {
		t.Errorf("real return = %v, want 60", v)
	}
	if gold.RealReturnFrom == nil || *gold.RealReturnFrom != "2024-01-01" {
		t.Errorf("real_return_from = %v, want 2024-01-01", gold.RealReturnFrom)
	}
	if gold.RealReturnTo == nil || *gold.RealReturnTo != "2025-01-01" {
		t.Errorf("real_return_to = %v, want 2025-01-01 -- NOT the window's own end", gold.RealReturnTo)
	}
	// The nominal window ends 2026-01-01. The reader must be told the real one
	// does not, rather than being left to compare two dates.
	if !notesMentioning(gold, "NOT the nominal window") {
		t.Errorf("the shortened window must be declared: %v", gold.Notes)
	}
}

func TestPerformance_RealReturnInANonIRTNumeraireIsQualified(t *testing.T) {
	got := performanceFixture(t, "USD")
	gold := itemFor(t, got, "IR_GOLD_18K")
	if gold.RealReturnPct == nil {
		t.Fatal("a USD-terms real return should still be computable")
	}
	if !notesMentioning(gold, "IRAN's consumer price index") {
		t.Errorf("deflating a dollar return by Iran's CPI must be flagged: %v", gold.Notes)
	}
}

func TestPerformance_WithoutACPISeries(t *testing.T) {
	from := day(2024, time.January, 1)
	spec, _ := parseNumeraire("IRT")
	got := buildPerformanceResponse(performanceInputs{
		Query:       performanceQuery{Numeraire: spec, Window: window{From: &from, To: day(2026, time.January, 1)}},
		Instruments: testInstruments(),
		Series:      testSeries(),
		CPIError:    "No real returns: this deployment does not carry the CPI series " + cpiSeriesCode + ".",
		AsOf:        day(2026, time.January, 1),
	})
	if got.CPISeries != nil || got.CPICoverageTo != nil {
		t.Error("no CPI means no cpi_series and no coverage claim")
	}
	if !warningsMentioning(got, "does not carry the CPI series") {
		t.Errorf("the absence must be declared: %v", got.Warnings)
	}
	gold := itemFor(t, got, "IR_GOLD_18K")
	if gold.RealReturnPct != nil {
		t.Error("no CPI means no real return, never an undeflated one wearing the name")
	}
	// The nominal table is still true without a deflator.
	if gold.NominalReturnPct == nil {
		t.Error("the nominal figures must survive a missing CPI")
	}
}

// --- withheld, not estimated ----------------------------------------------------------

func TestPerformance_InstrumentWithNoHistoryIsAllNullsWithAReason(t *testing.T) {
	got := performanceFixture(t, "IRT")
	fund := itemFor(t, got, "IR_GOLD_FUND_KAHRABA")
	if fund.StartValue != nil || fund.EndValue != nil || fund.NominalReturnPct != nil ||
		fund.ObservationVolatilityPct != nil || fund.MaxDrawdownPct != nil {
		t.Fatalf("an instrument with no observation must be entirely null: %+v", fund)
	}
	if fund.Observations != 0 {
		t.Errorf("observations = %d, want 0", fund.Observations)
	}
	if !notesMentioning(fund, "stored no observation") {
		t.Errorf("the reason must travel with the nulls: %v", fund.Notes)
	}
}

func TestPerformance_WindowOutsideAnInstrumentsCoverage(t *testing.T) {
	from := day(2030, time.January, 1)
	spec, _ := parseNumeraire("IRT")
	got := buildPerformanceResponse(performanceInputs{
		Query:       performanceQuery{Numeraire: spec, Window: window{From: &from, To: day(2031, time.January, 1)}},
		Instruments: testInstruments(),
		Series:      testSeries(),
		AsOf:        day(2031, time.January, 1),
	})
	gold := itemFor(t, got, "IR_GOLD_18K")
	if gold.NominalReturnPct != nil {
		t.Fatal("a window past the data must produce nulls, not the nearest values")
	}
	if !notesMentioning(gold, "Stored coverage is 2024-01-01..2026-01-01") {
		t.Errorf("the real coverage must be named: %v", gold.Notes)
	}
}

func TestPerformance_DroppedDaysAreCounted(t *testing.T) {
	// The fx rate starts a year after gold does, so the first gold day cannot
	// be expressed in dollars at all.
	series := testSeries()
	series["USD_IRT"] = dailySeries{
		pt(2025, time.January, 1, 50),
		pt(2026, time.January, 1, 80),
	}
	spec, _ := parseNumeraire("USD")
	from := day(2024, time.January, 1)
	got := buildPerformanceResponse(performanceInputs{
		Query:       performanceQuery{Numeraire: spec, Window: window{From: &from, To: day(2026, time.January, 1)}},
		Instruments: testInstruments(),
		Series:      series,
		AsOf:        day(2026, time.January, 1),
	})
	gold := itemFor(t, got, "IR_GOLD_18K")
	if gold.Observations != 2 {
		t.Fatalf("observations = %d, want 2 -- the 2024 day has no prior fx quote", gold.Observations)
	}
	if gold.CoverageFrom == nil || *gold.CoverageFrom != "2025-01-01" {
		t.Fatalf("coverage_from = %v, want 2025-01-01", gold.CoverageFrom)
	}
	if !notesMentioning(gold, "price the past with its own future") {
		t.Errorf("the look-ahead refusal must be explained: %v", gold.Notes)
	}
}

// --- top-level shape --------------------------------------------------------------------

func TestPerformance_EchoesTheWindowAndTheNumeraire(t *testing.T) {
	got := performanceFixture(t, "GOLD")
	if got.Numeraire != "GOLD" {
		t.Errorf("numeraire = %q", got.Numeraire)
	}
	if got.NumeraireSeries.Series == nil || *got.NumeraireSeries.Series != goldSeriesCode {
		t.Fatalf("the backing series must be named: %+v", got.NumeraireSeries)
	}
	if got.NumeraireSeries.Unit != "grams of 18k gold" {
		t.Errorf("unit = %q, want the specific 18k-gram label", got.NumeraireSeries.Unit)
	}
	if got.NumeraireSeries.QualityTier == nil {
		t.Error("the backing series' quality tier must travel")
	}
	if got.From == nil || !got.From.Equal(day(2024, time.January, 1)) {
		t.Errorf("from = %v", got.From)
	}
	if got.Count != len(got.Items) {
		t.Errorf("count %d does not match %d items", got.Count, len(got.Items))
	}
}

func TestPerformance_NumeraireProvenanceIsProxyAware(t *testing.T) {
	got := performanceFixture(t, "USD")
	if got.NumeraireSeries.IsProxy == nil || !*got.NumeraireSeries.IsProxy {
		t.Fatal("a dollar figure computed through the USDT/toman market must say it is a proxy")
	}
}

func TestPerformance_MaxWindowReportsTheEarliestRealDay(t *testing.T) {
	spec, _ := parseNumeraire("IRT")
	got := buildPerformanceResponse(performanceInputs{
		Query:       performanceQuery{Numeraire: spec, Window: window{From: nil, To: day(2026, time.January, 1), Period: ptr("max")}},
		Instruments: testInstruments(),
		Series:      testSeries(),
		AsOf:        day(2026, time.January, 1),
	})
	if got.From == nil || !got.From.Equal(day(2024, time.January, 1)) {
		t.Fatalf("from = %v, want the earliest day any item could be served", got.From)
	}
}

func TestPerformance_EmptyCollectionsAreNeverNull(t *testing.T) {
	spec, _ := parseNumeraire("IRT")
	got := buildPerformanceResponse(performanceInputs{
		Query:       performanceQuery{Numeraire: spec, Window: window{To: day(2026, time.January, 1)}},
		Instruments: nil,
		Series:      nil,
		AsOf:        day(2026, time.January, 1),
	})
	if got.Items == nil || got.Warnings == nil {
		t.Fatal("items and warnings must serialize as [] rather than null")
	}
}

// --- the numeraire menu ------------------------------------------------------------------

func TestNumeraireList_MarksTheUnbackedOnesUnavailable(t *testing.T) {
	// Nothing has ever been collected for the gold divisor.
	series := map[string]dailySeries{usdSeriesCode: {pt(2026, time.January, 1, 80)}}
	got := buildNumeraireList(testInstruments(), series, day(2026, time.January, 1))
	if got.Count != len(numeraireSpecs) {
		t.Fatalf("count = %d, want every numeraire listed", got.Count)
	}
	for _, item := range got.Items {
		switch item.Key {
		case "IRT":
			if !item.Available || !item.IsIdentity {
				t.Errorf("the hub is always available: %+v", item)
			}
		case "USD":
			if !item.Available {
				t.Errorf("USD is backed here: %+v", item)
			}
			if item.CoverageFrom == nil || item.Observations != 1 {
				t.Errorf("coverage must be reported: %+v", item)
			}
		case "GOLD":
			if item.Available {
				t.Fatal("GOLD cannot be offered without IR_GOLD_18K")
			}
			if item.UnavailableReason == nil || !strings.Contains(*item.UnavailableReason, goldSeriesCode) {
				t.Errorf("the reason must name the missing series: %+v", item.UnavailableReason)
			}
		}
	}
}

func TestNumeraireList_WarnsWhenTomanCannotReachDollarAssets(t *testing.T) {
	got := buildNumeraireList(testInstruments(), map[string]dailySeries{}, day(2026, time.January, 1))
	if len(got.Warnings) == 0 || !strings.Contains(got.Warnings[0], usdSeriesCode) {
		t.Fatalf("the partial IRT answer must be explained: %v", got.Warnings)
	}
}

func TestPriceableSymbolsAlwaysIncludeBothConversionSeries(t *testing.T) {
	// usd_return_pct and gold_return_pct appear on every row, so both divisors
	// must be loaded whatever numeraire was asked for.
	got := priceableSymbols(testInstruments())
	for _, want := range []string{usdSeriesCode, goldSeriesCode} {
		found := false
		for _, s := range got {
			if s == want {
				found = true
			}
		}
		if !found {
			t.Errorf("%s missing from %v", want, got)
		}
	}
	for _, unwanted := range []string{"US10Y", "DXY", "RETIRED_SYMBOL"} {
		for _, s := range got {
			if s == unwanted {
				t.Errorf("%s should not be read: it can never appear in the table", unwanted)
			}
		}
	}
}

func TestSortInstrumentsIsDomainThenCode(t *testing.T) {
	rows := []instrumentRow{
		{Code: "XAUUSD", Domain: "global"},
		{Code: "IR_GOLD_18K", Domain: "gold"},
		{Code: "DXY", Domain: "global"},
		{Code: "USD_IRT", Domain: "fx"},
	}
	sortInstruments(rows)
	want := []string{"USD_IRT", "DXY", "XAUUSD", "IR_GOLD_18K"}
	for i, code := range want {
		if rows[i].Code != code {
			t.Fatalf("order = %v, want %v", rows, want)
		}
	}
}

// --- the registry caveat reaches the wire -------------------------------------------------

// The defect this guards: instrumentRef carried `Notes string json:"notes"` at
// depth 1 while performanceItem carried `Notes []string json:"notes"` at depth
// 0. encoding/json resolves a tag collision in favour of the shallower field,
// so the registry caveat was dropped from every item -- silently, with no
// compiler error and no failing test, while the page rendered a notes list that
// looked complete. The assertion is deliberately made against MARSHALLED bytes:
// a struct-field check would have passed throughout the bug's whole life.
func TestPerformanceItem_RegistryCaveatSurvivesMarshalling(t *testing.T) {
	got := performanceFixture(t, "IRT")
	xau := itemFor(t, got, "XAUUSD")

	blob, err := json.Marshal(xau)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !bytes.Contains(blob, []byte("COMEX")) {
		t.Fatalf("the futures-vs-spot caveat never reached the wire:\n%s", blob)
	}

	var decoded struct {
		Code  string   `json:"code"`
		Notes []string `json:"notes"`
	}
	if err := json.Unmarshal(blob, &decoded); err != nil {
		t.Fatalf("notes must marshal as an ARRAY on a performance item: %v\n%s", err, blob)
	}
	if decoded.Code != "XAUUSD" {
		t.Fatalf("the identity block must still marshal: %+v", decoded)
	}
	found := false
	for _, n := range decoded.Notes {
		if strings.Contains(n, "COMEX front-month future") {
			found = true
		}
	}
	if !found {
		t.Fatalf("the caveat must be one of the item's notes: %v", decoded.Notes)
	}
}

// `notes` has ONE arity across this whole feature: an array, on every endpoint.
// It used to be an array on /markets/performance and a scalar string on the
// /relative-value legs, which is a trap rather than a saving -- the first client
// to copy the `(leg.notes ?? []).map(...)` idiom from one page to the other
// would have thrown at runtime on a string.
func TestInstrumentRef_MarshalsItsCaveatAsANotesArray(t *testing.T) {
	blob, err := json.Marshal(refOf(instrumentRow{Code: "XAUUSD", Notes: "COMEX front-month future."}))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var decoded struct {
		Notes []string `json:"notes"`
	}
	if err := json.Unmarshal(blob, &decoded); err != nil {
		t.Fatalf("a standalone instrumentRef must emit notes as an ARRAY: %v\n%s", err, blob)
	}
	if len(decoded.Notes) != 1 || decoded.Notes[0] != "COMEX front-month future." {
		t.Fatalf("notes = %v", decoded.Notes)
	}

	// An instrument with no caveat marshals [] and never null, so a client
	// mapping over it needs no nil check.
	empty, err := json.Marshal(refOf(instrumentRow{Code: "IR_GOLD_18K"}))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !bytes.Contains(empty, []byte(`"notes":[]`)) {
		t.Fatalf("an empty caveat must marshal as []: %s", empty)
	}
}

// --- every return ships the window it was measured over ------------------------------------

// The production shape: the asset has years of history the conversion series
// does not, so usd_return_pct is measured over a strictly shorter window than
// nominal_return_pct in the same row. Publishing the two percentages side by
// side without their windows is what makes the row a lie.
func TestPerformance_CrossNumeraireReturnShipsItsOwnWindow(t *testing.T) {
	series := testSeries()
	series["USD_IRT"] = dailySeries{
		pt(2025, time.January, 1, 50),
		pt(2026, time.January, 1, 80),
	}
	spec, _ := parseNumeraire("IRT")
	from := day(2024, time.January, 1)
	got := buildPerformanceResponse(performanceInputs{
		Query:       performanceQuery{Numeraire: spec, Window: window{From: &from, To: day(2026, time.January, 1)}},
		Instruments: testInstruments(),
		Series:      series,
		AsOf:        day(2026, time.January, 1),
	})
	gold := itemFor(t, got, "IR_GOLD_18K")

	// Nominal: 500 -> 2000 toman over three observations from 2024.
	if v := mustFloat(t, gold.NominalReturnPct, "nominal"); math.Abs(v-300) > 1e-6 {
		t.Fatalf("nominal = %v, want 300", v)
	}
	if gold.CoverageFrom == nil || *gold.CoverageFrom != "2024-01-01" || gold.Observations != 3 {
		t.Fatalf("nominal window = %v..%v over %d obs", gold.CoverageFrom, gold.CoverageTo, gold.Observations)
	}
	// USD: the 2024 day has no prior fx quote, so the leg starts in 2025.
	// 1000/50 = 20 -> 2000/80 = 25 is +25%, measured over TWO observations.
	if v := mustFloat(t, gold.USDReturnPct, "usd"); math.Abs(v-25) > 1e-6 {
		t.Fatalf("usd_return_pct = %v, want 25", v)
	}
	if gold.USDReturnFrom == nil || *gold.USDReturnFrom != "2025-01-01" {
		t.Fatalf("usd_return_from = %v, want 2025-01-01 -- NOT the nominal 2024-01-01", gold.USDReturnFrom)
	}
	if gold.USDReturnTo == nil || *gold.USDReturnTo != "2026-01-01" {
		t.Fatalf("usd_return_to = %v, want 2026-01-01", gold.USDReturnTo)
	}
	if gold.USDReturnObservations != 2 {
		t.Fatalf("usd_return_observations = %d, want 2", gold.USDReturnObservations)
	}
	// A reader comparing +300% with +25% must not have to diff two pairs of
	// dates to discover they describe different spans.
	if !notesMentioning(gold, "a DIFFERENT window") {
		t.Fatalf("the shorter cross-numeraire window must be declared: %v", gold.Notes)
	}
}

// When the two windows really are the same, no divergence note is manufactured.
func TestPerformance_CrossNumeraireWindowIsSilentWhenItMatches(t *testing.T) {
	got := performanceFixture(t, "IRT")
	gold := itemFor(t, got, "IR_GOLD_18K")
	if gold.USDReturnFrom == nil || *gold.USDReturnFrom != *gold.CoverageFrom {
		t.Fatalf("usd_return_from = %v, want the nominal %v", gold.USDReturnFrom, gold.CoverageFrom)
	}
	if notesMentioning(gold, "a DIFFERENT window") {
		t.Errorf("no divergence to declare here: %v", gold.Notes)
	}
}

// --- an asset in its own numeraire ----------------------------------------------------------

func TestPerformance_DefinitionalCellsAreNullNotZero(t *testing.T) {
	got := performanceFixture(t, "GOLD")
	gold := itemFor(t, got, "IR_GOLD_18K")
	if gold.NominalReturnPct != nil || gold.ObservationVolatilityPct != nil ||
		gold.MaxDrawdownPct != nil || gold.RealReturnPct != nil {
		t.Fatalf("gold measured in grams of gold is 1.000 by construction; every derived "+
			"figure must be null rather than a definitional 0.00%%: %+v", gold)
	}
	if !notesMentioning(gold, "by construction") ||
		!notesMentioning(gold, "different facts and must not render the same") {
		t.Fatalf("the definitional row must explain itself: %v", gold.Notes)
	}
	// It is still a row: the identity, the coverage and the 1.000 endpoints are
	// all real facts about the window.
	if gold.Observations != 3 || gold.StartValue == nil || *gold.StartValue != 1 {
		t.Errorf("the identity row still reports its coverage and its 1.000: %+v", gold)
	}

	// The dollar column beside it is a genuine measurement and must survive.
	if gold.USDReturnPct == nil {
		t.Error("usd_return_pct is not definitional and must still be published")
	}
}

func TestPerformance_TheFxSeriesInDollarsIsAlsoDefinitional(t *testing.T) {
	got := performanceFixture(t, "IRT")
	usd := itemFor(t, got, "USD_IRT")
	if usd.USDReturnPct != nil {
		t.Fatalf("USD_IRT in USD = %v, want null: it is 1.000 every day by construction",
			*usd.USDReturnPct)
	}
	if !notesMentioning(usd, "DEFINES this unit") {
		t.Fatalf("the definitional cell must explain itself: %v", usd.Notes)
	}
	// Against gold it is a real measurement, and stays one.
	if v := mustFloat(t, usd.GoldReturnPct, "usd in gold"); math.Abs(v-(-50)) > 1e-6 {
		t.Errorf("USD_IRT in grams of gold = %v, want -50", v)
	}
}

// --- the deflator's identity -----------------------------------------------------------------

func TestPerformance_CPIProvenanceTravelsWithTheRealReturns(t *testing.T) {
	got := performanceFixture(t, "IRT")
	if got.CPIProvenance == nil {
		t.Fatal("a real return published against an invisible deflator is not interpretable")
	}
	p := *got.CPIProvenance
	for _, tc := range []struct{ name, got, want string }{
		{"code", p.Code, cpiSeriesCode},
		{"base_period", p.BasePeriod, "2010=100"},
		{"measure", p.Measure, "index"},
		{"frequency", p.Frequency, "annual"},
		{"quality_tier", p.QualityTier, "official_mirror"},
	} {
		if tc.got != tc.want {
			t.Errorf("cpi_provenance.%s = %q, want %q", tc.name, tc.got, tc.want)
		}
	}
	if !strings.Contains(p.Notes, "Statistical Centre of Iran") {
		t.Errorf("the catalog's own caveat must travel: %q", p.Notes)
	}
	if p.CoverageTo == nil || *p.CoverageTo != "2025-01-01" {
		t.Errorf("cpi_provenance.coverage_to = %v, want 2025-01-01", p.CoverageTo)
	}
	// The two flat fields stay exactly where they were.
	if got.CPISeries == nil || *got.CPISeries != cpiSeriesCode {
		t.Errorf("cpi_series = %v", got.CPISeries)
	}

	blob, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	for _, field := range []string{`"base_period"`, `"measure"`, `"frequency"`, `"quality_tier"`} {
		if !bytes.Contains(blob, []byte(field)) {
			t.Errorf("%s never reached the wire", field)
		}
	}
}

func TestPerformance_NoCPIMeansNoProvenanceBlock(t *testing.T) {
	got := buildPerformanceResponse(performanceInputs{
		Query:       performanceQuery{Numeraire: numeraireSpecs[0], Window: window{To: day(2026, time.January, 1)}},
		Instruments: testInstruments(),
		Series:      testSeries(),
		CPIError:    "No real returns: this deployment does not carry the CPI series.",
		AsOf:        day(2026, time.January, 1),
	})
	if got.CPIProvenance != nil {
		t.Fatal("a deployment with no deflator must not describe one")
	}
}

// --- an unpriceable instrument is refused, never treated as an identity -------------------------

func TestBuildPerformanceItem_RefusesAnUnpriceableInstrument(t *testing.T) {
	// buildPerformanceResponse filters these out first, so this exercises the
	// guard directly. The defect it replaces: `steps, _ := conversionSteps(...)`
	// discarded the ok flag, and a nil chain is an IDENTITY conversion -- DXY's
	// index points would have been published as though they were already toman.
	dxy := instrumentRow{Code: "DXY", Domain: "global", QuoteCurrency: quoteINDEX,
		Unit: "index", Enabled: true}
	spec, _ := parseNumeraire("IRT")
	from := day(2024, time.January, 1)
	item := buildPerformanceItem(dxy, performanceInputs{
		Query:  performanceQuery{Numeraire: spec, Window: window{From: &from, To: day(2026, time.January, 1)}},
		Series: testSeries(),
		AsOf:   day(2026, time.January, 1),
	})
	if item.StartValue != nil || item.NominalReturnPct != nil || item.Observations != 0 {
		t.Fatalf("an index level must not be published as though it were toman: %+v", item)
	}
	if !notesMentioning(item, "not a price in a currency") {
		t.Fatalf("the refusal must be stated: %v", item.Notes)
	}
}

// --- the numeraire menu's contract ----------------------------------------------------------------

func TestNumeraireList_PublishesADefaultAndLabels(t *testing.T) {
	got := buildNumeraireList(testInstruments(), testSeries(), day(2026, time.January, 1))
	if got.Default != defaultNumeraire {
		t.Fatalf("default = %q, want %q", got.Default, defaultNumeraire)
	}
	// The default must be one of the offered keys, and an available one: a
	// fallback a client cannot select is not a fallback.
	var fallback *numeraireItem
	for i := range got.Items {
		if got.Items[i].Key == got.Default {
			fallback = &got.Items[i]
		}
	}
	if fallback == nil || !fallback.Available {
		t.Fatalf("default %q is not an available item", got.Default)
	}

	blob, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	// The identifier is `key`, the backing instrument is `series`, notes is an
	// ARRAY, and there is a top-level default. A menu built against `code`,
	// `series_code` or a scalar `note` is reading fields that do not exist.
	var decoded struct {
		Default string `json:"default"`
		Items   []struct {
			Key               string   `json:"key"`
			LabelEN           string   `json:"label_en"`
			LabelFA           string   `json:"label_fa"`
			NameEN            string   `json:"name_en"`
			Available         bool     `json:"available"`
			UnavailableReason *string  `json:"unavailable_reason"`
			IsIdentity        bool     `json:"is_identity"`
			Series            *string  `json:"series"`
			Notes             []string `json:"notes"`
		} `json:"items"`
	}
	if err := json.Unmarshal(blob, &decoded); err != nil {
		t.Fatalf("the published shape does not decode: %v\n%s", err, blob)
	}
	if decoded.Default != "IRT" {
		t.Fatalf("default = %q", decoded.Default)
	}
	for _, it := range decoded.Items {
		if it.Key == "" || it.LabelEN == "" || it.LabelFA == "" || len(it.Notes) == 0 {
			t.Fatalf("every item needs a key, both labels and its notes array: %+v", it)
		}
		if it.LabelEN != it.NameEN {
			t.Errorf("label_en and name_en must be the same string: %q vs %q", it.LabelEN, it.NameEN)
		}
	}
	if bytes.Contains(blob, []byte(`"series_code"`)) || bytes.Contains(blob, []byte(`"code"`)) {
		t.Errorf("this endpoint has never emitted code/series_code:\n%s", blob)
	}
}

// --- the volatility field is named for what it measures ------------------------------

// The defect: a per-OBSERVATION sigma shipped under a per-DAY name. These
// series have holes, so most of its steps span more than one calendar day and
// "daily" overstated the per-day figure while claiming a unit it does not have.
// The fix is the name, not an annualisation and not an interpolation.
func TestPerformance_VolatilityFieldIsNamedPerObservation(t *testing.T) {
	got := performanceFixture(t, "IRT")
	blob, err := json.Marshal(itemFor(t, got, "IR_GOLD_18K"))
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	if !bytes.Contains(blob, []byte(`"observation_volatility_pct"`)) {
		t.Fatalf("the field must be named for what it measures:\n%s", blob)
	}
	if bytes.Contains(blob, []byte(`"daily_volatility_pct"`)) {
		t.Fatalf("the per-day name must be gone, not duplicated:\n%s", blob)
	}
	if bytes.Contains(blob, []byte("annualis")) && !bytes.Contains(blob, []byte("not annualised")) &&
		!bytes.Contains(blob, []byte("NOT annualised")) {
		t.Errorf("nothing here is annualised and the note must say so:\n%s", blob)
	}
}
