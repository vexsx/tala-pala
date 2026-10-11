package liquidity

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestDollarDegradedWithNothingIngested(t *testing.T) {
	resp := buildDollarResponse(dollarInput{Now: researchNow, M2: unregistered(CodeLiquidity)}, 12)
	if resp.Available || len(resp.Unavailable) != 2 || resp.Anchor != nil || resp.Band != nil ||
		resp.Backtest != nil || resp.LiquidityDollar != nil || resp.Trend != nil || resp.MoneyAsOf != nil {
		t.Fatalf("%+v", resp)
	}
	if !strings.Contains(resp.Unavailable[0], "CBI_LIQUIDITY is not registered") ||
		!strings.Contains(resp.Unavailable[1], "USD_IRT") {
		t.Errorf("reasons %v", resp.Unavailable)
	}
	raw, _ := json.Marshal(resp)
	for _, want := range []string{`"history":[]`, `"scenarios":[]`, `"band":null`, `"backtest":null`} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("body lacks %s", want)
		}
	}
	if !resp.DataAge.USD.Stale || !resp.DataAge.Money.Stale {
		t.Errorf("data_age %+v", resp.DataAge)
	}
}

// With the dollar but no M2 yet, the dollar's own history and trend are still
// served -- they need no money data -- and nothing M2-based is.
func TestDollarWithoutM2(t *testing.T) {
	m2 := unregistered(CodeLiquidity)
	m2.Registered = true
	resp := buildDollarResponse(dollarInput{Now: researchNow, USD: researchUSD(t), M2: m2}, 12)
	if resp.Available || resp.Anchor == nil || resp.Trend == nil || len(resp.History) == 0 {
		t.Fatalf("%+v", resp)
	}
	if len(resp.Scenarios) != 0 || resp.Band != nil || resp.Backtest != nil || resp.LiquidityDollar != nil ||
		resp.IndexBase != nil {
		t.Error("nothing M2-based may appear without M2")
	}
	if !strings.Contains(resp.Unavailable[0], "no observation available") {
		t.Errorf("reason %v", resp.Unavailable)
	}
}

func TestDollarHorizon(t *testing.T) {
	money := researchMoney(t)
	in := dollarInput{Now: researchNow, USD: researchUSD(t), M2: money[CodeLiquidity]}
	for _, hz := range []int{12, 24} {
		resp := buildDollarResponse(in, hz)
		if resp.Horizon != hz || len(resp.Band.ByH) != hz || len(resp.Trend.Path) != hz {
			t.Fatalf("horizon %d: band %d, trend %d", hz, len(resp.Band.ByH), len(resp.Trend.Path))
		}
		for _, s := range resp.Scenarios {
			if len(s.Path) != hz || s.Path[hz-1].Lo == nil {
				t.Fatalf("%s path %d long", s.Key, len(s.Path))
			}
		}
		// Measured coverage, not a nominal level; 24 months is published
		// either way so the page can state it.
		if resp.Band.Coverage["1-12"] == nil || resp.Band.Coverage["24"] == nil ||
			*resp.Band.Coverage["24"] >= *resp.Band.Coverage["1-12"] {
			t.Fatalf("coverage %v", resp.Band.Coverage)
		}
		if !strings.Contains(resp.Band.Note, "less often than the 90%") {
			t.Errorf("note: %s", resp.Band.Note)
		}
		if !strings.Contains(resp.Band.Note, "median 12-month error was +0.021") {
			t.Errorf("the skew sentence must come from the measured median: %s", resp.Band.Note)
		}
	}
}

