package signalsvc

import (
	"encoding/json"
	"strings"
	"testing"
	"time"
)

// --- the frozen /signals/current shape ---------------------------------------

// currentContractKeys is the payload Overview, Brief and ActionPlanner consume
// today, in wire order. It is written out rather than derived so that adding a
// field to signalRow fails HERE, in a test whose comment explains the cost,
// instead of in a frontend that silently stopped matching the server.
//
// `symbol` is deliberately absent: the caller already knows which symbol it
// asked for, and the multi-asset contract lives on /signals/overview.
var currentContractKeys = []string{
	"id", "generated_at", "signal", "score", "confidence", "explanation",
	"supporting", "conflicting", "risks", "invalidation", "review_at",
	"data_fresh", "inputs",
}

// jsonKeysInOrder reads the top-level keys of an object as they were written.
func jsonKeysInOrder(t *testing.T, body []byte) []string {
	t.Helper()
	dec := json.NewDecoder(strings.NewReader(string(body)))
	tok, err := dec.Token()
	if err != nil || tok != json.Delim('{') {
		t.Fatalf("not a JSON object: %v", err)
	}
	var keys []string
	depth := 0
	for dec.More() || depth > 0 {
		tok, err := dec.Token()
		if err != nil {
			t.Fatalf("decode: %v", err)
		}
		switch v := tok.(type) {
		case json.Delim:
			switch v {
			case '{', '[':
				depth++
			case '}', ']':
				depth--
				if depth < 0 {
					return keys
				}
			}
		case string:
			if depth == 0 {
				keys = append(keys, v)
				// consume the value
				var discard json.RawMessage
				if err := dec.Decode(&discard); err != nil {
					t.Fatalf("decode value of %q: %v", v, err)
				}
			}
		}
	}
	return keys
}

func TestCurrentResponseShapeIsFrozen(t *testing.T) {
	body, err := json.Marshal(sampleCurrentRow())
	if err != nil {
		t.Fatal(err)
	}
	got := jsonKeysInOrder(t, body)
	if len(got) != len(currentContractKeys) {
		t.Fatalf("/signals/current has %d fields (%v), want %d (%v)",
			len(got), got, len(currentContractKeys), currentContractKeys)
	}
	for i, key := range currentContractKeys {
		if got[i] != key {
			t.Errorf("field %d = %q, want %q — Overview, Brief and ActionPlanner "+
				"consume this payload byte for byte", i, got[i], key)
		}
	}
}

// The SELECT list is the other half of the same promise: the same columns, in
// the same order, so the gold reading serialises exactly as it does today.
func TestSignalColsUnchangedByTheMultiAssetWork(t *testing.T) {
	const want = `id, generated_at, signal, score, confidence, explanation,
	supporting, conflicting, risks, invalidation, review_at, data_fresh, inputs`
	if signalCols != want {
		t.Errorf("signalCols changed:\n got %q\nwant %q", signalCols, want)
	}
	if strings.Contains(signalCols, "symbol") {
		t.Error("symbol is filtered on, never selected: adding it changes the payload")
	}
}

// Before 0026 the statement had no WHERE clause and the table held gold only.
// With other symbols writing into it, an unfiltered "latest row" would hand a
// caller asking about Tehran gold whichever symbol was scored last.
func TestSignalSelectsFilterBySymbol(t *testing.T) {
	for name, stmt := range map[string]string{
		"current": currentSignalSelect,
		"history": historySignalSelect,
	} {
		if !strings.Contains(stmt, "WHERE symbol =") {
			t.Errorf("%s statement does not filter by symbol:\n%s", name, stmt)
		}
		if !strings.Contains(stmt, "ORDER BY generated_at DESC") {
			t.Errorf("%s statement is not newest-first:\n%s", name, stmt)
		}
	}
}

// The default is what makes the three existing consumers keep working: they
// send no ?symbol= at all.
func TestCurrentDefaultsToGold(t *testing.T) {
	symbol, verdict, _ := ParseSignalSymbol("")
	if symbol != "IR_GOLD_18K" || verdict != verdictEligible {
		t.Fatalf("an absent ?symbol= resolved to %q (verdict %v), want IR_GOLD_18K",
			symbol, verdict)
	}
}

// The overview reads the newest row per symbol off the index migration 0026
// adds; without the DISTINCT ON it would return every historical reading.
func TestLatestSignalSelectIsOneRowPerSymbol(t *testing.T) {
	if !strings.Contains(latestSignalPerSymbolSelect, "DISTINCT ON (symbol)") {
		t.Errorf("overview statement is not one row per symbol:\n%s", latestSignalPerSymbolSelect)
	}
	if !strings.Contains(latestSignalPerSymbolSelect, "ORDER BY symbol, generated_at DESC") {
		t.Errorf("DISTINCT ON needs the matching ORDER BY:\n%s", latestSignalPerSymbolSelect)
	}
}

// The bound belongs in the STATEMENT, not in the projection above it. `signals`
// is pruned only at a year (retention.py), and the engine legitimately stops
// publishing for a symbol whose factors stop computing -- universe.py withholds
// rather than writing a hold it cannot support. Without this predicate the
// overview re-served that symbol's last reading forever, under an `as_of` taken
// from whatever published minutes ago. Fetching it and then filtering it in Go
// would leave a working code path that serves it.
func TestLatestSignalSelectIsRecencyBounded(t *testing.T) {
	if !strings.Contains(latestSignalPerSymbolSelect, "generated_at >= $2") {
		t.Errorf("overview statement has no recency bound:\n%s", latestSignalPerSymbolSelect)
	}
	if signalReadingMaxAge <= signalReadingStaleAfter {
		t.Errorf("the hard bound (%v) must be looser than the stale flag (%v), or a "+
			"reading could never be shown as stale before it disappeared",
			signalReadingMaxAge, signalReadingStaleAfter)
	}
}

