package signalsvc

// Generates the two frontend fixtures by MARSHALLING THE REAL STRUCTS.
//
// The last round of this project shipped a blocker because a hand-written
// frontend fixture invented the server shape: 51 frontend tests passed against
// a page that could not load. So neither file here is typed by hand. Both are
// produced by running realistic stored rows through the same pure projection
// the handlers use and encoding the result the way httpserver.JSON does, and
// the test then re-reads what it wrote and checks the key set. A field renamed
// in Go rewrites the fixture on the next `go test ./...`, and the frontend test
// that depended on the old name fails where the change was made.
//
// WHAT THE ROWS THEMSELVES MAY CONTAIN, AND WHAT THEY MAY NOT.
//
// The previous version of this file got the shape right and the CONTENT wrong,
// which is the same failure one level down: it fabricated six per-market cost
// hurdles (2.4%, 0.35%, 0.5%, 1.0%) that prediction-python/app/core/costs.py
// explicitly refuses to produce, invented factor keys and omission reasons the
// engine never writes, published readings for two symbols the engine withholds
// on every pass, and gave technical-only rows model-shaped confidences. A
// frontend built against that learns behaviour the server does not have.
//
// The rule this file now follows: every value below is either
//
//   - determined by a constant or a hard gate in prediction-python, in which
//     case it is THAT value and a comment names the module it came from; or
//   - the output of the scorer's arithmetic (the 0-100 score and the resulting
//     signal word), in which case it is illustrative and labelled as such.
//
// Go deliberately does not reproduce the arithmetic -- re-deriving the scorer
// here would create the second, silently diverging answer overview.go exists to
// prevent. Everything that is NOT arithmetic is reproduced exactly.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"
)

// overviewContractKeys is the per-item wire contract, in order.
var overviewContractKeys = []string{
	"symbol", "name_en", "name_fa", "quote_currency", "unit", "quality_tier",
	"is_proxy", "signal", "score", "confidence", "evidence_basis", "data_fresh",
	"generated_at", "reading_age_hours", "reading_stale", "headline",
	"top_supporting", "top_conflicting", "omitted_factors", "cost_pct",
	"cost_basis", "stale_reason",
}

// overviewEnvelopeKeys is the response contract around those items.
var overviewEnvelopeKeys = []string{
	"as_of", "oldest_reading_at", "stale_after_hours", "items", "unavailable",
}

// --- the clock the fixture is generated on -----------------------------------

// fixturePassAt is the signals pass most of these rows were written by, and
// fixtureNow is five minutes later. Both are fixed so `reading_age_hours` is
// deterministic: a fixture whose numbers moved with wall-clock time could not
// be asserted against.
var (
	fixturePassAt = time.Date(2026, 9, 9, 6, 0, 0, 0, time.UTC)
	fixtureNow    = time.Date(2026, 9, 9, 6, 5, 0, 0, time.UTC)

	// IR_COIN_EMAMI's last successful pass. app/jobs/signals.py scores each
	// symbol independently and records a per-symbol failure without sinking the
	// run, so one symbol can fall several passes behind while the rest publish
	// on schedule. That is the case this row exists to give the page: a reading
	// that is real, nine hours old, and must not be stamped with `as_of`.
	fixtureLaggingPassAt = time.Date(2026, 9, 8, 21, 0, 0, 0, time.UTC)
)

// --- values prediction-python determines, not this fixture --------------------

// fallbackRoundTripCostPct is FALLBACK_ROUND_TRIP_COST_PCT from
// prediction-python/app/core/costs.py: 2*DEFAULT_FEE_PCT(0.5) +
// DEFAULT_SPREAD_PCT(1.0) + 2*DEFAULT_SLIPPAGE_PCT(0.1).
//
// EVERY ASSET WITHOUT A DEALER QUOTE GETS THIS SAME NUMBER. costs.py devotes
// its module docstring to why, and the previous fixture is the exact mistake it
// names: "a coin spread, a USDT/toman exchange fee or a TSE brokerage
// commission written down from memory is a number nobody measured, and once it
// is in the payload it is indistinguishable from the 0.49% that WAS measured."
// SPREAD_SOURCES has one entry, IR_GOLD_18K -> hamrahgold, so exactly one asset
// can ever carry an observed hurdle and the other six cannot.
const fallbackRoundTripCostPct = 2.2

// goldObservedCostPct is a hamrahgold two-sided spread, the one hurdle in this
// file that is a measurement. costs.py accepts a quote in [MIN_SANE_PCT 0.1,
// MAX_SANE_PCT 10.0] no older than MAX_AGE_HOURS 72.
const goldObservedCostPct = 0.49

// technicalOnlyConfidence: compute_signal has no per-horizon confidences to
// average for a symbol with no trained model, so it sets mean_conf = 0.3
// explicitly (engine.py, the `else` arm of the model-confidence block) and the
// published confidence is clip(mean_conf * 1.0, 0.05, 0.95). It is a constant,
// not an estimate: no technical-only row can carry any other value while its
// inputs are fresh.
const technicalOnlyConfidence = 0.3

// technicalOnlyStaleConfidence is the same value through the freshness
// penalty: confidence = clip(mean_conf * STALE_CONFIDENCE_MULTIPLIER, 0.05,
// 0.95), rounded to three.
const technicalOnlyStaleConfidence = 0.09

