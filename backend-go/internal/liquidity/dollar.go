package liquidity

// GET /api/v1/liquidity/dollar, built: the dollar monthly beside M2, the
// scenario paths, the dollar's own trend, the band with its measured coverage,
// the backtest that says what all of it is worth, and the "liquidity dollar"
// as a range that shows how much its base month decides.
//
// Everything is a pure function of the stored inputs and the clock, so the
// whole response is unit-tested and the acceptance test can rebuild the
// research's numbers from its own data.

import (
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
)

// dollarInput is everything the dollar response is computed from.
type dollarInput struct {
	Now time.Time
	// USD is USD_IRT's daily closes in toman, up to Now.
	USD []dayClose
	M2  moneySeries
}

type anchorBlock struct {
	Date       string  `json:"date"`
	Jalali     string  `json:"jalali"`
	CloseToman float64 `json:"close_toman"`
	Source     string  `json:"source"`
	Note       string  `json:"note"`
}

type moneyAsOfBlock struct {
	Period       string     `json:"period"`
	GregorianEnd string     `json:"gregorian_end"`
	PublishedAt  *time.Time `json:"published_at"`
	AvailableAt  time.Time  `json:"available_at"`
	Vintage      int        `json:"vintage"`
}

type historyPoint struct {
	Period       string  `json:"period"`
	GregorianEnd string  `json:"gregorian_end"`
	UsdAvg       float64 `json:"usd_avg"`
	UsdClose     float64 `json:"usd_close"`
	UsdCloseDate string  `json:"usd_close_date"`
	Days         int     `json:"days"`
	// Partial is true for a month the model does not use (still running, or
	// not covered from its start); PartialReason says which.
	Partial       bool     `json:"partial"`
	PartialReason *string  `json:"partial_reason"`
	M2            *float64 `json:"m2"`
	UsdPerM2Index *float64 `json:"usd_per_m2_index"`
}

type scenarioBlock struct {
	Key             string      `json:"key"`
	LabelEN         string      `json:"label_en"`
	LabelFA         string      `json:"label_fa"`
	AnnualGrowthPct float64     `json:"annual_growth_pct"`
	Basis           string      `json:"basis"`
	From            string      `json:"from"`
	To              string      `json:"to"`
	Path            []pathPoint `json:"path"`
}

type trendBlock struct {
	AnnualGrowthPct float64     `json:"annual_growth_pct"`
	MonthlyLogRate  float64     `json:"monthly_log_rate"`
	Since           string      `json:"since"`
	Through         string      `json:"through"`
	Path            []pathPoint `json:"path"`
	Note            string      `json:"note"`
}

type bandStepWire struct {
	H     int     `json:"h"`
	Lo    float64 `json:"lo"`
	Hi    float64 `json:"hi"`
	RawLo float64 `json:"raw_lo"`
	RawHi float64 `json:"raw_hi"`
	N     int     `json:"n"`
}

type bandBlock struct {
	LowerQ    float64        `json:"lower_q"`
	UpperQ    float64        `json:"upper_q"`
	ByH       []bandStepWire `json:"by_h"`
	AppliedTo string         `json:"applied_to"`
	Target    string         `json:"target"`
	// Coverage is the measured hit rate of a band built this way, pooled over
	// horizons 1-12 and 13-24, and at 24 alone; CoverageN the (origin,
	// horizon) pairs each rate is over. A rate with nothing to measure is null.
	Coverage    map[string]*float64 `json:"coverage"`
	CoverageN   map[string]int      `json:"coverage_n"`
	OriginsFrom string              `json:"origins_from"`
	LagMonths   int                 `json:"lag_months"`
	NByH        map[string]int      `json:"n_by_h"`
	Note        string              `json:"note"`
}

