package liquidity

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

var avail = time.Date(2026, 6, 1, 0, 0, 0, 0, time.UTC)

func mp(label string, v float64, method string) moneyPoint {
	m, err := ParseMonth(label)
	if err != nil {
		panic(err)
	}
	return moneyPoint{Month: m, Value: v, Vintage: 1, AvailableAt: avail, Method: method, Source: "excel"}
}

func pointsByPeriod(ps []aggregatePoint) map[string]aggregatePoint {
	out := map[string]aggregatePoint{}
	for _, p := range ps {
		out[p.Period] = p
	}
	return out
}

func TestGrowthStepsJalaliLabelsAcrossTheYear(t *testing.T) {
	s := moneySeries{Code: CodeLiquidity, Points: []moneyPoint{
		mp("1403-01", 80, ""), mp("1403-12", 100, ""), mp("1404-01", 104, ""), mp("1404-02", 106.08, ""),
	}}
	got := pointsByPeriod(buildPoints(s))
	f := got["1404-01"]
	if f.MomPct == nil || *f.MomPct != 4 {
		t.Errorf("1404-01 m/m against 1403-12 = %v, want 4", f.MomPct)
	}
	if f.YoyPct == nil || *f.YoyPct != 30 {
		t.Errorf("1404-01 y/y against 1403-01 = %v, want 30", f.YoyPct)
	}
	if f.YtdPct == nil || *f.YtdPct != 4 {
		t.Errorf("1404-01 since Esfand 1403 = %v, want 4", f.YtdPct)
	}
	if o := got["1404-02"]; o.YtdPct == nil || *o.YtdPct != 6.08 || *o.MomPct != 2 {
		t.Errorf("1404-02 = %+v", o)
	}
	if f.GregorianEnd != "2025-04-20" || f.Frequency != "M" || f.Method != nil {
		t.Errorf("1404-01 ends %s (want 2025-04-20), frequency %s, method %v", f.GregorianEnd, f.Frequency, f.Method)
	}
}

func TestMissingComparisonIsNullWithAReason(t *testing.T) {
	q1, q2 := mp("1395-06", 100, ""), mp("1396-06", 120, "")
	q1.Source, q2.Source = "tsdview", "tsdview"
	s := moneySeries{Code: CodeLiquidity, Points: []moneyPoint{q1, q2, mp("1397-04", 130, ""), mp("1397-05", 131, "")}}
	got := pointsByPeriod(buildPoints(s))
	p := got["1396-06"]
	if p.YoyPct == nil || *p.YoyPct != 20 {
		t.Fatalf("y/y = %v, want 20", p.YoyPct)
	}
	if p.MomPct != nil || p.NullReason["mom_pct"] != "1396-05 is not stored, so there is no month-on-month "+
		"comparison (before 1397-04 only quarter-end months are stored)" {
		t.Errorf("m/m = %v, reason %q", p.MomPct, p.NullReason["mom_pct"])
	}
	if p.Frequency != "Q" || got["1397-05"].Frequency != "M" {
		t.Errorf("a tsdview point is quarterly, an Excel one monthly: %s, %s", p.Frequency, got["1397-05"].Frequency)
	}
	if _, ok := p.NullReason["yoy_pct"]; ok {
		t.Error("a computed field must carry no null reason")
	}
	if r := got["1397-05"].NullReason["yoy_pct"]; !strings.Contains(r, "1396-05 is not stored") ||
		!strings.Contains(r, "quarter-end") {
		t.Errorf("1397-05 y/y reason %q", r)
	}
	// A series with no quarterly history says only that the month is missing.
	m1 := moneySeries{Code: CodeM1, Points: []moneyPoint{mp("1398-04", 1, ""), mp("1398-06", 2, "")}}
	if r := pointsByPeriod(buildPoints(m1))["1398-06"].NullReason["mom_pct"]; strings.Contains(r, "quarter-end") {
		t.Errorf("no tsdview history, no quarter-end explanation: %q", r)
	}
}

