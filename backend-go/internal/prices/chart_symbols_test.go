package prices

import (
	"context"
	"errors"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"
)

// The chart's symbol vocabulary: its shapes, and the registry rows that widen
// it without a code change.

func TestClassifyCandleSymbol(t *testing.T) {
	cases := []struct {
		raw       string
		src       candleSource
		code      string
		canonical string
		ok        bool
	}{
		{"IR_GOLD_18K", sourceTicks, "IR_GOLD_18K", "IR_GOLD_18K", true},
		{" ir_silver_999 ", sourceTicks, "IR_SILVER_999", "IR_SILVER_999", true},
		{"IDX:32097828799138957", sourceTSEIndex, "32097828799138957", "IDX:32097828799138957", true},
		{"idx:5798407779416661", sourceTSEIndex, "5798407779416661", "IDX:5798407779416661", true},
		{"EQ:46348559193224090", sourceTSEEquity, "46348559193224090", "EQ:46348559193224090", true},
		{"IDX:", sourceTicks, "", "IDX:", false},
		{"IDX:12ab56789", sourceTicks, "", "IDX:12AB56789", false},
		{"EQ:123456789012345678901", sourceTicks, "", "EQ:123456789012345678901", false},
		{"فولاد", sourceTicks, "", "فولاد", false},
		{"1ABC", sourceTicks, "", "1ABC", false},
		{"", sourceTicks, "", "", false},
	}
	for _, c := range cases {
		src, code, canonical, ok := classifyCandleSymbol(c.raw)
		if src != c.src || code != c.code || canonical != c.canonical || ok != c.ok {
			t.Errorf("classify(%q) = %v %q %q %v, want %v %q %q %v",
				c.raw, src, code, canonical, ok, c.src, c.code, c.canonical, c.ok)
		}
	}
}

func TestEveryCanonicalSymbolHasATickShape(t *testing.T) {
	// The drawing validator and the registry gate both require the shape; a
	// canonical symbol that failed it would silently stop being chartable.
	for symbol := range KnownSymbols {
		if !tickSymbolRE.MatchString(symbol) || IsTehranChartSymbol(symbol) {
			t.Errorf("%s does not have the shape of a registry code", symbol)
		}
	}
}

func registryFixture() []registryRow {
	row := func(code, kind, domain, quote string, enabled bool) registryRow {
		return registryRow{RegistrySymbol: RegistrySymbol{Code: code, Kind: kind, Domain: domain,
			QuoteCurrency: quote}, Enabled: enabled}
	}
	return []registryRow{
		row("IR_GOLD_18K", "market_price", "gold", "IRT", true),
		row("IR_SILVER_999", "market_price", "silver", "IRT", true),
		row("IR_COIN_BAHAR", "market_price", "gold", "IRT", true),
		row("IR_SILVER_FUND_SILVER", "market_price", "fund", "IRT", true),
		row("USD_IRT", "fx", "fx", "IRT", true),
		row("BRENT_OIL", "market_price", "global", "USD", true),
		// A flow ratio in percent: not a price, whatever the table calls it.
		row("IR_GOLD_FUND_FLOW", "index", "fund", "PCT", true),
		row("US10Y", "market_price", "global", "PCT", true),
		row("DXY", "index", "global", "INDEX", true),
		row("CPI_IR", "economic_series", "macro", "INDEX", true),
		// Disabled by an operator: gone from the chart.
		row("IR_SAFFRON_FUND_SAFRON", "market_price", "fund", "IRT", false),
	}
}

func TestTheRegistryOffersOnlyPricesThatCanBeDrawn(t *testing.T) {
	got := tickSymbolsFrom(registryFixture())
	want := []string{"BRENT_OIL", "IR_COIN_BAHAR", "IR_GOLD_18K", "IR_SILVER_999",
		"IR_SILVER_FUND_SILVER", "USD_IRT"}
	if len(got) != len(want) {
		t.Fatalf("chartable = %v, want %v", got, want)
	}
	for _, code := range want {
		if _, ok := got[code]; !ok {
			t.Errorf("%s should be chartable", code)
		}
	}
	for _, code := range []string{"IR_GOLD_FUND_FLOW", "US10Y", "DXY", "CPI_IR", "IR_SAFFRON_FUND_SAFRON"} {
		if _, ok := got[code]; ok {
			t.Errorf("%s must not be offered as a chartable price", code)
		}
	}
}