type backtestBlock struct {
	Target    string                         `json:"target"`
	From      string                         `json:"from"`
	To        string                         `json:"to"`
	LagMonths int                            `json:"lag_months"`
	NOrigins  map[string]int                 `json:"n_origins"`
	RMSE      map[string]map[string]*float64 `json:"rmse"`
	// DMVsRandomWalkP and DMVsTrendP are one-sided p-values that the
	// M2-paced path's squared error is smaller than the random walk's / the
	// dollar trend's. DMTrendVsRandomWalkP is the same for the trend.
	DMVsRandomWalkP      map[string]*float64 `json:"dm_vs_random_walk_p"`
	DMVsTrendP           map[string]*float64 `json:"dm_vs_trend_p"`
	DMTrendVsRandomWalkP map[string]*float64 `json:"dm_trend_vs_random_walk_p"`
	Significance         float64             `json:"significance"`
	Verdict              string              `json:"verdict"`
	Note                 string              `json:"note"`
}

type ldEntry struct {
	// Base is the base month that gives this value; for a median of an even
	// number of bases it is null and Bases names the two middle ones.
	Base  *string  `json:"base"`
	Bases []string `json:"bases,omitempty"`
	Value float64  `json:"value"`
}

type liquidityDollarBlock struct {
	Period           string             `json:"period"`
	M2               float64            `json:"m2"`
	ActualAvg        float64            `json:"actual_avg"`
	ByBase           map[string]ldEntry `json:"by_base"`
	BasesConsidered  int                `json:"bases_considered"`
	BasesFrom        string             `json:"bases_from"`
	BasesTo          string             `json:"bases_to"`
	BasesBelowActual int                `json:"bases_below_actual"`
	Note             string             `json:"note"`
}

type usdAge struct {
	NewestDay      *string `json:"newest_day"`
	AgeDays        *int    `json:"age_days"`
	Stale          bool    `json:"stale"`
	StaleAfterDays int     `json:"stale_after_days"`
	Warning        string  `json:"warning,omitempty"`
}

type dollarAge struct {
	Money moneyAge `json:"money"`
	USD   usdAge   `json:"usd"`
}

type dollarResponse struct {
	AsOf    time.Time `json:"as_of"`
	Horizon int       `json:"horizon"`
	// Available is false when there is nothing to model -- no dollar history
	// or no M2 -- and Unavailable then says what is missing. The response is
	// still a 200: "not ingested yet" is a state of this deployment, not an
	// error in the request.
	Available       bool                  `json:"available"`
	Unavailable     []string              `json:"unavailable"`
	Anchor          *anchorBlock          `json:"anchor"`
	MoneyAsOf       *moneyAsOfBlock       `json:"money_as_of"`
	History         []historyPoint        `json:"history"`
	IndexBase       *string               `json:"index_base"`
	Scenarios       []scenarioBlock       `json:"scenarios"`
	Trend           *trendBlock           `json:"trend"`
	Band            *bandBlock            `json:"band"`
	Backtest        *backtestBlock        `json:"backtest"`
	LiquidityDollar *liquidityDollarBlock `json:"liquidity_dollar"`
	Notes           []string              `json:"notes"`
	DataAge         dollarAge             `json:"data_age"`
}

// usdStaleAfterDays: USD_IRT is collected continuously; a newest close more
// than four days old is a stopped collector, the same bound /prices/current
// applies to a TGJU daily close.
const usdStaleAfterDays = 4

const usdSourceNote = "USD_IRT is the free-market rate: TGJU's cash-dollar daily close until the " +
	"2022-04 join, then the 24/7 USDT/toman market (a stablecoin with its own small premium). " +
	"It is not CBI's official or NIMA rate."

const scenarioNote = "Scenario paths, not forecasts: each line is where the dollar would be if, " +
	"from the latest close, it grew as fast as M2 did over the stated span. In the backtest no " +
	"money-based model beat the dollar's own trend, so none of these lines is a prediction, a fair " +
	"value or a target."

const ldNote = "Not a fair value. The liquidity dollar is a base month's dollar scaled by M2 growth " +
	"since then, and the base month decides the answer: every month is a candidate and they " +
	"disagree by a factor of several. In the research this level model also lost to the dollar's " +
	"own trend as a forecast."

