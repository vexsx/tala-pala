package prices

import (
	"strings"
	"testing"
	"time"
)

// A series that only ever receives one settled close a day read as stale for
// most of every day: /prices/current applied the minutes rule to yesterday's
// 23:00 UTC close, the newest that can exist, and the Trade status bar showed
// "13h ago · STALE" beside it.
func TestCurrentEntryAgesADailyCloseByItsDate(t *testing.T) {
	now := time.Date(2026, 9, 29, 9, 30, 0, 0, time.UTC) // Tuesday, 13:00 Tehran
	yesterday := latestPrice{Symbol: "IR_SILVER_999", Value: 506570, Currency: "IRT",
		Unit: "gram", Source: "tgju_history", ObservedAt: time.Date(2026, 9, 28, 23, 0, 0, 0, time.UTC)}
	entry := currentEntry("IR_SILVER_999", yesterday, 500000, true, now, 30, "12:00", "20:00")
	if entry["stale"] != false {
		t.Errorf("yesterday's settled close is the newest there is; stale = %v", entry["stale"])
	}
	if entry["cadence"] != cadenceDailyClose {
		t.Errorf("cadence = %v, want %q", entry["cadence"], cadenceDailyClose)
	}
	fiveDays := yesterday
	fiveDays.ObservedAt = time.Date(2026, 9, 23, 23, 0, 0, 0, time.UTC)
	if entry := currentEntry("IR_SILVER_999", fiveDays, 0, false, now, 30, "12:00", "20:00"); entry["stale"] != true {
		t.Error("a close six days old is missing closes: stale")
	}

	// A live-quote series keeps the minutes rule and carries no cadence.
	live := latestPrice{Symbol: "IR_GOLD_18K", Value: 9e6, Currency: "IRT", Unit: "gram",
		Source: "hamrahgold", ObservedAt: now.Add(-45 * time.Minute)}
	entry = currentEntry("IR_GOLD_18K", live, 0, false, now, 30, "12:00", "20:00")
	if entry["stale"] != true {
		t.Error("an 18k quote 45 minutes old during an open market is stale")
	}
	if _, ok := entry["cadence"]; ok {
		t.Error("a live-quote series carries no cadence")
	}
	if entry["change_24h_pct"] != nil {
		t.Error("no price 24h ago means no change")
	}
}

// A row stamped after now is not the current price: the Yahoo backfill stored
// today's forming Brent bar at 23:00 UTC today, and /prices/current served it.
func TestTheCurrentPriceIsNeverStampedAfterNow(t *testing.T) {
	if !strings.Contains(latestPricesSelect, "observed_at <= $1") {
		t.Fatalf("latestPricesSelect must exclude rows stamped after now:\n%s", latestPricesSelect)
	}
}
