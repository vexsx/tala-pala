package signalsvc

// GET /api/v1/signals/overview -- the latest reading per eligible symbol.
//
// This file READS. Go computes no score, no factor, no forecast and no cost:
// the scorer lives in prediction-python/app/signals/engine.py, which owns the
// weights, the minimum-history gate and the per-asset capability profile.
// Re-deriving any of that here would create a second, silently diverging answer
// to the same question. What follows is a projection of two stored tables --
// `signals` for the reading and `instruments` for the display metadata -- onto
// the wire contract.
//
// Two things this projection refuses to do, and they are the whole reason the
// endpoint exists rather than the client fanning out over /signals/current:
//
//  1. It never fills a hole. A factor the scorer could not compute arrives as
//     an entry in `omitted_factors` carrying the reason it was skipped; it is
//     never replaced by a neutral value, because a neutral value dilutes the
//     score toward hold and then reads, on the page, as evidence that was
//     weighed. Likewise a cost hurdle that was assumed rather than observed
//     says so, and carries the sentence explaining why.
//
//  2. It states the boundary of its own coverage, completely and without
//     contradicting itself. `unavailable` names the asset classes this platform
//     does not collect at all AND the five registered symbols it collects but
//     declines to score, each under a `category` saying which kind of absence it
//     is -- so seven items cannot be mistaken for the whole investable universe,
//     and "not collected" is never claimed about a symbol that is.
//
//  3. It does not serve an old reading as a current one. Every item carries its
//     own age, a reading past the engine's six-hour review window is flagged,
//     and one past signalReadingMaxAge is not returned at all.

