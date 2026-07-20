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
		// Iranian symbols: Sat-Thu 09:00-20:00 Asia/Tehran (UTC+03:30).
		{"tehran wed midday open", "IR_GOLD_18K", utc(15, 8, 30), true},     // 12:00 Tehran
		{"tehran wed 21:00 closed", "IR_GOLD_18K", utc(15, 17, 30), false},  // 21:00 Tehran
		{"tehran open boundary 09:00", "USD_IRT", utc(15, 5, 30), true},     // 09:00 Tehran inclusive
		{"tehran before open 08:59", "USD_IRT", utc(15, 5, 29), false},      // 08:59 Tehran
		{"tehran last minute 19:59", "IR_COIN_EMAMI", utc(15, 16, 29), true},// 19:59 Tehran
		{"tehran close boundary 20:00", "IR_COIN_EMAMI", utc(15, 16, 30), false}, // 20:00 Tehran exclusive
		{"tehran friday closed", "IR_GOLD_18K", utc(17, 8, 30), false},      // Friday noon Tehran
		{"tehran saturday trades", "IR_GOLD_18K", utc(18, 8, 30), true},     // Saturday is a trading day
		{"tehran thursday trades", "IR_GOLD_18K", utc(16, 8, 30), true},

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
	// Session 10:00-14:00 Tehran: 12:00 Tehran open, 15:00 Tehran closed.
	if !IsOpen("IR_GOLD_18K", utc(15, 8, 30), "10:00", "14:00") {
		t.Fatal("12:00 Tehran should be open with 10:00-14:00 session")
	}
	if IsOpen("IR_GOLD_18K", utc(15, 11, 30), "10:00", "14:00") {
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
		// Wed 21:00 Tehran: closure began at Wed 20:00 Tehran = 16:30 UTC.
		{"tehran evening", "IR_GOLD_18K", utc(15, 17, 30), utc(15, 16, 30)},
		// Friday noon Tehran: closure began Thursday 20:00 Tehran.
		{"tehran friday", "IR_GOLD_18K", utc(17, 8, 30), utc(16, 16, 30)},
		// Saturday 03:00 Tehran (= Fri 23:30 UTC): Friday never traded, so
		// the closure still dates back to Thursday 20:00 Tehran.
		{"tehran sat pre-open", "IR_GOLD_18K", utc(17, 23, 30), utc(16, 16, 30)},
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

	// Open market: no closure in progress, returns `at` itself.
	at := utc(15, 8, 30)
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

		// Wed 21:00 Tehran (closed since 16:30 UTC): last-session data is
		// fresh down to closure start - 30m = 16:00 UTC.
		{"closed last session", "IR_GOLD_18K", utc(15, 16, 10), utc(15, 17, 30), true},
		{"closed boundary at 16:00", "IR_GOLD_18K", utc(15, 16, 0), utc(15, 17, 30), true},
		{"closed before window", "IR_GOLD_18K", utc(15, 15, 59), utc(15, 17, 30), false},

		// Friday noon Tehran: closure began Thu 16:30 UTC; Thursday-evening
		// data stays fresh all Friday, Thursday-morning data does not.
		{"friday thu evening ok", "IR_GOLD_18K", utc(16, 16, 20), utc(17, 8, 30), true},
		{"friday thu morning stale", "IR_GOLD_18K", utc(16, 10, 0), utc(17, 8, 30), false},

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
