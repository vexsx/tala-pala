package alerts

import (
	"strings"
	"testing"
)

// Migration 0026 turned `signals` from one row per pass into seven, one per
// symbol. This snapshot read is where that change did the most damage:
// signal_change compares LatestSignal with PreviousSignal, so an unfiltered
// "newest two rows" made every pass look like a change of view between two
// unrelated instruments.
//
// Same defect Addendum 13 fixed in _load_latest_predictions, where XAUUSD rows
// written after the gold rows overwrote the per-horizon map and the Tehran
// signal was scored from global-gold forecasts. Rows from different assets are
// not consecutive readings just because they share a table and a clock.
func TestAlertSignalSelectIsScopedToOneSymbol(t *testing.T) {
	if !strings.Contains(alertSignalSelect, "FROM signals") ||
		!strings.Contains(alertSignalSelect, "WHERE symbol = $1") {
		t.Errorf("the alert snapshot's signal read is not symbol-scoped:\n%s", alertSignalSelect)
	}
	if !strings.Contains(alertSignalSelect, "ORDER BY generated_at DESC") ||
		!strings.Contains(alertSignalSelect, "LIMIT 2") {
		t.Errorf("signal_change needs the two newest rows of ONE symbol:\n%s", alertSignalSelect)
	}
	if alertSignalSymbol != "IR_GOLD_18K" {
		t.Errorf("alertSignalSymbol = %q; the rest of the snapshot (price, premium, "+
			"volatility) is IR_GOLD_18K and the signal fields must describe the same asset",
			alertSignalSymbol)
	}
}
