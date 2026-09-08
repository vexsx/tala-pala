package economic

import (
	"context"
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func day(y int, m time.Month, d int) time.Time {
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

func instant(y int, m time.Month, d, h int) time.Time {
	return time.Date(y, m, d, h, 0, 0, 0, time.UTC)
}

func timePtr(t time.Time) *time.Time { return &t }

func intPtr(n int) *int { return &n }

func values(pairs ...string) url.Values {
	q := url.Values{}
	for i := 0; i+1 < len(pairs); i += 2 {
		q.Set(pairs[i], pairs[i+1])
	}
	return q
}

// requestWithCode builds a request carrying the chi route parameter the
// handlers read, so the refusal paths can be exercised without a router.
func requestWithCode(target, code string) *http.Request {
	r := httptest.NewRequest(http.MethodGet, target, nil)
	rctx := chi.NewRouteContext()
	rctx.URLParams.Add("code", code)
	return r.WithContext(context.WithValue(r.Context(), chi.RouteCtxKey, rctx))
}

// decodeError reads the standard {"error":{code,message,details}} envelope.
func decodeError(t *testing.T, rec *httptest.ResponseRecorder) httpserver.ErrorBody {
	t.Helper()
	var body httpserver.ErrorBody
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("response is not the error envelope: %v", err)
	}
	return body
}

// --- parameter parsing: instruments ------------------------------------------

func TestParseInstrumentQuery_Kind(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		wantErr bool
	}{
		{"absent means no filter", "", false},
		{"market price", "market_price", false},
		{"economic series", "economic_series", false},
		{"fx", "fx", false},
		{"index", "index", false},
		{"basket", "basket", false},
		{"equity", "equity", false},
		// kind carries a CHECK constraint, so an unknown value can never match
		// a row. Serving it as an empty list would read as "there are no gold
		// instruments", which is false.
		{"unknown kind rejected", "gold", true},
		{"uppercase rejected", "MARKET_PRICE", true},
		{"padded rejected", " fx", true},
		{"injection rejected", "'; DROP TABLE instruments;--", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, perr := parseInstrumentQuery(values("kind", tc.raw))
			if tc.wantErr {
				if perr == nil {
					t.Fatalf("kind %q accepted", tc.raw)
				}
				if !strings.Contains(perr.Message, "kind") {
					t.Fatalf("message does not name the parameter: %q", perr.Message)
				}
				return
			}
			if perr != nil {
				t.Fatalf("kind %q rejected: %v", tc.raw, perr)
			}
			if tc.raw == "" {
				if got.Kind != nil {
					t.Fatalf("absent kind became a filter: %v", *got.Kind)
				}
				return
			}
			if got.Kind == nil || *got.Kind != tc.raw {
				t.Fatalf("kind %q not applied: %v", tc.raw, got.Kind)
			}
		})
	}
}

// domain has no CHECK constraint in migration 0024, so an unmatched value is a
// true empty result and must NOT be refused: "no instrument is in the housing
// domain" has to keep being answerable after the first housing instrument
// lands.
func TestParseInstrumentQuery_DomainIsNotAClosedVocabulary(t *testing.T) {
	got, perr := parseInstrumentQuery(values("domain", "housing"))
	if perr != nil {
		t.Fatalf("domain rejected: %v", perr)
	}
	if got.Domain == nil || *got.Domain != "housing" {
		t.Fatalf("domain not applied: %v", got.Domain)
	}
	if got, perr := parseInstrumentQuery(url.Values{}); perr != nil || got.Domain != nil {
		t.Fatalf("absent domain became a filter: %v %v", got.Domain, perr)
	}
}

func TestParseInstrumentQuery_Enabled(t *testing.T) {
	cases := []struct {
		raw     string
		want    *bool
		wantErr bool
	}{
		// Absent applies NO filter: every row carries its own enabled flag, so
		// returning the whole vocabulary is lossless, while defaulting to
		// enabled-only would hide rows without saying so.
		{"", nil, false},
		{"1", boolPtr(true), false},
		{"true", boolPtr(true), false},
		{"YES", boolPtr(true), false},
		{"0", boolPtr(false), false},
		{"false", boolPtr(false), false},
		{"no", boolPtr(false), false},
		{"maybe", nil, true},
		{"2", nil, true},
		{" 1", nil, true},
	}
	for _, tc := range cases {
		t.Run("enabled="+tc.raw, func(t *testing.T) {
			got, perr := parseInstrumentQuery(values("enabled", tc.raw))
			if tc.wantErr {
				if perr == nil {
					t.Fatalf("enabled %q accepted", tc.raw)
				}
				return
			}
			if perr != nil {
				t.Fatalf("enabled %q rejected: %v", tc.raw, perr)
			}
			switch {
			case tc.want == nil && got.Enabled != nil:
				t.Fatalf("enabled %q became %v, want no filter", tc.raw, *got.Enabled)
			case tc.want != nil && (got.Enabled == nil || *got.Enabled != *tc.want):
				t.Fatalf("enabled %q became %v, want %v", tc.raw, got.Enabled, *tc.want)
			}
		})
	}
}

func boolPtr(b bool) *bool { return &b }

// --- parameter parsing: observations -----------------------------------------

func TestParseObservationQuery_AsOfAcceptsBothForms(t *testing.T) {
	now := instant(2026, 9, 8, 12)
	want := time.Date(2026, 3, 1, 9, 30, 0, 0, time.UTC)

	cases := []struct {
		name string
		raw  string
	}{
		{"RFC3339 UTC", "2026-03-01T09:30:00Z"},
		{"RFC3339 with offset", "2026-03-01T13:00:00+03:30"},
		{"unix seconds", "1772357400"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, perr := parseObservationQuery("IR_CPI_M", values("as_of", tc.raw), now)
			if perr != nil {
				t.Fatalf("as_of %q rejected: %v", tc.raw, perr)
			}
			if !got.AsOf.Equal(want) {
				t.Fatalf("as_of %q parsed to %s, want %s", tc.raw, got.AsOf, want)
			}
			if got.AsOf.Location() != time.UTC {
				t.Fatalf("as_of must be normalized to UTC, got %s", got.AsOf.Location())
			}
			if !got.AsOfProvided {
				t.Fatal("an explicit as_of must be reported as provided")
			}
		})
	}
}

// An absent as_of means "latest known": the request time is used AND echoed,
// so the response always states the cutoff it was actually served at.
func TestParseObservationQuery_AsOfDefaultsToNowAndSaysSo(t *testing.T) {
	now := instant(2026, 9, 8, 12)
	got, perr := parseObservationQuery("IR_CPI_M", url.Values{}, now)
	if perr != nil {
		t.Fatalf("absent as_of rejected: %v", perr)
	}
	if !got.AsOf.Equal(now) {
		t.Fatalf("as_of = %s, want the request time %s", got.AsOf, now)
	}
	if got.AsOfProvided {
		t.Fatal("a substituted as_of must not be reported as caller-provided")
	}
}

