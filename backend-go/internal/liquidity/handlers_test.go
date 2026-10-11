package liquidity

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

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

func TestParseFreq(t *testing.T) {
	for raw, want := range map[string]string{"": "monthly", "monthly": "monthly", "ANNUAL": "annual"} {
		got, perr := parseFreq(url.Values{"freq": {raw}})
		if perr != nil || got != want {
			t.Errorf("freq=%q -> %q %v", raw, got, perr)
		}
	}
	for _, raw := range []string{"yearly", "quarterly", "1"} {
		if _, perr := parseFreq(url.Values{"freq": {raw}}); perr == nil {
			t.Errorf("freq=%q must be refused, not read as monthly", raw)
		}
	}
}

func TestParseHorizon(t *testing.T) {
	for raw, want := range map[string]int{"": 12, "12": 12, "24": 24} {
		got, perr := parseHorizon(url.Values{"horizon": {raw}})
		if perr != nil || got != want {
			t.Errorf("horizon=%q -> %d %v", raw, got, perr)
		}
	}
	for _, raw := range []string{"6", "36", "12m", "-12"} {
		if _, perr := parseHorizon(url.Values{"horizon": {raw}}); perr == nil {
			t.Errorf("horizon=%q must be refused: the band is measured for 12 and 24 only", raw)
		}
	}
}

// A bad parameter is refused before anything touches the database: the
// handler here has no pool at all.
func TestHandlersRefuseBadParametersFirst(t *testing.T) {
	h := &Handler{Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	for path, fn := range map[string]http.HandlerFunc{
		"/api/v1/liquidity/aggregates?freq=weekly": h.Aggregates,
		"/api/v1/liquidity/dollar?horizon=36":      h.Dollar,
	} {
		rec := httptest.NewRecorder()
		fn(rec, httptest.NewRequest(http.MethodGet, path, nil))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("%s: status %d", path, rec.Code)
		}
		var body httpserver.ErrorBody
		if err := json.NewDecoder(rec.Body).Decode(&body); err != nil || body.Error.Code != "bad_request" ||
			body.Error.Details["supported"] == nil {
			t.Fatalf("%s: body %+v (%v)", path, body, err)
		}
	}
}

func TestRevisionKeyCoversEverythingTheBuildReads(t *testing.T) {
	for _, want := range []string{
		"FROM economic_observations o", "count(*) FILTER (WHERE o.available_at <= $3)", "max(o.id)",
		"i.unit", "s.updated_at", "FROM prices", "symbol = $2 AND quality = 'ok' AND observed_at < $3",
	} {
		if !strings.Contains(revisionKeySelect, want) {
			t.Errorf("revision key lacks %q", want)
		}
	}
}

func detail(label string, v float64, cols map[string]any, url string) economic.ObservationDetail {
	return economic.ObservationDetail{
		Observation: economic.Observation{RefPeriodLabel: label, Value: v, Vintage: 1, AvailableAt: avail},
		Source:      &economic.SourceDocument{URL: url},
		Columns:     cols,
	}
}

func TestToMoneySeries(t *testing.T) {
	prov := economic.SeriesProvenance{Code: CodeBaseMoney, Unit: Unit, NameEN: "Base money", NameFA: "پایه پولی",
		ProviderCode: "cbi", QualityTier: "official"}
	later := detail("1404-12", 21959.3, map[string]any{"method": "revised"}, "https://www.cbi.ir/page/35570.aspx")
	later.AvailableAt = avail.Add(time.Hour)
	later.Source.MediaType = "application/vnd.ms-excel" // what the CBI ingest archives the edition as
	obs := []economic.ObservationDetail{
		detail("1404-12", 21000, nil, ""),
		later, // the same label twice: the later-available one stands
		detail("1399-06", 3720.6, nil, "https://tsdview.cis.cbi.ir/backend/api/value/graph"),
		detail("1400-06", 5189.2, map[string]any{"base_money_method": "pre-revision"}, ""),
		detail("1401-03", 6357.6, nil, ""),
		detail("Q2-2025", 1, nil, ""),
	}
	s := toMoneySeries(CodeBaseMoney, prov, obs)
	if !s.Registered || s.Refused != "" || s.Unreadable != 1 || len(s.Points) != 4 {
		t.Fatalf("%+v", s)
	}
	by := s.byMonth()
	if p := by[NewMonth(1404, 12)]; p.Value != 21959.3 || p.Method != methodRevised || p.MethodInferred ||
		p.Source != "excel" {
		t.Errorf("1404-12 = %+v", p)
	}
	if p := by[NewMonth(1399, 6)]; p.Method != methodOld || !p.MethodInferred || p.Source != "tsdview" {
		t.Errorf("1399-06 = %+v; before the break and unrecorded, it is old by inference", p)
	}
	if p := by[NewMonth(1400, 6)]; p.Method != methodOld || p.MethodInferred {
		t.Errorf("1400-06 = %+v; the stored old method wins", p)
	}
	if p := by[NewMonth(1401, 3)]; p.Method != "" {
		t.Errorf("1401-03 = %+v; printed on both definitions and unrecorded, it is unknown", p)
	}
	if s.Points[0].Month != NewMonth(1399, 6) || s.ProviderCode != "cbi" || s.NameFA != "پایه پولی" {
		t.Errorf("order or provenance wrong: %+v", s.Points[0])
	}
}

