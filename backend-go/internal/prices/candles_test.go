package prices

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// tehran is the display timezone. It exists in these tests for one purpose:
// proving that a +03:30 input still floors onto a UTC bucket boundary. Tehran
// time never reaches storage or bucketing.
var tehran = time.FixedZone("Asia/Tehran", 3*3600+1800)

func mustInterval(t *testing.T, name string) candleInterval {
	t.Helper()
	iv, err := ParseCandleInterval(name)
	if err != nil {
		t.Fatalf("interval %q: %v", name, err)
	}
	return iv
}

func TestParseCandleInterval(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		want    string
		seconds int64
		wantErr bool
	}{
		{"absent uses the daily default", "", "1d", 86400, false},
		{"finest supported", "5m", "5m", 300, false},
		{"ten minutes", "10m", "10m", 600, false},
		{"quarter hour", "15m", "15m", 900, false},
		{"twenty minutes", "20m", "20m", 1200, false},
		{"half hour", "30m", "30m", 1800, false},
		{"three quarter hour", "45m", "45m", 2700, false},
		{"one hour", "1h", "1h", 3600, false},
		{"two hours", "2h", "2h", 7200, false},
		{"three hours", "3h", "3h", 10800, false},
		{"four hours", "4h", "4h", 14400, false},
		{"six hours", "6h", "6h", 21600, false},
		{"eight hours", "8h", "8h", 28800, false},
		{"twelve hours", "12h", "12h", 43200, false},
		{"one day", "1d", "1d", 86400, false},
		{"two days", "2d", "2d", 172800, false},
		{"three days", "3d", "3d", 259200, false},
		{"one week", "1w", "1w", 604800, false},
		// The vocabulary the endpoint shipped with. A caller written against
		// the old contract must keep working unchanged.
		{"legacy daily alias", "daily", "1d", 86400, false},
		{"legacy hourly alias", "hourly", "1h", 3600, false},
		// Plausible-looking timeframes that are NOT in the canonical list.
		// Accepting them would mean inventing a bucketing nobody published.
		{"one minute rejected", "1m", "", 0, true},
		{"one month rejected", "1M", "", 0, true},
		{"uppercase rejected", "1D", "", 0, true},
		{"bare number rejected", "60", "", 0, true},
		{"padded rejected", " 1h", "", 0, true},
		{"nonsense rejected", "weekly", "", 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseCandleInterval(tc.raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("interval %q accepted as %q", tc.raw, got.Name)
				}
				return
			}
			if err != nil {
				t.Fatalf("interval %q rejected: %v", tc.raw, err)
			}
			if got.Name != tc.want || got.Seconds != tc.seconds {
				t.Fatalf("interval %q: got %s/%ds, want %s/%ds",
					tc.raw, got.Name, got.Seconds, tc.want, tc.seconds)
			}
		})
	}
}

func TestCandleIntervalsAreAscendingAndUnique(t *testing.T) {
	seen := map[string]bool{}
	var prev int64
	for _, iv := range candleIntervals {
		if seen[iv.Name] {
			t.Fatalf("duplicate interval %q", iv.Name)
		}
		seen[iv.Name] = true
		if iv.Seconds <= prev {
			t.Fatalf("interval %q (%ds) does not follow %ds", iv.Name, iv.Seconds, prev)
		}
		prev = iv.Seconds
	}
	// The aliases must resolve into the canonical set, not beside it.
	for alias, canonical := range candleIntervalAliases {
		if !seen[canonical] {
			t.Fatalf("alias %q points at unknown interval %q", alias, canonical)
		}
	}
}

func TestBucketStartFloorsOntoUTCBoundaries(t *testing.T) {
	// 2026-07-20T13:47:31Z — a Monday, chosen because that is the day the
	// 18k series switched from one tick a day to one every five minutes.
	base := time.Date(2026, 7, 20, 13, 47, 31, 0, time.UTC)
	cases := []struct {
		interval string
		want     time.Time
	}{
		{"5m", time.Date(2026, 7, 20, 13, 45, 0, 0, time.UTC)},
		{"10m", time.Date(2026, 7, 20, 13, 40, 0, 0, time.UTC)},
		{"15m", time.Date(2026, 7, 20, 13, 45, 0, 0, time.UTC)},
		{"20m", time.Date(2026, 7, 20, 13, 40, 0, 0, time.UTC)},
		{"30m", time.Date(2026, 7, 20, 13, 30, 0, 0, time.UTC)},
		// 2700 does not divide 3600: 45m buckets are epoch-aligned and drift
		// against the clock hour. That is what epoch flooring means, and the
		// database expression does exactly the same thing.
		{"45m", time.Date(2026, 7, 20, 13, 30, 0, 0, time.UTC)},
		{"1h", time.Date(2026, 7, 20, 13, 0, 0, 0, time.UTC)},
		{"2h", time.Date(2026, 7, 20, 12, 0, 0, 0, time.UTC)},
		{"3h", time.Date(2026, 7, 20, 12, 0, 0, 0, time.UTC)},
		{"4h", time.Date(2026, 7, 20, 12, 0, 0, 0, time.UTC)},
		{"6h", time.Date(2026, 7, 20, 12, 0, 0, 0, time.UTC)},
		{"8h", time.Date(2026, 7, 20, 8, 0, 0, 0, time.UTC)},
		{"12h", time.Date(2026, 7, 20, 12, 0, 0, 0, time.UTC)},
		{"1d", time.Date(2026, 7, 20, 0, 0, 0, 0, time.UTC)},
		// 2d/3d buckets are epoch-aligned, not calendar-aligned: unix 0 was
		// 1970-01-01, so the parity of the boundary is fixed by the epoch.
		{"2d", time.Date(2026, 7, 20, 0, 0, 0, 0, time.UTC)},
		{"3d", time.Date(2026, 7, 18, 0, 0, 0, 0, time.UTC)},
		// Weekly is the one date_trunc: Monday 00:00 UTC, the chart convention.
		{"1w", time.Date(2026, 7, 20, 0, 0, 0, 0, time.UTC)},
	}
	for _, tc := range cases {
		t.Run(tc.interval, func(t *testing.T) {
			iv := mustInterval(t, tc.interval)
			got := iv.BucketStart(base)
			if !got.Equal(tc.want) {
				t.Fatalf("%s: got %s, want %s", tc.interval,
					got.Format(time.RFC3339), tc.want.Format(time.RFC3339))
			}
			if got.Location() != time.UTC {
				t.Fatalf("%s: bucket start is not UTC: %v", tc.interval, got.Location())
			}
			// Display timezone must not move a boundary: the same INSTANT
			// expressed at Tehran's +03:30 offset floors identically.
			if tz := iv.BucketStart(base.In(tehran)); !tz.Equal(tc.want) {
				t.Fatalf("%s from Tehran: got %s, want %s", tc.interval,
					tz.Format(time.RFC3339), tc.want.Format(time.RFC3339))
			}
		})
	}
}

