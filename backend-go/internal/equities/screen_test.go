package equities

// Tests for GET /api/v1/stocks/screen.
//
// Everything here runs against the pure build* functions, with no database and
// no clock: the fixtures are stored bars and roster rows, the numeraire engine
// is the REAL internal/relvalue converter built over explicit quotes, and the
// assertions are about arithmetic and refusals rather than about plumbing.
//
// The four things these tests exist to stop are, in order of how much damage
// each would do:
//
//  1. A return computed from RAW closes. TestScreenReturnsComeFromAdjustedCloses
//     builds the shape that makes it obvious: a share whose raw close FELL 40%
//     over a capital increase and whose adjusted close ROSE 20%.
//  2. An annualised volatility wearing a per-session label.
//  3. A metric that could not be computed arriving as 0 instead of null.
//  4. A symbol vanishing from the universe instead of appearing in `excluded`.

import (
	"encoding/json"
	"math"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/relvalue"
)

// --- fixtures -------------------------------------------------------------------

// screenable roster rows: enabled, with bars, and a validated verdict. Anything
// short of all three is excluded from the screen by design, and the exclusion
// tests build those cases explicitly.
func rosterRow(symbol, name, sectorCode, sectorFA string, barCount int) stockRow {
	return stockRow{
		InsCode:           "ins-" + symbol,
		SymbolFA:          symbol,
		NameFA:            name,
		Market:            "bourse",
		Board:             "بازار اول (تابلوی اصلی) بورس",
		SectorCode:        sectorCode,
		SectorFA:          sectorFA,
		FirstBar:          ptr(day(2007, time.March, 11)),
		LastBar:           ptr(day(2026, time.September, 9)),
		BarCount:          barCount,
		Enabled:           true,
		AdjustmentVersion: ptr("priceYesterday-chain-v1"),
		AdjustmentStatus:  ptr("validated"),
		ActionsApplied:    ptr(31),
		Reopenings:        ptr(0),
		AdjustedFirstBar:  ptr(day(2007, time.March, 11)),
	}
}

// kachad is the roster row this whole endpoint's `excluded` block exists for:
// seeded DISABLED because the validation gate refuses its adjustment, with the
// measurement recorded in its notes.
func kachad() stockRow {
	return stockRow{
		InsCode:    "18027801615184692",
		SymbolFA:   "کچاد",
		NameFA:     "معدنی‌وصنعتی‌چادرملو",
		Market:     "bourse",
		Board:      "بازار اول (تابلوی اصلی) بورس",
		SectorCode: "13",
		SectorFA:   "استخراج کانه های فلزی",
		BarCount:   0,
		Enabled:    false,
		Notes: "SEEDED DISABLED, deliberately, and this is the measurement rather than a " +
			"policy: 5,304 bars, 47 actions, and the adjusted series still contains -60.4% " +
			"on 2007-01-09 and +152.7% on 2007-01-10. A single block trade of 227,000,010 " +
			"shares set the official closing price to 5,576 against a market of ~14,090.",
	}
}

func bar(d time.Time, finalClose float64, volume, trades int64, value, factor float64) screenBarRow {
	return screenBarRow{
		TradeDate: d, FinalClose: finalClose, Volume: volume,
		TradeCount: trades, Value: value, Factor: factor,
	}
}

// haltedBar is what TSETMC serves for a suspended session: the bar is stored,
// final_close carries the reference price, and volume and trade_count are zero.
func haltedBar(d time.Time, finalClose, factor float64) screenBarRow {
	return screenBarRow{TradeDate: d, FinalClose: finalClose, Factor: factor}
}

func testSectors() []sectorOption {
	return []sectorOption{
		{Code: "13", NameFA: "استخراج کانه های فلزی", Count: 1},
		{Code: "27", NameFA: "فلزات اساسی", Count: 2},
	}
}

// screenConverter is the REAL numeraire engine over explicit quotes. Using it
// rather than a stand-in is deliberate: a fake here would assert this
// package's belief about the carry-forward rule, which is exactly the second
// implementation internal/relvalue/export.go exists to prevent.
func screenConverter() *relvalue.Converter {
	return relvalue.NewConverterFromQuotes(day(2026, time.March, 10), map[string][]relvalue.Point{
		relvalue.SeriesUSD: {
			{Day: day(2026, time.March, 2), Value: 900_000},
			{Day: day(2026, time.March, 6), Value: 1_100_000},
		},
		relvalue.SeriesGold18K: {
			{Day: day(2026, time.March, 2), Value: 8_000_000},
			{Day: day(2026, time.March, 6), Value: 8_400_000},
		},
	})
}

func mustQuery(t *testing.T, raw string) screenQuery {
	t.Helper()
	values, err := url.ParseQuery(raw)
	if err != nil {
		t.Fatalf("bad fixture query %q: %v", raw, err)
	}
	q, perr := parseScreenQuery(values, testSectors(), day(2026, time.March, 10))
	if perr != nil {
		t.Fatalf("unexpected refusal for %q: %s", raw, perr.Message)
	}
	return q
}

// --- query parsing and refusals ---------------------------------------------------

