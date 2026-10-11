package liquidity

// GET /api/v1/liquidity/aggregates, built. A pure function of the four series
// as read and the clock.

import (
	"fmt"
	"time"
)

// CBI's measured publication lag: the last-saved times of the monthly
// workbooks CBI has not visibly re-saved put each one 21 to 164 days after the
// month it describes ended (docs/data-sources.md, "Publication"; the catalog
// note on every CBI series says the same).
const (
	cbiLagMinDays = 21
	cbiLagMaxDays = 164
)

// moneyStaleAfterDays is when the newest CBI month is called old. Its age is
// counted from the end of the newest STORED month, and that month is about a
// month older than the next one CBI has yet to publish: when 1403-12 arrived,
// 164 days after it ended, the newest stored month (1403-11) was 194 days old
// and nothing was late. So the bound is the longest lag plus one month (31
// days, the longest Jalali month); beyond it CBI is later than it has ever
// been, or a release has not been read.
const moneyStaleAfterDays = cbiLagMaxDays + 31

// moneyAge states how old the newest stored month is.
type moneyAge struct {
	NewestPeriod    *string    `json:"newest_period"`
	NewestPeriodEnd *string    `json:"newest_period_end"`
	AgeDays         *int       `json:"age_days"`
	AvailableAt     *time.Time `json:"available_at"`
	// AvailableAtApprox: available_at was reconstructed by the backfill, not
	// seen (null when the row does not say).
	AvailableAtApprox *bool      `json:"available_at_approx"`
	PublishedAt       *time.Time `json:"published_at"`
	Stale             bool       `json:"stale"`
	StaleAfterDays    int        `json:"stale_after_days"`
	// PublicationLagDays is the measured range behind StaleAfterDays, so a
	// reader states the same figures the threshold is built from.
	PublicationLagDays lagRange           `json:"publication_lag_days"`
	Warning            string             `json:"warning,omitempty"`
	Note               string             `json:"note"`
	ByCode             map[string]*string `json:"by_code,omitempty"`
}

// lagRange is CBI's measured publication lag, in days after the month ends.
type lagRange struct {
	Min int `json:"min"`
	Max int `json:"max"`
}

var moneyAgeNote = fmt.Sprintf("CBI states no release date. Its monthly workbooks have arrived "+
	"%d to %d days after the month they describe (three weeks to five and a half months, measured "+
	"from the workbooks' own last-saved times), often several at once. age_days is measured from "+
	"the end of the newest stored month to today (Tehran); that month is called stale after %d "+
	"days, the longest lag plus one month, because until the next month is published the newest "+
	"stored one is a month older than it.", cbiLagMinDays, cbiLagMaxDays, moneyStaleAfterDays)

func buildMoneyAge(s moneySeries, today time.Time) moneyAge {
	out := moneyAge{StaleAfterDays: moneyStaleAfterDays, Note: moneyAgeNote,
		PublicationLagDays: lagRange{Min: cbiLagMinDays, Max: cbiLagMaxDays}}
	p, ok := s.newest()
	if !ok {
		out.Stale = true
		out.Warning = fmt.Sprintf("No %s month is stored, so every figure here is empty rather than old.", s.Code)
		return out
	}
	period, end := p.Month.String(), p.Month.End().Format(dateLayout)
	age := daysBetween(p.Month.End(), today)
	av := p.AvailableAt.UTC()
	out.NewestPeriod, out.NewestPeriodEnd, out.AgeDays = &period, &end, &age
	out.AvailableAt, out.AvailableAtApprox, out.PublishedAt = &av, p.AvailableAtApprox, utcPtr(p.PublishedAt)
	if age > moneyStaleAfterDays {
		out.Stale = true
		out.Warning = fmt.Sprintf("The newest CBI month stored is %s, which ended %d days ago: "+
			"longer than CBI has taken to publish the next month (at most %d days after it ended). "+
			"CBI has not published a later month, or it has not been ingested yet.", period, age, cbiLagMaxDays)
	}
	return out
}

type latestBlock struct {
	Code         string     `json:"code"`
	Period       string     `json:"period"`
	GregorianEnd string     `json:"gregorian_end"`
	PublishedAt  *time.Time `json:"published_at"`
	AvailableAt  time.Time  `json:"available_at"`
	// AvailableAtApprox: see moneyAge.
	AvailableAtApprox *bool   `json:"available_at_approx"`
	AgeDays           int     `json:"age_days"`
	SourceURL         *string `json:"source_url"`
	Vintage           int     `json:"vintage"`
}

type seriesBlock struct {
	Code         string `json:"code"`
	NameEN       string `json:"name_en"`
	NameFA       string `json:"name_fa"`
	Unit         string `json:"unit"`
	ProviderCode string `json:"provider_code"`
	QualityTier  string `json:"quality_tier"`
	// Available is false when nothing is stored for the series yet;
	// UnavailableReason then says why.
	Available         bool    `json:"available"`
	UnavailableReason *string `json:"unavailable_reason"`
	// MonthlyFrom is where the unbroken run of monthly figures that ends at
	// the newest month begins (monthlyRun); before it only some months are
	// stored. Null for an empty series.
	MonthlyFrom *string          `json:"monthly_from"`
	Points      []aggregatePoint `json:"points"`
}

