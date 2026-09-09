package signalsvc

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

func at(y int, m time.Month, d, h int) time.Time {
	return time.Date(y, m, d, h, 0, 0, 0, time.UTC)
}

// testNow is the clock these projection tests run on: one minute after the
// 06:00 pass the rows below were written by, so nothing is stale unless the
// test deliberately backdates it.
var testNow = time.Date(2026, 9, 9, 6, 1, 0, 0, time.UTC)

func raw(s string) json.RawMessage { return json.RawMessage(s) }

// --- evidence basis ----------------------------------------------------------

// "hold from a model" and "hold from an RSI" are not the same claim, and only
// two of the seven eligible symbols have models at all. Defaulting an
// unanswerable case to model_backed would be the fabrication this field exists
// to prevent.
func TestResolveEvidenceBasis(t *testing.T) {
	cases := []struct {
		name, inputs, want string
	}{
		{
			"the generator's own field wins",
			`{"evidence_basis":"technical_only","expected_change_pct":{"1d":0.4}}`,
			evidenceTechnicalOnly,
		},
		{
			"model_backed is honoured too",
			`{"evidence_basis":"model_backed"}`,
			evidenceModelBacked,
		},
		{
			// The 1,222 rows written before the field existed still record the
			// answer, in the forecast map the scorer was handed.
			"a legacy row with forecasts is model-backed",
			`{"expected_change_pct":{"1d":0.31,"7d":0.9}}`,
			evidenceModelBacked,
		},
		{
			"a legacy row with no forecasts is technical-only",
			`{"expected_change_pct":{}}`,
			evidenceTechnicalOnly,
		},
		{
			"a legacy row with only null forecasts is technical-only",
			`{"expected_change_pct":{"1d":null}}`,
			evidenceTechnicalOnly,
		},
		{
			"no forecast key at all is technical-only",
			`{"regime":"trending"}`,
			evidenceTechnicalOnly,
		},
		{
			// A value outside the vocabulary is not trusted; the recorded
			// forecast map answers instead.
			"an unrecognised basis falls through to the forecasts",
			`{"evidence_basis":"vibes","expected_change_pct":{"1d":0.2}}`,
			evidenceModelBacked,
		},
		{
			"an unreadable forecast map is not guessed at",
			`{"expected_change_pct":"all of them"}`,
			evidenceUnknown,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := projectSignalInputs(raw(tc.inputs)).EvidenceBasis; got != tc.want {
				t.Errorf("evidence basis = %q, want %q", got, tc.want)
			}
		})
	}
}

func TestProjectSignalInputs_UnreadableBlobClaimsNothing(t *testing.T) {
	for _, blob := range []string{"", "not json", "[1,2,3]"} {
		got := projectSignalInputs(raw(blob))
		if got.EvidenceBasis != evidenceUnknown {
			t.Errorf("%q: evidence basis = %q, want %q", blob, got.EvidenceBasis, evidenceUnknown)
		}
		if got.OmittedFactors == nil {
			t.Errorf("%q: omitted factors must be an empty list, never nil", blob)
		}
		if got.CostPct != nil || got.CostBasis != nil || got.StaleReason != nil {
			t.Errorf("%q: nothing may be invented from an unreadable blob: %+v", blob, got)
		}
	}
}

// --- omitted factors ---------------------------------------------------------

func TestReadOmittedFactors(t *testing.T) {
	in := `[
	  {"factor":"forecast_vs_cost","reason":"No model is trained for this symbol."},
	  {"factor":"","reason":"nameless"},
	  {"factor":"local_premium_z"}
	]`
	got := readOmittedFactors(raw(in))
	if len(got) != 2 {
		t.Fatalf("got %d factors, want 2: %+v", len(got), got)
	}
	if got[0].Factor != "forecast_vs_cost" || !strings.Contains(got[0].Reason, "No model") {
		t.Errorf("first factor = %+v", got[0])
	}
	// A factor recorded without a reason keeps its place and is labelled as
	// unexplained. Inventing a plausible reason here would be exactly the kind
	// of fabrication this list exists to prevent.
	if got[1].Factor != "local_premium_z" || got[1].Reason != omittedFactorNoReason {
		t.Errorf("second factor = %+v", got[1])
	}
}