func TestScreenQueryDefaults(t *testing.T) {
	q := mustQuery(t, "")
	if q.Period != "1y" {
		t.Errorf("period default = %q, want 1y", q.Period)
	}
	if q.From == nil || !q.From.Equal(day(2025, time.March, 10)) {
		t.Errorf("1y from 2026-03-10 = %v, want 2025-03-10 (calendar years, not 365 days)", q.From)
	}
	if q.Numeraire != numeraireIRR {
		t.Errorf("numeraire default = %q, want IRR — the exchange's own unit", q.Numeraire)
	}
	if q.Sort.Key != "avg_value" || !q.Desc {
		t.Errorf("sort default = %q desc=%v, want avg_value desc", q.Sort.Key, q.Desc)
	}
	if q.Limit != defaultScreenLimit {
		t.Errorf("limit default = %d, want %d", q.Limit, defaultScreenLimit)
	}
	if q.Limit < 20 {
		t.Error("the default limit must exceed the roster, so an absent ?limit= never truncates")
	}
}

func TestScreenPeriodMaxHasNoLowerBound(t *testing.T) {
	// How far back "max" reaches is a property of each instrument, not of the
	// request, and is reported per row as metrics_from.
	q := mustQuery(t, "period=max")
	if q.From != nil {
		t.Errorf("period=max must leave from nil, got %v", q.From)
	}
}

func TestScreenRefusesEveryBadParameterAndNeverClamps(t *testing.T) {
	cases := []struct {
		name    string
		query   string
		mustSay []string
	}{
		{"unknown period", "period=2y", []string{"period", "1m, 3m, 6m, 1y, max", "2y"}},
		{"unknown numeraire", "numeraire=EUR", []string{"numeraire", "IRR, USD, GOLD", "EUR"}},
		{"unknown sector", "sector=99", []string{"sector", "99", "roster"}},
		{"unknown sort", "sort=pe", []string{"sort", "pe", "avg_value"}},
		{"unknown order", "sort=return&order=sideways", []string{"order", "asc", "desc"}},
		{"limit zero", "limit=0", []string{"limit", "between 1 and 200"}},
		{"limit above the cap", "limit=201", []string{"limit", "between 1 and 200"}},
		{"limit not a number", "limit=lots", []string{"limit", "lots"}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			values, _ := url.ParseQuery(c.query)
			_, perr := parseScreenQuery(values, testSectors(), day(2026, time.March, 10))
			if perr == nil {
				t.Fatalf("%q must be refused, not clamped or substituted", c.query)
			}
			for _, want := range c.mustSay {
				if !strings.Contains(perr.Message, want) {
					t.Errorf("refusal %q must mention %q", perr.Message, want)
				}
			}
			if len(perr.Details) == 0 {
				t.Error("a refusal must carry machine-readable details")
			}
		})
	}
}

func TestScreenSectorRefusalNamesTheRealSectors(t *testing.T) {
	// The vocabulary comes from the roster, so the message names what this
	// deployment carries today rather than a list compiled into the binary.
	values, _ := url.ParseQuery("sector=99")
	_, perr := parseScreenQuery(values, testSectors(), day(2026, time.March, 10))
	if perr == nil {
		t.Fatal("an unknown sector must be refused")
	}
	body, err := json.Marshal(perr.Details["supported"])
	if err != nil {
		t.Fatalf("details must marshal: %v", err)
	}
	if !strings.Contains(string(body), "فلزات اساسی") {
		t.Errorf("supported sectors must carry the Persian names, got %s", body)
	}
}

func TestScreenOrderOverridesTheColumnsNaturalDirection(t *testing.T) {
	if q := mustQuery(t, "sort=drawdown"); q.Desc {
		t.Error("drawdown is negative, so its natural order is ascending: worst first")
	}
	if q := mustQuery(t, "sort=drawdown&order=desc"); !q.Desc {
		t.Error("an explicit ?order=desc must win")
	}
	if q := mustQuery(t, "sort=return&order=asc"); q.Desc {
		t.Error("an explicit ?order=asc must win")
	}
}

func TestScreenNumeraireRefusalWhenTheDeploymentCannotBackIt(t *testing.T) {
	// A page of nulls cannot be told apart from a market that did nothing, so
	// the BASIS is refused outright.
	conv := relvalue.NewConverterFromQuotes(day(2026, time.March, 10), nil)
	perr := screenNumeraireRefusal(numeraireGold, conv)
	if perr == nil {
		t.Fatal("GOLD with no stored gold series must be refused")
	}
	if !strings.Contains(perr.Message, "IR_GOLD_18K") {
		t.Errorf("the refusal must name the missing series, got %q", perr.Message)
	}
	if screenNumeraireRefusal(numeraireIRR, conv) != nil {
		t.Error("IRR is the exchange's own unit and can never be unbacked")
	}
	if screenNumeraireRefusal(numeraireUSD, screenConverter()) != nil {
		t.Error("USD is backed in this fixture and must not be refused")
	}
}

// --- the arithmetic ---------------------------------------------------------------