import (
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"net/http"
	"time"

	"github.com/jackc/pgx/v5"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// --- how old a reading may be before it stops being current -------------------
//
// The signals job runs HOURLY (SCHEDULE_SIGNALS_CRON, default "10 * * * *") and
// scores every eligible symbol on each pass. But a pass can legitimately publish
// NOTHING for a symbol: universe.withholding_reason refuses to write a row when
// too few factors survived, precisely so an absence of evidence is not filed as
// a considered "hold". Nothing then overwrites that symbol's previous row, and
// retention keeps `signals` rows for a year (signal_retention_days, default 365,
// in prediction-python/app/core/retention.py).
//
// So without a bound the board served a symbol's last reading indefinitely --
// under an `as_of` that is the MAXIMUM generated_at across items, which is
// whatever gold published minutes ago. The reader saw a months-old "buy"
// stamped with the current time, and every honesty field on the item was about
// that old pass, not about now.
const (
	// signalReadingStaleAfter: past this the reading is still shown, and
	// flagged. Six hours is the engine's own statement of how long it considers
	// a reading current -- every row it writes carries
	// review_at = generated_at + REVIEW_AFTER, and REVIEW_AFTER is six hours
	// (prediction-python/app/signals/engine.py). Six missed hourly passes is an
	// outage or a deliberate withholding, not scheduler jitter.
	signalReadingStaleAfter = 6 * time.Hour

	// signalReadingMaxAge: past this it is not served as a reading at all. Four
	// review cycles' worth of consecutive failures is no longer a gap in a live
	// series; the symbol is not being published, and the honest answer is to say
	// so rather than to keep re-serving the last thing that was.
	signalReadingMaxAge = 24 * time.Hour
)

// --- why something is absent, as one machine-readable word -------------------
//
// The `unavailable` block carries four genuinely different kinds of absence,
// and a page that renders one sentence over all of them will contradict the
// per-entry reasons underneath it: "the data does not exist in this system" is
// true of cars and of IR_GOLD_FUND_KAHRABA, and flatly false of DXY, US10Y and
// BRENT_OIL, which are collected daily and refused for being index levels and
// yields rather than prices. The category is what lets a client group them.
const (
	// unavailableNoCurrentReading: signal-eligible, but nothing published
	// inside signalReadingMaxAge -- never scored, or no longer being scored.
	unavailableNoCurrentReading = "no_current_reading"
	// unavailableUnregistered: signal-eligible, but absent from `instruments`,
	// so it cannot even be named. An operational fault, not a policy.
	unavailableUnregistered = "not_registered"
	// unavailableNotScored: collected and registered, deliberately not scored.
	// The per-symbol reason says which property disqualifies it.
	unavailableNotScored = "not_scored"
	// unavailableNotCollected: this system holds no data for it at all.
	unavailableNotCollected = "not_collected"
)

// Evidence basis: whether a trained model stood behind the reading, or whether
// it is trend, momentum and premium only. Only IR_GOLD_18K and XAUUSD carry
// active models (7 each, measured 2026-09-09); the other five eligible symbols
// are technical-only and the contract has to say so, because "hold" from a
// model and "hold" from an RSI are not the same claim.
const (
	evidenceModelBacked   = "model_backed"
	evidenceTechnicalOnly = "technical_only"
	// evidenceUnknown covers a stored `inputs` blob that could not be read at
	// all. Unreachable while the column is JSONB NOT NULL DEFAULT '{}' -- an
	// empty object parses fine and is honestly technical_only -- but claiming
	// "no model evidence" about a row we could not read would be asserting
	// something we do not know.
	evidenceUnknown = "unknown"
)

// costBasisUnrecorded labels a hurdle whose provenance the generator did not
// write down. The number is still shown, because it is what the scorer used;
// what is withheld is any claim about where it came from.
const costBasisUnrecorded = "unrecorded"

const omittedFactorNoReason = "no reason recorded by the generator"

// OmittedFactor is one factor the scorer did not weigh, and why. This is the
// load-bearing field of the whole contract: it is what lets the page show that
// a ~50-day fund history could not carry an SMA-50, instead of showing a score
// that silently absorbed a missing indicator as neutral.
type OmittedFactor struct {
	Factor string `json:"factor"`
	Reason string `json:"reason"`
}

// CostBasis is the provenance of the round-trip cost hurdle, mirroring
// CostResolution in prediction-python/app/core/costs.py.
//
// It is an object rather than a bare string because the reason has to travel:
// an observed hamrahgold spread exists for IR_GOLD_18K and for nothing else, so
// every other asset's hurdle is an ASSUMPTION, and a UI that cannot tell the
// two apart will present a guess with the authority of a quote.
type CostBasis struct {
	// "observed_spread" | "assumed" | costBasisUnrecorded
	Basis string `json:"basis"`
	// Provider code when observed; null when the figure was assumed.
	Source *string `json:"source"`
	// When the quote behind an observed spread was taken (UTC).
	ObservedAt *time.Time `json:"observed_at"`
	// The generator's own sentence explaining the choice.
	Reason *string `json:"reason"`
}

// OverviewItem is one eligible symbol's latest reading. Field order here is the
// wire order; the frontend fixture is generated by marshalling this struct.
type OverviewItem struct {
	Symbol string `json:"symbol"`

	// Display metadata, joined from `instruments`. Not hardcoded here: the
	// registry exists precisely so Go stops carrying symbol vocabulary, and
	// quality_tier/is_proxy in particular are how the page says that XAUUSD is
	// a COMEX front-month proxy rather than the London spot fix.
	NameEn        string `json:"name_en"`
	NameFa        string `json:"name_fa"`
	QuoteCurrency string `json:"quote_currency"`
	Unit          string `json:"unit"`
	QualityTier   string `json:"quality_tier"`
	IsProxy       bool   `json:"is_proxy"`

	// The reading itself, passed through verbatim from the stored row.
	Signal        string  `json:"signal"`
	Score         int     `json:"score"`
	Confidence    float64 `json:"confidence"`
	EvidenceBasis string  `json:"evidence_basis"`

	// DataFresh is about the INPUTS the scorer was handed: false means the
	// prices feeding this reading were stale when it was computed, and the
	// engine forced a hold. It says nothing about how old the reading is --
	// ReadingStale below answers that, and the two are independent (a reading
	// computed on perfectly fresh prices three days ago is DataFresh true and
	// ReadingStale true).
	DataFresh   bool      `json:"data_fresh"`
	GeneratedAt time.Time `json:"generated_at"`

	// ReadingAgeHours is how long ago this row was written, at request time.
	// It travels because `as_of` on the envelope is the NEWEST item's timestamp,
	// so a page that stamps every row with `as_of` would print today's time over
	// yesterday's call. Rounded to two decimals; the row's own generated_at is
	// the exact value.
	ReadingAgeHours float64 `json:"reading_age_hours"`
	// ReadingStale is ReadingAgeHours past signalReadingStaleAfter: the engine
	// publishes hourly and considers its own readings due for review after six
	// hours, so this one has outlived the window it was written for. It is still
	// shown -- an old reading with its age attached is information; an old
	// reading presented as current is not.
	ReadingStale bool `json:"reading_stale"`

	// Headline is the scorer's own one-line call, taken from `inputs.headline`.
	//
	// NOT `explanation`, which is what this field used to carry. The engine
	// builds explanation FROM the headline and then appends a fixed closing
	// sentence: "This is an uncertain, model-based assessment of current
	// conditions -- not financial advice, and actual outcomes can differ." On the
	// five technical-only symbols that closing clause is a direct
	// self-contradiction, in the most-read string on the board: the same item
	// carries evidence_basis "technical_only" and an omitted_factors entry
	// stating that no model is trained for it, and then the headline calls itself
	// model-based. `inputs.headline` is the model-free first sentence the engine
	// already computes for exactly this purpose ("Conditions currently favor
	// waiting (score 52/100)."), and taking it from there keeps the wording of a
	// buy/sell call inside the engine that made it -- Go still decides nothing.
	Headline string `json:"headline"`
	// TopSupporting/TopConflicting are the FIRST entries of the stored
	// supporting/conflicting lists -- the order the scorer appended them, which
	// is factor order. Go does not re-rank them: choosing which argument is
	// strongest would be scoring, and the full lists stay on /signals/current.
	TopSupporting  *string `json:"top_supporting"`
	TopConflicting *string `json:"top_conflicting"`

	// Never null: an empty list means every factor this asset can carry was
	// computed, which is a different statement from "we did not record".
	OmittedFactors []OmittedFactor `json:"omitted_factors"`

	CostPct   *float64   `json:"cost_pct"`
	CostBasis *CostBasis `json:"cost_basis"`

	// Why the inputs were stale, when the generator recorded a reason. Null
	// rather than invented when it did not: the page can still show `data_fresh`
	// false without Go guessing at a cause.
	StaleReason *string `json:"stale_reason"`
}

// UnavailableEntry names something a reader might reasonably expect on this
// page and explains its absence. `symbol_or_class` carries an asset class this
// platform does not collect (cars, housing), a registered symbol it declines to
// score (DXY, US10Y), or an eligible symbol with no current reading.
type UnavailableEntry struct {
	SymbolOrClass string `json:"symbol_or_class"`
	// Category is which kind of absence this is, from the four constants at the
	// top of this file. It exists because the reasons are not interchangeable
	// and a single sentence written over the whole list is guaranteed to
	// contradict some of the entries under it.
	Category string `json:"category"`
	Reason   string `json:"reason"`
}

// OverviewResponse is the /signals/overview contract.
type OverviewResponse struct {
	// AsOf is the NEWEST generated_at among the items, UTC. Null when nothing
	// has been published: a zero timestamp would read as 1970.
	//
	// It is a maximum, not a common timestamp, and that distinction is why the
	// two fields below exist. Items are scored on one pass but not necessarily
	// published on it, so `as_of` alone let a page stamp the newest time over
	// every row.
	AsOf *time.Time `json:"as_of"`
	// OldestReadingAt is the oldest generated_at among the items, UTC, so the
	// spread between the two is visible without walking the list. Equal to AsOf
	// when every symbol published on the same pass, which is the normal case.
	OldestReadingAt *time.Time `json:"oldest_reading_at"`
	// StaleAfterHours is the threshold behind each item's `reading_stale`,
	// published so a client renders the server's rule rather than its own.
	StaleAfterHours float64 `json:"stale_after_hours"`

	Items       []OverviewItem     `json:"items"`
	Unavailable []UnavailableEntry `json:"unavailable"`
}

// instrumentMeta is the display half of an item, as scanned from `instruments`.
type instrumentMeta struct {
	NameEn        string
	NameFa        string
	QuoteCurrency string
	Unit          string
	QualityTier   string
	IsProxy       bool
}

// overviewSignalRow is the latest `signals` row for one symbol, as scanned.
type overviewSignalRow struct {
	Symbol      string
	Signal      string
	Score       int
	Confidence  float64
	Explanation string
	Supporting  json.RawMessage
	Conflicting json.RawMessage
	DataFresh   bool
	GeneratedAt time.Time
	Inputs      json.RawMessage
}

// --- pure projection ---------------------------------------------------------

// projectedInputs is what the overview reads out of the stored inputs JSONB.
type projectedInputs struct {
	EvidenceBasis string
	// Headline is `inputs.headline`, the engine's model-free first sentence.
	// Null on the rows written before the field existed; buildOverviewItem then
	// falls back to the stored explanation, which is what those rows have.
	Headline       *string
	OmittedFactors []OmittedFactor
	CostPct        *float64
	CostBasis      *CostBasis
	StaleReason    *string
}

// projectSignalInputs reads the fields the overview needs out of one stored
// `inputs` blob. Pure: no clock, no database.
//
// The blob is decoded field by field (map[string]json.RawMessage) rather than
// into one struct, the same way projectTimeframe does in
// internal/prices/trendalignment.go. That is deliberate: one malformed field
// then costs exactly that field, instead of sinking the whole projection and
// turning a readable reading into a blank card.
func projectSignalInputs(raw json.RawMessage) projectedInputs {
	stored := map[string]json.RawMessage{}
	if len(raw) == 0 || json.Unmarshal(raw, &stored) != nil {
		return projectedInputs{
			EvidenceBasis:  evidenceUnknown,
			OmittedFactors: []OmittedFactor{},
		}
	}
	return projectedInputs{
		EvidenceBasis:  resolveEvidenceBasis(stored),
		Headline:       readString(stored["headline"]),
		OmittedFactors: readOmittedFactors(stored["omitted_factors"]),
		CostPct:        readFloat(stored["round_trip_cost_pct"]),
		CostBasis:      readCostBasis(stored),
		StaleReason:    readString(stored["stale_reason"]),
	}
}

// resolveEvidenceBasis answers "did a trained model stand behind this reading?".
//
// The generator records the answer directly in `inputs.evidence_basis`, and
// that is used whenever it is present. The fallback exists for the 1,222 rows
// written before the field did: for those, the answer is still recorded, just
// indirectly -- `inputs.expected_change_pct` is the per-horizon forecast map the
// scorer was handed, and an empty one is exactly the condition engine.py already
// calls technical-only in its own risk note ("No model forecasts were available:
// this reading is technical-only"). Reading that map is a projection of what the
// scorer recorded, not a re-derivation of the reading; the alternative -- letting
// every legacy row default to "model_backed" -- would be a fabrication.
func resolveEvidenceBasis(stored map[string]json.RawMessage) string {
	if v := readString(stored["evidence_basis"]); v != nil {
		switch *v {
		case evidenceModelBacked, evidenceTechnicalOnly:
			return *v
		}
	}
	forecasts := map[string]*float64{}
	if raw, ok := stored["expected_change_pct"]; ok && len(raw) > 0 {
		if err := json.Unmarshal(raw, &forecasts); err != nil {
			return evidenceUnknown
		}
	}
	for _, v := range forecasts {
		if v != nil {
			return evidenceModelBacked
		}
	}
	return evidenceTechnicalOnly
}

// readOmittedFactors projects the list of skipped factors. Entries without a
// factor name are dropped (they say nothing); an entry whose reason is missing
// keeps its place and is labelled as unexplained rather than quietly given a
// plausible one.
func readOmittedFactors(raw json.RawMessage) []OmittedFactor {
	out := []OmittedFactor{}
	if len(raw) == 0 {
		return out
	}
	var entries []OmittedFactor
	if err := json.Unmarshal(raw, &entries); err != nil {
		return out
	}
	for _, e := range entries {
		if e.Factor == "" {
			continue
		}
		if e.Reason == "" {
			e.Reason = omittedFactorNoReason
		}
		out = append(out, e)
	}
	return out
}

// readCostBasis assembles the hurdle's provenance. Null when the row records no
// cost at all -- the page then shows no hurdle rather than a default one.
func readCostBasis(stored map[string]json.RawMessage) *CostBasis {
	basis := readString(stored["round_trip_cost_basis"])
	source := readString(stored["round_trip_cost_source"])
	reason := readString(stored["round_trip_cost_reason"])
	observedAt := readTime(stored["round_trip_cost_observed_at"])
	if basis == nil && source == nil && reason == nil && observedAt == nil {
		if readFloat(stored["round_trip_cost_pct"]) == nil {
			return nil
		}
		// A hurdle with no provenance at all: the number is real (the scorer
		// used it), its origin is not known, and saying so beats implying it
		// was observed.
		unrecorded := costBasisUnrecorded
		return &CostBasis{Basis: unrecorded}
	}
	out := &CostBasis{Basis: costBasisUnrecorded, Source: source, ObservedAt: observedAt, Reason: reason}
	if basis != nil {
		out.Basis = *basis
	}
	return out
}

func readString(raw json.RawMessage) *string {
	if len(raw) == 0 {
		return nil
	}
	var v *string
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil
	}
	return v
}