var dollarNotes = []string{
	scenarioNote,
	"Correlation is not causation: M2 and the dollar both rose with Iran's inflation, and the " +
		"growth of one did not lead the other at any stable lag.",
	"What the model cannot see: sanctions and oil revenue, war and politics, expectations, the " +
		"multiple exchange rates, and the central bank's foreign-exchange interventions.",
	usdSourceNote,
	"M2 is CBI's end-of-month stock in thousand billion rial (÷10 = همت). The backtest treats each " +
		"month as usable three months after it ended -- CBI states no release date -- and uses " +
		"today's figure for it; M2 has not been revised beyond rounding across CBI's editions.",
	"Base money is not used here: its 1399-12 redefinition breaks its growth, and it forecast no " +
		"better than M2 in the research.",
}

// buildDollarResponse is the whole response. Pure function (unit tested).
func buildDollarResponse(in dollarInput, horizon int) dollarResponse {
	today := tehranToday(in.Now)
	out := dollarResponse{
		AsOf:        in.Now.UTC(),
		Horizon:     horizon,
		Unavailable: []string{},
		History:     []historyPoint{},
		Scenarios:   []scenarioBlock{},
		Notes:       append([]string{}, dollarNotes...),
	}
	months, dropped := bucketUSD(in.USD, today)
	if dropped > 0 {
		out.Notes = append(out.Notes, fmt.Sprintf("%d stored daily close(s) were zero or negative and "+
			"were left out as stored faults.", dropped))
	}
	out.DataAge = dollarAge{Money: buildMoneyAge(in.M2, today), USD: buildUSDAge(in.USD, today)}

	newestM2, haveM2 := in.M2.newest()
	if haveM2 {
		out.MoneyAsOf = &moneyAsOfBlock{
			Period: newestM2.Month.String(), GregorianEnd: newestM2.Month.End().Format(dateLayout),
			PublishedAt: utcPtr(newestM2.PublishedAt), AvailableAt: newestM2.AvailableAt.UTC(),
			Vintage: newestM2.Vintage,
		}
	} else {
		out.Unavailable = append(out.Unavailable, unavailableSeries(in.M2))
	}

	p := newPanel(months, in.M2.Points)
	out.History, out.IndexBase = buildHistory(months, in.M2)

	last, haveUSD := lastClose(in.USD)
	if !haveUSD {
		out.Unavailable = append(out.Unavailable, "USD_IRT has no stored daily close on this "+
			"deployment, so there is no dollar to draw or model.")
		return out
	}
	jy, jm, jd, _ := GregorianToJalali(last.Day)
	anchor := jalaliDate{jy, jm, jd}
	out.Anchor = &anchorBlock{
		Date: last.Day.Format(dateLayout), Jalali: fmt.Sprintf("%04d/%02d/%02d", jy, jm, jd),
		CloseToman: last.Close, Source: "USD_IRT (prices): the last observation of the day",
		Note: "free-market rate (USDT/toman since 2022-04)",
	}
	if p.iLast < 0 {
		out.Unavailable = append(out.Unavailable, "No complete Jalali month of USD_IRT is stored yet.")
		return out
	}

	// The trend needs only the dollar.
	if slope := p.trendSlope(p.iLast); !math.IsNaN(slope) {
		out.Trend = &trendBlock{
			AnnualGrowthPct: roundTo(100*math.Expm1(12*slope), 4),
			MonthlyLogRate:  roundTo(slope, 8),
			Since:           p.month(p.iFirst).String(),
			Through:         p.month(p.iLast).String(),
			Path:            buildPath(anchor, last.Close, slope, horizon, nil),
			Note: "The dollar's own trend: the mean monthly change of its log monthly average since " +
				p.month(p.iFirst).String() + ", carried forward from the latest close. It is the " +
				"benchmark the M2 paths are measured against, and it is not a forecast either.",
		}
	}
	if !haveM2 {
		return out
	}
	out.Available = true

	band := p.band(targetClose, maxHorizon)
	cov := p.coverage(targetClose, bandLowerQ, bandUpperQ, true)
	out.Band = buildBandBlock(band, cov, horizon)

	rates, notes := scenarioRates(in.M2)
	out.Notes = append(out.Notes, notes...)
	for _, r := range rates {
		out.Scenarios = append(out.Scenarios, scenarioBlock{
			Key: r.Key, LabelEN: r.LabelEN, LabelFA: r.LabelFA,
			AnnualGrowthPct: roundTo(100*r.Growth, 4), Basis: r.Basis,
			From: r.From.String(), To: r.To.String(),
			Path: buildPath(anchor, last.Close, monthlyRate(r.Growth), horizon, band),
		})
	}

	if bt, ok := p.runBacktest(targetClose); ok {
		out.Backtest = buildBacktestBlock(bt)
	} else {
		out.Notes = append(out.Notes, "Too few months are stored to score the backtest, so no "+
			"comparison with the random walk or the trend is published.")
	}
	out.LiquidityDollar = buildLiquidityDollar(months, in.M2, p)
	return out
}