// sampleCurrentRow is one realistic gold reading, shaped exactly as
// prediction-python writes it. Shared with the fixture generator so the file
// the frontend tests against and the shape this test freezes cannot drift.
//
// Every STRING below is in compute_signal's own template, not a paraphrase of
// one, because a paraphrase teaches the frontend a wording the server never
// emits. In particular:
//
//   - the SMA line is the three-way "established uptrend" sentence, which is
//     the branch last_price > sma20 > sma50 takes;
//   - the forecast line quotes the WEIGHTED move, not the 1d leg, and formats
//     the hurdle as ~%.1f -- an observed 0.49% prints as "~0.5%";
//   - `explanation` is assembled from `headline` exactly as the engine
//     assembles it, so the two cannot disagree;
//   - `invalidation` is the 40 < score < 60 branch, which is the plain
//     "reassess" sentence: the SMA-crossing form is only produced at >= 60 or
//     <= 40.
//
// The score (54), the signal word and the confidence are the scorer's
// arithmetic, which Go does not reproduce -- see the header of fixtures_test.go.
func sampleCurrentRow() signalRow {
	reviewAt := time.Date(2026, 9, 9, 12, 0, 0, 0, time.UTC)
	// weighted_exp over {1d:0.31, 3d:0.48, 7d:-0.12} at FORECAST_WEIGHTS
	// {1d:1.0, 3d:0.8, 7d:0.6} = 0.622/2.4.
	weighted := 0.2592
	headline := engineHeadline("hold", 54)
	return signalRow{
		ID:          1223,
		GeneratedAt: fixturePassAt,
		Signal:      "hold",
		Score:       54,
		Confidence:  0.62,
		Explanation: engineExplanation("hold", 54, &weighted, 4, 2),
		// Engine append order: model confidence, SMA trend, MA alignment,
		// momentum. The forecast factor appended to `conflicting` instead,
		// because the move does not clear the hurdle.
		Supporting: jsonArray(
			"Average model confidence is 62%.",
			"Price is above SMA20 and SMA50 (established uptrend).",
			"1D, 4H and 1H moving averages are all stacked bullish on closed candles "+
				"(held 6.4 days). Counted at half weight: it confirms the SMA trend "+
				"factor rather than adding to it.",
			"Positive 10-period momentum (+1.8%).",
		),
		Conflicting: jsonArray(
			"Forecast move (+0.26%) is positive but below the ~0.5% round-trip cost threshold.",
			"Forecast horizons disagree on direction.",
		),
		// compute_signal seeds `risks` with these two: the fixed uncertainty
		// sentence and universe.MARKET_RISK for the symbol.
		Risks: jsonArray(
			"Forecasts are statistical estimates with real uncertainty; markets can move against any signal.",
			"Iranian gold prices are exposed to currency policy shocks and liquidity gaps.",
		),
		Invalidation: "Reassess if input data goes stale.",
		ReviewAt:     &reviewAt, // generated_at + REVIEW_AFTER (6h)
		DataFresh:    true,
		Inputs: inputsBlob(map[string]any{
			"symbol":         "IR_GOLD_18K",
			"evidence_basis": evidenceModelBacked,
			// Measured: the mean of the three per-horizon confidences below.
			// engine.py records the basis so a reader is never invited to
			// compare it with the fixed 0.3 a technical-only row carries.
			"confidence_basis": "model_mean",
			"confidence_reason": "mean of the 3 per-horizon confidence(s) the models " +
				"reported for this asset",
			"model_confidence": 0.62,
			// The model-free first sentence the overview renders. The engine
			// writes it precisely so the wording of a buy/sell call is decided
			// once, in the engine, and never re-derived downstream.
			"headline":            headline,
			"expected_change_pct": map[string]any{"1d": 0.31, "3d": 0.48, "7d": -0.12},
			"confidence":          map[string]any{"1d": 0.66, "3d": 0.61, "7d": 0.59},
			"last_price":          10120000,
			"sma20":               10043000,
			"sma50":               9878000,
			"rsi14":               57.4,
			"momentum_10_pct":     1.82,
			"premium_z":           0.64,
			"regime":              "trending",
			"market_closed":       false,
			// Gold is the one asset whose whole profile applies and whose
			// history clears every FactorSpec.min_bars.
			"omitted_factors": []OmittedFactor{},
			// The one measured hurdle in this repo: a hamrahgold two-sided
			// quote, inside costs.py's sanity band and age limit.
			"round_trip_cost_pct":         goldObservedCostPct,
			"round_trip_cost_basis":       "observed_spread",
			"round_trip_cost_source":      "hamrahgold",
			"round_trip_cost_observed_at": "2026-09-09T04:12:00+00:00",
			"round_trip_cost_reason":      "observed hamrahgold buy/sell spread, 1.8h old",
			"round_trip_cost_age_hours":   1.8,
			"stale_reason":                nil,
			"trend_alignment":             "full_bullish",
			"trend_states":                map[string]any{"1d": "bullish", "4h": "bullish", "1h": "bullish"},
			"trend_alignment_age_days":    6.4,
			// Half weight, because the alignment confirms the SMA factor.
			"trend_alignment_points": 4.5,
		}),
	}
}
