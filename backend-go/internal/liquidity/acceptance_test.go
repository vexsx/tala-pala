package liquidity

// ACCEPTANCE: the research's numbers, rebuilt by this package from the
// research's own data.
//
// testdata/ holds a trimmed copy of the research outputs (make_fixtures.py
// selects rows and columns; it computes nothing): TGJU's 3,973 daily dollar
// closes, CBI's M2 and base money by Jalali month (quarter-ends from tsdview
// 1352..1397, the monthly Excel editions after), and the tables the research
// published -- backtest RMSE and Diebold-Mariano p-values, the band
// quantiles, their calibration, the scenario paths and the liquidity-dollar
// range. Every expected number below is read from those tables, not typed in,
// except the handful the research stated only in its written report, which are
// marked "(report, prose)" where they appear.
//
// The research's cutoff was 2026-10-10 (1405/07/18): the last complete dollar
// month is 1405-06 and the newest M2 month 1404-12. The tests run the package
// at 2026-10-11 against the same data.

import (
	"encoding/json"
	"math"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

// researchNow is the day after the research's last close, in Tehran morning.
var researchNow = time.Date(2026, time.October, 11, 6, 0, 0, 0, time.UTC)

func num(t *testing.T, s string) float64 {
	t.Helper()
	v, err := strconv.ParseFloat(s, 64)
	if err != nil {
		t.Fatalf("not a number: %q", s)
	}
	return v
}

// researchUSD is the daily dollar fixture.
func researchUSD(t *testing.T) []dayClose {
	t.Helper()
	out := []dayClose{}
	for _, r := range readCSV(t, "usd_irt_daily_toman.csv") {
		out = append(out, dayClose{Day: date(r["g"]), Close: num(t, r["close"])})
	}
	return out
}

// researchMoney is M2 and base money as the point-in-time read would hand
// them over: one value per month, every one available long before the cutoff.
func researchMoney(t *testing.T) map[string]moneySeries {
	t.Helper()
	avail := time.Date(2026, time.July, 1, 0, 0, 0, 0, time.UTC)
	named := func(code string) moneySeries {
		s := unregistered(code)
		s.Registered, s.ProviderCode, s.QualityTier = true, "cbi", "official"
		return s
	}
	m2, base, m1, quasi := named(CodeLiquidity), named(CodeBaseMoney), named(CodeM1), named(CodeQuasiMoney)
	for _, r := range readCSV(t, "money_monthly.csv") {
		m, err := ParseMonth(r["jalali_month"])
		if err != nil {
			t.Fatal(err)
		}
		src := "excel"
		if strings.Contains(r["m2_src"], "tsdview") {
			src = "tsdview"
		}
		if r["m2"] != "" {
			m2.Points = append(m2.Points, moneyPoint{Month: m, Value: num(t, r["m2"]), Vintage: 1,
				AvailableAt: avail, Source: src})
		}
		if r["base"] != "" {
			method := methodOld
			if strings.Contains(r["base_method"], "revised") {
				method = methodRevised
			}
			bsrc := "excel"
			if strings.Contains(r["base_src"], "tsdview") {
				bsrc = "tsdview"
			}
			base.Points = append(base.Points, moneyPoint{Month: m, Value: num(t, r["base"]), Vintage: 1,
				AvailableAt: avail, Method: method, Source: bsrc})
		}
		// M1 and quasi-money come from the monthly Excel editions only.
		if r["m1"] != "" {
			m1.Points = append(m1.Points, moneyPoint{Month: m, Value: num(t, r["m1"]), Vintage: 1,
				AvailableAt: avail, Source: "excel"})
		}
		if r["quasi_money"] != "" {
			quasi.Points = append(quasi.Points, moneyPoint{Month: m, Value: num(t, r["quasi_money"]), Vintage: 1,
				AvailableAt: avail, Source: "excel"})
		}
	}
	return map[string]moneySeries{CodeLiquidity: m2, CodeBaseMoney: base, CodeM1: m1, CodeQuasiMoney: quasi}
}

// M2 is M1 plus quasi-money in every month CBI printed all three: a check on
// the fixture as much as on anything, so the page's stacked reading of the
// components is not built on a table that does not add up.
func TestAcceptance_ComponentsAddUp(t *testing.T) {
	money := researchMoney(t)
	m1, quasi := money[CodeM1].byMonth(), money[CodeQuasiMoney].byMonth()
	n := 0
	for _, p := range money[CodeLiquidity].Points {
		a, okA := m1[p.Month]
		b, okB := quasi[p.Month]
		if !okA || !okB {
			continue
		}
		near(t, p.Month.String()+" M1+quasi", a.Value+b.Value, p.Value, 0.15)
		n++
	}
	if n < 90 {
		t.Fatalf("only %d months carry all three", n)
	}
}

func researchPanel(t *testing.T) *panel {
	t.Helper()
	months, _ := bucketUSD(researchUSD(t), tehranToday(researchNow))
	return newPanel(months, researchMoney(t)[CodeLiquidity].Points)
}

func near(t *testing.T, what string, got, want, tol float64) {
	t.Helper()
	if math.IsNaN(got) || math.Abs(got-want) > tol {
		t.Errorf("%s = %.6f, research %.6f (±%g)", what, got, want, tol)
	}
}

func TestAcceptance_PanelMatchesTheResearchGrid(t *testing.T) {
	p := researchPanel(t)
	if got := p.month(p.iFirst).String(); got != "1390-10" {
		t.Errorf("first complete dollar month = %s, research 1390-10 (1390-09 has 16 days)", got)
	}
	if got := p.month(p.iLast).String(); got != "1405-06" {
		t.Errorf("last complete dollar month = %s, research 1405-06 (1405-07 is running)", got)
	}
	// The monthly average and close the research tabulated.
	for _, r := range readCSV(t, "expected_monthly.csv") {
		m, _ := ParseMonth(r["jalali_month"])
		i := p.idx(m)
		if r["usd_partial_month"] == "True" || i < p.iFirst || i > p.iLast {
			continue
		}
		near(t, m.String()+" usd avg", math.Exp(p.avg[i]), num(t, r["usd_avg_toman"]), 0.0006)
		near(t, m.String()+" usd close", math.Exp(p.close[i]), num(t, r["usd_close_toman"]), 1e-6)
	}
}

// The RMSE table and the Diebold-Mariano p-values, both targets.
func TestAcceptance_BacktestReproducesTheResearchTable(t *testing.T) {
	p := researchPanel(t)
	keys := map[string]model{"RW": modelRandomWalk, "DRIFT_all": modelTrend, "M2G": modelM2Paced}
	targets := map[string]target{"avg": targetAvg, "close": targetClose}
	bts := map[string]backtest{}
	for name, tg := range targets {
		bt, ok := p.runBacktest(tg)
		if !ok {
			t.Fatalf("%s: no backtest", name)
		}
		if bt.From.String() != "1394-01" || bt.To.String() != "1405-05" {
			t.Errorf("%s window %s..%s, research 1394-01..1405-05", name, bt.From, bt.To)
		}
		bts[name] = bt
	}
	rows := readCSV(t, "expected_backtest.csv")
	if len(rows) != 24 {
		t.Fatalf("expected_backtest has %d rows", len(rows))
	}
	for _, r := range rows {
		h := int(num(t, r["h"]))
		bt := bts[r["target"]]
		row, ok := bt.Scores[keys[r["model"]]][h]
		what := r["target"] + " " + r["model"] + " h=" + r["h"]
		if !ok {
			t.Errorf("%s: not scored", what)
			continue
		}
		if row.N != int(num(t, r["n"])) {
			t.Errorf("%s: n = %d, research %s", what, row.N, r["n"])
		}
		near(t, what+" RMSE", row.RMSE, num(t, r["rmse"]), 0.00006)
		if r["dm_p_vs_rw"] != "" {
			near(t, what+" DM p vs random walk", row.VsRandomWalk.P, num(t, r["dm_p_vs_rw"]), 0.00006)
		}
	}
	for _, r := range readCSV(t, "expected_dm_vs_trend.csv") {
		h := int(num(t, r["h"]))
		row := bts[r["target"]].Scores[modelM2Paced][h]
		near(t, r["target"]+" M2G vs trend h="+r["h"], row.VsTrend.P, num(t, r["dm_p_vs_trend"]), 0.00006)
		if row.VsTrend.N != int(num(t, r["n"])) {
			t.Errorf("%s h=%d: DM n = %d, research %s", r["target"], h, row.VsTrend.N, r["n"])
		}
	}
	// The report's headline: 0.172/0.150/0.149 at 3 months, monthly average.
	near(t, "avg RW 3m", bts["avg"].Scores[modelRandomWalk][3].RMSE, 0.172, 0.0005)
	near(t, "avg trend 3m", bts["avg"].Scores[modelTrend][3].RMSE, 0.150, 0.0005)
	near(t, "avg M2G 3m", bts["avg"].Scores[modelM2Paced][3].RMSE, 0.149, 0.0005)
}

func TestAcceptance_BandQuantilesMatch(t *testing.T) {
	p := researchPanel(t)
	band := p.band(targetClose, maxHorizon)
	rows := readCSV(t, "expected_band.csv")
	if len(band) != len(rows) {
		t.Fatalf("band has %d horizons, research %d", len(band), len(rows))
	}
	for i, r := range rows {
		b := band[i]
		what := "h=" + r["h"]
		if b.N != int(num(t, r["n"])) {
			t.Errorf("%s n = %d, research %s", what, b.N, r["n"])
		}
		near(t, what+" q05", b.RawLo, num(t, r["q050"]), 0.00006)
		near(t, what+" q95", b.RawHi, num(t, r["q950"]), 0.00006)
		near(t, what+" q05 monotone", b.Lo, num(t, r["q050_mono"]), 0.00006)
		near(t, what+" q95 monotone", b.Hi, num(t, r["q950_mono"]), 0.00006)
	}
	// The production parameters (report, prose): 3 m -0.200/+0.281;
	// 6 m -0.231/+0.480; 12 m -0.268/+0.750; 24 m -0.316/+0.860.
	for h, want := range map[int][2]float64{3: {-0.200, 0.281}, 6: {-0.231, 0.480},
		12: {-0.268, 0.750}, 24: {-0.316, 0.860}} {
		near(t, "prose lo", band[h-1].Lo, want[0], 0.0006)
		near(t, "prose hi", band[h-1].Hi, want[1], 0.0006)
	}
}

// Coverage, measured exactly as the research did (bands from each origin's own
// past errors, NOT widened across horizons): interval_calibration.csv.
func TestAcceptance_CalibrationMatches(t *testing.T) {
	p := researchPanel(t)
	cache := map[string]map[int]coverageCell{}
	for _, r := range readCSV(t, "expected_calibration.csv") {
		tg := targetClose
		if r["target"] == "avg" {
			tg = targetAvg
		}
		nominal := num(t, r["nominal"])
		key := r["target"] + r["nominal"]
		if cache[key] == nil {
			cache[key] = p.coverage(tg, (1-nominal)/2, 1-(1-nominal)/2, false)
		}
		h := int(num(t, r["h"]))
		c := cache[key][h]
		what := r["target"] + " nominal " + r["nominal"] + " h=" + r["h"]
		if c.N != int(num(t, r["n"])) {
			t.Errorf("%s: n = %d, research %s", what, c.N, r["n"])
		}
		near(t, what, c.rate(), num(t, r["coverage"]), 0.0006)
	}
}

func TestAcceptance_ScenariosTrendAndLiquidityDollar(t *testing.T) {
	money := researchMoney(t)
	resp := buildDollarResponse(dollarInput{Now: researchNow, USD: researchUSD(t), M2: money[CodeLiquidity]}, 24)
	var meta struct {
		Anchor float64   `json:"anchor_close_toman"`
		Date   string    `json:"anchor_date"`
		Jalali string    `json:"anchor_jalali"`
		Rates  []float64 `json:"scenario_annual_m2_growth_pct"`
		LD     struct {
			N       float64 `json:"n_bases"`
			M2      float64 `json:"m2_1404_12"`
			Actual  float64 `json:"usd_avg_1404_12"`
			Min     float64 `json:"ld_min"`
			Median  float64 `json:"ld_median"`
			Max     float64 `json:"ld_max"`
			MinBase string  `json:"ld_min_base"`
			MaxBase string  `json:"ld_max_base"`
		} `json:"liquidity_dollar"`
	}
	raw, err := os.ReadFile("testdata/expected_meta.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(raw, &meta); err != nil {
		t.Fatal(err)
	}
	if resp.Anchor == nil || resp.Anchor.CloseToman != meta.Anchor || resp.Anchor.Date != meta.Date ||
		resp.Anchor.Jalali != meta.Jalali {
		t.Fatalf("anchor = %+v, research %v on %s (%s)", resp.Anchor, meta.Anchor, meta.Date, meta.Jalali)
	}
	if len(resp.Scenarios) != 3 {
		t.Fatalf("%d scenarios", len(resp.Scenarios))
	}
	for i, s := range resp.Scenarios {
		near(t, s.Key+" annual growth", s.AnnualGrowthPct, meta.Rates[i], 0.005)
	}
	if b := resp.Scenarios[2].Basis; !strings.Contains(b, "1394-12→1397-12") {
		t.Errorf("slowest three years = %q, research 1394-12..1397-12", b)
	}
	// Paths, which the research rounded to the toman.
	for _, r := range readCSV(t, "expected_scenario_paths.csv") {
		h := int(num(t, r["h"]))
		if h == 0 {
			continue
		}
		for i, col := range []string{"last_12m", "three_year", "slowest_three_year"} {
			pp := resp.Scenarios[i].Path[h-1]
			near(t, col+" path h="+r["h"], pp.Value, num(t, r[col]), 0.51)
			if pp.Period != r["period"] {
				t.Errorf("%s h=%d period %s, research %s", col, h, pp.Period, r["period"])
			}
		}
	}
	// Band edges around the paths, which the research rounded to 100 toman --
	// and computed from the band quantiles AFTER rounding those to four
	// decimals (band_table_m2g_close_monotone.csv), so its edges also carry up
	// to 5e-5 of relative error: 757,700 for S2's 95% edge at 12 months where
	// the unrounded quantile gives 757,643. This side is the unrounded one.
	edgeTol := func(v float64) float64 { return 50.01 + 6e-5*v }
	byLabel := map[string]int{"S1": 0, "S2": 1, "S3": 2}
	for _, r := range readCSV(t, "expected_scenario_band.csv") {
		h := int(num(t, r["h"]))
		if strings.HasPrefix(r["scenario"], "USD own trend") {
			near(t, "trend path h="+r["h"], resp.Trend.Path[h-1].Value, num(t, r["path"]), 50.01)
			continue
		}
		pp := resp.Scenarios[byLabel[r["scenario"][:2]]].Path[h-1]
		near(t, r["scenario"]+" path h="+r["h"], pp.Value, num(t, r["path"]), 50.01)
		near(t, r["scenario"]+" q05 h="+r["h"], *pp.Lo, num(t, r["q05"]), edgeTol(*pp.Lo))
		near(t, r["scenario"]+" q95 h="+r["h"], *pp.Hi, num(t, r["q95"]), edgeTol(*pp.Hi))
	}
	// The trend: 0.02780 a month, 39.6% a year since 1390-10 (report, prose).
	near(t, "trend monthly", resp.Trend.MonthlyLogRate, 0.02780, 0.000005)
	near(t, "trend annual", resp.Trend.AnnualGrowthPct, 39.59, 0.005)
	if resp.Trend.Since != "1390-10" || resp.Trend.Through != "1405-06" {
		t.Errorf("trend %s..%s", resp.Trend.Since, resp.Trend.Through)
	}

	ld := resp.LiquidityDollar
	if ld == nil {
		t.Fatal("no liquidity dollar")
	}
	if ld.Period != "1404-12" || ld.BasesConsidered != int(meta.LD.N) || ld.M2 != meta.LD.M2 {
		t.Errorf("liquidity dollar %s over %d bases at M2 %v; research 1404-12, %v, %v",
			ld.Period, ld.BasesConsidered, ld.M2, meta.LD.N, meta.LD.M2)
	}
	near(t, "actual avg 1404-12", ld.ActualAvg, meta.LD.Actual, 0.006)
	near(t, "ld min", ld.ByBase["min"].Value, meta.LD.Min, 0.06)
	near(t, "ld median", ld.ByBase["median"].Value, meta.LD.Median, 0.006)
	near(t, "ld max", ld.ByBase["max"].Value, meta.LD.Max, 0.06)
	if *ld.ByBase["min"].Base != meta.LD.MinBase || *ld.ByBase["max"].Base != meta.LD.MaxBase {
		t.Errorf("bases %s/%s, research %s/%s", *ld.ByBase["min"].Base, *ld.ByBase["max"].Base,
			meta.LD.MinBase, meta.LD.MaxBase)
	}
	// "The actual dollar was above the liquidity dollar for 117 of the 118
	// bases" (report, prose).
	if ld.BasesBelowActual != 117 {
		t.Errorf("bases below the actual = %d, research 117", ld.BasesBelowActual)
	}
}

// The monthly and annual aggregates against the research's own tables.
func TestAcceptance_AggregatesMatchTheResearchTables(t *testing.T) {
	money := researchMoney(t)
	resp := buildAggregatesResponse(money, "annual", researchNow)
	pts := map[string]map[string]aggregatePoint{}
	for _, s := range resp.Series {
		pts[s.Code] = map[string]aggregatePoint{}
		for _, p := range s.Points {
			pts[s.Code][p.Period] = p
		}
	}
	mult := map[string]float64{}
	for _, m := range resp.Multiplier {
		mult[m.Period] = m.Value
	}
	checked := 0
	for _, r := range readCSV(t, "expected_monthly.csv") {
		per := r["jalali_month"]
		m2, ok := pts[CodeLiquidity][per]
		if !ok {
			continue
		}
		for col, got := range map[string]*float64{"m2_mom_pct": m2.MomPct, "m2_yoy_pct": m2.YoyPct,
			"m2_ytd_since_esfand_pct": m2.YtdPct} {
			if r[col] == "" {
				if col == "m2_ytd_since_esfand_pct" && strings.HasSuffix(per, "-12") {
					continue // the research left Esfand's since-Esfand blank; it equals y/y
				}
				if col == "m2_yoy_pct" && per < "1396-12" {
					continue // the research table starts at 1395-12: no year-earlier row in it
				}
				if got != nil {
					t.Errorf("%s %s = %v, research blank", per, col, *got)
				}
				continue
			}
			if got == nil {
				t.Errorf("%s %s null (%v), research %s", per, col, m2.NullReason, r[col])
				continue
			}
			near(t, per+" "+col, *got, num(t, r[col]), 0.0006)
			checked++
		}
		if r["money_multiplier_m2_over_base"] != "" {
			near(t, per+" multiplier", mult[per], num(t, r["money_multiplier_m2_over_base"]), 0.0006)
		}
		// Base money y/y: equal wherever both months carry one method. Where
		// they do not, the research reached for a value this point-in-time
		// read does not hold -- an old-method FIRST RELEASE (1399-12; 1401
		// months against 1400) or tsdview's revised quarter-end (1401-03/06/09
		// against 1400-03/06/09) -- and this API refuses rather than mixing.
		b := pts[CodeBaseMoney][per]
		if r["base_yoy_pct"] == "" {
			continue
		}
		cur, _ := ParseMonth(per)
		prev := pts[CodeBaseMoney][cur.Add(-12).String()]
		mixed := b.Method == nil || prev.Method == nil || *b.Method != *prev.Method
		switch {
		case mixed && b.YoyPct != nil:
			t.Errorf("%s base y/y = %v across a method change; must be null", per, *b.YoyPct)
		case mixed:
			if !strings.Contains(b.NullReason["yoy_pct"], "method") {
				t.Errorf("%s base y/y reason %q", per, b.NullReason["yoy_pct"])
			}
		case b.YoyPct == nil:
			t.Errorf("%s base y/y null (%v), research %s", per, b.NullReason, r["base_yoy_pct"])
		default:
			near(t, per+" base y/y", *b.YoyPct, num(t, r["base_yoy_pct"]), 0.0006)
		}
	}
	if checked < 200 {
		t.Fatalf("only %d monthly growth figures compared", checked)
	}

	annual := map[int]annualRow{}
	for _, a := range *resp.Annual {
		annual[a.Year] = a
	}
	for _, r := range readCSV(t, "expected_annual.csv") {
		y := int(num(t, r["jalali_year"]))
		a := annual[y]
		near(t, r["jalali_year"]+" M2 Esfand", *a.EndValue[CodeLiquidity], num(t, r["m2_esfand"]), 0.0006)
		near(t, r["jalali_year"]+" M2 Esfand growth", *a.EndGrowthPct[CodeLiquidity], num(t, r["m2_yoy_pct"]), 0.0006)
		got := a.EndGrowthPct[CodeBaseMoney]
		if y == 1399 { // research: first-release old 1399-12 against old 1398-12
			if got != nil {
				t.Errorf("1399 base Esfand growth %v crosses methods; must be null", *got)
			}
			continue
		}
		if got == nil {
			t.Errorf("%d base Esfand growth null, research %s", y, r["base_yoy_pct"])
			continue
		}
		near(t, r["jalali_year"]+" base Esfand growth", *got, num(t, r["base_yoy_pct"]), 0.0006)
	}
	// 1404: 53.3% liquidity, 61.5% base money, multiplier 7.095 (report, prose).
	near(t, "1404 M2", *annual[1404].EndGrowthPct[CodeLiquidity], 53.27, 0.005)
	near(t, "1404 base", *annual[1404].EndGrowthPct[CodeBaseMoney], 61.53, 0.005)
	near(t, "1404-12 multiplier", mult["1404-12"], 7.095, 0.0005)
	if annual[1404].Partial || annual[1404].MonthsPresent != 12 || annual[1396].MonthsPresent != 4 ||
		!annual[1396].Partial {
		t.Errorf("months present 1404=%d partial=%v, 1396=%d partial=%v", annual[1404].MonthsPresent,
			annual[1404].Partial, annual[1396].MonthsPresent, annual[1396].Partial)
	}
}

// The dollars-per-unit-of-liquidity index is base-invariant: its ratios must
// equal the research's (which is 100 at 1397-04; this one is 100 at the first
// month with both).
func TestAcceptance_IndexRatiosMatch(t *testing.T) {
	money := researchMoney(t)
	resp := buildDollarResponse(dollarInput{Now: researchNow, USD: researchUSD(t), M2: money[CodeLiquidity]}, 12)
	if resp.IndexBase == nil || *resp.IndexBase != "1390-12" {
		t.Fatalf("index base %v, want 1390-12 (first complete month with M2)", resp.IndexBase)
	}
	idx := map[string]float64{}
	for _, h := range resp.History {
		if h.UsdPerM2Index != nil {
			idx[h.Period] = *h.UsdPerM2Index
		}
	}
	for _, r := range readCSV(t, "expected_monthly.csv") {
		if r["usd_over_m2_index_1397_04_eq_100"] == "" || r["jalali_month"] == "1405-07" {
			continue
		}
		got := 100 * idx[r["jalali_month"]] / idx["1397-04"]
		near(t, r["jalali_month"]+" index", got, num(t, r["usd_over_m2_index_1397_04_eq_100"]), 0.002)
	}
}