func TestParseObservationQuery_UnparseableAsOfIsRefused(t *testing.T) {
	now := instant(2026, 9, 8, 12)
	refused := []string{
		"yesterday", "2026-03-01", "2026-13-01T00:00:00Z", "now", "-", "1e9",
		// An integer that is not a plausible epoch is the one substitution
		// parsePeriodDate refuses by name, and a cutoff earns the same
		// standard. Read as seconds these all landed in 1970, before which
		// nothing was knowable, so the endpoint answered 200 with an empty
		// observation list -- which a backtest harness iterating years reads as
		// "this series has no history" rather than as its own bad request.
		"2026",     // a bare Gregorian year -> 1970-01-01T00:33:46Z
		"20260908", // a compact date -> 1970-08-23
		"1405",     // a bare Jalali year -> 1970-01-01T00:23:25Z
		"0",        // the epoch itself, far likelier a stub than a cutoff
		"-1772357400",
		"1772357400000", // milliseconds; read as seconds this is the year 58147
	}
	for _, raw := range refused {
		t.Run(raw, func(t *testing.T) {
			_, perr := parseObservationQuery("IR_CPI_M", values("as_of", raw), now)
			if perr == nil {
				t.Fatalf("as_of %q accepted", raw)
			}
			if !strings.Contains(perr.Message, "as_of") {
				t.Fatalf("message does not name the parameter: %q", perr.Message)
			}
			// The refusal has to teach the caller what would have worked,
			// otherwise it is just a wall.
			if !strings.Contains(perr.Message, "RFC3339") ||
				!strings.Contains(perr.Message, "unix seconds") {
				t.Fatalf("message does not name the accepted formats: %q", perr.Message)
			}
			if perr.Details["as_of"] != raw {
				t.Fatalf("details do not echo the offending value: %v", perr.Details)
			}
		})
	}
}

// The refusal must not swallow the form a backtest harness actually sends: a
// real epoch cutoff still parses, and the boundaries themselves are inclusive.
func TestParseObservationQuery_PlausibleEpochSecondsStillParse(t *testing.T) {
	now := instant(2026, 9, 8, 12)
	for _, raw := range []string{"1000000000", "1772357400", "4102444800"} {
		got, perr := parseObservationQuery("IR_CPI_M", values("as_of", raw), now)
		if perr != nil {
			t.Fatalf("as_of %q refused: %v", raw, perr)
		}
		if got.AsOf.Year() < 2001 || got.AsOf.Year() > 2100 {
			t.Fatalf("as_of %q parsed to %s", raw, got.AsOf)
		}
	}
}

func TestParseObservationQuery_Limit(t *testing.T) {
	now := instant(2026, 9, 8, 12)
	cases := []struct {
		raw     string
		want    int
		wantErr bool
	}{
		{"", defaultObservationLimit, false},
		{"1", 1, false},
		{"5000", maxObservationLimit, false},
		{"0", 0, true},
		{"-1", 0, true},
		{"5001", 0, true},
		{"abc", 0, true},
		{"500.5", 0, true},
		{" 500", 0, true},
	}
	for _, tc := range cases {
		t.Run("limit="+tc.raw, func(t *testing.T) {
			got, perr := parseObservationQuery("IR_CPI_M", values("limit", tc.raw), now)
			if tc.wantErr {
				if perr == nil {
					t.Fatalf("limit %q accepted as %d", tc.raw, got.Limit)
				}
				// Never clamped: an out-of-range limit is refused, so a caller
				// can always tell "that is all there is" from "you were cut off".
				if got.Limit == maxObservationLimit && tc.raw == "5001" {
					t.Fatal("limit was clamped instead of refused")
				}
				return
			}
			if perr != nil {
				t.Fatalf("limit %q rejected: %v", tc.raw, perr)
			}
			if got.Limit != tc.want {
				t.Fatalf("limit %q: got %d, want %d", tc.raw, got.Limit, tc.want)
			}
		})
	}
}

func TestParseObservationQuery_FromTo(t *testing.T) {
	now := instant(2026, 9, 8, 12)

	got, perr := parseObservationQuery("IR_CPI_M",
		values("from", "2025-01-01", "to", "2025-12-01"), now)
	if perr != nil {
		t.Fatalf("valid window rejected: %v", perr)
	}
	if got.From == nil || !got.From.Equal(day(2025, 1, 1)) {
		t.Fatalf("from = %v", got.From)
	}
	if got.To == nil || !got.To.Equal(day(2025, 12, 1)) {
		t.Fatalf("to = %v", got.To)
	}

	// A full timestamp is accepted and reduced to its UTC date.
	got, perr = parseObservationQuery("IR_CPI_M", values("from", "2025-01-01T22:00:00Z"), now)
	if perr != nil || got.From == nil || !got.From.Equal(day(2025, 1, 1)) {
		t.Fatalf("RFC3339 from = %v (%v)", got.From, perr)
	}

	// A single period is a legitimate window.
	if _, perr := parseObservationQuery("IR_CPI_M",
		values("from", "2025-06-01", "to", "2025-06-01"), now); perr != nil {
		t.Fatalf("equal bounds rejected: %v", perr)
	}

	// Inverted window is refused, not silently swapped.
	_, perr = parseObservationQuery("IR_CPI_M",
		values("from", "2025-12-01", "to", "2025-01-01"), now)
	if perr == nil {
		t.Fatal("from > to accepted")
	}
	if !strings.Contains(perr.Message, "from") || !strings.Contains(perr.Message, "to") {
		t.Fatalf("message does not name both bounds: %q", perr.Message)
	}

	// A bare integer in a period field is far more likely a typo'd year than an
	// epoch, so it is refused rather than guessed at.
	for _, raw := range []string{"1735689600", "2025", "Jan 2025", "1405-06"} {
		if _, perr := parseObservationQuery("IR_CPI_M", values("from", raw), now); perr == nil {
			t.Fatalf("from %q accepted", raw)
		}
	}
}

func TestParseObservationQuery_IncludeProjections(t *testing.T) {
	now := instant(2026, 9, 8, 12)

	// A projection is not an observation, so it stays out unless asked for.
	got, perr := parseObservationQuery("IR_CPI_M", url.Values{}, now)
	if perr != nil {
		t.Fatalf("rejected: %v", perr)
	}
	if got.IncludeProjections {
		t.Fatal("include_projections must default to false")
	}

	for _, raw := range []string{"1", "true", "YES"} {
		got, perr := parseObservationQuery("IR_CPI_M", values("include_projections", raw), now)
		if perr != nil || !got.IncludeProjections {
			t.Fatalf("include_projections %q: %v %v", raw, got.IncludeProjections, perr)
		}
	}
	for _, raw := range []string{"0", "false", "no"} {
		got, perr := parseObservationQuery("IR_CPI_M", values("include_projections", raw), now)
		if perr != nil || got.IncludeProjections {
			t.Fatalf("include_projections %q: %v %v", raw, got.IncludeProjections, perr)
		}
	}
	// An unrecognised value must not be read as false: that would look like a
	// series which simply has no projections.
	for _, raw := range []string{"treu", "2", "-1", "on"} {
		if _, perr := parseObservationQuery("IR_CPI_M", values("include_projections", raw), now); perr == nil {
			t.Fatalf("include_projections %q accepted", raw)
		}
	}
}

