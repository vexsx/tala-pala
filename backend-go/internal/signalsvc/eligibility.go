package signalsvc

// Which symbols this service publishes a buy/hold/sell reading for, which it
// refuses to, and what this platform does not collect at all.
//
// THE POINT OF THIS FILE IS THE REFUSALS. A multi-asset advisory that quietly
// answers every symbol it is handed is worse than one that answers seven,
// because the reader cannot tell a scored asset from a defaulted one. So the
// eligible set is enumerated, every exclusion carries a stated reason, and the
// classes of asset that were never collected are named on the wire instead of
// being left as a gap the page silently implies away.
//
// Eligibility is NOT derived from `instruments`, and that is deliberate. A rule
// such as "kind IN (market_price, fx) AND quote_currency NOT IN (PCT, INDEX)"
// would admit BRENT_OIL and IR_GOLD_FUND_KAHRABA, which are excluded for
// reasons the registry does not encode: one is macro context for the models,
// the other has never collected an observation. Display metadata still comes
// from the registry (see overview.go) -- this file carries the policy, not the
// vocabulary.

import "fmt"

// defaultSignalSymbol is what ?symbol= means when absent. Overview, Brief and
// ActionPlanner call /signals/current with no parameter and must keep getting
// the Tehran 18k gold reading they get today.
const defaultSignalSymbol = "IR_GOLD_18K"

// eligibleSignalSymbols is the closed set, in report order. Order is part of
// the contract: /signals/overview returns items in exactly this sequence, so a
// page does not reshuffle between polls, and the primary instrument leads.
//
// Every entry is an asset a reader can actually hold and pay a round trip on,
// and every entry has price history in `prices` (measured on production
// 2026-09-09; row counts in the comments are that census).
var eligibleSignalSymbols = []string{
	"IR_GOLD_18K",       // 18,593 rows from 2013-07-22; 7 active models
	"USD_IRT",           // 17,850 rows from 2011-11-26; no model, technical-only
	"IR_COIN_EMAMI",     //  6,642 rows from 2010-04-04; no model, technical-only
	"XAUUSD",            // 11,678 rows from 2021-12-30; 7 active models
	"XAGUSD",            // 11,813 rows from 2021-07-26; no model, technical-only
	"IR_GOLD_FUND_AYAR", //    101 rows from 2026-07-21; ~50 days, most factors omitted
	"IR_GOLD_FUND_TALA", //    101 rows from 2026-07-21; ~50 days, most factors omitted
}

// eligibleSignalSymbolSet is the membership index for the list above.
var eligibleSignalSymbolSet = func() map[string]bool {
	set := make(map[string]bool, len(eligibleSignalSymbols))
	for _, s := range eligibleSignalSymbols {
		set[s] = true
	}
	return set
}()

// refusedSignalSymbols names the registered symbols this service will not score,
// each with the reason it is refused, in report order.
//
// IT IS A LIST, NOT JUST A MAP, AND IT IS ON THE WIRE. These five are collected
// (four of them daily) and a reader can see them on the charts, so a board that
// simply omitted them left the page's account of its own coverage incomplete:
// the reader could not tell "we score this and it is pending" from "we
// deliberately do not score this" from "we never heard of it". They therefore
// appear in /signals/overview's `unavailable` block carrying the SAME sentence
// a 400 would return, under a category that says which kind of absence this is.
// A map alone could not do that -- Go randomizes map iteration, and the block
// would reshuffle between polls.
var refusedSignalSymbols = []UnavailableEntry{
	{
		SymbolOrClass: "DXY",
		Category:      unavailableNotScored,
		Reason: "DXY is an index level, not a price a reader holds or pays a round-trip " +
			"cost on; it is carried as macro context for the models.",
	},
	{
		SymbolOrClass: "US10Y",
		Category:      unavailableNotScored,
		Reason: "US10Y is a yield in percent, not a price; a buy or sell call on a yield " +
			"has no unambiguous meaning, and it is carried as macro context for the models.",
	},
	{
		SymbolOrClass: "BRENT_OIL",
		Category:      unavailableNotScored,
		Reason: "BRENT_OIL is collected as macro context for the models rather than as a " +
			"holding this platform covers; it is the front-month future, which is not what a " +
			"reader of these signals transacts in.",
	},
	{
		SymbolOrClass: "IR_GOLD_FUND_FLOW",
		Category:      unavailableNotScored,
		Reason: "IR_GOLD_FUND_FLOW is the retail/institutional flow RATIO in " +
			"percent -- migration 0024 registers it as kind='index' for exactly this reason -- " +
			"not a fund price, so a buy or sell call on it would be meaningless. It is scored " +
			"as one caution-only factor inside the two gold-fund readings instead.",
	},
	{
		SymbolOrClass: "IR_GOLD_FUND_KAHRABA",
		Category:      unavailableNotScored,
		Reason: "IR_GOLD_FUND_KAHRABA is registered from TSETMC_FUNDS but has " +
			"never collected an observation, so there is no history any factor could be read from.",
	},
}

