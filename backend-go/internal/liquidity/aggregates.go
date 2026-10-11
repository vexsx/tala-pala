package liquidity

// Money aggregates: CBI's monthly stocks of liquidity (M2), base money, money
// (M1) and quasi-money, with the growth arithmetic CBI's own tables carry and
// nothing it does not.
//
// THE RULES THIS FILE ENFORCES
//
//   - A value is an END-OF-MONTH STOCK in the unit CBI published (thousand
//     billion rial). It is never averaged into a "monthly" figure and never
//     converted here; ÷10 to همت is the page's display step and is stated.
//   - Growth steps Jalali LABELS: month-on-month compares 1404-01 with
//     1403-12, year-on-year with 1403-01, year-to-date with the previous
//     Esfand (CBI's second growth column). A comparison month that is not
//     stored makes the growth null with a reason -- before the unbroken run
//     of monthly figures (monthlyRun) only quarter-end months exist, so a
//     quarterly-era month has a year-on-year figure and no month-on-month one.
//   - Base money changed DEFINITION: CBI revalued its foreign assets and
//     adopted the IMF's 2016 manual for months from 1399-12 on, and some
//     stored months after that are still old-method prints. Growth is never
//     computed across the two methods; it is null with a reason. Each point
//     carries its method.
//   - The annual average exists only for a year with all twelve months (and,
//     for base money, all twelve on one recorded method); the headline
//     annual growth is Esfand over Esfand, which is what CBI publishes and
//     what an average of month-end stocks is not.

import (
	"fmt"
	"math"
	"sort"
	"strings"
	"time"
)

// The four series (design §2), in the order the page lists them.
const (
	CodeLiquidity  = "CBI_LIQUIDITY"
	CodeBaseMoney  = "CBI_BASE_MONEY"
	CodeM1         = "CBI_MONEY_M1"
	CodeQuasiMoney = "CBI_QUASI_MONEY"
)

var seriesOrder = []string{CodeLiquidity, CodeBaseMoney, CodeM1, CodeQuasiMoney}

// seriesNames is what a series is called when the registry has no row for it
// yet: the catalog's names, so a degraded response still says what is missing.
var seriesNames = map[string][2]string{
	CodeLiquidity:  {"Liquidity (M2)", "نقدینگی"},
	CodeBaseMoney:  {"Base money", "پایه پولی"},
	CodeM1:         {"Money (M1)", "پول"},
	CodeQuasiMoney: {"Quasi-money", "شبه پول"},
}

// Unit is the unit every value on this contract is in: CBI's own.
const Unit = "thousand_billion_rial"

// displayUnitNote is the one conversion the page applies.
const displayUnitNote = "÷10 = همت (trillion toman)"

// unitFactors are the stored units this package can express in Unit, and the
// factor that does it. A series stored in any other unit is refused rather
// than guessed at -- the rial/toman factor of ten is exactly the error this
// rule exists to stop.
var unitFactors = map[string]float64{
	Unit:             1,
	"trillion_toman": 10, // همت: 1e12 toman = 1e13 rial = 10 thousand billion rial
}

// Base-money methods.
const (
	methodOld     = "old"
	methodRevised = "revised"
)

var (
	// baseMoneyBreak is the first month CBI publishes on the revised
	// definition (its footnote: "from Esfand 1399" / "from 1400").
	baseMoneyBreak = NewMonth(1399, 12)
	// allRevisedFrom is the first month that has only ever been printed on the
	// revised definition: every 1402 edition already carried it.
	allRevisedFrom = NewMonth(1402, 1)
)

// moneyPoint is one series' stored value for one Jalali month, as the
// point-in-time read selected it.
type moneyPoint struct {
	Month       Month
	Value       float64 // in Unit
	Vintage     int
	AvailableAt time.Time
	// AvailableAtApprox is the ingest's flag that AvailableAt was
	// RECONSTRUCTED, not observed: the CBI backfill sets it to the workbook's
	// last-saved time plus a day (design §2). nil when the row records no such
	// flag -- unknown, which is not the same as exact.
	AvailableAtApprox *bool
	PublishedAt       *time.Time
	// Method is the base-money definition ("old"/"revised"); "" for the other
	// series, and for a base-money month whose method is not known.
	Method string
	// MethodInferred is true when the ingest recorded no method and Method was
	// read from the period (before 1399-12 old, from 1402-01 revised).
	MethodInferred bool
	Source         string // "excel" | "tsdview" | ""
	SourceURL      string
}