func TestVerdictIsDerivedFromTheNumbers(t *testing.T) {
	p := func(v float64) *float64 { return &v }
	b := &backtestBlock{From: "1394-01", To: "1405-05", Target: "month-end close",
		DMVsRandomWalkP:      map[string]*float64{"3": p(0.0015), "6": p(0.0018), "12": p(0.0007), "24": p(0.00001)},
		DMVsTrendP:           map[string]*float64{"3": p(0.16), "6": p(0.22), "12": p(0.29), "24": p(0.30)},
		DMTrendVsRandomWalkP: map[string]*float64{"3": p(0.004), "6": p(0.002), "12": p(0.0005), "24": p(0.0)},
	}
	v := verdict(b)
	for _, want := range []string{"beat “no change” at every horizon tested (3, 6, 12 and 24 months",
		"did not beat the dollar's own trend at any horizon tested (p = 0.160 to 0.300)",
		"added no measurable skill", "The trend alone beat"} {
		if !strings.Contains(v, want) {
			t.Errorf("verdict lacks %q:\n%s", want, v)
		}
	}
	// If the numbers said M2 helped, the sentence must say so.
	b.DMVsTrendP = map[string]*float64{"3": p(0.01), "6": p(0.02), "12": p(0.03), "24": p(0.04)}
	v = verdict(b)
	if strings.Contains(v, "no measurable skill") || !strings.Contains(v, "was better at every horizon tested") {
		t.Errorf("verdict ignores a significant improvement:\n%s", v)
	}
	b.DMVsTrendP = map[string]*float64{"3": p(0.5), "6": p(0.4), "12": p(0.03), "24": p(0.04)}
	v = verdict(b)
	if !strings.Contains(v, "was better at 12 and 24 months (p ≤ 0.040) but not at 3 and 6 months (p = 0.400 to 0.500)") {
		t.Errorf("partial verdict:\n%s", v)
	}
	b.DMVsRandomWalkP = map[string]*float64{"3": p(0.3), "6": p(0.2), "12": nil, "24": nil}
	if v := verdict(b); !strings.Contains(v, "did not beat “no change” at any horizon tested (p = 0.200 to 0.300)") {
		t.Errorf("failing verdict:\n%s", v)
	}
	if v := verdict(&backtestBlock{}); !strings.Contains(v, "Too few") {
		t.Errorf("empty verdict: %s", v)
	}
}

func TestLiquidityDollarOddMedianNamesItsBase(t *testing.T) {
	start := NewMonth(1400, 1)
	usd := synthUSD(start, 5, func(i int) float64 { return []float64{100, 120, 90, 150, 200}[i] })
	m2 := moneySeries{Code: CodeLiquidity}
	for i, v := range []float64{10, 11, 12, 13, 20} {
		m2.Points = append(m2.Points, moneyPoint{Month: start.Add(i), Value: v})
	}
	p := newPanel(usd, m2.Points)
	ld := buildLiquidityDollar(usd, m2, p)
	if ld == nil || ld.Period != "1400-05" || ld.BasesConsidered != 4 {
		t.Fatalf("%+v", ld)
	}
	// Bases: 100*20/10=200, 120*20/11=218.18, 90*20/12=150, 150*20/13=230.77;
	// the actual 200 is above only the 150 (equal is not above).
	if *ld.ByBase["min"].Base != "1400-03" || ld.ByBase["min"].Value != 150 ||
		*ld.ByBase["max"].Base != "1400-04" || ld.ByBase["median"].Base != nil ||
		len(ld.ByBase["median"].Bases) != 2 || ld.ByBase["median"].Value != 209.09 ||
		ld.BasesBelowActual != 1 {
		t.Fatalf("%+v", ld.ByBase)
	}
	usd = usd[1:]
	m2.Points = m2.Points[1:]
	ld = buildLiquidityDollar(usd, m2, newPanel(usd, m2.Points))
	if ld.BasesConsidered != 3 || ld.ByBase["median"].Base == nil || *ld.ByBase["median"].Base != "1400-02" {
		t.Fatalf("odd median %+v", ld.ByBase["median"])
	}
}

func TestUSDAge(t *testing.T) {
	today := date("2026-10-11")
	if a := buildUSDAge([]dayClose{{date("2026-10-10"), 1}}, today); a.Stale || *a.AgeDays != 1 {
		t.Errorf("%+v", a)
	}
	if a := buildUSDAge([]dayClose{{date("2026-10-01"), 1}}, today); !a.Stale || a.Warning == "" {
		t.Errorf("ten days old must be stale: %+v", a)
	}
}

func TestDollarNotesStateTheBoundaries(t *testing.T) {
	money := researchMoney(t)
	resp := buildDollarResponse(dollarInput{Now: researchNow, USD: researchUSD(t), M2: money[CodeLiquidity]}, 12)
	all := strings.Join(resp.Notes, " ") + resp.LiquidityDollar.Note
	for _, want := range []string{"not forecasts", "Correlation is not causation", "sanctions",
		"multiple exchange rates", "Not a fair value"} {
		if !strings.Contains(all, want) {
			t.Errorf("notes lack %q", want)
		}
	}
	raw, _ := json.Marshal(resp)
	lower := strings.ToLower(string(raw))
	for _, banned := range []string{"undervalued", "overvalued", "equilibrium", "should be", "buy ", "sell "} {
		if strings.Contains(lower, banned) {
			t.Errorf("the response says %q", banned)
		}
	}
}
