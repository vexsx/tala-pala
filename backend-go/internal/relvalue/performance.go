package relvalue

// GET /api/v1/markets/performance -- every priceable instrument this
// deployment carries, over one window, in one unit of account.
//
// The table's whole value is that the rows are COMPARABLE, which is why three
// kinds of row are refused a place in it rather than given a caveat:
//
//   - A RATE. US10Y is a yield and IR_GOLD_FUND_FLOW is a flow ratio; both are
//     quote_currency='PCT' in the registry for this exact reason. 4.10 to 4.30
//     is a move of twenty basis points, and printing it in a return column as
//     "+4.9%" is not a rough number, it is a meaningless one.
//   - An INDEX LEVEL. DXY is not denominated in any currency, so there is no
//     operation that carries it into toman, dollars or grams of gold. A column
//     headed "return in USD" containing an index-point change is a fabrication
//     of comparability.
//   - A DISABLED instrument.
//
// None of the three is silently dropped: each appears in `warnings`, named,
// with the reason. An omission a client cannot detect is the thing this
// endpoint is most able to get wrong.

import (
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// performanceQuery is a validated /markets/performance request.
type performanceQuery struct {
	Window    window
	Numeraire numeraireSpec
}

// parsePerformanceQuery validates the query string. Pure function (unit
// tested): the caller supplies `now`, so there is no hidden clock.
func parsePerformanceQuery(q url.Values, now time.Time) (performanceQuery, *paramError) {
	out := performanceQuery{}
	num, perr := parseNumeraire(q.Get("numeraire"))
	if perr != nil {
		return out, perr
	}
	out.Numeraire = num

	w, perr := parseWindow(q.Get("period"), q.Get("from"), q.Get("to"), now)
	if perr != nil {
		return out, perr
	}
	out.Window = w
	return out, nil
}

// --- wire shapes ---------------------------------------------------------------

// numeraireBlock is the provenance of the unit of account itself. It is not in
// the original sketch of this contract and it is here anyway: a USD-denominated
// return in this system is computed through USD_IRT, which is the 24/7
// USDT/toman market used as a documented free-market proxy. A reader who cannot
// see that is reading a number whose largest caveat is invisible.
type numeraireBlock struct {
	Key    string `json:"key"`
	Unit   string `json:"unit"`
	NameEN string `json:"name_en"`
	NameFA string `json:"name_fa"`
	// Series is null for IRT, which converts an IRT-quoted asset by doing
	// nothing to it.
	Series       *string  `json:"series"`
	QualityTier  *string  `json:"quality_tier"`
	IsProxy      *bool    `json:"is_proxy"`
	CoverageFrom *string  `json:"coverage_from"`
	CoverageTo   *string  `json:"coverage_to"`
	Observations int      `json:"observations"`
	Notes        []string `json:"notes"`
}

// performanceItem is one instrument over the window. Every numeric field is a
// pointer: null means "this could not be computed from what is stored", the
// reason is in Notes, and no value is ever substituted for a missing one.
//
// EVERY RETURN CARRIES ITS OWN WINDOW. nominal_return_pct is measured over
// coverage_from..coverage_to; the two cross-numeraire returns are measured over
// their own <key>_return_from..<key>_return_to, which are SHORTER whenever the
// conversion series had not yet quoted; the real return is measured over
// real_return_from..real_return_to. Those windows genuinely differ -- on this
// deployment IR_COIN_EMAMI has history from 2010 while USD_IRT starts in 2011,
// so a period=max row prints a 2010-based nominal return beside a USD return
// that begins after the 2012 rial collapse. A percentage whose window is not
// published beside it is worse than no percentage, so each one ships its own.
type performanceItem struct {
	instrumentRef
	StartValue       *float64 `json:"start_value"`
	EndValue         *float64 `json:"end_value"`
	NominalReturnPct *float64 `json:"nominal_return_pct"`

	USDReturnPct          *float64 `json:"usd_return_pct"`
	USDReturnFrom         *string  `json:"usd_return_from"`
	USDReturnTo           *string  `json:"usd_return_to"`
	USDReturnObservations int      `json:"usd_return_observations"`

	GoldReturnPct          *float64 `json:"gold_return_pct"`
	GoldReturnFrom         *string  `json:"gold_return_from"`
	GoldReturnTo           *string  `json:"gold_return_to"`
	GoldReturnObservations int      `json:"gold_return_observations"`

	RealReturnPct  *float64 `json:"real_return_pct"`
	RealReturnFrom *string  `json:"real_return_from"`
	RealReturnTo   *string  `json:"real_return_to"`

	// ObservationVolatilityPct is the sample standard deviation of log returns
	// between CONSECUTIVE OBSERVATIONS, not between consecutive calendar days.
	// It was published as `daily_volatility_pct` and that name was wrong: these
	// symbols do not quote every day, so most of its steps span more than one
	// day and the sigma is per observation. The field is named for what it
	// measures. It is still not annualised.
	ObservationVolatilityPct *float64 `json:"observation_volatility_pct"`

	MaxDrawdownPct *float64 `json:"max_drawdown_pct"`
	Observations   int      `json:"observations"`
	CoverageFrom   *string  `json:"coverage_from"`
	CoverageTo     *string  `json:"coverage_to"`
	// Notes is this item's caveats, INCLUDING the registry's own note about the
	// instrument, which buildPerformanceItem copies in from
	// instrumentRef.RegistryNote.
	//
	// This field deliberately shadows instrumentRef's `notes` tag at depth 1:
	// encoding/json prefers the shallower field, so an item marshals `notes` as
	// this array. That is the intended wire shape, and it is the reason
	// instrumentRef publishes the same `notes` key as an array at depth 1 -- with
	// both named Notes the registry caveat vanished from the payload with no
	// compiler complaint and no test failure.
	Notes []string `json:"notes"`
}

// cpiProvenanceBlock is the deflator's identity, carried onto the response that
// uses it.
//
// economic.SeriesProvenance says of itself that "this struct is what the
// downstream payload copies those fields out of", and until this block existed
// nothing but .Code survived the trip -- a real return published against an
// invisible deflator. "+60% real" measured against the World Bank's ANNUAL
// 2010=100 rebase and "+60% real" measured against the Statistical Centre of
// Iran's monthly 1400=100 index are different claims, and a reader who cannot
// see base_period, measure and frequency cannot tell which one is on the page.
type cpiProvenanceBlock struct {
	Code               string `json:"code"`
	NameEN             string `json:"name_en"`
	NameFA             string `json:"name_fa"`
	Measure            string `json:"measure"`
	Unit               string `json:"unit"`
	Frequency          string `json:"frequency"`
	Calendar           string `json:"calendar"`
	BasePeriod         string `json:"base_period"`
	ProviderCode       string `json:"provider_code"`
	ProviderSeriesID   string `json:"provider_series_id"`
	QualityTier        string `json:"quality_tier"`
	SplicePolicy       string `json:"splice_policy"`
	SeasonalAdjustment string `json:"seasonal_adjustment"`
	// Notes is the catalog's caveat about the series itself -- for WB_CPI_IRN,
	// that it is the World Bank's rebase and NOT the SCI's own index.
	Notes string `json:"notes"`
	// CoverageTo repeats cpi_coverage_to inside the block so the deflator's
	// identity and its reach can be read as one object.
	CoverageTo *string `json:"coverage_to"`
}

type performanceResponse struct {
	// Period is null when the caller gave an explicit from/to, so the value can
	// always be handed straight back as ?period= without earning a 400.
	Period          *string        `json:"period"`
	Numeraire       string         `json:"numeraire"`
	NumeraireSeries numeraireBlock `json:"numeraire_series"`
	// From is null only for period=max on a deployment with no stored
	// observation at all; otherwise it is the window actually served.
	From          *time.Time `json:"from"`
	To            time.Time  `json:"to"`
	CPISeries     *string    `json:"cpi_series"`
	CPICoverageTo *string    `json:"cpi_coverage_to"`
	// CPIProvenance is the deflator's full identity, null when this deployment
	// carries no CPI. cpi_series and cpi_coverage_to remain as the two flat
	// fields they have always been; this block is what makes the real returns
	// interpretable.
	CPIProvenance *cpiProvenanceBlock `json:"cpi_provenance"`
	AsOf          time.Time           `json:"as_of"`

	Items []performanceItem `json:"items"`
	Count int               `json:"count"`
	// Warnings names every instrument left out of `items` and why, plus
	// anything true of the whole table. Never nil.
	Warnings []string `json:"warnings"`
}

// --- pure assembly --------------------------------------------------------------

// performanceInputs is everything the response is built from, already fetched.
// Separating it from the handler is what makes the numeraire rule, the drop
// accounting and the CPI sub-window testable without a database.
type performanceInputs struct {
	Query       performanceQuery
	Instruments []instrumentRow
	// Series holds FULL histories, not windowed ones. The numeraire's
	// carry-forward rule reaches back before the window, so slicing before
	// converting would silently drop the first days of every window whose first
	// day the numeraire did not quote.
	Series map[string]dailySeries
	CPI    cpiTable
	// CPIProvenance is nil when this deployment carries no CPI series at all.
	CPIProvenance *economic.SeriesProvenance
	CPIError      string
	AsOf          time.Time
}

// identityNumeraire reports whether expressing `inst` in `key` divides the
// instrument by itself.
//
// IR_GOLD_18K measured in grams of 18k gold, and USD_IRT measured in dollars,
// are 1.000 on every day by CONSTRUCTION -- they are the series that define
// those units. The return is therefore 0.00% whatever the market did, and a
// 0.00% printed in the same column as a measured return is indistinguishable
// from an asset that genuinely went nowhere. Those cells are published as null
// with the reason instead.
func identityNumeraire(inst instrumentRow, key string) bool {
	spec, ok := lookupNumeraire(key)
	return ok && spec.Series != "" && spec.Series == inst.Code
}

// numeraireLeg is a cross-numeraire return TOGETHER WITH the window it was
// actually measured over. The two are inseparable: applyStep drops every asset
// day before the conversion series' first quote, so this window is routinely
// shorter than the item's own coverage, and a percentage published without it
// silently answers a different question from the one beside it.
type numeraireLeg struct {
	Pct          *float64
	From         *time.Time
	To           *time.Time
	Observations int
	// Chain names the series the conversion leaned on, for the notes.
	Chain string
	// Note is non-empty exactly when Pct is nil.
	Note string
}

// numeraireReturn re-expresses `windowed` in `key` and returns its total return
// with the window it was measured over, or a null leg plus the reason. Pure
// function (unit tested).
func numeraireReturn(windowed dailySeries, inst instrumentRow, key string,
	sources map[string]dailySeries) numeraireLeg {
	if identityNumeraire(inst, key) {
		spec, _ := lookupNumeraire(key)
		return numeraireLeg{Note: fmt.Sprintf(
			"no %s return: %s is the series that DEFINES this unit, so measured in %s it "+
				"is 1.000 on every day by construction. The cell is null rather than "+
				"0.00%%, which would be indistinguishable from an asset that went nowhere.",
			key, inst.Code, spec.Unit)}
	}
	steps, ok := conversionSteps(inst.QuoteCurrency, key)
	if !ok {
		return numeraireLeg{Note: fmt.Sprintf(
			"no %s return: this instrument is quoted in %s, which is not a price in a "+
				"currency.", key, inst.QuoteCurrency)}
	}
	leg := numeraireLeg{Chain: stepSeriesNames(steps)}
	conv := convertSeries(windowed, steps, sources)
	if conv.MissingSeries != "" {
		leg.Note = fmt.Sprintf("no %s return: %s carries no stored observation in this "+
			"deployment, so the conversion cannot be performed.", key, conv.MissingSeries)
		return leg
	}
	r := totalReturnPct(conv.Points)
	if r == nil {
		leg.Note = fmt.Sprintf("no %s return: %d day(s) survived conversion, which cannot "+
			"express a return (%d dropped for want of a prior quote).",
			key, len(conv.Points), conv.NoPriorQuote)
		return leg
	}
	first, _ := conv.Points.first()
	last, _ := conv.Points.last()
	from, to := first.Day, last.Day
	leg.Pct, leg.From, leg.To, leg.Observations = r, &from, &to, len(conv.Points)
	return leg
}

// crossWindowNote states in words that a cross-numeraire return was measured
// over a different window from the nominal return printed beside it.
//
// The dates are published as <key>_return_from/_to whether or not this note
// fires; the note exists because a reader comparing "+300% nominal" with
// "+100% in USD" will not diff two pairs of dates first, and on this deployment
// those pairs really do differ by years.
func crossWindowNote(key string, leg numeraireLeg,
	nominalFrom, nominalTo time.Time, nominalObs int) string {
	if leg.Pct == nil || leg.From == nil || leg.To == nil {
		return ""
	}
	if leg.From.Equal(nominalFrom) && leg.To.Equal(nominalTo) && leg.Observations == nominalObs {
		return ""
	}
	chain := leg.Chain
	if chain == "" {
		chain = "the conversion series"
	}
	return fmt.Sprintf(
		"%s_return_pct is measured over %s..%s (%d observation(s)) -- a DIFFERENT window "+
			"from the nominal return beside it (%s..%s, %d observation(s)). %s had not "+
			"quoted at or before the missing days and they are not filled, so the two "+
			"percentages do not describe the same span and must not be differenced.",
		strings.ToLower(key), dayString(*leg.From), dayString(*leg.To), leg.Observations,
		dayString(nominalFrom), dayString(nominalTo), nominalObs, chain)
}

// stepSeriesNames lists the series a conversion chain leans on, for a note.
func stepSeriesNames(steps []convStep) string {
	names := make([]string, 0, len(steps))
	for _, s := range steps {
		names = append(names, s.Series)
	}
	return strings.Join(names, " then ")
}

// buildPerformanceItem projects one instrument onto the contract. Pure function
// (unit tested).
func buildPerformanceItem(inst instrumentRow, in performanceInputs) performanceItem {
	item := performanceItem{instrumentRef: refOf(inst), Notes: []string{}}
	// The registry's own caveat about the instrument, FIRST. It is the caveat a
	// reader most needs before reading any number below it -- that XAUUSD is a
	// COMEX front-month future and not the London spot fix, that USD_IRT is the
	// USDT/toman market -- and it reaches the payload only because
	// buildPerformanceItem copies it here; the identity block's own `notes` tag
	// is shadowed by this item's array. See performanceItem.Notes.
	if inst.Notes != "" {
		item.Notes = append(item.Notes, inst.Notes)
	}
	num := in.Query.Numeraire
	steps, ok := conversionSteps(inst.QuoteCurrency, num.Key)
	if !ok {
		// buildPerformanceResponse filters these out before reaching here, so
		// this is the belt to that braces. It used to be `steps, _ :=`, which
		// turned an unpriceable instrument into an IDENTITY conversion: an
		// index level would have been published as though its points were
		// already toman.
		item.Notes = append(item.Notes, fmt.Sprintf(
			"Cannot be expressed in %s: this instrument is quoted in %s, which is not a "+
				"price in a currency, so there is no operation that carries it into %s.",
			num.Key, inst.QuoteCurrency, num.Unit))
		return item
	}

	raw := in.Series[inst.Code]
	if len(raw) == 0 {
		item.Notes = append(item.Notes,
			"This deployment has stored no observation for this instrument, so every "+
				"figure below is withheld rather than estimated.")
		return item
	}
	windowed := raw.slice(in.Query.Window.From, in.Query.Window.To)
	if len(windowed) == 0 {
		first, _ := raw.first()
		last, _ := raw.last()
		item.Notes = append(item.Notes, fmt.Sprintf(
			"No observation inside this window. Stored coverage is %s..%s.",
			dayString(first.Day), dayString(last.Day)))
		return item
	}

	conv := convertSeries(windowed, steps, in.Series)
	if conv.MissingSeries != "" {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"Cannot be expressed in %s: %s carries no stored observation in this "+
				"deployment.", num.Key, conv.MissingSeries))
		return item
	}
	pts := conv.Points
	if len(pts) == 0 {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"Every one of the %d observation(s) in this window was dropped converting "+
				"into %s: %s had not quoted at or before them, and a later quote would "+
				"be look-ahead.", len(windowed), num.Key, stepSeriesNames(steps)))
		return item
	}

	if conv.NoPriorQuote > 0 {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"%d of %d day(s) dropped: %s had not quoted at or before them. Those days "+
				"are not filled -- the only value available is a LATER quote, and using "+
				"it would price the past with its own future.",
			conv.NoPriorQuote, len(windowed), stepSeriesNames(steps)))
	}
	if conv.NonPositiveQuote > 0 {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"%d day(s) dropped: the conversion series carried a non-positive value, "+
				"which is a data fault rather than a rate.", conv.NonPositiveQuote))
	}
	if conv.CarriedForward > 0 {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"On %d of %d day(s) the conversion used %s's last quote from an EARLIER "+
				"day, carried forward. Nothing is interpolated.",
			conv.CarriedForward, len(pts), stepSeriesNames(steps)))
	}

	start, _ := pts.first()
	end, _ := pts.last()
	item.StartValue = fp(start.Close)
	item.EndValue = fp(end.Close)
	item.Observations = len(pts)
	item.CoverageFrom = dayStringPtr(&start.Day)
	item.CoverageTo = dayStringPtr(&end.Day)

	// An asset expressed in the unit its own series defines is 1.000 every day.
	// Nothing derived from that constant is a measurement, so nothing derived
	// from it is published as a number.
	identity := identityNumeraire(inst, num.Key)
	if identity {
		item.Notes = append(item.Notes, fmt.Sprintf(
			"%s is the series that DEFINES this numeraire, so measured in %s it is 1.000 "+
				"on every day by construction. nominal_return_pct, "+
				"observation_volatility_pct, max_drawdown_pct and real_return_pct are "+
				"null rather than 0.00%%: a definitional identity and a measured zero are "+
				"different facts and must not render the same.", inst.Code, num.Unit))
	} else {
		item.NominalReturnPct = totalReturnPct(pts)
		item.MaxDrawdownPct = maxDrawdownPct(pts)
		item.ObservationVolatilityPct = observationVolatilityPct(pts)
		if item.ObservationVolatilityPct == nil {
			item.Notes = append(item.Notes,
				"No volatility: fewer than two usable log returns in this window, and a "+
					"standard deviation of one observation is not a dispersion.")
		} else if gaps := nonAdjacentSteps(pts); gaps > 0 {
			item.Notes = append(item.Notes, fmt.Sprintf(
				"observation_volatility_pct is the standard deviation of log returns "+
					"between consecutive OBSERVATIONS, and %d of %d step(s) span more than "+
					"one calendar day (this instrument does not quote every day, and "+
					"missing days are not interpolated), so it is not a per-day figure. It "+
					"is not annualised either: these symbols keep three different calendars "+
					"and no single sqrt(N) is right for them.", gaps, len(pts)-1))
		} else {
			item.Notes = append(item.Notes,
				"observation_volatility_pct is the standard deviation of log returns "+
					"between consecutive observations. Every step in this window happens to "+
					"span exactly one calendar day, so here it is also a per-day figure. It "+
					"is NOT annualised.")
		}
	}

	usd := numeraireReturn(windowed, inst, "USD", in.Series)
	item.USDReturnPct = usd.Pct
	item.USDReturnFrom, item.USDReturnTo = dayStringPtr(usd.From), dayStringPtr(usd.To)
	item.USDReturnObservations = usd.Observations
	gold := numeraireReturn(windowed, inst, "GOLD", in.Series)
	item.GoldReturnPct = gold.Pct
	item.GoldReturnFrom, item.GoldReturnTo = dayStringPtr(gold.From), dayStringPtr(gold.To)
	item.GoldReturnObservations = gold.Observations
	for _, leg := range []struct {
		key string
		val numeraireLeg
	}{{"USD", usd}, {"GOLD", gold}} {
		if leg.val.Note != "" {
			item.Notes = append(item.Notes, leg.val.Note)
			continue
		}
		if n := crossWindowNote(leg.key, leg.val, start.Day, end.Day, len(pts)); n != "" {
			item.Notes = append(item.Notes, n)
		}
	}

	// The real return deflates the SELECTED numeraire's nominal return -- the
	// one printed directly above it -- over the CPI-covered sub-window.
	if in.CPIProvenance == nil {
		if in.CPIError != "" {
			item.Notes = append(item.Notes, in.CPIError)
		}
	} else if identity {
		// Deflating a series that is 1.000 by construction would report minus
		// the inflation rate and dress it as this asset's real return.
		item.Notes = append(item.Notes,
			"No real return: deflating a series that is 1.000 by construction would "+
				"report the negative of Iranian inflation and label it this instrument's "+
				"real return.")
	} else {
		from := start.Day
		if in.Query.Window.From != nil {
			from = *in.Query.Window.From
		}
		rr := realReturn(pts, in.CPI, from, in.Query.Window.To)
		item.RealReturnPct = rr.Pct
		item.RealReturnFrom = dayStringPtr(rr.From)
		item.RealReturnTo = dayStringPtr(rr.To)
		item.Notes = append(item.Notes, rr.Note)
		if rr.Pct != nil && num.Key != "IRT" {
			item.Notes = append(item.Notes, fmt.Sprintf(
				"The deflator is IRAN's consumer price index, applied here to a return "+
					"measured in %s. It answers \"what did this buy in Iran\", not "+
					"\"what was the real return to a %s-based holder\".", num.Unit, num.Key))
		}
	}
	return item
}