// moneySeries is one series as read.
type moneySeries struct {
	Code         string
	NameEN       string
	NameFA       string
	ProviderCode string
	QualityTier  string
	// Registered is false when the registry has no such series yet: the
	// ingest has not run on this deployment.
	Registered bool
	// Refused is non-empty when the series exists but cannot be served (a
	// unit this package will not convert); Points is then empty.
	Refused string
	// Points ascend by month, one per month.
	Points []moneyPoint
	// Unreadable counts stored observations whose label is not a Jalali month.
	Unreadable int
}

func (s moneySeries) byMonth() map[Month]moneyPoint {
	out := make(map[Month]moneyPoint, len(s.Points))
	for _, p := range s.Points {
		out[p.Month] = p
	}
	return out
}

func (s moneySeries) newest() (moneyPoint, bool) {
	if len(s.Points) == 0 {
		return moneyPoint{}, false
	}
	return s.Points[len(s.Points)-1], true
}

// --- growth ---------------------------------------------------------------------

// monthlyRun is where the unbroken run of monthly figures that ends at the
// series' newest month begins: the first of the consecutive months, none of
// them tsdview's quarter-ends, counted back from the newest. Before it a
// series holds only some months -- tsdview's quarter-ends, and whichever
// single month the oldest workbook reaches back to (Esfand 1397) -- so this
// is the one month every "monthly from" statement names. ok is false for an
// empty series or one whose newest point is a quarter-end.
func monthlyRun(s moneySeries) (Month, bool) {
	start, found := Month(0), false
	for i := len(s.Points) - 1; i >= 0; i-- {
		p := s.Points[i]
		if p.Source == "tsdview" || (found && p.Month != start-1) {
			break
		}
		start, found = p.Month, true
	}
	return start, found
}

// growthRule is what a growth comparison needs to know about its series.
type growthRule struct {
	all map[Month]moneyPoint
	// methodSensitive applies the base-money rule.
	methodSensitive bool
	// monthlyFrom is the start of the monthly run (monthlyRun) when the series
	// also holds months before it; zero when it holds none. sparse names what
	// those earlier months are. Both only explain a missing month, they never
	// fill one.
	monthlyFrom Month
	sparse      string
}

func newGrowthRule(s moneySeries) growthRule {
	g := growthRule{all: s.byMonth(), methodSensitive: s.Code == CodeBaseMoney}
	run, ok := monthlyRun(s)
	if !ok || len(s.Points) == 0 || s.Points[0].Month >= run {
		return g
	}
	g.monthlyFrom, g.sparse = run, "quarter-end months"
	for _, p := range s.Points {
		if p.Month < run && p.Month.Of()%3 != 0 {
			g.sparse = "some months"
		}
	}
	return g
}

// pct is 100*(cur/base - 1) when it may be computed, else nil and why.
func (g growthRule) pct(cur moneyPoint, base Month, what string) (*float64, string) {
	b, ok := g.all[base]
	if !ok {
		reason := fmt.Sprintf("%s is not stored, so there is no %s comparison", base, what)
		if g.monthlyFrom != 0 && base < g.monthlyFrom {
			reason += fmt.Sprintf(" (before %s only %s are stored)", g.monthlyFrom, g.sparse)
		}
		return nil, reason
	}
	if b.Value <= 0 {
		return nil, fmt.Sprintf("the stored value for %s is not positive", base)
	}
	if g.methodSensitive {
		switch {
		case cur.Method == "" || b.Method == "":
			unknown := cur.Month
			if b.Method == "" {
				unknown = base
			}
			return nil, fmt.Sprintf("the base-money method of %s is not recorded, and %s..%s may "+
				"straddle CBI's 1399-12 redefinition; growth across methods is not computed",
				unknown, base, cur.Month)
		case cur.Method != b.Method:
			return nil, fmt.Sprintf("%s is on the %s base-money method and %s on the %s one "+
				"(CBI redefined base money from 1399-12); growth across the two methods is not computed",
				cur.Month, cur.Method, base, b.Method)
		}
	}
	v := 100 * (cur.Value/b.Value - 1)
	return &v, ""
}

