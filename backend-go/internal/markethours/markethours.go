// Package markethours implements the Addendum 1 market-calendar rules,
// mirrored between the Go and Python services from the same env vars:
//
//   - IR_GOLD_18K and USD_IRT are ALWAYS open: their primary sources quote
//     24/7 every day (Hamrah Gold; the USDT market). Only the plain
//     STALE_MINUTES age rule applies.
//   - IR_COIN_EMAMI and the TGJU daily-close instruments of migration 0031
//     (silver 999, the Bahar Azadi, half, quarter and gram coins, 24k gold,
//     melted gold per mesghal) trade Sat-Wed between MARKET_TEHRAN_OPEN and
//     MARKET_TEHRAN_CLOSE (Asia/Tehran local, open inclusive, close
//     exclusive); closed all Thursday and Friday.
//   - Tehran-exchange commodity funds (IR_<underlying>_FUND_*: gold, silver,
//     saffron) trade Sat-Wed 12:00-18:00 Tehran; closed Thursday and Friday.
//   - Global symbols (XAUUSD, XAGUSD, BRENT_OIL, DXY, US10Y, ...) are closed
//     from Friday 21:00 UTC until Sunday 22:00 UTC.
//
// Freshness follows the addendum: while a market is OPEN, data older than
// STALE_MINUTES is stale; while CLOSED, data observed during the last session
// (observed_at >= closure start - STALE_MINUTES) is still acceptably fresh.
package markethours

import (
	"strings"
	"time"
)

// Contract defaults for the Tehran session (Asia/Tehran local, HH:MM).
const (
	DefaultOpen  = "12:00"
	DefaultClose = "20:00"
)

// TSE commodity-fund session (Addendum 7): Sat-Wed 12:00-18:00 Asia/Tehran,
// closed Thursday AND Friday. Fixed here (display freshness only); the
// Python service reads MARKET_TSE_OPEN/CLOSE for prediction-side rules.
const (
	tseOpen  = "12:00"
	tseClose = "18:00"
)

// isTSEFund reports whether symbol is a Tehran-exchange commodity fund,
// IR_<underlying>_FUND*. It is a rule over the code rather than the old
// "IR_GOLD_FUND" prefix so the silver and saffron funds (IR_SILVER_FUND_*,
// IR_SAFFRON_FUND_*) get the session the gold funds always had.
// IR_GOLD_FUND_FLOW stays on it, as it always was: the flow ratio is computed
// from the funds' own sessions (migration 0024 files it under calendar_class
// 'tse_session'). IR_SILVER_999 is not a fund.
func isTSEFund(symbol string) bool {
	parts := strings.Split(symbol, "_")
	return len(parts) >= 3 && parts[0] == "IR" && parts[2] == "FUND"
}

// alwaysOpen: primary sources quote 24/7 every day of the week (Hamrah
// Gold for 18k; the USDT market for the free-market dollar).
var alwaysOpen = map[string]bool{
	"IR_GOLD_18K": true,
	"USD_IRT":     true,
}

// iranian is the set of symbols that follow the Tehran bazaar calendar.
// Every other symbol follows the global (UTC weekend) calendar. The seven
// migration 0031 codes are one settled close per day from TGJU, but the market
// they close is the bazaar, so the last session's close stays acceptably fresh
// through the Thursday+Friday closure instead of reading as stale.
var iranian = map[string]bool{
	"IR_COIN_EMAMI":   true,
	"IR_SILVER_999":   true,
	"IR_COIN_BAHAR":   true,
	"IR_COIN_HALF":    true,
	"IR_COIN_QUARTER": true,
	"IR_COIN_GERAMI":  true,
	"IR_GOLD_24K":     true,
	"IR_GOLD_MESGHAL": true,
}

// tehran is the Asia/Tehran location. The runtime container installs tzdata
// (apk) and Go toolchains ship zoneinfo, so LoadLocation normally succeeds;
// the fallback is the fixed +03:30 offset (Iran abolished DST in 2022).
var tehran = loadTehran()

func loadTehran() *time.Location {
	if loc, err := time.LoadLocation("Asia/Tehran"); err == nil {
		return loc
	}
	return time.FixedZone("Asia/Tehran", 3*3600+30*60)
}

