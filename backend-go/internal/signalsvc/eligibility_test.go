package signalsvc

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

// decodeError reads the standard {"error":{code,message,details}} envelope.
func decodeError(t *testing.T, rec *httptest.ResponseRecorder) httpserver.ErrorBody {
	t.Helper()
	var body httpserver.ErrorBody
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("response is not the error envelope: %v", err)
	}
	return body
}

// --- the eligible set --------------------------------------------------------

// The set is the design, measured on production 2026-09-09, and widening it is
// the failure mode this test exists to catch: every symbol here has price
// history a factor can be read from, and nothing else does.
func TestEligibleSignalSymbols_IsExactlyTheSevenWithHistory(t *testing.T) {
	want := []string{
		"IR_GOLD_18K", "USD_IRT", "IR_COIN_EMAMI", "XAUUSD", "XAGUSD",
		"IR_GOLD_FUND_AYAR", "IR_GOLD_FUND_TALA",
	}
	if len(eligibleSignalSymbols) != len(want) {
		t.Fatalf("eligible set is %d symbols, want %d: %v",
			len(eligibleSignalSymbols), len(want), eligibleSignalSymbols)
	}
	for i, code := range want {
		if eligibleSignalSymbols[i] != code {
			t.Errorf("eligible[%d] = %q, want %q (order is part of the contract)",
				i, eligibleSignalSymbols[i], code)
		}
	}
	if eligibleSignalSymbols[0] != defaultSignalSymbol {
		t.Errorf("the primary instrument must lead the list, got %q", eligibleSignalSymbols[0])
	}
}

// A symbol may not be both scored and refused: the two tables answer the same
// question and a code in both would make the answer depend on lookup order.
func TestEligibilityTables_DoNotOverlap(t *testing.T) {
	for code := range ineligibleSignalSymbols {
		if eligibleSignalSymbolSet[code] {
			t.Errorf("%s is listed as both eligible and ineligible", code)
		}
	}
}

// Every refusal has to say something specific. A blank or generic reason is
// what this endpoint exists to avoid: the caller must learn WHY, not just that.
func TestIneligibleReasons_NameTheSymbolAndTheCause(t *testing.T) {
	for code, reason := range ineligibleSignalSymbols {
		if !strings.Contains(reason, code) {
			t.Errorf("%s: reason does not name the symbol: %q", code, reason)
		}
		if len(reason) < 60 {
			t.Errorf("%s: reason is too thin to be a real explanation: %q", code, reason)
		}
	}
	for _, want := range []struct{ code, fragment string }{
		{"US10Y", "yield"},
		{"IR_GOLD_FUND_FLOW", "RATIO"},
		{"DXY", "index"},
		{"BRENT_OIL", "macro context"},
		{"IR_GOLD_FUND_KAHRABA", "never collected"},
	} {
		if !strings.Contains(ineligibleSignalSymbols[want.code], want.fragment) {
			t.Errorf("%s: reason should mention %q, got %q",
				want.code, want.fragment, ineligibleSignalSymbols[want.code])
		}
	}
}

// The coverage-gap block is the page's honesty about its own boundary. An entry
// with no reason would be worse than no entry at all.
func TestUncollectedAssetClasses_NameCarsHousingEquitiesAndLocalSilver(t *testing.T) {
	got := map[string]string{}
	for _, e := range uncollectedAssetClasses {
		if e.Reason == "" {
			t.Errorf("%s carries no reason", e.SymbolOrClass)
		}
		if !strings.Contains(e.Reason, "Not collected") {
			t.Errorf("%s: the reason must say it is not collected, got %q",
				e.SymbolOrClass, e.Reason)
		}
		got[e.SymbolOrClass] = e.Reason
	}
	for _, class := range []string{"cars", "housing", "tehran_equities", "ir_silver"} {
		if _, ok := got[class]; !ok {
			t.Errorf("coverage gaps do not mention %q", class)
		}
	}
	if !strings.Contains(got["ir_silver"], "XAGUSD") {
		t.Errorf("the silver gap must explain that XAGUSD is not a local series: %q", got["ir_silver"])
	}
}

// --- ParseSignalSymbol -------------------------------------------------------