func TestParseObservationQuery_EmptyCodeIsRefused(t *testing.T) {
	if _, perr := parseObservationQuery("", url.Values{}, instant(2026, 9, 8, 12)); perr == nil {
		t.Fatal("empty series code accepted")
	}
}

// --- point-in-time selection -------------------------------------------------

// cpiVintages is one reference period (Mordad 1405 / August 2025) that was
// first printed in January 2026 and revised in June 2026, plus an untouched
// neighbouring period. This is the shape the whole table exists for.
func cpiVintages() []observationRow {
	return []observationRow{
		{
			RefPeriodStart: day(2025, 8, 1), RefPeriodEnd: day(2025, 8, 31),
			RefPeriodLabel: "1404-05", Value: 100.0, Vintage: 1,
			AvailableAt: instant(2026, 1, 15, 9), PublishedAt: timePtr(instant(2026, 1, 14, 12)),
		},
		{
			RefPeriodStart: day(2025, 8, 1), RefPeriodEnd: day(2025, 8, 31),
			RefPeriodLabel: "1404-05", Value: 103.5, Vintage: 2,
			AvailableAt: instant(2026, 6, 1, 9), PublishedAt: timePtr(instant(2026, 5, 30, 12)),
		},
		{
			RefPeriodStart: day(2025, 9, 1), RefPeriodEnd: day(2025, 9, 30),
			RefPeriodLabel: "1404-06", Value: 108.0, Vintage: 1,
			AvailableAt: instant(2026, 2, 15, 9), PublishedAt: timePtr(instant(2026, 2, 14, 12)),
		},
	}
}

// THE point of economic_observations. A cutoff before the revision must return
// the number that existed at that cutoff, not the one that exists today.
// Getting this backwards leaks the future into every backtest that reads it.
func TestApplyPointInTime_BeforeRevisionReturnsTheOriginalValue(t *testing.T) {
	got := applyPointInTime(cpiVintages(), instant(2026, 3, 1, 0), false)
	if len(got) != 2 {
		t.Fatalf("got %d periods, want 2", len(got))
	}
	// Newest period first, matching the SQL.
	if !got[0].RefPeriodStart.Equal(day(2025, 9, 1)) {
		t.Fatalf("first row is %s, want the newest period", got[0].RefPeriodStart)
	}
	revised := got[1]
	if revised.Vintage != 1 {
		t.Fatalf("vintage = %d, want the first print (1)", revised.Vintage)
	}
	if revised.Value != 100.0 {
		t.Fatalf("value = %v, want the original 100.0 (the revision was not knowable yet)", revised.Value)
	}
}

func TestApplyPointInTime_AfterRevisionReturnsTheRevisedValue(t *testing.T) {
	got := applyPointInTime(cpiVintages(), instant(2026, 9, 8, 0), false)
	if len(got) != 2 {
		t.Fatalf("got %d periods, want 2", len(got))
	}
	revised := got[1]
	if revised.Vintage != 2 || revised.Value != 103.5 {
		t.Fatalf("got vintage %d value %v, want vintage 2 value 103.5",
			revised.Vintage, revised.Value)
	}
}

// The cutoff is inclusive: a row that became available at exactly as_of was
// knowable at as_of.
func TestApplyPointInTime_CutoffIsInclusive(t *testing.T) {
	got := applyPointInTime(cpiVintages(), instant(2026, 6, 1, 9), false)
	if len(got) != 2 || got[1].Vintage != 2 {
		t.Fatalf("a row available at exactly as_of must be visible: %+v", got)
	}
}

// A period whose first print is still in the future at the cutoff is absent
// entirely. An empty answer is the truth; inventing a placeholder would not be.
func TestApplyPointInTime_PeriodNotYetKnowableIsAbsent(t *testing.T) {
	got := applyPointInTime(cpiVintages(), instant(2026, 1, 20, 0), false)
	if len(got) != 1 {
		t.Fatalf("got %d periods, want 1", len(got))
	}
	if !got[0].RefPeriodStart.Equal(day(2025, 8, 1)) || got[0].Value != 100.0 {
		t.Fatalf("wrong row survived the cutoff: %+v", got[0])
	}
	if len(applyPointInTime(cpiVintages(), instant(2025, 1, 1, 0), false)) != 0 {
		t.Fatal("a cutoff before every print must return nothing at all")
	}
}

// available_at is the only clock the cutoff consults. A source that published
// on the 14th but only became readable to us on the 15th was NOT knowable on
// the 14th; filtering on published_at would be the classic look-ahead bug.
func TestApplyPointInTime_FiltersAvailabilityNotPublication(t *testing.T) {
	rows := []observationRow{{
		RefPeriodStart: day(2025, 8, 1), RefPeriodEnd: day(2025, 8, 31),
		Value: 100.0, Vintage: 1,
		PublishedAt: timePtr(instant(2026, 1, 14, 12)),
		AvailableAt: instant(2026, 1, 15, 9),
	}}
	if got := applyPointInTime(rows, instant(2026, 1, 14, 18), false); len(got) != 0 {
		t.Fatalf("a row published but not yet available leaked through: %+v", got)
	}
	if got := applyPointInTime(rows, instant(2026, 1, 15, 10), false); len(got) != 1 {
		t.Fatal("a row past its available_at must be visible")
	}
}

// Same availability, different vintage: the higher vintage supersedes.
func TestApplyPointInTime_EqualAvailabilityBreaksOnVintage(t *testing.T) {
	at := instant(2026, 6, 1, 9)
	rows := []observationRow{
		{RefPeriodStart: day(2025, 8, 1), Value: 1, Vintage: 1, AvailableAt: at},
		{RefPeriodStart: day(2025, 8, 1), Value: 2, Vintage: 3, AvailableAt: at},
		{RefPeriodStart: day(2025, 8, 1), Value: 3, Vintage: 2, AvailableAt: at},
	}
	got := applyPointInTime(rows, instant(2026, 9, 1, 0), false)
	if len(got) != 1 || got[0].Vintage != 3 {
		t.Fatalf("got %+v, want the single highest vintage", got)
	}
}

