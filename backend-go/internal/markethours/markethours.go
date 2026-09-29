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
//
// Series that only ever receive ONE settled close per session (DailyCloseOnly)
// have their own rule instead, because the minutes rule can only ever call
// them stale: their newest row is yesterday's close, and today's does not
// exist until the session has settled. How old that close may be depends on
// what brings it (DailyClose): TGJU's daily job, DailyCloseStaleDays; the
// weekly off-server TSETMC fetch, WeeklyFetchStaleDays.
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

// The sources that write one settled close per session, stamped 23:00 UTC on
// its own date. They are what a daily close's staleness is judged by, not the
// symbol: a symbol on these lists can be given a live source by configuration
// (TSETMC_FUNDS names کهربا in .env.example, and BrsApi then quotes it every
// session), and its live quotes must keep the session rule.
const (
	// SourceTGJUHistory: TGJU's daily history table, read twice a day by the
	// tgju-daily job (migration 0031).
	SourceTGJUHistory = "tgju_history"
	// SourceTSETMCCloses: the commodity funds' settled closes, stored by the
	// weekly off-server TSETMC fetch (migration 0030).
	SourceTSETMCCloses = "tsetmc_cdn"
)

// tgjuDailyCloses are migration 0031's seven TGJU daily-history instruments,
// and weeklyFetchCloses the commodity funds with no live source by default,
// whose only rows are the weekly TSETMC fetch's settled closes. AYAR and TALA
// have BrsApi's intraday mirror by default and keep the session rule.
var (
	tgjuDailyCloses = map[string]bool{
		"IR_SILVER_999":   true,
		"IR_COIN_BAHAR":   true,
		"IR_COIN_HALF":    true,
		"IR_COIN_QUARTER": true,
		"IR_COIN_GERAMI":  true,
		"IR_GOLD_24K":     true,
		"IR_GOLD_MESGHAL": true,
	}
	weeklyFetchCloses = map[string]bool{
		"IR_SILVER_FUND_SILVER": true,
		"IR_SILVER_FUND_SIMIN":  true,
		"IR_GOLD_FUND_KAHRABA":  true,
	}
)

// DailyCloseStaleDays bounds a TGJU daily close. TGJU publishes a close for
// Saturday to Wednesday and for most Thursdays (20 to 23 of the last 26 for
// the Bahar coin, 24k and silver; silver also on 13 Fridays), and the job
// stores a day's close at 00:25 UTC the next morning (03:55 Tehran), with a
// retry at 12:25 UTC. So the newest close is normally one or two days old,
// and the most it reaches in an ordinary week is four: early on a Sunday,
// before that morning's run stores Saturday's close, when TGJU published no
// Thursday close. That is the bound — which also means a stalled job reads
// fresh for up to four days, and a holiday against the weekend can flag a
// few early-morning hours. A long closure (Nowruz runs to about thirteen
// days) reads as stale, and during it the series IS that old.
const DailyCloseStaleDays = 4

// WeeklyFetchStaleDays bounds a close the weekly off-server TSETMC fetch
// stores: the SAME boundary as bourse.StaleAfterDays and the equity bars'
// (a test pins them together). The funds' closes arrive in the same run as
// the indices and the shares, whose pages call that run stopped after ten
// days, and the Trade chart must not call a fund STALE — it did, five days
// in seven — beside an index chart of the same run reading fresh.
const WeeklyFetchStaleDays = 10

// DailyCloseOnly reports whether symbol only ever receives one settled close
// per session by default: no live source is configured for it out of the box.
func DailyCloseOnly(symbol string) bool {
	return tgjuDailyCloses[symbol] || weeklyFetchCloses[symbol]
}

// DailyClose reports whether symbol's newest row, from source, is a settled
// daily close, and how many calendar days old it may be and still be the
// newest one that can exist. The source decides (a live quote of a symbol on
// these lists keeps the session rule); an empty source — a caller that does
// not know it — falls back on the symbol's default. A symbol off these lists
// is never judged as a daily close, whatever wrote its newest row: TGJU's
// gap-fill writes the Emami coin's missing days as closes, and those must not
// relax the live collector's own freshness rule.
func DailyClose(symbol, source string) (maxAgeDays int, ok bool) {
	if !DailyCloseOnly(symbol) {
		return 0, false
	}
	switch source {
	case SourceTGJUHistory:
		return DailyCloseStaleDays, true
	case SourceTSETMCCloses:
		return WeeklyFetchStaleDays, true
	case "":
		if weeklyFetchCloses[symbol] {
			return WeeklyFetchStaleDays, true
		}
		return DailyCloseStaleDays, true
	}
	return 0, false
}

// dailyCloseAgeDays is the calendar-day age of a daily close stamped at
// observedAt, measured at `at`: the Tehran date now against the close's own
// date. A close is stamped 23:00 UTC on its trade date, so its UTC date IS the
// trade date.
func dailyCloseAgeDays(observedAt, at time.Time) int {
	o := observedAt.UTC()
	closeDay := time.Date(o.Year(), o.Month(), o.Day(), 0, 0, 0, 0, time.UTC)
	t := at.In(tehran)
	today := time.Date(t.Year(), t.Month(), t.Day(), 0, 0, 0, 0, time.UTC)
	return int(today.Sub(closeDay).Hours() / 24)
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
//
// and, for a daily close (DailyClose, by the symbol's default source), under
// the daily-close rule instead: the close is no older than its source allows.
func AcceptablyFresh(symbol string, observedAt, at time.Time, staleMinutes int, open, close string) bool {
	return AcceptablyFreshFrom(symbol, "", observedAt, at, staleMinutes, open, close)
}

// AcceptablyFreshFrom is AcceptablyFresh for an observation whose source is
// known: the source, not the symbol's default, decides whether it is a daily
// close and how old one may be.
func AcceptablyFreshFrom(symbol, source string, observedAt, at time.Time, staleMinutes int,
	open, close string) bool {
	if maxAge, ok := DailyClose(symbol, source); ok {
		return dailyCloseAgeDays(observedAt, at) <= maxAge
	}
	stale := time.Duration(staleMinutes) * time.Minute
	if IsOpen(symbol, at, open, close) {
		return at.Sub(observedAt) <= stale
	}
	return !observedAt.Before(ClosureStartedAt(symbol, at, open, close).Add(-stale))
}