// aggregatePoint is one month of one series on the wire.
type aggregatePoint struct {
	Period       string     `json:"period"`
	GregorianEnd string     `json:"gregorian_end"`
	Value        float64    `json:"value"`
	MomPct       *float64   `json:"mom_pct"`
	YoyPct       *float64   `json:"yoy_pct"`
	YtdPct       *float64   `json:"ytd_pct"`
	Method       *string    `json:"method"`
	Frequency    string     `json:"frequency"`
	Vintage      int        `json:"vintage"`
	Source       *string    `json:"source"`
	PublishedAt  *time.Time `json:"published_at"`
	AvailableAt  time.Time  `json:"available_at"`
	// AvailableAtApprox is true when available_at was reconstructed by the
	// backfill (last-saved + 1 day) rather than seen; null when not recorded.
	AvailableAtApprox *bool `json:"available_at_approx"`
	// NullReason names, for each growth field that is null, why.
	NullReason map[string]string `json:"null_reason,omitempty"`
}

// buildPoints derives the wire points of one series. Pure function.
func buildPoints(s moneySeries) []aggregatePoint {
	rule := newGrowthRule(s)
	out := make([]aggregatePoint, 0, len(s.Points))
	for _, p := range s.Points {
		ap := aggregatePoint{
			Period:            p.Month.String(),
			GregorianEnd:      p.Month.End().Format(dateLayout),
			Value:             p.Value,
			Frequency:         "M",
			Vintage:           p.Vintage,
			PublishedAt:       utcPtr(p.PublishedAt),
			AvailableAt:       p.AvailableAt.UTC(),
			AvailableAtApprox: p.AvailableAtApprox,
		}
		if p.Source != "" {
			src := p.Source
			ap.Source = &src
		}
		if p.Source == "tsdview" {
			ap.Frequency = "Q"
		}
		if rule.methodSensitive && p.Method != "" {
			m := p.Method
			ap.Method = &m
		}
		reasons := map[string]string{}
		var r string
		if ap.MomPct, r = rule.pct(p, p.Month.Add(-1), "month-on-month"); r != "" {
			reasons["mom_pct"] = r
		}
		if ap.YoyPct, r = rule.pct(p, p.Month.Add(-12), "year-on-year"); r != "" {
			reasons["yoy_pct"] = r
		}
		if ap.YtdPct, r = rule.pct(p, NewMonth(p.Month.Year()-1, 12), "since-Esfand"); r != "" {
			reasons["ytd_pct"] = r
		}
		ap.MomPct, ap.YoyPct, ap.YtdPct = round6(ap.MomPct), round6(ap.YoyPct), round6(ap.YtdPct)
		if len(reasons) > 0 {
			ap.NullReason = reasons
		}
		out = append(out, ap)
	}
	return out
}

// --- multiplier -----------------------------------------------------------------

type multiplierPoint struct {
	Period string  `json:"period"`
	Value  float64 `json:"value"`
	// BaseMethod is the definition of the base money it divides by, because
	// the redefinition moves the multiplier too.
	BaseMethod *string `json:"base_method"`
}

// buildMultiplier is M2 / base money for every month both are stored. Both
// are stocks at the same month end, so the ratio is the money multiplier CBI
// itself prints (7.095 at 1404-12).
func buildMultiplier(m2, base moneySeries) []multiplierPoint {
	bm := base.byMonth()
	out := []multiplierPoint{}
	for _, p := range m2.Points {
		b, ok := bm[p.Month]
		if !ok || b.Value <= 0 {
			continue
		}
		mp := multiplierPoint{Period: p.Month.String(), Value: roundTo(p.Value/b.Value, 6)}
		if b.Method != "" {
			m := b.Method
			mp.BaseMethod = &m
		}
		out = append(out, mp)
	}
	return out
}

// --- annual -----------------------------------------------------------------------