func unavailableSeries(s moneySeries) string {
	switch {
	case !s.Registered:
		return s.Code + " is not registered on this deployment: the CBI monetary ingest has not run yet."
	case s.Refused != "":
		return s.Refused
	}
	return s.Code + " is registered but has no observation available yet."
}

func lastClose(days []dayClose) (dayClose, bool) {
	var best dayClose
	found := false
	for _, d := range days {
		if d.Close <= 0 {
			continue
		}
		if !found || d.Day.After(best.Day) {
			best, found = d, true
		}
	}
	return best, found
}

// buildHistory lays the dollar months beside M2 and the dollars-per-unit-of-
// liquidity index, 100 at the first complete month with both.
func buildHistory(months []usdMonth, m2 moneySeries) ([]historyPoint, *string) {
	byM := m2.byMonth()
	out := make([]historyPoint, 0, len(months))
	var baseRatio float64
	var indexBase *string
	for _, u := range months {
		hp := historyPoint{
			Period: u.Month.String(), GregorianEnd: u.Month.End().Format(dateLayout),
			UsdAvg: roundTo(u.Avg, 4), UsdClose: u.Close, UsdCloseDate: u.CloseDay.Format(dateLayout),
			Days: u.Days, Partial: !u.Complete,
		}
		if !u.Complete {
			r := u.Incomplete
			hp.PartialReason = &r
		}
		if q, ok := byM[u.Month]; ok && q.Value > 0 {
			v := q.Value
			hp.M2 = &v
			if u.Complete {
				ratio := u.Avg / q.Value
				if indexBase == nil {
					baseRatio = ratio
					b := u.Month.String()
					indexBase = &b
				}
				hp.UsdPerM2Index = fptr(100*ratio/baseRatio, 4)
			}
		}
		out = append(out, hp)
	}
	return out, indexBase
}

