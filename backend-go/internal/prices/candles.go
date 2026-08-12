package prices

// The trading panel's data feed: OHLC candles synthesized from tick history at
// an arbitrary timeframe, paginated, with chart-ready overlays computed on the
// same buckets and aligned by index.
//
// `prices` holds TICKS, not exchange OHLC. Every candle here is manufactured by
// grouping ticks into a time bucket, so two things a normal candle API takes for
// granted are false and are reported rather than papered over:
//
//   - A bucket containing a single tick has NO real high/low — its four prices
//     are one number repeated. `synthetic` says so, and the chart renders those
//     bars differently instead of implying a range that never existed. This is
//     not a corner case: IR_GOLD_18K was one observation per day from 2022-04-20
//     until 2026-07-20, so the overwhelming majority of daily buckets are flat.
//   - There is no volume column anywhere in `prices`. `volume` is always null.
//     A synthesized volume would be read as a measurement.
//
// `coverage` states the limits of the source directly — the base granularity of
// the ticks, when intraday density actually begins, and which timeframes that
// leaves — and an interval the data cannot support is REFUSED. A chart that
// quietly shows 1h when asked for 15m is worse than an error.
//
// Buckets are floored in UTC epoch space, matching
// prediction-python/app/jobs/trend_alignment.py::_load_candles exactly:
// quality='ok' only, open = first value by (observed_at ASC, id ASC), close =
// last by (observed_at DESC, id DESC), max/min for the extremes. The chart and
// the indicators drawn on it must not disagree about what a candle is. Flooring
// in Tehran local time would land 4h boundaries on :30 marks, so display
// timezone never touches bucketing.
//
// A requested ?from/?to window is snapped OUTWARD onto bucket boundaries —
// `from` floored down, `to` ceiled up — and the snapped bounds gate BOTH which
// buckets are selected AND which ticks are aggregated into them, so every
// bucket returned is whole. Snapping inward would publish a sliced bucket under
// a whole bucket's identity: ?from=2026-08-11T23:50Z at 1d would otherwise
// stamp ten minutes of ticks as the confirmed daily candle for Aug 11, with
// `synthetic` computed off that truncated count and a "daily range" that is
// really a ten-minute one — exactly the dishonesty the flags above exist to
// prevent, and the chart caches bars by bucket start, so the corrupted bar
// lands under the real bucket's identity. `effective_window` states the bounds
// actually served. The one bucket bounded by something other than a boundary
// is the newest, which is bounded by `now` because that is where the data ends;
// it is labelled confirmed:false rather than trimmed.
//
// `to` is exclusive after snapping: the bucket CONTAINING `to` is served, and a
// `to` that already sits on a boundary opens a bucket that is not, so adjacent
// windows tile without overlapping. `before` is the one bound that is never
// ceiled — it is the pagination cursor, exclusive on bucket start, and ceiling
// it would re-serve the caller the bucket it just received.
//
// Nothing here forecasts. Candle synthesis and the indicator overlays are
// technical arithmetic over stored observations; model input, prediction,
// calibration and the buy/sell policy are untouched.

import (
	"context"
	"fmt"
	"math"
	"net/http"
	"net/url"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/indicators"
)

// candleInterval is one selectable timeframe.
type candleInterval struct {
	Name    string
	Seconds int64
	// Weekly buckets are the one interval that is NOT an epoch floor: unix 0
	// was a Thursday, so 604800-second epoch buckets would start mid-week,
	// and every charting convention draws weekly candles from Monday.
	Weekly bool
}

// candleIntervals is the canonical timeframe list, ascending. Order is the
// published order of `coverage.supported_intervals`.
var candleIntervals = []candleInterval{
	{Name: "5m", Seconds: 300},
	{Name: "10m", Seconds: 600},
	{Name: "15m", Seconds: 900},
	{Name: "20m", Seconds: 1200},
	{Name: "30m", Seconds: 1800},
	{Name: "45m", Seconds: 2700},
	{Name: "1h", Seconds: 3600},
	{Name: "2h", Seconds: 7200},
	{Name: "3h", Seconds: 10800},
	{Name: "4h", Seconds: 14400},
	{Name: "6h", Seconds: 21600},
	{Name: "8h", Seconds: 28800},
	{Name: "12h", Seconds: 43200},
	{Name: "1d", Seconds: 86400},
	{Name: "2d", Seconds: 172800},
	{Name: "3d", Seconds: 259200},
	{Name: "1w", Seconds: 604800, Weekly: true},
}

// candleIntervalAliases keeps the two names the endpoint shipped with alive.
// Callers written against the old contract must keep working; the response
// always echoes the canonical name they resolve to.
var candleIntervalAliases = map[string]string{
	"hourly": "1h",
	"daily":  "1d",
}

