// Package liquidity serves Iran's money aggregates -- liquidity (M2), base
// money, money (M1) and quasi-money, as the Central Bank of Iran publishes them
// -- and the dollar set beside them: monthly history, three scenario paths in
// which the dollar keeps pace with M2 growth, the dollar's own trend, an error
// band with its measured coverage, and the backtest that says what each of
// those is worth.
//
// # WHAT THE EVIDENCE ALLOWS, AND THEREFORE WHAT THIS PACKAGE SAYS
//
// The research behind this package (2026-10-11; summarised in docs/api.md,
// "Money and liquidity") measured, over 1394..1405, that NO money-based model forecasts
// the free-market dollar better than the dollar's own past trend. Letting the
// dollar grow at the trailing M2 growth beats "no change" but not the trend;
// the level models -- the "liquidity dollar", log-level regressions, error
// correction -- lose to the trend. So:
//
//   - the forward lines are SCENARIOS ("if the dollar kept pace with M2 growth
//     of X%"), never forecasts, and nothing here is a fair value, an
//     equilibrium, a "should be" price, or advice;
//   - the band is labelled with the coverage it measurably had in the
//     backtest, never with a nominal probability;
//   - the backtest table and a verdict computed from it travel with every
//     answer, so the page states what the numbers show rather than what a
//     reader might hope;
//   - correlation is not causality, and the response says so.
//
// # WHAT THIS PACKAGE IS ALLOWED TO DO
//
// Read and do arithmetic. CBI's figures are ingested by prediction-python
// (provider `cbi`) and read here only through internal/economic's
// point-in-time rule; the dollar's daily closes only through internal/relvalue's
// daily-close statement. Nothing is stored, trained or written: every answer
// is recomputed from stored data and held in memory until a revision key over
// that data changes (handlers.go).
//
// # UNITS AND CALENDAR
//
// Money values are END-OF-MONTH STOCKS in thousand billion rial, exactly as
// CBI publishes them; the page divides by ten for همت and says so. The dollar
// is in toman. The two are never mixed in one number except the explicit
// liquidity-dollar and dollars-per-liquidity arithmetic, which are ratios of
// growth and cancel the unit. Periods are Jalali months, stepped as labels
// (jalali.go), never by Gregorian date arithmetic.
package liquidity

import "time"

const dateLayout = "2006-01-02"

// timeNow is the clock; tests replace it.
var timeNow = time.Now
