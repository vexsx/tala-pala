package relvalue

// The EXPORTED slice of this package: what a stored value is worth in a chosen
// unit of account, for a caller that holds its own price series.
//
// WHY THIS FILE EXISTS. internal/equities serves the Tehran roster and its
// bars, and its screener has to express a share price in dollars and in grams
// of 18k gold. There are exactly two ways to do that: re-implement the
// carry-forward rule over USD_IRT and IR_GOLD_18K inside that package, or call
// this one. The first is how a system ends up with two answers to "what was
// فولاد worth in gold on 2022-08-09?" — and the two would diverge the first
// time either side changed what it does with a day the numeraire never quoted.
// So the rule stays here, in ONE implementation, and this file is the door.
//
// WHAT IT DOES NOT RELAX. Everything below runs through convertSeries and
// applyStep, so the four rules the package is built on hold unchanged for an
// external caller:
//
//   - The numeraire is carried FORWARD only. A day's rate is the last close AT
//     OR BEFORE that day; a later quote did not exist yet and using it would
//     price history with information from its own future.
//   - A day with no prior quote is DROPPED, never guessed. On this surface a
//     dropped single value comes back as a nil Value with a Reason, and a
//     dropped series day is absent from Points and counted in
//     DroppedNoPriorQuote.
//   - A non-positive stored quote is a data fault, not a rate. The value is
//     withheld rather than divided by it.
//   - Nothing is interpolated, at any point, for any reason.
//
// TOMAN, NOT RIAL. Every entry point here takes TOMAN, because rule 1 of this
// package is that IRT is canonical for every Iranian value and "IRR is a
// display-only x10 [that] never appears in this package at all". A caller
// holding rials — internal/equities does, TSETMC quotes rial — divides by ten
// itself, in its own code, where the factor of ten is visible next to the
// exchange that caused it. There is deliberately no ConvertRial: a rial door
// into this package would put the x10 somewhere the reader of a rial payload
// never looks.