// technicalOnlyConfidenceReason is the sentence engine.py stores beside that
// constant, verbatim. It exists for the same reason round_trip_cost_reason
// does: 0.300 printed under a bare "Confidence" label beside gold's measured
// 0.62 invites a comparison that is not available, and the row has to say which
// of the two numbers was measured.
//
// The overview contract does not project it yet -- it carries `confidence` with
// no basis, which is the cost-hurdle problem one field over -- but the stored
// blob is what production writes, and /signals/current serialises `inputs`
// whole, so the fixture carries it.
const technicalOnlyConfidenceReason = "no model ran for this asset, so no confidence was " +
	"measured; 0.3 is a fixed structural assumption standing in for one and must not be " +
	"read as a measurement or compared with a model-derived figure"

// The engine's fixed tail sentences, verbatim from compute_signal.
const (
	engineDisclaimer = "This is an uncertain, model-based assessment of current conditions — " +
		"not financial advice, and actual outcomes can differ."
	engineStaleHeadline = "Input data is stale, so the engine holds regardless of model output."
	engineStaleTail     = "Conditions will be reassessed when fresh data arrives. " +
		"This is an uncertain, model-based assessment — not financial advice."
)

// engineDirectionWord is compute_signal's own signal -> word map. It is the
// reason `headline` can be shown beside a technical-only badge without
// contradicting it: the headline makes no claim about model evidence.
var engineDirectionWord = map[string]string{
	"strong_buy": "accumulating", "buy": "buying", "hold": "waiting",
	"sell": "reducing exposure", "strong_sell": "exiting positions",
}

// engineHeadline reproduces the headline format string in compute_signal.
func engineHeadline(signal string, score int) string {
	return fmt.Sprintf("Conditions currently favor %s (score %d/100).",
		engineDirectionWord[signal], score)
}

// engineExplanation reproduces how compute_signal assembles `explanation`: the
// headline, then the weighted forecast when one exists, then the factor census,
// then the fixed disclaimer.
//
// It is built FROM the headline in the engine so the two can never disagree,
// and it is built from it here for the same reason. The closing clause is
// exactly why the overview reads `inputs.headline` instead of this string: on a
// technical-only row "model-based assessment" contradicts the evidence_basis
// badge rendered beside it.
func engineExplanation(signal string, score int, weightedPct *float64, supporting, conflicting int) string {
	out := engineHeadline(signal, score) + " "
	if weightedPct != nil {
		out += fmt.Sprintf("Weighted forecast move: %+.2f%%. ", *weightedPct)
	}
	out += fmt.Sprintf("%d supporting vs %d conflicting factors. ", supporting, conflicting)
	return out + engineDisclaimer
}

// --- cost provenance, as costs.py writes it -----------------------------------

// noObservedCostDetail is NO_OBSERVED_COST_REASON from
// prediction-python/app/core/costs.py, verbatim. It is per asset because the
// NUMBER cannot be: the same 2.2% assumption carries a different explanation of
// what measurement is missing for each market.
var noObservedCostDetail = map[string]string{
	"USD_IRT": "collected as a single mid price from the 24/7 USDT/toman market; no " +
		"order book and no dealer buy/sell pair is stored, so the real round " +
		"trip (exchange fee both sides plus the book's spread) has never been " +
		"measured here",
	"IR_COIN_EMAMI": "no source here publishes a two-sided Emami-coin quote; hamrahgold " +
		"quotes 18k gold only, and the coin is a different market -- it " +
		"carries a minting premium and its own, wider, bazaar margin",
	"XAUUSD": "collected from the Yahoo GC=F front-month settlement, a single " +
		"settlement price with no bid/ask; broker commission and futures roll " +
		"cost are not observed by this system",
	"XAGUSD": "collected from the Yahoo SI=F front-month settlement, a single " +
		"settlement price with no bid/ask; broker commission and futures roll " +
		"cost are not observed by this system",
}

// assumedCostReason reproduces the sentence resolve_cost writes into
// `round_trip_cost_reason` when SPREAD_SOURCES has no provider for the symbol.
func assumedCostReason(symbol string) string {
	detail := noObservedCostDetail[symbol]
	if detail == "" {
		return fmt.Sprintf("no dealer publishes a two-sided quote for %s into this "+
			"system; using the conservative assumption", symbol)
	}
	return fmt.Sprintf("no dealer publishes a two-sided quote for %s into this "+
		"system: %s; using the conservative assumption", symbol, detail)
}

// --- omitted factors, as universe.py writes them ------------------------------

// The factor KEYS are universe.py's FactorSpec.key values. They are spelled out
// here because the previous fixture invented "forecast_edge_vs_cost", which no
// engine writes -- a frontend that special-cased that string would never match
// a real row.
const (
	factorForecast    = "forecast_vs_cost"  // FACTOR_FORECAST
	factorPremium     = "local_premium_z"   // FACTOR_PREMIUM
	factorFundFlow    = "gold_fund_flow"    // FACTOR_FUND_FLOW
	factorMAAlignment = "ma_alignment"      // FACTOR_MA_ALIGNMENT
	factorTrendSMA    = "trend_sma"         // FACTOR_TREND_SMA
	factorVolatility  = "volatility_regime" // FACTOR_VOLATILITY
)

// modelledSymbols and alignedSymbols are the two lists universe.py reads from
// their owning modules (app.models.training.FORECAST_SYMBOLS and
// app.jobs.trend_alignment.SUPPORTED_SYMBOLS). Both reasons print them sorted.
const (
	modelledSymbols = "IR_GOLD_18K, XAUUSD"
	alignedSymbols  = "IR_GOLD_18K, XAUUSD"
)