func buildBandBlock(band []bandStep, cov map[int]coverageCell, horizon int) *bandBlock {
	b := &bandBlock{
		LowerQ: bandLowerQ, UpperQ: bandUpperQ, ByH: []bandStepWire{}, AppliedTo: "each",
		Target: targetClose.label(), OriginsFrom: evalStart.String(), LagMonths: publicationLagMonths,
		NByH: map[string]int{}, Coverage: map[string]*float64{}, CoverageN: map[string]int{},
	}
	for _, s := range band {
		if s.H > horizon {
			break
		}
		b.ByH = append(b.ByH, bandStepWire{H: s.H, Lo: roundTo(s.Lo, 4), Hi: roundTo(s.Hi, 4),
			RawLo: roundTo(s.RawLo, 4), RawHi: roundTo(s.RawHi, 4), N: s.N})
		b.NByH[fmt.Sprint(s.H)] = s.N
	}
	for key, r := range map[string][2]int{"1-12": {1, 12}, "13-24": {13, 24}, "24": {24, 24}} {
		c := pooledCoverage(cov, r[0], r[1])
		b.Coverage[key] = fptr(c.rate(), 3)
		b.CoverageN[key] = c.N
	}
	short, long := b.Coverage["1-12"], b.Coverage["24"]
	switch {
	case len(band) == 0:
		b.Note = "Fewer than 12 backtest errors are known at every horizon, so no band is drawn."
	case short == nil:
		b.Note = "The band is the 5th–95th percentile of the M2-paced path's past errors; its " +
			"coverage could not be measured yet."
	default:
		b.Note = fmt.Sprintf("The band is the 5th–95th percentile of the M2-paced path's past errors "+
			"(month-end close, origins from %s, at least %d per horizon, widened so it never "+
			"narrows). In the backtest, a band built this way from the errors known at each date "+
			"held the actual price %s of the time at 1–12 months", evalStart, minErrors, pct(*short))
		if long != nil {
			b.Note += fmt.Sprintf(" and %s at 24 months", pct(*long))
		}
		b.Note += "."
		nominal := bandUpperQ - bandLowerQ
		if *short < nominal-0.02 || (long != nil && *long < nominal-0.02) {
			b.Note += fmt.Sprintf(" That is less often than the %s its quantiles span: later "+
				"errors fell outside the range of the errors before them more often than that "+
				"implies, and the future can surprise by more than the past did.", pct(nominal))
		}
		if s, ok := bandAt(band, 12); ok && math.Abs(s.Median) >= 0.01 {
			dir := "above"
			if s.Median < 0 {
				dir = "below"
			}
			b.Note += fmt.Sprintf(" The band is not centred on the path: the median 12-month error "+
				"was %+.3f, so the dollar ended %s the path more often than not.", s.Median, dir)
		}
	}
	return b
}

func bandAt(band []bandStep, h int) (bandStep, bool) {
	if h < 1 || h > len(band) {
		return bandStep{}, false
	}
	return band[h-1], true
}

func pct(v float64) string { return fmt.Sprintf("%.0f%%", 100*v) }

func buildBacktestBlock(bt backtest) *backtestBlock {
	b := &backtestBlock{
		Target: bt.Target.label(), From: bt.From.String(), To: bt.To.String(),
		LagMonths: publicationLagMonths, NOrigins: map[string]int{},
		RMSE:                 map[string]map[string]*float64{},
		DMVsRandomWalkP:      map[string]*float64{},
		DMVsTrendP:           map[string]*float64{},
		DMTrendVsRandomWalkP: map[string]*float64{},
		Significance:         significance,
		Note: "Root mean square of log errors (0.10 is roughly a 10% miss). One-sided " +
			"Diebold–Mariano tests with the Harvey–Leybourne–Newbold small-sample correction; " +
			"forecasts from consecutive months overlap, so at 24 months the origins hold only " +
			"about five independent outcomes.",
	}
	for _, m := range []model{modelRandomWalk, modelTrend, modelM2Paced} {
		b.RMSE[m.key()] = map[string]*float64{}
	}
	for _, h := range scoredHorizons {
		k := fmt.Sprint(h)
		b.NOrigins[k] = bt.NOrigins[h]
		for _, m := range []model{modelRandomWalk, modelTrend, modelM2Paced} {
			if row, ok := bt.Scores[m][h]; ok {
				b.RMSE[m.key()][k] = fptr(row.RMSE, 4)
			} else {
				b.RMSE[m.key()][k] = nil
			}
		}
		b.DMVsRandomWalkP[k] = dmP(bt.Scores[modelM2Paced][h].VsRandomWalk)
		b.DMVsTrendP[k] = dmP(bt.Scores[modelM2Paced][h].VsTrend)
		b.DMTrendVsRandomWalkP[k] = dmP(bt.Scores[modelTrend][h].VsRandomWalk)
	}
	b.Verdict = verdict(b)
	return b
}