func TestParseSignalSymbol(t *testing.T) {
	cases := []struct {
		name        string
		raw         string
		wantSymbol  string
		wantVerdict symbolVerdict
	}{
		// The default is load-bearing: Overview, Brief and ActionPlanner send
		// no ?symbol= and must keep getting the Tehran gold reading.
		{"absent means the primary symbol", "", defaultSignalSymbol, verdictEligible},
		{"an eligible symbol passes", "XAUUSD", "XAUUSD", verdictEligible},
		{"a fund passes", "IR_GOLD_FUND_TALA", "IR_GOLD_FUND_TALA", verdictEligible},
		{"a yield is refused by policy", "US10Y", "US10Y", verdictIneligible},
		{"an index is refused by policy", "DXY", "DXY", verdictIneligible},
		{"a ratio is refused by policy", "IR_GOLD_FUND_FLOW", "IR_GOLD_FUND_FLOW", verdictIneligible},
		{"macro context is refused by policy", "BRENT_OIL", "BRENT_OIL", verdictIneligible},
		{"a symbol with no history is refused", "IR_GOLD_FUND_KAHRABA", "IR_GOLD_FUND_KAHRABA", verdictIneligible},
		// Neither of these is normalized into something servable: upper-casing
		// or trimming a caller's input is substituting, not refusing.
		{"lower case is not normalized", "ir_gold_18k", "ir_gold_18k", verdictUnknownToPolicy},
		{"whitespace is not trimmed", " XAUUSD", " XAUUSD", verdictUnknownToPolicy},
		{"an invented symbol needs the registry", "IR_TESLA", "IR_TESLA", verdictUnknownToPolicy},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			symbol, verdict, reason := ParseSignalSymbol(tc.raw)
			if symbol != tc.wantSymbol {
				t.Errorf("symbol = %q, want %q", symbol, tc.wantSymbol)
			}
			if verdict != tc.wantVerdict {
				t.Errorf("verdict = %v, want %v", verdict, tc.wantVerdict)
			}
			if (reason != "") != (tc.wantVerdict == verdictIneligible) {
				t.Errorf("reason = %q for verdict %v", reason, verdict)
			}
		})
	}
}

// --- registryIneligibleReason ------------------------------------------------

// A symbol the policy has never heard of but the registry knows gets a reason
// assembled from what the registry actually records, so a symbol added by a
// later migration is refused with a true sentence rather than a generic one.
func TestRegistryIneligibleReason(t *testing.T) {
	cases := []struct {
		name, code, kind, quote, want string
	}{
		{"economic series", "IR_CPI", "economic_series", "INDEX", "economic series"},
		{"percent quote", "IR_POLICY_RATE", "market_price", "PCT", "rate or a ratio"},
		{"ratio quote", "SOME_RATIO", "index", "RATIO", "rate or a ratio"},
		{"index quote", "SOME_INDEX", "index", "INDEX", "index level"},
		{"a tradable price still not on the list", "IR_GOLD_24K", "market_price", "IRT", "not signal-eligible"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := registryIneligibleReason(tc.code, tc.kind, tc.quote)
			if !strings.Contains(got, tc.code) {
				t.Errorf("reason does not name the symbol: %q", got)
			}
			if !strings.Contains(got, tc.want) {
				t.Errorf("reason %q does not contain %q", got, tc.want)
			}
		})
	}
	// The economic-series case wins over the quote currency: "this is macro
	// data with a publication lag" is a more useful answer than "this is an
	// index level", and both are true of a CPI series.
	if strings.Contains(registryIneligibleReason("IR_CPI", "economic_series", "INDEX"), "index level") {
		t.Error("an economic series should be refused as a series, not as an index level")
	}
}

func TestUnknownSymbolMessage_IsDistinctFromIneligible(t *testing.T) {
	unknown := unknownSymbolMessage("IR_TESLA")
	if !strings.Contains(unknown, "not in the instrument registry") {
		t.Errorf("unknown-symbol message is not specific: %q", unknown)
	}
	// "does not exist here" and "exists but is not scored" send a client to two
	// different fixes, so the two messages must not be interchangeable.
	if strings.Contains(unknown, "not signal-eligible") {
		t.Errorf("unknown and ineligible must read differently: %q", unknown)
	}
}

func TestSymbolRefusalDetails_CarriesTheEligibleSet(t *testing.T) {
	details := symbolRefusalDetails("US10Y")
	if details["symbol"] != "US10Y" {
		t.Errorf("details.symbol = %v", details["symbol"])
	}
	eligible, ok := details["eligible"].([]string)
	if !ok || len(eligible) != len(eligibleSignalSymbols) {
		t.Fatalf("details.eligible = %v", details["eligible"])
	}
	// A copy, not the package slice: a handler that mutated the details map
	// must not be able to rewrite the eligibility policy for the process.
	eligible[0] = "MUTATED"
	if eligibleSignalSymbols[0] == "MUTATED" {
		t.Error("details.eligible aliases the package-level eligible set")
	}
}

// --- refusals reach the wire -------------------------------------------------

// A policy refusal never touches the database, which is what lets these two
// run against a handler with no pool at all.
func TestCurrentRefusesIneligibleSymbol(t *testing.T) {
	h := &Handler{Log: quietLogger()}
	rec := httptest.NewRecorder()
	h.Current(rec, httptest.NewRequest(http.MethodGet, "/api/v1/signals/current?symbol=US10Y", nil))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
	body := decodeError(t, rec)
	if !strings.Contains(body.Error.Message, "yield") {
		t.Errorf("the 400 must say why US10Y is refused, got %q", body.Error.Message)
	}
	if body.Error.Details["symbol"] != "US10Y" {
		t.Errorf("details.symbol = %v", body.Error.Details["symbol"])
	}
}