func TestReadOmittedFactors_AbsentOrBrokenIsEmptyNotNil(t *testing.T) {
	for _, blob := range []string{"", "null", "[]", `"nope"`, `{"a":1}`} {
		got := readOmittedFactors(raw(blob))
		if got == nil {
			t.Errorf("%q: must be an empty list, never nil", blob)
		}
		if len(got) != 0 {
			t.Errorf("%q: got %+v", blob, got)
		}
	}
}

// --- cost provenance ---------------------------------------------------------

// The observed hamrahgold spread exists for IR_GOLD_18K and for nothing else.
// Every other asset's hurdle is an assumption, and the payload has to carry the
// sentence that says so or a UI will show a guess with the authority of a quote.
func TestReadCostBasis_ObservedCarriesItsProvenance(t *testing.T) {
	in := `{"round_trip_cost_pct":0.49,
	        "round_trip_cost_basis":"observed_spread",
	        "round_trip_cost_source":"hamrahgold",
	        "round_trip_cost_observed_at":"2026-09-09T04:12:00+00:00",
	        "round_trip_cost_reason":"observed hamrahgold buy/sell spread, 1.8h old"}`
	got := projectSignalInputs(raw(in))
	if got.CostPct == nil || *got.CostPct != 0.49 {
		t.Fatalf("cost pct = %v", got.CostPct)
	}
	cb := got.CostBasis
	if cb == nil || cb.Basis != "observed_spread" {
		t.Fatalf("cost basis = %+v", cb)
	}
	if cb.Source == nil || *cb.Source != "hamrahgold" {
		t.Errorf("source = %v", cb.Source)
	}
	if cb.Reason == nil || !strings.Contains(*cb.Reason, "spread") {
		t.Errorf("reason = %v", cb.Reason)
	}
	// A non-UTC offset is normalized, never passed through: every timestamp on
	// this contract is UTC.
	if cb.ObservedAt == nil || !cb.ObservedAt.Equal(time.Date(2026, 9, 9, 4, 12, 0, 0, time.UTC)) {
		t.Fatalf("observed_at = %v", cb.ObservedAt)
	}
	if cb.ObservedAt.Location() != time.UTC {
		t.Errorf("observed_at is not UTC: %v", cb.ObservedAt.Location())
	}
}

func TestReadCostBasis_AssumedSaysSoAndCarriesTheReason(t *testing.T) {
	in := `{"round_trip_cost_pct":2.2,
	        "round_trip_cost_basis":"assumed",
	        "round_trip_cost_reason":"no two-sided USD_IRT quote is collected; gold's dealer spread belongs to a different market and is not reused"}`
	got := projectSignalInputs(raw(in))
	if got.CostBasis == nil || got.CostBasis.Basis != "assumed" {
		t.Fatalf("cost basis = %+v", got.CostBasis)
	}
	if got.CostBasis.Source != nil {
		t.Errorf("an assumed hurdle has no source, got %v", *got.CostBasis.Source)
	}
	if got.CostBasis.Reason == nil || !strings.Contains(*got.CostBasis.Reason, "different market") {
		t.Errorf("the assumption's reason must travel, got %v", got.CostBasis.Reason)
	}
}

func TestReadCostBasis_NumberWithoutProvenanceIsLabelledUnrecorded(t *testing.T) {
	got := projectSignalInputs(raw(`{"round_trip_cost_pct":1.7}`))
	if got.CostPct == nil || *got.CostPct != 1.7 {
		t.Fatalf("cost pct = %v", got.CostPct)
	}
	if got.CostBasis == nil || got.CostBasis.Basis != costBasisUnrecorded {
		t.Fatalf("cost basis = %+v, want %q", got.CostBasis, costBasisUnrecorded)
	}
}

func TestReadCostBasis_NoCostAtAllIsNull(t *testing.T) {
	got := projectSignalInputs(raw(`{"regime":"trending"}`))
	if got.CostPct != nil || got.CostBasis != nil {
		t.Fatalf("a row recording no hurdle must show none: pct=%v basis=%+v", got.CostPct, got.CostBasis)
	}
}

func TestReadTime_RejectsWhatItCannotPlaceOnTheUTCClock(t *testing.T) {
	for _, blob := range []string{`"2026-09-09 04:12:00"`, `"yesterday"`, `1757390000`, `null`} {
		if got := readTime(raw(blob)); got != nil {
			t.Errorf("%s parsed to %v; an unplaceable timestamp must be null", blob, got)
		}
	}
}

// --- stale reason ------------------------------------------------------------

