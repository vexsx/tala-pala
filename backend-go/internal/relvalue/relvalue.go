// Package relvalue serves the numeraire and relative-value engine: what a
// stored asset was worth in a chosen unit of account, how that changed over a
// window, and how far two assets have drifted apart.
//
// # WHAT THIS PACKAGE IS ALLOWED TO DO
//
// Arithmetic over STORED observations, and nothing else. That is already this
// repository's boundary for Go: internal/prices/candles.go synthesizes candles
// from ticks, internal/indicators computes moving averages and a rolling
// correlation. Indexing a series to 100, dividing it by another series,
// differencing it into returns, and locating today's value in the distribution
// of its own history are the same class of operation. Nothing here forecasts,
// nothing here trains, nothing here calibrates, and nothing here writes.
//
// FIVE RULES THIS FILE AND ITS SIBLINGS ARE BUILT AROUND
//
//  1. IRT (toman) is canonical for every Iranian value. IRR is a display-only
//     x10 and never appears in this package at all. A conversion chain that
//     would mix the two does not exist here: every conversion routes through
//     the IRT hub (see conversionSteps).
//
//  2. Every stored timestamp is UTC and every bucket is a UTC day floor,
//     produced by the SAME expression candles.go uses. Tehran and Jalali are
//     display concerns and never touch bucketing. A chart and this endpoint
//     must not be able to disagree about what a day's close is.
//
//  3. Invalid input is REFUSED with a specific 400 -- an unknown code, a
//     numeraire this deployment cannot back, a rate asked to behave like a
//     price, an inverted window, a period outside the vocabulary, points
//     outside 1..2000. Nothing is clamped and nothing is substituted, which is
//     the pattern candles.go established: a caller that asked for 15m and
//     silently received 1h cannot tell that its chart is wrong.
//
//  4. NO number is ever fabricated. Where an input series does not cover what
//     a field needs, the field is null and a note says why. There is no
//     interpolation anywhere in this package: not of a missing daily close,
//     not of the annual CPI onto days, not of a numeraire onto a day it never
//     quoted. A withheld number is correct; an interpolated one is a lie
//     wearing a measurement's clothes.
//
//  5. Research register. Every string this package emits describes what HAS
//     happened over a measured window. "has lagged X% over this window" is a
//     statement about the past; "is due to catch up" is a forecast, and this
//     package does not make those.
//
// The one judgement call worth stating up front is the evidence gate on the
// gap percentile. A percentile computed from overlapping rolling windows has an
// enormous denominator and almost no information: 880 overlapping one-year
// windows drawn from three years of history are three observations wearing a
// statistic's clothes. So both counts are published, and the percentile itself
// is withheld unless the INDEPENDENT count clears its bar. That is the same
// refusal MIN_SCORED_FOR_COVERAGE makes in prediction-python's
// app/models/intervals.py, applied to a different denominator.
package relvalue

import (
	"fmt"
	"log/slog"
	"math"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/economic"
)

// Handler serves /api/v1/markets/performance, /api/v1/relative-value and
// /api/v1/markets/numeraires.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger
}

// dateLayout renders a calendar date. A CPI reference period is a DATE column
// and a daily bucket is a UTC day; serializing either with a time of day would
// invent precision the value does not have.
const dateLayout = "2006-01-02"

// daySeconds is the daily bucket width. It is the same 86400 candles.go floors
// on, spelled out here so the SQL below and the Go below cannot drift.
const daySeconds int64 = 86400

// cpiSeriesCode is the deflator. It is the World Bank's mirror of Iran's CPI,
// rebased by the World Bank to 2010=100 -- explicitly NOT the Statistical
// Centre of Iran's own 1400=100 index, which this deployment does not yet
// ingest (the SCI publishes it as PDFs whose older paths 404). The series' own
// `notes` carry that caveat and it is echoed onto every response that uses it,
// as cpi_provenance.notes -- along with the base_period, measure, frequency and
// quality_tier that say WHICH price index the real returns were deflated by.
// See cpiProvenanceBlock: for a while this comment was a promise the payload
// did not keep, because only .Code survived the trip out of economic.PointInTime.
//
// It is ANNUAL. Everything about how a real return is computed in this package
// follows from that one fact; see realReturn in cpi.go.
const cpiSeriesCode = "WB_CPI_IRN"