// dmP is a p-value for the wire, to four significant digits: a fixed number
// of decimals would print 0.00002 as 0, and "p = 0" is a claim of certainty no
// test makes.
func dmP(r dmResult) *float64 {
	if !r.OK || math.IsNaN(r.P) {
		return nil
	}
	v := roundSig(r.P, 4)
	return &v
}

func roundSig(v float64, digits int) float64 {
	if v == 0 || math.IsNaN(v) || math.IsInf(v, 0) {
		return v
	}
	e := math.Floor(math.Log10(math.Abs(v)))
	p := math.Pow(10, float64(digits-1)-e)
	return math.Round(v*p) / p
}

// verdict states what the backtest measured, from the numbers in it. Nothing
// in it is fixed text about the outcome: if the M2-paced path started beating
// the trend, this sentence would say so.
func verdict(b *backtestBlock) string {
	m2RW := sigSplit(b.DMVsRandomWalkP)
	m2Trend := sigSplit(b.DMVsTrendP)
	trendRW := sigSplit(b.DMTrendVsRandomWalkP)
	if len(m2RW.tested) == 0 {
		return "Too few forecasts could be scored to compare the models."
	}
	var s strings.Builder
	fmt.Fprintf(&s, "In the backtest (origins %s to %s, %s), ", b.From, b.To, b.Target)
	s.WriteString("the dollar growing at M2's pace " + m2RW.phrase("beat “no change”") + "; ")
	switch {
	case len(m2Trend.won) == 0:
		s.WriteString("it did not beat the dollar's own trend at any horizon tested " +
			m2Trend.pRange() + ", so M2 growth added no measurable skill over the trend.")
	default:
		s.WriteString("against the dollar's own trend it " + m2Trend.phrase("was better") + ".")
	}
	if len(trendRW.tested) > 0 {
		s.WriteString(" The trend alone " + trendRW.phrase("beat “no change”") + ".")
	}
	return s.String()
}

// sigResult splits a horizon -> p map at the significance level.
type sigResult struct {
	tested, won, lost []int
	ps                map[int]float64
}

func sigSplit(ps map[string]*float64) sigResult {
	r := sigResult{ps: map[int]float64{}}
	for _, h := range scoredHorizons {
		p := ps[fmt.Sprint(h)]
		if p == nil {
			continue
		}
		r.tested = append(r.tested, h)
		r.ps[h] = *p
		if *p < significance {
			r.won = append(r.won, h)
		} else {
			r.lost = append(r.lost, h)
		}
	}
	return r
}

func (r sigResult) pRange() string {
	return pBounds(r.tested, r.ps)
}

func pBounds(hs []int, ps map[int]float64) string {
	if len(hs) == 0 {
		return ""
	}
	lo, hi := math.Inf(1), math.Inf(-1)
	for _, h := range hs {
		lo, hi = math.Min(lo, ps[h]), math.Max(hi, ps[h])
	}
	if lo == hi {
		return fmt.Sprintf("(p = %.3f)", lo)
	}
	return fmt.Sprintf("(p = %.3f to %.3f)", lo, hi)
}

// phrase renders "<verb> at every horizon tested (3, 6, 12 and 24 months, ...)"
// and its partial and empty variants.
func (r sigResult) phrase(verb string) string {
	switch {
	case len(r.won) == len(r.tested):
		return fmt.Sprintf("%s at every horizon tested (%s months, one-sided p ≤ %.3f)",
			verb, listInts(r.tested), maxP(r.won, r.ps))
	case len(r.won) == 0:
		return fmt.Sprintf("did not %s at any horizon tested %s", infinitive(verb), pBounds(r.tested, r.ps))
	}
	return fmt.Sprintf("%s at %s months (p ≤ %.3f) but not at %s months %s", verb, listInts(r.won),
		maxP(r.won, r.ps), listInts(r.lost), pBounds(r.lost, r.ps))
}

// infinitive turns the past-tense verbs used above into their bare form.
func infinitive(verb string) string {
	switch {
	case strings.HasPrefix(verb, "beat"):
		return verb
	case strings.HasPrefix(verb, "was "):
		return "do " + strings.TrimPrefix(verb, "was ")
	}
	return verb
}