// buildPerformanceResponse assembles the whole table. Pure function (unit
// tested): every value in it has already been fetched by the time it is called.
func buildPerformanceResponse(in performanceInputs) performanceResponse {
	num := in.Query.Numeraire
	out := performanceResponse{
		Period:    in.Query.Window.Period,
		Numeraire: num.Key,
		To:        in.Query.Window.To,
		AsOf:      in.AsOf.UTC(),
		Items:     []performanceItem{},
		Warnings:  []string{},
	}
	out.NumeraireSeries = buildNumeraireBlock(num, in.Instruments, in.Series)

	if in.CPIProvenance != nil {
		code := in.CPIProvenance.Code
		out.CPISeries = &code
		out.CPICoverageTo = dayStringPtr(in.CPI.coverageTo())
		out.CPIProvenance = cpiProvenanceOf(*in.CPIProvenance, out.CPICoverageTo)
	} else if in.CPIError != "" {
		out.Warnings = append(out.Warnings, in.CPIError)
	}

	if in.Query.Window.FutureToRequested {
		out.Warnings = append(out.Warnings,
			"The requested `to` is later than as_of; the window was served to as_of, "+
				"because no observation can exist after it.")
	}

	rates := []string{}
	unpriceable := []string{}
	disabled := []string{}
	for _, inst := range in.Instruments {
		if !inst.Enabled {
			disabled = append(disabled, inst.Code)
			continue
		}
		if isRateQuote(inst.QuoteCurrency) {
			rates = append(rates, inst.Code)
			continue
		}
		if _, ok := conversionSteps(inst.QuoteCurrency, num.Key); !ok {
			unpriceable = append(unpriceable, inst.Code+" ("+inst.QuoteCurrency+")")
			continue
		}
		out.Items = append(out.Items, buildPerformanceItem(inst, in))
	}
	out.Count = len(out.Items)

	if len(rates) > 0 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"Excluded as RATES rather than prices: %s. They are quoted in percent, so a "+
				"\"return\" on them would be a percent change of a percentage -- a "+
				"meaningless number, not an approximate one.", strings.Join(rates, ", ")))
	}
	if len(unpriceable) > 0 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"Excluded as not denominated in any currency: %s. There is no operation "+
				"that carries an index level into %s, and printing its point change in a "+
				"%s column would fabricate comparability.",
			strings.Join(unpriceable, ", "), num.Key, num.Unit))
	}
	if len(disabled) > 0 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"Excluded as disabled in the instrument registry: %s.", strings.Join(disabled, ", ")))
	}

	// period=max has no caller-supplied lower bound; the window actually served
	// starts at the earliest day any surviving item could be converted on.
	if in.Query.Window.From != nil {
		f := in.Query.Window.From.UTC()
		out.From = &f
	} else {
		for _, item := range out.Items {
			if item.CoverageFrom == nil {
				continue
			}
			d, err := time.Parse(dateLayout, *item.CoverageFrom)
			if err != nil {
				continue
			}
			if out.From == nil || d.Before(*out.From) {
				u := d.UTC()
				out.From = &u
			}
		}
	}

	today := floorDay(in.AsOf)
	for _, item := range out.Items {
		if item.CoverageTo != nil && *item.CoverageTo == dayString(today) {
			out.Warnings = append(out.Warnings, fmt.Sprintf(
				"The final UTC day (%s) is still forming: its close is the latest "+
					"observation so far, not a settled daily close.", dayString(today)))
			break
		}
	}
	return out
}