// --- refusals ----------------------------------------------------------------

// paramError carries the message and details of a rejected parameter so each
// handler writes exactly one kind of 400. Same shape as candles.go and
// economic/handlers.go: caller input is refused with a specific message, never
// clamped, defaulted or silently substituted.
type paramError struct {
	Message string
	Details map[string]any
}

func (e *paramError) Error() string { return e.Message }

func badParam(message string, details map[string]any) *paramError {
	return &paramError{Message: message, Details: details}
}

// --- instants ----------------------------------------------------------------

// parseInstant accepts RFC3339 or bare unix seconds for ?from/?to, and refuses
// an integer outside the plausible-epoch window.
//
// The bounds are economic.MinEpochSeconds/MaxEpochSeconds -- the same numbers,
// imported rather than re-declared, so the two endpoints cannot drift apart on
// what counts as an epoch. What they catch is spelled out at their definition;
// the short version is that ?from=2015 read as seconds lands in 1970, which
// this endpoint would answer with a perfectly well-formed ten-year window that
// is not the one anybody asked for, and ?to=1757376000000 (JavaScript
// milliseconds) lands in the year 57000.
//
// The message is this package's own, because the details a caller needs here
// name this package's parameters.
func parseInstant(name, raw string) (time.Time, *paramError) {
	if n, err := strconv.ParseInt(raw, 10, 64); err == nil {
		if n < economic.MinEpochSeconds || n > economic.MaxEpochSeconds {
			return time.Time{}, badParam(
				fmt.Sprintf("%s must be RFC3339 (2026-03-01T00:00:00Z) or unix seconds "+
					"between %d and %d, got %q: a year, a YYYYMMDD date or milliseconds "+
					"is not an epoch",
					name, economic.MinEpochSeconds, economic.MaxEpochSeconds, raw),
				map[string]any{
					name:               raw,
					"accepted_formats": []string{"RFC3339", "unix seconds"},
					"epoch_seconds_range": []int64{
						economic.MinEpochSeconds, economic.MaxEpochSeconds,
					},
				})
		}
		return time.Unix(n, 0).UTC(), nil
	}
	// A bare calendar date is accepted too: this endpoint's windows are read
	// and written by humans in dates far more often than in instants, and
	// 2015-09-09 is unambiguous where 1441756800 is not.
	if d, err := time.Parse(dateLayout, raw); err == nil {
		return d.UTC(), nil
	}
	t, err := time.Parse(time.RFC3339, raw)
	if err != nil {
		return time.Time{}, badParam(
			fmt.Sprintf("%s must be a date (YYYY-MM-DD), RFC3339 or unix seconds, got %q", name, raw),
			map[string]any{
				name:               raw,
				"accepted_formats": []string{"YYYY-MM-DD", "RFC3339", "unix seconds"},
			})
	}
	return t.UTC(), nil
}

// floorDay floors an instant onto its UTC day boundary. It is the Go twin of
// the SQL bucket expression in dailyCloseSelect, and the twin of
// candleInterval.BucketStart at 1d. Epoch flooring works on the instant, so a
// Tehran +03:30 timestamp lands on the same UTC day its UTC equivalent does --
// which is the point: display timezone never touches bucketing.
func floorDay(t time.Time) time.Time {
	s := t.Unix()
	q := s / daySeconds
	// Go truncates integer division toward zero; SQL's floor() does not.
	if s < 0 && s%daySeconds != 0 {
		q--
	}
	return time.Unix(q*daySeconds, 0).UTC()
}

// daysBetween is the whole-day distance between two UTC day floors. Both
// arguments are day floors by construction, so this is exact integer
// arithmetic and never a rounded duration.
func daysBetween(from, to time.Time) int {
	return int(floorDay(to).Sub(floorDay(from)) / (24 * time.Hour))
}

// --- periods -----------------------------------------------------------------

// periodVocabulary is CLOSED. A period outside it is refused rather than
// resolved to the nearest thing: a caller that asked for 2y and silently
// received 1y reads a number that answers a different question.
var periodVocabulary = []string{"1m", "3m", "6m", "1y", "3y", "5y", "10y", "max"}

