// Package equitygate is the one rule every reader of a Tehran equity
// adjustment applies before it multiplies a raw close by a stored
// corporate-action factor: the stored actions must still describe the stored
// bars.
//
// TSETMC's CDN served one copy of the roster's histories without the
// 2023-03-27 session (measured 2026-09-29). Detected over that copy,
// 2023-03-28's reference read as a corporate action on eighteen shares, and
// when the other copy later stored the session the action stayed and rescaled
// every adjusted price before it — اخابر by ×1.0627 — while the verdict still
// said validated. The ingest now re-derives the actions over the stored bars
// (app/equities/ingest.py); a store an older ingest left inconsistent is
// refused on read until the next ingest recomputes it.
//
// It lives in its own package because two readers need it and neither can
// import the other: internal/equities (charts, bars, the screener) imports
// internal/relvalue (Performance and Relative value), and the rule used to be
// only in the first — so in the deploy window it exists for, the Performance
// table kept publishing شپنا at +1,229.68% over five years from a phantom
// ×1.0287 while its chart answered 409 out_of_date. One SQL fragment and one
// sentence, used by both, cannot drift apart.
package equitygate

import "fmt"

// StatusOutOfDate is the status a reader serves for a verdict whose stored
// actions the stored bars contradict, whatever the verdict row says.
const StatusOutOfDate = "out_of_date"

// StaleActionsSQL counts the stored corporate actions of the verdict's version
// whose evidence no longer holds: the stored bar just before the action is not
// the one it was measured from, because a session was stored between them
// after it was detected. A SELECT-list or WHERE expression; it expects
// `i` (equity_instruments) and `a` (the verdict row, with adjustment_version)
// in scope, and counts 0 where `a` is NULL.
const StaleActionsSQL = `(SELECT count(*)
	        FROM corporate_actions c
	        WHERE c.ins_code = i.ins_code
	          AND c.adjustment_version = a.adjustment_version
	          AND c.prev_trade_date IS DISTINCT FROM (
	              SELECT max(b.trade_date) FROM equity_bars b
	              WHERE b.ins_code = c.ins_code AND b.trade_date < c.effective_date)
	       )::int`

// OutOfDateReason is the sentence every reader serves with StatusOutOfDate.
func OutOfDateReason(staleActions int) string {
	return fmt.Sprintf("%d stored corporate action(s) were measured across a session stored "+
		"after they were detected, so the adjustment no longer describes the stored bars; it "+
		"is recomputed by the next ingest", staleActions)
}