func TestBucketStartIsIdempotentAndAligned(t *testing.T) {
	// A bucket start floors to itself, and every boundary is a whole multiple
	// of the interval since the epoch — the property the SQL relies on when it
	// treats "tick before the cursor" as "bucket before the cursor".
	base := time.Date(2026, 8, 12, 7, 13, 9, 0, time.UTC)
	for _, iv := range candleIntervals {
		start := iv.BucketStart(base)
		if again := iv.BucketStart(start); !again.Equal(start) {
			t.Fatalf("%s: flooring a boundary moved it: %s -> %s",
				iv.Name, start.Format(time.RFC3339), again.Format(time.RFC3339))
		}
		if !iv.Weekly && start.Unix()%iv.Seconds != 0 {
			t.Fatalf("%s: boundary %s is not epoch-aligned", iv.Name, start.Format(time.RFC3339))
		}
		if iv.Weekly && start.Weekday() != time.Monday {
			t.Fatalf("1w boundary %s is a %s, want Monday",
				start.Format(time.RFC3339), start.Weekday())
		}
		if start.After(base) {
			t.Fatalf("%s: floor %s is after the input", iv.Name, start.Format(time.RFC3339))
		}
	}
}

func TestFloorEpochBeforeUnixZero(t *testing.T) {
	// Go truncates integer division toward zero; SQL's floor() does not. No
	// stored price predates 1970, but a bucket boundary must not depend on it.
	iv := mustInterval(t, "1h")
	got := iv.BucketStart(time.Date(1969, 12, 31, 23, 30, 0, 0, time.UTC))
	want := time.Date(1969, 12, 31, 23, 0, 0, 0, time.UTC)
	if !got.Equal(want) {
		t.Fatalf("got %s, want %s", got.Format(time.RFC3339), want.Format(time.RFC3339))
	}
}

func TestConfirmedAtExactBoundary(t *testing.T) {
	iv := mustInterval(t, "1h")
	start := time.Date(2026, 8, 12, 9, 0, 0, 0, time.UTC)
	cases := []struct {
		name string
		now  time.Time
		want bool
	}{
		{"one second into the bucket", start.Add(time.Second), false},
		{"one second before the close", start.Add(time.Hour - time.Second), false},
		{"exactly at the close is confirmed", start.Add(time.Hour), true},
		{"one second after the close", start.Add(time.Hour + time.Second), true},
		{"a day later", start.Add(24 * time.Hour), true},
		// The clock landing before the bucket cannot confirm it. This is the
		// skewed-provider case the tick upper bound also guards against.
		{"before the bucket even opened", start.Add(-time.Second), false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := iv.Confirmed(start, tc.now); got != tc.want {
				t.Fatalf("confirmed(%s, now=%s) = %v, want %v",
					start.Format(time.RFC3339), tc.now.Format(time.RFC3339), got, tc.want)
			}
		})
	}
	if end := iv.BucketEnd(start); !end.Equal(start.Add(time.Hour)) {
		t.Fatalf("bucket end = %s", end.Format(time.RFC3339))
	}
}

func TestSyntheticFlagging(t *testing.T) {
	// The honesty flag. A bucket built from one tick has no observed range;
	// its high and low are that single price repeated.
	cases := []struct {
		name  string
		ticks int
		want  bool
	}{
		{"no ticks", 0, true},
		{"one tick has no range", 1, true},
		{"two ticks can express a range", 2, false},
		{"a dense five-minute bucket", 290, false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := syntheticBucket(tc.ticks); got != tc.want {
				t.Fatalf("ticks=%d: got %v, want %v", tc.ticks, got, tc.want)
			}
		})
	}
}

func TestBuildCandlesShape(t *testing.T) {
	iv := mustInterval(t, "1h")
	start := time.Date(2026, 8, 12, 9, 0, 0, 0, time.UTC)
	now := start.Add(90 * time.Minute) // mid-way through the second bucket
	bars := []candleBar{
		// A single-tick bucket: flat, and therefore synthetic.
		{date: start, open: 100, high: 100, low: 100, close: 100, ticks: 1},
		// The forming bucket, with a real intraday range.
		{date: start.Add(time.Hour), open: 100, high: 110, low: 95, close: 105, ticks: 12},
	}
	got := buildCandles(bars, iv, now)
	if len(got) != 2 {
		t.Fatalf("got %d candles", len(got))
	}
	first, second := got[0], got[1]
	if first.T != start.Unix() || !first.OpenTime.Equal(start) {
		t.Fatalf("first bucket start: t=%d open_time=%s", first.T, first.OpenTime.Format(time.RFC3339))
	}
	if !first.CloseTime.Equal(start.Add(time.Hour)) {
		t.Fatalf("close_time = %s", first.CloseTime.Format(time.RFC3339))
	}
	if !first.Confirmed || !first.Synthetic {
		t.Fatalf("finished single-tick bucket: confirmed=%v synthetic=%v", first.Confirmed, first.Synthetic)
	}
	if second.Confirmed || second.Synthetic {
		t.Fatalf("forming 12-tick bucket: confirmed=%v synthetic=%v", second.Confirmed, second.Synthetic)
	}
	// There is no volume data anywhere in `prices`; it must serialize as null.
	for i, c := range got {
		if c.Volume != nil {
			t.Fatalf("candle %d carries a volume: %v", i, *c.Volume)
		}
	}
	raw, err := json.Marshal(got[0])
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var wire map[string]any
	if err := json.Unmarshal(raw, &wire); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, key := range []string{"t", "open_time", "close_time", "open", "high", "low",
		"close", "volume", "ticks", "confirmed", "synthetic"} {
		if _, ok := wire[key]; !ok {
			t.Fatalf("candle is missing %q: %s", key, raw)
		}
	}
	if wire["volume"] != nil {
		t.Fatalf("volume serialized as %v, want null", wire["volume"])
	}
}

func TestLastConfirmedPicksTheNewestFinishedBar(t *testing.T) {
	candles := []candle{
		{Confirmed: true, Close: 1},
		{Confirmed: true, Close: 2},
		{Confirmed: false, Close: 3},
	}
	if got := lastConfirmed(candles); got != 1 {
		t.Fatalf("got index %d, want 1", got)
	}
	if got := lastConfirmed([]candle{{Confirmed: false}}); got != -1 {
		t.Fatalf("a page of one forming bar has no completed bar, got %d", got)
	}
	if got := lastConfirmed(nil); got != -1 {
		t.Fatalf("empty page, got %d", got)
	}
}