// defaultPeriod is what an absent ?period= means. One year is the window the
// performance table is read at, and it is echoed in the response so a caller
// never has to infer which window it was served.
const defaultPeriod = "1y"

// periodStart resolves a period name to the window's lower bound, counting back
// from `to` in CALENDAR months and years rather than in fixed day counts: "1y"
// from 2026-09-09 means 2025-09-09, not 2026-09-09 minus 365 days, and the two
// differ across a leap year.
//
// "max" returns nil, meaning "as far back as the data goes" -- which is a
// property of each series, not of the request, so it is resolved per item and
// reported as coverage_from.
func periodStart(period string, to time.Time) (*time.Time, bool) {
	var from time.Time
	switch period {
	case "1m":
		from = to.AddDate(0, -1, 0)
	case "3m":
		from = to.AddDate(0, -3, 0)
	case "6m":
		from = to.AddDate(0, -6, 0)
	case "1y":
		from = to.AddDate(-1, 0, 0)
	case "3y":
		from = to.AddDate(-3, 0, 0)
	case "5y":
		from = to.AddDate(-5, 0, 0)
	case "10y":
		from = to.AddDate(-10, 0, 0)
	case "max":
		return nil, true
	default:
		return nil, false
	}
	u := from.UTC()
	return &u, true
}

// window is the window a request is actually served over. Both bounds are
// echoed in every response: `From` nil means "from the start of each series'
// own history", and `To` is capped at as_of because no data can exist after it.
type window struct {
	From *time.Time
	To   time.Time
	// Period is the vocabulary word that produced this window, or nil when the
	// caller gave an explicit from/to. It is nil rather than "custom" so a
	// client can hand the value straight back as ?period= without receiving a
	// 400 for a word that is not in the vocabulary.
	Period *string
	// FutureToRequested records that the caller's ?to= was later than as_of.
	// The window is served to as_of -- there is nothing else it could be served
	// to -- and this makes the interpretation visible instead of leaving the
	// caller to infer it from a `to` that came back different.
	FutureToRequested bool
}

// parseWindow resolves ?period= and the explicit ?from=/?to= override. Pure
// function (unit tested): `now` arrives from the caller, so there is no hidden
// clock.
//
// An explicit window overrides the period completely, and supplying only one
// of from/to is legitimate -- ?from= alone means "from there to now".
func parseWindow(rawPeriod, rawFrom, rawTo string, now time.Time) (window, *paramError) {
	asOf := now.UTC()
	w := window{To: asOf}

	var from, to *time.Time
	for _, p := range []struct {
		name string
		raw  string
		dst  **time.Time
	}{{"from", rawFrom, &from}, {"to", rawTo, &to}} {
		if p.raw == "" {
			continue
		}
		t, perr := parseInstant(p.name, p.raw)
		if perr != nil {
			return w, perr
		}
		*p.dst = &t
	}

	if from != nil || to != nil {
		if from != nil && to != nil && from.After(*to) {
			return w, badParam("from must not be later than to", map[string]any{
				"from": from.Format(time.RFC3339), "to": to.Format(time.RFC3339),
			})
		}
		if to != nil {
			if to.After(asOf) {
				// Not a refusal: a future `to` is a perfectly ordinary way to
				// say "up to the present". It is capped and SAID SO.
				w.FutureToRequested = true
			} else {
				w.To = to.UTC()
			}
		}
		if from != nil {
			if from.After(w.To) {
				return w, badParam("from must not be later than as_of", map[string]any{
					"from": from.Format(time.RFC3339), "as_of": w.To.Format(time.RFC3339),
				})
			}
			u := from.UTC()
			w.From = &u
		}
		return w, nil
	}

	period := rawPeriod
	if period == "" {
		period = defaultPeriod
	}
	start, ok := periodStart(period, w.To)
	if !ok {
		return w, badParam(
			fmt.Sprintf("period must be one of %s, got %q",
				strings.Join(periodVocabulary, ", "), rawPeriod),
			map[string]any{"period": rawPeriod, "supported": periodVocabulary})
	}
	w.From = start
	name := period
	w.Period = &name
	return w, nil
}