func readFloat(raw json.RawMessage) *float64 {
	if len(raw) == 0 {
		return nil
	}
	var v *float64
	if err := json.Unmarshal(raw, &v); err != nil {
		return nil
	}
	return v
}

// readTime accepts the RFC3339 string Python writes into JSONB and normalizes
// it to UTC. Anything else yields null: a timestamp this service cannot place
// on the UTC clock is worse than no timestamp.
func readTime(raw json.RawMessage) *time.Time {
	s := readString(raw)
	if s == nil {
		return nil
	}
	t, err := time.Parse(time.RFC3339, *s)
	if err != nil {
		return nil
	}
	u := t.UTC()
	return &u
}

// firstEntry returns the first string of a stored JSON array, or null for an
// empty, absent or unreadable one.
func firstEntry(raw json.RawMessage) *string {
	if len(raw) == 0 {
		return nil
	}
	var entries []string
	if err := json.Unmarshal(raw, &entries); err != nil {
		return nil
	}
	for _, e := range entries {
		if e != "" {
			return &e
		}
	}
	return nil
}

// headlineFor picks the line the board prints for a reading.
//
// `inputs.headline` when the row has one: it is the engine's own first
// sentence, computed before the blended explanation is assembled and free of
// the "This is an uncertain, model-based assessment" clause that explanation
// always ends with -- a clause that contradicts the item beside it on every
// technical_only row.
//
// The stored explanation otherwise, because that is genuinely all the rows
// written before the field existed have. A row from that era is model-backed
// gold (the table was single-asset until migration 0026), so its explanation's
// closing clause is true of it; the contradiction this guards against only
// arises for the six symbols the field has existed for. Falling back is
// therefore projecting what was recorded, not asserting something new -- the
// same reasoning resolveEvidenceBasis applies to the same legacy rows.
func headlineFor(row overviewSignalRow, in projectedInputs) string {
	if in.Headline != nil && *in.Headline != "" {
		return *in.Headline
	}
	return row.Explanation
}