func maxP(hs []int, ps map[int]float64) float64 {
	m := 0.0
	for _, h := range hs {
		m = math.Max(m, ps[h])
	}
	return m
}

func listInts(hs []int) string {
	parts := make([]string, len(hs))
	for i, h := range hs {
		parts[i] = fmt.Sprint(h)
	}
	if len(parts) <= 1 {
		return strings.Join(parts, "")
	}
	return strings.Join(parts[:len(parts)-1], ", ") + " and " + parts[len(parts)-1]
}

// buildLiquidityDollar is the "liquidity dollar" at the newest M2 month: for
// every candidate base month b from the first complete dollar month, the
// dollar's average in b scaled by M2's growth since b. Published as the range
// over all bases, because which base is "right" is exactly the question the
// method cannot answer.
func buildLiquidityDollar(months []usdMonth, m2 moneySeries, p *panel) *liquidityDollarBlock {
	k, ok := m2.newest()
	if !ok || p.iFirst < 0 {
		return nil
	}
	usdBy := map[Month]usdMonth{}
	for _, u := range months {
		if u.Complete {
			usdBy[u.Month] = u
		}
	}
	actual, ok := usdBy[k.Month]
	if !ok {
		return nil
	}
	m2By := m2.byMonth()
	type cand struct {
		base  Month
		value float64
	}
	cands := []cand{}
	for b := p.month(p.iFirst); b < k.Month; b++ {
		u, okU := usdBy[b]
		q, okQ := m2By[b]
		if !okU || !okQ || q.Value <= 0 {
			continue
		}
		cands = append(cands, cand{b, u.Avg * k.Value / q.Value})
	}
	if len(cands) == 0 {
		return nil
	}
	sort.SliceStable(cands, func(i, j int) bool { return cands[i].value < cands[j].value })
	below := 0
	vals := make([]float64, len(cands))
	for i, c := range cands {
		vals[i] = c.value
		if actual.Avg > c.value {
			below++
		}
	}
	label := func(m Month) *string { s := m.String(); return &s }
	median := ldEntry{Value: roundTo(sortedQuantile(vals, 0.5), 2)}
	n := len(cands)
	if n%2 == 1 {
		median.Base = label(cands[n/2].base)
	} else {
		median.Bases = []string{cands[n/2-1].base.String(), cands[n/2].base.String()}
	}
	firstBase, lastBase := cands[0].base, cands[0].base
	for _, c := range cands {
		if c.base < firstBase {
			firstBase = c.base
		}
		if c.base > lastBase {
			lastBase = c.base
		}
	}
	return &liquidityDollarBlock{
		Period: k.Month.String(), M2: k.Value, ActualAvg: roundTo(actual.Avg, 2),
		ByBase: map[string]ldEntry{
			"min":    {Base: label(cands[0].base), Value: roundTo(cands[0].value, 2)},
			"median": median,
			"max":    {Base: label(cands[n-1].base), Value: roundTo(cands[n-1].value, 2)},
		},
		BasesConsidered: n, BasesFrom: firstBase.String(), BasesTo: lastBase.String(),
		BasesBelowActual: below, Note: ldNote,
	}
}

func buildUSDAge(days []dayClose, today time.Time) usdAge {
	out := usdAge{StaleAfterDays: usdStaleAfterDays}
	last, ok := lastClose(days)
	if !ok {
		out.Stale = true
		out.Warning = "No USD_IRT close is stored."
		return out
	}
	d := last.Day.Format(dateLayout)
	age := daysBetween(last.Day, today)
	out.NewestDay, out.AgeDays = &d, &age
	if age > usdStaleAfterDays {
		out.Stale = true
		out.Warning = fmt.Sprintf("The newest USD_IRT close is %s, %d days ago: the scenarios start "+
			"from a stale price.", d, age)
	}
	return out
}