type aggregatesResponse struct {
	AsOf            time.Time         `json:"as_of"`
	Freq            string            `json:"freq"`
	Unit            string            `json:"unit"`
	DisplayUnitNote string            `json:"display_unit_note"`
	Available       bool              `json:"available"`
	Latest          *latestBlock      `json:"latest"`
	Series          []seriesBlock     `json:"series"`
	Multiplier      []multiplierPoint `json:"multiplier"`
	// Annual is present only for freq=annual.
	Annual  *[]annualRow `json:"annual,omitempty"`
	Breaks  []breakEntry `json:"breaks"`
	Notes   []string     `json:"notes"`
	DataAge moneyAge     `json:"data_age"`
}

var aggregateNotes = []string{
	"Source: Central Bank of Iran, Selected Economic Indicators (monthly Excel editions from Tir " +
		"1399, three months per edition, so monthly levels from Tir 1398), and CBI's tsdview " +
		"time-series history for quarter-end months before that.",
	"Every value is an END-OF-MONTH STOCK in thousand billion rial (هزار میلیارد ریال), exactly " +
		"as CBI publishes it; ÷10 gives همت (trillion toman). It is not a monthly average.",
	"Growth: m/m against the previous month, y/y against the same month a year earlier, and " +
		"since-Esfand against the previous Esfand, all by Jalali month. The annual headline is " +
		"Esfand over Esfand, as CBI reports it; the annual average needs all twelve months.",
	"About 2.7 points of 1400's liquidity growth come only from Mehr Eqtesad bank's figures being " +
		"added to Sepah's; CBI states this has no monetary effect, and growth is still computed " +
		"across it.",
	"CBI marks its newest base-money figure as fully preliminary (کاملا مقدماتی); it is revised " +
		"in later editions more often than liquidity is.",
}

// buildAggregatesResponse is the whole response. series must hold every code
// in seriesOrder (an unregistered one with Registered false).
func buildAggregatesResponse(series map[string]moneySeries, freq string, now time.Time) aggregatesResponse {
	today := tehranToday(now)
	out := aggregatesResponse{
		AsOf: now.UTC(), Freq: freq, Unit: Unit, DisplayUnitNote: displayUnitNote,
		Series: []seriesBlock{}, Multiplier: []multiplierPoint{},
		Notes: append([]string{}, aggregateNotes...),
	}
	ordered := make([]moneySeries, 0, len(seriesOrder))
	for _, code := range seriesOrder {
		s := series[code]
		s.Code = code
		ordered = append(ordered, s)
		blk := seriesBlock{
			Code: code, NameEN: s.NameEN, NameFA: s.NameFA, Unit: Unit,
			ProviderCode: s.ProviderCode, QualityTier: s.QualityTier,
			Available: len(s.Points) > 0, Points: buildPoints(s),
		}
		if run, ok := monthlyRun(s); ok {
			m := run.String()
			blk.MonthlyFrom = &m
		}
		if !blk.Available {
			r := unavailableSeries(s)
			blk.UnavailableReason = &r
		} else {
			out.Available = true
		}
		if s.Unreadable > 0 {
			out.Notes = append(out.Notes, fmt.Sprintf("%s: %d stored observation(s) carry a period "+
				"label that is not a Jalali month (YYYY-MM) and are left out.", code, s.Unreadable))
		}
		if n := inferredMethods(s); n > 0 {
			out.Notes = append(out.Notes, fmt.Sprintf("%s: %d month(s) carry no recorded method; "+
				"those before 1399-12 are read as the old definition and those from 1402-01 as the "+
				"revised one, the only definition each was ever printed on.", code, n))
		}
		out.Series = append(out.Series, blk)
	}
	m2, base := series[CodeLiquidity], series[CodeBaseMoney]
	out.Multiplier = buildMultiplier(m2, base)
	out.Breaks = buildBreaks(base)
	if freq == "annual" {
		a := buildAnnual(ordered)
		out.Annual = &a
	}

	headline := m2
	if len(headline.Points) == 0 {
		for _, s := range ordered {
			if len(s.Points) > 0 {
				headline = s
				break
			}
		}
	}
	out.DataAge = buildMoneyAge(headline, today)
	out.DataAge.ByCode = map[string]*string{}
	for _, s := range ordered {
		if p, ok := s.newest(); ok {
			l := p.Month.String()
			out.DataAge.ByCode[s.Code] = &l
		} else {
			out.DataAge.ByCode[s.Code] = nil
		}
	}
	if p, ok := headline.newest(); ok {
		lb := &latestBlock{
			Code: headline.Code, Period: p.Month.String(), GregorianEnd: p.Month.End().Format(dateLayout),
			PublishedAt: utcPtr(p.PublishedAt), AvailableAt: p.AvailableAt.UTC(),
			AvailableAtApprox: p.AvailableAtApprox,
			AgeDays:           daysBetween(p.Month.End(), today), Vintage: p.Vintage,
		}
		if p.SourceURL != "" {
			u := p.SourceURL
			lb.SourceURL = &u
		}
		out.Latest = lb
	}
	return out
}

func inferredMethods(s moneySeries) int {
	n := 0
	for _, p := range s.Points {
		if p.MethodInferred {
			n++
		}
	}
	return n
}