func TestBaseMoneyNeverCrossesTheMethodBreak(t *testing.T) {
	s := moneySeries{Code: CodeBaseMoney, Points: []moneyPoint{
		mp("1398-12", 3528.5, methodOld), mp("1399-11", 4361.0, methodOld),
		mp("1399-12", 4506.7, methodRevised), mp("1400-01", 4652.5, methodOld),
		mp("1400-12", 5981.1, methodRevised), mp("1401-06", 6862.2, ""),
	}}
	got := pointsByPeriod(buildPoints(s))
	e := got["1399-12"]
	if e.MomPct != nil || e.YoyPct != nil || e.YtdPct != nil {
		t.Fatalf("1399-12 (revised) against old-method months must be null: %+v", e)
	}
	if !strings.Contains(e.NullReason["yoy_pct"], "revised") || !strings.Contains(e.NullReason["yoy_pct"], "old") {
		t.Errorf("reason %q should name both methods", e.NullReason["yoy_pct"])
	}
	if *e.Method != methodRevised {
		t.Errorf("method = %v", *e.Method)
	}
	if g := got["1400-12"]; g.YoyPct == nil || g.YtdPct == nil {
		t.Errorf("1400-12 against 1399-12, both revised, must be computed: %+v", g)
	}
	if g := got["1400-01"]; g.MomPct != nil {
		t.Errorf("1400-01 (old) against 1399-12 (revised): %v", *g.MomPct)
	}
	// An unknown method is not assumed to match.
	if g := got["1401-06"]; g.YtdPct != nil || !strings.Contains(g.NullReason["ytd_pct"], "not recorded") {
		t.Errorf("unknown-method month: %+v", g)
	}
	// The same arithmetic for M2 ignores methods entirely.
	m2 := s
	m2.Code = CodeLiquidity
	if g := pointsByPeriod(buildPoints(m2))["1399-12"]; g.YoyPct == nil {
		t.Error("liquidity has no method break")
	}
}

func TestMultiplier(t *testing.T) {
	m2 := moneySeries{Code: CodeLiquidity, Points: []moneyPoint{mp("1404-11", 146400.3, ""), mp("1404-12", 155812.2, "")}}
	base := moneySeries{Code: CodeBaseMoney, Points: []moneyPoint{mp("1404-12", 21959.3, methodRevised)}}
	got := buildMultiplier(m2, base)
	if len(got) != 1 || got[0].Period != "1404-12" || got[0].Value != 7.095499 || *got[0].BaseMethod != methodRevised {
		t.Fatalf("multiplier = %+v; CBI prints 7.095 for 1404-12 and 1404-11 has no base money here", got)
	}
}

func year(y int, start float64, step float64, method string) []moneyPoint {
	out := []moneyPoint{}
	for m := 1; m <= 12; m++ {
		out = append(out, moneyPoint{Month: NewMonth(y, m), Value: start + step*float64(m-1), Method: method,
			AvailableAt: avail, Vintage: 1})
	}
	return out
}