type annualRow struct {
	Year int `json:"year"`
	// EndValue is the Esfand stock; EndGrowthPct is Esfand over the previous
	// Esfand, CBI's headline annual growth.
	EndValue     map[string]*float64 `json:"end_value"`
	EndGrowthPct map[string]*float64 `json:"end_growth_pct"`
	// AvgValue is the mean of the twelve month-end stocks, only for a series
	// with all twelve (and, for base money, all on one recorded method);
	// AvgGrowthPct needs two such years on the same method. Null when no
	// series qualifies.
	AvgValue     map[string]*float64 `json:"avg_value"`
	AvgGrowthPct map[string]*float64 `json:"avg_growth_pct"`
	// MonthsPresent is the headline series' (liquidity) stored months in the
	// year; MonthsPresentByCode has every series'.
	MonthsPresent       int            `json:"months_present"`
	MonthsPresentByCode map[string]int `json:"months_present_by_code"`
	// Partial is true when the headline series has fewer than twelve months.
	Partial    bool                         `json:"partial"`
	NullReason map[string]map[string]string `json:"null_reason,omitempty"`
}

// yearStats is one series over one Jalali year.
type yearStats struct {
	months  int
	sum     float64
	methods map[string]bool
}

func (y yearStats) complete() bool { return y.months == 12 }

func (y yearStats) avg() float64 { return y.sum / 12 }

// buildAnnual derives the Jalali-year table. Pure function.
func buildAnnual(series []moneySeries) []annualRow {
	stats := map[string]map[int]yearStats{}
	rules := map[string]growthRule{}
	minY, maxY := math.MaxInt32, math.MinInt32
	for _, s := range series {
		stats[s.Code] = map[int]yearStats{}
		rules[s.Code] = newGrowthRule(s)
		for _, p := range s.Points {
			y := p.Month.Year()
			st := stats[s.Code][y]
			if st.methods == nil {
				st.methods = map[string]bool{}
			}
			st.months++
			st.sum += p.Value
			st.methods[p.Method] = true
			stats[s.Code][y] = st
			if y < minY {
				minY = y
			}
			if y > maxY {
				maxY = y
			}
		}
	}
	out := []annualRow{}
	for y := minY; y <= maxY; y++ {
		row := annualRow{
			Year:                y,
			EndValue:            map[string]*float64{},
			EndGrowthPct:        map[string]*float64{},
			AvgValue:            map[string]*float64{},
			AvgGrowthPct:        map[string]*float64{},
			MonthsPresentByCode: map[string]int{},
		}
		reasons := map[string]map[string]string{}
		note := func(code, field, why string) {
			if reasons[code] == nil {
				reasons[code] = map[string]string{}
			}
			reasons[code][field] = why
		}
		anyAvg, anyAvgGrowth := false, false
		for _, s := range series {
			code := s.Code
			rule := rules[code]
			row.EndValue[code], row.EndGrowthPct[code] = nil, nil
			row.AvgValue[code], row.AvgGrowthPct[code] = nil, nil
			row.MonthsPresentByCode[code] = stats[code][y].months
			if e, ok := rule.all[NewMonth(y, 12)]; ok {
				v := e.Value
				row.EndValue[code] = &v
				g, why := rule.pct(e, NewMonth(y-1, 12), "Esfand-over-Esfand")
				row.EndGrowthPct[code] = round6(g)
				if why != "" {
					note(code, "end_growth_pct", why)
				}
			} else if stats[code][y].months > 0 {
				note(code, "end_value", fmt.Sprintf("Esfand %d is not stored", y))
			}
			cur, prev := stats[code][y], stats[code][y-1]
			if !cur.complete() {
				if cur.months > 0 {
					note(code, "avg_value", fmt.Sprintf("%d of 12 months are stored; an average of "+
						"fewer is not the year's average", cur.months))
				}
				continue
			}
			// A mean of base-money stocks printed on two definitions is a
			// number of neither: 1399 and 1400 each hold eleven old-method
			// months and a revised Esfand.
			if rule.methodSensitive && !oneKnownMethod(cur.methods, nil) {
				note(code, "avg_value", fmt.Sprintf("the months of %d are not all on one recorded "+
					"base-money method; an average across the two definitions is not computed", y))
				continue
			}
			avg := roundTo(cur.avg(), 6)
			row.AvgValue[code] = &avg
			anyAvg = true
			switch {
			case !prev.complete():
				note(code, "avg_growth_pct", fmt.Sprintf("%d does not have all twelve months", y-1))
			case rule.methodSensitive && !oneKnownMethod(cur.methods, prev.methods):
				note(code, "avg_growth_pct", fmt.Sprintf("the months of %d and %d are not all on one "+
					"recorded base-money method; growth across methods is not computed", y-1, y))
			default:
				g := 100 * (cur.avg()/prev.avg() - 1)
				row.AvgGrowthPct[code] = round6(&g)
				anyAvgGrowth = true
			}
		}
		if !anyAvg {
			row.AvgValue = nil
		}
		if !anyAvgGrowth {
			row.AvgGrowthPct = nil
		}
		row.MonthsPresent = row.MonthsPresentByCode[CodeLiquidity]
		row.Partial = row.MonthsPresent < 12
		if len(reasons) > 0 {
			row.NullReason = reasons
		}
		out = append(out, row)
	}
	return out
}