// buildOverviewItem projects one stored row plus its registry metadata onto the
// contract. Pure: no database, and the clock arrives as an argument rather than
// being read here, which is what keeps every branch unit-testable.
func buildOverviewItem(now time.Time, row overviewSignalRow, meta instrumentMeta) OverviewItem {
	in := projectSignalInputs(row.Inputs)
	generatedAt := row.GeneratedAt.UTC()
	age := now.UTC().Sub(generatedAt)
	return OverviewItem{
		Symbol:          row.Symbol,
		NameEn:          meta.NameEn,
		NameFa:          meta.NameFa,
		QuoteCurrency:   meta.QuoteCurrency,
		Unit:            meta.Unit,
		QualityTier:     meta.QualityTier,
		IsProxy:         meta.IsProxy,
		Signal:          row.Signal,
		Score:           row.Score,
		Confidence:      row.Confidence,
		EvidenceBasis:   in.EvidenceBasis,
		DataFresh:       row.DataFresh,
		GeneratedAt:     generatedAt,
		ReadingAgeHours: roundHours(age),
		ReadingStale:    age > signalReadingStaleAfter,
		Headline:        headlineFor(row, in),
		TopSupporting:   firstEntry(row.Supporting),
		TopConflicting:  firstEntry(row.Conflicting),
		OmittedFactors:  in.OmittedFactors,
		CostPct:         in.CostPct,
		CostBasis:       in.CostBasis,
		StaleReason:     in.StaleReason,
	}
}

