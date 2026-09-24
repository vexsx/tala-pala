package relvalue

// Tehran equities in the cross-asset table.
//
// Until now this package could only see `prices`, so the purchasing-power
// comparison covered gold, coins, the dollar and the global series and stopped
// there -- while 72,000 adjusted daily bars for nineteen Tehran instruments,
// some reaching back to 2001, sat one table away and appeared only in the
// screener. "How did Tehran stocks do against gold, after inflation" is the
// question an Iranian holder actually has, and it was the one question this
// endpoint could not answer.
//
// Three things make equities different from every other row here, and each is
// handled rather than assumed away.
//
// 1. THE UNIT. TSETMC quotes in RIALS and this platform is toman-canonical
//    (migration 0024; IRR is display-only, x10). Returns are ratios, so the
//    factor of ten cancels and a naive implementation would look correct in
//    every percentage on the page -- and then print an end-of-window LEVEL ten
//    times too large, beside gold quoted in toman, with nothing to signal it.
//    The division happens once, here, at the edge where the data enters.
//
// 2. THE ADJUSTMENT. A raw Tehran close series is not a price history: فولاد
//    is x1.52 raw against x907.86 adjusted over nineteen years, because capital
//    increases and splits are not price moves. Every series here is
//    corporate-action adjusted, and an instrument whose adjustment the
//    validation gate did not pass is EXCLUDED with its reason rather than
//    served raw. That is the same rule internal/equities' screenable() applies,
//    for the same reason: a missed action corrupts every return drawn from the
//    series, not only the day it landed on.
//
// 3. HALTED SESSIONS. TSETMC stores a halt as a bar whose final_close is the
//    carried reference price rather than a print. Those sessions are dropped,
//    matching barItem.traded: keeping them would flatten volatility and invent
//    days on which nothing happened but a number existed.

import (
	"context"
	"fmt"
	"sort"
	"time"
)

const (
	// rialsPerToman is the whole reason equityBarsSelect divides. One toman is
	// ten rials; the platform stores and compares toman.
	rialsPerToman = 10.0

	// equityDomain groups these rows in the registry's published order. A
	// domain of their own so a reader can see at a glance which rows are
	// Tehran equities and which are the macro assets they are being compared
	// against.
	equityDomain = "ir_equity"

	// equityStatusValidated mirrors internal/equities' statusValidated. It is
	// repeated rather than imported because importing that package here would
	// make the two handlers circular; the string is the DATABASE's contract,
	// which is what both sides actually depend on.
	equityStatusValidated = "validated"

	// sourceEquityBars marks an instrument whose prices live in equity_bars
	// rather than in `prices`, so performanceSeriesCodes does not ask the
	// prices table for a symbol it has never held.
	sourceEquityBars = "equity_bars"
)

// equityRosterSelect reads the instruments and their newest adjustment verdict.
//
// The LATERAL is copied deliberately from internal/equities' verdictJoin, down
// to ORDER BY computed_at DESC: migration 0028 keys equity_adjustments on
// (ins_code, adjustment_version) so a v2 can be computed beside v1, and a plain
// join would return the instrument twice with no indication which verdict to
// believe. Two places deciding "which adjustment is current" by two different
// rules would be worse than either rule.
//
// LEFT, so an instrument that has never been adjusted still arrives and can be
// excluded WITH A REASON. Dropping it in SQL would make it vanish from the
// warnings too, and a reader would never learn it exists.
const equityRosterSelect = `
	SELECT i.symbol_fa, i.name_fa, i.sector_fa, i.enabled, i.bar_count,
	       COALESCE(a.status, ''), COALESCE(a.refusal_reason, ''),
	       COALESCE(a.adjustment_version, '')
	FROM equity_instruments i
	LEFT JOIN LATERAL (
	    SELECT status, refusal_reason, adjustment_version
	    FROM equity_adjustments
	    WHERE ins_code = i.ins_code
	    ORDER BY computed_at DESC
	    LIMIT 1
	) a ON TRUE
	ORDER BY i.symbol_fa`

// equityBarsSelect is the adjusted daily close per eligible instrument, in
// TOMAN, for traded sessions only.
//
// The factor subquery is the same one internal/equities' screenBarsSelect uses:
// the cumulative factor of the first action effective strictly AFTER this bar,
// and 1.0 when none follows. Bars before the adjustment's own first_bar are
// excluded, because the chain was never validated across them.
const equityBarsSelect = `
	SELECT i.symbol_fa, b.trade_date,
	       b.final_close * COALESCE((
	           SELECT c.cumulative_factor
	           FROM corporate_actions c
	           WHERE c.ins_code = b.ins_code
	             AND c.adjustment_version = a.adjustment_version
	             AND c.effective_date > b.trade_date
	           ORDER BY c.effective_date
	           LIMIT 1
	       ), 1.0) AS adjusted_close_rial
	FROM equity_bars b
	JOIN equity_instruments i ON i.ins_code = b.ins_code
	JOIN LATERAL (
	    SELECT status, adjustment_version, first_bar
	    FROM equity_adjustments
	    WHERE ins_code = i.ins_code
	    ORDER BY computed_at DESC
	    LIMIT 1
	) a ON TRUE
	WHERE i.enabled
	  AND a.status = $1
	  AND (a.first_bar IS NULL OR b.trade_date >= a.first_bar)
	  AND b.trade_date <= $2::date
	  AND (b.volume > 0 OR b.trade_count > 0)
	ORDER BY i.symbol_fa, b.trade_date`

// equityExclusion is one instrument kept OUT of the table, and why.
type equityExclusion struct {
	Symbol string
	Reason string
}