func TestScreenReturnsComeFromAdjustedCloses(t *testing.T) {
	// THE test. A capital increase halves the raw price without destroying a
	// rial of value; the exchange's printed closes fall 40% and the adjusted
	// series rises 20%. A screener sorting on the first ranks companies by how
	// recently each did a capital increase — فولاد is x1.52 raw against
	// x907.86 adjusted over the same nineteen years.
	bars := []screenBarRow{
		bar(day(2026, time.January, 5), 1000, 1_000, 100, 1_000_000, 0.5),
		bar(day(2026, time.February, 5), 1000, 1_000, 100, 1_000_000, 0.5),
		bar(day(2026, time.March, 5), 600, 1_000, 100, 1_000_000, 1.0),
	}
	q := mustQuery(t, "period=max")
	latest := bars[2]
	item := buildScreenItem(rosterRow("فولاد", "فولاد مبارکه اصفهان", "27", "فلزات اساسی", 3),
		bars, &latest, q, screenConverter())

	if item.ReturnPct == nil {
		t.Fatalf("expected a return, got null: %v", item.ReturnReason)
	}
	if math.Abs(*item.ReturnPct-20) > 1e-9 {
		t.Fatalf("adjusted return = %v%%, want +20%% (500 -> 600)", *item.ReturnPct)
	}
	if math.Abs(*item.ReturnPct-(-40)) < 1e-9 {
		t.Fatal("that is the RAW return: the screener is ranking capital increases")
	}
	if item.ReturnReason != nil {
		t.Errorf("a computed metric must carry no reason, got %q", *item.ReturnReason)
	}
	// last_close stays RAW, and says so.
	if item.LastClose == nil || *item.LastClose != 600 {
		t.Errorf("last_close = %v, want the raw 600", item.LastClose)
	}
	if !strings.Contains(item.LastCloseBasis, "raw") {
		t.Errorf("last_close_basis must say the price is raw, got %q", item.LastCloseBasis)
	}
}

func TestScreenVolatilityIsPerSessionAndNotAnnualised(t *testing.T) {
	// Session returns of +10%, -10%, +10%. The sample standard deviation of
	// those three is 11.547005%, and anything near 183% is that figure
	// multiplied by sqrt(252) — a trading-day calendar Tehran does not keep.
	bars := []screenBarRow{
		bar(day(2026, time.March, 2), 100, 10, 1, 1000, 1.0),
		bar(day(2026, time.March, 3), 110, 10, 1, 1000, 1.0),
		bar(day(2026, time.March, 4), 99, 10, 1, 1000, 1.0),
		bar(day(2026, time.March, 5), 108.9, 10, 1, 1000, 1.0),
	}
	q := mustQuery(t, "period=max")
	item := buildScreenItem(rosterRow("خودرو", "ایران‌ خودرو", "34", "خودرو و ساخت قطعات", 4),
		bars, nil, q, screenConverter())

	if item.VolatilityPct == nil {
		t.Fatalf("expected a volatility, got null: %v", item.VolatilityReason)
	}
	want := 11.547005
	if math.Abs(*item.VolatilityPct-want) > 1e-6 {
		t.Fatalf("volatility = %v%%, want %v%% (sample sd of +10/-10/+10)", *item.VolatilityPct, want)
	}
	if *item.VolatilityPct > 100 {
		t.Fatal("this figure has been annualised; Tehran trades Saturday to Wednesday and " +
			"there is no single N for which sqrt(N) is right")
	}
}

func TestScreenVolatilityOfASingleReturnIsNullNotZero(t *testing.T) {
	// A single return carries no information about dispersion at all. A stored
	// 0.00 would read as "this share did not move", which is a claim nobody
	// measured.
	bars := []screenBarRow{
		bar(day(2026, time.March, 2), 100, 10, 1, 1000, 1.0),
		bar(day(2026, time.March, 3), 110, 10, 1, 1000, 1.0),
	}
	item := buildScreenItem(rosterRow("ذوب", "ذوب آهن اصفهان", "27", "فلزات اساسی", 2),
		bars, nil, mustQuery(t, "period=max"), screenConverter())

	if item.VolatilityPct != nil {
		t.Fatalf("volatility = %v, want null", *item.VolatilityPct)
	}
	if item.VolatilityReason == nil || !strings.Contains(*item.VolatilityReason, "did not move") {
		t.Fatalf("the null must carry its reason, got %v", item.VolatilityReason)
	}
	// The return over the same two sessions IS computable, and is.
	if item.ReturnPct == nil || math.Abs(*item.ReturnPct-10) > 1e-9 {
		t.Errorf("return = %v, want +10%%", item.ReturnPct)
	}
}

func TestScreenMaxDrawdownIsPeakToTrough(t *testing.T) {
	// Not indicators.DrawdownPct, which answers "how far below its trailing
	// high is it RIGHT NOW". A share that fell 50% and half-recovered has a max
	// drawdown of -50% and a current drawdown of -25%.
	bars := []screenBarRow{
		bar(day(2026, time.March, 2), 100, 10, 1, 1000, 1.0),
		bar(day(2026, time.March, 3), 120, 10, 1, 1000, 1.0),
		bar(day(2026, time.March, 4), 60, 10, 1, 1000, 1.0),
		bar(day(2026, time.March, 5), 90, 10, 1, 1000, 1.0),
	}
	item := buildScreenItem(rosterRow("شپنا", "پالایش نفت اصفهان", "23", "فراورده های نفتی", 4),
		bars, nil, mustQuery(t, "period=max"), screenConverter())

	if item.MaxDrawdownPct == nil {
		t.Fatalf("expected a drawdown, got null: %v", item.MaxDrawdownReason)
	}
	if math.Abs(*item.MaxDrawdownPct-(-50)) > 1e-9 {
		t.Fatalf("max drawdown = %v%%, want -50%% (120 -> 60)", *item.MaxDrawdownPct)
	}
}

