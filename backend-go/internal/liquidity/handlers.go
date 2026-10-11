package liquidity

// The database side and the two routes.
//
// Both answers are a pure function of five stored series and the date, so
// they are built once per revision of that data and every other request reads
// memory, the pattern internal/bourse uses for its index store. The revision
// key is the footprint of everything the build reads: the CBI observations
// (how many rows, how many already available, the newest id and availability),
// each series' registry row and unit, USD_IRT's stored closes and its entry in
// the splice register -- plus the Tehran date and the UTC date, because how
// old the newest figures are changes at Tehran's midnight and which dollar
// month is still running changes at UTC's (the daily closes are UTC-day
// buckets), both without any row changing.
//
// A series that is not ingested yet is not an error. The registry lookup
// returns economic.ErrSeriesNotFound, the series is carried as unregistered,
// and both routes answer 200 with what they have and a reason for what they do
// not. Only a database failure is a 500.

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/url"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/relvalue"
)

// Handler serves /api/v1/liquidity/*.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger

	mu    sync.Mutex
	built *builtResponses
}

// NewHandler is the constructor main.go wires.
func NewHandler(pool *pgxpool.Pool, log *slog.Logger) *Handler {
	return &Handler{Pool: pool, Log: log}
}

// builtResponses is every answer the two routes can give, for one revision.
type builtResponses struct {
	key     string
	monthly aggregatesResponse
	annual  aggregatesResponse
	dollar  map[int]dollarResponse
}

// horizons is the closed ?horizon= vocabulary: 12 months by default, 24 with
// its lower measured coverage stated.
var horizons = []int{12, 24}

const defaultHorizon = 12

// --- parameters -----------------------------------------------------------------

type paramError struct {
	Message string
	Details map[string]any
}

func badParam(message string, details map[string]any) *paramError {
	return &paramError{Message: message, Details: details}
}

// parseFreq reads ?freq=. Absent means monthly; anything outside the
// vocabulary is refused rather than read as monthly.
func parseFreq(q url.Values) (string, *paramError) {
	switch raw := strings.ToLower(strings.TrimSpace(q.Get("freq"))); raw {
	case "", "monthly":
		return "monthly", nil
	case "annual":
		return "annual", nil
	default:
		return "", badParam(fmt.Sprintf("freq must be monthly or annual, got %q", q.Get("freq")),
			map[string]any{"freq": q.Get("freq"), "supported": []string{"monthly", "annual"}})
	}
}

// parseHorizon reads ?horizon=. Only 12 and 24: the band and its coverage are
// measured for those, and a horizon the backtest cannot support is refused
// rather than drawn.
func parseHorizon(q url.Values) (int, *paramError) {
	raw := strings.TrimSpace(q.Get("horizon"))
	if raw == "" {
		return defaultHorizon, nil
	}
	for _, h := range horizons {
		if raw == fmt.Sprint(h) {
			return h, nil
		}
	}
	return 0, badParam(fmt.Sprintf("horizon must be 12 or 24 (months), got %q", raw),
		map[string]any{"horizon": raw, "supported": horizons})
}

// --- routes ---------------------------------------------------------------------

