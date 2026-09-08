package relvalue

// Every refusal path. The rule this package inherits from candles.go and
// economic/handlers.go is that invalid input earns a specific 400 and is never
// clamped, defaulted or substituted, so each of these tests asserts that a bad
// value is REJECTED rather than that a good value survives.

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

func quietLogger() *slog.Logger { return slog.New(slog.NewTextHandler(io.Discard, nil)) }

func day(y int, m time.Month, d int) time.Time {
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

func values(pairs ...string) url.Values {
	q := url.Values{}
	for i := 0; i+1 < len(pairs); i += 2 {
		q.Set(pairs[i], pairs[i+1])
	}
	return q
}

func decodeError(t *testing.T, rec *httptest.ResponseRecorder) httpserver.ErrorBody {
	t.Helper()
	var body httpserver.ErrorBody
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatalf("response is not the error envelope: %v", err)
	}
	return body
}

// testNow is the clock every pure parser test is handed, so nothing here
// depends on when the suite runs.
var testNow = time.Date(2026, time.September, 9, 12, 30, 0, 0, time.UTC)

// --- numeraire vocabulary ------------------------------------------------------

func TestParseNumeraire(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		want    string
		wantErr bool
	}{
		{"absent means the stored unit", "", "IRT", false},
		{"toman", "IRT", "IRT", false},
		{"dollar", "USD", "USD", false},
		{"grams of gold", "GOLD", "GOLD", false},
		// IRR is a display-only x10 of IRT and must never become a unit of
		// account here: a table half in rial and half in toman is off by ten.
		{"rial refused", "IRR", "", true},
		{"lowercase refused", "usd", "", true},
		{"padded refused", " USD", "", true},
		{"unknown refused", "EUR", "", true},
		{"injection refused", "'; DROP TABLE prices;--", "", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, perr := parseNumeraire(tc.raw)
			if tc.wantErr {
				if perr == nil {
					t.Fatalf("numeraire %q was accepted as %q; it must be refused", tc.raw, got.Key)
				}
				if !strings.Contains(perr.Message, "numeraire must be one of") {
					t.Errorf("message does not name the vocabulary: %q", perr.Message)
				}
				return
			}
			if perr != nil {
				t.Fatalf("numeraire %q refused: %v", tc.raw, perr)
			}
			if got.Key != tc.want {
				t.Errorf("numeraire %q = %q, want %q", tc.raw, got.Key, tc.want)
			}
		})
	}
}

func TestNumeraireUnitsAreSpecific(t *testing.T) {
	gold, _ := lookupNumeraire("GOLD")
	// "gold" would be read as troy ounces of fine metal. The divisor is the
	// toman price of one gram of 18k, so the unit is a count of 18k grams.
	if gold.Unit != "grams of 18k gold" {
		t.Errorf("GOLD unit = %q, want the specific 18k-gram label", gold.Unit)
	}
	irt, _ := lookupNumeraire("IRT")
	if irt.Unit != "toman" {
		t.Errorf("IRT unit = %q, want toman (rial is display-only)", irt.Unit)
	}
}

// --- windows -------------------------------------------------------------------

func TestParseWindow_PeriodVocabulary(t *testing.T) {
	cases := []struct {
		period   string
		wantFrom *time.Time
	}{
		{"1m", ptr(time.Date(2026, time.August, 9, 12, 30, 0, 0, time.UTC))},
		{"3m", ptr(time.Date(2026, time.June, 9, 12, 30, 0, 0, time.UTC))},
		{"6m", ptr(time.Date(2026, time.March, 9, 12, 30, 0, 0, time.UTC))},
		{"1y", ptr(time.Date(2025, time.September, 9, 12, 30, 0, 0, time.UTC))},
		{"3y", ptr(time.Date(2023, time.September, 9, 12, 30, 0, 0, time.UTC))},
		{"5y", ptr(time.Date(2021, time.September, 9, 12, 30, 0, 0, time.UTC))},
		{"10y", ptr(time.Date(2016, time.September, 9, 12, 30, 0, 0, time.UTC))},
		{"max", nil},
	}
	for _, tc := range cases {
		t.Run(tc.period, func(t *testing.T) {
			w, perr := parseWindow(tc.period, "", "", testNow)
			if perr != nil {
				t.Fatalf("period %q refused: %v", tc.period, perr)
			}
			if tc.wantFrom == nil {
				if w.From != nil {
					t.Fatalf("period max resolved a lower bound %v; it must stay open", *w.From)
				}
			} else if w.From == nil || !w.From.Equal(*tc.wantFrom) {
				t.Fatalf("period %q from = %v, want %v", tc.period, w.From, *tc.wantFrom)
			}
			if w.Period == nil || *w.Period != tc.period {
				t.Errorf("period not echoed: %v", w.Period)
			}
			if !w.To.Equal(testNow) {
				t.Errorf("to = %v, want as_of %v", w.To, testNow)
			}
		})
	}
}