func TestStaleReason_IsProjectedNeverInvented(t *testing.T) {
	got := projectSignalInputs(raw(`{"stale_reason":"the last XAGUSD observation is 31h old"}`))
	if got.StaleReason == nil || !strings.Contains(*got.StaleReason, "31h") {
		t.Fatalf("stale reason = %v", got.StaleReason)
	}
	// data_fresh false with no recorded cause stays null: the page can say
	// "stale" without Go guessing at why.
	if projectSignalInputs(raw(`{}`)).StaleReason != nil {
		t.Error("a missing stale reason must not be filled in")
	}
}

// --- top supporting / conflicting -------------------------------------------

func TestFirstEntry(t *testing.T) {
	cases := []struct {
		name, in string
		want     *string
	}{
		{"first of several", `["a","b"]`, strPtr("a")},
		{"empty list", `[]`, nil},
		{"absent", ``, nil},
		{"null", `null`, nil},
		{"not an array", `"a"`, nil},
		{"skips a blank entry", `["","b"]`, strPtr("b")},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := firstEntry(raw(tc.in))
			switch {
			case tc.want == nil && got != nil:
				t.Errorf("got %q, want null", *got)
			case tc.want != nil && got == nil:
				t.Errorf("got null, want %q", *tc.want)
			case tc.want != nil && *got != *tc.want:
				t.Errorf("got %q, want %q", *got, *tc.want)
			}
		})
	}
}

func strPtr(s string) *string { return &s }

// --- buildOverviewResponse ---------------------------------------------------

func goldMeta() instrumentMeta {
	return instrumentMeta{
		NameEn: "Iranian 18k gold, per gram", NameFa: "طلای ۱۸ عیار",
		QuoteCurrency: "IRT", Unit: "gram", QualityTier: "official_mirror",
	}
}

func goldRow() overviewSignalRow {
	return overviewSignalRow{
		Symbol: "IR_GOLD_18K", Signal: "hold", Score: 54, Confidence: 0.62,
		Explanation: "Conditions currently favor holding.",
		Supporting:  raw(`["Average model confidence is 62%."]`),
		Conflicting: raw(`["Forecast horizons disagree on direction."]`),
		DataFresh:   true, GeneratedAt: at(2026, 9, 9, 6),
		Inputs: raw(`{"expected_change_pct":{"1d":0.31},"round_trip_cost_pct":0.49,
		              "round_trip_cost_basis":"observed_spread","round_trip_cost_source":"hamrahgold"}`),
	}
}

func TestBuildOverviewResponse_ProjectsAnItemPerPublishedSymbol(t *testing.T) {
	resp := buildOverviewResponse(
		testNow,
		[]string{"IR_GOLD_18K"},
		map[string]instrumentMeta{"IR_GOLD_18K": goldMeta()},
		map[string]overviewSignalRow{"IR_GOLD_18K": goldRow()},
		nil,
	)
	if len(resp.Items) != 1 {
		t.Fatalf("got %d items, want 1", len(resp.Items))
	}
	it := resp.Items[0]
	// Display metadata comes from the registry, not from a symbol list in Go.
	if it.NameFa != "طلای ۱۸ عیار" || it.Unit != "gram" || it.QualityTier != "official_mirror" {
		t.Errorf("registry metadata did not reach the item: %+v", it)
	}
	if it.EvidenceBasis != evidenceModelBacked {
		t.Errorf("evidence basis = %q", it.EvidenceBasis)
	}
	if it.TopSupporting == nil || !strings.Contains(*it.TopSupporting, "confidence") {
		t.Errorf("top supporting = %v", it.TopSupporting)
	}
	if resp.AsOf == nil || !resp.AsOf.Equal(at(2026, 9, 9, 6)) {
		t.Errorf("as_of = %v", resp.AsOf)
	}
}

// An eligible symbol with no reading is not dropped and not emitted as a
// zero-valued item that would render as a confident "hold" built out of
// nothing. It moves to `unavailable` and says why.
func TestBuildOverviewResponse_UnpublishedSymbolMovesToUnavailable(t *testing.T) {
	resp := buildOverviewResponse(
		testNow,
		[]string{"IR_GOLD_18K", "IR_GOLD_FUND_TALA"},
		map[string]instrumentMeta{"IR_GOLD_18K": goldMeta(), "IR_GOLD_FUND_TALA": {NameEn: "Tala gold ETF"}},
		map[string]overviewSignalRow{"IR_GOLD_18K": goldRow()},
		nil,
	)
	if len(resp.Items) != 1 || resp.Items[0].Symbol != "IR_GOLD_18K" {
		t.Fatalf("items = %+v", resp.Items)
	}
	if len(resp.Unavailable) != 1 ||
		resp.Unavailable[0].SymbolOrClass != "IR_GOLD_FUND_TALA" ||
		resp.Unavailable[0].Reason != reasonNoCurrentReading ||
		resp.Unavailable[0].Category != unavailableNoCurrentReading {
		t.Fatalf("unavailable = %+v", resp.Unavailable)
	}
}

