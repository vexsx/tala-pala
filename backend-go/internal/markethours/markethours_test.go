package markethours

import (
	"testing"
	"time"
)

// Fixed reference week (all instants constructed in UTC; Asia/Tehran is
// UTC+03:30 with no DST since 2022):
//
//	2026-07-15 Wednesday   2026-07-16 Thursday   2026-07-17 Friday
//	2026-07-18 Saturday    2026-07-19 Sunday     2026-07-20 Monday
func utc(day, hour, min int) time.Time {
	return time.Date(2026, 7, day, hour, min, 0, 0, time.UTC)
}

func TestIsOpen(t *testing.T) {
	cases := []struct {
		name   string
		symbol string
		at     time.Time
		want   bool
	}{
		// IR_GOLD_18K: 24h/day on Iranian trading days (Sat-Wed);
		// the MARKET_TEHRAN_OPEN/CLOSE window does not apply.
		{"18k wed midday open", "IR_GOLD_18K", utc(15, 8, 30), true},        // 12:00 Tehran
		{"18k wed 21:00 open (24h)", "IR_GOLD_18K", utc(15, 17, 30), true},  // 21:00 Tehran, past window
		{"18k wed 03:00 open (24h)", "IR_GOLD_18K", utc(14, 23, 30), true},  // Wed 03:00 Tehran, pre-window
		{"18k thursday closed", "IR_GOLD_18K", utc(16, 8, 30), false},       // Thursday noon Tehran
		{"18k friday closed", "IR_GOLD_18K", utc(17, 8, 30), false},         // Friday noon Tehran
		{"18k saturday trades", "IR_GOLD_18K", utc(18, 8, 30), true},        // Saturday is a trading day

		// Windowed Iranian symbols: Sat-Wed 12:00-20:00 Asia/Tehran (UTC+03:30);
		// closed all Thursday and Friday.
		{"tehran open boundary 12:00", "IR_COIN_EMAMI", utc(15, 8, 30), true}, // 12:00 Tehran inclusive
		{"tehran before open 11:59", "IR_COIN_EMAMI", utc(15, 8, 29), false},  // 11:59 Tehran (USD has its own 10:00 open)
		{"tehran last minute 19:59", "IR_COIN_EMAMI", utc(15, 16, 29), true}, // 19:59 Tehran
		{"tehran close boundary 20:00", "IR_COIN_EMAMI", utc(15, 16, 30), false}, // 20:00 Tehran exclusive
		{"tehran thursday closed", "USD_IRT", utc(16, 8, 30), false},         // Thursday noon Tehran
		{"tehran friday closed", "USD_IRT", utc(17, 8, 30), false},           // Friday noon Tehran
		{"tehran saturday midday open", "USD_IRT", utc(18, 8, 30), true},     // Saturday 12:00 Tehran

		// Global symbols: closed Fri 21:00 UTC -> Sun 22:00 UTC.
		{"global wed midday open", "XAUUSD", utc(15, 12, 0), true},
		{"global fri 20:59 open", "XAUUSD", utc(17, 20, 59), true},
		{"global fri 21:00 closed", "XAUUSD", utc(17, 21, 0), false},
		{"global saturday closed", "DXY", utc(18, 12, 0), false},
		{"global sun 21:59 closed", "BRENT_OIL", utc(19, 21, 59), false},
		{"global sun 22:00 open", "BRENT_OIL", utc(19, 22, 0), true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := IsOpen(tc.symbol, tc.at, DefaultOpen, DefaultClose); got != tc.want {
				t.Fatalf("IsOpen(%s, %s) = %v, want %v", tc.symbol, tc.at, got, tc.want)
			}
		})
	}
}

func TestIsOpen_CustomHours(t *testing.T) {
	// IR_GOLD_18K ignores the configured window entirely: with a 10:00-14:00
	// session it is still open at any hour of a trading day...
	if !IsOpen("IR_GOLD_18K", utc(15, 8, 30), "10:00", "14:00") { // Wed 12:00 Tehran
		t.Fatal("18k must be open at 12:00 Tehran regardless of the window")
	}
	if !IsOpen("IR_GOLD_18K", utc(15, 11, 30), "10:00", "14:00") { // Wed 15:00 Tehran (outside window)
		t.Fatal("18k must be open at 15:00 Tehran: the session window does not apply")
	}
	// ...and still closed on the Thursday off-day, window or not.
	if IsOpen("IR_GOLD_18K", utc(16, 8, 30), "10:00", "14:00") { // Thu 12:00 Tehran
		t.Fatal("18k must stay closed all Thursday even with a custom window")
	}

	// Windowed symbols honor the custom session: 10:00-14:00 Tehran means
	// 12:00 Tehran open, 15:00 Tehran closed.
	if !IsOpen("USD_IRT", utc(15, 8, 30), "10:00", "14:00") {
		t.Fatal("12:00 Tehran should be open with 10:00-14:00 session")
	}
	if IsOpen("USD_IRT", utc(15, 11, 30), "10:00", "14:00") {
		t.Fatal("15:00 Tehran should be closed with 10:00-14:00 session")
	}
}