func TestParseWindow_DefaultPeriodIsEchoed(t *testing.T) {
	w, perr := parseWindow("", "", "", testNow)
	if perr != nil {
		t.Fatalf("absent period refused: %v", perr)
	}
	if w.Period == nil || *w.Period != "1y" {
		t.Fatalf("default period not echoed as 1y: %v", w.Period)
	}
}

func TestParseWindow_UnknownPeriodRefused(t *testing.T) {
	for _, raw := range []string{"2y", "1M", "1w", "ytd", "forever", "1"} {
		t.Run(raw, func(t *testing.T) {
			_, perr := parseWindow(raw, "", "", testNow)
			if perr == nil {
				t.Fatalf("period %q was accepted; it is outside the vocabulary", raw)
			}
			if !strings.Contains(perr.Message, "period must be one of") {
				t.Errorf("message does not name the vocabulary: %q", perr.Message)
			}
		})
	}
}

func TestParseWindow_ExplicitWindowOverridesPeriod(t *testing.T) {
	w, perr := parseWindow("1y", "2020-01-01", "2021-01-01", testNow)
	if perr != nil {
		t.Fatalf("explicit window refused: %v", perr)
	}
	if w.From == nil || !w.From.Equal(day(2020, time.January, 1)) {
		t.Fatalf("from = %v, want 2020-01-01", w.From)
	}
	if !w.To.Equal(day(2021, time.January, 1)) {
		t.Fatalf("to = %v, want 2021-01-01", w.To)
	}
	// A caller that hands `period` straight back must not earn a 400 for a word
	// outside the vocabulary, so an explicit window reports no period at all.
	if w.Period != nil {
		t.Errorf("period echoed as %q for an explicit window; want null", *w.Period)
	}
}

func TestParseWindow_InvertedWindowRefused(t *testing.T) {
	_, perr := parseWindow("", "2021-01-01", "2020-01-01", testNow)
	if perr == nil {
		t.Fatal("from later than to was accepted; it must be refused, not swapped")
	}
	if !strings.Contains(perr.Message, "from must not be later than to") {
		t.Errorf("unexpected message: %q", perr.Message)
	}
}

func TestParseWindow_EqualBoundsAllowed(t *testing.T) {
	// A single day is a legitimate, if uninformative, request. It is not an
	// error, and the endpoints answer it with nulls and notes rather than a 400.
	if _, perr := parseWindow("", "2024-05-05", "2024-05-05", testNow); perr != nil {
		t.Fatalf("equal bounds refused: %v", perr)
	}
}

func TestParseWindow_FutureToIsCappedAndDeclared(t *testing.T) {
	w, perr := parseWindow("", "2020-01-01", "2030-01-01", testNow)
	if perr != nil {
		t.Fatalf("future `to` refused: %v", perr)
	}
	if !w.To.Equal(testNow) {
		t.Fatalf("to = %v, want the as_of cap %v", w.To, testNow)
	}
	if !w.FutureToRequested {
		t.Error("the cap was applied silently; the response must be able to say so")
	}
}

func TestParseWindow_FromAfterAsOfRefused(t *testing.T) {
	if _, perr := parseWindow("", "2030-01-01", "", testNow); perr == nil {
		t.Fatal("a `from` after as_of was accepted; no observation can exist there")
	}
}

