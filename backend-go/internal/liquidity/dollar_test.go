package liquidity

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
	"time"
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
		// Measured coverage, not a nominal level, for every range whatever the
		// horizon asked for, so the page can state the longer one beside the
		// 24-month choice. Which way the numbers point is the data's business
		// (TestCoverageIsMeasuredOnThePublishedBand recounts them); the note
		// must say each one and name exactly those below what the quantiles
		// span.
		for _, key := range []string{"1-12", "13-24", "24"} {
			c := resp.Band.Coverage[key]
			if c == nil || *c < 0 || *c > 1 || resp.Band.CoverageN[key] == 0 {
				t.Fatalf("coverage %s = %v over %d", key, c, resp.Band.CoverageN[key])
			}
			if !strings.Contains(resp.Band.Note, pct(*c)) {
				t.Errorf("note does not state the %s coverage %s: %s", key, pct(*c), resp.Band.Note)
			}
		}
		if !strings.Contains(resp.Band.Note, "independent outcomes") {
			t.Errorf("the 24-month figure must say how few outcomes it rests on: %s", resp.Band.Note)
		}
		if !strings.Contains(resp.Band.Note, "median 12-month error was +0.021") {
			t.Errorf("the skew sentence must come from the measured median: %s", resp.Band.Note)
		}
	}
}

// The band note names exactly the ranges whose measured coverage fell short
// of what the quantiles span -- never one sentence over a rate that did not.
func TestBandNoteNamesOnlyTheRangesBelowNominal(t *testing.T) {
	band := make([]bandStep, maxHorizon)
	for i := range band {
		band[i] = bandStep{H: i + 1, Lo: -0.1, Hi: 0.1, Median: 0}
	}
	cells := func(short, long coverageCell) map[int]coverageCell {
		out := map[int]coverageCell{}
		for h := 1; h <= maxHorizon; h++ {
			if h <= 12 {
				out[h] = short
			} else {
				out[h] = long
			}
		}
		return out
	}
	// 80% within a year, 92% beyond it.
	b := buildBandBlock(band, cells(coverageCell{Hits: 8, N: 10}, coverageCell{Hits: 92, N: 100}), 24)
	if !strings.Contains(b.Note, "80% of the time at 1–12 months, 92% at 13–24 months and 92% at 24 months") ||
		!strings.Contains(b.Note, "At 1–12 months that is less often than the 90%") ||
		strings.Contains(b.Note, "At 1–12 months and") {
		t.Errorf("note: %s", b.Note)
	}
	// Every range short: all three named.
	b = buildBandBlock(band, cells(coverageCell{Hits: 8, N: 10}, coverageCell{Hits: 7, N: 10}), 24)
	if !strings.Contains(b.Note, "At 1–12 months, 13–24 months and 24 months that is less often") {
		t.Errorf("note: %s", b.Note)
	}
	// None short: no shortfall sentence at all.
	b = buildBandBlock(band, cells(coverageCell{Hits: 9, N: 10}, coverageCell{Hits: 95, N: 100}), 24)
	if strings.Contains(b.Note, "less often") {
		t.Errorf("note claims a shortfall the numbers do not show: %s", b.Note)
	}
}

// The page reads data_age.stale / age_days / warning at the top of the
// dollar response, about the close every path starts from.
func TestDollarDataAgeTopLevelIsTheAnchors(t *testing.T) {
	stale := buildDollarResponse(dollarInput{Now: researchNow, USD: []dayClose{{date("2026-09-01"), 100}},
		M2: unregistered(CodeLiquidity)}, 12)
	raw, err := json.Marshal(stale.DataAge)
	if err != nil {
		t.Fatal(err)
	}
	var top map[string]any
	if err := json.Unmarshal(raw, &top); err != nil {
		t.Fatal(err)
	}
	if top["stale"] != true || top["age_days"] != 40.0 || top["newest_close"] != "2026-09-01" ||
		!strings.Contains(top["warning"].(string), "stale price") || top["money"] == nil || top["usd"] == nil {
		t.Fatalf("data_age %s", raw)
	}
	fresh := buildDollarResponse(dollarInput{Now: researchNow, USD: researchUSD(t),
		M2: researchMoney(t)[CodeLiquidity]}, 12)
	// Fresh dollar, old money: the top level is the dollar's, the money's
	// staleness stays under money.
	if fresh.DataAge.Stale || *fresh.DataAge.AgeDays != 1 || !fresh.DataAge.Money.Stale {
		t.Fatalf("data_age %+v", fresh.DataAge)
	}
}