// loadEquityInstruments returns the registry rows for equities this package may
// measure, plus the exclusions for those it may not.
//
// The exclusions are returned rather than logged because they belong to the
// reader: "nineteen Tehran instruments, one excluded because its adjustment was
// refused" is a different and more trustworthy statement than a table that
// silently holds eighteen.
func (h *Handler) loadEquityInstruments(ctx context.Context) ([]instrumentRow, []equityExclusion, error) {
	rows, err := h.Pool.Query(ctx, equityRosterSelect)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()

	out := []instrumentRow{}
	excluded := []equityExclusion{}
	for rows.Next() {
		var symbol, nameFA, sectorFA, status, refusal, version string
		var enabled bool
		var barCount int
		if err := rows.Scan(&symbol, &nameFA, &sectorFA, &enabled, &barCount,
			&status, &refusal, &version); err != nil {
			return nil, nil, err
		}

		inst, ex, ok := equityEligibility(equityRosterRow{
			Symbol: symbol, NameFA: nameFA, SectorFA: sectorFA,
			Enabled: enabled, BarCount: barCount,
			Status: status, Refusal: refusal, Version: version,
		})
		if !ok {
			excluded = append(excluded, ex)
			continue
		}
		out = append(out, inst)
	}
	if err := rows.Err(); err != nil {
		return nil, nil, err
	}
	return out, excluded, nil
}

// loadEquitySeries returns the adjusted daily close per symbol, in toman.
func (h *Handler) loadEquitySeries(ctx context.Context, asOf time.Time) (map[string]dailySeries, error) {
	rows, err := h.Pool.Query(ctx, equityBarsSelect, equityStatusValidated, asOf.UTC())
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := map[string]dailySeries{}
	for rows.Next() {
		var symbol string
		var day time.Time
		var rial float64
		if err := rows.Scan(&symbol, &day, &rial); err != nil {
			return nil, err
		}
		// The one division, at the one edge. See rialsPerToman.
		out[symbol] = append(out[symbol], dailyPoint{
			Day:   day.UTC(),
			Close: rial / rialsPerToman,
		})
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	// Re-sorted in Go for the same reason loadDailySeries re-sorts: the
	// arithmetic downstream depends on ascending order, and that dependency
	// should be a property of this function rather than a promise from an
	// ORDER BY no unit test can execute.
	for symbol := range out {
		s := out[symbol]
		sort.SliceStable(s, func(i, j int) bool { return s[i].Day.Before(s[j].Day) })
		out[symbol] = s
	}
	return out, nil
}

// equityRosterRow is one row of equityRosterSelect, before any decision.
type equityRosterRow struct {
	Symbol, NameFA, SectorFA string
	Enabled                  bool
	BarCount                 int
	Status, Refusal, Version string
}

// equityEligibility decides whether one instrument may be measured here, and
// builds either its registry row or its exclusion. Pure function (unit tested):
// no database, so the gate that keeps unvalidated adjustments out of the table
// can be exercised directly rather than only through a live roster.
//
// The order of the cases is the order of the questions: is it switched on, does
// it have data, has an adjustment ever run, did that adjustment pass. Each
// produces a DIFFERENT sentence, because "disabled" and "refused" are different
// facts and collapsing them would tell the reader nothing they can act on.
func equityEligibility(r equityRosterRow) (instrumentRow, equityExclusion, bool) {
	switch {
	case !r.Enabled:
		return instrumentRow{}, equityExclusion{r.Symbol, fmt.Sprintf(
			"%s is disabled in the equity roster, so it is not measured here.",
			r.Symbol)}, false
	case r.BarCount == 0:
		return instrumentRow{}, equityExclusion{r.Symbol, fmt.Sprintf(
			"%s is registered but carries no stored bar, so there is nothing to "+
				"measure. That is a real state, reported rather than hidden.",
			r.Symbol)}, false
	case r.Status == "":
		return instrumentRow{}, equityExclusion{r.Symbol, fmt.Sprintf(
			"no corporate-action adjustment has ever been computed for %s, and every "+
				"return here is taken from adjusted closes. Raw Tehran closes would "+
				"rank it by how recently it did a capital increase.", r.Symbol)}, false
	case r.Status != equityStatusValidated:
		reason := fmt.Sprintf(
			"the adjustment validation gate refused %s (status %q), and every return "+
				"here is taken from adjusted closes. A missed corporate action corrupts "+
				"the whole series, not only the day it landed on.", r.Symbol, r.Status)
		if r.Refusal != "" {
			reason += " " + r.Refusal
		}
		return instrumentRow{}, equityExclusion{r.Symbol, reason}, false
	}

	return instrumentRow{
		Code:   r.Symbol,
		Kind:   "market_price",
		NameEN: r.Symbol,
		NameFA: r.NameFA,
		Domain: equityDomain,
		// IRT because loadEquitySeries divides these closes into toman before
		// they leave. If that division is ever removed, THIS is the line that
		// becomes a lie.
		QuoteCurrency: quoteIRT,
		Unit:          "share",
		Decimals:      0,
		QualityTier:   "official_mirror",
		// Not a proxy: these are the exchange's own prints, mirrored. Derived,
		// because the close served is the RAW close multiplied by a cumulative
		// corporate-action factor this platform computed -- a reader comparing
		// it with a TSETMC screen must know why the numbers differ.
		IsProxy:   false,
		IsDerived: true,
		Enabled:   true,
		Source:    sourceEquityBars,
		Notes: fmt.Sprintf(
			"Tehran Stock Exchange, %s. Adjusted closes (%s), quoted here in TOMAN -- "+
				"TSETMC publishes rials, and this platform is toman-canonical. Adjusted "+
				"and raw series differ enormously over long windows: فولاد is x1.52 raw "+
				"against x907.86 adjusted over nineteen years, because capital increases "+
				"are not price moves.", r.SectorFA, r.Version),
	}, equityExclusion{}, true
}