// ineligibleSignalSymbols is the membership index behind the list above, used to
// refuse a ?symbol= with the very sentence the page shows for it. Derived, so
// the 400 and the board can never state different reasons for the same symbol.
var ineligibleSignalSymbols = func() map[string]string {
	out := make(map[string]string, len(refusedSignalSymbols))
	for _, e := range refusedSignalSymbols {
		out[e.SymbolOrClass] = e.Reason
	}
	return out
}()

// uncollectedAssetClasses is the honesty block on /signals/overview: asset
// classes this platform does NOT collect, named so the seven items above are
// not mistaken for the whole investable universe.
//
// HAND-MAINTAINED ON PURPOSE. There is no table to derive this from -- the
// whole content of each line is "we hold zero rows for this, and here is why
// that is a real gap rather than an oversight". Verified against production on
// 2026-09-09: `prices` has no row for any of them. When collection starts for
// one of these, its entry is deleted here in the same change that adds it to
// eligibleSignalSymbols.
var uncollectedAssetClasses = []UnavailableEntry{
	{
		SymbolOrClass: "cars",
		Category:      unavailableNotCollected,
		// Same shape as housing: an official INDEX exists and is ingested
		// (SCI_CPI_VEHICLES, '071 - vehicle purchase', monthly from Farvardin
		// 1381), but it is a quality-adjusted CPI basket, which by construction
		// smooths away the factory-versus-market divergence a buyer cares about.
		// No source carries more than about five weeks of retrievable Iranian
		// market car prices, so there is no price to score.
		Reason: "No buy/sell reading: no vehicle PRICE series exists in this system, and no " +
			"source publishes more than about five weeks of retrievable Iranian market car " +
			"prices. The Statistical Centre of Iran's vehicle-purchase price INDEX is " +
			"collected (monthly, from Farvardin 1381) and can be compared against inflation " +
			"under Relative value; it is a quality-adjusted basket, not a market quote.",
	},
	{
		SymbolOrClass: "housing",
		Category:      unavailableNotCollected,
		// Precise, because a nearby statement is now true and this one must not
		// be confused with it: the Statistical Centre of Iran's HOUSING and RENT
		// price INDICES are ingested (SCI_CPI_HOUSING, SCI_CPI_RENT, monthly,
		// back to Farvardin 1381) and can be compared against inflation on
		// /relative-value. What does NOT exist is a housing PRICE -- no
		// per-square-metre series and no transaction series -- so there is
		// nothing to score a buy or sell against.
		//
		// The distinction is not pedantic: the SCI housing index is a
		// rent-dominated component of a CONSUMPTION index (housing 95.6x since
		// 1381 against rent 94.5x), so it tracks what people PAY TO OCCUPY
		// housing, not what a property costs. The CBI's monthly Tehran
		// price-per-square-metre report ran Farvardin 1396 to Mordad 1403 and
		// then stopped when the CBI lost access to the transaction registry;
		// nothing has replaced it.
		Reason: "No buy/sell reading: this system holds no housing PRICE series -- no " +
			"per-square-metre and no transaction series exists publicly since the Central " +
			"Bank's Tehran report stopped at Mordad 1403. Housing and rent price INDICES " +
			"from the Statistical Centre of Iran are collected (monthly, from Farvardin " +
			"1381) and can be compared against inflation under Relative value; they measure " +
			"the cost of occupying housing, not the price of buying it.",
	},
	{
		SymbolOrClass: "tehran_equities",
		Category:      unavailableNotCollected,
		Reason: "Not collected. The only Tehran-listed instruments here are the gold ETFs; no " +
			"share price for any TSE-listed company is ingested, so no equity can be read.",
	},
	{
		SymbolOrClass: "ir_silver",
		Category:      unavailableNotCollected,
		Reason: "Not collected. There is no local Iranian silver series: XAGUSD is the COMEX " +
			"front-month contract quoted in USD per troy ounce, not a Tehran toman silver price.",
	},
}