// The backfill's reconstructed availability says so on every surface that
// shows available_at; a row that records nothing is unknown (null), not exact.
func TestApproxAvailabilityTravels(t *testing.T) {
	prov := economic.SeriesProvenance{Code: CodeLiquidity, Unit: Unit}
	approx := detail("1404-12", 155812.2, map[string]any{"available_at_approx": true},
		"https://www.cbi.ir/page/35570.aspx")
	older := detail("1404-11", 150000, map[string]any{}, "https://www.cbi.ir/page/35500.aspx")
	s := toMoneySeries(CodeLiquidity, prov, []economic.ObservationDetail{older, approx})
	pts := pointsByPeriod(buildPoints(s))
	if a := pts["1404-12"].AvailableAtApprox; a == nil || !*a {
		t.Errorf("1404-12 approx = %v", a)
	}
	if a := pts["1404-11"].AvailableAtApprox; a != nil {
		t.Errorf("a row that records no flag is unknown, got %v", *a)
	}
	agg := buildAggregatesResponse(map[string]moneySeries{CodeLiquidity: s}, "monthly", researchNow)
	if agg.Latest == nil || agg.Latest.AvailableAtApprox == nil || !*agg.Latest.AvailableAtApprox ||
		agg.DataAge.AvailableAtApprox == nil || !*agg.DataAge.AvailableAtApprox {
		t.Errorf("latest %+v, data_age %+v", agg.Latest, agg.DataAge)
	}
	d := buildDollarResponse(dollarInput{Now: researchNow, USD: researchUSD(t), M2: s}, 12)
	if d.MoneyAsOf == nil || d.MoneyAsOf.AvailableAtApprox == nil || !*d.MoneyAsOf.AvailableAtApprox {
		t.Errorf("money_as_of %+v", d.MoneyAsOf)
	}
	raw, _ := json.Marshal(buildPoints(s))
	if !strings.Contains(string(raw), `"available_at_approx":true`) ||
		!strings.Contains(string(raw), `"available_at_approx":null`) {
		t.Errorf("wire: %s", raw)
	}
}

// The cache key moves at Tehran's midnight (ages) and at UTC's (the running
// dollar month), with no row changing.
func TestRevisionKeyMovesAtBothMidnights(t *testing.T) {
	at := func(h, m int) string { return revisionKey("f", time.Date(2026, 9, 22, h, m, 0, 0, time.UTC)) }
	if at(20, 0) == at(21, 0) {
		t.Error("Tehran's midnight (20:30 UTC) must move the key")
	}
	if at(21, 0) != at(23, 0) {
		t.Error("nothing the answers depend on changes between 21:00 and 23:00 UTC")
	}
	if at(23, 0) == revisionKey("f", time.Date(2026, 9, 23, 0, 30, 0, 0, time.UTC)) {
		t.Error("UTC's midnight must move the key: a dollar month stops running there")
	}
}

func TestToMoneySeriesUnits(t *testing.T) {
	obs := []economic.ObservationDetail{detail("1404-12", 15581.22, nil, "")}
	hamt := toMoneySeries(CodeLiquidity, economic.SeriesProvenance{Unit: "trillion_toman"}, obs)
	if len(hamt.Points) != 1 || hamt.Points[0].Value != 155812.2 || hamt.Points[0].Method != "" {
		t.Fatalf("همت ×10 is thousand billion rial: %+v", hamt.Points)
	}
	odd := toMoneySeries(CodeLiquidity, economic.SeriesProvenance{Unit: "billion_rial"}, obs)
	if odd.Refused == "" || len(odd.Points) != 0 || !strings.Contains(odd.Refused, "billion_rial") {
		t.Fatalf("an unknown unit must be refused, not scaled: %+v", odd)
	}
	resp := buildAggregatesResponse(map[string]moneySeries{CodeLiquidity: odd}, "monthly", researchNow)
	if resp.Series[0].Available || !strings.Contains(*resp.Series[0].UnavailableReason, "billion_rial") {
		t.Errorf("the refusal must reach the payload: %+v", resp.Series[0])
	}
	if names := toMoneySeries(CodeM1, economic.SeriesProvenance{Unit: Unit}, nil); names.NameEN != "Money (M1)" ||
		names.NameFA != "پول" {
		t.Errorf("registry without names falls back to the catalog's: %+v", names)
	}
}

func TestBuildAllBuildsEveryAnswer(t *testing.T) {
	b := buildAll("k", researchMoney(t), researchUSD(t), researchNow)
	if b.key != "k" || b.monthly.Freq != "monthly" || b.annual.Annual == nil || len(b.dollar) != 2 ||
		b.dollar[12].Horizon != 12 || b.dollar[24].Horizon != 24 || !b.dollar[24].Available {
		t.Fatalf("built: monthly %s, annual %v, dollar %d", b.monthly.Freq, b.annual.Annual != nil, len(b.dollar))
	}
	// Both encode: a NaN anywhere would make encoding/json fail and
	// httpserver.JSON would send a truncated body.
	for _, v := range []any{b.monthly, b.annual, b.dollar[12], b.dollar[24]} {
		if _, err := json.Marshal(v); err != nil {
			t.Fatal(err)
		}
	}
}