// "Not scored yet" and "not in the registry" are different operational
// problems and must not share a sentence.
func TestBuildOverviewResponse_UnregisteredSymbolSaysSoDistinctly(t *testing.T) {
	resp := buildOverviewResponse(
		testNow,
		[]string{"XAGUSD"},
		map[string]instrumentMeta{},
		map[string]overviewSignalRow{"XAGUSD": {Symbol: "XAGUSD", GeneratedAt: at(2026, 9, 9, 5)}},
		nil,
	)
	if len(resp.Items) != 0 {
		t.Fatalf("an unregistered symbol cannot be described, got %+v", resp.Items)
	}
	if resp.Unavailable[0].Reason != reasonUnregistered ||
		resp.Unavailable[0].Category != unavailableUnregistered {
		t.Errorf("entry = %+v", resp.Unavailable[0])
	}
	if reasonUnregistered == reasonNoCurrentReading {
		t.Error("the two unavailability reasons must read differently")
	}
}

func TestBuildOverviewResponse_KeepsEligibleOrderAndAppendsCoverageGaps(t *testing.T) {
	registry := map[string]instrumentMeta{}
	latest := map[string]overviewSignalRow{}
	for _, code := range eligibleSignalSymbols {
		registry[code] = instrumentMeta{NameEn: code}
		latest[code] = overviewSignalRow{Symbol: code, GeneratedAt: at(2026, 9, 9, 6), Inputs: raw(`{}`)}
	}
	resp := buildOverviewResponse(testNow, eligibleSignalSymbols, registry, latest, signalCoverageGaps)

	for i, code := range eligibleSignalSymbols {
		if resp.Items[i].Symbol != code {
			t.Fatalf("items[%d] = %s, want %s", i, resp.Items[i].Symbol, code)
		}
	}
	if len(resp.Unavailable) != len(signalCoverageGaps) {
		t.Fatalf("unavailable = %+v", resp.Unavailable)
	}
	// The coverage gaps come last, after any per-symbol entries, in their own
	// declared order -- refusals, then the never-collected classes.
	for i, want := range signalCoverageGaps {
		if resp.Unavailable[i].SymbolOrClass != want.SymbolOrClass {
			t.Fatalf("coverage gaps are out of order at %d: %+v", i, resp.Unavailable)
		}
	}
}

// as_of is the newest reading in the response, not the newest symbol in the
// list: an overview whose as_of came from a stale item would misdate the page.
func TestBuildOverviewResponse_AsOfIsTheNewestItem(t *testing.T) {
	stale := goldRow()
	stale.Symbol, stale.GeneratedAt = "XAGUSD", at(2026, 9, 8, 5)
	fresh := goldRow()
	resp := buildOverviewResponse(
		testNow,
		[]string{"XAGUSD", "IR_GOLD_18K"},
		map[string]instrumentMeta{"XAGUSD": {}, "IR_GOLD_18K": goldMeta()},
		map[string]overviewSignalRow{"XAGUSD": stale, "IR_GOLD_18K": fresh},
		nil,
	)
	if resp.AsOf == nil || !resp.AsOf.Equal(at(2026, 9, 9, 6)) {
		t.Fatalf("as_of = %v, want the newest reading", resp.AsOf)
	}
}

// Nothing published at all is a null as_of and two empty lists — never a zero
// timestamp, which renders as 1970, and never a null list, which a client has
// to special-case.
func TestBuildOverviewResponse_EmptyIsExplicit(t *testing.T) {
	resp := buildOverviewResponse(testNow, nil, nil, nil, nil)
	if resp.AsOf != nil {
		t.Errorf("as_of = %v, want null", resp.AsOf)
	}
	body, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	const want = `{"as_of":null,"oldest_reading_at":null,"stale_after_hours":6,` +
		`"items":[],"unavailable":[]}`
	if got := string(body); got != want {
		t.Errorf("empty response = %s", got)
	}
}

// --- the gold pipeline is untouched ------------------------------------------