func TestSupportedCandleIntervals(t *testing.T) {
	intraday := time.Date(2026, 7, 20, 0, 0, 0, 0, time.UTC)
	fiveMin, oneHour, oneDay, oneWeek := 300, 3600, 86400, 604800
	all := candleIntervalNames()
	dailyAndCoarser := []string{"1d", "2d", "3d", "1w"}
	cases := []struct {
		name     string
		base     *int
		intraday *time.Time
		want     []string
	}{
		{
			// Production today: 5-minute ticks, dense since 2026-07-20.
			name: "five minute base with intraday density", base: &fiveMin,
			intraday: &intraday, want: all,
		},
		{
			// The seeded era, and any symbol still in it: one tick a day
			// cannot produce an intraday candle, so none are offered.
			name: "daily base, no intraday density", base: &oneDay, intraday: nil,
			want: dailyAndCoarser,
		},
		{
			// The case this table was missing, and the whole of F1. A quiet
			// stretch measures a multi-day median gap, which snaps to 604800 —
			// a base COARSER than a daily candle. Daily and above stay
			// available: four years of one-tick-a-day history produce them
			// perfectly well, and 1d is the endpoint's own default, so gating
			// it on the recent cadence made a parameterless request 400.
			name: "weekly base still offers the daily default", base: &oneWeek, intraday: nil,
			want: dailyAndCoarser,
		},
		{
			// Same coarse base with density present: it gates the sub-day
			// vocabulary it describes, and stops there.
			name: "weekly base drops only sub-day intervals", base: &oneWeek, intraday: &intraday,
			want: dailyAndCoarser,
		},
		{
			// The inversion that proved the old basis wrong: an unmeasurable
			// base (fewer than two ticks in the window) offered every daily
			// interval, so a symbol with NO recent data was served while one
			// with three sparse ticks was refused. Both rows must now agree.
			name: "daily base with density behaves like an unmeasurable one",
			base: &oneDay, intraday: &intraday, want: dailyAndCoarser,
		},
		{
			// Density gates sub-day intervals even when the ticks themselves
			// are fine-grained: a dense fortnight that ended is not coverage.
			name: "fine ticks but density does not reach now", base: &fiveMin, intraday: nil,
			want: dailyAndCoarser,
		},
		{
			// Nothing finer than the ticks: hourly collection cannot make a
			// 5m/10m/.../45m candle, whatever the density flag says.
			name: "hourly base drops everything finer", base: &oneHour, intraday: &intraday,
			want: []string{"1h", "2h", "3h", "4h", "6h", "8h", "12h", "1d", "2d", "3d", "1w"},
		},
		{
			// A symbol with no recent ticks at all: granularity unmeasurable,
			// so only the density rule applies.
			name: "unmeasurable base with density", base: nil, intraday: &intraday, want: all,
		},
		{
			name: "unmeasurable base without density", base: nil, intraday: nil,
			want: dailyAndCoarser,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := supportedCandleIntervals(tc.base, tc.intraday)
			if len(got) != len(tc.want) {
				t.Fatalf("got %v, want %v", got, tc.want)
			}
			for i := range got {
				if got[i] != tc.want[i] {
					t.Fatalf("got %v, want %v", got, tc.want)
				}
			}
		})
	}
}

// fakeGapMeasurer stands in for the database. It records the window
// buildCoverage asked for — the basis of the base-granularity measurement is
// the thing under test — and answers with a fixed median.
type fakeGapMeasurer struct {
	gap   *float64
	calls int
	since *time.Time
}

func (f *fakeGapMeasurer) measure(_ context.Context, _ string, since *time.Time) (*float64, error) {
	f.calls++
	f.since = since
	return f.gap, nil
}

func TestBuildCoverageDoesNotLetAQuietWeekRevokeDailyCandles(t *testing.T) {
	// Regression for F1. `base_granularity_seconds` describes the intraday
	// feed; it used to gate the WHOLE interval vocabulary, and it used to be
	// measured over a trailing week that a single provider outage can own.
	now := time.Date(2026, 8, 12, 6, 30, 0, 0, time.UTC)
	day := func(y, m, d int) time.Time {
		return time.Date(y, time.Month(m), d, 0, 0, 0, 0, time.UTC)
	}
	historyFrom := day(2022, 4, 20)

	// The measured production shape: one tick a day from 2022-04-20, then
	// five-minute collection from 2026-07-20 onward.
	var denseDays []dayTickCount
	for d := 20; d <= 31; d++ {
		denseDays = append(denseDays, dayTickCount{Day: day(2026, 7, d), Ticks: 288})
	}
	for d := 1; d <= 12; d++ {
		denseDays = append(denseDays, dayTickCount{Day: day(2026, 8, d), Ticks: 288})
	}
	// The same symbol after a three-day outage: a handful of sparse days.
	var sparseDays []dayTickCount
	for d := 5; d <= 12; d++ {
		sparseDays = append(sparseDays, dayTickCount{Day: day(2026, 8, d), Ticks: 1})
	}

	threeAndAHalfDays, fiveMinutes := 302400.0, 300.0
	cases := []struct {
		name          string
		days          []dayTickCount
		gap           *float64
		wantSince     *time.Time // the window the measurement must be taken over
		wantBase      *int
		wantIntervals []string
	}{
		{
			// The reproduced defect: three sparse days put the median gap at
			// 3.5 days, which snaps to 604800, which dropped every interval
			// shorter than a week — including 1d, the endpoint's own default.
			// GET /market/candles with NO parameters then answered 400, pinned
			// for coverageTTL by the coverage cache.
			name: "a sparse week cannot revoke the daily default",
			days: sparseDays, gap: &threeAndAHalfDays,
			wantSince: nil, wantBase: ptrInt(604800),
			wantIntervals: []string{"1d", "2d", "3d", "1w"},
		},
		{
			// The basis. The dense era begins 2026-07-20, so that is the window
			// the median must be read over — not now-7d, which one outage can
			// consume entirely, leaving a "base granularity" that describes the
			// outage instead of the collector.
			name: "the gap is measured over the dense period, not a trailing week",
			days: denseDays, gap: &fiveMinutes,
			wantSince: ptrTime(day(2026, 7, 20)), wantBase: ptrInt(300),
			wantIntervals: candleIntervalNames(),
		},
		{
			// No dense period at all: nothing to characterize but the whole
			// history, and base granularity gates nothing either way because
			// every sub-day interval is already refused for want of density.
			name: "no dense period measures all history",
			days: sparseDays, gap: nil,
			wantSince: nil, wantBase: nil,
			wantIntervals: []string{"1d", "2d", "3d", "1w"},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fake := &fakeGapMeasurer{gap: tc.gap}
			cov, err := buildCoverage(context.Background(), "IR_GOLD_18K", tc.days,
				&historyFrom, now, fake.measure)
			if err != nil {
				t.Fatalf("buildCoverage: %v", err)
			}
			if fake.calls != 1 {
				t.Fatalf("gap measured %d times, want exactly 1", fake.calls)
			}
			switch {
			case tc.wantSince == nil && fake.since != nil:
				t.Fatalf("gap measured from %s, want all history",
					fake.since.Format(time.RFC3339))
			case tc.wantSince != nil && fake.since == nil:
				t.Fatalf("gap measured over all history, want from %s",
					tc.wantSince.Format(time.RFC3339))
			case tc.wantSince != nil && !fake.since.Equal(*tc.wantSince):
				t.Fatalf("gap measured from %s, want %s",
					fake.since.Format(time.RFC3339), tc.wantSince.Format(time.RFC3339))
			}
			switch {
			case tc.wantBase == nil && cov.BaseGranularitySeconds != nil:
				t.Fatalf("base = %ds, want null", *cov.BaseGranularitySeconds)
			case tc.wantBase != nil && cov.BaseGranularitySeconds == nil:
				t.Fatalf("base = null, want %ds", *tc.wantBase)
			case tc.wantBase != nil && *cov.BaseGranularitySeconds != *tc.wantBase:
				t.Fatalf("base = %ds, want %ds", *cov.BaseGranularitySeconds, *tc.wantBase)
			}
			if strings.Join(cov.SupportedIntervals, ",") != strings.Join(tc.wantIntervals, ",") {
				t.Fatalf("supported = %v, want %v", cov.SupportedIntervals, tc.wantIntervals)
			}
			// The parameterless request must always be answerable.
			if !slices.Contains(cov.SupportedIntervals, defaultCandleInterval) {
				t.Fatalf("the default interval %q is not offered: %v",
					defaultCandleInterval, cov.SupportedIntervals)
			}
		})
	}
}