func TestScreenSingleSessionYieldsNullMetricsWithReasons(t *testing.T) {
	// Rule 3, in the shape it actually arrives in: a window that contains one
	// session of an instrument. Every period metric is null, each with its own
	// reason, and not one of them is 0.
	bars := []screenBarRow{bar(day(2026, time.March, 5), 1000, 10, 1, 5000, 1.0)}
	item := buildScreenItem(rosterRow("نوری", "پتروشیمی نوری", "44", "محصولات شیمیایی", 1),
		bars, &bars[0], mustQuery(t, "period=1m"), screenConverter())

	for _, c := range []struct {
		name   string
		value  *float64
		reason *string
	}{
		{"return_pct", item.ReturnPct, item.ReturnReason},
		{"volatility_pct", item.VolatilityPct, item.VolatilityReason},
		{"max_drawdown_pct", item.MaxDrawdownPct, item.MaxDrawdownReason},
	} {
		if c.value != nil {
			t.Errorf("%s = %v, want null", c.name, *c.value)
		}
		if c.reason == nil || *c.reason == "" {
			t.Errorf("%s is null and must carry a reason", c.name)
		}
	}
	// Liquidity over one traded session is a real average of one session.
	if item.AvgValue == nil || *item.AvgValue != 5000 {
		t.Errorf("avg_value = %v, want 5000", item.AvgValue)
	}
}

func TestScreenExcludesHaltedSessionsFromEveryFigure(t *testing.T) {
	// A halted bar's final_close is the carried reference price, not a print.
	// Averaging its zeros into the liquidity columns would report lower
	// turnover for a suspended share than for a thinly traded one, and its 0%
	// "return" would dampen the volatility by however often the exchange
	// suspended the instrument.
	bars := []screenBarRow{
		bar(day(2026, time.March, 2), 100, 1_000, 10, 100_000, 1.0),
		haltedBar(day(2026, time.March, 3), 100, 1.0),
		haltedBar(day(2026, time.March, 4), 100, 1.0),
		bar(day(2026, time.March, 5), 110, 3_000, 30, 300_000, 1.0),
	}
	item := buildScreenItem(rosterRow("شپنا", "پالایش نفت اصفهان", "23", "فراورده های نفتی", 4),
		bars, &bars[3], mustQuery(t, "period=max"), screenConverter())

	if item.SessionsInPeriod != 4 || item.TradedSessions != 2 || item.HaltedSessions != 2 {
		t.Fatalf("sessions=%d traded=%d halted=%d, want 4/2/2",
			item.SessionsInPeriod, item.TradedSessions, item.HaltedSessions)
	}
	if item.MetricsObservations != 2 {
		t.Errorf("metrics_observations = %d, want 2 traded sessions", item.MetricsObservations)
	}
	if item.AvgValue == nil || *item.AvgValue != 200_000 {
		t.Errorf("avg_value = %v, want 200,000 — the mean over TRADED sessions", item.AvgValue)
	}
	if item.AvgVolume == nil || *item.AvgVolume != 2_000 {
		t.Errorf("avg_volume = %v, want 2,000", item.AvgVolume)
	}
	found := false
	for _, n := range item.Notes {
		if strings.Contains(n, "halted") {
			found = true
		}
	}
	if !found {
		t.Error("the row must say that halted sessions were excluded")
	}
}

func TestScreenLiquidityIsNullWhenNothingTraded(t *testing.T) {
	bars := []screenBarRow{
		haltedBar(day(2026, time.March, 3), 100, 1.0),
		haltedBar(day(2026, time.March, 4), 100, 1.0),
	}
	item := buildScreenItem(rosterRow("ذوب", "ذوب آهن اصفهان", "27", "فلزات اساسی", 2),
		bars, nil, mustQuery(t, "period=max"), screenConverter())

	if item.AvgValue != nil || item.AvgVolume != nil || item.AvgTradeCount != nil {
		t.Fatal("no traded session means no average, and never a zero")
	}
	if item.LiquidityReason == nil || !strings.Contains(*item.LiquidityReason, "no traded session") {
		t.Fatalf("the null must carry its reason, got %v", item.LiquidityReason)
	}
}

// --- the numeraire ------------------------------------------------------------------

func TestScreenReturnInDollarsUsesTheNumeraireEngine(t *testing.T) {
	// The whole reason a numeraire exists in this system: a Tehran share that
	// rose 22.2% in rial went nowhere in dollars, because the rial fell by
	// exactly as much over the same window.
	bars := []screenBarRow{
		bar(day(2026, time.March, 2), 18_000_000, 100, 10, 1_000_000, 1.0),
		bar(day(2026, time.March, 6), 22_000_000, 100, 10, 1_000_000, 1.0),
	}
	latest := bars[1]
	meta := rosterRow("فملی", "ملی‌ صنایع‌ مس‌ ایران‌", "27", "فلزات اساسی", 2)

	inRial := buildScreenItem(meta, bars, &latest, mustQuery(t, "period=max"), screenConverter())
	if inRial.ReturnPct == nil || math.Abs(*inRial.ReturnPct-22.222222) > 1e-5 {
		t.Fatalf("rial return = %v, want +22.222222%%", inRial.ReturnPct)
	}
	if inRial.Conversion != nil {
		t.Error("numeraire=IRR converts nothing, so the conversion block must be null")
	}

	inUSD := buildScreenItem(meta, bars, &latest,
		mustQuery(t, "period=max&numeraire=USD"), screenConverter())
	if inUSD.ReturnPct == nil {
		t.Fatalf("expected a USD return, got null: %v", inUSD.ReturnReason)
	}
	if math.Abs(*inUSD.ReturnPct) > 1e-9 {
		t.Fatalf("USD return = %v%%, want 0%% — 1,800,000/900,000 and 2,200,000/1,100,000 "+
			"are both 2 USD", *inUSD.ReturnPct)
	}
	if inUSD.MetricsNumeraire != "USD" {
		t.Errorf("metrics_numeraire = %q, want USD", inUSD.MetricsNumeraire)
	}
	if inUSD.Conversion == nil || len(inUSD.Conversion.Chain) != 1 ||
		inUSD.Conversion.Chain[0] != relvalue.SeriesUSD {
		t.Errorf("the conversion block must name USD_IRT, got %+v", inUSD.Conversion)
	}
}

