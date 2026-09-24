package alerts

import (
	"context"
	"encoding/json"
	"log/slog"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/indicators"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/obs"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/prices"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/storage"
)

// alertSignalSymbol is the asset the alert snapshot's signal fields describe.
//
// Every alert this runner can evaluate is a gold alert: Gold, PremiumPct and
// VolatilityAnnPct in the Snapshot are all IR_GOLD_18K (see the queries below),
// and signal_change / confidence_above are read on the same page beside them.
// Naming the symbol once keeps the snapshot internally consistent instead of
// leaving one field to whatever the signal job wrote most recently.
const alertSignalSymbol = "IR_GOLD_18K"

// alertSignalSelect is the snapshot's signal read. It is a constant so that a
// test can assert the symbol filter is present without a database -- the same
// guard signalsvc puts on its own statements, because an unfiltered "latest
// row" is precisely the bug migration 0026 makes possible.
const alertSignalSelect = `
	SELECT signal, confidence, generated_at
	  FROM signals WHERE symbol = $1
	 ORDER BY generated_at DESC LIMIT 2`

// Runner loads the shared snapshot, evaluates every enabled alert and writes
// alert_events. It is invoked by the scheduler.
type Runner struct {
	Pool                *pgxpool.Pool
	Log                 *slog.Logger
	StaleMinutesDefault int
	// Tehran session bounds ("HH:MM") for market-hours-aware staleness.
	MarketOpen  string
	MarketClose string
}

// Run performs one evaluation pass. Returns the number of events created.
func (rn *Runner) Run(ctx context.Context) (int, error) {
	snap, err := rn.loadSnapshot(ctx)
	if err != nil {
		return 0, err
	}

	rows, err := rn.Pool.Query(ctx, `
		SELECT id, user_id, alert_type, condition, cooldown_minutes, last_triggered_at
		FROM alerts WHERE enabled`)
	if err != nil {
		return 0, err
	}
	var list []Alert
	for rows.Next() {
		var a Alert
		var cond []byte
		if err := rows.Scan(&a.ID, &a.UserID, &a.Type, &cond, &a.CooldownMinutes, &a.LastTriggeredAt); err != nil {
			rows.Close()
			return 0, err
		}
		a.Enabled = true
		_ = json.Unmarshal(cond, &a.Condition)
		list = append(list, a)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return 0, err
	}

	events := 0
	for _, a := range list {
		if !CooldownOK(a.LastTriggeredAt, a.CooldownMinutes, snap.Now) {
			continue
		}
		res := Evaluate(a, snap)
		if !res.Triggered {
			continue
		}
		payload, err := json.Marshal(res.Payload)
		if err != nil {
			payload = []byte("{}")
		}
		tx, err := rn.Pool.Begin(ctx)
		if err != nil {
			return events, err
		}
		_, err = tx.Exec(ctx, `
			INSERT INTO alert_events (alert_id, user_id, triggered_at, message, payload)
			VALUES ($1, $2, $3, $4, $5)`,
			a.ID, a.UserID, snap.Now, res.Message, payload)
		if err == nil {
			_, err = tx.Exec(ctx,
				`UPDATE alerts SET last_triggered_at = $1, updated_at = now() WHERE id = $2`,
				snap.Now, a.ID)
		}
		if err != nil {
			_ = tx.Rollback(ctx)
			rn.Log.Error("alert_event_write", "alert_id", a.ID, "error", err)
			continue
		}
		if err := tx.Commit(ctx); err != nil {
			rn.Log.Error("alert_event_commit", "alert_id", a.ID, "error", err)
			continue
		}
		events++
	}
	return events, nil
}

// --- freshness of the manually refreshed data classes ------------------------
//
// This platform has two classes of data and only one of them looks after
// itself.
//
//	SELF-UPDATING  gold, FX, coin and the global series, collected every ten
//	               minutes by the collect cron on the server.
//	MANUAL         Tehran equity bars (TSETMC) and SCI CPI. Neither source is
//	               reachable from the production host — measured, not assumed:
//	               amar.org.ir accepts TCP on 443 and 80 and then answers
//	               nothing, and cdn.tsetmc.com fails TCP 443 outright with
//	               http=000 while answering fine from outside that network.
//	               So they are fetched elsewhere and pushed, by
//	               scripts/sci_fetch.py and scripts/tsetmc_fetch.py.
//
// The architecture is right. What was missing is that nothing SCHEDULES those
// runs and nothing NOTICED when they stopped: on 2026-09-24 the newest equity
// bar in production was 2026-09-09, fifteen days old, and the screener had
// been serving those prices under a window labelled with the current date the
// whole time. The only reason anyone knew is that a human went looking. The
// gauges below are what makes "the human stopped running the script" a
// monitorable event instead of a discovery.
//
// Both follow the GROUP BY shape UpdateFreshness already uses for prices, and
// for the same reason: a new symbol or a new provider produces a new series
// with no code change here, so widening the roster stays an INSERT.