func TestComputeIntradayFrom(t *testing.T) {
	day := func(d int) time.Time { return time.Date(2026, 8, d, 0, 0, 0, 0, time.UTC) }
	now := time.Date(2026, 8, 12, 6, 30, 0, 0, time.UTC)
	dense := func(d int) dayTickCount { return dayTickCount{Day: day(d), Ticks: 288} }
	sparse := func(d int) dayTickCount { return dayTickCount{Day: day(d), Ticks: 1} }

	cases := []struct {
		name string
		days []dayTickCount
		want *time.Time
	}{
		{"no history at all", nil, nil},
		{
			"the whole series is one tick a day",
			[]dayTickCount{sparse(9), sparse(10), sparse(11), sparse(12)},
			nil,
		},
		{
			// The measured shape: years of one-per-day, then a switch to
			// five-minute collection that is still running.
			"density starts mid-history",
			[]dayTickCount{sparse(7), sparse(8), dense(9), dense(10), dense(11), dense(12)},
			ptrTime(day(9)),
		},
		{
			// The current UTC day is still filling. At 06:30 it holds 78 of
			// its eventual 288 ticks — but at 00:05 it holds one, and that
			// must not delete every intraday timeframe for the first hour.
			"today is still filling",
			[]dayTickCount{dense(9), dense(10), dense(11), {Day: day(12), Ticks: 2}},
			ptrTime(day(9)),
		},
		{
			// A gap breaks the run: only the unbroken tail counts.
			"a missing day breaks the run",
			[]dayTickCount{dense(6), dense(7), dense(9), dense(10), dense(11), dense(12)},
			ptrTime(day(9)),
		},
		{
			// A sparse day inside the run breaks it just as a missing one does.
			"a sparse day breaks the run",
			[]dayTickCount{dense(8), sparse(9), dense(10), dense(11), dense(12)},
			ptrTime(day(10)),
		},
		{
			// Collection stopped two days ago: yesterday is tolerated (the
			// UTC day may have only just turned over), older is not.
			"density that stopped yesterday still counts",
			[]dayTickCount{dense(9), dense(10), dense(11)},
			ptrTime(day(9)),
		},
		{
			"density that stopped two days ago does not",
			[]dayTickCount{dense(8), dense(9), dense(10)},
			nil,
		},
		{
			// A dense fortnight in the past does not make 15m candles
			// available today.
			"dense long ago, sparse since",
			[]dayTickCount{
				{Day: day(1), Ticks: 288}, {Day: day(2), Ticks: 288},
				sparse(10), sparse(11), sparse(12),
			},
			nil,
		},
		{
			"exactly at the density threshold",
			[]dayTickCount{{Day: day(11), Ticks: intradayMinTicksPerDay}, dense(12)},
			ptrTime(day(11)),
		},
		{
			"one tick below the threshold",
			[]dayTickCount{{Day: day(11), Ticks: intradayMinTicksPerDay - 1}, dense(12)},
			ptrTime(day(12)),
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := computeIntradayFrom(tc.days, now)
			switch {
			case tc.want == nil && got != nil:
				t.Fatalf("got %s, want nil", got.Format(time.RFC3339))
			case tc.want != nil && got == nil:
				t.Fatalf("got nil, want %s", tc.want.Format(time.RFC3339))
			case tc.want != nil && !got.Equal(*tc.want):
				t.Fatalf("got %s, want %s", got.Format(time.RFC3339), tc.want.Format(time.RFC3339))
			}
		})
	}
}

func ptrTime(t time.Time) *time.Time { return &t }

func ptrInt(n int) *int { return &n }

func TestSnapGranularity(t *testing.T) {
	cases := []struct {
		name    string
		measure float64
		want    int
	}{
		// The measured production values: p50 300.0s, p90 300.5s.
		{"exactly five minutes", 300.0, 300},
		{"five minutes with poll jitter", 300.5, 300},
		{"just under five minutes", 299.2, 300},
		{"one observation a day", 86400, 86400},
		{"a daily series with drift", 86_395, 86400},
		{"hourly collection", 3600, 3600},
		{"a minute", 60, 60},
		{"nothing measurable", 0, 0},
		{"negative is nothing measurable", -1, 0},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := snapGranularity(tc.measure); got != tc.want {
				t.Fatalf("snap(%v) = %d, want %d", tc.measure, got, tc.want)
			}
		})
	}
}

func TestPaginateCandles(t *testing.T) {
	base := time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	bars := func(n int) []candleBar {
		out := make([]candleBar, n)
		for i := range out {
			out[i] = candleBar{date: base.AddDate(0, 0, i), close: float64(i), ticks: 1}
		}
		return out
	}
	cases := []struct {
		name        string
		bars        []candleBar
		limit       int
		olderExists bool
		wantStart   int
		wantMore    bool
		wantBefore  *time.Time
	}{
		{
			// A full fetch: 60 warm-up buckets were trimmed off the front, and
			// their existence alone proves there is older history.
			name: "warm-up buckets prove there is more", bars: bars(560), limit: 500,
			wantStart: 60, wantMore: true, wantBefore: ptrTime(base.AddDate(0, 0, 60)),
		},
		{
			// Nothing was trimmed, so the fetch says nothing; the caller's
			// database probe decides.
			name: "untrimmed page, database says there is more", bars: bars(120), limit: 500,
			olderExists: true, wantStart: 0, wantMore: true, wantBefore: ptrTime(base),
		},
		{
			name: "untrimmed page at the start of history", bars: bars(120), limit: 500,
			olderExists: false, wantStart: 0, wantMore: false, wantBefore: ptrTime(base),
		},
		{
			name: "exactly the limit", bars: bars(500), limit: 500,
			wantStart: 0, wantMore: false, wantBefore: ptrTime(base),
		},
		{
			name: "one bucket over the limit", bars: bars(501), limit: 500,
			wantStart: 1, wantMore: true, wantBefore: ptrTime(base.AddDate(0, 0, 1)),
		},
		{
			name: "a single bucket", bars: bars(1), limit: 1,
			wantStart: 0, wantMore: false, wantBefore: ptrTime(base),
		},
		{
			// No candles means no cursor: a client must not be handed a
			// `before` that would page it into an empty region forever.
			name: "no buckets at all", bars: nil, limit: 500,
			olderExists: true, wantStart: 0, wantMore: false, wantBefore: nil,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := paginateCandles(tc.bars, tc.limit, tc.olderExists)
			if got.Start != tc.wantStart {
				t.Fatalf("start = %d, want %d", got.Start, tc.wantStart)
			}
			if got.HasMore != tc.wantMore {
				t.Fatalf("has_more = %v, want %v", got.HasMore, tc.wantMore)
			}
			switch {
			case tc.wantBefore == nil && got.NextBefore != nil:
				t.Fatalf("next_before = %s, want null", got.NextBefore.Format(time.RFC3339))
			case tc.wantBefore != nil && got.NextBefore == nil:
				t.Fatalf("next_before = null, want %s", tc.wantBefore.Format(time.RFC3339))
			case tc.wantBefore != nil && !got.NextBefore.Equal(*tc.wantBefore):
				t.Fatalf("next_before = %s, want %s",
					got.NextBefore.Format(time.RFC3339), tc.wantBefore.Format(time.RFC3339))
			}
			// The cursor must be the oldest bucket the client actually
			// received, or the page before this one would overlap or skip.
			if got.NextBefore != nil && !got.NextBefore.Equal(tc.bars[got.Start].date) {
				t.Fatalf("cursor %s is not the oldest returned bucket %s",
					got.NextBefore.Format(time.RFC3339), tc.bars[got.Start].date.Format(time.RFC3339))
			}
		})
	}
}