func TestScreenNumeraireDropsUncoveredSessionsAndCountsThem(t *testing.T) {
	// 2026-03-01 is before the fixture's first USD_IRT quote. It is DROPPED,
	// not back-filled, and the row says so in its conversion block, its
	// metrics_from and its notes.
	bars := []screenBarRow{
		bar(day(2026, time.March, 1), 10_000_000, 100, 10, 1_000_000, 1.0),
		bar(day(2026, time.March, 2), 18_000_000, 100, 10, 1_000_000, 1.0),
		bar(day(2026, time.March, 6), 22_000_000, 100, 10, 1_000_000, 1.0),
	}
	latest := bars[2]
	item := buildScreenItem(rosterRow("فملی", "ملی مس", "27", "فلزات اساسی", 3),
		bars, &latest, mustQuery(t, "period=max&numeraire=USD"), screenConverter())

	if item.Conversion == nil || item.Conversion.DroppedNoPriorQuote != 1 {
		t.Fatalf("expected one dropped session, got %+v", item.Conversion)
	}
	if item.MetricsObservations != 2 {
		t.Errorf("metrics_observations = %d, want 2", item.MetricsObservations)
	}
	if item.MetricsFrom == nil || *item.MetricsFrom != "2026-03-02" {
		t.Errorf("metrics_from = %v, want 2026-03-02 — the window the figures were actually "+
			"measured over, not the one that was asked for", item.MetricsFrom)
	}
	// The instrument's own session count is unchanged: three sessions were
	// stored, and the conversion is what could not carry one of them.
	if item.SessionsInPeriod != 3 || item.TradedSessions != 3 {
		t.Errorf("sessions=%d traded=%d, want 3/3", item.SessionsInPeriod, item.TradedSessions)
	}
	found := false
	for _, n := range item.Notes {
		if strings.Contains(n, "own future") {
			found = true
		}
	}
	if !found {
		t.Error("the row must say why the dropped session was not filled in")
	}
}

func TestScreenPriceColumnsCrossRialToTomanExactlyOnce(t *testing.T) {
	// The factor-of-ten this endpoint is most able to get wrong. TSETMC quotes
	// RIAL; internal/relvalue is a toman engine. 22,000,000 rials is 2,200,000
	// toman, which at 1,100,000 toman/USD is 2 USD — not 20, and not 0.2.
	bars := []screenBarRow{
		bar(day(2026, time.March, 2), 18_000_000, 100, 10, 1_000_000, 1.0),
		bar(day(2026, time.March, 6), 22_000_000, 100, 10, 1_000_000, 1.0),
	}
	latest := bars[1]
	item := buildScreenItem(rosterRow("فملی", "ملی مس", "27", "فلزات اساسی", 2),
		bars, &latest, mustQuery(t, "period=max"), screenConverter())

	if item.PriceUSD == nil {
		t.Fatalf("expected a USD price, got null: %v", item.PriceUSDReason)
	}
	if math.Abs(*item.PriceUSD-2.0) > 1e-9 {
		t.Fatalf("price_usd = %v, want 2.0", *item.PriceUSD)
	}
	if item.PriceGoldGrams == nil {
		t.Fatalf("expected a gold price, got null: %v", item.PriceGoldGramsReason)
	}
	// 2,200,000 toman / 8,400,000 toman per gram.
	if math.Abs(*item.PriceGoldGrams-0.261904761905) > 1e-9 {
		t.Fatalf("price_gold_grams = %v, want 0.261904761905", *item.PriceGoldGrams)
	}
	// Both columns are published whatever ?numeraire= asked for: they are
	// columns, not the basis.
	if item.PriceUSDReason != nil || item.PriceGoldGramsReason != nil {
		t.Error("a computed price must carry no reason")
	}
	if item.PriceUSDCarriedForward {
		t.Error("2026-03-06 has its own USD_IRT quote")
	}
}

func TestScreenPriceIsNullWithAReasonWhenTheNumeraireCannotReach(t *testing.T) {
	// A share that last traded before the conversion series began. The value is
	// withheld rather than priced with a rate that did not exist yet.
	bars := []screenBarRow{bar(day(2026, time.February, 20), 10_000_000, 100, 10, 1_000, 1.0)}
	item := buildScreenItem(rosterRow("ذوب", "ذوب آهن اصفهان", "27", "فلزات اساسی", 1),
		bars, &bars[0], mustQuery(t, "period=max"), screenConverter())

	if item.PriceUSD != nil {
		t.Fatalf("price_usd = %v, want null", *item.PriceUSD)
	}
	if item.PriceUSDReason == nil {
		t.Fatal("a null price must carry a reason")
	}
	if !strings.Contains(*item.PriceUSDReason, "carried FORWARD only") {
		t.Errorf("the reason must state the rule, got %q", *item.PriceUSDReason)
	}
	if item.PriceUSDRateDate != nil {
		t.Error("no quote was used, so no rate date may be published")
	}
}

