package markethours

import (
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/bourse"
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
		{"tehran open boundary 12:00", "IR_COIN_EMAMI", utc(15, 8, 30), true},    // 12:00 Tehran inclusive
		{"tehran before open 11:59", "IR_COIN_EMAMI", utc(15, 8, 29), false},     // 11:59 Tehran
		{"tehran last minute 19:59", "IR_COIN_EMAMI", utc(15, 16, 29), true},     // 19:59 Tehran
		{"tehran close boundary 20:00", "IR_COIN_EMAMI", utc(15, 16, 30), false}, // 20:00 Tehran exclusive
		{"coin thursday closed", "IR_COIN_EMAMI", utc(16, 8, 30), false},         // Thursday noon Tehran
		{"coin friday closed", "IR_COIN_EMAMI", utc(17, 8, 30), false},           // Friday noon Tehran
		{"coin saturday midday open", "IR_COIN_EMAMI", utc(18, 8, 30), true},     // Saturday 12:00 Tehran

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

// Silver and saffron commodity funds get the session the gold funds always had,
// and the gold-fund flow ratio keeps it. A symbol that merely starts with IR_
// and is not a fund -- silver 999 above all -- does not.
func TestTSEFundRuleCoversEveryUnderlying(t *testing.T) {
	tue13 := time.Date(2026, 7, 21, 9, 30, 0, 0, time.UTC)  // Tue 13:00 Tehran
	tue18 := time.Date(2026, 7, 21, 14, 30, 0, 0, time.UTC) // Tue 18:00 Tehran
	for _, code := range []string{
		"IR_GOLD_FUND_AYAR", "IR_GOLD_FUND_FLOW",
		"IR_SILVER_FUND_SILVER", "IR_SILVER_FUND_SIMIN", "IR_SAFFRON_FUND_SAFRON",
	} {
		if !isTSEFund(code) {
			t.Errorf("%s must follow the TSE fund session", code)
		}
		if !IsOpen(code, tue13, DefaultOpen, DefaultClose) {
			t.Errorf("%s: Tuesday 13:00 Tehran must be open", code)
		}
		// 18:00 is the fund close, two hours before the bazaar's 20:00.
		if IsOpen(code, tue18, DefaultOpen, DefaultClose) {
			t.Errorf("%s: 18:00 Tehran must be closed for a fund", code)
		}
	}
	for _, code := range []string{"IR_SILVER_999", "IR_GOLD_18K", "IR_COIN_EMAMI", "FUND", "XAUUSD"} {
		if isTSEFund(code) {
			t.Errorf("%s is not a fund", code)
		}
	}
}

// Migration 0031's TGJU daily-close codes follow the bazaar, like the Emami
// coin: open on a Saturday session, closed Thursday and Friday, and Friday's
// closure traces back to Wednesday's 20:00 close.
func TestTGJUDailyCodesFollowTheBazaarCalendar(t *testing.T) {
	for _, code := range []string{
		"IR_SILVER_999", "IR_COIN_BAHAR", "IR_COIN_HALF", "IR_COIN_QUARTER",
		"IR_COIN_GERAMI", "IR_GOLD_24K", "IR_GOLD_MESGHAL",
	} {
		if !IsOpen(code, utc(18, 9, 0), DefaultOpen, DefaultClose) { // Sat 12:30 Tehran
			t.Errorf("%s: Saturday 12:30 Tehran must be open", code)
		}
		if IsOpen(code, utc(16, 8, 30), DefaultOpen, DefaultClose) { // Thursday
			t.Errorf("%s: Thursday must be closed", code)
		}
		if IsOpen(code, utc(15, 16, 30), DefaultOpen, DefaultClose) { // Wed 20:00
			t.Errorf("%s: the 20:00 Tehran close is exclusive", code)
		}
		if got := ClosureStartedAt(code, utc(17, 8, 30), DefaultOpen, DefaultClose); !got.Equal(utc(15, 16, 30)) {
			t.Errorf("%s: Friday's closure began %s, want Wednesday 20:00 Tehran", code, got)
		}
		// A close from Wednesday's session is still acceptably fresh on Friday:
		// there has been no session since to replace it.
		if !AcceptablyFresh(code, utc(15, 16, 10), utc(17, 8, 30), 30, DefaultOpen, DefaultClose) {
			t.Errorf("%s: Wednesday's close must not read as stale during the closure", code)
		}
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

// A series that only ever receives one settled close per session was always
// stale under the minutes rule: during bazaar hours its newest row is
// yesterday's 23:00 UTC close, and today's does not exist yet. The Trade
// status bar showed "13h ago · STALE" beside data as fresh as it can be.
func TestADailyCloseIsFreshUntilItIsMoreThanFourDaysOld(t *testing.T) {
	closeOf := func(day int) time.Time { return utc(day, 23, 0) } // 23:00 UTC on its date
	cases := []struct {
		name     string
		observed time.Time
		at       time.Time
		want     bool
	}{
		// Monday 13:00 Tehran, Sunday's close the newest that exists.
		{"yesterday's close mid-session", closeOf(19), utc(20, 9, 30), true},
		// Saturday morning: Wednesday's is the newest, three days back.
		{"the weekend", closeOf(15), utc(18, 6, 0), true},
		// A holiday on Saturday as well: four days, still the newest.
		{"the weekend and a holiday", closeOf(15), utc(19, 6, 0), true},
		// Five days: a close is missing.
		{"five days", closeOf(15), utc(20, 6, 0), false},
		// The age is counted in Tehran's date, which turns at 20:30 UTC.
		{"four days until Tehran midnight", closeOf(15), utc(19, 20, 29), true},
		{"five days after it", closeOf(15), utc(19, 20, 31), false},
	}
	for _, code := range []string{"IR_SILVER_999", "IR_GOLD_24K", "IR_COIN_BAHAR", "IR_GOLD_MESGHAL"} {
		if !DailyCloseOnly(code) {
			t.Fatalf("%s only receives daily closes", code)
		}
		for _, tc := range cases {
			if got := AcceptablyFresh(code, tc.observed, tc.at, 30, DefaultOpen, DefaultClose); got != tc.want {
				t.Errorf("%s %s: AcceptablyFresh = %v, want %v", code, tc.name, got, tc.want)
			}
			if got := AcceptablyFreshFrom(code, SourceTGJUHistory, tc.observed, tc.at, 30,
				DefaultOpen, DefaultClose); got != tc.want {
				t.Errorf("%s %s from TGJU: AcceptablyFresh = %v, want %v", code, tc.name, got, tc.want)
			}
		}
	}
	// Live-quote series keep the session rule: the funds with BrsApi's
	// intraday mirror, 18k gold, the Emami coin.
	for _, code := range []string{"IR_GOLD_FUND_AYAR", "IR_GOLD_FUND_TALA", "IR_GOLD_18K",
		"IR_COIN_EMAMI", "XAUUSD", "IR_GOLD_FUND_FLOW"} {
		if DailyCloseOnly(code) {
			t.Errorf("%s has a live source and keeps the session rule", code)
		}
	}
	if AcceptablyFresh("IR_COIN_EMAMI", closeOf(19), utc(20, 9, 30), 30, DefaultOpen, DefaultClose) {
		t.Error("a live-quote series a session old is stale, as before")
	}
}

// The funds with no live quote get their closes from the weekly off-server
// TSETMC fetch, in the same run as the indices and the shares, whose pages
// call that run stopped after ten days. On the four-day rule a Friday run
// that stored Wednesday's close read STALE from Monday to Thursday — five
// days in seven on an on-time weekly refresh — beside index charts of the
// same run reading fresh.
func TestAWeeklyFetchedFundCloseKeepsTheWeeklyRunsBound(t *testing.T) {
	wed := utc(15, 23, 0) // Wednesday's settled close, stored by Friday the 17th's run
	for _, code := range []string{"IR_SILVER_FUND_SILVER", "IR_SILVER_FUND_SIMIN", "IR_GOLD_FUND_KAHRABA"} {
		for _, source := range []string{"", SourceTSETMCCloses} {
			for day := 18; day <= 25; day++ { // Saturday to the next Saturday
				if !AcceptablyFreshFrom(code, source, wed, utc(day, 9, 30), 30, DefaultOpen, DefaultClose) {
					t.Errorf("%s (source %q): Wednesday's close stored by Friday's run is stale on "+
						"July %d", code, source, day)
				}
			}
			// Eleven days: the next Friday's run has not stored anything either.
			if AcceptablyFreshFrom(code, source, wed, utc(26, 9, 30), 30, DefaultOpen, DefaultClose) {
				t.Errorf("%s: eleven days old is a weekly run that has stopped", code)
			}
		}
	}
	if WeeklyFetchStaleDays != bourse.StaleAfterDays {
		t.Fatalf("WeeklyFetchStaleDays = %d, bourse.StaleAfterDays = %d: one run, one boundary",
			WeeklyFetchStaleDays, bourse.StaleAfterDays)
	}
}

// The source decides, not the symbol: کهربا configured live (TSETMC_FUNDS)
// has BrsApi's intraday quotes, which keep the session rule — on the
// symbol's list an intraday quote three days old during the session read
// fresh, and was labelled a daily close. And a symbol with a live source by default is never judged as
// a daily close, whatever wrote its newest row (TGJU's gap-fill writes the
// Emami coin's missing days as closes).
func TestTheNewestRowsSourceDecidesTheDailyCloseRule(t *testing.T) {
	session := utc(20, 9, 30) // Monday 13:00 Tehran, the fund session open
	quote := utc(18, 9, 0)    // Saturday's live quote, two days old
	if AcceptablyFreshFrom("IR_GOLD_FUND_KAHRABA", "tse_funds", quote, session, 30, DefaultOpen, DefaultClose) {
		t.Error("a live fund quote two days old during the session is stale")
	}
	if _, daily := DailyClose("IR_GOLD_FUND_KAHRABA", "tse_funds"); daily {
		t.Error("a live quote is not a daily close")
	}
	if maxAge, daily := DailyClose("IR_GOLD_FUND_KAHRABA", SourceTSETMCCloses); !daily || maxAge != WeeklyFetchStaleDays {
		t.Errorf("the weekly fetch's close: %d %v", maxAge, daily)
	}
	for _, code := range []string{"IR_COIN_EMAMI", "IR_GOLD_18K", "USD_IRT", "IR_GOLD_FUND_AYAR"} {
		if _, daily := DailyClose(code, SourceTGJUHistory); daily {
			t.Errorf("%s keeps its live rule whatever wrote its newest row", code)
		}
		if _, daily := DailyClose(code, SourceTSETMCCloses); daily {
			t.Errorf("%s keeps its live rule whatever wrote its newest row", code)
		}
	}
}
