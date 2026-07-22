// Package markethours implements the Addendum 1 market-calendar rules,
// mirrored between the Go and Python services from the same env vars:
//
//   - IR_GOLD_18K trades 24h/day on Iranian trading days: its primary source
//     (Milli Gold) is an online platform with no intraday session, but the
//     Iranian off-days still apply — closed all Thursday and Friday (Tehran).
//   - Other Iranian symbols (USD_IRT, IR_COIN_EMAMI) trade Sat-Wed between
//     MARKET_TEHRAN_OPEN and MARKET_TEHRAN_CLOSE (Asia/Tehran local,
//     open inclusive, close exclusive); closed all Thursday and Friday.
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

// TSE gold-fund session (Addendum 7): Sat-Wed 12:00-18:00 Asia/Tehran,
// closed Thursday AND Friday. Fixed here (display freshness only); the
// Python service reads MARKET_TSE_OPEN/CLOSE for prediction-side rules.
const (
	tseOpen  = "12:00"
	tseClose = "18:00"
)

// usdOpen: the free-market USD session opens earlier than the gold bazaar
// (close is shared). Fixed here for display freshness; the Python service
// reads MARKET_USD_OPEN for prediction-side rules.
const usdOpen = "10:00"

// tseFundPrefix marks Tehran-exchange gold-fund symbols.
const tseFundPrefix = "IR_GOLD_FUND"

// iran24h: symbols whose primary source trades around the clock on Iranian
// trading days. IR_GOLD_18K's primary provider is Milli Gold (milli.gold),
// a 24-hour online platform — no intraday session window, but the Iranian
// off-days (Thursday + Friday, Tehran) still close the market.
var iran24h = map[string]bool{
	"IR_GOLD_18K": true,
}

// iranian is the set of symbols that follow the Tehran bazaar calendar.
// Every other symbol follows the global (UTC weekend) calendar.
var iranian = map[string]bool{
	"USD_IRT":       true,
	"IR_COIN_EMAMI": true,
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
	if iran24h[symbol] {
		wd := at.In(tehran).Weekday()
		return wd != time.Thursday && wd != time.Friday
	}
	if strings.HasPrefix(symbol, tseFundPrefix) {
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
		openM := parseHHMM(open, DefaultOpen)
		if symbol == "USD_IRT" {
			openM = parseHHMM(usdOpen, usdOpen)
		}
		return m >= openM && m < parseHHMM(close, DefaultClose)
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
	if iran24h[symbol] {
		// Closed only during the Thu+Fri block; the closure began at
		// Thursday 00:00 Tehran of the current block.
		lt := at.In(tehran)
		day := time.Date(lt.Year(), lt.Month(), lt.Day(), 0, 0, 0, 0, tehran)
		if lt.Weekday() == time.Friday {
			day = day.AddDate(0, 0, -1)
		}
		return day.UTC()
	}
	if strings.HasPrefix(symbol, tseFundPrefix) {
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