// profileOmissions is universe.profile_for's `inapplicable` list for one
// symbol, in its order: forecast, premium, fund flow, MA alignment. Each
// sentence is that module's template with the symbol substituted.
func profileOmissions(symbol string, modelled, parity, fund, aligned bool) []OmittedFactor {
	out := []OmittedFactor{}
	if !modelled {
		out = append(out, OmittedFactor{
			Factor: factorForecast,
			Reason: fmt.Sprintf("No forecasting model is trained for %s: "+
				"app.models.training.FORECAST_SYMBOLS covers %s. This reading is "+
				"technical-only and carries no model evidence.", symbol, modelledSymbols),
		})
	}
	if !parity {
		out = append(out, OmittedFactor{
			Factor: factorPremium,
			Reason: "The local premium is measured against the 18k parity price " +
				"(XAUUSD / troy ounce x USD_IRT x 0.750). No such theoretical " +
				"price is defined for " + symbol + ", so it has no premium to be rich " +
				"or cheap against.",
		})
	}
	if !fund {
		out = append(out, OmittedFactor{
			Factor: factorFundFlow,
			Reason: "IR_GOLD_FUND_FLOW is the retail/institutional flow ratio of the " +
				"Tehran gold ETFs. It describes who is trading those funds and " +
				"says nothing about " + symbol + ".",
		})
	}
	if !aligned {
		out = append(out, OmittedFactor{
			Factor: factorMAAlignment,
			Reason: "The 1D/4H/1H stack needs a 220-period slow MA on all three " +
				"timeframes; app.jobs.trend_alignment.SUPPORTED_SYMBOLS covers " +
				alignedSymbols + " because only those have the continuous history for it.",
		})
	}
	return out
}

// --- the registry half --------------------------------------------------------

// fixtureRegistry is the `instruments` seed of migration 0024, verbatim. Copied
// rather than invented: name_fa, quality_tier and is_proxy are what the page
// renders, and a fixture that guessed at them would teach the frontend the
// wrong vocabulary — XAUUSD in particular is a COMEX front-month PROXY, not the
// London spot fix, and the card has to be able to say so.
var fixtureRegistry = map[string]instrumentMeta{
	"IR_GOLD_18K": {
		NameEn: "Iranian 18k gold, per gram", NameFa: "طلای ۱۸ عیار",
		QuoteCurrency: "IRT", Unit: "gram", QualityTier: "official_mirror", IsProxy: false,
	},
	"USD_IRT": {
		NameEn: "US dollar, free market", NameFa: "دلار آزاد",
		QuoteCurrency: "IRT", Unit: "usd", QualityTier: "proxy", IsProxy: true,
	},
	"IR_COIN_EMAMI": {
		NameEn: "Emami gold coin", NameFa: "سکه امامی",
		QuoteCurrency: "IRT", Unit: "coin", QualityTier: "official_mirror", IsProxy: false,
	},
	"XAUUSD": {
		NameEn: "Gold, COMEX front month (spot proxy)", NameFa: "انس طلا",
		QuoteCurrency: "USD", Unit: "ozt", QualityTier: "proxy", IsProxy: true,
	},
	"XAGUSD": {
		NameEn: "Silver, COMEX front month (spot proxy)", NameFa: "انس نقره",
		QuoteCurrency: "USD", Unit: "ozt", QualityTier: "proxy", IsProxy: true,
	},
	"IR_GOLD_FUND_AYAR": {
		NameEn: "Ayar gold ETF", NameFa: "صندوق عیار",
		QuoteCurrency: "IRT", Unit: "unit", QualityTier: "official_mirror", IsProxy: false,
	},
	"IR_GOLD_FUND_TALA": {
		NameEn: "Tala gold ETF", NameFa: "صندوق طلا",
		QuoteCurrency: "IRT", Unit: "unit", QualityTier: "official_mirror", IsProxy: false,
	},
}

// --- the stored rows ----------------------------------------------------------