func TestAnnual(t *testing.T) {
	m2 := moneySeries{Code: CodeLiquidity}
	m2.Points = append(m2.Points, year(1402, 100, 1, "")...)
	m2.Points = append(m2.Points, year(1403, 120, 1, "")...)
	m2.Points = append(m2.Points, mp("1404-01", 140, ""), mp("1404-02", 141, ""))
	base := moneySeries{Code: CodeBaseMoney}
	base.Points = append(base.Points, year(1402, 10, 0.1, methodRevised)...)
	b1403 := year(1403, 12, 0.1, methodRevised)
	b1403[3].Method = methodOld // a stray old-method print inside the year
	base.Points = append(base.Points, b1403...)
	rows := buildAnnual([]moneySeries{m2, base, {Code: CodeM1}, {Code: CodeQuasiMoney}})
	by := map[int]annualRow{}
	for _, r := range rows {
		by[r.Year] = r
	}
	if len(rows) != 3 {
		t.Fatalf("%d years", len(rows))
	}
	r := by[1403]
	if *r.EndValue[CodeLiquidity] != 131 {
		t.Errorf("1403 Esfand = %v, want 131", *r.EndValue[CodeLiquidity])
	}
	near(t, "1403 Esfand over Esfand", *r.EndGrowthPct[CodeLiquidity], 100*(131.0/111-1), 1e-6)
	if *r.AvgValue[CodeLiquidity] != 125.5 {
		t.Errorf("1403 average = %v, want 125.5", *r.AvgValue[CodeLiquidity])
	}
	near(t, "1403 average growth", *r.AvgGrowthPct[CodeLiquidity], 100*(125.5/105.5-1), 1e-6)
	if r.AvgGrowthPct[CodeBaseMoney] != nil || !strings.Contains(r.NullReason[CodeBaseMoney]["avg_growth_pct"], "method") {
		t.Errorf("base money average growth across a stray old print: %v %v", r.AvgGrowthPct[CodeBaseMoney], r.NullReason)
	}
	if r.MonthsPresent != 12 || r.Partial {
		t.Errorf("1403 months %d partial %v", r.MonthsPresent, r.Partial)
	}
	p := by[1404]
	if p.MonthsPresent != 2 || !p.Partial || p.AvgValue != nil || p.EndValue[CodeLiquidity] != nil {
		t.Errorf("1404 = %+v; two months is partial, with no average and no Esfand", p)
	}
	if !strings.Contains(p.NullReason[CodeLiquidity]["avg_value"], "2 of 12") {
		t.Errorf("1404 reason %v", p.NullReason)
	}
	if by[1402].AvgGrowthPct != nil {
		t.Error("1402 has no complete prior year, so no average growth for any series")
	}
	if by[1402].MonthsPresentByCode[CodeBaseMoney] != 12 || by[1402].MonthsPresentByCode[CodeM1] != 0 {
		t.Errorf("months by code %v", by[1402].MonthsPresentByCode)
	}
}

func TestBreaksListOldPrintsAfterTheBreak(t *testing.T) {
	base := moneySeries{Code: CodeBaseMoney, Points: []moneyPoint{
		mp("1399-11", 1, methodOld), mp("1399-12", 1, methodRevised), mp("1400-01", 1, methodOld),
		mp("1400-12", 1, methodRevised),
	}}
	b := buildBreaks(base)
	if len(b) != 1 || b[0].From != "1399-12" || len(b[0].OldMethodAfter) != 1 || b[0].OldMethodAfter[0] != "1400-01" {
		t.Fatalf("breaks = %+v", b)
	}
}

func TestStoredMethod(t *testing.T) {
	cases := []struct {
		cols map[string]any
		want string
	}{
		{map[string]any{"method": "revised"}, methodRevised},
		{map[string]any{"base_money_method": "MFSMCG2016-revised"}, methodRevised},
		{map[string]any{"method": "old"}, methodOld},
		{map[string]any{"base_money_method": "pre-revision"}, methodOld},
		{map[string]any{"metadata": map[string]any{"method": "old"}}, methodOld},
		{map[string]any{"value": 1.0, "notes": "anything"}, ""},
		{map[string]any{"method": 3.0}, ""},
		{map[string]any{}, ""},
	}
	for _, c := range cases {
		if got := storedMethod(c.cols); got != c.want {
			t.Errorf("storedMethod(%v) = %q, want %q", c.cols, got, c.want)
		}
	}
}

func TestEffectiveMethod(t *testing.T) {
	cases := []struct {
		label, stored, want string
		inferred            bool
	}{
		{"1399-11", "", methodOld, true},
		{"1399-12", "", "", false}, // printed on both: unknown
		{"1401-12", "", "", false},
		{"1402-01", "", methodRevised, true},
		{"1400-05", methodOld, methodOld, false}, // stored wins
		{"1398-01", methodRevised, methodRevised, false},
	}
	for _, c := range cases {
		m, _ := ParseMonth(c.label)
		got, inf := effectiveMethod(m, c.stored)
		if got != c.want || inf != c.inferred {
			t.Errorf("%s stored %q -> %q inferred %v, want %q %v", c.label, c.stored, got, inf, c.want, c.inferred)
		}
	}
}