// --- numeraires --------------------------------------------------------------

// The two market series that back a numeraire. Both are ordinary instruments
// with ordinary provenance, and that provenance travels: USD_IRT is collected
// from the 24/7 USDT/toman market as a documented free-market proxy, so a
// USD-denominated return computed here inherits that proxy status and the
// response says so.
const (
	usdSeriesCode  = "USD_IRT"
	goldSeriesCode = "IR_GOLD_18K"
)

// numeraireSpec is one unit of account this deployment can express values in.
type numeraireSpec struct {
	Key string
	// Series is the market instrument whose daily close performs the
	// conversion, or "" for IRT -- the hub, which converts an IRT-quoted asset
	// by doing nothing to it.
	Series string
	// Unit is what one converted value MEANS. "grams of 18k gold" and not
	// "gold": a value divided by IR_GOLD_18K is a count of 18-karat grams, and
	// labelling it "gold" invites it to be read as troy ounces of fine metal.
	Unit   string
	NameEN string
	NameFA string
	Note   string
}

// numeraireSpecs is the closed vocabulary of ?numeraire=. Anything else is
// refused; a numeraire whose backing series carries no observation in this
// deployment is refused too, by the handler, with the reason.
var numeraireSpecs = []numeraireSpec{
	{
		Key: "IRT", Series: "", Unit: "toman", NameEN: "Iranian toman", NameFA: "تومان",
		Note: "Toman (IRT) is the canonical stored unit for every Iranian value in this " +
			"system; rial (IRR) is a display-only x10 and never appears in these figures. " +
			"An IRT-quoted asset needs no conversion at all. A USD-quoted asset is " +
			"multiplied by USD_IRT, so its IRT figures inherit that series' proxy status.",
	},
	{
		Key: "USD", Series: usdSeriesCode, Unit: "USD", NameEN: "US dollar", NameFA: "دلار",
		Note: "An IRT-quoted asset is divided by USD_IRT; a USD-quoted asset is already " +
			"in USD and is passed through untouched rather than multiplied and divided " +
			"back. USD_IRT is the free-market (USDT/toman) proxy, not an official rate.",
	},
	{
		Key: "GOLD", Series: goldSeriesCode, Unit: "grams of 18k gold",
		NameEN: "Grams of 18k gold", NameFA: "گرم طلای ۱۸ عیار",
		Note: "The asset's value in toman divided by the toman price of one gram of 18k " +
			"gold. The result is a count of 18-karat grams -- not troy ounces and not " +
			"fine gold. IR_GOLD_18K measured in itself is 1.000 on every day by " +
			"construction, which is a property of the unit, not a finding.",
	},
}

func lookupNumeraire(key string) (numeraireSpec, bool) {
	for _, n := range numeraireSpecs {
		if n.Key == key {
			return n, true
		}
	}
	return numeraireSpec{}, false
}

func numeraireKeys() []string {
	out := make([]string, 0, len(numeraireSpecs))
	for _, n := range numeraireSpecs {
		out = append(out, n.Key)
	}
	return out
}

// defaultNumeraire is what an absent ?numeraire= means: the stored unit, so the
// default view of an Iranian asset is the number an Iranian reader already
// holds in their head.
const defaultNumeraire = "IRT"

// parseNumeraire resolves ?numeraire= against the closed vocabulary. Pure
// function (unit tested). Whether the deployment can actually BACK the chosen
// numeraire is a separate question, answered against the price history by the
// handler, because it depends on data rather than on the request.
func parseNumeraire(raw string) (numeraireSpec, *paramError) {
	key := raw
	if key == "" {
		key = defaultNumeraire
	}
	spec, ok := lookupNumeraire(key)
	if !ok {
		return numeraireSpec{}, badParam(
			fmt.Sprintf("numeraire must be one of %s, got %q",
				strings.Join(numeraireKeys(), ", "), raw),
			map[string]any{"numeraire": raw, "supported": numeraireKeys()})
	}
	return spec, nil
}

// --- conversion chains --------------------------------------------------------

// convOp is what a conversion step does to the asset's value.
type convOp uint8