// fixtureSignalRows is one realistic latest row per PUBLISHING symbol.
//
// THE TWO GOLD FUNDS ARE ABSENT, AND THAT IS THE PRODUCTION STATE, not a gap in
// the fixture. universe.py's own header says so: at ~50 daily bars neither fund
// clears FACTOR_TREND_SMA's min_bars of 100 (window 50 + first_bar 50), the SMA
// trend is the only directional factor either could carry, and
// withholding_reason refuses to publish without a directional factor. So they
// write no `signals` row at all on any pass, and the board's honest rendering of
// them is an `unavailable` entry -- which is exactly what buildOverviewResponse
// produces for a symbol with no current reading. The previous fixture published
// full fund readings, complete with fabricated hurdles; a frontend built against
// that would have a fund card that production can never fill.
func fixtureSignalRows() map[string]overviewSignalRow {
	// Gold is derived from the /signals/current sample, so the two fixtures
	// cannot disagree about the same reading.
	cur := sampleCurrentRow()
	gold := overviewSignalRow{
		Symbol: "IR_GOLD_18K", Signal: cur.Signal, Score: cur.Score,
		Confidence: cur.Confidence, Explanation: cur.Explanation,
		Supporting: cur.Supporting, Conflicting: cur.Conflicting,
		DataFresh: cur.DataFresh, GeneratedAt: cur.GeneratedAt, Inputs: cur.Inputs,
	}

	return map[string]overviewSignalRow{
		"IR_GOLD_18K": gold,

		// Technical-only and fresh. Four omissions, not three: USD_IRT carries
		// no model, no parity premium, no fund flow AND no 1D/4H/1H stack --
		// trend_alignment covers gold and XAUUSD only.
		"USD_IRT": technicalRow(technicalRowSpec{
			Symbol: "USD_IRT", Signal: "buy", Score: 62,
			GeneratedAt: fixturePassAt, DataFresh: true,
			// engine.py appends the no-model line to `conflicting` before any
			// technical factor is evaluated, so it is always conflicting[0].
			Supporting: []string{
				"Price is above SMA20 and SMA50 (established uptrend).",
				"Positive 10-period momentum (+2.4%).",
			},
			Conflicting: []string{"No model forecast available for any horizon."},
			LastPrice:   1043500, SMA20: 1035200, SMA50: 1021900,
			RSI: 61.8, Momentum: 2.41, Regime: "trending",
		}),

		// The lagging reading: everything about it is fine, it is simply nine
		// hours old. It is what `reading_stale` and `oldest_reading_at` exist
		// for -- stamped with the envelope's `as_of` it would read as current.
		"IR_COIN_EMAMI": technicalRow(technicalRowSpec{
			Symbol: "IR_COIN_EMAMI", Signal: "hold", Score: 49,
			GeneratedAt: fixtureLaggingPassAt, DataFresh: true,
			Supporting: []string{"Price is above SMA20."},
			Conflicting: []string{
				"No model forecast available for any horizon.",
				"Negative 10-period momentum (-1.4%).",
			},
			LastPrice: 1187000000, SMA20: 1180400000, SMA50: 1192600000,
			RSI: 48.2, Momentum: -1.42, Regime: "trending",
		}),

		// Model-backed, and the honest consequence of an ASSUMED hurdle: a
		// +0.43% weighted forecast cannot clear a 2.2% round trip, so the model
		// edge lands in `conflicting`. costs.py states this asymmetry as
		// deliberate -- an asset whose cost was never measured needs a far
		// larger edge than gold does at its observed 0.49%.
		"XAUUSD": {
			Symbol: "XAUUSD", Signal: "hold", Score: 57, Confidence: 0.577,
			Explanation: engineExplanation("hold", 57, fp64(0.4258), 3, 1),
			Supporting: jsonArray(
				"Forecast horizons largely agree on direction.",
				"Price is above SMA20 and SMA50 (established uptrend).",
				"Positive 10-period momentum (+1.1%).",
			),
			Conflicting: jsonArray(
				"Forecast move (+0.43%) is positive but below the ~2.2% round-trip cost threshold.",
			),
			DataFresh: true, GeneratedAt: fixturePassAt,
			Inputs: inputsBlob(map[string]any{
				"symbol":         "XAUUSD",
				"evidence_basis": evidenceModelBacked,
				// Measured: the mean of the three per-horizon confidences below.
				"confidence_basis": "model_mean",
				"confidence_reason": "mean of the 3 per-horizon confidence(s) the models " +
					"reported for this asset",
				"model_confidence":    0.5767,
				"headline":            engineHeadline("hold", 57),
				"expected_change_pct": map[string]any{"1d": 0.44, "3d": 0.51, "7d": 0.29},
				"confidence":          map[string]any{"1d": 0.61, "3d": 0.57, "7d": 0.55},
				"last_price":          4218.40,
				"sma20":               4180.10,
				"sma50":               4102.75,
				"rsi14":               55.1,
				"momentum_10_pct":     1.14,
				"regime":              "trending",
				"market_closed":       false,
				"omitted_factors": profileOmissions("XAUUSD",
					true /*modelled*/, false /*parity*/, false /*fund*/, true /*aligned*/),
				"round_trip_cost_pct":    fallbackRoundTripCostPct,
				"round_trip_cost_basis":  "assumed",
				"round_trip_cost_source": nil,
				"round_trip_cost_reason": assumedCostReason("XAUUSD"),
				"stale_reason":           nil,
			}),
		},

		// Stale INPUTS, which is a different thing from a stale reading: this
		// row was written on the current pass and is only minutes old, but the
		// prices behind it were not current. engine.py's freshness gate is
		// absolute -- data_fresh false forces signal "hold" at score 50, swaps
		// the headline for the stale sentence and multiplies confidence by 0.3
		// -- so no other combination is producible.
		"XAGUSD": {
			Symbol: "XAGUSD", Signal: "hold", Score: 50,
			Confidence:  technicalOnlyStaleConfidence,
			Explanation: engineStaleHeadline + " " + engineStaleTail,
			// No supporting factor at all, which is a real shape the page has
			// to render: nothing in a downtrend with no model argues for it.
			Supporting: jsonArray(),
			Conflicting: jsonArray(
				"No model forecast available for any horizon.",
				"Price is below SMA20 and SMA50 (established downtrend).",
				"Negative 10-period momentum (-1.9%).",
			),
			DataFresh: false, GeneratedAt: fixturePassAt,
			Inputs: inputsBlob(map[string]any{
				"symbol":              "XAGUSD",
				"evidence_basis":      evidenceTechnicalOnly,
				"confidence_basis":    "assumed",
				"confidence_reason":   technicalOnlyConfidenceReason,
				"model_confidence":    nil,
				"headline":            engineStaleHeadline,
				"expected_change_pct": map[string]any{},
				"confidence":          map[string]any{},
				"last_price":          52.18,
				"sma20":               53.90,
				"sma50":               55.12,
				"rsi14":               41.6,
				"momentum_10_pct":     -1.93,
				"regime":              "trending",
				"market_closed":       false,
				"omitted_factors": profileOmissions("XAGUSD",
					false /*modelled*/, false /*parity*/, false /*fund*/, false /*aligned*/),
				"round_trip_cost_pct":    fallbackRoundTripCostPct,
				"round_trip_cost_basis":  "assumed",
				"round_trip_cost_source": nil,
				"round_trip_cost_reason": assumedCostReason("XAGUSD"),
				// _freshness returns the reason with the flag; "stale" on its
				// own is not something a reader can check.
				"stale_reason": "the most recent XAGUSD observation is 31.4h old, past this " +
					"symbol's freshness budget",
			}),
		},
	}
}

