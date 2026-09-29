// Package bourse serves the Tehran market as a whole: TSETMC's own indices
// (the all-share, the equal-weighted, every board, segment and sector), the
// total market value, the market-wide money-flow table for the roster, and the
// overview as it stood when a refresh last ran.
//
// This package READS. Everything it serves was ingested by prediction-python
// (app/bourse), which owns the one correction this data needs — a per-row
// power of ten for index values TSETMC stores off by exactly a factor of ten
// (migration 0029's header has the measurements). What is computed here is
// arithmetic over the corrected closes: returns over windows, the spread
// between the equal- and cap-weighted indices, dispersion, drawdown, and a
// unit-of-account conversion through internal/relvalue's single
// implementation. Nothing here forecasts.
//
// The gate. An index is served corrected only when its newest verdict in
// market_index_checks says 'validated' — the ingest refuses one whose
// corrected history disagrees with the exchange's own live figure. A refused
// index keeps its registry row and its evidence and serves no corrected value,
// exactly as 0028's refused equity serves no adjusted one.
//
// A closed market is a repeated value. TEDPIX stood at 3,713,955.9 for 50
// sessions from 2026-02-25; those rows are what TSETMC published and are drawn
// as they are, but an EXACT repeat of the previous close is read as "no
// session" by every dispersion figure here, the index-level counterpart of
// excluding a halted bar — fifty 0% returns would otherwise halve a volatility.
package bourse

import (
	"context"
	"fmt"
	"log/slog"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// Handler serves /api/v1/bourse/*.
//
// It holds every validated index series in memory, because the page that
// reads them needs all 71 at once and they change only when an operator runs
// the off-server fetch. The cache is keyed on the ingest's own footprint (see
// storeKeySelect), so the first request after an ingest reloads and every
// other request reads memory: ~256,000 corrected closes, ~10 MB.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger

	mu    sync.Mutex
	store *indexStore
}

// NewHandler is the constructor main.go wires.
func NewHandler(pool *pgxpool.Pool, log *slog.Logger) *Handler {
	return &Handler{Pool: pool, Log: log}
}

const dateLayout = "2006-01-02"

// The indices this package names in code. Everything else is data: the
// registry (migration 0029) is where an index's kind, sector and weighting
// live, and a page that lists indices reads them from there.
const (
	// TEDPIX, شاخص کل: cap-weighted, price and dividends.
	TEDPIX = "32097828799138957"
	// شاخص کل (هم وزن): the equal-weighted all-share, from 2014-03-19.
	EqualWeighted = "67130298613737946"
	// TEPIX, شاخص قیمت (وزنی-ارزشی): cap-weighted, price only.
	TEPIX = "5798407779416661"
	// شاخص قیمت (هم وزن): equal-weighted, price only.
	EqualWeightedPrice = "8384385859414435"
	// IFX, شاخص کل فرابورس.
	IFX = "43685683301327984"
)

const statusValidated = "validated"

// --- how old the data is -----------------------------------------------------

// StaleAfterDays and RefreshCommand are the SAME boundary and the same fix
// internal/equities publishes for the bars (equityStaleAfterDays and
// equityRefreshCommand; a test there pins them together). The indices arrive
// in the same off-server run as the bars, so a page reading one and a page
// reading the other must not disagree about when that run has stopped.
const (
	StaleAfterDays = 10
	RefreshCommand = "make refresh-equities"
)

// DataAge states how old the stored market data is, on every response.
type DataAge struct {
	NewestTradeDate *string `json:"newest_trade_date"`
	AgeDays         *int    `json:"age_days"`
	AsOf            string  `json:"as_of"`
	Stale           bool    `json:"stale"`
	StaleAfterDays  int     `json:"stale_after_days"`
	RefreshCommand  string  `json:"refresh_command"`
	Warning         string  `json:"warning,omitempty"`
	Note            string  `json:"note"`
}

const dataAgeNote = "Tehran market data does not refresh itself: TSETMC is unreachable from " +
	"the production host, so indices, market value and money flow arrive only when an " +
	"operator runs the fetch from a network that can reach it. This is the age of the " +
	"newest stored session, measured against today."