// oneKnownMethod is true when every month of both years carries the same
// recorded method.
func oneKnownMethod(a, b map[string]bool) bool {
	seen := map[string]bool{}
	for m := range a {
		seen[m] = true
	}
	for m := range b {
		seen[m] = true
	}
	return len(seen) == 1 && !seen[""]
}

// --- breaks ---------------------------------------------------------------------

type breakEntry struct {
	Code string `json:"code"`
	// From is the first month of CBI's revised definition, as its footnote
	// states it -- not the first month stored on it (FirstRevised).
	From string `json:"from"`
	Note string `json:"note"`
	// FirstRevised is the first stored month from the break on that is on
	// the revised definition; null when none is yet.
	FirstRevised *string `json:"first_revised"`
	// OldMethodAfter lists stored months from the break on that are still
	// old-method prints -- months no edition stored here restates on the
	// revised definition -- so a chart can mark each crossing.
	OldMethodAfter []string `json:"old_method_after"`
}

const baseMoneyBreakNote = "CBI redefined base money from Esfand 1399: its foreign assets were " +
	"revalued and the IMF's 2016 Monetary and Financial Statistics Manual classification was " +
	"adopted (footnote 1 of the monthly tables). A month is on the revised definition only where " +
	"an edition printed it so, and growth is not computed across the two definitions; each month " +
	"carries its method."

// buildBreaks states the base-money break and where the stored months
// actually cross it -- read from each month's method, so the note says what
// this deployment holds rather than what some edition once showed.
func buildBreaks(base moneySeries) []breakEntry {
	b := breakEntry{Code: CodeBaseMoney, From: baseMoneyBreak.String(), Note: baseMoneyBreakNote,
		OldMethodAfter: []string{}}
	unknown := 0
	for _, p := range base.Points {
		if p.Month < baseMoneyBreak {
			continue
		}
		switch p.Method {
		case methodOld:
			b.OldMethodAfter = append(b.OldMethodAfter, p.Month.String())
		case methodRevised:
			if b.FirstRevised == nil {
				m := p.Month.String()
				b.FirstRevised = &m
			}
		default:
			unknown++
		}
	}
	switch {
	case len(base.Points) == 0:
	case b.FirstRevised == nil:
		b.Note += " No stored month is on the revised definition yet."
	case len(b.OldMethodAfter) == 0:
		b.Note += fmt.Sprintf(" In the stored data the first month on the revised definition is %s.",
			*b.FirstRevised)
	default:
		b.Note += fmt.Sprintf(" In the stored data the first month on the revised definition is %s; "+
			"%d stored month(s) from %s on are still old-method prints (old_method_after, %s to %s), "+
			"because no edition stored here restates them.", *b.FirstRevised, len(b.OldMethodAfter),
			baseMoneyBreak, b.OldMethodAfter[0], b.OldMethodAfter[len(b.OldMethodAfter)-1])
	}
	if unknown > 0 {
		b.Note += fmt.Sprintf(" %d stored month(s) from %s on carry no recorded method.", unknown,
			baseMoneyBreak)
	}
	return []breakEntry{b}
}

// --- method and source, from what the ingest stored -------------------------------

// methodColumns are the names a base-money method may be stored under, as a
// column of the observation row or a key inside a JSON column of it. The CBI
// ingest (prediction-python, provider `cbi`) owns that choice; this list is
// this package's reading of "the method is stored per observation, as old or
// revised", and the first match wins.
var methodColumns = []string{"base_money_method", "method", "definition", "methodology"}