const (
	defaultCandleSymbol   = "IR_GOLD_18K"
	defaultCandleInterval = "1d"
	defaultCandleLimit    = 500
	maxCandleLimit        = 2000

	// Overlay warm-up: SMA50 needs 50 buckets and Ichimoku senkou B needs 52
	// before either is defined. These extra buckets are fetched and fed to the
	// indicators but never returned, so the first VISIBLE candle already has
	// its overlays instead of a chart that starts with 50 blank bars.
	candleWarmupBuckets = 60

	// Lookback of the published support/resistance band. It is measured over
	// the FULL fetched array (warm-up included) and never over the returned
	// page: indicators.SupportResistance clamps a short slice to whatever it
	// was given instead of refusing, so a three-bar page — what the shipped
	// chart's live poll asks for — would answer with a three-bar extreme that
	// is indistinguishable on the wire from a twenty-bucket one. A fetch that
	// holds fewer than this many buckets reports null instead.
	supportResistanceLookback = 20

	// A UTC day needs at least this many ticks to count as intraday-capable.
	// Twelve is deliberately low: it is two orders of magnitude above the
	// one-per-day seeded era and still well under the ~290 a 5-minute day
	// produces, so the boundary lands on the density change rather than on a
	// day the collector happened to miss a few polls.
	intradayMinTicksPerDay = 12

	// How far back one page may scan, as a multiple of the buckets it needs.
	// The LIMIT already bounds the result; this bounds the aggregation, which
	// would otherwise re-scan the symbol's entire history on every poll. It is
	// an optimization only — a short page triggers an unbounded retry below,
	// because a sparse stretch must not be mistaken for the end of the data.
	candleScanSlack = 4

	// The one message a client sees when it asks for a timeframe this symbol's
	// data cannot honestly produce.
	unsupportedIntervalMessage = "This timeframe is not available for the current data source."
)

// coverageTTL is how long a symbol's coverage answer is reused. The coverage
// query scans that symbol's whole history to find where intraday density
// begins; the chart polls once a minute and pages backwards through history,
// and none of those requests may pay for it.
const coverageTTL = 10 * time.Minute

// --- intervals ---------------------------------------------------------------

func lookupCandleInterval(name string) (candleInterval, bool) {
	for _, iv := range candleIntervals {
		if iv.Name == name {
			return iv, true
		}
	}
	return candleInterval{}, false
}

func candleIntervalNames() []string {
	out := make([]string, 0, len(candleIntervals))
	for _, iv := range candleIntervals {
		out = append(out, iv.Name)
	}
	return out
}

// ParseCandleInterval resolves ?interval=, applying the back-compat aliases.
// Empty means the default; anything unrecognized is an error naming the whole
// vocabulary, so a client never has to guess what it may ask for.
//
// Like /intelligence/news and unlike the older market endpoints, this refuses
// rather than substitutes: a caller that asked for 15m and silently received
// 1h cannot tell that its chart is wrong.
func ParseCandleInterval(raw string) (candleInterval, error) {
	name := raw
	if name == "" {
		name = defaultCandleInterval
	}
	if canonical, ok := candleIntervalAliases[name]; ok {
		name = canonical
	}
	iv, ok := lookupCandleInterval(name)
	if !ok {
		return candleInterval{}, fmt.Errorf("interval must be one of %s (or the aliases daily, hourly), got %q",
			strings.Join(candleIntervalNames(), ", "), raw)
	}
	return iv, nil
}

// floorEpoch is the Go twin of
// to_timestamp(floor(extract(epoch from observed_at) / n) * n).
func floorEpoch(t time.Time, seconds int64) time.Time {
	s := t.Unix()
	q := s / seconds
	// Go truncates integer division toward zero, SQL's floor() does not.
	// Prices are all post-1970, but a bucket boundary must not depend on that.
	if s < 0 && s%seconds != 0 {
		q--
	}
	return time.Unix(q*seconds, 0).UTC()
}

// floorWeekUTC is the Go twin of date_trunc('week', observed_at) under a UTC
// session: Monday 00:00 UTC.
func floorWeekUTC(t time.Time) time.Time {
	u := t.UTC()
	day := time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC)
	return day.AddDate(0, 0, -((int(day.Weekday()) + 6) % 7))
}

// BucketStart floors an instant onto this interval's bucket boundary, in UTC.
// The input's location is irrelevant by construction: epoch flooring works on
// the instant, so a Tehran +03:30 timestamp lands on the same UTC boundary its
// UTC equivalent does.
func (iv candleInterval) BucketStart(t time.Time) time.Time {
	if iv.Weekly {
		return floorWeekUTC(t)
	}
	return floorEpoch(t, iv.Seconds)
}

// BucketEnd is the exclusive end of the bucket starting at `start`, matching
// `start + timedelta(seconds=width)` in the Python trend-alignment engine.
func (iv candleInterval) BucketEnd(start time.Time) time.Time {
	return start.Add(time.Duration(iv.Seconds) * time.Second)
}

// BucketCeil rounds an instant UP onto a bucket boundary: the exclusive end of
// the bucket containing it, or the instant itself when it is already a
// boundary. This is the upper half of snapping a window outward — it keeps the
// bucket that contains `to` whole instead of dropping it, while a `to` that is
// already a boundary stays put so adjacent windows tile without overlapping.
func (iv candleInterval) BucketCeil(t time.Time) time.Time {
	// Equal compares instants, so a Tehran-offset input needs no conversion:
	// it is on a boundary exactly when its UTC equivalent is.
	start := iv.BucketStart(t)
	if start.Equal(t) {
		return start
	}
	return iv.BucketEnd(start)
}

// Confirmed reports whether a bucket has finished. The forming candle is still
// returned — a chart needs its live bar — but it is labelled, because a value
// read off an unfinished bucket can still move.
func (iv candleInterval) Confirmed(start, now time.Time) bool {
	return !iv.BucketEnd(start).After(now)
}

// --- request parsing ---------------------------------------------------------

// candleQuery is a validated request. Parsing is separated from the handler so
// the contract is testable without a database.
type candleQuery struct {
	Symbol   string
	Interval candleInterval
	Limit    int
	// Before is the pagination cursor: an EXCLUSIVE upper bound on bucket
	// start, already floored onto a bucket boundary so a page can never end
	// with a bucket that was cut in half by the cursor.
	Before   *time.Time
	From     *time.Time
	To       *time.Time
	Overlays bool
}