// --- the whole response ---------------------------------------------------------------

func TestScreenExcludedSymbolsNeverVanish(t *testing.T) {
	refused := rosterRow("شتران", "پالایش نفت تهران", "23", "فراورده های نفتی", 2371)
	refused.AdjustmentStatus = ptr("refused")
	refused.RefusalReason = ptr("the adjusted series still contains a -58.4% artefact on 2014-03-16.")

	neverIngested := rosterRow("همراه", "شرکت ارتباطات سیار ایران", "64", "مخابرات", 3148)
	neverIngested.AdjustmentStatus = nil
	neverIngested.AdjustmentVersion = nil

	empty := rosterRow("کهربا", "کهربا", "27", "فلزات اساسی", 0)

	ok := rosterRow("فولاد", "فولاد مبارکه اصفهان", "27", "فلزات اساسی", 4636)
	out := buildScreenResponse(screenInputs{
		Query:  mustQuery(t, "period=max"),
		Roster: []stockRow{ok, kachad(), refused, neverIngested, empty},
		Window: map[string][]screenBarRow{
			ok.InsCode: {
				bar(day(2026, time.March, 2), 100, 10, 1, 1000, 1.0),
				bar(day(2026, time.March, 5), 110, 10, 1, 1000, 1.0),
			},
		},
		Latest:    map[string]screenBarRow{},
		Converter: screenConverter(),
		Sectors:   testSectors(),
	})

	if out.Count != 1 || out.Items[0].Symbol != "فولاد" {
		t.Fatalf("expected exactly فولاد in items, got %d rows", out.Count)
	}
	if out.ExcludedCount != 4 {
		t.Fatalf("expected 4 excluded symbols, got %d", out.ExcludedCount)
	}

	bySymbol := map[string]excludedItem{}
	for _, e := range out.Excluded {
		bySymbol[e.Symbol] = e
	}
	kc, ok2 := bySymbol["کچاد"]
	if !ok2 {
		t.Fatal("کچاد is disabled by the validation gate and MUST appear in `excluded`")
	}
	if kc.ReasonCode != reasonDisabled {
		t.Errorf("کچاد reason_code = %q, want %q", kc.ReasonCode, reasonDisabled)
	}
	if !strings.Contains(kc.Notes, "227,000,010") {
		t.Error("the roster's own recorded measurement must travel with the exclusion")
	}
	if !strings.Contains(kc.Reason, "disabled") {
		t.Errorf("کچاد reason = %q", kc.Reason)
	}

	if got := bySymbol["شتران"]; got.ReasonCode != reasonRefused ||
		!strings.Contains(got.Reason, "-58.4%") {
		t.Errorf("a refused adjustment must carry the gate's own reason, got %+v", got)
	}
	if got := bySymbol["همراه"]; got.ReasonCode != reasonNeverIngested {
		t.Errorf("همراه reason_code = %q, want %q", got.ReasonCode, reasonNeverIngested)
	}
	if got := bySymbol["کهربا"]; got.ReasonCode != reasonNoBars {
		t.Errorf("کهربا reason_code = %q, want %q", got.ReasonCode, reasonNoBars)
	}
	if !strings.Contains(out.ExcludedNote, "whole universe") {
		t.Errorf("the block must explain itself, got %q", out.ExcludedNote)
	}
}

func TestScreenSectorFilterIsNotReportedAsAnExclusion(t *testing.T) {
	// Narrowing the universe is the caller's own decision. Reporting its filter
	// back as an omission would bury the symbols that genuinely could not be
	// screened.
	metals := rosterRow("فولاد", "فولاد مبارکه اصفهان", "27", "فلزات اساسی", 4636)
	out := buildScreenResponse(screenInputs{
		Query:  mustQuery(t, "period=max&sector=27"),
		Roster: []stockRow{metals, kachad()},
		Window: map[string][]screenBarRow{
			metals.InsCode: {
				bar(day(2026, time.March, 2), 100, 10, 1, 1000, 1.0),
				bar(day(2026, time.March, 5), 110, 10, 1, 1000, 1.0),
			},
		},
		Latest:    map[string]screenBarRow{},
		Converter: screenConverter(),
		Sectors:   testSectors(),
	})
	if out.Count != 1 {
		t.Fatalf("expected 1 row in sector 27, got %d", out.Count)
	}
	// کچاد is sector 13 and was filtered out, not excluded.
	if out.ExcludedCount != 0 {
		t.Fatalf("a sector filter is a selection, not an exclusion; got %d excluded",
			out.ExcludedCount)
	}
	if out.Sector == nil || *out.Sector != "27" {
		t.Errorf("the response must echo the sector it was served for, got %v", out.Sector)
	}
}

func TestScreenSortsNullsLastInBothDirections(t *testing.T) {
	items := []screenItem{
		{Symbol: "b", ReturnPct: floatOf(5)},
		{Symbol: "a", ReturnPct: nil},
		{Symbol: "c", ReturnPct: floatOf(-3)},
	}
	spec, _ := lookupScreenSort("return")

	sortScreenItems(items, spec, true)
	if got := []string{items[0].Symbol, items[1].Symbol, items[2].Symbol}; got[0] != "b" ||
		got[1] != "c" || got[2] != "a" {
		t.Fatalf("desc order = %v, want [b c a] with the null last", got)
	}
	sortScreenItems(items, spec, false)
	if got := []string{items[0].Symbol, items[1].Symbol, items[2].Symbol}; got[0] != "c" ||
		got[1] != "b" || got[2] != "a" {
		t.Fatalf("asc order = %v, want [c b a] — a share with no return is not the worst "+
			"performer", got)
	}
}