func TestPaginateCandlesCursorWalksHistoryWithoutGaps(t *testing.T) {
	// Paging backwards with ?before=next_before must visit every bucket
	// exactly once. Simulated over 23 buckets at a page size of 5.
	base := time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	iv := mustInterval(t, "1d")
	all := make([]candleBar, 23)
	for i := range all {
		all[i] = candleBar{date: base.AddDate(0, 0, i), close: float64(i), ticks: 2}
	}

	seen := map[int64]int{}
	cursor := (*time.Time)(nil)
	for pages := 0; ; pages++ {
		if pages > 10 {
			t.Fatal("pagination did not terminate")
		}
		// The server side of ?before=: buckets strictly before the floored
		// cursor, newest `limit` of them.
		var window []candleBar
		for _, b := range all {
			if cursor == nil || b.date.Before(iv.BucketStart(*cursor)) {
				window = append(window, b)
			}
		}
		page := paginateCandles(window, 5, false)
		for _, b := range window[page.Start:] {
			seen[b.date.Unix()]++
		}
		// Exactly the loop a client runs: keep asking while has_more.
		if !page.HasMore {
			break
		}
		cursor = page.NextBefore
	}
	if len(seen) != len(all) {
		t.Fatalf("visited %d distinct buckets, want %d", len(seen), len(all))
	}
	for ts, n := range seen {
		if n != 1 {
			t.Fatalf("bucket %s returned %d times", time.Unix(ts, 0).UTC().Format(time.RFC3339), n)
		}
	}
}

func TestCandleOverlaysAreIndexAlignedAtEveryPageSize(t *testing.T) {
	// A symbol with no ticks in the requested window returns an empty page,
	// and a page shorter than an indicator's warm-up returns nulls — neither
	// may panic, and every series must stay index-aligned with the candles.
	base := time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	build := func(n int) []candleBar {
		out := make([]candleBar, n)
		for i := range out {
			v := 1000 + float64(i)
			out[i] = candleBar{
				date: base.AddDate(0, 0, i),
				open: v, high: v + 3, low: v - 3, close: v, ticks: 4,
			}
		}
		return out
	}
	for _, tc := range []struct{ bars, start int }{
		{0, 0}, {1, 0}, {5, 0}, {5, 4}, {70, 60}, {120, 60},
	} {
		bars := build(tc.bars)
		overlays := candleOverlays(bars, tc.start)
		want := tc.bars - tc.start
		for key, series := range overlays {
			var got int
			switch s := series.(type) {
			case []*float64:
				got = len(s)
			case []int:
				got = len(s)
			default:
				t.Fatalf("%s has unexpected type %T", key, series)
			}
			if got != want {
				t.Fatalf("bars=%d start=%d: %s has %d points, want %d",
					tc.bars, tc.start, key, got, want)
			}
		}
	}
}

func TestParseCandleQuery(t *testing.T) {
	cases := []struct {
		name    string
		query   string
		wantErr bool
		check   func(t *testing.T, q candleQuery)
	}{
		{
			name: "empty query uses the documented defaults", query: "",
			check: func(t *testing.T, q candleQuery) {
				if q.Symbol != "IR_GOLD_18K" || q.Interval.Name != "1d" ||
					q.Limit != defaultCandleLimit || !q.Overlays {
					t.Fatalf("defaults: %+v", q)
				}
				if q.Before != nil || q.From != nil || q.To != nil {
					t.Fatalf("unset windows: %+v", q)
				}
			},
		},
		{
			name: "canonical request", query: "symbol=XAUUSD&interval=15m&limit=250&overlays=0",
			check: func(t *testing.T, q candleQuery) {
				if q.Symbol != "XAUUSD" || q.Interval.Name != "15m" || q.Limit != 250 || q.Overlays {
					t.Fatalf("%+v", q)
				}
			},
		},
		{
			// The exact request the shipped frontend makes today.
			name: "legacy daily request", query: "interval=daily&days=120",
			check: func(t *testing.T, q candleQuery) {
				if q.Interval.Name != "1d" || q.Limit != 120 {
					t.Fatalf("legacy daily: interval=%s limit=%d", q.Interval.Name, q.Limit)
				}
			},
		},
		{
			// The old endpoint showed days*24 hourly buckets.
			name: "legacy hourly request", query: "interval=hourly&days=14",
			check: func(t *testing.T, q candleQuery) {
				if q.Interval.Name != "1h" || q.Limit != 336 {
					t.Fatalf("legacy hourly: interval=%s limit=%d", q.Interval.Name, q.Limit)
				}
			},
		},
		{
			name: "days generalizes to any interval", query: "interval=5m&days=1",
			check: func(t *testing.T, q candleQuery) {
				if q.Limit != 288 {
					t.Fatalf("limit = %d, want 288", q.Limit)
				}
			},
		},
		{
			name: "days cannot exceed the hard cap", query: "interval=5m&days=3650",
			check: func(t *testing.T, q candleQuery) {
				if q.Limit != maxCandleLimit {
					t.Fatalf("limit = %d, want %d", q.Limit, maxCandleLimit)
				}
			},
		},
		{
			name: "explicit limit wins over legacy days", query: "interval=1d&days=120&limit=7",
			check: func(t *testing.T, q candleQuery) {
				if q.Limit != 7 {
					t.Fatalf("limit = %d, want 7", q.Limit)
				}
			},
		},
		{
			// days keeps its old lenient clamp; a caller that worked
			// yesterday must not start receiving 400s today.
			name: "unreadable days falls back like the old endpoint", query: "interval=daily&days=abc",
			check: func(t *testing.T, q candleQuery) {
				if q.Limit != 120 {
					t.Fatalf("limit = %d, want 120", q.Limit)
				}
			},
		},
		{
			name: "before as RFC3339", query: "before=2026-08-12T09:00:00Z",
			check: func(t *testing.T, q candleQuery) {
				if q.Before == nil || !q.Before.Equal(time.Date(2026, 8, 12, 9, 0, 0, 0, time.UTC)) {
					t.Fatalf("before = %v", q.Before)
				}
			},
		},
		{
			// A Tehran-offset cursor is the same instant; it is stored in UTC.
			name: "before at a Tehran offset", query: "before=2026-08-12T12:30:00%2B03:30",
			check: func(t *testing.T, q candleQuery) {
				if q.Before == nil || !q.Before.Equal(time.Date(2026, 8, 12, 9, 0, 0, 0, time.UTC)) {
					t.Fatalf("before = %v", q.Before)
				}
				if q.Before.Location() != time.UTC {
					t.Fatalf("before is not normalized to UTC: %v", q.Before.Location())
				}
			},
		},
		{
			name: "before as unix seconds", query: "before=1786604400",
			check: func(t *testing.T, q candleQuery) {
				if q.Before == nil || q.Before.Unix() != 1786604400 {
					t.Fatalf("before = %v", q.Before)
				}
			},
		},
		{
			name:  "explicit window",
			query: "from=2026-07-20T00:00:00Z&to=2026-08-01T00:00:00Z",
			check: func(t *testing.T, q candleQuery) {
				if q.From == nil || q.To == nil {
					t.Fatalf("window not parsed: %+v", q)
				}
			},
		},
		{"unknown symbol rejected", "symbol=NOT_A_SYMBOL", true, nil},
		{"empty-but-present symbol falls back to the default", "symbol=", false, func(t *testing.T, q candleQuery) {
			if q.Symbol != "IR_GOLD_18K" {
				t.Fatalf("symbol = %q", q.Symbol)
			}
		}},
		{"unknown interval rejected", "interval=1m", true, nil},
		{"limit zero rejected", "limit=0", true, nil},
		{"negative limit rejected", "limit=-5", true, nil},
		{"limit above the cap rejected", "limit=2001", true, nil},
		{"limit at the cap accepted", "limit=2000", false, func(t *testing.T, q candleQuery) {
			if q.Limit != maxCandleLimit {
				t.Fatalf("limit = %d", q.Limit)
			}
		}},
		{"non numeric limit rejected", "limit=abc", true, nil},
		{"float limit rejected", "limit=10.5", true, nil},
		{"unparseable before rejected", "before=yesterday", true, nil},
		{"unparseable from rejected", "from=2026-13-45", true, nil},
		{"inverted window rejected", "from=2026-08-02T00:00:00Z&to=2026-08-01T00:00:00Z", true, nil},
		{"empty window rejected", "from=2026-08-01T00:00:00Z&to=2026-08-01T00:00:00Z", true, nil},
		{"unknown overlays value rejected", "overlays=maybe", true, nil},
		{"overlays true accepted", "overlays=true", false, func(t *testing.T, q candleQuery) {
			if !q.Overlays {
				t.Fatal("overlays=true should enable overlays")
			}
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			values, err := url.ParseQuery(tc.query)
			if err != nil {
				t.Fatalf("bad test query %q: %v", tc.query, err)
			}
			got, perr := parseCandleQuery(values)
			if tc.wantErr {
				if perr == nil {
					t.Fatalf("query %q accepted: %+v", tc.query, got)
				}
				if perr.Message == "" {
					t.Fatal("rejection carries no message")
				}
				return
			}
			if perr != nil {
				t.Fatalf("query %q rejected: %s", tc.query, perr.Message)
			}
			if tc.check != nil {
				tc.check(t, got)
			}
		})
	}
}

