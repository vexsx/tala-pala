package prices

import (
	"strings"
	"testing"
)

// /api/v1/market/summary is a gold page: current_18k, the premium and the 24h
// change all describe IR_GOLD_18K, and the frontend renders the `signal` block
// beside them as THE advisory for that price. Before migration 0026 `signals`
// held one row per pass and "the newest row" was gold by construction; the
// engine now writes seven rows per pass, so an unfiltered read would show a
// reader silver's or the dollar's call under gold's price.
//
// Addendum 13 fixed the same shape of defect in _load_latest_predictions: the
// XAUUSD rows written after the gold rows every cycle overwrote the per-horizon
// map and the Tehran signal was scored from global-gold forecasts.
func TestSummarySignalSelectIsScopedToGold(t *testing.T) {
	if !strings.Contains(summarySignalSelect, "FROM signals") ||
		!strings.Contains(summarySignalSelect, "WHERE symbol = $1") {
		t.Errorf("the summary's advisory read is not symbol-scoped:\n%s", summarySignalSelect)
	}
	if !strings.Contains(summarySignalSelect, "ORDER BY generated_at DESC") ||
		!strings.Contains(summarySignalSelect, "LIMIT 1") {
		t.Errorf("the summary needs the newest row of ONE symbol:\n%s", summarySignalSelect)
	}
	if summarySignalSymbol != "IR_GOLD_18K" {
		t.Errorf("summarySignalSymbol = %q, want the symbol every other key in the "+
			"payload describes", summarySignalSymbol)
	}
	// The SELECT list is the /signals/current shape minus `inputs`; `symbol` is
	// filtered on and never selected, so the payload the frontend already
	// consumes does not change shape.
	if strings.Contains(summarySignalSelect, "symbol,") {
		t.Error("symbol is filtered on, never selected: adding it changes the payload")
	}
}

// The string guard above pins the SQL text. It cannot see the call site, and an
// adversarial review proved that gap is reachable: leaving the constant alone
// and binding "XAUUSD" at the call site kept the whole package green while
// /api/v1/market/summary served silver's reading as the gold advisory.
//
// This asserts the half the string cannot: the symbol actually bound.
func TestSummarySignalQueryBindsGold(t *testing.T) {
	query, args := summarySignalQuery()

	if len(args) != 1 {
		t.Fatalf("summary advisory read takes exactly one bind; got %d: %v", len(args), args)
	}
	if got, ok := args[0].(string); !ok || got != "IR_GOLD_18K" {
		t.Fatalf("the summary advisory must be bound to IR_GOLD_18K, got %v -- "+
			"/api/v1/market/summary is the GOLD summary, and since migration 0026 "+
			"the signals table holds a row per asset per pass", args[0])
	}
	if !strings.Contains(query, "WHERE symbol = $1") {
		t.Fatalf("the advisory read lost its symbol filter:\n%s", query)
	}
}