import (
	"context"
	"fmt"
	"math"
	"sort"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// The numeraire keys an external caller may name. They are the same closed
// vocabulary parseNumeraire enforces, exported so a caller can spell them
// without copying string literals that nothing would keep in step.
const (
	// NumeraireToman is the hub and the identity conversion: a toman value
	// expressed in toman is itself, and needs no backing series.
	NumeraireToman = "IRT"
	NumeraireUSD   = "USD"
	NumeraireGold  = "GOLD"
)

// The market series that back the two non-identity numeraires. Exported for
// the same reason as the keys, and used by tests to build a converter over
// explicit quotes.
const (
	SeriesUSD     = usdSeriesCode
	SeriesGold18K = goldSeriesCode
)

// Point is one day's value. Day is floored onto its UTC day by every function
// that accepts one, so a caller passing a Tehran-local timestamp and a caller
// passing its UTC equivalent land on the same bucket.
type Point struct {
	Day   time.Time
	Value float64
}

// TomanValue is ONE toman amount re-expressed in a numeraire, together with
// everything a reader needs to judge it.
//
// Value and Reason are mutually exclusive and exhaustive: exactly one of them
// is set. There is no third state where a number arrives with no account of
// itself, and there is no state where a missing number arrives as zero — a
// zero in a price column is a claim that the asset is worthless.
type TomanValue struct {
	// Value is nil when the conversion could not be performed. Reason then
	// says which of the rules withheld it.
	Value *float64
	// RateDay is the day of the numeraire quote actually used — which is the
	// value's day when the numeraire quoted that day, and an EARLIER day when
	// the rate was carried forward. nil whenever Value is nil. On a two-leg
	// chain it is the OLDEST quote used, because that is the leg the result's
	// staleness is governed by.
	RateDay *time.Time
	// CarriedForward is true when at least one leg used a quote from an
	// earlier day than the value's. Nothing is wrong with such a figure, and a
	// reader is still entitled to know that it rests on a stale rate.
	CarriedForward bool
	// Reason is empty exactly when Value is non-nil.
	Reason string
	// Chain names the series the conversion leaned on, in order. Empty for the
	// identity conversion.
	Chain []string
	// Unit is what one converted value MEANS: "toman", "USD", or "grams of 18k
	// gold". It travels with the number because "0.000062 gold" is not a unit
	// and "grams of 18k gold" is not the same claim as troy ounces of fine
	// metal.
	Unit string
}

// SeriesConversion is a whole toman series re-expressed in a numeraire, plus a
// full account of what the conversion could not carry.
//
// The counts are not diagnostics. A return computed over Points is measured
// over a window that may be SHORTER than the window the caller asked for —
// applyStep drops every day before the numeraire's first quote — and a
// percentage whose window is not published beside it answers a different
// question from the one it appears to answer. A caller that ignores these
// fields is publishing an unlabelled window.
type SeriesConversion struct {
	// Points is ascending by Day, and shorter than the input by exactly
	// DroppedNoPriorQuote + DroppedNonPositiveQuote.
	Points []Point
	// CarriedForward counts converted days whose numeraire quote came from an
	// earlier day.
	CarriedForward int
	// DroppedNoPriorQuote counts input days the numeraire had never quoted at
	// or before. They are not guessable and are not guessed.
	DroppedNoPriorQuote int
	// DroppedNonPositiveQuote counts input days whose carried-forward quote was
	// zero or negative — a stored fault rather than a rate.
	DroppedNonPositiveQuote int
	// MissingSeries names the first conversion series this deployment does not
	// carry at all, or "" when every leg resolved. It is a different failure
	// from a dropped day: the deployment cannot perform this numeraire at all,
	// and a caller should refuse the request rather than publish a page of
	// nulls.
	MissingSeries string
	// Reason is non-empty when the conversion could not be attempted — an
	// unknown numeraire key, or a chain that does not exist. Points is then
	// empty.
	Reason string
	Chain  []string
	Unit   string
}

// NumeraireInfo is the unit of account's own identity and reach, in the SAME
// shape /api/v1/markets/performance publishes it as `numeraire_series`. It is
// copied field by field rather than embedded so that a payload built by
// another package and a payload built by this one cannot describe the same
// numeraire with different keys.
type NumeraireInfo struct {
	Key    string `json:"key"`
	Unit   string `json:"unit"`
	NameEN string `json:"name_en"`
	NameFA string `json:"name_fa"`
	// Series is null for the toman hub, which converts by doing nothing.
	Series       *string  `json:"series"`
	QualityTier  *string  `json:"quality_tier"`
	IsProxy      *bool    `json:"is_proxy"`
	CoverageFrom *string  `json:"coverage_from"`
	CoverageTo   *string  `json:"coverage_to"`
	Observations int      `json:"observations"`
	Notes        []string `json:"notes"`
	// Available is what a caller should gate a menu on, and what it should
	// refuse a request against. False means every figure in this unit would be
	// null, and a page of nulls cannot be told apart from a market that did
	// nothing.
	Available bool `json:"available"`
	// UnavailableReason names the missing series rather than saying "not
	// configured", because the fix is an ingest and not a setting.
	UnavailableReason *string `json:"unavailable_reason"`
}

// Converter holds the conversion series once, so a caller converting twenty
// symbols reads USD_IRT and IR_GOLD_18K ONCE rather than per symbol.
type Converter struct {
	asOf        time.Time
	instruments []instrumentRow
	sources     map[string]dailySeries
}

// NewConverter loads the conversion series this deployment carries, up to
// (and excluding) asOf.
//
// Both series are loaded whatever numeraire the caller intends to use: a
// screener publishes a USD price and a gold price on every row, so asking
// which one is "the" numeraire before reading them would only produce a second
// round trip.
func NewConverter(ctx context.Context, pool *pgxpool.Pool, asOf time.Time) (*Converter, error) {
	h := &Handler{Pool: pool}
	instruments, err := h.loadInstruments(ctx)
	if err != nil {
		return nil, err
	}
	sources, err := h.loadDailySeries(ctx, conversionSeriesCodes, asOf)
	if err != nil {
		return nil, err
	}
	return &Converter{asOf: asOf.UTC(), instruments: instruments, sources: sources}, nil
}

// NewConverterFromQuotes builds a converter over explicit numeraire quotes,
// with no database.
//
// It exists so a caller's own build*Response functions can be tested against
// the REAL carry-forward rule instead of a hand-rolled stand-in. That
// distinction is the whole point: a fake converter in another package's test
// would assert that package's belief about the rule, which is exactly the
// second implementation this file exists to prevent.
//
// The registry provenance (quality_tier, is_proxy, the instrument's own
// caveat) is absent from a converter built this way, so NumeraireInfo reports
// coverage and notes but leaves those fields null.
func NewConverterFromQuotes(asOf time.Time, quotes map[string][]Point) *Converter {
	sources := map[string]dailySeries{}
	for code, points := range quotes {
		sources[code] = toDailySeries(points)
	}
	return &Converter{asOf: asOf.UTC(), sources: sources}
}

// AsOf is the cutoff the conversion series were read at.
func (c *Converter) AsOf() time.Time { return c.asOf }

// NumeraireInfo describes one unit of account against what is actually stored.
// The second return is false for a key outside the closed vocabulary.
func (c *Converter) NumeraireInfo(key string) (NumeraireInfo, bool) {
	spec, ok := lookupNumeraire(key)
	if !ok {
		return NumeraireInfo{}, false
	}
	block := buildNumeraireBlock(spec, c.instruments, c.sources)
	info := NumeraireInfo{
		Key:          block.Key,
		Unit:         block.Unit,
		NameEN:       block.NameEN,
		NameFA:       block.NameFA,
		Series:       block.Series,
		QualityTier:  block.QualityTier,
		IsProxy:      block.IsProxy,
		CoverageFrom: block.CoverageFrom,
		CoverageTo:   block.CoverageTo,
		Observations: block.Observations,
		Notes:        block.Notes,
		Available:    true,
	}
	if spec.Series != "" && len(c.sources[spec.Series]) == 0 {
		reason := fmt.Sprintf(
			"%s carries no stored observation in this deployment, so no value can be "+
				"converted into %s.", spec.Series, spec.Key)
		info.Available = false
		info.UnavailableReason = &reason
	}
	return info, true
}

// NumeraireKeys is the closed vocabulary, in publication order.
func NumeraireKeys() []string { return numeraireKeys() }

// ConvertToman re-expresses one toman amount, observed on `day`, in `key`.
//
// The value itself is produced by convertSeries over a one-point series, so
// this entry point cannot drift from the series entry point below or from
// /markets/performance: all three are the same walk. What this function adds
// is the account a single figure needs — which quote day was used, and whether
// it was carried forward — which a series conversion reports as totals.
func (c *Converter) ConvertToman(day time.Time, toman float64, key string) TomanValue {
	spec, ok := lookupNumeraire(key)
	if !ok {
		return TomanValue{Reason: fmt.Sprintf(
			"%q is not a unit of account this system knows; supported: %v", key, numeraireKeys())}
	}
	steps, ok := conversionSteps(quoteIRT, key)
	if !ok {
		return TomanValue{Unit: spec.Unit, Reason: fmt.Sprintf(
			"no conversion carries a toman value into %s", spec.Key)}
	}
	out := TomanValue{Unit: spec.Unit, Chain: chainCodes(steps)}

	d := floorDay(day)
	conv := convertSeries(dailySeries{{Day: d, Close: toman}}, steps, c.sources)
	if len(conv.Points) == 1 {
		out.Value = fpUnit(conv.Points[0].Close)
		if out.Value == nil {
			// Unreachable with a positive quote and a stored NUMERIC close,
			// and still handled: a non-finite value that reached a payload
			// would truncate the JSON body rather than announce itself.
			out.Reason = fmt.Sprintf(
				"the %s conversion of %v toman on %s is not a finite number",
				spec.Key, toman, dayString(d))
			return out
		}
		// The staleness of the result is the staleness of its OLDEST leg.
		for _, step := range steps {
			q, found := c.sources[step.Series].quoteAtOrBefore(d)
			if !found {
				continue
			}
			if !q.Day.Equal(d) {
				out.CarriedForward = true
			}
			if out.RateDay == nil || q.Day.Before(*out.RateDay) {
				qd := q.Day
				out.RateDay = &qd
			}
		}
		return out
	}

	switch {
	case conv.MissingSeries != "":
		out.Reason = fmt.Sprintf(
			"%s carries no stored observation in this deployment, so no value can be "+
				"expressed in %s.", conv.MissingSeries, spec.Key)
	case conv.NonPositiveQuote > 0:
		out.Reason = fmt.Sprintf(
			"the %s quote in force on %s is zero or negative, which is a stored fault "+
				"rather than a rate; the value is withheld rather than divided by it.",
			chainLabel(steps), dayString(d))
	default:
		out.Reason = fmt.Sprintf(
			"%s has no quote at or before %s, and a rate is carried FORWARD only — "+
				"using a later quote would price this day with information from its own "+
				"future, so the value is withheld rather than guessed.",
			chainLabel(steps), dayString(d))
	}
	return out
}

// ConvertTomanSeries re-expresses a whole toman series in `key`.
//
// The input is copied and sorted before conversion: applyStep is a merge walk
// that assumes both sides ascend, and making that a property of this function
// rather than a promise from the caller is the same discipline loadDailySeries
// applies to its own ORDER BY.
func (c *Converter) ConvertTomanSeries(points []Point, key string) SeriesConversion {
	spec, ok := lookupNumeraire(key)
	if !ok {
		return SeriesConversion{Points: []Point{}, Reason: fmt.Sprintf(
			"%q is not a unit of account this system knows; supported: %v", key, numeraireKeys())}
	}
	steps, ok := conversionSteps(quoteIRT, key)
	if !ok {
		return SeriesConversion{Points: []Point{}, Unit: spec.Unit, Reason: fmt.Sprintf(
			"no conversion carries a toman value into %s", spec.Key)}
	}
	conv := convertSeries(toDailySeries(points), steps, c.sources)
	out := SeriesConversion{
		Points:                  fromDailySeries(conv.Points),
		CarriedForward:          conv.CarriedForward,
		DroppedNoPriorQuote:     conv.NoPriorQuote,
		DroppedNonPositiveQuote: conv.NonPositiveQuote,
		MissingSeries:           conv.MissingSeries,
		Chain:                   chainCodes(steps),
		Unit:                    spec.Unit,
	}
	if conv.MissingSeries != "" {
		out.Reason = fmt.Sprintf(
			"%s carries no stored observation in this deployment, so no value can be "+
				"expressed in %s.", conv.MissingSeries, spec.Key)
	}
	return out
}

// --- helpers ------------------------------------------------------------------

// quoteAtOrBefore is the carry-forward rule as a lookup: the last point at or
// before `day`, or false when the series had not started yet.
//
// It is the SAME rule applyStep's merge walk applies, written as a binary
// search because ConvertToman resolves one day rather than a whole series.
// Two spellings of one rule is exactly the drift this file exists to avoid, so
// it is used only to REPORT which quote was used — the value itself always
// comes from convertSeries — and TestQuoteAtOrBeforeAgreesWithApplyStep pins
// the two together over a fixture with holes at both ends.
func (s dailySeries) quoteAtOrBefore(day time.Time) (dailyPoint, bool) {
	d := floorDay(day)
	// The first index whose Day is strictly after d; everything before it is
	// at or before d, and the series ascends.
	i := sort.Search(len(s), func(i int) bool { return s[i].Day.After(d) })
	if i == 0 {
		return dailyPoint{}, false
	}
	return s[i-1], true
}

// toDailySeries copies exported points into the package's own series type,
// flooring each day and sorting ascending.
func toDailySeries(points []Point) dailySeries {
	out := make(dailySeries, 0, len(points))
	for _, p := range points {
		out = append(out, dailyPoint{Day: floorDay(p.Day), Close: p.Value})
	}
	sort.SliceStable(out, func(i, j int) bool { return out[i].Day.Before(out[j].Day) })
	return out
}

// fromDailySeries copies back out. Never nil: an empty conversion is an empty
// slice, so a caller ranging over it has nothing to nil-check.
func fromDailySeries(s dailySeries) []Point {
	out := make([]Point, 0, len(s))
	for _, p := range s {
		out = append(out, Point{Day: p.Day, Value: p.Close})
	}
	return out
}

func chainCodes(steps []convStep) []string {
	out := make([]string, 0, len(steps))
	for _, s := range steps {
		out = append(out, s.Series)
	}
	return out
}

// chainLabel names the chain for a refusal message. The identity conversion
// has no chain and cannot produce one of these messages.
func chainLabel(steps []convStep) string {
	codes := chainCodes(steps)
	switch len(codes) {
	case 0:
		return "the identity conversion"
	case 1:
		return codes[0]
	default:
		return fmt.Sprintf("%v", codes)
	}
}

// fpUnit guards a converted PRICE for JSON without imposing a decimal
// precision on it.
//
// fp's six decimals are right for a percentage and wrong for a price in grams
// of gold: one share of a 500-toman stock is 0.0000625 grams, which six
// decimals round to 0.000063 — a 0.8% error introduced by a display
// convention, in a column whose whole purpose is to be small. This rounds to
// twelve SIGNIFICANT digits instead, which removes float noise and leaves the
// value alone. Like fp, it returns nil for a non-finite value, because
// encoding/json refuses to encode one and httpserver.JSON discards the
// encoder's error — a single NaN would truncate the response body mid-object.
func fpUnit(v float64) *float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	if v == 0 {
		z := 0.0
		return &z
	}
	e := math.Floor(math.Log10(math.Abs(v)))
	if e < -290 || e > 290 {
		r := v
		return &r
	}
	mag := math.Pow(10, 11-e)
	scaled := v * mag
	if math.IsInf(scaled, 0) {
		r := v
		return &r
	}
	r := math.Round(scaled) / mag
	return &r
}