func TestCandleLimitFromDays(t *testing.T) {
	cases := []struct {
		name     string
		days     int
		interval string
		want     int
	}{
		// The two windows the old endpoint served, reproduced exactly.
		{"old daily default", 120, "1d", 120},
		{"old hourly default", 14, "1h", 336},
		{"a week of four-hour bars", 7, "4h", 42},
		{"a day of five-minute bars", 1, "5m", 288},
		{"a year of weekly bars", 365, "1w", 53}, // ceil, so the partial week is kept
		{"a day of two-day bars rounds up to one", 1, "2d", 1},
		{"zero days still yields a bar", 0, "1d", 1},
		{"a decade of five-minute bars hits the cap", 3650, "5m", maxCandleLimit},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			iv := mustInterval(t, tc.interval)
			if got := candleLimitFromDays(tc.days, iv); got != tc.want {
				t.Fatalf("days=%d %s: got %d, want %d", tc.days, tc.interval, got, tc.want)
			}
		})
	}
}

func TestCoverageNoteStatesTheLimits(t *testing.T) {
	base := 300
	from := time.Date(2026, 7, 20, 0, 0, 0, 0, time.UTC)
	note := coverageNote(&base, &from)
	for _, want := range []string{"synthetic", "volume", "300s", "2026-07-20T00:00:00Z"} {
		if !strings.Contains(note, want) {
			t.Fatalf("note does not mention %q: %s", want, note)
		}
	}
	bare := coverageNote(nil, nil)
	for _, want := range []string{"could not be measured", "only daily and coarser"} {
		if !strings.Contains(bare, want) {
			t.Fatalf("note does not mention %q: %s", want, bare)
		}
	}
}

// --- the requested window is snapped OUTWARD onto bucket boundaries ----------

func mustQuery(t *testing.T, query string) candleQuery {
	t.Helper()
	values, err := url.ParseQuery(query)
	if err != nil {
		t.Fatalf("bad test query %q: %v", query, err)
	}
	q, perr := parseCandleQuery(values)
	if perr != nil {
		t.Fatalf("query %q rejected: %s", query, perr.Message)
	}
	return q
}