// paramError carries the message and details of a rejected parameter, so the
// handler writes exactly one kind of 400.
type paramError struct {
	Message string
	Details map[string]any
}

func (e *paramError) Error() string { return e.Message }

func badParam(message string, details map[string]any) *paramError {
	return &paramError{Message: message, Details: details}
}

// parseCandleTime accepts RFC3339 or bare unix seconds. Both forms appear in
// the wild: the frontend round-trips the ISO string this endpoint returns, and
// chart libraries hand back the unix seconds they were given as `t`.
func parseCandleTime(name, raw string) (time.Time, *paramError) {
	if n, err := strconv.ParseInt(raw, 10, 64); err == nil {
		return time.Unix(n, 0).UTC(), nil
	}
	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return time.Time{}, badParam(
			fmt.Sprintf("%s must be RFC3339 or unix seconds, got %q", name, raw),
			map[string]any{name: raw})
	}
	return t.UTC(), nil
}

// parseCandleBool reads the ?overlays= switch. Unknown values are refused
// rather than treated as false: silently dropping the overlay block would look
// like an empty indicator set.
func parseCandleBool(name, raw string) (bool, *paramError) {
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

// legacyDefaultDays reproduces the window the old endpoint fell back to when
// ?days= was present but unreadable: 120 daily bars, 14 days of hourly ones.
func legacyDefaultDays(iv candleInterval) int {
	if iv.Seconds >= 86400 {
		return 120
	}
	return 14
}

// candleLimitFromDays converts the legacy ?days= window into a bucket count.
// The old endpoint showed `days` daily buckets or `days*24` hourly ones; the
// same arithmetic generalized to any interval reproduces both exactly.
func candleLimitFromDays(days int, iv candleInterval) int {
	buckets := (int64(days)*86400 + iv.Seconds - 1) / iv.Seconds // ceil
	if buckets < 1 {
		buckets = 1
	}
	if buckets > maxCandleLimit {
		buckets = maxCandleLimit
	}
	return int(buckets)
}

// parseCandleQuery validates the query string. Pure function (unit tested):
// no clock, no database.
func parseCandleQuery(q url.Values) (candleQuery, *paramError) {
	out := candleQuery{Symbol: q.Get("symbol"), Overlays: true}
	if out.Symbol == "" {
		out.Symbol = defaultCandleSymbol
	}
	if !KnownSymbols[out.Symbol] {
		return out, badParam("unknown symbol", map[string]any{"symbol": out.Symbol})
	}

	iv, err := ParseCandleInterval(q.Get("interval"))
	if err != nil {
		return out, badParam(err.Error(), map[string]any{"interval": q.Get("interval")})
	}
	out.Interval = iv

	// limit is the modern, strict parameter; days is the legacy one and keeps
	// its old lenient clamp, because a caller that worked yesterday must not
	// start receiving 400s today.
	switch raw := q.Get("limit"); {
	case raw != "":
		n, convErr := strconv.Atoi(raw)
		if convErr != nil {
			return out, badParam(
				fmt.Sprintf("limit must be an integer, got %q", raw),
				map[string]any{"limit": raw})
		}
		if n < 1 || n > maxCandleLimit {
			return out, badParam(
				fmt.Sprintf("limit must be between 1 and %d, got %d", maxCandleLimit, n),
				map[string]any{"limit": raw})
		}
		out.Limit = n
	case q.Get("days") != "":
		out.Limit = candleLimitFromDays(intParam(q.Get("days"), legacyDefaultDays(iv), 1, 3650), iv)
	default:
		out.Limit = defaultCandleLimit
	}

	for _, p := range []struct {
		name string
		dst  **time.Time
	}{{"before", &out.Before}, {"from", &out.From}, {"to", &out.To}} {
		raw := q.Get(p.name)
		if raw == "" {
			continue
		}
		t, perr := parseCandleTime(p.name, raw)
		if perr != nil {
			return out, perr
		}
		*p.dst = &t
	}
	if out.From != nil && out.To != nil && !out.From.Before(*out.To) {
		return out, badParam("from must be earlier than to", map[string]any{
			"from": out.From.Format(time.RFC3339), "to": out.To.Format(time.RFC3339),
		})
	}

	if raw := q.Get("overlays"); raw != "" {
		on, perr := parseCandleBool("overlays", raw)
		if perr != nil {
			return out, perr
		}
		out.Overlays = on
	}
	return out, nil
}

// --- effective window --------------------------------------------------------

// candleWindow is the window a request is actually served over, in bucket
// space: [From, To), both already snapped onto boundaries. It is echoed in the
// response as `effective_window` so a caller can see that ?to=23:59:59 was
// served as the whole day rather than guessing.
type candleWindow struct {
	// From is nil when ?from= was absent — the page is then bounded by the
	// pagination cursor and the scan floor, not by a caller's window.
	From *time.Time
	// To is exclusive and always set; with no cursor it is `now`, the only
	// bound in this file that is not a bucket boundary (see the file header).
	To time.Time
}

// snapCandleWindow resolves ?from/?to/?before into the window that will be
// served, snapping OUTWARD onto bucket boundaries. Pure function (unit tested).
//
//   - `from` is floored DOWN. Left unsnapped it slices the oldest bucket and
//     that bucket is then published under its whole-bucket identity (F3).
//   - `to` is ceiled UP. Floored down and applied exclusively it dropped the
//     bucket containing `to`, so a window naming exactly one whole day
//     (00:00:00 to 23:59:59) returned nothing at all (F2).
//   - `before` is floored down and stays exclusive: it is a cursor, not a
//     window edge, and it names a bucket the caller already has.
//
// `now` is always an upper bound: a provider with a skewed clock must not
// manufacture buckets in the future.
func snapCandleWindow(q candleQuery, iv candleInterval, now time.Time) candleWindow {
	w := candleWindow{To: now.UTC()}
	if q.From != nil {
		from := iv.BucketStart(*q.From)
		w.From = &from
	}
	if q.To != nil {
		if to := iv.BucketCeil(*q.To); to.Before(w.To) {
			w.To = to
		}
	}
	if q.Before != nil {
		if before := iv.BucketStart(*q.Before); before.Before(w.To) {
			w.To = before
		}
	}
	return w
}

// --- coverage ----------------------------------------------------------------

// candleCoverage describes what the tick history can and cannot support. It is
// part of the response because the honest answer to "why can't I see 5-minute
// candles from 2023" is a property of the data, not of the endpoint.
type candleCoverage struct {
	BaseGranularitySeconds *int       `json:"base_granularity_seconds"`
	IntradayFrom           *time.Time `json:"intraday_from"`
	HistoryFrom            *time.Time `json:"history_from"`
	SupportedIntervals     []string   `json:"supported_intervals"`
	Note                   string     `json:"note"`
}

// dayTickCount is one UTC day of tick density, as read from the database.
type dayTickCount struct {
	Day   time.Time
	Ticks int
}

// granularityLadder holds the conventional collection cadences a measured
// median gap is reported as. Snapping is in log space so the choice between
// neighbouring rungs is proportional rather than absolute.
var granularityLadder = []int{
	1, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1200, 1800, 2700,
	3600, 7200, 10800, 14400, 21600, 28800, 43200, 86400, 604800,
}

// snapGranularity reports a measured median inter-tick gap as the nearest
// conventional cadence. The measured p50 for a 5-minute collector is 300.0s
// with a p90 of 300.5s; reporting "300.5 seconds" as the base granularity
// would be precision the number does not have.
func snapGranularity(seconds float64) int {
	if seconds <= 0 {
		return 0
	}
	best, bestDist := granularityLadder[0], math.Inf(1)
	for _, rung := range granularityLadder {
		if d := math.Abs(math.Log(seconds / float64(rung))); d < bestDist {
			best, bestDist = rung, d
		}
	}
	return best
}

// computeIntradayFrom finds the earliest instant from which ticks are dense
// enough for intraday candles: the start of the earliest day with at least
// intradayMinTicksPerDay ticks in an unbroken run of such days reaching the
// present. Pure function (unit tested).
//
// The run must reach the present because the flag gates SUB-DAY intervals for
// the whole feed. A dense fortnight two years ago does not make 15m candles
// available today, and paging back into the sparse era is handled by the
// per-bucket `synthetic` flag instead.
//
// The current UTC day is exempt from the density threshold — it is still
// filling, and requiring twelve ticks of it would drop every intraday
// timeframe for the first hour of every day.
func computeIntradayFrom(days []dayTickCount, now time.Time) *time.Time {
	if len(days) == 0 {
		return nil
	}
	today := floorEpoch(now, 86400)
	i := len(days) - 1
	if days[i].Day.Equal(today) && days[i].Ticks < intradayMinTicksPerDay {
		i--
	}
	if i < 0 || days[i].Ticks < intradayMinTicksPerDay {
		return nil
	}
	// Density that stopped yesterday-but-one is density that stopped.
	if days[i].Day.Before(today.AddDate(0, 0, -1)) {
		return nil
	}
	start := i
	for start > 0 &&
		days[start-1].Ticks >= intradayMinTicksPerDay &&
		days[start-1].Day.Equal(days[start].Day.AddDate(0, 0, -1)) {
		start--
	}
	from := days[start].Day.UTC()
	return &from
}

// supportedCandleIntervals filters the canonical list down to what this
// symbol's data can actually produce: no sub-day interval without continuous
// intraday density, and nothing finer than the ticks themselves. Pure function
// (unit tested).
//
// Base granularity gates SUB-DAY intervals only. It is a property of the
// intraday feed — "no 5m candle can exist if the collector polls every hour" —
// and it says nothing about a daily candle, which four years of one-tick-a-day
// history supports whatever the recent cadence is. Letting it gate daily and
// coarser produced the inversion that proves the rule wrong: a symbol whose
// ticks were sparse enough to measure a multi-day median had every interval
// including the 1d default refused, while a symbol with too few ticks to
// measure anything at all (base nil) was served the whole daily vocabulary.
func supportedCandleIntervals(baseSeconds *int, intradayFrom *time.Time) []string {
	out := make([]string, 0, len(candleIntervals))
	for _, iv := range candleIntervals {
		if iv.Seconds < 86400 {
			if intradayFrom == nil {
				continue
			}
			if baseSeconds != nil && iv.Seconds < int64(*baseSeconds) {
				continue
			}
		}
		out = append(out, iv.Name)
	}
	return out
}

// coverageNote is the sentence a user reads when a timeframe is missing or a
// bar looks wrong. Pure function (unit tested).
func coverageNote(baseSeconds *int, intradayFrom *time.Time) string {
	var b strings.Builder
	b.WriteString("Candles are synthesized by bucketing stored ticks; " +
		"`prices` holds no exchange OHLC and no volume, so volume is always null " +
		"and a bucket with a single tick is flagged synthetic (its high and low " +
		"are that one price, not a real range).")
	if baseSeconds != nil {
		fmt.Fprintf(&b, " Measured base granularity is %ds, so no finer intraday timeframe can exist.", *baseSeconds)
	} else {
		b.WriteString(" Base granularity could not be measured (too few ticks).")
	}
	if intradayFrom != nil {
		fmt.Fprintf(&b, " Intraday density is continuous from %s; earlier buckets are daily-resolution history.",
			intradayFrom.Format(time.RFC3339))
	} else {
		b.WriteString(" Tick density has never been high enough for intraday candles, so only daily and coarser timeframes are offered.")
	}
	return b.String()
}

// coverageCache memoizes the per-symbol coverage scan. Guarded by its own
// mutex and keyed by symbol; entries are small and the symbol set is fixed, so
// it never needs eviction beyond expiry.
var coverageCache = struct {
	sync.Mutex
	entries map[string]coverageCacheEntry
}{entries: map[string]coverageCacheEntry{}}

type coverageCacheEntry struct {
	value   candleCoverage
	expires time.Time
}

func cachedCoverage(symbol string, now time.Time) (candleCoverage, bool) {
	coverageCache.Lock()
	defer coverageCache.Unlock()
	e, ok := coverageCache.entries[symbol]
	if !ok || now.After(e.expires) {
		return candleCoverage{}, false
	}
	return e.value, true
}

func storeCoverage(symbol string, cov candleCoverage, now time.Time) {
	coverageCache.Lock()
	defer coverageCache.Unlock()
	coverageCache.entries[symbol] = coverageCacheEntry{value: cov, expires: now.Add(coverageTTL)}
}

// dayTickCounts returns per-UTC-day tick density for the symbol, oldest first,
// plus the first observation ever recorded. One scan, cached by the caller.
func (h *Handler) dayTickCounts(ctx context.Context, symbol string) ([]dayTickCount, *time.Time, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT date_trunc('day', observed_at) AS d, count(*)::int, min(observed_at)
		FROM prices
		WHERE symbol=$1 AND quality='ok'
		GROUP BY d ORDER BY d ASC`, symbol)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	var out []dayTickCount
	var first *time.Time
	for rows.Next() {
		var d dayTickCount
		var min time.Time
		if err := rows.Scan(&d.Day, &d.Ticks, &min); err != nil {
			return nil, nil, err
		}
		d.Day = d.Day.UTC()
		out = append(out, d)
		if first == nil {
			u := min.UTC()
			first = &u
		}
	}
	return out, first, rows.Err()
}

// medianTickGap is the median inter-tick gap in seconds from `since` onward
// (all history when nil), or nil when fewer than two ticks exist in that
// window.
func (h *Handler) medianTickGap(ctx context.Context, symbol string, since *time.Time) (*float64, error) {
	var gap *float64
	err := h.Pool.QueryRow(ctx, `
		SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY gap)
		FROM (
			SELECT extract(epoch FROM observed_at
			               - lag(observed_at) OVER (ORDER BY observed_at ASC, id ASC)) AS gap
			FROM prices
			WHERE symbol=$1 AND quality='ok'
			      AND ($2::timestamptz IS NULL OR observed_at >= $2)
		) gaps
		WHERE gap IS NOT NULL AND gap > 0`, symbol, since).Scan(&gap)
	if err != nil {
		return nil, err
	}
	return gap, nil
}

// tickGapMeasurer measures the median inter-tick gap from an optional lower
// bound. It is a parameter of buildCoverage so the window the measurement is
// asked for — the thing defect F1 got wrong — is unit-testable without a
// database.
type tickGapMeasurer func(ctx context.Context, symbol string, since *time.Time) (*float64, error)

// buildCoverage assembles the coverage answer from the per-day density scan and
// exactly one gap measurement.
//
// The gap is measured over the intraday-dense period, NOT over a trailing
// window: base granularity describes the collector's intraday cadence, and a
// trailing week can be consumed almost entirely by one provider outage, in
// which case the median describes the outage. Measured from intraday_from the
// number keeps meaning what it says. With no dense period there is nothing to
// characterize but the whole history, and base granularity then gates nothing
// (every sub-day interval is already refused for want of density).
func buildCoverage(ctx context.Context, symbol string, days []dayTickCount,
	historyFrom *time.Time, now time.Time, measure tickGapMeasurer) (candleCoverage, error) {
	intraday := computeIntradayFrom(days, now)
	gap, err := measure(ctx, symbol, intraday)
	if err != nil {
		return candleCoverage{}, err
	}
	var base *int
	if gap != nil {
		if snapped := snapGranularity(*gap); snapped > 0 {
			base = &snapped
		}
	}
	return candleCoverage{
		BaseGranularitySeconds: base,
		IntradayFrom:           intraday,
		HistoryFrom:            historyFrom,
		SupportedIntervals:     supportedCandleIntervals(base, intraday),
		Note:                   coverageNote(base, intraday),
	}, nil
}

func (h *Handler) candleCoverage(ctx context.Context, symbol string, now time.Time) (candleCoverage, error) {
	if cov, ok := cachedCoverage(symbol, now); ok {
		return cov, nil
	}
	days, historyFrom, err := h.dayTickCounts(ctx, symbol)
	if err != nil {
		return candleCoverage{}, err
	}
	cov, err := buildCoverage(ctx, symbol, days, historyFrom, now, h.medianTickGap)
	if err != nil {
		return candleCoverage{}, err
	}
	storeCoverage(symbol, cov, now)
	return cov, nil
}

// --- buckets -----------------------------------------------------------------

type candleBar struct {
	date                   time.Time
	open, high, low, close float64
	ticks                  int
}

// bucketExpr is the SQL that floors observed_at onto this interval.
//
// date_trunc is deliberately NOT used for sub-day buckets: it only supports
// hour and day, and arbitrary timeframes are the whole point. Epoch flooring
// is also what the Python trend-alignment engine resamples onto, so 4h here
// and 4h there are the same instants.
func (iv candleInterval) bucketExpr() string {
	if iv.Weekly {
		// The only expression here whose answer depends on the session time
		// zone — date_trunc on a timestamptz truncates in it. The deployed
		// container runs UTC, the same setting the Python engine relies on.
		return "date_trunc('week', observed_at)"
	}
	// %d is an interval constant from candleIntervals, never client input.
	// The ::float8 is explicit because extract() yields numeric on PG14+ and
	// to_timestamp takes double precision; every boundary is well under 2^53,
	// so the conversion is exact.
	return fmt.Sprintf("to_timestamp((floor(extract(epoch from observed_at) / %d) * %d)::float8)",
		iv.Seconds, iv.Seconds)
}

// candleBuckets aggregates ticks into the newest `limit` buckets strictly
// before `upper`, returned oldest first.
//
// open/close use the same ordering keys as _load_candles in
// prediction-python/app/jobs/trend_alignment.py — (observed_at, id) — so two
// sources quoting the same instant resolve identically on both sides.
func (h *Handler) candleBuckets(ctx context.Context, symbol string, iv candleInterval,
	lower *time.Time, upper time.Time, limit int) ([]candleBar, error) {
	sql := `
		SELECT bucket, open, high, low, close, ticks FROM (
			SELECT ` + iv.bucketExpr() + ` AS bucket,
			       (array_agg(value ORDER BY observed_at ASC, id ASC))[1]::float8  AS open,
			       max(value)::float8                                             AS high,
			       min(value)::float8                                             AS low,
			       (array_agg(value ORDER BY observed_at DESC, id DESC))[1]::float8 AS close,
			       count(*)::int                                                  AS ticks
			FROM prices
			WHERE symbol=$1 AND quality='ok' AND observed_at < $2
			      AND ($4::timestamptz IS NULL OR observed_at >= $4)
			GROUP BY bucket ORDER BY bucket DESC LIMIT $3
		) recent ORDER BY bucket ASC`
	rows, err := h.Pool.Query(ctx, sql, symbol, upper, limit, lower)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []candleBar
	for rows.Next() {
		var b candleBar
		if err := rows.Scan(&b.date, &b.open, &b.high, &b.low, &b.close, &b.ticks); err != nil {
			return nil, err
		}
		b.date = b.date.UTC()
		out = append(out, b)
	}
	return out, rows.Err()
}

// ticksExistBefore answers "is there older history" exactly, without
// aggregating it. A tick strictly before a bucket boundary always belongs to a
// strictly earlier bucket, so this is the same question as "are there older
// buckets" — and it is an index lookup instead of a second full aggregation.
// `lower` is the caller's explicit ?from= window, if any: with one set,
// "older" can only mean older *inside the window the client asked for*.
func (h *Handler) ticksExistBefore(ctx context.Context, symbol string,
	lower *time.Time, cutoff time.Time) (bool, error) {
	var exists bool
	err := h.Pool.QueryRow(ctx, `
		SELECT EXISTS(
			SELECT 1 FROM prices
			WHERE symbol=$1 AND quality='ok' AND observed_at < $2
			      AND ($3::timestamptz IS NULL OR observed_at >= $3))`,
		symbol, cutoff, lower).Scan(&exists)
	return exists, err
}

// --- assembly ----------------------------------------------------------------

type candle struct {
	T         int64     `json:"t"` // unix seconds (bucket start, UTC)
	OpenTime  time.Time `json:"open_time"`
	CloseTime time.Time `json:"close_time"`
	Open      float64   `json:"open"`
	High      float64   `json:"high"`
	Low       float64   `json:"low"`
	Close     float64   `json:"close"`
	// Volume is always nil: `prices` has no volume column, and inventing one
	// would be indistinguishable from a measurement.
	Volume *float64 `json:"volume"`
	Ticks  int      `json:"ticks"`
	// Confirmed is false for the bucket that is still forming.
	Confirmed bool `json:"confirmed"`
	// Synthetic marks a bucket built from at most one tick: its high and low
	// are that single price, not an observed range.
	Synthetic bool `json:"synthetic"`
}

// syntheticBucket reports whether a bucket's range is real. One tick (or zero)
// cannot express a high and a low, so the four prices are one number repeated.
func syntheticBucket(ticks int) bool { return ticks <= 1 }

// buildCandles turns aggregated buckets into wire candles. Pure function
// (unit tested): `now` decides only what is confirmed.
func buildCandles(bars []candleBar, iv candleInterval, now time.Time) []candle {
	out := make([]candle, 0, len(bars))
	for _, b := range bars {
		start := b.date.UTC()
		out = append(out, candle{
			T:         start.Unix(),
			OpenTime:  start,
			CloseTime: iv.BucketEnd(start),
			Open:      b.open, High: b.high, Low: b.low, Close: b.close,
			Volume:    nil,
			Ticks:     b.ticks,
			Confirmed: iv.Confirmed(start, now),
			Synthetic: syntheticBucket(b.ticks),
		})
	}
	return out
}

// candlePage is the pagination decision: which of the fetched buckets are
// returned (the rest are overlay warm-up), and what the client sends back to
// get the page before this one.
type candlePage struct {
	Start      int
	HasMore    bool
	NextBefore *time.Time
}

// paginateCandles returns the newest `limit` of the fetched buckets.
// `olderExists` covers the case where the fetch itself hit the end of what was
// asked for: buckets trimmed off the front prove older history exists, but an
// untrimmed page proves nothing and the caller must ask the database.
// Pure function (unit tested).
func paginateCandles(bars []candleBar, limit int, olderExists bool) candlePage {
	start := len(bars) - limit
	if start < 0 {
		start = 0
	}
	p := candlePage{Start: start}
	if len(bars) == 0 {
		return p
	}
	oldest := bars[start].date.UTC()
	p.NextBefore = &oldest
	p.HasMore = start > 0 || olderExists
	return p
}

func fpSlice(vals []float64) []*float64 {
	out := make([]*float64, len(vals))
	for i, v := range vals {
		out[i] = fp(v)
	}
	return out
}

// candleOverlays computes the chart overlays over `bars` (warm-up included)
// and returns them sliced from `start`, index-aligned with the candles.
func candleOverlays(bars []candleBar, start int) map[string]any {
	n := len(bars)
	closes := make([]float64, n)
	highs := make([]float64, n)
	lows := make([]float64, n)
	for i, b := range bars {
		closes[i], highs[i], lows[i] = b.close, b.high, b.low
	}
	bbU, bbM, bbL := indicators.Bollinger(closes, 20, 2)
	stLine, stDir := indicators.SuperTrend(highs, lows, closes, 10, 3)
	tenkan, kijun, senkouA, senkouB := indicators.Ichimoku(highs, lows)
	window := func(vals []float64) []*float64 { return fpSlice(vals[start:]) }
	return map[string]any{
		"sma_20":            window(indicators.SMA(closes, 20)),
		"sma_50":            window(indicators.SMA(closes, 50)),
		"bollinger_upper":   window(bbU),
		"bollinger_mid":     window(bbM),
		"bollinger_lower":   window(bbL),
		"supertrend":        window(stLine),
		"supertrend_dir":    stDir[start:],
		"psar":              window(indicators.ParabolicSAR(highs, lows, 0.02, 0.02, 0.2)),
		"ichimoku_tenkan":   window(tenkan),
		"ichimoku_kijun":    window(kijun),
		"ichimoku_senkou_a": window(senkouA),
		"ichimoku_senkou_b": window(senkouB),
	}
}

// lastConfirmed is the index of the newest finished candle in the page, or -1.
// Classic pivots are defined on a COMPLETED bar; deriving them from the
// forming one would produce levels that move all session.
func lastConfirmed(candles []candle) int {
	for i := len(candles) - 1; i >= 0; i-- {
		if candles[i].Confirmed {
			return i
		}
	}
	return -1
}

// candlePayload is everything in the response that is derived from the fetched
// buckets: the visible page, and what is drawn on it.
type candlePayload struct {
	Candles []candle
	// Overlays is nil when ?overlays=0, and serializes as null.
	Overlays map[string]any
	// Pivots is nil when no fetched bucket has finished.
	Pivots              *indicators.PivotPoints
	Support, Resistance *float64
}

// buildCandlePayload cuts the page out of the fetched buckets and computes the
// overlays and levels drawn on it. `start` is the first VISIBLE bucket;
// everything before it is warm-up. Pure function (unit tested).
//
// Candles and overlay series are page-shaped by definition — they are per-bar,
// index-aligned with the candles. The single LEVELS are not: pivots and the
// support/resistance band are computed from the full fetched array, warm-up
// included, so that they do not narrow with ?limit=. A caller asking for three
// bars is asking for three bars, not for a support level measured over three
// bars — and it cannot tell the two apart in the response.
func buildCandlePayload(bars []candleBar, start int, iv candleInterval,
	now time.Time, overlays bool) candlePayload {
	all := buildCandles(bars, iv, now)
	p := candlePayload{Candles: all[start:]}
	if overlays {
		p.Overlays = candleOverlays(bars, start)
	}
	// The newest COMPLETED bucket of the fetch. It is normally inside the page
	// as well — only the forming bucket is unconfirmed — but a page of one
	// forming bar must not report the levels as missing when a finished bucket
	// was fetched right behind it.
	if i := lastConfirmed(all); i >= 0 {
		pivots := indicators.Pivots(all[i].High, all[i].Low, all[i].Close)
		p.Pivots = &pivots
	}
	// Refused rather than approximated: fewer buckets than the lookback asks
	// for is not a narrower band, it is a different measurement.
	if len(bars) >= supportResistanceLookback {
		closes := make([]float64, len(bars))
		for i, b := range bars {
			closes[i] = b.close
		}
		support, resistance := indicators.SupportResistance(closes, supportResistanceLookback)
		p.Support, p.Resistance = fp(support), fp(resistance)
	}
	return p
}

// candleResponseKeys is the published body of GET /api/v1/market/candles. It
// exists so a key cannot quietly disappear from the contract: the frontend
// reads every one of these.
var candleResponseKeys = []string{
	"symbol", "interval", "interval_seconds", "timezone", "candles",
	"has_more", "next_before", "coverage", "effective_window", "overlays",
	"pivots", "support", "resistance", "as_of",
}

// candleResponse is the wire body. Pure function (unit tested): every value in
// it has already been decided by the time it is called.
func candleResponse(q candleQuery, iv candleInterval, cov candleCoverage,
	win candleWindow, page candlePage, payload candlePayload, now time.Time) map[string]any {
	return map[string]any{
		"symbol":           q.Symbol,
		"interval":         iv.Name,
		"interval_seconds": iv.Seconds,
		"timezone":         "UTC",
		"candles":          payload.Candles,
		"has_more":         page.HasMore,
		"next_before":      page.NextBefore,
		"coverage":         cov,
		// The window actually served, after snapping the requested one outward
		// onto bucket boundaries: `from` is null when none was asked for, `to`
		// is exclusive. Without this a caller cannot tell that its
		// ?to=23:59:59 was served as the whole day — or, on the newest page,
		// that `to` is simply `now`.
		"effective_window": map[string]any{"from": win.From, "to": win.To},
		"overlays":         payload.Overlays,
		// pivots/support/resistance are always present, null when the fetched
		// buckets cannot produce them, so a client never has to distinguish
		// "absent" from "not computable".
		"pivots":     payload.Pivots,
		"support":    payload.Support,
		"resistance": payload.Resistance,
		"as_of":      now,
	}
}

// --- handler -----------------------------------------------------------------

// Candles implements GET /api/v1/market/candles.
//
// Query: symbol (default IR_GOLD_18K), interval (default 1d, aliases daily and
// hourly), limit (default 500, 1..2000), before (pagination cursor, RFC3339 or
// unix seconds, exclusive on bucket start), from/to (explicit window),
// overlays (default 1; 0 for cheap history pages), days (legacy).
//
// Returns the newest `limit` buckets at or before the cursor, oldest first, so
// the client appends pages backwards with ?before=next_before. A from/to window
// is snapped outward onto bucket boundaries and echoed as `effective_window`;
// every bucket returned is whole (see the file header).
func (h *Handler) Candles(w http.ResponseWriter, r *http.Request) {
	q, perr := parseCandleQuery(r.URL.Query())
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	ctx := r.Context()
	now := time.Now().UTC()
	iv := q.Interval

	cov, err := h.candleCoverage(ctx, q.Symbol, now)
	if err != nil {
		h.Log.Error("candles_coverage", "error", err, "symbol", q.Symbol)
		httpserver.Internal(w, "database error")
		return
	}
	if !slices.Contains(cov.SupportedIntervals, iv.Name) {
		httpserver.BadRequest(w, unsupportedIntervalMessage, map[string]any{
			"symbol":              q.Symbol,
			"interval":            iv.Name,
			"supported_intervals": cov.SupportedIntervals,
		})
		return
	}

	// The window actually served: snapped outward onto bucket boundaries, so
	// the same bounds can gate bucket selection AND tick aggregation without
	// any bucket being built from part of itself.
	win := snapCandleWindow(q, iv, now)

	fetch := q.Limit
	if q.Overlays {
		fetch += candleWarmupBuckets
	}

	// Bounded scan first (see candleScanSlack). With no ?from= the floor is
	// synthetic, and it is floored onto a boundary too: an unaligned scan floor
	// would slice the oldest bucket it admits exactly the way an unfloored
	// ?from= did.
	lower := win.From
	if lower == nil {
		floor := iv.BucketStart(win.To.Add(-time.Duration(iv.Seconds*int64(fetch)*candleScanSlack) * time.Second))
		lower = &floor
	}
	bars, err := h.candleBuckets(ctx, q.Symbol, iv, lower, win.To, fetch)
	if err != nil {
		h.Log.Error("candles_query", "error", err, "symbol", q.Symbol, "interval", iv.Name)
		httpserver.Internal(w, "database error")
		return
	}
	if len(bars) < fetch && win.From == nil {
		// A short page from the bounded scan is ambiguous: this series was one
		// tick per day for four years, so a sparse stretch looks exactly like
		// the end of the data. Confirm against the index and re-run unbounded
		// when there really is more.
		older, existsErr := h.ticksExistBefore(ctx, q.Symbol, nil, *lower)
		if existsErr != nil {
			h.Log.Error("candles_older_probe", "error", existsErr, "symbol", q.Symbol)
			httpserver.Internal(w, "database error")
			return
		}
		if older {
			bars, err = h.candleBuckets(ctx, q.Symbol, iv, nil, win.To, fetch)
			if err != nil {
				h.Log.Error("candles_query_unbounded", "error", err, "symbol", q.Symbol)
				httpserver.Internal(w, "database error")
				return
			}
		}
	}

	page := paginateCandles(bars, q.Limit, false)
	if page.NextBefore != nil && !page.HasMore {
		// Inside a ?from= window, "older" means older within the SNAPPED
		// window: ticks between the caller's `from` and the boundary it floored
		// to are part of the oldest bucket already returned, not older history.
		older, existsErr := h.ticksExistBefore(ctx, q.Symbol, win.From, *page.NextBefore)
		if existsErr != nil {
			h.Log.Error("candles_has_more", "error", existsErr, "symbol", q.Symbol)
			httpserver.Internal(w, "database error")
			return
		}
		page.HasMore = older
	}
	payload := buildCandlePayload(bars, page.Start, iv, now, q.Overlays)
	httpserver.JSON(w, http.StatusOK, candleResponse(q, iv, cov, win, page, payload, now))
}