// storedMethod reads the base-money method from the observation row's stored
// columns. "" when none is recorded.
func storedMethod(cols map[string]any) string {
	for _, k := range methodColumns {
		if m := normalizeMethod(cols[k]); m != "" {
			return m
		}
	}
	// One level down, for a JSON metadata column.
	keys := make([]string, 0, len(cols))
	for k := range cols {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		inner, ok := cols[k].(map[string]any)
		if !ok {
			continue
		}
		for _, mk := range methodColumns {
			if m := normalizeMethod(inner[mk]); m != "" {
				return m
			}
		}
	}
	return ""
}

// approxColumn is the observation column the CBI ingest flags a
// reconstructed availability with (migration 0034).
const approxColumn = "available_at_approx"

// storedApprox reads that flag; nil when the row has no such column or it is
// not a boolean.
func storedApprox(cols map[string]any) *bool {
	if b, ok := cols[approxColumn].(bool); ok {
		return &b
	}
	return nil
}

func normalizeMethod(v any) string {
	s, ok := v.(string)
	if !ok {
		return ""
	}
	s = strings.ToLower(strings.TrimSpace(s))
	// "pre-revision" contains "revis", so the old spellings are matched first.
	switch {
	case s == "":
		return ""
	case s == "old" || strings.Contains(s, "pre-revision") || strings.Contains(s, "pre_revision") ||
		strings.Contains(s, "old_method") || strings.Contains(s, "old method"):
		return methodOld
	case strings.Contains(s, "revis") || strings.Contains(s, "mfsmcg") || s == "new":
		return methodRevised
	}
	return ""
}

// effectiveMethod is a base-money month's method: the stored one, else the
// only one its period was ever printed on. 1399-12..1401-12 were printed on
// both, so with nothing stored it stays unknown -- and growth touching it is
// withheld rather than guessed.
func effectiveMethod(m Month, stored string) (method string, inferred bool) {
	if stored != "" {
		return stored, false
	}
	switch {
	case m < baseMoneyBreak:
		return methodOld, true
	case m >= allRevisedFrom:
		return methodRevised, true
	}
	return "", false
}

// sourceKind classifies the cited document: CBI's monthly Excel edition or
// the tsdview quarterly history, from what the archive row says it is. A
// document that says neither -- a PDF bulletin from cbi.ir, say, which the
// pre-1399 editions are -- is "" (null on the wire), not guessed to be a
// workbook because of the host it came from: the edition is downloaded from
// www.cbi.ir/page/{id}.aspx, a URL that says nothing about its format, and
// the ingest records the workbook's media type (application/vnd.ms-excel).
func sourceKind(url, title, mediaType string) string {
	u := strings.ToLower(url + " " + title)
	switch {
	case strings.Contains(u, "tsdview") || strings.Contains(u, "tsd.cbi.ir"):
		return "tsdview"
	case strings.Contains(strings.ToLower(mediaType), "excel") ||
		strings.Contains(strings.ToLower(mediaType), "spreadsheet") ||
		strings.HasSuffix(strings.TrimSpace(strings.ToLower(url)), ".xls") ||
		strings.HasSuffix(strings.TrimSpace(strings.ToLower(url)), ".xlsx") ||
		strings.Contains(u, ".xls"):
		return "excel"
	}
	return ""
}

// --- small helpers --------------------------------------------------------------

func utcPtr(t *time.Time) *time.Time {
	if t == nil {
		return nil
	}
	u := t.UTC()
	return &u
}

// roundTo rounds to n decimals.
func roundTo(v float64, n int) float64 {
	p := math.Pow(10, float64(n))
	return math.Round(v*p) / p
}

// round6 rounds a nullable figure to six decimals and turns a non-finite one
// into null: encoding/json refuses NaN, and httpserver.JSON drops the
// encoder's error, so one would truncate the body mid-object.
func round6(v *float64) *float64 {
	if v == nil || math.IsNaN(*v) || math.IsInf(*v, 0) {
		return nil
	}
	r := roundTo(*v, 6)
	return &r
}

func fptr(v float64, n int) *float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	r := roundTo(v, n)
	return &r
}