func TestSnapCandleWindow(t *testing.T) {
	// Regression for F2 and F3, which are one rule seen from both ends: a
	// window edge that lands inside a bucket must move OUTWARD, so the bucket
	// is served whole or not at all. `to` used to floor down (dropping the
	// bucket that contains it) and `from` was not snapped at all (slicing the
	// oldest bucket and publishing the slice under the whole bucket's identity).
	now := time.Date(2026, 8, 12, 6, 30, 0, 0, time.UTC)
	at := func(m, d, h, min int) time.Time {
		return time.Date(2026, time.Month(m), d, h, min, 0, 0, time.UTC)
	}
	cases := []struct {
		name     string
		query    string
		wantFrom *time.Time
		wantTo   time.Time
	}{
		{
			// F2, exactly as reproduced: a window naming one whole day
			// returned `candles: []` for a day that has data, because `to` was
			// floored onto 08-01T00:00 and applied exclusively.
			name:     "a window naming one whole day serves that day",
			query:    "from=2026-08-01T00:00:00Z&to=2026-08-01T23:59:59Z&interval=1d",
			wantFrom: ptrTime(at(8, 1, 0, 0)), wantTo: at(8, 2, 0, 0),
		},
		{
			// F3, exactly as reproduced: ten minutes of Aug 11 came back
			// stamped t=2026-08-11T00:00:00Z, confirmed, with `synthetic`
			// computed off the truncated tick count.
			name:     "from inside a bucket floors to the whole bucket",
			query:    "from=2026-08-11T23:50:00Z&interval=1d",
			wantFrom: ptrTime(at(8, 11, 0, 0)), wantTo: now,
		},
		{
			// Half-open: a `to` that is already a boundary opens a bucket it
			// does not ask for, so adjacent windows tile without overlapping.
			name:     "to already on a boundary does not drag in the next bucket",
			query:    "from=2026-08-01T00:00:00Z&to=2026-08-02T00:00:00Z&interval=1d",
			wantFrom: ptrTime(at(8, 1, 0, 0)), wantTo: at(8, 2, 0, 0),
		},
		{
			name:     "both edges snap outward at 5m",
			query:    "from=2026-08-11T09:03:00Z&to=2026-08-11T09:07:00Z&interval=5m",
			wantFrom: ptrTime(at(8, 11, 9, 0)), wantTo: at(8, 11, 9, 10),
		},
		{
			// Weekly is the one non-epoch floor: Monday 00:00 UTC either way.
			// 2026-08-05 is a Wednesday.
			name:     "weekly floors to Monday and ceils to the next Monday",
			query:    "from=2026-08-05T12:00:00Z&to=2026-08-06T00:00:00Z&interval=1w",
			wantFrom: ptrTime(at(8, 3, 0, 0)), wantTo: at(8, 10, 0, 0),
		},
		{
			// The cursor is NOT a window edge. It names a bucket the caller
			// already received, so it floors down and stays exclusive —
			// ceiling it would re-serve that bucket on every page.
			name:     "before floors down and stays exclusive",
			query:    "before=2026-08-11T23:50:00Z&interval=1d",
			wantFrom: nil, wantTo: at(8, 11, 0, 0),
		},
		{
			name:     "before already on a boundary excludes that bucket",
			query:    "before=2026-08-11T00:00:00Z&interval=1d",
			wantFrom: nil, wantTo: at(8, 11, 0, 0),
		},
		{
			name:     "the tighter of before and to wins",
			query:    "to=2026-08-11T23:59:59Z&before=2026-08-10T05:00:00Z&interval=1d",
			wantFrom: nil, wantTo: at(8, 10, 0, 0),
		},
		{
			name:     "and it is the tighter one, not the last one parsed",
			query:    "to=2026-08-05T12:00:00Z&before=2026-08-11T00:00:00Z&interval=1d",
			wantFrom: nil, wantTo: at(8, 6, 0, 0),
		},
		{
			// `now` is the one bound that is not a boundary: it is where the
			// data ends, and the forming bucket is labelled, not trimmed.
			name:     "a to in the future is capped at now",
			query:    "to=2026-09-01T00:00:00Z&interval=1d",
			wantFrom: nil, wantTo: now,
		},
		{
			name:     "no window at all",
			query:    "interval=1d",
			wantFrom: nil, wantTo: now,
		},
		{
			// Display timezone never touches bucketing: this is 08-10T23:50Z.
			name:     "a Tehran-offset from floors onto the same UTC bucket",
			query:    "from=2026-08-11T03:20:00%2B03:30&interval=1d",
			wantFrom: ptrTime(at(8, 10, 0, 0)), wantTo: now,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			q := mustQuery(t, tc.query)
			iv := q.Interval
			got := snapCandleWindow(q, iv, now)

			switch {
			case tc.wantFrom == nil && got.From != nil:
				t.Fatalf("from = %s, want null", got.From.Format(time.RFC3339))
			case tc.wantFrom != nil && got.From == nil:
				t.Fatalf("from = null, want %s", tc.wantFrom.Format(time.RFC3339))
			case tc.wantFrom != nil && !got.From.Equal(*tc.wantFrom):
				t.Fatalf("from = %s, want %s",
					got.From.Format(time.RFC3339), tc.wantFrom.Format(time.RFC3339))
			}
			if !got.To.Equal(tc.wantTo) {
				t.Fatalf("to = %s, want %s",
					got.To.Format(time.RFC3339), tc.wantTo.Format(time.RFC3339))
			}

			// The invariants the table above is one instance of, stated once.
			if got.From != nil {
				if !iv.BucketStart(*got.From).Equal(*got.From) {
					t.Fatalf("from %s is not a bucket boundary", got.From.Format(time.RFC3339))
				}
				if got.From.After(*q.From) {
					t.Fatalf("from snapped INWARD: %s -> %s",
						q.From.Format(time.RFC3339), got.From.Format(time.RFC3339))
				}
			}
			// `to` must cover its own bucket — unless one of the two legitimate
			// tighteners applies: the cursor, which names a bucket the caller
			// already has, or `now`, where the data ends.
			if q.To != nil && q.Before == nil && !got.To.Equal(now) {
				edge := iv.BucketStart(*q.To)
				if edge.Equal(*q.To) {
					if !got.To.Equal(edge) {
						t.Fatalf("an aligned to=%s moved to %s",
							q.To.Format(time.RFC3339), got.To.Format(time.RFC3339))
					}
				} else if got.To.Before(iv.BucketEnd(edge)) {
					t.Fatalf("the bucket containing to=%s ends at %s but the window stops at %s",
						q.To.Format(time.RFC3339), iv.BucketEnd(edge).Format(time.RFC3339),
						got.To.Format(time.RFC3339))
				}
			}
			if got.To.After(now) {
				t.Fatalf("to = %s is in the future", got.To.Format(time.RFC3339))
			}
		})
	}
}

func TestSnapCandleWindowServesWholeBucketsOnly(t *testing.T) {
	// The consequence F2 and F3 share: every bucket that overlaps the served
	// window must lie entirely inside it, at every interval. A bucket built
	// from part of itself is a lie the wire format cannot express — `ticks`
	// and `synthetic` would describe the slice, not the bucket.
	//
	// The window ends a week before `now` so that no interval's last bucket is
	// the forming one: that bucket is bounded by where the data ends rather
	// than by a boundary, which is the one documented exception to this rule.
	now := time.Date(2026, 8, 12, 6, 30, 0, 0, time.UTC)
	for _, name := range candleIntervalNames() {
		t.Run(name, func(t *testing.T) {
			q := mustQuery(t, "from=2026-08-01T07:13:09Z&to=2026-08-05T19:41:02Z&interval="+name)
			iv := q.Interval
			w := snapCandleWindow(q, iv, now)
			if w.From == nil {
				t.Fatal("from was dropped")
			}
			if !w.From.Before(w.To) {
				t.Fatalf("empty window [%s, %s)",
					w.From.Format(time.RFC3339), w.To.Format(time.RFC3339))
			}
			// Walk every bucket the window admits: each must start and end
			// inside it. Buckets are aggregated over ticks in [From, To), so a
			// bucket whose end exceeds To would be built from part of itself.
			for b := *w.From; b.Before(w.To); b = iv.BucketEnd(b) {
				if !iv.BucketStart(b).Equal(b) {
					t.Fatalf("%s is not a bucket boundary", b.Format(time.RFC3339))
				}
				if iv.BucketEnd(b).After(w.To) {
					t.Fatalf("bucket %s..%s is cut off by the window end %s",
						b.Format(time.RFC3339), iv.BucketEnd(b).Format(time.RFC3339),
						w.To.Format(time.RFC3339))
				}
			}
			// And the caller's own edges are covered: nothing it asked for was
			// dropped because it fell mid-bucket.
			if w.From.After(*q.From) || w.To.Before(*q.To) {
				t.Fatalf("window [%s, %s) does not cover the request [%s, %s)",
					w.From.Format(time.RFC3339), w.To.Format(time.RFC3339),
					q.From.Format(time.RFC3339), q.To.Format(time.RFC3339))
			}
		})
	}
}

func TestCandleResponseEchoesTheEffectiveWindow(t *testing.T) {
	// A caller that asked for 23:59:59 and was served the whole day has to be
	// able to see that. The snapped bounds are part of the answer, not an
	// internal detail.
	now := time.Date(2026, 8, 12, 6, 30, 0, 0, time.UTC)
	q := mustQuery(t, "from=2026-08-01T00:00:00Z&to=2026-08-01T23:59:59Z&interval=1d")
	win := snapCandleWindow(q, q.Interval, now)
	body := candleResponse(q, q.Interval, candleCoverage{}, win, candlePage{},
		buildCandlePayload(nil, 0, q.Interval, now, false), now)

	raw, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var wire map[string]any
	if err := json.Unmarshal(raw, &wire); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	for _, key := range candleResponseKeys {
		if _, ok := wire[key]; !ok {
			t.Fatalf("response is missing %q: %s", key, raw)
		}
	}
	window, ok := wire["effective_window"].(map[string]any)
	if !ok {
		t.Fatalf("effective_window is %T", wire["effective_window"])
	}
	if window["from"] != "2026-08-01T00:00:00Z" {
		t.Fatalf("effective_window.from = %v, want the floored 08-01T00:00:00Z", window["from"])
	}
	if window["to"] != "2026-08-02T00:00:00Z" {
		t.Fatalf("effective_window.to = %v, want the ceiled 08-02T00:00:00Z", window["to"])
	}
	// An empty page still serializes as [], never null: the chart appends to it.
	if candles, ok := wire["candles"].([]any); !ok || len(candles) != 0 {
		t.Fatalf("candles = %v, want []", wire["candles"])
	}
}