// technicalRowSpec is one technical-only reading. Confidence is not a field:
// engine.py determines it (0.3 fresh, 0.09 stale) and this fixture may not
// choose it.
type technicalRowSpec struct {
	Symbol      string
	Signal      string
	Score       int
	GeneratedAt time.Time
	DataFresh   bool
	Supporting  []string
	Conflicting []string
	LastPrice   float64
	SMA20       float64
	SMA50       float64
	RSI         float64
	Momentum    float64
	Regime      string
}

// technicalRow builds a stored row for a symbol with no trained model. Every
// technical-only symbol here omits the same four factors -- no model, no parity
// premium, no fund flow, no multi-timeframe stack -- because the profile is a
// property of the symbol, not of this fixture.
func technicalRow(spec technicalRowSpec) overviewSignalRow {
	conf := technicalOnlyConfidence
	headline := engineHeadline(spec.Signal, spec.Score)
	explanation := engineExplanation(spec.Signal, spec.Score, nil,
		len(spec.Supporting), len(spec.Conflicting))
	if !spec.DataFresh {
		conf = technicalOnlyStaleConfidence
		headline = engineStaleHeadline
		explanation = engineStaleHeadline + " " + engineStaleTail
	}
	return overviewSignalRow{
		Symbol:      spec.Symbol,
		Signal:      spec.Signal,
		Score:       spec.Score,
		Confidence:  conf,
		Explanation: explanation,
		Supporting:  jsonArray(spec.Supporting...),
		Conflicting: jsonArray(spec.Conflicting...),
		DataFresh:   spec.DataFresh,
		GeneratedAt: spec.GeneratedAt,
		Inputs: inputsBlob(map[string]any{
			"symbol":         spec.Symbol,
			"evidence_basis": evidenceTechnicalOnly,
			// The confidence's own provenance, mirroring the cost hurdle's.
			"confidence_basis":    "assumed",
			"confidence_reason":   technicalOnlyConfidenceReason,
			"model_confidence":    nil,
			"headline":            headline,
			"expected_change_pct": map[string]any{},
			"confidence":          map[string]any{},
			"last_price":          spec.LastPrice,
			"sma20":               spec.SMA20,
			"sma50":               spec.SMA50,
			"rsi14":               spec.RSI,
			"momentum_10_pct":     spec.Momentum,
			"regime":              spec.Regime,
			"market_closed":       false,
			"omitted_factors": profileOmissions(spec.Symbol,
				false /*modelled*/, false /*parity*/, false /*fund*/, false /*aligned*/),
			"round_trip_cost_pct":    fallbackRoundTripCostPct,
			"round_trip_cost_basis":  "assumed",
			"round_trip_cost_source": nil,
			"round_trip_cost_reason": assumedCostReason(spec.Symbol),
			"stale_reason":           nil,
		}),
	}
}

func fp64(v float64) *float64 { return &v }

// jsonArray encodes a supporting/conflicting list the way the column stores it.
// An empty list is `[]`, never null: the engine always writes both arrays.
func jsonArray(entries ...string) json.RawMessage {
	if entries == nil {
		entries = []string{}
	}
	b, err := json.Marshal(entries)
	if err != nil {
		panic(err)
	}
	return b
}

func inputsBlob(blob map[string]any) json.RawMessage {
	b, err := json.Marshal(blob)
	if err != nil {
		panic(err)
	}
	return b
}

// encodeLikeTheWire encodes v the way httpserver.JSON does (encoding/json with
// HTML escaping on), indented so the fixture is readable in review. Indentation
// is whitespace only: the key set, the ordering and every value are exactly
// what the endpoint writes.
func encodeLikeTheWire(t *testing.T, v any) []byte {
	t.Helper()
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetIndent("", "  ")
	if err := enc.Encode(v); err != nil {
		t.Fatalf("encode: %v", err)
	}
	return buf.Bytes()
}

func fixturePath(t *testing.T, name string) string {
	t.Helper()
	return filepath.Join("..", "..", "..", "frontend", "src", "test", "fixtures", name)
}

