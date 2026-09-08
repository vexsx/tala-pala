// Package economic serves the instrument vocabulary and the point-in-time
// economic-series store that migration 0024 created.
//
// This package READS. It computes nothing: no splicing, no rebasing, no
// year-on-year arithmetic, no interpolation, no filling of a missing period.
// Those belong to prediction-python, which owns the ingest and the definitions;
// re-deriving any of them here would create a second, silently diverging answer
// to the same question. What follows is a projection of four stored tables --
// instruments, economic_series, economic_observations and (by reference)
// source_documents -- onto the wire contract.
//
// The one thing this package is strict about is TIME. A macro observation has
// three clocks that all differ: the reference period it describes, the moment
// it was published, and the revision it belongs to. Every read here filters on
// available_at -- when THIS system could first have known the value -- and
// never on published_at, which is the rule migration 0017 states for
// news_articles and the entire reason economic_observations stores vintages
// instead of updating rows in place. A backtest that reads today's revised
// number at a 2024 cutoff is reading the future, and the point-in-time read
// below is what makes that impossible.
//
// A projection (IMF WEO carries them to 2031 in the same series as history) is
// not an observation. It is excluded by default and, when explicitly asked
// for, flagged on every row it appears on.
package economic

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Handler serves /api/v1/instruments and /api/v1/series/*.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger
}

const (
	// defaultObservationLimit is a whole modern history for a monthly series
	// (500 months is ~41 years) without paging.
	defaultObservationLimit = 500
	// maxObservationLimit caps a single page. A daily series over a century is
	// still under this, so the cap is a guard against accidents rather than a
	// real ceiling on any series the registry holds.
	maxObservationLimit = 5000
)

// dateLayout is how a reference period is rendered. ref_period_start/end are
// DATE columns: serializing them as timestamps would invent a time of day and
// a timezone that the source never stated.
const dateLayout = "2006-01-02"

// The window of integers this package will read as unix seconds. Outside it,
// an integer is refused rather than converted -- see parseInstant.
const (
	// minEpochSeconds is 2001-09-09T01:46:40Z. Every integer a caller actually
	// mistypes into a cutoff is smaller than this: a Gregorian year (2026), a
	// Jalali year (1405), a compact date (20260908). Read as seconds they all
	// land in 1970, which this endpoint would answer with a perfectly
	// well-formed empty history -- indistinguishable from "this series has
	// none".
	minEpochSeconds = 1_000_000_000
	// maxEpochSeconds is 2100-01-01T00:00:00Z. Above it the overwhelmingly
	// likely input is milliseconds: a JavaScript Date.now() read as seconds
	// lands in the year 58000, which passes every availability filter and
	// quietly serves today's fully revised numbers to a backtest that asked
	// for a historical cutoff.
	maxEpochSeconds = 4_102_444_800
)

// instrumentKinds is the CHECK set on instruments.kind (migration 0024). It is
// a CLOSED vocabulary, so an unrecognised ?kind= is a client bug -- it can
// never match a row -- and is refused rather than served as an empty list.
//
// domain is deliberately NOT validated the same way: it carries no CHECK
// constraint, so "no instrument is in that domain" is a true answer to
// ?domain=housing today and must keep being one after a migration adds the
// first housing instrument.
var instrumentKinds = []string{
	"market_price", "economic_series", "equity", "index", "basket", "fx",
}

// --- refusals ----------------------------------------------------------------

// paramError carries the message and details of a rejected parameter, so each
// handler writes exactly one kind of 400. Same shape as candles.go: invalid
// caller input is refused with a specific message, never clamped, defaulted or
// silently substituted.
type paramError struct {
	Message string
	Details map[string]any
}

func (e *paramError) Error() string { return e.Message }

func badParam(message string, details map[string]any) *paramError {
	return &paramError{Message: message, Details: details}
}

// parseBoolParam reads a switch parameter. Unknown values are refused rather
// than read as false: an ?include_projections=treu that quietly meant "false"
// would look like a series that simply has no projections.
func parseBoolParam(name, raw string) (bool, *paramError) {
	switch strings.ToLower(raw) {
	case "1", "true", "yes":
		return true, nil
	case "0", "false", "no":
		return false, nil
	}
	return false, badParam(
		fmt.Sprintf("%s must be 0 or 1, got %q", name, raw),
		map[string]any{name: raw})
}

// parseInstant accepts RFC3339 or bare unix seconds, exactly as candles.go
// does for ?from/?to/?before. Both forms appear in the wild: a UI round-trips
// the ISO string this API returns, and a backtest harness hands back the unix
// cutoff it is iterating over.
//
// An integer outside [minEpochSeconds, maxEpochSeconds] is REFUSED rather than
// converted. parsePeriodDate below states the reasoning for ?from/?to -- a bare
// integer is far more likely a typo'd year than an epoch -- and a cutoff
// deserves the same standard, only more so: ?as_of=2026 became
// 1970-01-01T00:33:46Z, before which nothing was ever knowable, so the endpoint
// answered 200 with an empty observation list. A harness iterating years reads
// that as "this series has no history" and silently drops the series from a
// backtest. Refusing costs a caller one corrected request; guessing costs a
// study its data.
func parseInstant(name, raw string) (time.Time, *paramError) {
	if n, err := strconv.ParseInt(raw, 10, 64); err == nil {
		if n < minEpochSeconds || n > maxEpochSeconds {
			return time.Time{}, badParam(
				fmt.Sprintf("%s must be RFC3339 (2026-03-01T09:30:00Z) or unix seconds "+
					"between %d and %d, got %q: a year, a YYYYMMDD date or milliseconds "+
					"is not an epoch cutoff",
					name, minEpochSeconds, maxEpochSeconds, raw),
				map[string]any{
					name:               raw,
					"accepted_formats": []string{"RFC3339", "unix seconds"},
					"epoch_seconds_range": []int64{
						minEpochSeconds, maxEpochSeconds,
					},
				})
		}
		return time.Unix(n, 0).UTC(), nil
	}
	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return time.Time{}, badParam(
			fmt.Sprintf("%s must be RFC3339 (2026-03-01T09:30:00Z) or unix seconds, got %q",
				name, raw),
			map[string]any{
				name:               raw,
				"accepted_formats": []string{"RFC3339", "unix seconds"},
			})
	}
	return t.UTC(), nil
}

// parsePeriodDate reads a ?from/?to/?before bound. These select REFERENCE
// PERIODS, not instants, so a calendar date is the primary form; a full RFC3339
// timestamp is accepted (its UTC date is used) so a caller holding a timestamp
// is not stuck, and everything else is refused. Unix seconds are deliberately
// NOT accepted here: a bare integer in a period field is far more likely to be
// a typo'd year than an epoch, and guessing which would be exactly the silent
// substitution this codebase forbids.
//
// A timestamp carrying a non-UTC offset is converted first, so a Tehran client
// sending 2025-01-01T00:00:00+03:30 (local midnight) selects the period bound
// 2024-12-31. That is the only defensible reading -- every stored timestamp on
// this contract is UTC and this function cannot know whether the caller meant
// the Tehran day or the instant -- but it is a full period's difference on an
// inclusive bound, so the interpreted bounds are echoed back as
// `effective_window` rather than left for the caller to infer. Guessing at
// intent is what is refused; stating the interpretation is what replaces it.
func parsePeriodDate(name, raw string) (time.Time, *paramError) {
	if d, err := time.Parse(dateLayout, raw); err == nil {
		return d.UTC(), nil
	}
	if t, err := time.Parse(time.RFC3339, raw); err == nil {
		u := t.UTC()
		return time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC), nil
	}
	return time.Time{}, badParam(
		fmt.Sprintf("%s must be a date (YYYY-MM-DD) or RFC3339, got %q", name, raw),
		map[string]any{name: raw})
}

// optional turns an absent filter into a nil SQL parameter, so one prepared
// statement serves both the filtered and unfiltered read.
func optional(raw string) *string {
	if raw == "" {
		return nil
	}
	v := raw
	return &v
}

// --- instruments -------------------------------------------------------------

// instrumentQuery is a validated /instruments request.
type instrumentQuery struct {
	Kind    *string
	Domain  *string
	Enabled *bool
}

// parseInstrumentQuery validates the query string. Pure function (unit
// tested): no clock, no database.
//
// An absent ?enabled= applies NO filter. The registry exists so clients stop
// hard-coding symbol lists, and every row carries its own `enabled` flag, so
// returning the whole vocabulary is lossless while defaulting to enabled-only
// would hide rows without saying so -- a disabled instrument would look like an
// instrument that does not exist.
func parseInstrumentQuery(q url.Values) (instrumentQuery, *paramError) {
	out := instrumentQuery{}

	if raw := q.Get("kind"); raw != "" {
		if !knownInstrumentKind(raw) {
			return out, badParam(
				fmt.Sprintf("kind must be one of %s, got %q",
					strings.Join(instrumentKinds, ", "), raw),
				map[string]any{"kind": raw, "supported": instrumentKinds})
		}
		out.Kind = optional(raw)
	}
	out.Domain = optional(q.Get("domain"))
	if raw := q.Get("enabled"); raw != "" {
		on, perr := parseBoolParam("enabled", raw)
		if perr != nil {
			return out, perr
		}
		out.Enabled = &on
	}
	return out, nil
}

func knownInstrumentKind(kind string) bool {
	for _, k := range instrumentKinds {
		if k == kind {
			return true
		}
	}
	return false
}

// instrumentRow is `instruments` as scanned.
type instrumentRow struct {
	Code          string
	Kind          string
	NameEN        string
	NameFA        string
	Domain        string
	QuoteCurrency string
	Unit          string
	Decimals      int
	CalendarClass string
	QualityTier   string
	IsProxy       bool
	IsDerived     bool
	Enabled       bool
	Notes         string
}

// instrumentItem is one instrument on the wire. quality_tier, is_proxy and
// is_derived travel with every row on purpose: USD_IRT is collected from the
// 24/7 USDT/toman market as a documented free-market proxy, and a client that
// renders it identically to an official mirror is misrepresenting it.
type instrumentItem struct {
	Code          string `json:"code"`
	Kind          string `json:"kind"`
	NameEN        string `json:"name_en"`
	NameFA        string `json:"name_fa"`
	Domain        string `json:"domain"`
	QuoteCurrency string `json:"quote_currency"`
	Unit          string `json:"unit"`
	Decimals      int    `json:"decimals"`
	CalendarClass string `json:"calendar_class"`
	QualityTier   string `json:"quality_tier"`
	IsProxy       bool   `json:"is_proxy"`
	IsDerived     bool   `json:"is_derived"`
	Enabled       bool   `json:"enabled"`
	Notes         string `json:"notes"`
}

type instrumentsResponse struct {
	Items []instrumentItem `json:"items"`
	Count int              `json:"count"`
}

const instrumentsSelect = `
	SELECT code, kind, name_en, name_fa, domain, quote_currency, unit, decimals,
	       calendar_class, quality_tier, is_proxy, is_derived, enabled, notes
	FROM instruments
	WHERE ($1::text IS NULL OR kind = $1)
	  AND ($2::text IS NULL OR domain = $2)
	  AND ($3::bool IS NULL OR enabled = $3)
	ORDER BY kind, domain, code`

// sortInstrumentRows applies the documented order: kind, then domain, then
// code. instrumentsSelect already returns rows this way; the comparator exists
// so the ordering is expressible (and testable) without a database, and
// re-applying it costs nothing at registry size.
func sortInstrumentRows(rows []instrumentRow) {
	sort.SliceStable(rows, func(i, j int) bool {
		a, b := rows[i], rows[j]
		if a.Kind != b.Kind {
			return a.Kind < b.Kind
		}
		if a.Domain != b.Domain {
			return a.Domain < b.Domain
		}
		return a.Code < b.Code
	})
}

// buildInstrumentsResponse projects the stored rows onto the contract. Pure
// function (unit tested): it orders and copies, and computes nothing.
func buildInstrumentsResponse(rows []instrumentRow) instrumentsResponse {
	sortInstrumentRows(rows)
	// Never nil: an empty registry read is a 200 with an empty list, not a
	// missing key a client has to special-case.
	out := instrumentsResponse{Items: make([]instrumentItem, 0, len(rows))}
	for _, r := range rows {
		out.Items = append(out.Items, instrumentItem{
			Code:          r.Code,
			Kind:          r.Kind,
			NameEN:        r.NameEN,
			NameFA:        r.NameFA,
			Domain:        r.Domain,
			QuoteCurrency: r.QuoteCurrency,
			Unit:          r.Unit,
			Decimals:      r.Decimals,
			CalendarClass: r.CalendarClass,
			QualityTier:   r.QualityTier,
			IsProxy:       r.IsProxy,
			IsDerived:     r.IsDerived,
			Enabled:       r.Enabled,
			Notes:         r.Notes,
		})
	}
	out.Count = len(out.Items)
	return out
}

// Instruments implements GET /api/v1/instruments?kind=&domain=&enabled=.
func (h *Handler) Instruments(w http.ResponseWriter, r *http.Request) {
	q, perr := parseInstrumentQuery(r.URL.Query())
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}

	rows, err := h.Pool.Query(r.Context(), instrumentsSelect, q.Kind, q.Domain, q.Enabled)
	if err != nil {
		h.Log.Error("instruments_query", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	scanned := []instrumentRow{}
	for rows.Next() {
		var it instrumentRow
		if err := rows.Scan(&it.Code, &it.Kind, &it.NameEN, &it.NameFA, &it.Domain,
			&it.QuoteCurrency, &it.Unit, &it.Decimals, &it.CalendarClass,
			&it.QualityTier, &it.IsProxy, &it.IsDerived, &it.Enabled, &it.Notes); err != nil {
			h.Log.Error("instruments_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		scanned = append(scanned, it)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("instruments_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	httpserver.JSON(w, http.StatusOK, buildInstrumentsResponse(scanned))
}

// --- series registry ---------------------------------------------------------

// seriesRow is economic_series joined to instruments, with the coverage
// aggregate. ID is scanned for the observations read and never serialized.
type seriesRow struct {
	ID                 int64
	Code               string
	NameEN             string
	NameFA             string
	Domain             string
	Frequency          string
	Calendar           string
	Measure            string
	SeasonalAdjustment string
	BasePeriod         string
	ProviderCode       string
	ProviderSeriesID   string
	PublicationLagDays *int
	Revisable          bool
	SplicePolicy       string
	Enabled            bool
	QualityTier        string
	Unit               string
	Decimals           int
	// Notes is the catalog's free-text warning about this instrument. It is
	// not decoration: it is where "this is NOT the Statistical Centre of
	// Iran's own 1400=100 index" lives, and it travels onto every payload that
	// carries a value from the series.
	Notes string

	FirstPeriod       *time.Time
	LastPeriod        *time.Time
	ObservationCount  int64
	LatestAvailableAt *time.Time
	MaxVintage        *int
	ProjectionCount   int64
}

// coverage describes what is actually stored for a series.
//
// Every field except projection_count describes OBSERVATIONS only. IMF WEO
// carries projections to 2031 inside the same series as history, so a
// last_period that silently included them would report 2031 as the end of
// measured history -- a number nobody measured. projection_count exists so the
// exclusion is visible rather than an omission the client cannot detect.
//
// Both counts count REFERENCE PERIODS, not stored rows. economic_observations
// is bitemporal: one period revised six times is six rows and one period. A
// count(*) here reported 72 for a 66-year annual series and sat directly beside
// first_period 1960 and last_period 2025, where it could only be read as the
// number of periods between them -- and it got wronger the more faithfully the
// store recorded revisions. The row count is not lost: max_vintage still says
// how deeply revised the series is.
type coverage struct {
	FirstPeriod *string `json:"first_period"`
	LastPeriod  *string `json:"last_period"`
	// ObservationCount is distinct non-projection reference periods.
	ObservationCount  int64      `json:"observation_count"`
	LatestAvailableAt *time.Time `json:"latest_available_at"`
	MaxVintage        *int       `json:"max_vintage"`
	// ProjectionCount is distinct projected reference periods.
	ProjectionCount int64 `json:"projection_count"`
}

// seriesItem is one registered series on the wire. It is deliberately
// self-describing: measure, base_period, seasonal_adjustment and splice_policy
// are part of series IDENTITY, not display options. Iran publishes both
// point-to-point and twelve-month-average inflation and they diverged by 20+
// points in Tir 1405, so a client that drops `measure` cannot tell which number
// it is holding.
type seriesItem struct {
	Code               string   `json:"code"`
	NameEN             string   `json:"name_en"`
	NameFA             string   `json:"name_fa"`
	Domain             string   `json:"domain"`
	Frequency          string   `json:"frequency"`
	Calendar           string   `json:"calendar"`
	Measure            string   `json:"measure"`
	SeasonalAdjustment string   `json:"seasonal_adjustment"`
	BasePeriod         string   `json:"base_period"`
	Unit               string   `json:"unit"`
	Decimals           int      `json:"decimals"`
	ProviderCode       string   `json:"provider_code"`
	ProviderSeriesID   string   `json:"provider_series_id"`
	PublicationLagDays *int     `json:"publication_lag_days"`
	Revisable          bool     `json:"revisable"`
	SplicePolicy       string   `json:"splice_policy"`
	QualityTier        string   `json:"quality_tier"`
	Notes              string   `json:"notes"`
	Enabled            bool     `json:"enabled"`
	Coverage           coverage `json:"coverage"`
}

type seriesListResponse struct {
	Items []seriesItem `json:"items"`
	Count int          `json:"count"`
}

// seriesSelectColumns and seriesSelectFrom are shared by the list and the
// single-code read so the two can never describe the same series differently.
//
// The coverage aggregate is a LEFT JOIN LATERAL: one query for the whole page,
// never a per-series follow-up. FILTER (WHERE ...) splits observations from
// projections in the same pass.
//
// The counts are count(DISTINCT o.ref_period_start), never count(*). This table
// stores VINTAGES: a revised period is several rows and one period. count(*)
// sitting beside first_period/last_period announced 72 observations for the
// 1960..2025 annual series that has 66 of them, and the discrepancy grew with
// every honestly recorded revision.
const seriesSelectColumns = `
	       s.id, s.code, i.name_en, i.name_fa, i.domain,
	       s.frequency, s.calendar, s.measure, s.seasonal_adjustment, s.base_period,
	       s.provider_code, s.provider_series_id, s.publication_lag_days,
	       s.revisable, s.splice_policy, s.enabled,
	       i.quality_tier, i.unit, i.decimals, i.notes,
	       cov.first_period, cov.last_period, cov.observation_count,
	       cov.latest_available_at, cov.max_vintage, cov.projection_count`

const seriesSelectFrom = `
	FROM economic_series s
	JOIN instruments i ON i.code = s.code
	LEFT JOIN LATERAL (
	    SELECT min(o.ref_period_start) FILTER (WHERE NOT o.is_projection) AS first_period,
	           max(o.ref_period_start) FILTER (WHERE NOT o.is_projection) AS last_period,
	           count(DISTINCT o.ref_period_start)
	               FILTER (WHERE NOT o.is_projection) AS observation_count,
	           max(o.available_at)     FILTER (WHERE NOT o.is_projection) AS latest_available_at,
	           max(o.vintage)          FILTER (WHERE NOT o.is_projection) AS max_vintage,
	           count(DISTINCT o.ref_period_start)
	               FILTER (WHERE o.is_projection)     AS projection_count
	    FROM economic_observations o
	    WHERE o.series_id = s.id
	) cov ON TRUE`

const seriesListSelect = `SELECT` + seriesSelectColumns + seriesSelectFrom + `
	WHERE ($1::text IS NULL OR i.domain = $1)
	  AND ($2::text IS NULL OR s.provider_code = $2)
	ORDER BY i.domain, s.code`

const seriesByCodeSelect = `SELECT` + seriesSelectColumns + seriesSelectFrom + `
	WHERE s.code = $1`

func scanSeriesRow(s pgx.Row) (seriesRow, error) {
	var r seriesRow
	err := s.Scan(&r.ID, &r.Code, &r.NameEN, &r.NameFA, &r.Domain,
		&r.Frequency, &r.Calendar, &r.Measure, &r.SeasonalAdjustment, &r.BasePeriod,
		&r.ProviderCode, &r.ProviderSeriesID, &r.PublicationLagDays,
		&r.Revisable, &r.SplicePolicy, &r.Enabled,
		&r.QualityTier, &r.Unit, &r.Decimals, &r.Notes,
		&r.FirstPeriod, &r.LastPeriod, &r.ObservationCount,
		&r.LatestAvailableAt, &r.MaxVintage, &r.ProjectionCount)
	return r, err
}

// sortSeriesRows applies the documented order: domain, then code.
// seriesListSelect already returns rows this way; the comparator exists so the
// ordering is expressible and testable without a database.
func sortSeriesRows(rows []seriesRow) {
	sort.SliceStable(rows, func(i, j int) bool {
		a, b := rows[i], rows[j]
		if a.Domain != b.Domain {
			return a.Domain < b.Domain
		}
		return a.Code < b.Code
	})
}

// dateString renders a nullable DATE as YYYY-MM-DD. A period that was never
// observed stays null; it is never filled in with a plausible date.
func dateString(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := t.UTC().Format(dateLayout)
	return &s
}

// utcPtr normalizes a nullable timestamp to UTC without aliasing the caller's
// value. Every timestamp on this contract is serialized UTC; Tehran and Jalali
// are display concerns and never touch stored time.
func utcPtr(t *time.Time) *time.Time {
	if t == nil {
		return nil
	}
	u := t.UTC()
	return &u
}

// buildSeriesItem projects one stored row onto the contract. Pure function
// (unit tested): it copies, and computes nothing.
func buildSeriesItem(r seriesRow) seriesItem {
	return seriesItem{
		Code:               r.Code,
		NameEN:             r.NameEN,
		NameFA:             r.NameFA,
		Domain:             r.Domain,
		Frequency:          r.Frequency,
		Calendar:           r.Calendar,
		Measure:            r.Measure,
		SeasonalAdjustment: r.SeasonalAdjustment,
		BasePeriod:         r.BasePeriod,
		Unit:               r.Unit,
		Decimals:           r.Decimals,
		ProviderCode:       r.ProviderCode,
		ProviderSeriesID:   r.ProviderSeriesID,
		PublicationLagDays: r.PublicationLagDays,
		Revisable:          r.Revisable,
		SplicePolicy:       r.SplicePolicy,
		QualityTier:        r.QualityTier,
		Notes:              r.Notes,
		Enabled:            r.Enabled,
		Coverage: coverage{
			FirstPeriod:       dateString(r.FirstPeriod),
			LastPeriod:        dateString(r.LastPeriod),
			ObservationCount:  r.ObservationCount,
			LatestAvailableAt: utcPtr(r.LatestAvailableAt),
			MaxVintage:        r.MaxVintage,
			ProjectionCount:   r.ProjectionCount,
		},
	}
}

// buildSeriesListResponse orders and projects the registry page. Pure function
// (unit tested).
func buildSeriesListResponse(rows []seriesRow) seriesListResponse {
	sortSeriesRows(rows)
	out := seriesListResponse{Items: make([]seriesItem, 0, len(rows))}
	for _, r := range rows {
		out.Items = append(out.Items, buildSeriesItem(r))
	}
	out.Count = len(out.Items)
	return out
}

// SeriesList implements GET /api/v1/series?domain=&provider=.
func (h *Handler) SeriesList(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	// domain and provider_code carry no CHECK constraint, so an unmatched value
	// is a true empty result rather than a client error; see the note on
	// instrumentKinds.
	domain := optional(q.Get("domain"))
	provider := optional(q.Get("provider"))

	rows, err := h.Pool.Query(r.Context(), seriesListSelect, domain, provider)
	if err != nil {
		h.Log.Error("series_list_query", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	scanned := []seriesRow{}
	for rows.Next() {
		it, err := scanSeriesRow(rows)
		if err != nil {
			h.Log.Error("series_list_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		scanned = append(scanned, it)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("series_list_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	httpserver.JSON(w, http.StatusOK, buildSeriesListResponse(scanned))
}

// unknownSeries writes the 404 for a code that is not a registered economic
// series.
//
// There is deliberately NO fallback to a market symbol. IR_GOLD_18K is a real
// instrument with real prices, but it is not an economic series, and answering
// /series/IR_GOLD_18K with anything other than "no such series" would invite a
// client to read tick prices through a contract that promises reference periods
// and vintages.
func unknownSeries(w http.ResponseWriter, code string) {
	httpserver.Error(w, http.StatusNotFound, "not_found",
		fmt.Sprintf("%q is not a known economic series", code),
		map[string]any{"code": code})
}

// Series implements GET /api/v1/series/{code}.
func (h *Handler) Series(w http.ResponseWriter, r *http.Request) {
	code := chi.URLParam(r, "code")
	if code == "" {
		httpserver.BadRequest(w, "series code is required", map[string]any{"code": code})
		return
	}

	row, err := scanSeriesRow(h.Pool.QueryRow(r.Context(), seriesByCodeSelect, code))
	if errors.Is(err, pgx.ErrNoRows) {
		unknownSeries(w, code)
		return
	}
	if err != nil {
		h.Log.Error("series_by_code", "error", err, "code", code)
		httpserver.Internal(w, "database error")
		return
	}

	httpserver.JSON(w, http.StatusOK, buildSeriesItem(row))
}

// --- point-in-time observations ----------------------------------------------

// observationQuery is a validated /series/{code}/observations request.
type observationQuery struct {
	Code string
	// AsOf is the vintage cutoff: only rows whose available_at is at or before
	// it may be seen. Never nil after parsing -- an absent parameter is filled
	// with the request time and echoed, so the response always states the
	// cutoff it was actually served at.
	AsOf time.Time
	// AsOfProvided distinguishes an echoed caller cutoff from the substituted
	// now, so a client can tell "you asked for this" from "we used the clock".
	AsOfProvided bool
	// From and To bound ref_period_start (the reference period), not
	// availability. They are separate axes and conflating them is the bug this
	// whole table exists to prevent.
	From *time.Time
	To   *time.Time
	// Before is the pagination cursor, EXCLUSIVE on ref_period_start, and it
	// is a period bound like From/To rather than an instant. Same role as
	// ?before= on /market/candles: the client hands back the `next_before` it
	// was given -- a period it already holds -- and receives the page of older
	// periods.
	Before             *time.Time
	Limit              int
	IncludeProjections bool
}

// fetchLimit is how many periods the statement is asked for: one more than the
// page will carry. That extra row is the has_more proof and is never
// serialized, the same trick paginateCandles uses to distinguish "that is all
// there is" from "you were cut off" without a second COUNT query.
func (q observationQuery) fetchLimit() int {
	if q.Limit < 1 {
		return 1
	}
	return q.Limit + 1
}

// parseObservationQuery validates the query string. Pure function (unit
// tested): the caller supplies `now`, so there is no hidden clock.
func parseObservationQuery(code string, q url.Values, now time.Time) (observationQuery, *paramError) {
	out := observationQuery{
		Code:  code,
		AsOf:  now.UTC(),
		Limit: defaultObservationLimit,
	}
	if code == "" {
		return out, badParam("series code is required", map[string]any{"code": code})
	}

	if raw := q.Get("as_of"); raw != "" {
		t, perr := parseInstant("as_of", raw)
		if perr != nil {
			return out, perr
		}
		out.AsOf = t
		out.AsOfProvided = true
	}

	for _, p := range []struct {
		name string
		dst  **time.Time
	}{{"from", &out.From}, {"to", &out.To}, {"before", &out.Before}} {
		raw := q.Get(p.name)
		if raw == "" {
			continue
		}
		d, perr := parsePeriodDate(p.name, raw)
		if perr != nil {
			return out, perr
		}
		*p.dst = &d
	}
	// Equal bounds are legitimate: "the single period starting on this date".
	if out.From != nil && out.To != nil && out.From.After(*out.To) {
		return out, badParam("from must not be later than to", map[string]any{
			"from": out.From.Format(dateLayout), "to": out.To.Format(dateLayout),
		})
	}

	if raw := q.Get("limit"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil {
			return out, badParam(
				fmt.Sprintf("limit must be an integer, got %q", raw),
				map[string]any{"limit": raw})
		}
		if n < 1 || n > maxObservationLimit {
			return out, badParam(
				fmt.Sprintf("limit must be between 1 and %d, got %d", maxObservationLimit, n),
				map[string]any{"limit": raw})
		}
		out.Limit = n
	}

	if raw := q.Get("include_projections"); raw != "" {
		on, perr := parseBoolParam("include_projections", raw)
		if perr != nil {
			return out, perr
		}
		out.IncludeProjections = on
	}
	return out, nil
}

// observationRow is one economic_observations row as scanned.
type observationRow struct {
	RefPeriodStart time.Time
	RefPeriodEnd   time.Time
	RefPeriodLabel string
	Value          float64
	Vintage        int
	AvailableAt    time.Time
	PublishedAt    *time.Time
	IsProjection   bool
	IsNowcast      bool
}

// observationItem is one period on the wire.
//
// available_at and published_at both travel, and they are not interchangeable:
// available_at is when this system could first have known the value and is the
// only column any point-in-time read filters on; published_at is what the
// source claimed, and is null whenever the source claimed nothing. Reporting
// available_at as a publication time would be a fabrication.
type observationItem struct {
	RefPeriodStart string     `json:"ref_period_start"`
	RefPeriodEnd   string     `json:"ref_period_end"`
	RefPeriodLabel string     `json:"ref_period_label"`
	Value          float64    `json:"value"`
	Vintage        int        `json:"vintage"`
	AvailableAt    time.Time  `json:"available_at"`
	PublishedAt    *time.Time `json:"published_at"`
	IsProjection   bool       `json:"is_projection"`
	IsNowcast      bool       `json:"is_nowcast"`
}

// effectiveWindow is the reference-period window the request was actually
// served over, echoed the way /market/candles echoes its own.
//
// It exists because the bounds a caller sends are not always the bounds the
// server reads. A ?from carrying a non-UTC offset is reduced to its UTC date
// (Tehran local midnight is the previous day), and ?before is exclusive. Null
// on a side means unbounded there. Without this block a client cannot tell
// which period its window actually started at, and an inclusive bound off by
// one period is invisible in the values themselves.
type effectiveWindow struct {
	From   *string `json:"from"`
	To     *string `json:"to"`
	Before *string `json:"before"`
}

// observationsResponse carries the series' provenance at the top level. A
// number that cannot state its provenance does not ship: measure, unit,
// base_period, splice_policy, provider_code, quality_tier and notes are what
// make the values below interpretable, and as_of plus vintages_used are what
// make them reproducible.
type observationsResponse struct {
	Code         string `json:"code"`
	Measure      string `json:"measure"`
	Unit         string `json:"unit"`
	Frequency    string `json:"frequency"`
	Calendar     string `json:"calendar"`
	ProviderCode string `json:"provider_code"`
	QualityTier  string `json:"quality_tier"`
	BasePeriod   string `json:"base_period"`
	SplicePolicy string `json:"splice_policy"`
	// Notes is the catalog's caveat about this instrument, carried onto the
	// payload that holds the numbers rather than left on /instruments where a
	// chart component fetching observations would never see it. It is the only
	// place a reader learns that a proxy index is not the Statistical Centre of
	// Iran's own 1400=100 series, or that no Article IV consultation has been
	// concluded since 2018.
	Notes string `json:"notes"`

	// AsOf is the cutoff actually used, whether the caller gave one or not.
	AsOf         time.Time `json:"as_of"`
	AsOfProvided bool      `json:"as_of_provided"`
	// EffectiveWindow states the period bounds the server read, as interpreted.
	EffectiveWindow effectiveWindow `json:"effective_window"`
	// VintagesUsed lists the distinct vintages present in `observations`,
	// ascending. A payload containing only vintage 1 is a first-print series;
	// a mixed list is the honest description of a partially revised history.
	VintagesUsed       []int `json:"vintages_used"`
	IncludeProjections bool  `json:"include_projections"`
	// ProjectionsInPage counts the projection rows in `observations`. It was
	// called projection_count, which on the default read (include_projections
	// off, so every projection withheld) was always 0 and read as "this series
	// has none". ProjectionsAvailable is the series-wide coverage count --
	// every stored projected period, unfiltered by as_of, window or limit -- so
	// "0 in page, 6 available" cannot be mistaken for "there are none".
	ProjectionsInPage    int   `json:"projections_in_page"`
	ProjectionsAvailable int64 `json:"projections_available"`
	// HasMore says older periods matched this request and did not fit under
	// `limit`; NextBefore is the oldest period in this page, which the client
	// sends back as ?before= to continue. Without them a 700-period series read
	// with no parameters returned its newest 500 and a client charting "full
	// history" plotted a series that starts 41 years late, with nothing in the
	// payload to say so.
	HasMore      bool              `json:"has_more"`
	NextBefore   *string           `json:"next_before"`
	Observations []observationItem `json:"observations"`
	Count        int               `json:"count"`
}

// observationsSelect is THE point-in-time read.
//
// DISTINCT ON (ref_period_start) with ORDER BY ref_period_start DESC,
// available_at DESC, vintage DESC keeps, for each reference period, the newest
// row that was already available at the cutoff -- which is exactly the column
// order of idx_econ_obs_pit, so the index serves the statement directly.
//
// The filter is available_at, never published_at. A source that publishes a
// revision on the 1st but only becomes readable to us on the 5th was not
// knowable on the 2nd, and pretending otherwise leaks the future into a
// backtest.
// $5 is the pagination cursor and is EXCLUSIVE: it names a period the caller
// already has. $7 is fetchLimit(), one more than the page carries, so the
// statement itself proves whether older periods exist.
const observationsSelect = `
	SELECT DISTINCT ON (o.ref_period_start)
	       o.ref_period_start, o.ref_period_end, o.ref_period_label,
	       o.value::float8, o.vintage, o.available_at, o.published_at,
	       o.is_projection, o.is_nowcast
	FROM economic_observations o
	WHERE o.series_id = $1
	  AND o.available_at <= $2::timestamptz
	  AND ($3::date IS NULL OR o.ref_period_start >= $3)
	  AND ($4::date IS NULL OR o.ref_period_start <= $4)
	  AND ($5::date IS NULL OR o.ref_period_start < $5)
	  AND ($6::bool OR NOT o.is_projection)
	ORDER BY o.ref_period_start DESC, o.available_at DESC, o.vintage DESC
	LIMIT $7`

// applyPointInTime reduces rows to the point-in-time view: for each reference
// period, the row with the highest (available_at, vintage) among those already
// available at the cutoff. Rows after the cutoff are dropped entirely, and
// projections are dropped unless explicitly asked for.
//
// observationsSelect already returns rows this way. The reduction is repeated
// here for the reason sortTrendEventRows exists in internal/prices: it makes
// the contract expressible -- and genuinely testable -- without a database, and
// re-applying it to an already-reduced page costs nothing. Vintage selection is
// the one rule in this package that must never be wrong, so it is enforced
// twice rather than trusted to a statement no Go test can execute.
//
// Result order is ref_period_start DESCENDING, matching the SQL, so a caller
// that truncates keeps the most recent periods.
func applyPointInTime(rows []observationRow, asOf time.Time, includeProjections bool) []observationRow {
	best := map[string]observationRow{}
	for _, r := range rows {
		if r.AvailableAt.After(asOf) {
			continue
		}
		if r.IsProjection && !includeProjections {
			continue
		}
		key := r.RefPeriodStart.UTC().Format(dateLayout)
		cur, seen := best[key]
		if !seen || newerVintage(r, cur) {
			best[key] = r
		}
	}
	out := make([]observationRow, 0, len(best))
	for _, r := range best {
		out = append(out, r)
	}
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if !a.RefPeriodStart.Equal(b.RefPeriodStart) {
			return a.RefPeriodStart.After(b.RefPeriodStart)
		}
		return newerVintage(a, b)
	})
	return out
}

// newerVintage reports whether a supersedes b for the same reference period:
// later availability wins, and an equal availability is broken by the higher
// vintage number. Same precedence as the ORDER BY in observationsSelect.
func newerVintage(a, b observationRow) bool {
	if !a.AvailableAt.Equal(b.AvailableAt) {
		return a.AvailableAt.After(b.AvailableAt)
	}
	return a.Vintage > b.Vintage
}

// buildObservationsResponse renders the point-in-time payload. Pure function
// (unit tested): no clock (the cutoff arrives in q), no database.
//
// The limit takes the most RECENT periods, then the payload is presented oldest
// first, which is the order a series is read, differenced and charted in. When
// the limit actually cut something off, has_more and next_before say so: a
// truncated page that looks exactly like a complete one is how a client comes to
// chart a 700-period series as if its first 200 periods never existed.
func buildObservationsResponse(meta seriesRow, rows []observationRow, q observationQuery) observationsResponse {
	selected := applyPointInTime(rows, q.AsOf, q.IncludeProjections)
	hasMore := false
	if q.Limit >= 0 && len(selected) > q.Limit {
		selected = selected[:q.Limit] // newest-first, so this keeps the newest
		hasMore = true
	}

	out := observationsResponse{
		Code:         meta.Code,
		Measure:      meta.Measure,
		Unit:         meta.Unit,
		Frequency:    meta.Frequency,
		Calendar:     meta.Calendar,
		ProviderCode: meta.ProviderCode,
		QualityTier:  meta.QualityTier,
		BasePeriod:   meta.BasePeriod,
		SplicePolicy: meta.SplicePolicy,
		Notes:        meta.Notes,
		AsOf:         q.AsOf.UTC(),
		AsOfProvided: q.AsOfProvided,
		EffectiveWindow: effectiveWindow{
			From:   dateString(q.From),
			To:     dateString(q.To),
			Before: dateString(q.Before),
		},
		IncludeProjections: q.IncludeProjections,
		// The series-wide projection count travels even on a default read that
		// withholds every projection, so "none in this page" cannot be read as
		// "this series has none".
		ProjectionsAvailable: meta.ProjectionCount,
		HasMore:              hasMore,
		// Never nil: a series with no observation at this cutoff is a 200 with
		// an empty list. "Nothing was knowable yet" is a real answer and must
		// not be confused with a missing key or a 404.
		VintagesUsed: []int{},
		Observations: make([]observationItem, 0, len(selected)),
	}
	// The cursor is the OLDEST period in this page, handed back as ?before=
	// (exclusive), so the next page continues where this one stopped. Null on
	// an empty page (nothing to page back from) AND null once has_more is
	// false: a client that pages until the cursor is null would otherwise make
	// one guaranteed-empty extra request, which makes next_before a weaker
	// completion signal than has_more for no reason.
	if hasMore && len(selected) > 0 {
		oldest := selected[len(selected)-1].RefPeriodStart.UTC().Format(dateLayout)
		out.NextBefore = &oldest
	}

	seenVintage := map[int]bool{}
	// Walk oldest-first so the payload comes out chronological.
	for i := len(selected) - 1; i >= 0; i-- {
		r := selected[i]
		if r.IsProjection {
			out.ProjectionsInPage++
		}
		if !seenVintage[r.Vintage] {
			seenVintage[r.Vintage] = true
			out.VintagesUsed = append(out.VintagesUsed, r.Vintage)
		}
		out.Observations = append(out.Observations, observationItem{
			RefPeriodStart: r.RefPeriodStart.UTC().Format(dateLayout),
			RefPeriodEnd:   r.RefPeriodEnd.UTC().Format(dateLayout),
			RefPeriodLabel: r.RefPeriodLabel,
			Value:          r.Value,
			Vintage:        r.Vintage,
			AvailableAt:    r.AvailableAt.UTC(),
			PublishedAt:    utcPtr(r.PublishedAt),
			IsProjection:   r.IsProjection,
			IsNowcast:      r.IsNowcast,
		})
	}
	sort.Ints(out.VintagesUsed)
	out.Count = len(out.Observations)
	return out
}

// Observations implements
// GET /api/v1/series/{code}/observations?as_of=&from=&to=&before=&limit=&include_projections=.
//
// The page carries the newest `limit` periods; ?before= walks backwards from
// `next_before` exactly as ?before= does on /market/candles.
func (h *Handler) Observations(w http.ResponseWriter, r *http.Request) {
	code := chi.URLParam(r, "code")
	q, perr := parseObservationQuery(code, r.URL.Query(), time.Now())
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}

	ctx := r.Context()
	// The series must be resolved first: it supplies the id the observations
	// are keyed by AND the provenance the response is required to carry. An
	// unknown code is refused here rather than answered with an empty list.
	meta, err := scanSeriesRow(h.Pool.QueryRow(ctx, seriesByCodeSelect, q.Code))
	if errors.Is(err, pgx.ErrNoRows) {
		unknownSeries(w, q.Code)
		return
	}
	if err != nil {
		h.Log.Error("observations_series", "error", err, "code", q.Code)
		httpserver.Internal(w, "database error")
		return
	}

	// fetchLimit(), not Limit: the extra period is what proves has_more.
	rows, err := h.Pool.Query(ctx, observationsSelect,
		meta.ID, q.AsOf, q.From, q.To, q.Before, q.IncludeProjections, q.fetchLimit())
	if err != nil {
		h.Log.Error("observations_query", "error", err, "code", q.Code)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	scanned := []observationRow{}
	for rows.Next() {
		var o observationRow
		if err := rows.Scan(&o.RefPeriodStart, &o.RefPeriodEnd, &o.RefPeriodLabel,
			&o.Value, &o.Vintage, &o.AvailableAt, &o.PublishedAt,
			&o.IsProjection, &o.IsNowcast); err != nil {
			h.Log.Error("observations_scan", "error", err, "code", q.Code)
			httpserver.Internal(w, "database error")
			return
		}
		scanned = append(scanned, o)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("observations_rows", "error", err, "code", q.Code)
		httpserver.Internal(w, "database error")
		return
	}

	httpserver.JSON(w, http.StatusOK, buildObservationsResponse(meta, scanned, q))
}