// equityFreshnessSelect is the newest stored bar per ENABLED instrument.
//
// A constant so a test can assert its shape without a database. GROUP BY, not
// DISTINCT ON, to match the prices read above; the join is to
// equity_instruments because the label has to be the Persian trading symbol a
// human recognises and /api/v1/stocks resolves, not the opaque 17-digit
// TSETMC ins_code. max() over a group is never NULL, so there is no
// null-timestamp case to get wrong.
//
// WHERE i.enabled is load-bearing, and it is here to match the FETCH rather
// than the API. scripts/tsetmc_fetch.py takes its roster from
// app/economic/ingest.py roster(include_disabled=False), so a disabled
// instrument never receives another bar BY DESIGN. Without this filter its
// gauge would keep publishing a frozen timestamp, and 30 days later
// EquityInstrumentBarsStale would fire for it on every evaluation, forever,
// while the system is behaving exactly as configured -- the precise failure
// newestAcross below refuses to create, for the reason given there.
//
// This is latent rather than live: the only disabled instrument in production
// today (کچاد, seeded disabled by migration 0028) has never been ingested, so
// the inner join drops it anyway. It bites the moment anyone disables an
// instrument that already HAS history, which is the ordinary way this switch
// gets used. Note the API deliberately does the opposite and keeps disabled
// instruments visible (internal/equities/handlers.go): listing an instrument
// and expecting the fetch to refresh it are different questions.
const equityFreshnessSelect = `
	SELECT i.symbol_fa, max(b.trade_date)
	  FROM equity_bars b
	  JOIN equity_instruments i ON i.ins_code = b.ins_code
	 WHERE i.enabled
	 GROUP BY i.symbol_fa`

// economicFreshnessSelect is the newest observation per provider, by
// available_at.
//
// available_at, NOT ref_period_end. "What period does this describe" and "when
// did we last learn something" are different questions and only the second one
// is about staleness: SCI's Mordad 1405 print describes a period that ended
// 2026-08-22 and reached us weeks later, so a ref_period_end gauge reports a
// perfectly healthy pipeline as permanently weeks behind — and therefore
// cannot distinguish it from a dead one. migration 0024 says the same thing
// about this column: "every point-in-time read filters on this column and
// nothing else".
//
// Grouped by provider rather than by series: a provider is what a refresh run
// covers, so it is the unit at which a refresh can stop.
const economicFreshnessSelect = `
	SELECT s.provider_code, max(o.available_at)
	  FROM economic_observations o
	  JOIN economic_series s ON s.id = o.series_id
	 GROUP BY s.provider_code`

// freshnessRow is one (label, newest instant) pair from a GROUP BY read.
type freshnessRow struct {
	Label  string
	Newest time.Time
}

// newestAcross reduces per-label freshness to the newest instant across all of
// them, reporting false when there are no rows at all.
//
// The bool is the whole point and is why this is a function rather than a
// max() in a loop. An empty equity_bars table must produce NO gauge: the zero
// time would be published as a Unix timestamp of -6795364578 or, after a
// float64 round trip through a Gauge, as something equally meaningless, and
// every staleness rule reading it would compute an age of decades and fire
// permanently on a fresh deployment that has simply never ingested. A rule
// that fires forever on a correct system is worse than no rule, because it
// teaches the operator to ignore the one that matters. Self-gating — no row,
// no series, no alert — is the same property the news and economic rules in
// observability/alerts.yml rely on.
func newestAcross(rows []freshnessRow) (time.Time, bool) {
	var newest time.Time
	found := false
	for _, r := range rows {
		if r.Newest.IsZero() {
			// A zero instant is not data. Skipping it here rather than at the
			// call site keeps "the table is empty" and "the table holds a
			// zero" on the same, silent path.
			continue
		}
		if !found || r.Newest.After(newest) {
			newest, found = r.Newest, true
		}
	}
	return newest, found
}