func TestApplyPointInTime_ProjectionsExcludedUnlessAsked(t *testing.T) {
	rows := append(cpiVintages(), observationRow{
		RefPeriodStart: day(2031, 1, 1), RefPeriodEnd: day(2031, 12, 31),
		RefPeriodLabel: "2031", Value: 42.0, Vintage: 1,
		AvailableAt: instant(2026, 4, 1, 9), IsProjection: true,
	})

	got := applyPointInTime(rows, instant(2026, 9, 8, 0), false)
	for _, r := range got {
		if r.IsProjection {
			t.Fatal("a projection entered the default series; a return computed from it would be fiction")
		}
	}
	if len(got) != 2 {
		t.Fatalf("got %d rows, want 2 observations", len(got))
	}

	got = applyPointInTime(rows, instant(2026, 9, 8, 0), true)
	if len(got) != 3 {
		t.Fatalf("got %d rows, want 3 with projections included", len(got))
	}
	if !got[0].IsProjection {
		t.Fatalf("the 2031 projection should sort first (newest period): %+v", got[0])
	}
}

func TestApplyPointInTime_EmptyInput(t *testing.T) {
	if got := applyPointInTime(nil, instant(2026, 9, 8, 0), false); len(got) != 0 {
		t.Fatalf("got %+v, want nothing", got)
	}
}

// --- observations response ---------------------------------------------------

func cpiSeries() seriesRow {
	return seriesRow{
		ID: 7, Code: "IR_CPI_M", NameEN: "Iran CPI, monthly", NameFA: "شاخص بهای مصرف‌کننده",
		Domain: "macro", Frequency: "M", Calendar: "jalali", Measure: "index",
		SeasonalAdjustment: "nsa", BasePeriod: "1400=100",
		ProviderCode: "sci", ProviderSeriesID: "cpi-monthly",
		PublicationLagDays: intPtr(45), Revisable: true, SplicePolicy: "chain_growth",
		Enabled: true, QualityTier: "official", Unit: "index", Decimals: 1,
		// The catalog writes this warning so it travels WITH the number. It is
		// the difference between a chart of Iranian CPI and a chart of
		// something that merely looks like it.
		Notes: "Rebased proxy: this is NOT the Statistical Centre of Iran's own 1400=100 index.",
	}
}

func TestBuildObservationsResponse_CarriesProvenance(t *testing.T) {
	q := observationQuery{
		Code: "IR_CPI_M", AsOf: instant(2026, 9, 8, 12), AsOfProvided: true,
		Limit: defaultObservationLimit,
	}
	got := buildObservationsResponse(cpiSeries(), cpiVintages(), q)

	// A number that cannot state its provenance does not ship: everything
	// needed to interpret the values travels with them.
	if got.Code != "IR_CPI_M" || got.Measure != "index" || got.Unit != "index" ||
		got.Frequency != "M" || got.ProviderCode != "sci" || got.QualityTier != "official" ||
		got.BasePeriod != "1400=100" || got.SplicePolicy != "chain_growth" ||
		got.Calendar != "jalali" {
		t.Fatalf("provenance is incomplete: %+v", got)
	}
	// notes is provenance too, and it is the piece a chart component can get
	// NOWHERE else: it fetches observations, not /instruments. A caveat that
	// only exists on a different endpoint is a caveat nobody reads.
	if got.Notes != cpiSeries().Notes {
		t.Fatalf("notes did not travel with the values: %q", got.Notes)
	}
	blob, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(blob), `"notes":"Rebased proxy`) {
		t.Fatalf("notes missing from the wire payload: %s", blob)
	}
	if !got.AsOf.Equal(q.AsOf) || !got.AsOfProvided {
		t.Fatalf("as_of = %s (provided=%v), want the effective cutoff", got.AsOf, got.AsOfProvided)
	}
	if got.IncludeProjections {
		t.Fatal("include_projections must be echoed as false")
	}
}

// The same caveat has to reach the registry payload, and the statement that
// feeds it has to actually select the column.
func TestBuildSeriesItem_CarriesNotes(t *testing.T) {
	got := buildSeriesItem(cpiSeries())
	if got.Notes != cpiSeries().Notes {
		t.Fatalf("notes = %q, want the catalog's warning", got.Notes)
	}
	if !strings.Contains(seriesSelectColumns, "i.notes") {
		t.Fatal("the series statement must select instruments.notes, or the field is always empty")
	}
}

func TestBuildObservationsResponse_VintagesUsedDescribesThePayload(t *testing.T) {
	base := observationQuery{Code: "IR_CPI_M", Limit: defaultObservationLimit}

	before := base
	before.AsOf = instant(2026, 3, 1, 0)
	got := buildObservationsResponse(cpiSeries(), cpiVintages(), before)
	if len(got.VintagesUsed) != 1 || got.VintagesUsed[0] != 1 {
		t.Fatalf("vintages_used = %v, want [1] before the revision", got.VintagesUsed)
	}

	after := base
	after.AsOf = instant(2026, 9, 8, 0)
	got = buildObservationsResponse(cpiSeries(), cpiVintages(), after)
	if len(got.VintagesUsed) != 2 || got.VintagesUsed[0] != 1 || got.VintagesUsed[1] != 2 {
		t.Fatalf("vintages_used = %v, want [1 2] for a partly revised history", got.VintagesUsed)
	}
}

// The payload reads chronologically: a series is differenced and charted
// oldest-first, so that is the order it ships in.
func TestBuildObservationsResponse_IsChronological(t *testing.T) {
	q := observationQuery{Code: "IR_CPI_M", AsOf: instant(2026, 9, 8, 0), Limit: defaultObservationLimit}
	got := buildObservationsResponse(cpiSeries(), cpiVintages(), q)
	if got.Count != 2 || len(got.Observations) != 2 {
		t.Fatalf("count = %d, want 2", got.Count)
	}
	if got.Observations[0].RefPeriodStart != "2025-08-01" ||
		got.Observations[1].RefPeriodStart != "2025-09-01" {
		t.Fatalf("not oldest-first: %+v", got.Observations)
	}
	// A DATE is rendered as a date. Serializing it as a timestamp would invent
	// a time of day and a timezone the source never stated.
	if got.Observations[0].RefPeriodEnd != "2025-08-31" {
		t.Fatalf("ref_period_end = %q, want a plain date", got.Observations[0].RefPeriodEnd)
	}
	if got.Observations[0].RefPeriodLabel != "1404-05" {
		t.Fatalf("the source's own period label must survive: %q", got.Observations[0].RefPeriodLabel)
	}
}