func TestVerdictIsDerivedFromTheNumbers(t *testing.T) {
	p := func(v float64) *float64 { return &v }
	b := &backtestBlock{From: "1394-01", To: "1405-05", OutcomesThrough: "1405-06", Target: "month-end close",
		RMSE: map[string]map[string]*float64{
			"m2_paced":     {"3": p(0.16), "6": p(0.2362), "12": p(0.3391), "24": p(0.4183)},
			"dollar_trend": {"3": p(0.1613), "6": p(0.2388), "12": p(0.3464), "24": p(0.4451)},
		},
		DMVsRandomWalkP:      map[string]*float64{"3": p(0.0015), "6": p(0.0018), "12": p(0.0007), "24": p(0.00001)},
		DMVsTrendP:           map[string]*float64{"3": p(0.186), "6": p(0.22), "12": p(0.29), "24": p(0.303)},
		DMTrendVsRandomWalkP: map[string]*float64{"3": p(0.004), "6": p(0.002), "12": p(0.0005), "24": p(0.0)},
	}
	v := verdict(b)
	for _, want := range []string{"origins from 1394-01 with outcomes known by 1405-06",
		"beat “no change” at every horizon tested (3, 6, 12 and 24 months",
		// A failure to reject, said as one, with the errors that were measured:
		// the M2-paced RMSE was lower at every horizon, by too little to tell
		// from chance -- never "added no skill".
		"against the dollar's own trend it was not measurably better at any horizon tested " +
			"(RMSE 0.8% to 6.0% lower, one-sided p = 0.186 to 0.303)",
		"The trend alone beat"} {
		if !strings.Contains(v, want) {
			t.Errorf("verdict lacks %q:\n%s", want, v)
		}
	}
	for _, overclaim := range []string{"no measurable skill", "added no", "did not beat the dollar's own trend"} {
		if strings.Contains(v, overclaim) {
			t.Errorf("verdict says more than a failure to reject: %q in\n%s", overclaim, v)
		}
	}
	// Higher at some horizons and lower at others: the span is stated.
	b.RMSE["m2_paced"]["3"] = p(0.17)
	if v := verdict(b); !strings.Contains(v, "RMSE between 6.0% lower and 5.4% higher") {
		t.Errorf("mixed RMSE:\n%s", v)
	}
	b.RMSE["m2_paced"]["3"] = p(0.16)
	// If the numbers said M2 helped, the sentence must say so.
	b.DMVsTrendP = map[string]*float64{"3": p(0.01), "6": p(0.02), "12": p(0.03), "24": p(0.04)}
	v = verdict(b)
	if strings.Contains(v, "not measurably better") || !strings.Contains(v, "was better at every horizon tested") {
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
	// "fair value" appears only negated, as "not a fair value": the page's
	// language check reads the served notes as they are.
	if n := strings.Count(lower, "fair value"); n != strings.Count(lower, "not a fair value") {
		t.Errorf("the response mentions a fair value other than to deny one: %s", raw)
	}
}

// The rerun at a six-month lag is compared horizon by horizon, and the note
// says whether the published conclusions moved.
func TestLagCheckSaysWhetherTheConclusionsMove(t *testing.T) {
	p := func(v float64) *float64 { return &v }
	pub := &backtestBlock{
		DMVsRandomWalkP: map[string]*float64{"3": p(0.001), "6": p(0.002), "12": p(0.001), "24": p(0.001)},
		DMVsTrendP:      map[string]*float64{"3": p(0.19), "6": p(0.22), "12": p(0.29), "24": p(0.30)},
	}
	late := &backtestBlock{
		RMSE: map[string]map[string]*float64{
			"m2_paced":     {"3": p(0.161), "6": p(0.24), "12": p(0.34), "24": p(0.42)},
			"dollar_trend": {"3": p(0.1613), "6": p(0.2388), "12": p(0.3464), "24": p(0.4451)},
		},
		DMVsRandomWalkP: map[string]*float64{"3": p(0.002), "6": p(0.002), "12": p(0.001), "24": p(0.001)},
		DMVsTrendP:      map[string]*float64{"3": p(0.27), "6": p(0.3), "12": p(0.33), "24": p(0.38)},
	}
	lc := buildLagCheck(pub, late)
	if lc.LagMonths != 6 || !lc.SameConclusions || lc.RMSE["24"] == nil ||
		!strings.Contains(lc.Note, "usable only 6 months after it ended") ||
		!strings.Contains(lc.Note, "do not rest on the 3-month assumption") {
		t.Fatalf("%+v", lc)
	}
	late.DMVsTrendP["24"] = p(0.01)
	if lc := buildLagCheck(pub, late); lc.SameConclusions || !strings.Contains(lc.Note, "differs from the result at 3 months") {
		t.Fatalf("a moved horizon must be said: %+v", lc)
	}
}

// On the research data the published backtest carries its rerun, and its
// window says where outcomes stop.
func TestBacktestCarriesTheLagCheck(t *testing.T) {
	money := researchMoney(t)
	resp := buildDollarResponse(dollarInput{Now: researchNow, USD: researchUSD(t), M2: money[CodeLiquidity]}, 12)
	bt := resp.Backtest
	if bt == nil || bt.LagCheck == nil || bt.OutcomesThrough != "1405-06" || bt.LagCheck.LagMonths != checkLagMonths {
		t.Fatalf("backtest %+v", bt)
	}
	for _, h := range []string{"3", "6", "12", "24"} {
		if bt.LagCheck.DMVsTrendP[h] == nil || bt.LagCheck.DMVsRandomWalkP[h] == nil {
			t.Errorf("lag check at %s months: %+v", h, bt.LagCheck)
		}
	}
	if !strings.Contains(bt.Note, "independent outcomes") || strings.Contains(bt.Note, "about five") {
		t.Errorf("the overlap sentence is computed from n_origins: %s", bt.Note)
	}
	if want := independentOutcomes(bt.NOrigins["24"], 24); !strings.Contains(bt.Note, fmt.Sprintf("about %d independent", want)) {
		t.Errorf("note %q, want about %d", bt.Note, want)
	}
	if c := resp.Band.CoverageIndependent; c["24"] != independentOutcomes(resp.Band.CoverageN["24"], 24) || len(c) != 1 {
		t.Errorf("coverage_independent %v over %v", c, resp.Band.CoverageN)
	}
}

// Where USD_IRT changes definition is read from the splice register, never
// written into a sentence: with no entry no date is claimed, and with one the
// notes name its date and both definitions.
func TestDollarNotesFollowTheSpliceRegister(t *testing.T) {
	money := researchMoney(t)
	in := dollarInput{Now: researchNow, USD: researchUSD(t), M2: money[CodeLiquidity]}
	none := buildDollarResponse(in, 12)
	raw, _ := json.Marshal(none)
	if strings.Contains(string(raw), "since 2022-04") || strings.Contains(string(raw), "USDT") ||
		none.Anchor.Note != "free-market rate" ||
		!strings.Contains(strings.Join(none.Notes, " "), "records no change of its definition") {
		t.Errorf("no register entry, no splice claim: anchor %q", none.Anchor.Note)
	}
	sp, err := parseSplice([]byte(`{"splice_at": "2026-10-11T09:18:40.668349+00:00",
		"backfill_definition": "one daily close of TGJU's price_dollar_rl", "live_definition": "the 24/7 USDT/toman market"}`))
	if err != nil || !sp.At.Equal(time.Date(2026, 10, 11, 9, 18, 40, 668349000, time.UTC)) {
		t.Fatalf("parse %v %+v", err, sp)
	}
	in.Splice = sp
	// Every stored close (TGJU's, to 2026-10-10) is before the splice day.
	before := buildDollarResponse(in, 12)
	if before.Anchor.Note != "free-market rate: one daily close of TGJU's price_dollar_rl (USD_IRT's definition until 2026-10-10)" {
		t.Errorf("anchor before the splice: %q", before.Anchor.Note)
	}
	notes := strings.Join(before.Notes, " ")
	if !strings.Contains(notes, "USD_IRT changes definition on 2026-10-11 (the splice register): before it, one daily close") ||
		!strings.Contains(notes, "from it, the 24/7 USDT/toman market") {
		t.Errorf("notes %s", notes)
	}
	in.USD = append(append([]dayClose{}, in.USD...), dayClose{date("2026-10-11"), 264630})
	in.Now = time.Date(2026, 10, 11, 12, 0, 0, 0, time.UTC)
	if a := buildDollarResponse(in, 12).Anchor; a.CloseToman != 264630 ||
		a.Note != "free-market rate: the 24/7 USDT/toman market (USD_IRT's definition since 2026-10-11)" {
		t.Errorf("anchor after the splice: %+v", a)
	}
	// Production's register: the join in 2022-04 is read, not assumed.
	prod, _ := parseSplice([]byte(`{"splice_at": "2022-04-09T10:00:00+00:00"}`))
	if n := anchorNote(prod, date("2026-10-10")); !strings.Contains(n, "since 2022-04-09") || !strings.Contains(n, "the live collection") {
		t.Errorf("production-shaped register: %q", n)
	}
}

func TestParseSplice(t *testing.T) {
	for _, raw := range []string{"", "null"} {
		if sp, err := parseSplice([]byte(raw)); sp != nil || err != nil {
			t.Errorf("%q: %+v %v", raw, sp, err)
		}
	}
	for _, raw := range []string{`{"splice_at": "yesterday"}`, `{}`, `[1]`} {
		if sp, err := parseSplice([]byte(raw)); sp != nil || err == nil {
			t.Errorf("%q must be refused, not guessed: %+v", raw, sp)
		}
	}
}

// What the backtest can say about M2's revisions is read from the vintages.
func TestDollarNotesCountM2Revisions(t *testing.T) {
	m2 := moneySeries{Code: CodeLiquidity, Points: []moneyPoint{mp("1404-11", 1, ""), mp("1404-12", 2, "")}}
	if n := strings.Join(dollarNotes(nil, m2), " "); !strings.Contains(n, "no stored M2 month has been revised") ||
		!strings.Contains(n, "21 to 164 days") {
		t.Errorf("%s", n)
	}
	m2.Points[1].Vintage = 2
	if n := strings.Join(dollarNotes(nil, m2), " "); !strings.Contains(n, "1 stored M2 month(s) were revised") {
		t.Errorf("%s", n)
	}
}