// buildNumeraireBlock describes the unit of account and its backing series.
// Pure function (unit tested).
func buildNumeraireBlock(num numeraireSpec, instruments []instrumentRow,
	series map[string]dailySeries) numeraireBlock {
	block := numeraireBlock{
		Key: num.Key, Unit: num.Unit, NameEN: num.NameEN, NameFA: num.NameFA,
		Notes: []string{num.Note},
	}
	if num.Series == "" {
		return block
	}
	code := num.Series
	block.Series = &code
	if inst, ok := findInstrument(instruments, num.Series); ok {
		tier, proxy := inst.QualityTier, inst.IsProxy
		block.QualityTier, block.IsProxy = &tier, &proxy
		if inst.Notes != "" {
			block.Notes = append(block.Notes, inst.Notes)
		}
	}
	s := series[num.Series]
	block.Observations = len(s)
	if first, ok := s.first(); ok {
		block.CoverageFrom = dayStringPtr(&first.Day)
	}
	if last, ok := s.last(); ok {
		block.CoverageTo = dayStringPtr(&last.Day)
	}
	return block
}

// cpiProvenanceOf copies the deflator's identity out of the economic package's
// provenance struct. Every field it carries is copied; a provenance field that
// existed but was not published would be the same defect this function exists
// to close.
func cpiProvenanceOf(p economic.SeriesProvenance, coverageTo *string) *cpiProvenanceBlock {
	return &cpiProvenanceBlock{
		Code:               p.Code,
		NameEN:             p.NameEN,
		NameFA:             p.NameFA,
		Measure:            p.Measure,
		Unit:               p.Unit,
		Frequency:          p.Frequency,
		Calendar:           p.Calendar,
		BasePeriod:         p.BasePeriod,
		ProviderCode:       p.ProviderCode,
		ProviderSeriesID:   p.ProviderSeriesID,
		QualityTier:        p.QualityTier,
		SplicePolicy:       p.SplicePolicy,
		SeasonalAdjustment: p.SeasonalAdjustment,
		Notes:              p.Notes,
		CoverageTo:         coverageTo,
	}
}