func TestScreenSortsByTheRequestedColumn(t *testing.T) {
	small := rosterRow("ذوب", "ذوب آهن اصفهان", "27", "فلزات اساسی", 1132)
	big := rosterRow("فولاد", "فولاد مبارکه اصفهان", "27", "فلزات اساسی", 4636)
	window := map[string][]screenBarRow{
		small.InsCode: {
			bar(day(2026, time.March, 2), 100, 10, 1, 1_000, 1.0),
			bar(day(2026, time.March, 5), 200, 10, 1, 1_000, 1.0), // +100%
		},
		big.InsCode: {
			bar(day(2026, time.March, 2), 100, 10, 1, 9_000_000, 1.0),
			bar(day(2026, time.March, 5), 110, 10, 1, 9_000_000, 1.0), // +10%
		},
	}
	base := screenInputs{
		Roster: []stockRow{small, big}, Window: window,
		Latest: map[string]screenBarRow{}, Converter: screenConverter(), Sectors: testSectors(),
	}

	base.Query = mustQuery(t, "period=max")
	byValue := buildScreenResponse(base)
	if byValue.Items[0].Symbol != "فولاد" {
		t.Errorf("the default sort is avg_value desc, so the liquid name leads; got %q",
			byValue.Items[0].Symbol)
	}

	base.Query = mustQuery(t, "period=max&sort=return")
	byReturn := buildScreenResponse(base)
	if byReturn.Items[0].Symbol != "ذوب" {
		t.Errorf("sort=return desc must lead with +100%%; got %q", byReturn.Items[0].Symbol)
	}
	if byReturn.Sort != "return" || byReturn.Order != "desc" {
		t.Errorf("the response must echo sort=%q order=%q", byReturn.Sort, byReturn.Order)
	}
}

func TestScreenTruncationIsAnnouncedNotHidden(t *testing.T) {
	a := rosterRow("فولاد", "فولاد مبارکه اصفهان", "27", "فلزات اساسی", 4636)
	b := rosterRow("ذوب", "ذوب آهن اصفهان", "27", "فلزات اساسی", 1132)
	out := buildScreenResponse(screenInputs{
		Query:  mustQuery(t, "period=max&limit=1"),
		Roster: []stockRow{a, b},
		Window: map[string][]screenBarRow{
			a.InsCode: {bar(day(2026, time.March, 2), 100, 10, 1, 9_000, 1.0)},
			b.InsCode: {bar(day(2026, time.March, 2), 100, 10, 1, 1_000, 1.0)},
		},
		Latest:    map[string]screenBarRow{},
		Converter: screenConverter(),
		Sectors:   testSectors(),
	})
	if out.Count != 1 || out.Matched != 2 || !out.Truncated {
		t.Fatalf("count=%d matched=%d truncated=%v, want 1/2/true",
			out.Count, out.Matched, out.Truncated)
	}
	found := false
	for _, w := range out.Warnings {
		if strings.Contains(w, "truncated") {
			found = true
		}
	}
	if !found {
		t.Error("a truncated page must say so in warnings")
	}
}

func TestScreenStatesItsCurrencyAndItsBoundary(t *testing.T) {
	row := rosterRow("فولاد", "فولاد مبارکه اصفهان", "27", "فلزات اساسی", 4636)
	out := buildScreenResponse(screenInputs{
		Query:  mustQuery(t, "period=max"),
		Roster: []stockRow{row},
		Window: map[string][]screenBarRow{
			row.InsCode: {
				bar(day(2026, time.March, 2), 100, 10, 1, 1000, 1.0),
				bar(day(2026, time.March, 5), 110, 10, 1, 1000, 1.0),
			},
		},
		Latest:    map[string]screenBarRow{},
		Converter: screenConverter(),
		Sectors:   testSectors(),
	})

	if out.Currency != "IRR" {
		t.Fatalf("currency = %q, want IRR — TSETMC quotes rial", out.Currency)
	}
	if !strings.Contains(out.CurrencyNote, "TOMAN") {
		t.Error("the note must warn that the rest of this API reports toman")
	}
	if out.PriceBasis.LastCloseAdjusted || !out.PriceBasis.ReturnsAdjusted {
		t.Error("price_basis must say last_close is raw and the returns are adjusted")
	}
	if !strings.Contains(out.PriceBasis.VolatilityBasis, "NOT ANNUALISED") {
		t.Errorf("volatility_basis must say it is not annualised, got %q",
			out.PriceBasis.VolatilityBasis)
	}

	// The boundary, per row, from the API rather than from frontend copy.
	want := map[string]bool{
		"pe_ratio": false, "eps_growth": false, "roe": false,
		"dividend_yield": false, "market_cap": false,
	}
	for _, m := range out.Items[0].AbsentMetrics {
		if _, known := want[m.Metric]; !known {
			t.Errorf("unexpected absent metric %q", m.Metric)
			continue
		}
		want[m.Metric] = true
		if m.Reason == "" || m.Requires == "" {
			t.Errorf("%s must name the real obstacle and what it would require", m.Metric)
		}
	}
	for metric, seen := range want {
		if !seen {
			t.Errorf("absent_metrics must name %q", metric)
		}
	}

	var pe absentMetric
	for _, m := range out.Items[0].AbsentMetrics {
		if m.Metric == "pe_ratio" {
			pe = m
		}
	}
	for _, phrase := range []string{"XBRL", "estimatedEPS", "FY1396", "basis label"} {
		if !strings.Contains(pe.Reason, phrase) {
			t.Errorf("the P/E reason must mention %q — a later reader will find TSETMC's "+
				"scalar and wonder why it was skipped", phrase)
		}
	}
	if !strings.Contains(marketCapReason(out.Items[0].AbsentMetrics), "shares outstanding") {
		t.Error("market cap must be absent WITH the shares-outstanding reason, never an " +
			"empty column")
	}
}