func TestTheRegistryIsReadOncePerTTLAndAFailureIsNotCached(t *testing.T) {
	loads := 0
	fail := false
	clock := time.Date(2026, 9, 29, 8, 0, 0, 0, time.UTC)
	reg := newInstrumentRegistry(func(context.Context) ([]registryRow, error) {
		loads++
		if fail {
			return nil, errors.New("db down")
		}
		return registryFixture(), nil
	}, registryTTL, func() time.Time { return clock })

	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if set, err := reg.TickSymbols(ctx); err != nil || len(set) != 6 {
			t.Fatalf("read %d: %v %v", i, len(set), err)
		}
	}
	if loads != 1 {
		t.Fatalf("loads = %d within the TTL, want 1", loads)
	}

	clock = clock.Add(registryTTL + time.Second)
	fail = true
	if _, err := reg.TickSymbols(ctx); err == nil {
		t.Fatal("a failed read after expiry must be reported")
	}
	fail = false
	if _, err := reg.TickSymbols(ctx); err != nil || loads != 3 {
		t.Fatalf("the failure was cached: loads %d, err %v", loads, err)
	}
}

type fakeRegistry struct {
	set   map[string]RegistrySymbol
	err   error
	calls int
}

func (f *fakeRegistry) TickSymbols(context.Context) (map[string]RegistrySymbol, error) {
	f.calls++
	return f.set, f.err
}

func TestTheCanonicalSymbolsNeverWaitOnTheRegistry(t *testing.T) {
	reg := &fakeRegistry{err: errors.New("db down")}
	h := &Handler{Log: quietLogger(), Registry: reg}
	for symbol := range KnownSymbols {
		if ok, err := h.tickSymbolServed(context.Background(), symbol); !ok || err != nil {
			t.Fatalf("%s: %v %v", symbol, ok, err)
		}
	}
	if reg.calls != 0 {
		t.Fatalf("the registry was read %d times for canonical symbols", reg.calls)
	}
	// A garbage string costs no read either.
	if ok, err := h.tickSymbolServed(context.Background(), "not a code"); ok || err != nil || reg.calls != 0 {
		t.Fatalf("garbage: %v %v calls=%d", ok, err, reg.calls)
	}
	if _, err := h.tickSymbolServed(context.Background(), "IR_SILVER_999"); err == nil {
		t.Fatal("a registry failure must be reported, not read as unknown")
	}
}

func TestARegistryCodeChartsWithoutACodeChange(t *testing.T) {
	served := func(s string) bool { return KnownSymbols[s] || s == "IR_SILVER_999" }
	q, perr := parseCandleQueryFor(url.Values{"symbol": {"ir_silver_999"}}, served)
	if perr != nil || q.Symbol != "IR_SILVER_999" || q.Source != sourceTicks {
		t.Fatalf("%+v %v", q, perr)
	}
	if _, perr := parseCandleQuery(url.Values{"symbol": {"IR_SILVER_999"}}); perr == nil {
		t.Fatal("without the registry the code is unknown")
	}

	// Through the handler: the registry admits the symbol, so the request
	// gets as far as its interval — refused before any database is touched.
	h := &Handler{Log: quietLogger(), Registry: &fakeRegistry{set: tickSymbolsFrom(registryFixture())}}
	rec, body := serveCandles(t, h, "symbol=IR_SILVER_999&interval=1m")
	msg := body["error"].(map[string]any)["message"].(string)
	if rec.Code != http.StatusBadRequest || !strings.HasPrefix(msg, "interval must be one of") {
		t.Fatalf("%d %q", rec.Code, msg)
	}
	rec, body = serveCandles(t, h, "symbol=IR_GOLD_FUND_FLOW_X")
	if rec.Code != http.StatusBadRequest || body["error"].(map[string]any)["message"] != "unknown symbol" {
		t.Fatalf("an unregistered code: %d %v", rec.Code, body)
	}

	// A registry that cannot be read is a server fault, never "unknown symbol".
	down := &Handler{Log: quietLogger(), Registry: &fakeRegistry{err: errors.New("db down")}}
	if rec, _ := serveCandles(t, down, "symbol=IR_SILVER_999"); rec.Code != http.StatusInternalServerError {
		t.Fatalf("status %d, want 500", rec.Code)
	}
}