// roundHours renders a duration as hours to two decimals. A clock skew that
// puts a row marginally in the future reports 0 rather than a negative age:
// "-0.02 hours old" is noise, and no reading is ever fresher than now.
func roundHours(d time.Duration) float64 {
	if d < 0 {
		return 0
	}
	return math.Round(d.Hours()*100) / 100
}

// Reasons an eligible symbol appears under `unavailable` instead of `items`.
//
// reasonNoCurrentReading covers two situations the bounded read genuinely
// cannot tell apart -- never scored, and no longer being scored -- and says so
// rather than picking one. Splitting them would take a second, unbounded query
// whose only product is a sentence, and asserting "never scored" about a symbol
// that has a year of rows just outside the window would be false.
var reasonNoCurrentReading = fmt.Sprintf(
	"Signal-eligible, but no reading has been published for it in the last %.0f hours. "+
		"Either it has never been scored, or the engine has stopped publishing for it "+
		"(it withholds a reading rather than writing a hold it cannot support). Its last "+
		"reading, if any, is deliberately not shown: a buy or sell call that old is not a "+
		"current call.", signalReadingMaxAge.Hours())

const reasonUnregistered = "Signal-eligible, but absent from the instrument registry, so its " +
	"name, currency and quality tier cannot be stated."

// buildOverviewResponse assembles the whole contract from the eligible list,
// the registry and the latest reading per symbol. Pure: no clock, no database,
// which is what makes every branch below unit-testable.
//
// An eligible symbol with no reading is NOT dropped and NOT emitted as a
// zero-valued item. It moves to `unavailable` with the reason it is missing,
// because "we score this and it has not been scored yet" is information the
// page should show, and a hold assembled out of nothing is not.
func buildOverviewResponse(
	now time.Time,
	eligible []string,
	registry map[string]instrumentMeta,
	latest map[string]overviewSignalRow,
	gaps []UnavailableEntry,
) OverviewResponse {
	resp := OverviewResponse{
		StaleAfterHours: signalReadingStaleAfter.Hours(),
		Items:           []OverviewItem{},
		Unavailable:     []UnavailableEntry{},
	}
	var newest, oldest *time.Time
	for _, symbol := range eligible {
		meta, registered := registry[symbol]
		if !registered {
			resp.Unavailable = append(resp.Unavailable, UnavailableEntry{
				SymbolOrClass: symbol,
				Category:      unavailableUnregistered,
				Reason:        reasonUnregistered,
			})
			continue
		}
		// `latest` is already bounded to signalReadingMaxAge by the query that
		// filled it (latestSignalPerSymbolSelect), so "absent" here means "no
		// CURRENT reading", not "no row in the table".
		row, published := latest[symbol]
		if !published {
			resp.Unavailable = append(resp.Unavailable, UnavailableEntry{
				SymbolOrClass: symbol,
				Category:      unavailableNoCurrentReading,
				Reason:        reasonNoCurrentReading,
			})
			continue
		}
		item := buildOverviewItem(now, row, meta)
		if newest == nil || item.GeneratedAt.After(*newest) {
			t := item.GeneratedAt
			newest = &t
		}
		if oldest == nil || item.GeneratedAt.Before(*oldest) {
			t := item.GeneratedAt
			oldest = &t
		}
		resp.Items = append(resp.Items, item)
	}
	resp.AsOf = newest
	resp.OldestReadingAt = oldest
	// The coverage gaps come last: the per-symbol entries above are operational
	// ("not published right now"), these are policy and structure ("collected but
	// not scored", "never collected"), and each carries its own category.
	resp.Unavailable = append(resp.Unavailable, gaps...)
	return resp
}