func TestParseWindow_ImplausibleEpochsRefused(t *testing.T) {
	cases := []struct{ name, raw string }{
		// Read as seconds these all land in 1970 and would be answered with a
		// perfectly well-formed window nobody asked for.
		{"gregorian year", "2015"},
		{"jalali year", "1394"},
		{"compact date", "20150909"},
		// Milliseconds read as seconds land in the year 47000.
		{"javascript milliseconds", "1441756800000"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			_, perr := parseWindow("", tc.raw, "", testNow)
			if perr == nil {
				t.Fatalf("from=%q was accepted as an epoch", tc.raw)
			}
			if !strings.Contains(perr.Message, "unix seconds") {
				t.Errorf("message does not explain the accepted forms: %q", perr.Message)
			}
		})
	}
}

func TestParseWindow_AcceptedInstantForms(t *testing.T) {
	want := day(2015, time.September, 9)
	for _, raw := range []string{"2015-09-09", "2015-09-09T00:00:00Z", "1441756800"} {
		t.Run(raw, func(t *testing.T) {
			w, perr := parseWindow("", raw, "", testNow)
			if perr != nil {
				t.Fatalf("from=%q refused: %v", raw, perr)
			}
			if w.From == nil || !w.From.Equal(want) {
				t.Fatalf("from=%q parsed to %v, want %v", raw, w.From, want)
			}
		})
	}
}

func TestParseWindow_GarbageRefused(t *testing.T) {
	if _, perr := parseWindow("", "yesterday", "", testNow); perr == nil {
		t.Fatal("from=yesterday was accepted")
	}
}

// --- relative-value parameters --------------------------------------------------

func TestParseRelativeQuery_BothCodesRequired(t *testing.T) {
	for _, q := range []url.Values{
		values("b", "USD_IRT"),
		values("a", "IR_GOLD_18K"),
		values(),
	} {
		if _, perr := parseRelativeQuery(q, testNow); perr == nil {
			t.Fatalf("query %v was accepted with a missing side", q)
		}
	}
}

func TestParseRelativeQuery_SameCodeRefused(t *testing.T) {
	_, perr := parseRelativeQuery(values("a", "USD_IRT", "b", "USD_IRT"), testNow)
	if perr == nil {
		t.Fatal("a == b was accepted; indexed against itself every series is flat")
	}
	if !strings.Contains(perr.Message, "must be different") {
		t.Errorf("unexpected message: %q", perr.Message)
	}
}

func TestParseRelativeQuery_Points(t *testing.T) {
	cases := []struct {
		raw     string
		want    int
		wantErr bool
	}{
		{"", defaultRelativePoints, false},
		// points=1 cannot carry a first AND a last point, so it is refused
		// rather than quietly served as the most recent observation alone.
		{"1", 0, true},
		{"2", 2, false},
		{"2000", 2000, false},
		{"0", 0, true},
		{"-5", 0, true},
		{"2001", 0, true},
		{"1e3", 0, true},
		{"abc", 0, true},
	}
	for _, tc := range cases {
		t.Run("points="+tc.raw, func(t *testing.T) {
			q, perr := parseRelativeQuery(values("a", "IR_GOLD_18K", "b", "USD_IRT", "points", tc.raw), testNow)
			if tc.wantErr {
				if perr == nil {
					t.Fatalf("points=%q accepted as %d; it must be refused, not clamped", tc.raw, q.Points)
				}
				return
			}
			if perr != nil {
				t.Fatalf("points=%q refused: %v", tc.raw, perr)
			}
			if q.Points != tc.want {
				t.Errorf("points=%q -> %d, want %d", tc.raw, q.Points, tc.want)
			}
		})
	}
}

// --- refusals over HTTP ----------------------------------------------------------

// Every refusal below is written before any statement runs, so a nil pool is
// proof that the handler never reached the database.
func refusalHandler() *Handler { return &Handler{Pool: nil, Log: quietLogger()} }

func TestPerformance_RefusesBeforeTouchingTheDatabase(t *testing.T) {
	cases := []struct{ name, target, want string }{
		{"unknown numeraire", "/api/v1/markets/performance?numeraire=EUR", "numeraire must be one of"},
		{"rial numeraire", "/api/v1/markets/performance?numeraire=IRR", "numeraire must be one of"},
		{"unknown period", "/api/v1/markets/performance?period=2y", "period must be one of"},
		{"inverted window", "/api/v1/markets/performance?from=2025-01-01&to=2024-01-01", "from must not be later than to"},
		{"year as epoch", "/api/v1/markets/performance?from=2015", "unix seconds"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			refusalHandler().Performance(rec, httptest.NewRequest(http.MethodGet, tc.target, nil))
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400", rec.Code)
			}
			body := decodeError(t, rec)
			if body.Error.Code != "bad_request" {
				t.Errorf("error code = %q", body.Error.Code)
			}
			if !strings.Contains(body.Error.Message, tc.want) {
				t.Errorf("message %q does not contain %q", body.Error.Message, tc.want)
			}
		})
	}
}