// The limit selects the most RECENT periods, then the page is presented
// oldest-first.
func TestBuildObservationsResponse_LimitKeepsTheMostRecentPeriods(t *testing.T) {
	rows := []observationRow{}
	for i := 1; i <= 5; i++ {
		rows = append(rows, observationRow{
			RefPeriodStart: day(2025, time.Month(i), 1),
			RefPeriodEnd:   day(2025, time.Month(i), 28),
			Value:          float64(i), Vintage: 1,
			AvailableAt: instant(2025, time.Month(i), 15, 9),
		})
	}
	q := observationQuery{Code: "IR_CPI_M", AsOf: instant(2026, 9, 8, 0), Limit: 2}
	got := buildObservationsResponse(cpiSeries(), rows, q)
	if got.Count != 2 {
		t.Fatalf("count = %d, want 2", got.Count)
	}
	if got.Observations[0].RefPeriodStart != "2025-04-01" ||
		got.Observations[1].RefPeriodStart != "2025-05-01" {
		t.Fatalf("limit dropped the wrong end of the series: %+v", got.Observations)
	}
	// A truncated page that looks exactly like a complete one is how a client
	// charting "full history" plots a series that starts three periods late.
	if !got.HasMore {
		t.Fatal("three periods were withheld and has_more says otherwise")
	}
	if got.NextBefore == nil || *got.NextBefore != "2025-04-01" {
		t.Fatalf("next_before = %v, want the oldest period in this page", got.NextBefore)
	}
}

// The signal has to be believable in both directions: a page that carries
// everything must not claim there is more, or a client pages forever.
func TestBuildObservationsResponse_CompletePageDoesNotClaimMore(t *testing.T) {
	q := observationQuery{
		Code: "IR_CPI_M", AsOf: instant(2026, 9, 8, 0), Limit: defaultObservationLimit,
	}
	got := buildObservationsResponse(cpiSeries(), cpiVintages(), q)
	if got.HasMore {
		t.Fatal("a complete page must not report has_more")
	}
	// ...and it must not hand back a cursor either. A client that pages until
	// next_before is null would otherwise make one guaranteed-empty extra
	// request on every complete series, which makes the cursor a weaker
	// completion signal than has_more for no benefit.
	if got.NextBefore != nil {
		t.Fatalf("next_before = %v, want nil once has_more is false", *got.NextBefore)
	}

	// Nothing at this cutoff: no page, no cursor, and still not "has_more".
	q.AsOf = instant(2020, 1, 1, 0)
	empty := buildObservationsResponse(cpiSeries(), cpiVintages(), q)
	if empty.HasMore || empty.NextBefore != nil {
		t.Fatalf("empty page: has_more=%v next_before=%v", empty.HasMore, empty.NextBefore)
	}
	blob, _ := json.Marshal(empty)
	for _, want := range []string{`"has_more":false`, `"next_before":null`} {
		if !strings.Contains(string(blob), want) {
			t.Fatalf("missing %s in %s", want, blob)
		}
	}
}

// The cursor a page hands out must be the cursor that fetches the next page.
// ?before= is exclusive -- it names a period the caller already holds -- so
// paging back must neither repeat nor skip a period.
func TestObservationsCursor_PagesBackWithoutGapOrRepeat(t *testing.T) {
	rows := []observationRow{}
	for i := 1; i <= 5; i++ {
		rows = append(rows, observationRow{
			RefPeriodStart: day(2025, time.Month(i), 1),
			RefPeriodEnd:   day(2025, time.Month(i), 28),
			Value:          float64(i), Vintage: 1,
			AvailableAt: instant(2025, time.Month(i), 15, 9),
		})
	}
	q := observationQuery{Code: "IR_CPI_M", AsOf: instant(2026, 9, 8, 0), Limit: 2}

	seen := []string{}
	for page := 0; page < 4; page++ {
		// What the database does with ?before= (exclusive on ref_period_start),
		// applied here so the cursor contract is testable without one.
		visible := []observationRow{}
		for _, r := range rows {
			if q.Before != nil && !r.RefPeriodStart.Before(*q.Before) {
				continue
			}
			visible = append(visible, r)
		}
		got := buildObservationsResponse(cpiSeries(), visible, q)
		for _, o := range got.Observations {
			seen = append(seen, o.RefPeriodStart)
		}
		if !got.HasMore {
			break
		}
		if got.NextBefore == nil {
			t.Fatal("has_more with no cursor to follow it")
		}
		cursor, err := time.Parse(dateLayout, *got.NextBefore)
		if err != nil {
			t.Fatalf("next_before is not a period date: %v", err)
		}
		q.Before = &cursor
	}

	want := []string{
		"2025-04-01", "2025-05-01", // page 1, oldest-first within the page
		"2025-02-01", "2025-03-01", // page 2
		"2025-01-01", // page 3
	}
	if len(seen) != len(want) {
		t.Fatalf("paged %v, want %v", seen, want)
	}
	for i := range want {
		if seen[i] != want[i] {
			t.Fatalf("paged %v, want %v", seen, want)
		}
	}
}

// The window a caller sends is not always the window the server reads: a
// Tehran client's local midnight is the previous UTC day, and on an inclusive
// period bound that is a whole period. The interpretation is echoed rather than
// guessed at, the way /market/candles echoes effective_window.
func TestObservations_EffectiveWindowEchoesTheInterpretedBounds(t *testing.T) {
	now := instant(2026, 9, 8, 12)
	q, perr := parseObservationQuery("IR_CPI_M", values(
		"from", "2025-01-01T00:00:00+03:30",
		"to", "2025-12-01",
		"before", "2025-07-01",
	), now)
	if perr != nil {
		t.Fatalf("window rejected: %v", perr)
	}
	// +03:30 local midnight is the previous UTC day. That is the reading, and
	// the payload has to say so out loud.
	if q.From == nil || !q.From.Equal(day(2024, 12, 31)) {
		t.Fatalf("from = %v, want the UTC date 2024-12-31", q.From)
	}

	got := buildObservationsResponse(cpiSeries(), cpiVintages(), q)
	w := got.EffectiveWindow
	if w.From == nil || *w.From != "2024-12-31" {
		t.Fatalf("effective_window.from = %v, want the bound actually used", w.From)
	}
	if w.To == nil || *w.To != "2025-12-01" {
		t.Fatalf("effective_window.to = %v", w.To)
	}
	if w.Before == nil || *w.Before != "2025-07-01" {
		t.Fatalf("effective_window.before = %v", w.Before)
	}

	// An unbounded side is null, never an invented date.
	plain, perr := parseObservationQuery("IR_CPI_M", url.Values{}, now)
	if perr != nil {
		t.Fatalf("unbounded request rejected: %v", perr)
	}
	blob, _ := json.Marshal(buildObservationsResponse(cpiSeries(), cpiVintages(), plain))
	if !strings.Contains(string(blob), `"effective_window":{"from":null,"to":null,"before":null}`) {
		t.Fatalf("an unbounded window must encode as nulls: %s", blob)
	}
}