// The same factors are available to IR_GOLD_18K after this change as before, so
// its reading must come out identical. Go cannot prove the scorer's arithmetic —
// that belongs to prediction-python — but it owns the projection, and this
// asserts the projection is a pass-through: every number the overview shows for
// gold is the number stored on the row, unrounded, unrescaled and unreordered.
func TestGoldReadingPassesThroughUnchanged(t *testing.T) {
	row := goldRow()
	item := buildOverviewItem(testNow, row, goldMeta())

	if item.Signal != row.Signal || item.Score != row.Score || item.Confidence != row.Confidence {
		t.Errorf("the reading was altered in projection: %+v vs row %+v", item, row)
	}
	// goldRow's blob predates `inputs.headline`, so the projection falls back to
	// the stored explanation -- which is all a row from that era has.
	if item.Headline != row.Explanation {
		t.Errorf("headline = %q, want the stored explanation %q", item.Headline, row.Explanation)
	}
	if item.DataFresh != row.DataFresh || !item.GeneratedAt.Equal(row.GeneratedAt) {
		t.Errorf("freshness or timestamp was altered: %+v", item)
	}
	// Gold is the one asset with a trained model, an observed dealer spread and
	// a computable premium, so nothing about it is omitted.
	if len(item.OmittedFactors) != 0 {
		t.Errorf("gold should omit no factor, got %+v", item.OmittedFactors)
	}
	if item.EvidenceBasis != evidenceModelBacked {
		t.Errorf("gold is model-backed, got %q", item.EvidenceBasis)
	}
	if item.CostBasis == nil || item.CostBasis.Basis != "observed_spread" {
		t.Errorf("gold's hurdle is the observed dealer spread, got %+v", item.CostBasis)
	}
}

// --- the headline the board prints -------------------------------------------

// `explanation` always ends "This is an uncertain, model-based assessment of
// current conditions". On a technical_only row that sentence is denied by the
// same item three fields earlier, and it was the most-read string on the board.
// The engine already computes a model-free first sentence into `inputs.headline`
// for exactly this purpose.
func TestHeadlineIsTheEnginesOwnLineNotTheBlendedExplanation(t *testing.T) {
	const engineHeadline = "Conditions currently favor waiting (score 52/100)."
	row := overviewSignalRow{
		Symbol: "USD_IRT", Signal: "hold", Score: 52, GeneratedAt: at(2026, 9, 9, 6),
		Explanation: engineHeadline + " 2 supporting vs 1 conflicting factors. " +
			"This is an uncertain, model-based assessment of current conditions — " +
			"not financial advice, and actual outcomes can differ.",
		Inputs: raw(`{"evidence_basis":"technical_only","headline":` +
			`"Conditions currently favor waiting (score 52/100)."}`),
	}
	item := buildOverviewItem(testNow, row, instrumentMeta{})
	if item.Headline != engineHeadline {
		t.Fatalf("headline = %q, want inputs.headline %q", item.Headline, engineHeadline)
	}
	if strings.Contains(item.Headline, "model-based") {
		t.Errorf("a technical_only item's headline claims model evidence: %q", item.Headline)
	}
	if item.EvidenceBasis != evidenceTechnicalOnly {
		t.Fatalf("evidence basis = %q", item.EvidenceBasis)
	}
}

// The rows written before the field existed are single-asset gold and genuinely
// have nothing else; falling back to their explanation projects what they
// recorded rather than blanking the most-read line on the card.
func TestHeadlineFallsBackToTheExplanationOnLegacyRows(t *testing.T) {
	for _, blob := range []string{`{}`, `{"headline":null}`, `{"headline":""}`, `not json`} {
		row := goldRow()
		row.Inputs = raw(blob)
		if got := buildOverviewItem(testNow, row, goldMeta()).Headline; got != row.Explanation {
			t.Errorf("%s: headline = %q, want the stored explanation", blob, got)
		}
	}
}

// --- how old the reading is ---------------------------------------------------