// buildDataAge is the age arithmetic. Pure function (unit tested).
func buildDataAge(newest *time.Time, now time.Time) DataAge {
	today := dayFloor(now)
	out := DataAge{
		AsOf:           today.Format(dateLayout),
		StaleAfterDays: StaleAfterDays,
		RefreshCommand: RefreshCommand,
		Note:           dataAgeNote,
	}
	if newest == nil {
		out.Warning = "No stored Tehran market session, so every figure here is empty " +
			"rather than old. Run `" + RefreshCommand + "` from a host that can reach TSETMC."
		return out
	}
	d := dayFloor(*newest)
	days := int(today.Sub(d).Hours() / 24)
	label := d.Format(dateLayout)
	out.NewestTradeDate = &label
	out.AgeDays = &days
	switch {
	case days < 0:
		out.Stale = true
		out.Warning = fmt.Sprintf(
			"The newest stored session is %s, %d day(s) AFTER today (%s). A session "+
				"cannot be in the future, so this is a stored date fault, not fresh data.",
			label, -days, out.AsOf)
	case days > StaleAfterDays:
		out.Stale = true
		out.Warning = fmt.Sprintf(
			"This market data is %d days old: the newest stored session is %s and today "+
				"is %s. Beyond %d days it is a refresh that has stopped, not the market "+
				"calendar — run `%s` from a network that can reach TSETMC.",
			days, label, out.AsOf, StaleAfterDays, RefreshCommand)
	}
	return out
}

// --- small shared helpers ----------------------------------------------------

func dayFloor(t time.Time) time.Time {
	u := t.UTC()
	return time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC)
}

func dayString(t time.Time) string { return t.UTC().Format(dateLayout) }

func dayPtr(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := dayString(*t)
	return &s
}

type paramError struct {
	Message string
	Details map[string]any
}

func (e *paramError) Error() string { return e.Message }

func badParam(message string, details map[string]any) *paramError {
	return &paramError{Message: message, Details: details}
}

func parseDate(name, raw string) (time.Time, *paramError) {
	if d, err := time.Parse(dateLayout, raw); err == nil {
		return d.UTC(), nil
	}
	if t, err := time.Parse(time.RFC3339, raw); err == nil {
		return dayFloor(t), nil
	}
	return time.Time{}, badParam(
		fmt.Sprintf("%s must be a date (YYYY-MM-DD) or RFC3339, got %q", name, raw),
		map[string]any{name: raw})
}

// --- windows -----------------------------------------------------------------

// periodVocabulary matches internal/relvalue's, so the two pages offer the
// same choices and a period means the same span on both.
var periodVocabulary = []string{"1m", "3m", "6m", "1y", "3y", "5y", "10y", "max"}

const defaultPeriod = "1y"

// periodStart is `end` minus the period, by calendar arithmetic; nil for max.
func periodStart(period string, end time.Time) (*time.Time, bool) {
	var t time.Time
	switch period {
	case "1m":
		t = end.AddDate(0, -1, 0)
	case "3m":
		t = end.AddDate(0, -3, 0)
	case "6m":
		t = end.AddDate(0, -6, 0)
	case "1y":
		t = end.AddDate(-1, 0, 0)
	case "3y":
		t = end.AddDate(-3, 0, 0)
	case "5y":
		t = end.AddDate(-5, 0, 0)
	case "10y":
		t = end.AddDate(-10, 0, 0)
	case "max":
		return nil, true
	default:
		return nil, false
	}
	return &t, true
}

// window is a resolved [From, To] over one or more stored series.
//
// Windows END AT THE NEWEST STORED SESSION, not at today, unless the caller
// says otherwise with ?to=. This data is refreshed by hand; a "1y" window that
// ended today on data a week old would silently be a 51-week window, and the
// data_age block already says how old the newest session is.
type window struct {
	Period string
	From   *time.Time
	To     time.Time
}

// parseWindow reads ?period=&from=&to=. `newest` is the newest stored session
// the window will be measured over. Explicit dates win over a period.
func parseWindow(q url.Values, newest time.Time) (window, *paramError) {
	period := strings.ToLower(strings.TrimSpace(q.Get("period")))
	if period == "" {
		period = defaultPeriod
	}
	w := window{Period: period, To: dayFloor(newest)}
	if raw := q.Get("to"); raw != "" {
		to, perr := parseDate("to", raw)
		if perr != nil {
			return window{}, perr
		}
		w.To = to
	}
	if raw := q.Get("from"); raw != "" {
		from, perr := parseDate("from", raw)
		if perr != nil {
			return window{}, perr
		}
		if from.After(w.To) {
			return window{}, badParam("from must not be after to",
				map[string]any{"from": raw, "to": dayString(w.To)})
		}
		w.From = &from
		w.Period = "custom"
		return w, nil
	}
	start, ok := periodStart(period, w.To)
	if !ok {
		return window{}, badParam(
			fmt.Sprintf("period must be one of %v, got %q", periodVocabulary, period),
			map[string]any{"period": period, "supported": periodVocabulary})
	}
	w.From = start
	return w, nil
}

type windowItem struct {
	Period string  `json:"period"`
	From   *string `json:"from"`
	To     string  `json:"to"`
}

func (w window) item() windowItem {
	return windowItem{Period: w.Period, From: dayPtr(w.From), To: dayString(w.To)}
}

// storeFor is a context-bound accessor used by every handler.
func (h *Handler) storeFor(ctx context.Context) (*indexStore, error) {
	return h.loadStore(ctx)
}