func TestBuildObservationsResponse_ProjectionsAreFlaggedAndCounted(t *testing.T) {
	rows := append(cpiVintages(), observationRow{
		RefPeriodStart: day(2031, 1, 1), RefPeriodEnd: day(2031, 12, 31),
		RefPeriodLabel: "2031", Value: 42.0, Vintage: 1,
		AvailableAt: instant(2026, 4, 1, 9), IsProjection: true,
	})
	meta := cpiSeries()
	meta.ProjectionCount = 6 // what the series holds, whatever this read returns
	q := observationQuery{
		Code: "IR_CPI_M", AsOf: instant(2026, 9, 8, 0),
		Limit: defaultObservationLimit, IncludeProjections: true,
	}
	got := buildObservationsResponse(meta, rows, q)
	if !got.IncludeProjections || got.ProjectionsInPage != 1 {
		t.Fatalf("projections not declared at the envelope: include=%v in_page=%d",
			got.IncludeProjections, got.ProjectionsInPage)
	}
	last := got.Observations[len(got.Observations)-1]
	if !last.IsProjection {
		t.Fatalf("the projection row is not flagged: %+v", last)
	}
	for _, o := range got.Observations[:len(got.Observations)-1] {
		if o.IsProjection {
			t.Fatalf("a real observation was flagged as a projection: %+v", o)
		}
	}

	q.IncludeProjections = false
	got = buildObservationsResponse(meta, rows, q)
	if got.ProjectionsInPage != 0 || got.Count != 2 {
		t.Fatalf("projection survived the default read: count=%d in_page=%d",
			got.Count, got.ProjectionsInPage)
	}
}

// The default read withholds every projection, so a count of what is IN the
// page is always 0 there -- which reads as "this series has no projections"
// rather than "some were withheld". The page count and the series-wide count
// therefore both ship, under names that cannot be confused.
func TestBuildObservationsResponse_WithheldProjectionsAreNotReportedAsAbsent(t *testing.T) {
	meta := cpiSeries()
	meta.ProjectionCount = 6
	q := observationQuery{
		Code: "IR_CPI_M", AsOf: instant(2026, 9, 8, 0),
		Limit: defaultObservationLimit, // include_projections defaults to false
	}
	got := buildObservationsResponse(meta, cpiVintages(), q)

	if got.ProjectionsInPage != 0 {
		t.Fatalf("projections_in_page = %d, want 0 on a default read", got.ProjectionsInPage)
	}
	if got.ProjectionsAvailable != 6 {
		t.Fatalf("projections_available = %d, want the series' 6: a withheld projection "+
			"must not be indistinguishable from one that does not exist",
			got.ProjectionsAvailable)
	}

	blob, _ := json.Marshal(got)
	// The old name is gone: a lone `projection_count` next to
	// include_projections=false could only be read as "there are none".
	if strings.Contains(string(blob), `"projection_count"`) {
		t.Fatalf("the ambiguous projection_count is still on the wire: %s", blob)
	}
	for _, want := range []string{`"projections_in_page":0`, `"projections_available":6`} {
		if !strings.Contains(string(blob), want) {
			t.Fatalf("missing %s in %s", want, blob)
		}
	}
}

// "Nothing was knowable yet" is a real answer: a 200 with empty lists, not a
// 404 and not a missing key a client has to special-case.
func TestBuildObservationsResponse_EmptyEncodesAsLists(t *testing.T) {
	q := observationQuery{Code: "IR_CPI_M", AsOf: instant(2020, 1, 1, 0), Limit: defaultObservationLimit}
	got := buildObservationsResponse(cpiSeries(), cpiVintages(), q)
	if got.Count != 0 {
		t.Fatalf("count = %d, want 0", got.Count)
	}
	blob, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{`"observations":[]`, `"vintages_used":[]`} {
		if !strings.Contains(string(blob), want) {
			t.Fatalf("missing %s in %s", want, blob)
		}
	}
}

func TestBuildObservationsResponse_NormalizesTimestampsToUTC(t *testing.T) {
	tehran := time.FixedZone("Asia/Tehran", 3*3600+1800)
	rows := []observationRow{{
		RefPeriodStart: day(2025, 8, 1).In(tehran),
		RefPeriodEnd:   day(2025, 8, 31).In(tehran),
		Value:          100, Vintage: 1,
		AvailableAt: instant(2026, 1, 15, 9).In(tehran),
		PublishedAt: timePtr(instant(2026, 1, 14, 12).In(tehran)),
	}}
	q := observationQuery{Code: "IR_CPI_M", AsOf: instant(2026, 9, 8, 0).In(tehran), Limit: 10}
	got := buildObservationsResponse(cpiSeries(), rows, q)

	if got.AsOf.Location() != time.UTC {
		t.Fatalf("as_of location = %s", got.AsOf.Location())
	}
	o := got.Observations[0]
	if o.AvailableAt.Location() != time.UTC || o.PublishedAt.Location() != time.UTC {
		t.Fatal("stored timestamps must serialize as UTC; Tehran is display-only")
	}
	if o.RefPeriodStart != "2025-08-01" {
		t.Fatalf("ref_period_start = %q after a timezone round trip", o.RefPeriodStart)
	}
}

// published_at is null whenever the source stated no publication time. It must
// never be backfilled from available_at, which is a different fact.
func TestBuildObservationsResponse_NullPublishedAtStaysNull(t *testing.T) {
	rows := []observationRow{{
		RefPeriodStart: day(2025, 8, 1), RefPeriodEnd: day(2025, 8, 31),
		Value: 100, Vintage: 1, AvailableAt: instant(2026, 1, 15, 9), PublishedAt: nil,
	}}
	q := observationQuery{Code: "IR_CPI_M", AsOf: instant(2026, 9, 8, 0), Limit: 10}
	got := buildObservationsResponse(cpiSeries(), rows, q)
	if got.Observations[0].PublishedAt != nil {
		t.Fatalf("published_at was invented: %v", got.Observations[0].PublishedAt)
	}
	blob, _ := json.Marshal(got.Observations[0])
	if !strings.Contains(string(blob), `"published_at":null`) {
		t.Fatalf("published_at should encode as null: %s", blob)
	}
}

// --- the point-in-time statement ---------------------------------------------

// A Go test cannot execute the statement, but it can hold the two properties
// that make it correct: the cutoff is applied to available_at, and the ordering
// matches idx_econ_obs_pit so the index serves it directly.
func TestObservationsSelect_IsPointInTimeOnAvailableAt(t *testing.T) {
	if !strings.Contains(observationsSelect, "o.available_at <= $2::timestamptz") {
		t.Fatal("the cutoff must be applied to available_at")
	}
	if strings.Contains(observationsSelect, "published_at <=") ||
		strings.Contains(observationsSelect, "published_at >=") {
		t.Fatal("published_at must never gate a point-in-time read (migration 0017 rule)")
	}
	if !strings.Contains(observationsSelect, "DISTINCT ON (o.ref_period_start)") {
		t.Fatal("one row per reference period is the whole contract")
	}
	if !strings.Contains(observationsSelect,
		"ORDER BY o.ref_period_start DESC, o.available_at DESC, o.vintage DESC") {
		t.Fatal("the ORDER BY must match idx_econ_obs_pit's column order")
	}
}