// A symbol the engine has stopped publishing kept serving its last reading, and
// `as_of` -- a maximum across items -- stamped it with the newest pass's time.
// Every item now carries its own age, and one past the engine's six-hour review
// window says so.
func TestReadingAgeIsMeasuredPerItem(t *testing.T) {
	cases := []struct {
		name      string
		generated time.Time
		wantAge   float64
		wantStale bool
	}{
		{"minutes after its pass", testNow.Add(-1 * time.Minute), 0.02, false},
		{"just inside the review window", testNow.Add(-5*time.Hour - 59*time.Minute), 5.98, false},
		{"just outside it", testNow.Add(-6*time.Hour - 1*time.Minute), 6.02, true},
		{"most of a day old", testNow.Add(-20 * time.Hour), 20, true},
		// Clock skew must not print a negative age.
		{"stamped slightly in the future", testNow.Add(2 * time.Minute), 0, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			row := goldRow()
			row.GeneratedAt = tc.generated
			item := buildOverviewItem(testNow, row, goldMeta())
			if item.ReadingAgeHours != tc.wantAge {
				t.Errorf("reading_age_hours = %v, want %v", item.ReadingAgeHours, tc.wantAge)
			}
			if item.ReadingStale != tc.wantStale {
				t.Errorf("reading_stale = %v, want %v", item.ReadingStale, tc.wantStale)
			}
		})
	}
}

// data_fresh is about the PRICES the scorer was handed; reading_stale is about
// the row's own age. Conflating them would let a page call a minutes-old
// forced-hold "stale data from an old pass", or the reverse.
func TestStaleInputsAndAStaleReadingAreIndependent(t *testing.T) {
	fresh := goldRow()
	fresh.DataFresh = false
	fresh.GeneratedAt = testNow.Add(-1 * time.Minute)
	item := buildOverviewItem(testNow, fresh, goldMeta())
	if item.DataFresh || item.ReadingStale {
		t.Errorf("stale inputs, current reading: %+v", item)
	}

	old := goldRow()
	old.DataFresh = true
	old.GeneratedAt = testNow.Add(-9 * time.Hour)
	item = buildOverviewItem(testNow, old, goldMeta())
	if !item.DataFresh || !item.ReadingStale {
		t.Errorf("fresh inputs, old reading: %+v", item)
	}
}

// as_of is a maximum, and a page that stamps every row with it misdates the
// laggards. The spread has to be on the wire.
func TestBuildOverviewResponse_PublishesTheSpreadBetweenReadings(t *testing.T) {
	lagging := goldRow()
	lagging.Symbol, lagging.GeneratedAt = "XAGUSD", at(2026, 9, 8, 21)
	resp := buildOverviewResponse(
		testNow,
		[]string{"IR_GOLD_18K", "XAGUSD"},
		map[string]instrumentMeta{"IR_GOLD_18K": goldMeta(), "XAGUSD": {}},
		map[string]overviewSignalRow{"IR_GOLD_18K": goldRow(), "XAGUSD": lagging},
		nil,
	)
	if resp.AsOf == nil || !resp.AsOf.Equal(at(2026, 9, 9, 6)) {
		t.Fatalf("as_of = %v", resp.AsOf)
	}
	if resp.OldestReadingAt == nil || !resp.OldestReadingAt.Equal(at(2026, 9, 8, 21)) {
		t.Fatalf("oldest_reading_at = %v", resp.OldestReadingAt)
	}
	if resp.StaleAfterHours != signalReadingStaleAfter.Hours() {
		t.Errorf("stale_after_hours = %v", resp.StaleAfterHours)
	}
}

// A symbol whose newest row is older than the bound never reaches the map the
// query fills, so it lands under `unavailable` with a reason that covers both
// possibilities the bounded read genuinely cannot separate.
func TestAnExpiredSymbolIsAbsentNotStale(t *testing.T) {
	resp := buildOverviewResponse(
		testNow,
		[]string{"IR_GOLD_18K", "IR_GOLD_FUND_AYAR"},
		map[string]instrumentMeta{"IR_GOLD_18K": goldMeta(), "IR_GOLD_FUND_AYAR": {NameEn: "Ayar"}},
		map[string]overviewSignalRow{"IR_GOLD_18K": goldRow()},
		nil,
	)
	if len(resp.Items) != 1 || resp.Items[0].Symbol != "IR_GOLD_18K" {
		t.Fatalf("items = %+v", resp.Items)
	}
	entry := resp.Unavailable[0]
	if entry.SymbolOrClass != "IR_GOLD_FUND_AYAR" || entry.Category != unavailableNoCurrentReading {
		t.Fatalf("unavailable = %+v", entry)
	}
	// The sentence has to name the window, or the reader cannot tell how long
	// "no current reading" means.
	if !strings.Contains(entry.Reason, "24 hours") {
		t.Errorf("the reason does not state the bound: %q", entry.Reason)
	}
}