func marketCapReason(metrics []absentMetric) string {
	for _, m := range metrics {
		if m.Metric == "market_cap" {
			return m.Reason
		}
	}
	return ""
}

func TestScreenAdjustmentBlockIsTheSameOneStocksAndBarsPublish(t *testing.T) {
	// "Which arithmetic produced this number" must not have three spellings
	// across three endpoints.
	row := foolad()
	out := buildScreenResponse(screenInputs{
		Query:  mustQuery(t, "period=max"),
		Roster: []stockRow{row},
		Window: map[string][]screenBarRow{
			row.InsCode: {
				bar(day(2026, time.March, 2), 100, 10, 1, 1000, 1.0),
				bar(day(2026, time.March, 5), 110, 10, 1, 1000, 1.0),
			},
		},
		Latest:    map[string]screenBarRow{},
		Converter: screenConverter(),
		Sectors:   testSectors(),
	})
	got := out.Items[0].Adjustment
	if got.Version != "priceYesterday-chain-v1" || got.Status != "validated" {
		t.Fatalf("adjustment = %+v", got)
	}
	if got.ActionsApplied != 31 {
		t.Errorf("actions_applied = %d, want 31", got.ActionsApplied)
	}
	if !got.Servable {
		t.Error("a validated verdict is servable")
	}
	// The reopening caveat travels onto the row, as it does onto /bars.
	found := false
	for _, n := range out.Items[0].Notes {
		if strings.Contains(n, "reopening") {
			found = true
		}
	}
	if !found {
		t.Error("فولاد's one reopening must be noted: a return spanning it is not a session return")
	}
}

func TestRosterSectorsAreBuiltFromTheRoster(t *testing.T) {
	rows := []stockRow{
		rosterRow("فولاد", "فولاد", "27", "فلزات اساسی", 10),
		rosterRow("فملی", "ملی مس", "27", "فلزات اساسی", 10),
		kachad(),
	}
	got := rosterSectors(rows)
	if len(got) != 2 {
		t.Fatalf("expected 2 sectors, got %d", len(got))
	}
	if got[0].Code != "13" || got[0].Count != 1 {
		t.Errorf("first sector = %+v, want 13 with 1 instrument", got[0])
	}
	if got[1].Code != "27" || got[1].Count != 2 {
		t.Errorf("second sector = %+v, want 27 with 2 instruments", got[1])
	}
	// A disabled instrument still contributes its sector: the vocabulary
	// describes the roster, and `excluded` is where the gate speaks.
	if got[0].NameFA != "استخراج کانه های فلزی" {
		t.Errorf("sector 13 name = %q", got[0].NameFA)
	}
}

func TestScreenResponseMarshalsWithEveryNullPresent(t *testing.T) {
	// A client reading `return_pct` must find the key whether or not the figure
	// exists; a key that disappears when the value is null is a key every
	// consumer has to guess at.
	row := rosterRow("نوری", "پتروشیمی نوری", "44", "محصولات شیمیایی", 1)
	out := buildScreenResponse(screenInputs{
		Query:     mustQuery(t, "period=1m"),
		Roster:    []stockRow{row},
		Window:    map[string][]screenBarRow{row.InsCode: {bar(day(2026, time.March, 5), 1000, 1, 1, 10, 1.0)}},
		Latest:    map[string]screenBarRow{},
		Converter: screenConverter(),
		Sectors:   testSectors(),
	})
	body, err := json.Marshal(out)
	if err != nil {
		t.Fatalf("the response must marshal: %v", err)
	}
	var decoded map[string]any
	if err := json.Unmarshal(body, &decoded); err != nil {
		t.Fatalf("the response must round-trip: %v", err)
	}
	items, _ := decoded["items"].([]any)
	if len(items) != 1 {
		t.Fatalf("expected 1 item, got %d", len(items))
	}
	first, _ := items[0].(map[string]any)
	for _, key := range []string{
		"return_pct", "return_reason", "volatility_pct", "volatility_reason",
		"max_drawdown_pct", "max_drawdown_reason", "price_usd", "price_usd_reason",
		"price_gold_grams", "price_gold_grams_reason", "last_close", "last_trade_date",
		"metrics_from", "metrics_to", "conversion", "absent_metrics", "adjustment",
	} {
		if _, ok := first[key]; !ok {
			t.Errorf("item is missing the key %q", key)
		}
	}
	if v, ok := first["return_pct"]; !ok || v != nil {
		t.Errorf("return_pct over one session must be null, got %v", v)
	}
	if v, _ := first["return_reason"].(string); v == "" {
		t.Error("a null return must carry a non-empty reason")
	}
}