// --- SQL ---------------------------------------------------------------------

// instrumentMetaSelect reads the display half of the contract from the registry
// rather than from a symbol table in Go. `enabled` is not filtered on: a symbol
// this service scores must still be describable if an operator disables its
// collection, and the row would otherwise vanish into "unregistered".
const instrumentMetaSelect = `
	SELECT code, name_en, name_fa, quote_currency, unit, quality_tier, is_proxy
	  FROM instruments
	 WHERE code = ANY($1::text[])`

// latestSignalPerSymbolSelect is one DISTINCT ON per symbol -- the newest
// CURRENT reading each eligible symbol has. It rides idx_signals_symbol_time
// (symbol, generated_at DESC), which migration 0026 adds for exactly this scan;
// the pre-existing idx_signals_time leads on generated_at and would walk the
// whole table discarding six symbols out of seven to find one row.
//
// THE RECENCY BOUND IS PART OF THE QUERY, not of the projection above it, and
// that placement is the point: a row older than signalReadingMaxAge is not
// fetched at all, so no later refactor of the projection can leak it onto the
// board. Without it this statement happily returned a symbol's last reading
// forever -- `signals` is pruned only at a year (retention.py), and the engine
// legitimately stops publishing for a symbol whose factors stop computing, so
// "newest row" and "current reading" are not the same question.
const latestSignalPerSymbolSelect = `
	SELECT DISTINCT ON (symbol)
	       symbol, signal, score, confidence, explanation, supporting,
	       conflicting, data_fresh, generated_at, inputs
	  FROM signals
	 WHERE symbol = ANY($1::text[])
	   AND generated_at >= $2
	 ORDER BY symbol, generated_at DESC`