// Aggregates implements GET /api/v1/liquidity/aggregates?freq=monthly|annual.
func (h *Handler) Aggregates(w http.ResponseWriter, r *http.Request) {
	freq, perr := parseFreq(r.URL.Query())
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	b, err := h.responses(r.Context())
	if err != nil {
		h.Log.Error("liquidity_build", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if freq == "annual" {
		httpserver.JSON(w, http.StatusOK, b.annual)
		return
	}
	httpserver.JSON(w, http.StatusOK, b.monthly)
}

// Dollar implements GET /api/v1/liquidity/dollar?horizon=12|24.
func (h *Handler) Dollar(w http.ResponseWriter, r *http.Request) {
	horizon, perr := parseHorizon(r.URL.Query())
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	b, err := h.responses(r.Context())
	if err != nil {
		h.Log.Error("liquidity_build", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, b.dollar[horizon])
}

// --- the cache --------------------------------------------------------------------

// revisionKeySelect is the footprint of everything the build reads. $1 is the
// CBI codes, $2 the dollar's symbol, $3 the build instant: rows that become
// available later change the key when they do.
const revisionKeySelect = `
	SELECT coalesce((SELECT count(*) || '/' || count(*) FILTER (WHERE o.available_at <= $3)
	                        || '/' || coalesce(max(o.id), 0)
	                        || '/' || coalesce(max(o.available_at)::text, '')
	                 FROM economic_observations o
	                 JOIN economic_series s ON s.id = o.series_id
	                 WHERE s.code = ANY($1)), '')
	    || '|' || coalesce((SELECT string_agg(s.code || ':' || i.unit || ':' || s.updated_at::text, ','
	                                          ORDER BY s.code)
	                        FROM economic_series s
	                        JOIN instruments i ON i.code = s.code
	                        WHERE s.code = ANY($1)), '')
	    || '|' || coalesce((SELECT count(*) || '/' || coalesce(max(observed_at)::text, '')
	                        FROM prices
	                        WHERE symbol = $2 AND quality = 'ok' AND observed_at < $3), '')
	    || '|' || coalesce((SELECT md5(value::text) FROM app_settings WHERE key = 'series_splices'), '')`

// spliceSelect is USD_IRT's entry in the splice register the TGJU backfill
// keeps (prediction-python app/jobs/tgju_backfill.py): where the series
// changes definition and what it measures on each side. NULL when the
// register has no such entry; no row when there is no register.
const spliceSelect = `SELECT value -> 'USD_IRT' FROM app_settings WHERE key = 'series_splices'`

// responses returns the built answers for the data as it stands, rebuilding
// them when the revision key has moved. The lock is held across a rebuild so
// concurrent requests after an ingest build once, not once each.
func (h *Handler) responses(ctx context.Context) (*builtResponses, error) {
	now := timeNow().UTC()
	var footprint string
	if err := h.Pool.QueryRow(ctx, revisionKeySelect, seriesOrder, relvalue.SeriesUSD, now).
		Scan(&footprint); err != nil {
		return nil, fmt.Errorf("revision key: %w", err)
	}
	key := revisionKey(footprint, now)

	h.mu.Lock()
	defer h.mu.Unlock()
	if h.built != nil && h.built.key == key {
		return h.built, nil
	}
	money, err := h.loadMoney(ctx, now)
	if err != nil {
		return nil, err
	}
	closes, err := relvalue.LoadDailyCloses(ctx, h.Pool, relvalue.SeriesUSD, now)
	if err != nil {
		return nil, fmt.Errorf("usd closes: %w", err)
	}
	var rawSplice []byte
	err = h.Pool.QueryRow(ctx, spliceSelect).Scan(&rawSplice)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, fmt.Errorf("splice register: %w", err)
	}
	splice, err := parseSplice(rawSplice)
	if err != nil {
		// A register this package cannot read is reported and left out: the
		// notes then claim no splice date rather than a guessed one.
		h.Log.Warn("liquidity_splice_register", "error", err)
	}
	b := buildAll(key, money, toDayCloses(closes), splice, now)
	h.built = b
	return b, nil
}

// revisionKey is the data footprint plus the two dates the answers depend on
// without any row changing (see the file comment). Pure function.
func revisionKey(footprint string, now time.Time) string {
	return footprint + "|" + tehranToday(now).Format(dateLayout) + "|" + utcDay(now).Format(dateLayout)
}

// parseSplice reads USD_IRT's splice-register entry. nil for no entry; an
// error, and nil, for one without a readable splice_at.
func parseSplice(raw []byte) (*usdSplice, error) {
	if len(raw) == 0 || string(raw) == "null" {
		return nil, nil
	}
	var e struct {
		SpliceAt string `json:"splice_at"`
		Before   string `json:"backfill_definition"`
		After    string `json:"live_definition"`
	}
	if err := json.Unmarshal(raw, &e); err != nil {
		return nil, fmt.Errorf("USD_IRT entry: %w", err)
	}
	at, err := time.Parse(time.RFC3339Nano, e.SpliceAt)
	if err != nil {
		return nil, fmt.Errorf("USD_IRT splice_at %q: %w", e.SpliceAt, err)
	}
	if e.Before == "" {
		e.Before = "the backfilled history"
	}
	if e.After == "" {
		e.After = "the live collection"
	}
	return &usdSplice{At: at.UTC(), Before: e.Before, After: e.After}, nil
}

// buildAll builds every response for one revision. Pure function.
func buildAll(key string, money map[string]moneySeries, usd []dayClose, splice *usdSplice,
	now time.Time) *builtResponses {
	b := &builtResponses{
		key:     key,
		monthly: buildAggregatesResponse(money, "monthly", now),
		annual:  buildAggregatesResponse(money, "annual", now),
		dollar:  map[int]dollarResponse{},
	}
	in := dollarInput{Now: now, USD: usd, M2: money[CodeLiquidity], Splice: splice}
	for _, hz := range horizons {
		b.dollar[hz] = buildDollarResponse(in, hz)
	}
	return b
}

func toDayCloses(points []relvalue.Point) []dayClose {
	out := make([]dayClose, 0, len(points))
	for _, p := range points {
		out = append(out, dayClose{Day: p.Day.UTC(), Close: p.Value})
	}
	return out
}

// --- reading the money series -------------------------------------------------------

func (h *Handler) loadMoney(ctx context.Context, asOf time.Time) (map[string]moneySeries, error) {
	out := map[string]moneySeries{}
	for _, code := range seriesOrder {
		prov, obs, err := economic.PointInTimeDetailed(ctx, h.Pool, code, asOf)
		if errors.Is(err, economic.ErrSeriesNotFound) {
			out[code] = unregistered(code)
			continue
		}
		if err != nil {
			return nil, fmt.Errorf("%s: %w", code, err)
		}
		out[code] = toMoneySeries(code, prov, obs)
	}
	return out, nil
}

// unregistered is a series the registry does not carry yet.
func unregistered(code string) moneySeries {
	n := seriesNames[code]
	return moneySeries{Code: code, NameEN: n[0], NameFA: n[1], Points: []moneyPoint{}}
}

// toMoneySeries projects the point-in-time read onto this package's months.
// Pure function (unit tested).
//
//   - The period is the publisher's own label (YYYY-MM); a label that is not
//     a Jalali month is counted and left out, never re-derived from dates.
//   - A unit other than CBI's own is converted only when the factor is known
//     (unitFactors); otherwise the whole series is refused, because a factor
//     of ten guessed wrong is a silent tenfold error.
//   - Base money carries its method: the stored one, else the only definition
//     its period was ever printed on.
func toMoneySeries(code string, prov economic.SeriesProvenance, obs []economic.ObservationDetail) moneySeries {
	s := unregistered(code)
	s.Registered = true
	if prov.NameEN != "" {
		s.NameEN = prov.NameEN
	}
	if prov.NameFA != "" {
		s.NameFA = prov.NameFA
	}
	s.ProviderCode, s.QualityTier = prov.ProviderCode, prov.QualityTier
	factor, ok := unitFactors[prov.Unit]
	if !ok {
		s.Refused = fmt.Sprintf("%s is stored in unit %q, which this page does not convert (it "+
			"expects %s, CBI's own unit); it is not shown rather than shown at a guessed scale.",
			code, prov.Unit, Unit)
		return s
	}
	byMonth := map[Month]moneyPoint{}
	for _, o := range obs {
		m, err := ParseMonth(o.RefPeriodLabel)
		if err != nil {
			s.Unreadable++
			continue
		}
		p := moneyPoint{
			Month: m, Value: o.Value, Vintage: o.Vintage,
			AvailableAt: o.AvailableAt.UTC(), AvailableAtApprox: storedApprox(o.Columns),
			PublishedAt: o.PublishedAt,
		}
		if factor != 1 {
			// Twelve significant digits: 15581.22 همت is 155812.2, not the
			// 155812.19999999998 binary arithmetic makes of it.
			p.Value = roundSig(o.Value*factor, 12)
		}
		if o.Source != nil {
			p.Source = sourceKind(o.Source.URL, o.Source.Title, o.Source.MediaType)
			p.SourceURL = o.Source.URL
		}
		if code == CodeBaseMoney {
			p.Method, p.MethodInferred = effectiveMethod(m, storedMethod(o.Columns))
		}
		if prev, dup := byMonth[m]; dup && !p.AvailableAt.After(prev.AvailableAt) {
			continue // two periods with one label: the later-available one stands
		}
		byMonth[m] = p
	}
	s.Points = make([]moneyPoint, 0, len(byMonth))
	for _, p := range byMonth {
		s.Points = append(s.Points, p)
	}
	sort.Slice(s.Points, func(i, j int) bool { return s.Points[i].Month < s.Points[j].Month })
	return s
}