func TestClosureStartedAt(t *testing.T) {
	cases := []struct {
		name   string
		symbol string
		at     time.Time
		want   time.Time
	}{
		// 18k is closed only during the Thu+Fri block; the closure begins at
		// Thursday 00:00 Tehran = Wednesday 20:30 UTC.
		{"18k thursday noon", "IR_GOLD_18K", utc(16, 8, 30), utc(15, 20, 30)},
		{"18k friday noon", "IR_GOLD_18K", utc(17, 8, 30), utc(15, 20, 30)},
		// Friday 23:59 Tehran (= 20:29 UTC): still the same block.
		{"18k friday 23:59 tehran", "IR_GOLD_18K", utc(17, 20, 29), utc(15, 20, 30)},

		// Windowed symbols: Wed 21:00 Tehran -> closure began at Wed 20:00
		// Tehran = 16:30 UTC.
		{"usd wed evening", "USD_IRT", utc(15, 17, 30), utc(15, 16, 30)},
		// Thursday and Friday never trade, so the walk-back lands on
		// Wednesday's 20:00 Tehran close for the whole Thu+Fri block.
		{"usd thursday noon", "USD_IRT", utc(16, 8, 30), utc(15, 16, 30)},
		{"usd friday noon", "USD_IRT", utc(17, 8, 30), utc(15, 16, 30)},
		// Saturday 03:00 Tehran (= Fri 23:30 UTC): before Saturday's open,
		// so the last close is still Wednesday 20:00 Tehran.
		{"usd sat pre-open", "USD_IRT", utc(17, 23, 30), utc(15, 16, 30)},

		// Global weekend: closure began Friday 21:00 UTC.
		{"global saturday", "XAUUSD", utc(18, 12, 0), utc(17, 21, 0)},
		{"global sunday", "XAUUSD", utc(19, 21, 0), utc(17, 21, 0)},
		{"global friday night", "XAUUSD", utc(17, 22, 0), utc(17, 21, 0)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ClosureStartedAt(tc.symbol, tc.at, DefaultOpen, DefaultClose)
			if !got.Equal(tc.want) {
				t.Fatalf("ClosureStartedAt(%s, %s) = %s, want %s", tc.symbol, tc.at, got, tc.want)
			}
		})
	}

	// Open market: no closure in progress, returns `at` itself. Wed 21:00
	// Tehran is now an OPEN instant for the 24h symbol.
	at := utc(15, 17, 30)
	if got := ClosureStartedAt("IR_GOLD_18K", at, DefaultOpen, DefaultClose); !got.Equal(at) {
		t.Fatalf("open market closure start = %s, want %s", got, at)
	}
}

func TestAcceptablyFresh(t *testing.T) {
	const stale = 30
	cases := []struct {
		name     string
		symbol   string
		observed time.Time
		at       time.Time
		want     bool
	}{
		// Open market: plain age check against STALE_MINUTES.
		{"open young", "IR_GOLD_18K", utc(15, 8, 20), utc(15, 8, 30), true},
		{"open boundary 30m", "IR_GOLD_18K", utc(15, 8, 0), utc(15, 8, 30), true},
		{"open boundary 31m", "IR_GOLD_18K", utc(15, 7, 59), utc(15, 8, 30), false},
		// Wed 21:00 Tehran is open for 18k (24h): the age rule applies there too.
		{"open wed evening 30m", "IR_GOLD_18K", utc(15, 17, 0), utc(15, 17, 30), true},
		{"open wed evening 31m", "IR_GOLD_18K", utc(15, 16, 59), utc(15, 17, 30), false},

		// 18k Thursday noon Tehran: closed since Thu 00:00 Tehran = Wed 20:30
		// UTC; last-session data is fresh down to 20:30 - 30m = 20:00 UTC.
		{"18k thu last session", "IR_GOLD_18K", utc(15, 20, 10), utc(16, 8, 30), true},
		{"18k thu boundary 20:00", "IR_GOLD_18K", utc(15, 20, 0), utc(16, 8, 30), true},
		{"18k thu before window", "IR_GOLD_18K", utc(15, 19, 59), utc(16, 8, 30), false},

		// 18k Friday noon Tehran: same closure block (Wed 20:30 UTC start);
		// Wednesday-night data stays fresh through Friday, older data does not.
		{"18k friday wed-night ok", "IR_GOLD_18K", utc(15, 20, 15), utc(17, 8, 30), true},
		{"18k friday wed-morning stale", "IR_GOLD_18K", utc(15, 10, 0), utc(17, 8, 30), false},

		// USD_IRT Thursday noon Tehran: closure began Wed 20:00 Tehran =
		// 16:30 UTC; fresh down to 16:00 UTC.
		{"usd thu last session", "USD_IRT", utc(15, 16, 10), utc(16, 8, 30), true},
		{"usd thu boundary 16:00", "USD_IRT", utc(15, 16, 0), utc(16, 8, 30), true},
		{"usd thu before window", "USD_IRT", utc(15, 15, 59), utc(16, 8, 30), false},

		// Global weekend: closure began Fri 21:00 UTC.
		{"weekend fri close ok", "XAUUSD", utc(17, 20, 45), utc(18, 12, 0), true},
		{"weekend fri afternoon stale", "XAUUSD", utc(17, 18, 0), utc(18, 12, 0), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := AcceptablyFresh(tc.symbol, tc.observed, tc.at, stale, DefaultOpen, DefaultClose)
			if got != tc.want {
				t.Fatalf("AcceptablyFresh(%s, obs=%s, at=%s) = %v, want %v",
					tc.symbol, tc.observed, tc.at, got, tc.want)
			}
		})
	}
}