// checkFixtureShape compares the committed fixture's KEY SET against what these
// structs marshal. It deliberately does NOT write the file.
//
// This helper used to overwrite the fixture on every `go test ./...`, and that
// was the mechanism behind three consecutive rounds of the same defect: each
// round a reviewer found the fixture describing a payload the server cannot
// emit, each round it was corrected, and each round the next test run silently
// replaced the correction with values a human had chosen for sampleOverview().
// It even reverted a commit whose entire purpose was to replace these files
// with captures from production.
//
// The fixtures are now CAPTURED (see frontend/src/test/fixtures/README.md), so
// they carry real values the backend genuinely produced. What is still worth
// checking automatically is the SHAPE -- that Go has not added, removed or
// renamed a field the frontend reads -- and a comparison does that without
// destroying the values.
func checkFixtureShape(t *testing.T, name string, body []byte) {
	t.Helper()
	path := fixturePath(t, name)
	committed, err := os.ReadFile(path)
	if err != nil {
		t.Skipf("committed fixture %s is unavailable (%v); the backend tests "+
			"still pass, but the wire shape was not cross-checked", name, err)
	}

	var fromStructs, fromFixture any
	if err := json.Unmarshal(body, &fromStructs); err != nil {
		t.Fatalf("marshalled payload is not valid JSON: %v", err)
	}
	if err := json.Unmarshal(committed, &fromFixture); err != nil {
		t.Fatalf("committed fixture %s is not valid JSON: %v", name, err)
	}

	missing := keysOf(fromStructs, "").Difference(keysOf(fromFixture, ""))
	extra := keysOf(fromFixture, "").Difference(keysOf(fromStructs, ""))
	if len(missing) > 0 {
		t.Errorf("%s is missing %d key(s) Go emits: %v\n"+
			"Re-capture it from a running stack rather than editing it by hand; "+
			"see frontend/src/test/fixtures/README.md.", name, len(missing), missing.Sorted())
	}
	// The reverse direction is REPORTED, never failed, and the distinction is
	// not laziness. This compares the fixture against one sample INSTANCE, not
	// against the Go types: an empty slice or nil map in the sample contributes
	// no member keys at all, so a captured fixture with a populated
	// `omitted_factors` or a full per-horizon map legitimately carries keys the
	// sample cannot show. Failing on that would push the next person to prune
	// real captured data to satisfy a synthetic sample -- which is precisely
	// how these fixtures drifted away from reality three times already.
	//
	// The direction that matters is the one above: a field the frontend reads
	// and the fixture lacks is a test passing on a payload shape that cannot
	// occur. Invented VALUES are a different failure, and the defence against
	// those is capturing rather than authoring (see the fixtures README).
	if len(extra) > 0 {
		t.Logf("%s carries %d key(s) this sample does not populate: %v\n"+
			"Expected when the capture is richer than the sample (populated "+
			"arrays, full horizon maps). Not a failure.",
			name, len(extra), extra.Sorted())
	}
}

// keySet is a tiny set of dotted key paths, enough to diff two payload shapes.
type keySet map[string]struct{}

func (k keySet) Difference(other keySet) keySet {
	out := keySet{}
	for key := range k {
		if _, ok := other[key]; !ok {
			out[key] = struct{}{}
		}
	}
	return out
}

func (k keySet) Sorted() []string {
	out := make([]string, 0, len(k))
	for key := range k {
		out = append(out, key)
	}
	sort.Strings(out)
	return out
}

// keysOf walks a decoded payload and collects every dotted key path. Arrays
// collapse to their first element: the shape of a list is the shape of its
// members, and a captured fixture legitimately has a different NUMBER of them
// than a synthetic sample.
func keysOf(v any, prefix string) keySet {
	out := keySet{}
	switch t := v.(type) {
	case map[string]any:
		for key, val := range t {
			path := prefix + key
			out[path] = struct{}{}
			for k := range keysOf(val, path+".") {
				out[k] = struct{}{}
			}
		}
	case []any:
		if len(t) > 0 {
			for k := range keysOf(t[0], prefix) {
				out[k] = struct{}{}
			}
		}
	}
	return out
}

func TestSignalsCurrentFixtureMatchesTheWireShape(t *testing.T) {
	body := encodeLikeTheWire(t, sampleCurrentRow())
	checkFixtureShape(t, "signals-current.json", body)

	// Read back what was written and check it is the frozen contract: the
	// fixture is only worth having if it is the server's own shape.
	got := jsonKeysInOrder(t, body)
	if len(got) != len(currentContractKeys) {
		t.Fatalf("fixture has %d fields %v, want %d", len(got), got, len(currentContractKeys))
	}
	for i, key := range currentContractKeys {
		if got[i] != key {
			t.Fatalf("fixture field %d = %q, want %q", i, got[i], key)
		}
	}
	var decoded signalRow
	if err := json.Unmarshal(body, &decoded); err != nil {
		t.Fatalf("the fixture does not round-trip into signalRow: %v", err)
	}
	if decoded.Signal != "hold" || decoded.Score != 54 {
		t.Errorf("round-tripped reading changed: %+v", decoded)
	}
	// The stored blob must carry the model-free headline the overview reads.
	// Without it the board falls back to `explanation`, whose closing clause is
	// what defect 3 was about.
	if h := projectSignalInputs(decoded.Inputs).Headline; h == nil || *h == "" {
		t.Fatal("the /current sample records no inputs.headline")
	}
}