// applyFreshness publishes one gauge per row and, when asked, the aggregate
// across them.
//
// Pure over (gauges, rows): no clock, no database. It exists so the
// no-row-no-gauge rule can be asserted directly — the test collects the
// registry after calling this with an empty slice and requires zero series.
func applyFreshness(per *obs.DualGaugeVec, roster *obs.DualGaugeVec, rows []freshnessRow) {
	for _, r := range rows {
		if r.Newest.IsZero() {
			continue
		}
		per.WithLabelValues(r.Label).Set(float64(r.Newest.Unix()))
	}
	if roster == nil {
		return
	}
	if newest, ok := newestAcross(rows); ok {
		// Touched only inside the ok branch: an untouched zero-label Vec
		// publishes nothing, which is what keeps the rule self-gating.
		roster.WithLabelValues().Set(float64(newest.Unix()))
	}
}

// scanFreshness runs one of the GROUP BY reads above.
func (rn *Runner) scanFreshness(ctx context.Context, query string) ([]freshnessRow, error) {
	rows, err := rn.Pool.Query(ctx, query)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []freshnessRow{}
	for rows.Next() {
		var r freshnessRow
		if err := rows.Scan(&r.Label, &r.Newest); err != nil {
			return nil, err
		}
		r.Newest = r.Newest.UTC()
		out = append(out, r)
	}
	return out, rows.Err()
}

// UpdateFreshness refreshes the prediction/price freshness gauges (the
// Go-side prediction-freshness job, run alongside alert evaluation).
func (rn *Runner) UpdateFreshness(ctx context.Context, m *obs.Metrics) error {
	rows, err := rn.Pool.Query(ctx, `
		SELECT symbol, max(observed_at) FROM prices GROUP BY symbol`)
	if err != nil {
		return err
	}
	for rows.Next() {
		var sym string
		var ts time.Time
		if err := rows.Scan(&sym, &ts); err != nil {
			rows.Close()
			return err
		}
		m.LastPriceTimestamp.WithLabelValues(sym).Set(float64(ts.Unix()))
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return err
	}

	predRows, err := rn.Pool.Query(ctx, `
		SELECT horizon, max(predicted_at) FROM predictions GROUP BY horizon`)
	if err != nil {
		return err
	}
	for predRows.Next() {
		var hz string
		var ts time.Time
		if err := predRows.Scan(&hz, &ts); err != nil {
			predRows.Close()
			return err
		}
		m.LastPredictionTimestamp.WithLabelValues(hz).Set(float64(ts.Unix()))
	}
	predRows.Close()
	if err := predRows.Err(); err != nil {
		return err
	}

	// The two manually refreshed classes. They are read here, in the job that
	// already re-reads freshness from Postgres every five minutes, rather than
	// being set by whatever ingested them: an ingest-time gauge only exists
	// while the process that wrote it lives, and the thing being monitored
	// here is precisely an ingest that is NOT happening.
	equity, err := rn.scanFreshness(ctx, equityFreshnessSelect)
	if err != nil {
		return err
	}
	applyFreshness(m.LastEquityBarTimestamp, m.LastEquityBarRosterTimestamp, equity)

	economic, err := rn.scanFreshness(ctx, economicFreshnessSelect)
	if err != nil {
		return err
	}
	applyFreshness(m.LastEconomicObservation, nil, economic)
	return nil
}