func TestSourceKind(t *testing.T) {
	cases := map[[3]string]string{
		{"https://www.cbi.ir/page/35570.aspx", "140412.xls", "application/vnd.ms-excel"}: "excel",
		{"https://tsdview.cis.cbi.ir/backend/api/value/graph", "", "application/json"}:   "tsdview",
		{"file:///tmp/pol1402,12.xls", "", ""}:                                           "excel",
		{"https://example.org/x", "", "text/html"}:                                       "",
	}
	for in, want := range cases {
		if got := sourceKind(in[0], in[1], in[2]); got != want {
			t.Errorf("sourceKind(%v) = %q, want %q", in, got, want)
		}
	}
}

func TestAggregatesDegradedWhenNothingIsIngested(t *testing.T) {
	series := map[string]moneySeries{}
	for _, c := range seriesOrder {
		series[c] = unregistered(c)
	}
	now := time.Date(2026, 10, 11, 6, 0, 0, 0, time.UTC)
	for _, freq := range []string{"monthly", "annual"} {
		resp := buildAggregatesResponse(series, freq, now)
		if resp.Available || resp.Latest != nil || len(resp.Series) != 4 {
			t.Fatalf("%s: %+v", freq, resp)
		}
		for _, s := range resp.Series {
			if s.Available || s.UnavailableReason == nil || !strings.Contains(*s.UnavailableReason, "not registered") ||
				s.NameFA == "" {
				t.Errorf("%s: %+v", s.Code, s)
			}
		}
		if !resp.DataAge.Stale || resp.DataAge.Warning == "" || resp.DataAge.AgeDays != nil {
			t.Errorf("data_age = %+v; nothing stored is stale with a warning and no age", resp.DataAge)
		}
		raw, err := json.Marshal(resp)
		if err != nil {
			t.Fatal(err)
		}
		body := string(raw)
		for _, want := range []string{`"points":[]`, `"multiplier":[]`, `"latest":null`, `"breaks":[`} {
			if !strings.Contains(body, want) {
				t.Errorf("%s: body lacks %s", freq, want)
			}
		}
		if (freq == "annual") != strings.Contains(body, `"annual":[]`) {
			t.Errorf("%s: annual key presence wrong: %s", freq, body)
		}
	}
}

func TestAggregatesLatestAndAge(t *testing.T) {
	now := time.Date(2026, 10, 11, 6, 0, 0, 0, time.UTC)
	pub := time.Date(2026, 6, 21, 0, 0, 0, 0, time.UTC)
	p := mp("1404-12", 155812.2, "")
	p.PublishedAt, p.SourceURL, p.Vintage = &pub, "https://www.cbi.ir/page/35570.aspx", 2
	series := map[string]moneySeries{CodeLiquidity: {Code: CodeLiquidity, Registered: true, Points: []moneyPoint{p}}}
	resp := buildAggregatesResponse(series, "monthly", now)
	l := resp.Latest
	if l == nil || l.Period != "1404-12" || l.GregorianEnd != "2026-03-20" || l.AgeDays != 205 ||
		*l.SourceURL != p.SourceURL || l.Vintage != 2 || !l.PublishedAt.Equal(pub) {
		t.Fatalf("latest = %+v", l)
	}
	if !resp.DataAge.Stale || *resp.DataAge.AgeDays != 205 || resp.DataAge.ByCode[CodeLiquidity] == nil ||
		resp.DataAge.ByCode[CodeBaseMoney] != nil {
		t.Fatalf("data_age = %+v", resp.DataAge)
	}
	fresh := buildAggregatesResponse(series, "monthly", time.Date(2026, 5, 1, 0, 0, 0, 0, time.UTC))
	if fresh.DataAge.Stale || fresh.DataAge.Warning != "" {
		t.Errorf("42 days after the month ended is not stale: %+v", fresh.DataAge)
	}
	if resp.Annual != nil {
		t.Error("monthly carries no annual table")
	}
}