func TestSignalsOverviewFixtureMatchesTheWireShape(t *testing.T) {
	resp := buildOverviewResponse(
		fixtureNow, eligibleSignalSymbols, fixtureRegistry, fixtureSignalRows(), signalCoverageGaps)
	body := encodeLikeTheWire(t, resp)
	checkFixtureShape(t, "signals-overview.json", body)

	// Round-trip through the wire types, then assert the properties the page
	// depends on. Everything below is checked against the DECODED fixture, so
	// the file the frontend reads is the thing under test.
	var decoded OverviewResponse
	if err := json.Unmarshal(body, &decoded); err != nil {
		t.Fatalf("the fixture does not round-trip into OverviewResponse: %v", err)
	}
	if keys := jsonKeysInOrder(t, body); len(keys) != len(overviewEnvelopeKeys) {
		t.Fatalf("envelope has %d fields %v, want %d", len(keys), keys, len(overviewEnvelopeKeys))
	}

	published := fixtureSignalRows()
	if len(decoded.Items) != len(published) {
		t.Fatalf("fixture has %d items, want one per PUBLISHING symbol (%d); the two "+
			"gold funds are withheld by universe.withholding_reason on every pass",
			len(decoded.Items), len(published))
	}
	if decoded.AsOf == nil || !decoded.AsOf.Equal(fixturePassAt) {
		t.Fatalf("as_of = %v, want the newest reading", decoded.AsOf)
	}
	// as_of is a MAXIMUM. The spread has to be on the wire, or a page that
	// prints one timestamp over every row misdates the lagging one.
	if decoded.OldestReadingAt == nil || !decoded.OldestReadingAt.Equal(fixtureLaggingPassAt) {
		t.Fatalf("oldest_reading_at = %v, want the lagging reading", decoded.OldestReadingAt)
	}
	if decoded.StaleAfterHours != signalReadingStaleAfter.Hours() {
		t.Errorf("stale_after_hours = %v", decoded.StaleAfterHours)
	}

	// Every item carries exactly the contract keys, in order.
	rawItems := extractItemObjects(t, body)
	for i, item := range rawItems {
		keys := jsonKeysInOrder(t, item)
		if len(keys) != len(overviewContractKeys) {
			t.Fatalf("item %d has %d fields %v, want %d", i, len(keys), keys, len(overviewContractKeys))
		}
		for j, key := range overviewContractKeys {
			if keys[j] != key {
				t.Fatalf("item %d field %d = %q, want %q", i, j, keys[j], key)
			}
		}
	}

	byCode := map[string]OverviewItem{}
	for _, it := range decoded.Items {
		byCode[it.Symbol] = it
	}

	// The two trained symbols are model-backed; the rest say plainly that they
	// are not. This is the distinction the whole contract exists to carry.
	for code, want := range map[string]string{
		"IR_GOLD_18K": evidenceModelBacked, "XAUUSD": evidenceModelBacked,
		"USD_IRT": evidenceTechnicalOnly, "IR_COIN_EMAMI": evidenceTechnicalOnly,
		"XAGUSD": evidenceTechnicalOnly,
	} {
		if byCode[code].EvidenceBasis != want {
			t.Errorf("%s evidence_basis = %q, want %q", code, byCode[code].EvidenceBasis, want)
		}
	}

	// THE HEADLINE MUST NOT CONTRADICT THE BADGE BESIDE IT. The engine's
	// `explanation` always closes with "This is an uncertain, model-based
	// assessment"; on a technical_only row that is a claim the same item denies
	// three fields earlier. The board reads inputs.headline instead.
	for _, it := range decoded.Items {
		if it.Headline == "" {
			t.Errorf("%s has no headline", it.Symbol)
		}
		if it.EvidenceBasis == evidenceTechnicalOnly && strings.Contains(it.Headline, "model-based") {
			t.Errorf("%s is technical_only but its headline calls itself model-based: %q",
				it.Symbol, it.Headline)
		}
	}

	// Exactly one asset has an OBSERVED cost. Every other hurdle is the SAME
	// conservative assumption -- costs.py cannot produce a per-market figure --
	// and carries the sentence naming the measurement that is missing.
	observed := 0
	for _, it := range decoded.Items {
		if it.CostBasis == nil || it.CostPct == nil {
			t.Fatalf("%s carries no cost hurdle", it.Symbol)
		}
		switch it.CostBasis.Basis {
		case "observed_spread":
			observed++
			if it.Symbol != "IR_GOLD_18K" {
				t.Errorf("%s claims an observed spread; SPREAD_SOURCES has one entry", it.Symbol)
			}
			if it.CostBasis.Source == nil || *it.CostBasis.Source != "hamrahgold" {
				t.Errorf("%s observed source = %v", it.Symbol, it.CostBasis.Source)
			}
			if *it.CostPct != goldObservedCostPct {
				t.Errorf("%s observed hurdle = %v", it.Symbol, *it.CostPct)
			}
		case "assumed":
			if *it.CostPct != fallbackRoundTripCostPct {
				t.Errorf("%s assumes %v%%; costs.py returns FALLBACK_ROUND_TRIP_COST_PCT "+
					"(%v) for every asset without a dealer quote, and a per-market figure "+
					"written down from memory is indistinguishable from a measured one",
					it.Symbol, *it.CostPct, fallbackRoundTripCostPct)
			}
			if it.CostBasis.Source != nil {
				t.Errorf("%s assumes its hurdle but names a source: %v", it.Symbol, *it.CostBasis.Source)
			}
			if it.CostBasis.Reason == nil || !strings.Contains(*it.CostBasis.Reason, it.Symbol) {
				t.Errorf("%s assumes its hurdle without naming what is missing for IT: %v",
					it.Symbol, it.CostBasis.Reason)
			}
		default:
			t.Errorf("%s cost basis = %q", it.Symbol, it.CostBasis.Basis)
		}
	}
	if observed != 1 {
		t.Errorf("%d assets claim an observed spread, want exactly 1", observed)
	}

	// The capability profile: gold omits nothing, and every symbol without a
	// model omits four factors, not three -- the 1D/4H/1H stack is available to
	// gold and XAUUSD only.
	if len(byCode["IR_GOLD_18K"].OmittedFactors) != 0 {
		t.Errorf("gold should omit no factor: %+v", byCode["IR_GOLD_18K"].OmittedFactors)
	}
	for _, code := range []string{"USD_IRT", "IR_COIN_EMAMI", "XAGUSD"} {
		if n := len(byCode[code].OmittedFactors); n != 4 {
			t.Errorf("%s omits %d factors, want 4 (model, premium, fund flow, alignment)", code, n)
		}
	}
	if n := len(byCode["XAUUSD"].OmittedFactors); n != 2 {
		t.Errorf("XAUUSD omits %d factors, want 2 (premium and fund flow)", n)
	}
	for _, it := range decoded.Items {
		for _, f := range it.OmittedFactors {
			if f.Reason == "" || f.Reason == omittedFactorNoReason {
				t.Errorf("%s omission has no real reason: %+v", it.Symbol, f)
			}
			// Invented factor keys are how a frontend ends up branching on a
			// string no engine writes.
			switch f.Factor {
			case factorForecast, factorPremium, factorFundFlow, factorMAAlignment,
				factorTrendSMA, factorVolatility:
			default:
				t.Errorf("%s omits %q, which is not a universe.py FactorSpec key",
					it.Symbol, f.Factor)
			}
		}
	}

	// Stale INPUTS and a stale READING are different claims and the page needs
	// one of each to render.
	silver := byCode["XAGUSD"]
	if silver.DataFresh {
		t.Error("the fixture should carry one item whose INPUTS were stale")
	}
	if silver.StaleReason == nil || *silver.StaleReason == "" {
		t.Error("a stale-input item must carry the reason it is stale")
	}
	if silver.Signal != "hold" || silver.Score != 50 {
		t.Errorf("engine.py forces hold at 50 when data_fresh is false, got %s/%d",
			silver.Signal, silver.Score)
	}
	if silver.ReadingStale {
		t.Error("XAGUSD's reading is minutes old; only its inputs were stale")
	}
	coin := byCode["IR_COIN_EMAMI"]
	if !coin.ReadingStale || coin.ReadingAgeHours < signalReadingStaleAfter.Hours() {
		t.Errorf("the lagging reading must be flagged: stale=%v age=%v",
			coin.ReadingStale, coin.ReadingAgeHours)
	}
	if !coin.DataFresh || coin.StaleReason != nil {
		t.Error("the lagging reading's INPUTS were fine when it was computed")
	}
	if byCode["IR_GOLD_18K"].ReadingStale || byCode["IR_GOLD_18K"].StaleReason != nil {
		t.Error("a current, fresh item must claim neither kind of staleness")
	}

	// The coverage block accounts for everything the board does not publish,
	// and each entry says which KIND of absence it is.
	if len(decoded.Unavailable) != 2+len(signalCoverageGaps) {
		t.Fatalf("unavailable has %d entries, want the two withheld funds plus %d gaps: %+v",
			len(decoded.Unavailable), len(signalCoverageGaps), decoded.Unavailable)
	}
	byClass := map[string]UnavailableEntry{}
	for _, u := range decoded.Unavailable {
		byClass[u.SymbolOrClass] = u
		if u.Reason == "" {
			t.Errorf("%s has no reason", u.SymbolOrClass)
		}
		switch u.Category {
		case unavailableNoCurrentReading, unavailableUnregistered,
			unavailableNotScored, unavailableNotCollected:
		default:
			t.Errorf("%s has category %q", u.SymbolOrClass, u.Category)
		}
	}
	for class, want := range map[string]string{
		// Withheld every pass: eligible, scored by nothing.
		"IR_GOLD_FUND_AYAR": unavailableNoCurrentReading,
		"IR_GOLD_FUND_TALA": unavailableNoCurrentReading,
		// Collected daily and deliberately not scored. A page that called these
		// "not collected" would be stating the opposite of the truth.
		"DXY": unavailableNotScored, "US10Y": unavailableNotScored,
		"BRENT_OIL": unavailableNotScored, "IR_GOLD_FUND_FLOW": unavailableNotScored,
		"IR_GOLD_FUND_KAHRABA": unavailableNotScored,
		// Never collected at all.
		"cars": unavailableNotCollected, "housing": unavailableNotCollected,
		"tehran_equities": unavailableNotCollected, "ir_silver": unavailableNotCollected,
	} {
		entry, ok := byClass[class]
		if !ok {
			t.Errorf("the fixture does not account for %q", class)
			continue
		}
		if entry.Category != want {
			t.Errorf("%s category = %q, want %q", class, entry.Category, want)
		}
	}
}

// extractItemObjects pulls the raw JSON of each element of "items", so the key
// ORDER of an item can be checked (a map would lose it).
func extractItemObjects(t *testing.T, body []byte) []json.RawMessage {
	t.Helper()
	var envelope struct {
		Items []json.RawMessage `json:"items"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		t.Fatalf("decode items: %v", err)
	}
	return envelope.Items
}