// signalCoverageGaps is the complete, ordered account of everything this board
// does NOT publish a reading for, and it is why the page can describe its own
// boundary without contradicting itself.
//
// It carries two different kinds of absence, and they used to be collapsed into
// one undifferentiated list under a single sentence. That sentence could only
// ever be false about half of it: five of these symbols ARE collected -- DXY,
// US10Y and BRENT_OIL daily, IR_GOLD_FUND_FLOW with the funds -- and are
// refused because they are index levels, yields and ratios rather than because
// the data is missing, while cars and housing genuinely have zero rows. Each
// entry now states its own `category`, so a client renders the right sentence
// per group instead of one blanket claim over all of them.
//
// Refusals first, then the structural gaps: the refusals are about symbols this
// system holds and the reader can see elsewhere in the app, and the gaps are
// about whole asset classes it has never touched.
var signalCoverageGaps = func() []UnavailableEntry {
	out := make([]UnavailableEntry, 0, len(refusedSignalSymbols)+len(uncollectedAssetClasses))
	out = append(out, refusedSignalSymbols...)
	out = append(out, uncollectedAssetClasses...)
	return out
}()

// --- refusals ----------------------------------------------------------------

// paramError carries the message and details of a rejected parameter, so each
// handler writes exactly one kind of 400. Same shape as economic/handlers.go:
// invalid caller input is refused with a specific message, never clamped,
// defaulted or silently substituted.
type paramError struct {
	Message string
	Details map[string]any
}

func (e *paramError) Error() string { return e.Message }

// symbolVerdict is what the eligibility policy alone can decide about a symbol.
type symbolVerdict int

const (
	// verdictEligible: a signal is published for this symbol.
	verdictEligible symbolVerdict = iota
	// verdictIneligible: known to the policy, refused, reason already stated.
	verdictIneligible
	// verdictUnknownToPolicy: the policy has nothing to say. Only the
	// `instruments` registry can tell "this symbol does not exist" from "this
	// symbol exists and is simply not something we score", and those are
	// different messages to send a client -- see registryIneligibleReason.
	verdictUnknownToPolicy
)

// ParseSignalSymbol resolves ?symbol= against the eligibility policy. Pure: no
// clock, no database.
//
// An absent symbol means the primary one, as on /market/candles and
// /predictions. Case is NOT normalized: symbols are uppercase in the registry,
// and upper-casing a caller's ?symbol=ir_gold_18k would be substituting input
// rather than refusing it.
func ParseSignalSymbol(raw string) (symbol string, verdict symbolVerdict, reason string) {
	if raw == "" {
		return defaultSignalSymbol, verdictEligible, ""
	}
	if eligibleSignalSymbolSet[raw] {
		return raw, verdictEligible, ""
	}
	if why, ok := ineligibleSignalSymbols[raw]; ok {
		return raw, verdictIneligible, why
	}
	return raw, verdictUnknownToPolicy, ""
}

// registryIneligibleReason explains why a symbol that IS in the instrument
// registry still gets no signal. Pure: the sentence is assembled from the two
// registry fields that carry the answer, so a symbol added by a future
// migration is refused with a true reason instead of a generic one.
func registryIneligibleReason(code, kind, quoteCurrency string) string {
	switch {
	case kind == "economic_series":
		return fmt.Sprintf(
			"%s is an economic series -- revisable macro data with a publication lag, "+
				"not a price a reader holds -- so no buy or sell reading is published for it.", code)
	case quoteCurrency == "PCT" || quoteCurrency == "RATIO":
		return fmt.Sprintf(
			"%s is quoted in %s: it is a rate or a ratio, not a price, and a round trip "+
				"cannot be costed on it.", code, quoteCurrency)
	case quoteCurrency == "INDEX":
		return fmt.Sprintf(
			"%s is an index level, not a price a reader holds or pays a round-trip cost on.", code)
	}
	return fmt.Sprintf(
		"%s is registered but is not signal-eligible; readings are published only for the "+
			"instruments listed in `eligible`.", code)
}

// unknownSymbolMessage is the other 400: the symbol is not in the registry at
// all. Distinct from the sentence above on purpose -- "you asked about
// something that does not exist here" and "that exists, but is not the kind of
// thing we score" send a client to two different fixes.
func unknownSymbolMessage(code string) string {
	return fmt.Sprintf(
		"unknown symbol %q: it is not in the instrument registry.", code)
}

// symbolRefusalDetails is the details map every symbol 400 carries, so a client
// can render the eligible set without a second request.
func symbolRefusalDetails(raw string) map[string]any {
	return map[string]any{
		"symbol":   raw,
		"eligible": append([]string(nil), eligibleSignalSymbols...),
	}
}