const (
	opDivide convOp = iota
	opMultiply
)

// convStep is one leg of a conversion: a market series, and what to do with it.
type convStep struct {
	Series string
	Op     convOp
}

// Quote currencies this package understands as PRICES. Everything else is
// either a rate or an index level, and neither can be re-expressed in a unit of
// account -- see conversionSteps.
const (
	quoteIRT   = "IRT"
	quoteUSD   = "USD"
	quotePCT   = "PCT"
	quoteINDEX = "INDEX"
	quoteRATIO = "RATIO"
)

// conversionSteps returns the chain that carries an asset quoted in
// `quoteCurrency` into `numeraireKey`. Pure function (unit tested). The second
// return is false when no honest chain exists.
//
// IRT IS THE HUB, and that is the whole design. IR_GOLD_18K is quoted in toman
// per gram, so anything divided by it must be in toman first; a USD price
// divided by a toman price is a number with no unit at all. Routing every chain
// through IRT is what makes "never mix IRT and USD" a structural property here
// rather than a rule someone has to remember.
//
// Two chains are deliberately EMPTY rather than round trips:
//   - an IRT asset in IRT terms
//   - a USD asset in USD terms
//
// Multiplying by USD_IRT and dividing straight back would be exact in real
// arithmetic and merely near-exact in floating point, but the real damage is
// elsewhere: it would make a USD-terms figure for XAUUSD depend on USD_IRT
// having quoted that day, and DROP days where it did not. XAUUSD in dollars
// does not need the toman rate to exist.
func conversionSteps(quoteCurrency, numeraireKey string) ([]convStep, bool) {
	switch quoteCurrency {
	case quoteIRT:
		switch numeraireKey {
		case "IRT":
			return nil, true
		case "USD":
			return []convStep{{usdSeriesCode, opDivide}}, true
		case "GOLD":
			return []convStep{{goldSeriesCode, opDivide}}, true
		}
	case quoteUSD:
		switch numeraireKey {
		case "IRT":
			return []convStep{{usdSeriesCode, opMultiply}}, true
		case "USD":
			return nil, true
		case "GOLD":
			// To toman first, then into grams. The order matters and the
			// intermediate unit is toman, which is the only unit in which
			// IR_GOLD_18K is a meaningful divisor.
			return []convStep{{usdSeriesCode, opMultiply}, {goldSeriesCode, opDivide}}, true
		}
	}
	// PCT, INDEX, RATIO and anything a future migration adds: not a price in a
	// currency, so there is no unit of account to carry it into.
	return nil, false
}

// isRateQuote reports whether an instrument is a RATE rather than a price.
// US10Y is a yield in percent and IR_GOLD_FUND_FLOW is a retail/institutional
// flow ratio in percent; migration 0024 gives both quote_currency='PCT' for
// exactly this reason. A "return on a percentage" -- 4.1% to 4.3% read as
// +4.9% -- is not a wrong number, it is a meaningless one, so these are refused
// outright rather than served with a caveat.
func isRateQuote(quoteCurrency string) bool { return quoteCurrency == quotePCT }

// --- float hygiene ------------------------------------------------------------

// fp converts a possibly non-finite float to a JSON-friendly *float64, rounding
// to six decimals. nil for NaN and +/-Inf, which matters more here than it
// looks: encoding/json REFUSES to encode a NaN, and httpserver.JSON discards
// the encoder's error, so a single NaN reaching the payload truncates the
// response body mid-object. Every derived number in this package goes through
// this function.
//
// Same helper, same rounding, as internal/prices/calc.go's fp.
func fp(v float64) *float64 {
	if math.IsNaN(v) || math.IsInf(v, 0) {
		return nil
	}
	r := math.Round(v*1e6) / 1e6
	return &r
}

// dayString renders a UTC day as YYYY-MM-DD.
func dayString(t time.Time) string { return t.UTC().Format(dateLayout) }

// dayStringPtr renders an optional UTC day. A day that does not exist stays
// null; it is never filled in with a plausible date.
func dayStringPtr(t *time.Time) *string {
	if t == nil {
		return nil
	}
	s := dayString(*t)
	return &s
}