// The fallback zone must match real tzdata: Iran has used a fixed +03:30
// offset since DST was abolished in 2022.
func TestTehranOffset(t *testing.T) {
	_, offset := utc(15, 12, 0).In(tehran).Zone()
	if offset != 3*3600+30*60 {
		t.Fatalf("Asia/Tehran offset = %d seconds, want +03:30", offset)
	}
}

func TestTSEFundCalendar(t *testing.T) {
	// Tue 2026-07-21 13:00 Tehran (09:30 UTC): open
	if !IsOpen("IR_GOLD_FUND_AYAR", time.Date(2026, 7, 21, 9, 30, 0, 0, time.UTC), DefaultOpen, DefaultClose) {
		t.Fatal("Tuesday 13:00 Tehran must be open for TSE funds")
	}
	// Tue 17:00 Tehran (13:30 UTC): still open with the 18:00 close
	if !IsOpen("IR_GOLD_FUND_AYAR", time.Date(2026, 7, 21, 13, 30, 0, 0, time.UTC), DefaultOpen, DefaultClose) {
		t.Fatal("17:00 Tehran must be open for TSE funds")
	}
	// Tue 18:00 Tehran boundary (14:30 UTC): closed (exclusive)
	if IsOpen("IR_GOLD_FUND_AYAR", time.Date(2026, 7, 21, 14, 30, 0, 0, time.UTC), DefaultOpen, DefaultClose) {
		t.Fatal("18:00 Tehran must be closed for TSE funds")
	}
	// Thursday: closed for funds and for the physical market alike
	thu := time.Date(2026, 7, 23, 9, 30, 0, 0, time.UTC)
	if IsOpen("IR_GOLD_FUND_FLOW", thu, DefaultOpen, DefaultClose) {
		t.Fatal("Thursday must be closed for TSE funds")
	}
	if IsOpen("IR_GOLD_18K", thu, DefaultOpen, DefaultClose) {
		t.Fatal("Thursday must be closed for the physical market too")
	}
	// Friday noon: last close was Wednesday 18:00 Tehran = 14:30 UTC
	got := ClosureStartedAt("IR_GOLD_FUND_AYAR", time.Date(2026, 7, 24, 9, 0, 0, 0, time.UTC), DefaultOpen, DefaultClose)
	want := time.Date(2026, 7, 22, 14, 30, 0, 0, time.UTC)
	if !got.Equal(want) {
		t.Fatalf("closure start = %s, want %s", got, want)
	}
}

func TestUSDOpensAt10Tehran(t *testing.T) {
	// Wed 10:30 Tehran (07:00 UTC): USD open, coin market still closed
	at := time.Date(2026, 7, 15, 7, 0, 0, 0, time.UTC)
	if !IsOpen("USD_IRT", at, DefaultOpen, DefaultClose) {
		t.Fatal("USD_IRT must be open at 10:30 Tehran")
	}
	if IsOpen("IR_COIN_EMAMI", at, DefaultOpen, DefaultClose) {
		t.Fatal("IR_COIN_EMAMI must still be closed before 12:00 Tehran")
	}
	// 09:59 Tehran (06:29 UTC): not yet open
	if IsOpen("USD_IRT", time.Date(2026, 7, 15, 6, 29, 0, 0, time.UTC), DefaultOpen, DefaultClose) {
		t.Fatal("USD_IRT must be closed at 09:59 Tehran")
	}
}