// --- handler --------------------------------------------------------------------

// conversionSeriesCodes are the market series any request may need, whatever
// numeraire was asked for: usd_return_pct and gold_return_pct are reported on
// every row, so both conversion series are always loaded.
var conversionSeriesCodes = []string{usdSeriesCode, goldSeriesCode}

// priceableSymbols is every symbol whose daily series must be read: each
// enabled instrument that is a price in a currency, plus the conversion series.
func priceableSymbols(instruments []instrumentRow) []string {
	seen := map[string]bool{}
	out := []string{}
	add := func(code string) {
		if !seen[code] {
			seen[code] = true
			out = append(out, code)
		}
	}
	for _, inst := range instruments {
		if !inst.Enabled || isRateQuote(inst.QuoteCurrency) {
			continue
		}
		if inst.QuoteCurrency == quoteIRT || inst.QuoteCurrency == quoteUSD {
			add(inst.Code)
		}
	}
	for _, code := range conversionSeriesCodes {
		add(code)
	}
	return out
}

// unbackedNumeraire is the 400 for a numeraire this deployment cannot perform.
// It is a refusal and not an empty table: a client that asked for GOLD terms
// and received a page of nulls cannot tell "gold did nothing" from "this server
// has never collected a gold price".
func unbackedNumeraire(w http.ResponseWriter, num numeraireSpec) {
	httpserver.BadRequest(w, fmt.Sprintf(
		"numeraire %s cannot be served by this deployment: its backing series %s has no "+
			"stored observation", num.Key, num.Series),
		map[string]any{
			"numeraire": num.Key,
			"series":    num.Series,
			"supported": numeraireKeys(),
		})
}