// parseHHMM parses "HH:MM" into minutes since midnight, falling back to the
// given default when the value is empty or malformed (config validates the
// env vars at boot; the fallback keeps this package total).
func parseHHMM(s, def string) int {
	t, err := time.Parse("15:04", s)
	if err != nil {
		t, _ = time.Parse("15:04", def)
	}
	return t.Hour()*60 + t.Minute()
}

// IsOpen reports whether the market for symbol is open at the instant `at`.
// open/close are "HH:MM" Tehran-local session bounds (only used for Iranian
// symbols); pass the configured MARKET_TEHRAN_OPEN / MARKET_TEHRAN_CLOSE.
func IsOpen(symbol string, at time.Time, open, close string) bool {
	if alwaysOpen[symbol] {
		return true
	}
	if isTSEFund(symbol) {
		lt := at.In(tehran)
		if lt.Weekday() == time.Thursday || lt.Weekday() == time.Friday {
			return false
		}
		m := lt.Hour()*60 + lt.Minute()
		return m >= parseHHMM(tseOpen, tseOpen) && m < parseHHMM(tseClose, tseClose)
	}
	if iranian[symbol] {
		lt := at.In(tehran)
		if lt.Weekday() == time.Thursday || lt.Weekday() == time.Friday {
			return false
		}
		m := lt.Hour()*60 + lt.Minute()
		return m >= parseHHMM(open, DefaultOpen) && m < parseHHMM(close, DefaultClose)
	}
	u := at.UTC()
	switch u.Weekday() {
	case time.Friday:
		return u.Hour() < 21
	case time.Saturday:
		return false
	case time.Sunday:
		return u.Hour() >= 22
	default:
		return true
	}
}

// ClosureStartedAt returns the UTC instant the closure containing `at`
// began: the end of the most recent trading session. When the market is open
// at `at` it returns `at` itself (no closure in progress).
func ClosureStartedAt(symbol string, at time.Time, open, close string) time.Time {
	if IsOpen(symbol, at, open, close) {
		return at.UTC()
	}
	if isTSEFund(symbol) {
		closeM := parseHHMM(tseClose, tseClose)
		lt := at.In(tehran)
		day := time.Date(lt.Year(), lt.Month(), lt.Day(), 0, 0, 0, 0, tehran)
		for i := 0; i < 9; i++ {
			d := day.AddDate(0, 0, -i)
			if d.Weekday() == time.Thursday || d.Weekday() == time.Friday {
				continue
			}
			closeT := d.Add(time.Duration(closeM) * time.Minute)
			if !closeT.After(lt) {
				return closeT.UTC()
			}
		}
		return at.UTC() // unreachable
	}
	if iranian[symbol] {
		closeM := parseHHMM(close, DefaultClose)
		lt := at.In(tehran)
		day := time.Date(lt.Year(), lt.Month(), lt.Day(), 0, 0, 0, 0, tehran)
		// Walk back to the most recent trading-day close at or before `at`.
		// Any 9-day span contains a non-Thu/Fri close in the past.
		for i := 0; i < 9; i++ {
			d := day.AddDate(0, 0, -i)
			if d.Weekday() == time.Thursday || d.Weekday() == time.Friday {
				continue
			}
			closeT := d.Add(time.Duration(closeM) * time.Minute)
			if !closeT.After(lt) {
				return closeT.UTC()
			}
		}
		return at.UTC() // unreachable
	}
	// Global markets close Friday 21:00 UTC.
	u := at.UTC()
	daysBack := (int(u.Weekday()) - int(time.Friday) + 7) % 7
	fri := time.Date(u.Year(), u.Month(), u.Day(), 21, 0, 0, 0, time.UTC).AddDate(0, 0, -daysBack)
	if fri.After(u) {
		fri = fri.AddDate(0, 0, -7)
	}
	return fri
}

// AcceptablyFresh reports whether an observation from observedAt is
// acceptably fresh at `at` under the Addendum 1 rules:
//
//	market open:   age <= staleMinutes
//	market closed: observedAt >= closure start - staleMinutes
//	               (i.e. last-session data never goes stale overnight)
func AcceptablyFresh(symbol string, observedAt, at time.Time, staleMinutes int, open, close string) bool {
	stale := time.Duration(staleMinutes) * time.Minute
	if IsOpen(symbol, at, open, close) {
		return at.Sub(observedAt) <= stale
	}
	return !observedAt.Before(ClosureStartedAt(symbol, at, open, close).Add(-stale))
}