// The pagination cursor is EXCLUSIVE on ref_period_start. Written as <= it
// would repeat one period on every page; written against available_at it would
// silently become a second, conflicting point-in-time cutoff.
func TestObservationsSelect_CursorIsAnExclusivePeriodBound(t *testing.T) {
	if !strings.Contains(observationsSelect, "($5::date IS NULL OR o.ref_period_start < $5)") {
		t.Fatal("?before= must be an exclusive bound on ref_period_start")
	}
	if !strings.Contains(observationsSelect, "LIMIT $7") {
		t.Fatal("the page limit must be the last bound parameter")
	}
}

// fetchLimit asks for one period more than the page carries: that row is the
// evidence for has_more, and asking for exactly `limit` would make a full page
// indistinguishable from a truncated one.
func TestObservationQuery_FetchLimitAsksForOneExtraPeriod(t *testing.T) {
	q := observationQuery{Limit: defaultObservationLimit}
	if q.fetchLimit() != defaultObservationLimit+1 {
		t.Fatalf("fetchLimit = %d, want %d", q.fetchLimit(), defaultObservationLimit+1)
	}
	if (observationQuery{Limit: 1}).fetchLimit() != 2 {
		t.Fatal("a limit of 1 must still fetch its proof row")
	}
	// maxObservationLimit is a guard against accidents, not a storage bound, so
	// one row past it is a fetch this database can serve.
	if (observationQuery{Limit: maxObservationLimit}).fetchLimit() != maxObservationLimit+1 {
		t.Fatal("the cap must not swallow the proof row")
	}
}

// economic_observations stores VINTAGES: a period revised six times is six rows
// and ONE period. count(*) sitting beside first_period/last_period reported 72
// observations for a 66-period annual series, and it got wronger the more
// faithfully the store recorded revisions. A Go test cannot execute the
// statement, but it can hold the semantics it is required to have.
func TestSeriesSelect_CoverageCountsPeriodsNotVintageRows(t *testing.T) {
	if strings.Contains(seriesSelectFrom, "count(*)") {
		t.Fatal("count(*) over a bitemporal table counts revisions, not reference periods")
	}
	distincts := strings.Count(seriesSelectFrom, "count(DISTINCT o.ref_period_start)")
	if distincts != 2 {
		t.Fatalf("found %d period counts, want 2 (observations and projections)", distincts)
	}
	// Both counts must still be split by is_projection: a projection is not an
	// observation, and last_period would otherwise report a year nobody
	// measured.
	if !strings.Contains(seriesSelectFrom, "FILTER (WHERE NOT o.is_projection) AS observation_count") ||
		!strings.Contains(seriesSelectFrom, "FILTER (WHERE o.is_projection)     AS projection_count") {
		t.Fatalf("the counts lost their projection split:\n%s", seriesSelectFrom)
	}
}

// --- instruments response ----------------------------------------------------

func TestBuildInstrumentsResponse_ShapeAndOrder(t *testing.T) {
	rows := []instrumentRow{
		{Code: "USD_IRT", Kind: "fx", NameEN: "US dollar, free market", NameFA: "دلار آزاد",
			Domain: "fx", QuoteCurrency: "IRT", Unit: "usd", Decimals: 0,
			CalendarClass: "always_open", QualityTier: "proxy", IsProxy: true,
			Enabled: true, Notes: "USDT/toman proxy"},
		{Code: "XAUUSD", Kind: "market_price", Domain: "global", QuoteCurrency: "USD",
			Unit: "ozt", Decimals: 2, CalendarClass: "global",
			QualityTier: "official_mirror", Enabled: true},
		{Code: "IR_GOLD_18K", Kind: "market_price", Domain: "gold", QuoteCurrency: "IRT",
			Unit: "gram", CalendarClass: "always_open", QualityTier: "official_mirror",
			Enabled: true},
	}
	got := buildInstrumentsResponse(rows)
	if got.Count != 3 {
		t.Fatalf("count = %d, want 3", got.Count)
	}
	// kind first ("fx" < "market_price"), then domain ("global" < "gold"), then code.
	wantOrder := []string{"USD_IRT", "XAUUSD", "IR_GOLD_18K"}
	for i, code := range wantOrder {
		if got.Items[i].Code != code {
			t.Fatalf("order = %s..., want %v", got.Items[i].Code, wantOrder)
		}
	}
	// USD_IRT is a documented free-market proxy, not an official mirror. A
	// client that cannot see that renders it as something it is not.
	if !got.Items[0].IsProxy || got.Items[0].QualityTier != "proxy" {
		t.Fatalf("proxy provenance lost: %+v", got.Items[0])
	}
	if got.Items[0].NameFA != "دلار آزاد" {
		t.Fatalf("Persian name lost: %q", got.Items[0].NameFA)
	}
}

func TestBuildInstrumentsResponse_EmptyEncodesAsList(t *testing.T) {
	blob, err := json.Marshal(buildInstrumentsResponse(nil))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(blob), `"items":[]`) {
		t.Fatalf("empty registry should encode as a list: %s", blob)
	}
}

// --- series response ---------------------------------------------------------

func TestBuildSeriesItem_CoverageAndIdentity(t *testing.T) {
	row := cpiSeries()
	row.FirstPeriod = timePtr(day(2015, 1, 1))
	row.LastPeriod = timePtr(day(2025, 9, 1))
	row.ObservationCount = 132
	row.LatestAvailableAt = timePtr(instant(2026, 6, 1, 9))
	row.MaxVintage = intPtr(2)
	row.ProjectionCount = 6

	got := buildSeriesItem(row)
	if got.Coverage.FirstPeriod == nil || *got.Coverage.FirstPeriod != "2015-01-01" {
		t.Fatalf("first_period = %v", got.Coverage.FirstPeriod)
	}
	if got.Coverage.LastPeriod == nil || *got.Coverage.LastPeriod != "2025-09-01" {
		t.Fatalf("last_period = %v", got.Coverage.LastPeriod)
	}
	if got.Coverage.ObservationCount != 132 || got.Coverage.ProjectionCount != 6 {
		t.Fatalf("coverage counts = %+v", got.Coverage)
	}
	if got.Coverage.MaxVintage == nil || *got.Coverage.MaxVintage != 2 {
		t.Fatalf("max_vintage = %v", got.Coverage.MaxVintage)
	}
	if got.Coverage.LatestAvailableAt == nil ||
		!got.Coverage.LatestAvailableAt.Equal(instant(2026, 6, 1, 9)) {
		t.Fatalf("latest_available_at = %v", got.Coverage.LatestAvailableAt)
	}
	// measure, base_period and seasonal adjustment are series IDENTITY: Iran
	// publishes point-to-point and twelve-month-average inflation and they
	// diverged by 20+ points, so a client that drops them cannot tell which
	// number it holds.
	if got.Measure != "index" || got.BasePeriod != "1400=100" ||
		got.SeasonalAdjustment != "nsa" || got.SplicePolicy != "chain_growth" {
		t.Fatalf("series identity incomplete: %+v", got)
	}
	if got.PublicationLagDays == nil || *got.PublicationLagDays != 45 {
		t.Fatalf("publication_lag_days = %v", got.PublicationLagDays)
	}
}