// loadInstrumentMeta reads the registry rows for the given codes.
func (h *Handler) loadInstrumentMeta(r *http.Request, codes []string) (map[string]instrumentMeta, error) {
	rows, err := h.Pool.Query(r.Context(), instrumentMetaSelect, codes)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]instrumentMeta, len(codes))
	for rows.Next() {
		var code string
		var m instrumentMeta
		if err := rows.Scan(&code, &m.NameEn, &m.NameFa, &m.QuoteCurrency,
			&m.Unit, &m.QualityTier, &m.IsProxy); err != nil {
			return nil, err
		}
		out[code] = m
	}
	return out, rows.Err()
}

// loadLatestSignals reads the newest signal row per symbol, within the recency
// bound. A symbol whose newest row is older than that is absent from the map,
// which is what moves it to `unavailable` instead of onto the board.
func (h *Handler) loadLatestSignals(r *http.Request, codes []string, now time.Time) (map[string]overviewSignalRow, error) {
	rows, err := h.Pool.Query(r.Context(), latestSignalPerSymbolSelect,
		codes, now.UTC().Add(-signalReadingMaxAge))
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := make(map[string]overviewSignalRow, len(codes))
	for rows.Next() {
		var s overviewSignalRow
		if err := rows.Scan(&s.Symbol, &s.Signal, &s.Score, &s.Confidence,
			&s.Explanation, &s.Supporting, &s.Conflicting, &s.DataFresh,
			&s.GeneratedAt, &s.Inputs); err != nil {
			return nil, err
		}
		out[s.Symbol] = s
	}
	return out, rows.Err()
}

// lookupInstrument reads one registry row, to tell an unknown symbol from a
// registered one this service does not score.
func (h *Handler) lookupInstrument(r *http.Request, code string) (kind, quoteCurrency string, found bool, err error) {
	err = h.Pool.QueryRow(r.Context(),
		`SELECT kind, quote_currency FROM instruments WHERE code = $1`, code).
		Scan(&kind, &quoteCurrency)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", "", false, nil
	}
	if err != nil {
		return "", "", false, err
	}
	return kind, quoteCurrency, true, nil
}

// refuseSymbol writes the 400 for a ?symbol= this service will not answer,
// consulting the registry so the message says which kind of refusal it is.
// Returns false when the request has already been answered.
func (h *Handler) refuseSymbol(w http.ResponseWriter, r *http.Request, raw string, verdict symbolVerdict, reason string) bool {
	switch verdict {
	case verdictEligible:
		return true
	case verdictIneligible:
		httpserver.BadRequest(w, reason, symbolRefusalDetails(raw))
		return false
	}
	kind, quoteCurrency, found, err := h.lookupInstrument(r, raw)
	if err != nil {
		h.Log.Error("signals_symbol_lookup", "error", err, "symbol", raw)
		httpserver.Internal(w, "database error")
		return false
	}
	if !found {
		httpserver.BadRequest(w, unknownSymbolMessage(raw), symbolRefusalDetails(raw))
		return false
	}
	httpserver.BadRequest(w, registryIneligibleReason(raw, kind, quoteCurrency), symbolRefusalDetails(raw))
	return false
}

// Overview implements GET /api/v1/signals/overview.
func (h *Handler) Overview(w http.ResponseWriter, r *http.Request) {
	// One clock for the whole response: the recency bound the query applies and
	// the age every item reports have to be measured from the same instant, or
	// an item could be fetched as current and then rendered as expired.
	now := time.Now().UTC()
	codes := append([]string(nil), eligibleSignalSymbols...)

	registry, err := h.loadInstrumentMeta(r, codes)
	if err != nil {
		h.Log.Error("signals_overview_instruments", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	latest, err := h.loadLatestSignals(r, codes, now)
	if err != nil {
		h.Log.Error("signals_overview_signals", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK,
		buildOverviewResponse(now, eligibleSignalSymbols, registry, latest, signalCoverageGaps))
}