// --- levels must not narrow with the page size -------------------------------

func TestCandlePayloadLevelsDoNotNarrowWithLimit(t *testing.T) {
	// Regression for F4. `limit` chooses how many bars are DRAWN; it must not
	// silently shorten the lookback of a level published beside them.
	// indicators.SupportResistance clamps a short slice to whatever it was
	// given instead of refusing, so reading it off the returned page answered
	// a 3-bar question with a 20-bucket band's confidence — and limit=3 is
	// exactly what the shipped chart's live poll sends (TAIL_LIMIT in
	// frontend/src/chart/useCandles.ts).
	iv := mustInterval(t, "1d")
	base := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	bars := make([]candleBar, 60)
	for i := range bars {
		v := float64(1000 + i) // strictly rising, so a short lookback reads high
		bars[i] = candleBar{
			date: base.AddDate(0, 0, i),
			open: v, high: v + 2, low: v - 2, close: v, ticks: 5,
		}
	}
	now := base.AddDate(0, 0, 60) // every fixture bucket has finished
	// The last supportResistanceLookback closes are 1040..1059, whatever the
	// page shows. The measured defect: support came back as 1057 on a 3-bar
	// page and 1040 on a wide one.
	const wantSupport, wantResistance = 1040, 1059

	for _, tc := range []struct {
		name  string
		start int
	}{
		{"the whole fetch is visible", 0},
		{"a twenty-bar page", 40},
		{"the live poll's three-bar page", 57},
		{"a single-bar page", 59},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := buildCandlePayload(bars, tc.start, iv, now, true)
			if len(p.Candles) != len(bars)-tc.start {
				t.Fatalf("page has %d candles, want %d", len(p.Candles), len(bars)-tc.start)
			}
			if p.Support == nil || p.Resistance == nil {
				t.Fatal("support/resistance are null on a fetch that has the lookback")
			}
			if *p.Support != wantSupport || *p.Resistance != wantResistance {
				t.Fatalf("start=%d: support/resistance = %v/%v, want %v/%v — the band "+
					"narrowed with the page", tc.start, *p.Support, *p.Resistance,
					float64(wantSupport), float64(wantResistance))
			}
			// Pivots come from the newest COMPLETED bucket of the fetch, which
			// is also page-size independent.
			if p.Pivots == nil {
				t.Fatal("pivots are null on a fetch with a completed bucket")
			}
			last := bars[len(bars)-1]
			if want := (last.high + last.low + last.close) / 3; p.Pivots.P != want {
				t.Fatalf("start=%d: pivot P = %v, want %v", tc.start, p.Pivots.P, want)
			}
		})
	}
}

func TestCandlePayloadRefusesLevelsItCannotMeasure(t *testing.T) {
	// The other half of F4: a fetch that genuinely lacks the lookback reports
	// null. A number computed from fewer bars than the indicator asks for is
	// indistinguishable on the wire from a real one.
	iv := mustInterval(t, "1d")
	base := time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)
	build := func(n int) []candleBar {
		out := make([]candleBar, n)
		for i := range out {
			v := float64(1000 + i)
			out[i] = candleBar{date: base.AddDate(0, 0, i),
				open: v, high: v + 2, low: v - 2, close: v, ticks: 5}
		}
		return out
	}
	cases := []struct {
		name      string
		bars      int
		start     int
		wantBand  bool
		wantPivot bool
	}{
		{"an empty fetch", 0, 0, false, false},
		{"one bucket short of the lookback", supportResistanceLookback - 1, 0, false, true},
		{"exactly the lookback", supportResistanceLookback, 0, true, true},
		{"a short page over a full fetch", 60, 57, true, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			bars := build(tc.bars)
			now := base.AddDate(0, 0, tc.bars) // every fetched bucket has finished
			p := buildCandlePayload(bars, tc.start, iv, now, false)
			if got := p.Support != nil; got != tc.wantBand {
				t.Fatalf("support present = %v, want %v (bars=%d)", got, tc.wantBand, tc.bars)
			}
			if got := p.Resistance != nil; got != tc.wantBand {
				t.Fatalf("resistance present = %v, want %v (bars=%d)", got, tc.wantBand, tc.bars)
			}
			if got := p.Pivots != nil; got != tc.wantPivot {
				t.Fatalf("pivots present = %v, want %v (bars=%d)", got, tc.wantPivot, tc.bars)
			}
			// Overlays were not asked for and must serialize as null, not {}.
			if p.Overlays != nil {
				t.Fatalf("overlays computed for overlays=0: %v", p.Overlays)
			}
		})
	}
}

func TestCandlePayloadKeepsPivotsOnAPageOfOneFormingBar(t *testing.T) {
	// The live poll can land on a page whose only bar is the forming one.
	// Classic pivots are defined on a COMPLETED bar, and one was fetched right
	// behind it — reporting them as missing would blank the levels on the
	// chart once a minute.
	iv := mustInterval(t, "1h")
	base := time.Date(2026, 8, 12, 0, 0, 0, 0, time.UTC)
	bars := make([]candleBar, 3)
	for i := range bars {
		v := float64(1000 + i)
		bars[i] = candleBar{date: base.Add(time.Duration(i) * time.Hour),
			open: v, high: v + 2, low: v - 2, close: v, ticks: 5}
	}
	now := base.Add(2*time.Hour + 30*time.Minute) // the last bucket is still open
	p := buildCandlePayload(bars, 2, iv, now, false)
	if len(p.Candles) != 1 || p.Candles[0].Confirmed {
		t.Fatalf("page = %d candles, confirmed=%v", len(p.Candles), p.Candles[0].Confirmed)
	}
	if p.Pivots == nil {
		t.Fatal("pivots are null on a page of one forming bar, but bucket 1 has finished")
	}
	finished := bars[1]
	if want := (finished.high + finished.low + finished.close) / 3; p.Pivots.P != want {
		t.Fatalf("pivot P = %v, want %v (the newest COMPLETED bucket)", p.Pivots.P, want)
	}
}

// --- handler-level refusals (no database is reached) -------------------------

func TestCandles_InvalidParamsReturn400(t *testing.T) {
	cases := []struct {
		name  string
		query string
	}{
		{"unknown symbol", "?symbol=NOPE"},
		{"unknown interval", "?interval=1m"},
		{"limit out of range", "?limit=5000"},
		{"unparseable cursor", "?before=tomorrow"},
		{"unknown overlays flag", "?overlays=perhaps"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			h := &Handler{Log: quietLogger()}
			rec := httptest.NewRecorder()
			// A nil pool would panic if the handler reached the database;
			// every case here must be refused before that.
			h.Candles(rec, httptest.NewRequest(http.MethodGet, "/api/v1/market/candles"+tc.query, nil))
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400 (body %s)", rec.Code, rec.Body.String())
			}
			var body httpserver.ErrorBody
			if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
				t.Fatalf("decode: %v", err)
			}
			if body.Error.Code != "bad_request" || body.Error.Message == "" {
				t.Fatalf("error envelope: %+v", body.Error)
			}
		})
	}
}

func TestUnsupportedIntervalMessageIsTheContractText(t *testing.T) {
	// The frontend shows this string verbatim when a timeframe button is
	// unavailable, so it is part of the contract, not a log line.
	if unsupportedIntervalMessage != "This timeframe is not available for the current data source." {
		t.Fatalf("message drifted: %q", unsupportedIntervalMessage)
	}
}