func TestRelativeValue_RefusesBeforeTouchingTheDatabase(t *testing.T) {
	cases := []struct{ name, target, want string }{
		{"missing a", "/api/v1/relative-value?b=USD_IRT", "a is required"},
		{"missing b", "/api/v1/relative-value?a=IR_GOLD_18K", "b is required"},
		{"same code", "/api/v1/relative-value?a=USD_IRT&b=USD_IRT", "must be different"},
		{"points too large", "/api/v1/relative-value?a=IR_GOLD_18K&b=USD_IRT&points=2001", "points must be between"},
		{"points zero", "/api/v1/relative-value?a=IR_GOLD_18K&b=USD_IRT&points=0", "points must be between"},
		{"points one", "/api/v1/relative-value?a=IR_GOLD_18K&b=USD_IRT&points=1", "points must be between"},
		{"unknown period", "/api/v1/relative-value?a=IR_GOLD_18K&b=USD_IRT&period=ytd", "period must be one of"},
		{"inverted window", "/api/v1/relative-value?a=IR_GOLD_18K&b=USD_IRT&from=2026-01-01&to=2025-01-01", "from must not be later than to"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			rec := httptest.NewRecorder()
			refusalHandler().RelativeValue(rec, httptest.NewRequest(http.MethodGet, tc.target, nil))
			if rec.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400", rec.Code)
			}
			if !strings.Contains(decodeError(t, rec).Error.Message, tc.want) {
				t.Errorf("message does not contain %q", tc.want)
			}
		})
	}
}

// unknownCode, rateCode and unbackedNumeraire are written by the handler after
// the registry read, so they are exercised directly: the point is the envelope
// and the specificity of the message, not the plumbing that reaches them.
func TestRefusalMessagesNameTheProblem(t *testing.T) {
	t.Run("unknown code", func(t *testing.T) {
		rec := httptest.NewRecorder()
		unknownCode(rec, "a", "IR_GOLD_24K")
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("status = %d, want 400", rec.Code)
		}
		body := decodeError(t, rec)
		if !strings.Contains(body.Error.Message, "IR_GOLD_24K") ||
			!strings.Contains(body.Error.Message, "not a registered instrument") {
			t.Errorf("message does not name the code and the problem: %q", body.Error.Message)
		}
	})

	t.Run("disabled is not comparable", func(t *testing.T) {
		// /markets/performance excludes a disabled instrument with a named
		// warning; /relative-value used to compare it without a word.
		rec := httptest.NewRecorder()
		disabledCode(rec, "a", instrumentRow{Code: "RETIRED_SYMBOL", QuoteCurrency: quoteIRT})
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("status = %d, want 400", rec.Code)
		}
		msg := decodeError(t, rec).Error.Message
		if !strings.Contains(msg, "RETIRED_SYMBOL") || !strings.Contains(msg, "DISABLED") {
			t.Errorf("message does not explain the refusal: %q", msg)
		}
	})

	t.Run("rate is not a price", func(t *testing.T) {
		rec := httptest.NewRecorder()
		rateCode(rec, "b", instrumentRow{Code: "US10Y", QuoteCurrency: quotePCT})
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("status = %d, want 400", rec.Code)
		}
		msg := decodeError(t, rec).Error.Message
		if !strings.Contains(msg, "US10Y") || !strings.Contains(msg, "RATE") {
			t.Errorf("message does not explain the refusal: %q", msg)
		}
	})

	t.Run("unbacked numeraire", func(t *testing.T) {
		rec := httptest.NewRecorder()
		spec, _ := lookupNumeraire("GOLD")
		unbackedNumeraire(rec, spec)
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("status = %d, want 400", rec.Code)
		}
		msg := decodeError(t, rec).Error.Message
		if !strings.Contains(msg, goldSeriesCode) {
			t.Errorf("message does not name the missing series: %q", msg)
		}
	})
}

func ptr[T any](v T) *T { return &v }
