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
		// Always-open symbols (Hamrah Gold / the USDT market quote 24/7):
		// every hour of every day, including Thursday and Friday.
		{"18k wed midday open", "IR_GOLD_18K", utc(15, 8, 30), true},
		{"18k wed 21:00 open", "IR_GOLD_18K", utc(15, 17, 30), true},
		{"18k thursday open", "IR_GOLD_18K", utc(16, 8, 30), true},
		{"18k friday open", "IR_GOLD_18K", utc(17, 8, 30), true},
		{"usd thursday open", "USD_IRT", utc(16, 8, 30), true},
		{"usd friday open", "USD_IRT", utc(17, 8, 30), true},
		{"usd wed 03:00 open", "USD_IRT", utc(14, 23, 30), true},

		// Windowed Iranian symbol (the coin): Sat-Wed 12:00-20:00 Tehran;
		// closed all Thursday and Friday.
		{"tehran open boundary 12:00", "IR_COIN_EMAMI", utc(15, 8, 30), true},     // 12:00 Tehran inclusive
		{"tehran before open 11:59", "IR_COIN_EMAMI", utc(15, 8, 29), false},      // 11:59 Tehran
		{"tehran last minute 19:59", "IR_COIN_EMAMI", utc(15, 16, 29), true},      // 19:59 Tehran
		{"tehran close boundary 20:00", "IR_COIN_EMAMI", utc(15, 16, 30), false},  // 20:00 Tehran exclusive
		{"coin thursday closed", "IR_COIN_EMAMI", utc(16, 8, 30), false},          // Thursday noon Tehran
		{"coin friday closed", "IR_COIN_EMAMI", utc(17, 8, 30), false},            // Friday noon Tehran
		{"coin saturday midday open", "IR_COIN_EMAMI", utc(18, 8, 30), true},      // Saturday 12:00 Tehran

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
	// Always-open symbols ignore the configured window entirely — any hour,
	// any day, including Thursday.
	if !IsOpen("IR_GOLD_18K", utc(15, 11, 30), "10:00", "14:00") { // Wed 15:00 Tehran (outside window)
		t.Fatal("18k must be open regardless of the session window")
	}
	if !IsOpen("USD_IRT", utc(16, 8, 30), "10:00", "14:00") { // Thu 12:00 Tehran
		t.Fatal("USD must be open on Thursday: the USDT market never closes")
	}

	// The windowed coin honors the custom session: 10:00-14:00 Tehran means
	// 12:00 Tehran open, 15:00 Tehran closed.
	if !IsOpen("IR_COIN_EMAMI", utc(15, 8, 30), "10:00", "14:00") {
		t.Fatal("12:00 Tehran should be open with 10:00-14:00 session")
	}
	if IsOpen("IR_COIN_EMAMI", utc(15, 11, 30), "10:00", "14:00") {
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
		// The windowed coin: Wed 21:00 Tehran -> closure began at Wed 20:00
		// Tehran = 16:30 UTC.
		{"coin wed evening", "IR_COIN_EMAMI", utc(15, 17, 30), utc(15, 16, 30)},
		// Thursday and Friday never trade, so the walk-back lands on
		// Wednesday's 20:00 Tehran close for the whole Thu+Fri block.
		{"coin thursday noon", "IR_COIN_EMAMI", utc(16, 8, 30), utc(15, 16, 30)},
		{"coin friday noon", "IR_COIN_EMAMI", utc(17, 8, 30), utc(15, 16, 30)},
		// Saturday 03:00 Tehran (= Fri 23:30 UTC): before Saturday's open,
		// so the last close is still Wednesday 20:00 Tehran.
		{"coin sat pre-open", "IR_COIN_EMAMI", utc(17, 23, 30), utc(15, 16, 30)},

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

	// Always-open symbols never enter a closure: any instant returns itself,
	// including Thursday and Friday.
	for _, at := range []time.Time{utc(15, 17, 30), utc(16, 8, 30), utc(17, 20, 29)} {
		if got := ClosureStartedAt("IR_GOLD_18K", at, DefaultOpen, DefaultClose); !got.Equal(at) {
			t.Fatalf("18k closure start = %s, want %s (always open)", got, at)
		}
		if got := ClosureStartedAt("USD_IRT", at, DefaultOpen, DefaultClose); !got.Equal(at) {
			t.Fatalf("usd closure start = %s, want %s (always open)", got, at)
		}
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

		// 18k and USD are open on Thursday/Friday too (always-open sources):
		// the plain age rule applies — Wednesday data is honestly stale by
		// Thursday noon, even though it would have survived the old closure.
		{"18k thu plain age ok", "IR_GOLD_18K", utc(16, 8, 5), utc(16, 8, 30), true},
		{"18k thu plain age stale", "IR_GOLD_18K", utc(15, 20, 10), utc(16, 8, 30), false},
		{"usd fri plain age ok", "USD_IRT", utc(17, 8, 10), utc(17, 8, 30), true},
		{"usd fri plain age stale", "USD_IRT", utc(15, 16, 10), utc(17, 8, 30), false},

		// The windowed coin on Thursday noon Tehran: closure began Wed 20:00
		// Tehran = 16:30 UTC; fresh down to 16:00 UTC.
		{"coin thu last session", "IR_COIN_EMAMI", utc(15, 16, 10), utc(16, 8, 30), true},
		{"coin thu boundary 16:00", "IR_COIN_EMAMI", utc(15, 16, 0), utc(16, 8, 30), true},
		{"coin thu before window", "IR_COIN_EMAMI", utc(15, 15, 59), utc(16, 8, 30), false},

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
	// Thursday: closed for funds; the physical 18k quote stays open 24/7
	thu := time.Date(2026, 7, 23, 9, 30, 0, 0, time.UTC)
	if IsOpen("IR_GOLD_FUND_FLOW", thu, DefaultOpen, DefaultClose) {
		t.Fatal("Thursday must be closed for TSE funds")
	}
	if !IsOpen("IR_GOLD_18K", thu, DefaultOpen, DefaultClose) {
		t.Fatal("18k must stay open on Thursday (always-open source)")
	}
	// Friday noon: last close was Wednesday 18:00 Tehran = 14:30 UTC
	got := ClosureStartedAt("IR_GOLD_FUND_AYAR", time.Date(2026, 7, 24, 9, 0, 0, 0, time.UTC), DefaultOpen, DefaultClose)
	want := time.Date(2026, 7, 22, 14, 30, 0, 0, time.UTC)
	if !got.Equal(want) {
		t.Fatalf("closure start = %s, want %s", got, want)
	}
}

func TestUSDAlwaysOpen(t *testing.T) {
	// USD follows the 24/7 USDT market: open at any hour, any day.
	for _, at := range []time.Time{
		time.Date(2026, 7, 15, 7, 0, 0, 0, time.UTC),  // Wed 10:30 Tehran
		time.Date(2026, 7, 15, 6, 29, 0, 0, time.UTC), // Wed 09:59 Tehran
		time.Date(2026, 7, 23, 9, 30, 0, 0, time.UTC), // Thursday
		time.Date(2026, 7, 24, 1, 0, 0, 0, time.UTC),  // Friday pre-dawn
	} {
		if !IsOpen("USD_IRT", at, DefaultOpen, DefaultClose) {
			t.Fatalf("USD_IRT must be open at %s (USDT never closes)", at)
		}
	}
	// The coin keeps its bazaar window: closed before 12:00 Tehran.
	if IsOpen("IR_COIN_EMAMI", time.Date(2026, 7, 15, 7, 0, 0, 0, time.UTC), DefaultOpen, DefaultClose) {
		t.Fatal("IR_COIN_EMAMI must still be closed before 12:00 Tehran")
	}
}