// A series with nothing stored reports nulls, not zeros or an invented date:
// "never observed" and "observed as zero" are different facts.
func TestBuildSeriesItem_UncharacterisedCoverageStaysNull(t *testing.T) {
	row := cpiSeries()
	row.PublicationLagDays = nil

	got := buildSeriesItem(row)
	if got.Coverage.FirstPeriod != nil || got.Coverage.LastPeriod != nil ||
		got.Coverage.LatestAvailableAt != nil || got.Coverage.MaxVintage != nil {
		t.Fatalf("empty coverage was filled in: %+v", got.Coverage)
	}
	if got.Coverage.ObservationCount != 0 || got.Coverage.ProjectionCount != 0 {
		t.Fatalf("counts = %+v, want zero", got.Coverage)
	}
	if got.PublicationLagDays != nil {
		t.Fatalf("an uncharacterised publication lag must stay null, got %v", *got.PublicationLagDays)
	}
	blob, _ := json.Marshal(got)
	for _, want := range []string{`"first_period":null`, `"max_vintage":null`, `"publication_lag_days":null`} {
		if !strings.Contains(string(blob), want) {
			t.Fatalf("missing %s in %s", want, blob)
		}
	}
}

func TestBuildSeriesListResponse_OrderAndEmpty(t *testing.T) {
	global := cpiSeries()
	global.Code = "WB_IR_CPI_A"
	global.Domain = "global"
	second := cpiSeries()
	second.Code = "IR_CPI_M_AVG"

	got := buildSeriesListResponse([]seriesRow{global, cpiSeries(), second})
	if got.Count != 3 {
		t.Fatalf("count = %d", got.Count)
	}
	// Ordered by domain, then code: "global" sorts before "macro".
	if got.Items[0].Code != "WB_IR_CPI_A" || got.Items[0].Domain != "global" {
		t.Fatalf("domain must sort first (global < macro): %s", got.Items[0].Code)
	}
	if got.Items[1].Code != "IR_CPI_M" || got.Items[2].Code != "IR_CPI_M_AVG" {
		t.Fatalf("codes not ordered within a domain: %s, %s",
			got.Items[1].Code, got.Items[2].Code)
	}

	blob, _ := json.Marshal(buildSeriesListResponse(nil))
	if !strings.Contains(string(blob), `"items":[]`) {
		t.Fatalf("empty registry should encode as a list: %s", blob)
	}
}

// --- handler refusals (no database is reached) --------------------------------

func TestInstruments_UnknownKindReturns400(t *testing.T) {
	h := &Handler{Log: quietLogger()} // nil Pool: parsing must refuse first
	rec := httptest.NewRecorder()
	h.Instruments(rec, httptest.NewRequest(http.MethodGet, "/api/v1/instruments?kind=gold", nil))

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
	body := decodeError(t, rec)
	if body.Error.Code != "bad_request" || body.Error.Message == "" {
		t.Fatalf("error envelope = %+v", body.Error)
	}
	if body.Error.Details["kind"] != "gold" {
		t.Fatalf("details should echo the refused value: %v", body.Error.Details)
	}
}

func TestInstruments_UnparseableEnabledReturns400(t *testing.T) {
	h := &Handler{Log: quietLogger()}
	rec := httptest.NewRecorder()
	h.Instruments(rec, httptest.NewRequest(http.MethodGet, "/api/v1/instruments?enabled=maybe", nil))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

func TestObservations_BadParametersAreRefusedBeforeAnyQuery(t *testing.T) {
	cases := []struct {
		name  string
		query string
		names []string
	}{
		{"unparseable as_of", "as_of=yesterday", []string{"as_of"}},
		{"inverted window", "from=2025-12-01&to=2025-01-01", []string{"from", "to"}},
		{"limit below range", "limit=0", []string{"limit"}},
		{"limit above range", "limit=5001", []string{"limit"}},
		{"limit not an integer", "limit=many", []string{"limit"}},
		{"unparseable from", "from=Jan+2025", []string{"from"}},
		{"unparseable include_projections", "include_projections=treu", []string{"include_projections"}},
		// The cursor is a period bound and gets the period bound's standard.
		{"unparseable before", "before=1735689600", []string{"before"}},
		// A bare year in the cutoff answered 200 with an empty history, which a
		// harness iterating years cannot tell from a series that has none.
		{"bare year as_of", "as_of=2026", []string{"as_of"}},
		{"compact date as_of", "as_of=20260908", []string{"as_of"}},
	}
	// nil Pool: any of these reaching the database would panic, which is
	// exactly the assertion — refusal happens before a statement is prepared.
	h := &Handler{Log: quietLogger()}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			h.Observations(rec, requestWithCode(
				"/api/v1/series/IR_CPI_M/observations?"+tc.query, "IR_CPI_M"))

			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400", rec.Code)
			}
			body := decodeError(t, rec)
			if body.Error.Code != "bad_request" {
				t.Fatalf("code = %q, want bad_request", body.Error.Code)
			}
			for _, name := range tc.names {
				if !strings.Contains(body.Error.Message, name) {
					t.Fatalf("message %q does not name %q", body.Error.Message, name)
				}
			}
		})
	}
}

func TestSeries_EmptyCodeReturns400(t *testing.T) {
	h := &Handler{Log: quietLogger()}
	rec := httptest.NewRecorder()
	h.Series(rec, requestWithCode("/api/v1/series/", ""))
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400", rec.Code)
	}
}

// An unknown code is refused with the standard envelope and never falls back to
// a market symbol: IR_GOLD_18K has prices, but it is not an economic series and
// must not be served through a contract promising reference periods and
// vintages.
func TestUnknownSeries_EnvelopeAndStatus(t *testing.T) {
	rec := httptest.NewRecorder()
	unknownSeries(rec, "IR_GOLD_18K")

	if rec.Code != http.StatusNotFound {
		t.Fatalf("status = %d, want 404", rec.Code)
	}
	body := decodeError(t, rec)
	if body.Error.Code != "not_found" {
		t.Fatalf("code = %q, want not_found", body.Error.Code)
	}
	if !strings.Contains(body.Error.Message, "IR_GOLD_18K") {
		t.Fatalf("message does not name the code: %q", body.Error.Message)
	}
	if body.Error.Details["code"] != "IR_GOLD_18K" {
		t.Fatalf("details = %v", body.Error.Details)
	}
}