func (rn *Runner) loadSnapshot(ctx context.Context) (Snapshot, error) {
	now := time.Now().UTC()
	snap := Snapshot{
		Now:          now,
		StaleMinutes: storage.GetStaleMinutes(ctx, rn.Pool, rn.StaleMinutesDefault),
		MarketOpen:   rn.MarketOpen,
		MarketClose:  rn.MarketClose,
	}

	// Latest gold / xau / usd prices.
	latest := map[string]PricePoint{}
	rows, err := rn.Pool.Query(ctx, `
		SELECT DISTINCT ON (symbol) symbol, value::float8, observed_at
		FROM prices
		WHERE quality = 'ok' AND symbol IN ('IR_GOLD_18K','XAUUSD','USD_IRT')
		ORDER BY symbol, observed_at DESC`)
	if err != nil {
		return snap, err
	}
	for rows.Next() {
		var sym string
		var p PricePoint
		if err := rows.Scan(&sym, &p.Value, &p.ObservedAt); err != nil {
			rows.Close()
			return snap, err
		}
		p.ObservedAt = p.ObservedAt.UTC()
		latest[sym] = p
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return snap, err
	}
	if g, ok := latest["IR_GOLD_18K"]; ok {
		snap.Gold = &g
	}
	if x, ok := latest["XAUUSD"]; ok {
		if u, ok2 := latest["USD_IRT"]; ok2 && snap.Gold != nil {
			theo := prices.Theoretical18kIRT(x.Value, u.Value)
			p := prices.PremiumPct(snap.Gold.Value, theo)
			snap.PremiumPct = &p
		}
	}

	// The last two IR_GOLD_18K signals, for signal_change / confidence_above.
	//
	// THE SYMBOL FILTER IS LOAD-BEARING, and its absence was worse here than
	// anywhere else in this codebase. `signals` held one row per pass until
	// migration 0026; the engine now writes seven rows per pass, one per symbol
	// (prediction-python/app/signals/universe.py). An unfiltered "newest two
	// rows" therefore returns two DIFFERENT ASSETS from the SAME pass, so
	// LatestSignal and PreviousSignal stop being consecutive readings of one
	// instrument. Evaluate()'s signal_change case compares them for inequality:
	// silver "sell" against gold "hold" is not a change of view, it is two
	// unrelated instruments, and it would fire "Signal changed: hold -> sell"
	// at every subscriber on every pass where any two symbols disagree.
	// confidence_above is the same defect one row shallower -- it would gate on
	// whichever asset was scored last.
	//
	// Same reasoning as Addendum 13's `_load_latest_predictions` fix: XAUUSD
	// rows written after the gold rows every cycle overwrote the per-horizon
	// map, and the Tehran signal was scored from global-gold forecasts. Rows
	// from different assets are not interchangeable just because they share a
	// table and a clock; the query has to say which asset it means.
	sigRows, err := rn.Pool.Query(ctx, alertSignalSelect, alertSignalSymbol)
	if err != nil {
		return snap, err
	}
	var sigs []SignalInfo
	for sigRows.Next() {
		var s SignalInfo
		if err := sigRows.Scan(&s.Signal, &s.Confidence, &s.GeneratedAt); err != nil {
			sigRows.Close()
			return snap, err
		}
		s.GeneratedAt = s.GeneratedAt.UTC()
		sigs = append(sigs, s)
	}
	sigRows.Close()
	if len(sigs) > 0 {
		snap.LatestSignal = &sigs[0]
	}
	if len(sigs) > 1 {
		snap.PreviousSignal = &sigs[1]
	}

	// Rolling annualized volatility from daily closes (last ~40 days).
	closeRows, err := rn.Pool.Query(ctx, `
		SELECT (array_agg(value ORDER BY observed_at DESC))[1]::float8
		FROM prices
		WHERE symbol = 'IR_GOLD_18K' AND quality = 'ok' AND observed_at >= $1
		GROUP BY date_trunc('day', observed_at)
		ORDER BY date_trunc('day', observed_at) ASC`, now.AddDate(0, 0, -40))
	if err != nil {
		return snap, err
	}
	var closes []float64
	for closeRows.Next() {
		var v float64
		if err := closeRows.Scan(&v); err != nil {
			closeRows.Close()
			return snap, err
		}
		closes = append(closes, v)
	}
	closeRows.Close()
	if vol := indicators.Last(indicators.Volatility(closes, 20)); vol == vol { // not NaN
		pct := vol * 100
		snap.VolatilityAnnPct = &pct
	}

	// Provider health.
	provRows, err := rn.Pool.Query(ctx, `
		SELECT code, enabled, consecutive_failures, COALESCE(last_error, '')
		FROM data_providers`)
	if err != nil {
		return snap, err
	}
	for provRows.Next() {
		var p ProviderHealth
		if err := provRows.Scan(&p.Code, &p.Enabled, &p.ConsecutiveFailures, &p.LastError); err != nil {
			provRows.Close()
			return snap, err
		}
		snap.Providers = append(snap.Providers, p)
	}
	provRows.Close()

	// Active models vs baseline (smape from metrics JSON).
	modelRows, err := rn.Pool.Query(ctx, `
		SELECT horizon, model_name,
		       COALESCE((metrics->>'smape')::float8, 0),
		       COALESCE((baseline_metrics->>'smape')::float8, 0)
		FROM model_versions WHERE is_active`)
	if err != nil {
		return snap, err
	}
	for modelRows.Next() {
		var m ModelPerf
		if err := modelRows.Scan(&m.Horizon, &m.ModelName, &m.SMAPE, &m.BaselineSMAPE); err != nil {
			modelRows.Close()
			return snap, err
		}
		snap.Models = append(snap.Models, m)
	}
	modelRows.Close()

	return snap, nil
}