func TestHistoryRefusesTheFlowRatio(t *testing.T) {
	h := &Handler{Log: quietLogger()}
	rec := httptest.NewRecorder()
	h.History(rec, httptest.NewRequest(http.MethodGet, "/api/v1/signals/history?symbol=IR_GOLD_FUND_FLOW", nil))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
	if msg := decodeError(t, rec).Error.Message; !strings.Contains(msg, "RATIO") {
		t.Errorf("the 400 must say the flow series is a ratio, got %q", msg)
	}
}

// --- the coverage block is complete and self-consistent ----------------------

// The five refused symbols used to exist only as 400 messages: they appeared
// NOWHERE on the board, so a reader who could see DXY, US10Y and BRENT_OIL on
// the charts had no way to learn why there is no reading for them. Omission is
// not an explanation.
func TestSignalCoverageGaps_AccountForEveryRefusedSymbol(t *testing.T) {
	byCode := map[string]UnavailableEntry{}
	for _, e := range signalCoverageGaps {
		byCode[e.SymbolOrClass] = e
	}
	for code, reason := range ineligibleSignalSymbols {
		entry, ok := byCode[code]
		if !ok {
			t.Errorf("%s is refused by policy but appears nowhere in the coverage block", code)
			continue
		}
		// The 400 and the page must not state different reasons for the same
		// symbol, which is why one is derived from the other.
		if entry.Reason != reason {
			t.Errorf("%s: the board says %q and the 400 says %q", code, entry.Reason, reason)
		}
	}
	for _, class := range []string{"cars", "housing", "tehran_equities", "ir_silver"} {
		if _, ok := byCode[class]; !ok {
			t.Errorf("the coverage block dropped %q", class)
		}
	}
	if len(signalCoverageGaps) != len(refusedSignalSymbols)+len(uncollectedAssetClasses) {
		t.Errorf("coverage block has %d entries, want %d refusals plus %d structural gaps",
			len(signalCoverageGaps), len(refusedSignalSymbols), len(uncollectedAssetClasses))
	}
}

// A single sentence written over the whole block cannot be true of all of it.
// "The data does not exist in this system" is true of cars and false of DXY,
// US10Y and BRENT_OIL, which are collected daily. The category is what lets a
// client say the right thing about each group.
func TestSignalCoverageGaps_DistinguishNotCollectedFromNotScored(t *testing.T) {
	for _, e := range signalCoverageGaps {
		if e.Category == "" {
			t.Errorf("%s carries no category", e.SymbolOrClass)
		}
		if e.Reason == "" {
			t.Errorf("%s carries no reason", e.SymbolOrClass)
		}
	}
	for _, e := range refusedSignalSymbols {
		if e.Category != unavailableNotScored {
			t.Errorf("%s: category = %q, want %q", e.SymbolOrClass, e.Category, unavailableNotScored)
		}
		// These are collected. A reason claiming otherwise would contradict the
		// charts the same app draws for them.
		if strings.Contains(e.Reason, "Not collected") {
			t.Errorf("%s is collected; its reason must not say otherwise: %q",
				e.SymbolOrClass, e.Reason)
		}
	}
	for _, e := range uncollectedAssetClasses {
		if e.Category != unavailableNotCollected {
			t.Errorf("%s: category = %q, want %q", e.SymbolOrClass, e.Category, unavailableNotCollected)
		}
	}
	// The one refused symbol that genuinely has no observations says so in its
	// own sentence rather than borrowing the structural category.
	if !strings.Contains(ineligibleSignalSymbols["IR_GOLD_FUND_KAHRABA"], "never collected") {
		t.Error("KAHRABA's reason must state that it has no observations")
	}
	// Ordering is part of the contract: the block must not reshuffle between
	// polls, which is why the refusals are a slice and not a map iteration.
	if signalCoverageGaps[0].SymbolOrClass != refusedSignalSymbols[0].SymbolOrClass {
		t.Errorf("coverage gaps start with %q", signalCoverageGaps[0].SymbolOrClass)
	}
}

// Every eligible symbol and every refused symbol is accounted for exactly once
// across the two policy tables, so the page's account of its own coverage has
// no symbol in two places and none in neither.
func TestEveryRegisteredSignalSymbolIsAccountedForOnce(t *testing.T) {
	seen := map[string]int{}
	for _, code := range eligibleSignalSymbols {
		seen[code]++
	}
	for _, e := range refusedSignalSymbols {
		seen[e.SymbolOrClass]++
	}
	for code, n := range seen {
		if n != 1 {
			t.Errorf("%s appears %d times across the eligible and refused sets", code, n)
		}
	}
	if len(seen) != len(eligibleSignalSymbols)+len(refusedSignalSymbols) {
		t.Errorf("the two tables overlap: %d distinct codes from %d entries",
			len(seen), len(eligibleSignalSymbols)+len(refusedSignalSymbols))
	}
}