// Performance implements
// GET /api/v1/markets/performance?period=&numeraire=&from=&to=.
func (h *Handler) Performance(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	q, perr := parsePerformanceQuery(r.URL.Query(), now)
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	ctx := r.Context()

	instruments, err := h.loadInstruments(ctx)
	if err != nil {
		h.Log.Error("performance_instruments", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	series, err := h.loadDailySeries(ctx, priceableSymbols(instruments), now)
	if err != nil {
		h.Log.Error("performance_series", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	if q.Numeraire.Series != "" && len(series[q.Numeraire.Series]) == 0 {
		unbackedNumeraire(w, q.Numeraire)
		return
	}

	in := performanceInputs{
		Query:       q,
		Instruments: instruments,
		Series:      series,
		AsOf:        now,
	}
	// The CPI is read through internal/economic's exported point-in-time rule,
	// never re-implemented here. Its absence is a degraded response (real
	// returns withheld, warning stated), not an error: the nominal table is
	// still true without a deflator.
	prov, obs, cpiErr := economic.PointInTime(ctx, h.Pool, cpiSeriesCode, now)
	switch {
	case cpiErr == nil:
		in.CPIProvenance = &prov
		in.CPI = buildCPITable(obs)
		if len(in.CPI) == 0 {
			in.CPIProvenance = nil
			in.CPIError = fmt.Sprintf(
				"No real returns: %s is registered but carries no observation available "+
					"at this cutoff.", cpiSeriesCode)
		}
	case errors.Is(cpiErr, economic.ErrSeriesNotFound):
		in.CPIError = fmt.Sprintf(
			"No real returns: this deployment does not carry the CPI series %s, so "+
				"nothing here is deflated.", cpiSeriesCode)
	default:
		h.Log.Error("performance_cpi", "error", cpiErr)
		httpserver.Internal(w, "database error")
		return
	}

	httpserver.JSON(w, http.StatusOK, buildPerformanceResponse(in))
}
