package alerts

import (
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/obs"
)

// These cover the gauges that make the MANUALLY REFRESHED data classes
// monitorable. The failure they exist to prevent was measured on production
// 2026-09-24: the newest equity bar was 2026-09-09 and the newest SCI CPI
// print described a period that had ended over a month earlier, and neither
// fact reached anything that could raise it.

// --- the queries -------------------------------------------------------------

func TestFreshnessReadsAreGroupedSoANewSymbolNeedsNoCodeChange(t *testing.T) {
	// The property worth keeping from the prices read this was modelled on:
	// "SELECT symbol, max(observed_at) FROM prices GROUP BY symbol" grows a
	// new series when a new symbol appears, with no edit here. The equity
	// roster is ~700 companies and is meant to grow, and the provider list
	// gains a row whenever a new source is added, so both reads have to have
	// that shape rather than naming their labels.
	for name, q := range map[string]string{
		"equity":   equityFreshnessSelect,
		"economic": economicFreshnessSelect,
	} {
		if !strings.Contains(q, "GROUP BY") || !strings.Contains(q, "max(") {
			t.Errorf("%s freshness read is not a GROUP BY over a max():\n%s", name, q)
		}
	}

	if !strings.Contains(equityFreshnessSelect, "FROM equity_bars") ||
		!strings.Contains(equityFreshnessSelect, "max(b.trade_date)") {
		t.Errorf("the equity gauge must read the newest stored bar:\n%s", equityFreshnessSelect)
	}
	// Labelled by the Persian trading symbol, not by TSETMC's opaque 17-digit
	// ins_code: the label is read by a human deciding whether to go run the
	// fetch script.
	if !strings.Contains(equityFreshnessSelect, "i.symbol_fa") {
		t.Errorf("the equity gauge must be labelled by symbol:\n%s", equityFreshnessSelect)
	}
	// Scoped to enabled instruments, because the gauge has to match the FETCH
	// and not the API. scripts/tsetmc_fetch.py takes its roster from
	// roster(include_disabled=False), so a disabled instrument never gets
	// another bar by design; without this filter its gauge freezes and
	// EquityInstrumentBarsStale fires for it forever on a correctly operating
	// system. Pinned here because the join alone hides it: the only disabled
	// instrument in production has never been ingested, so an inner join drops
	// it today and the bug would not show up until someone disables a symbol
	// that already has history.
	if !strings.Contains(equityFreshnessSelect, "WHERE i.enabled") {
		t.Errorf("the equity gauge must ignore disabled instruments, or it alerts "+
			"forever on one that was switched off on purpose:\n%s", equityFreshnessSelect)
	}
}

func TestEconomicFreshnessMeasuresWhenWeLearnedNotWhatPeriodItDescribes(t *testing.T) {
	// The whole correctness of this gauge. SCI's Mordad 1405 workbook
	// describes a period that ended 2026-08-22 and was published weeks after
	// it; a gauge on ref_period_end therefore reports a perfectly healthy
	// pipeline as permanently weeks behind, which makes it unable to
	// distinguish one from a pipeline that has stopped. available_at is when
	// this system could first have known the value, which is the question a
	// staleness rule is actually asking.
	if !strings.Contains(economicFreshnessSelect, "max(o.available_at)") {
		t.Errorf("the economic gauge must be stamped with available_at:\n%s",
			economicFreshnessSelect)
	}
	if strings.Contains(economicFreshnessSelect, "ref_period_end") ||
		strings.Contains(economicFreshnessSelect, "ref_period_start") {
		t.Errorf("the economic gauge must NOT be stamped with a reference period:\n%s",
			economicFreshnessSelect)
	}
	// Per provider, because a provider is what one refresh run covers and
	// therefore the unit at which a refresh can stop.
	if !strings.Contains(economicFreshnessSelect, "s.provider_code") ||
		!strings.Contains(economicFreshnessSelect, "GROUP BY s.provider_code") {
		t.Errorf("the economic gauge must be grouped by provider:\n%s",
			economicFreshnessSelect)
	}
}

// --- the shaping -------------------------------------------------------------

func TestNewestAcrossReportsAbsenceRatherThanAZeroTime(t *testing.T) {
	// The reason this returns a bool at all. A zero time.Time published as a
	// Unix timestamp is a number from the year 1 that every staleness rule
	// reads as an age of two millennia, so a gauge carrying it fires
	// permanently on a deployment that has simply never ingested an equity
	// bar. A rule that fires forever on a correct system trains the operator
	// to ignore the one that matters.
	if _, ok := newestAcross(nil); ok {
		t.Error("no rows must report no timestamp")
	}
	if _, ok := newestAcross([]freshnessRow{}); ok {
		t.Error("an empty slice must report no timestamp")
	}
	if _, ok := newestAcross([]freshnessRow{{Label: "فولاد"}}); ok {
		t.Error("a zero instant is not data and must not be reported as the newest")
	}
}

func TestNewestAcrossTakesTheNewestWhicheverOrderTheRowsArrive(t *testing.T) {
	sep9 := time.Date(2026, time.September, 9, 0, 0, 0, 0, time.UTC)
	aug20 := time.Date(2026, time.August, 20, 0, 0, 0, 0, time.UTC)
	rows := []freshnessRow{
		{Label: "خودرو", Newest: aug20},
		{Label: "فولاد", Newest: sep9},
		{Label: "کهربا"}, // registered, never ingested
	}
	got, ok := newestAcross(rows)
	if !ok {
		t.Fatal("rows with real timestamps must report one")
	}
	if !got.Equal(sep9) {
		t.Errorf("newest = %s, want %s", got, sep9)
	}
}

// --- the gauges --------------------------------------------------------------

// seriesCount counts the published time series for one metric name. Zero means
// the metric exists in the binary but publishes nothing, which is exactly the
// state a self-gating alert rule needs.
func seriesCount(t *testing.T, m *obs.Metrics, name string) int {
	t.Helper()
	families, err := m.Registry.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() == name {
			return len(f.GetMetric())
		}
	}
	return 0
}

func gaugeValue(t *testing.T, m *obs.Metrics, name string) float64 {
	t.Helper()
	families, err := m.Registry.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, f := range families {
		if f.GetName() != name {
			continue
		}
		if len(f.GetMetric()) != 1 {
			t.Fatalf("%s has %d series, want exactly 1", name, len(f.GetMetric()))
		}
		return f.GetMetric()[0].GetGauge().GetValue()
	}
	t.Fatalf("%s publishes no series", name)
	return 0
}

const (
	equityPerSymbolMetric = "talapala_api_last_equity_bar_timestamp_seconds"
	equityRosterMetric    = "talapala_api_last_equity_bar_roster_timestamp_seconds"
	economicMetric        = "talapala_api_last_economic_observation_timestamp_seconds"
)

func TestAnEmptyTableProducesNoGaugeRatherThanNineteenSeventy(t *testing.T) {
	// The single most important property of these gauges, and the one a
	// registered plain Gauge cannot have: it would publish 0 from process
	// start, 0 is the Unix epoch, and `time() - 0` is fifty-six years. Every
	// rule in observability/alerts.yml reading it would fire within one
	// evaluation interval of a fresh deployment coming up — before anybody had
	// a chance to ingest anything, and with nothing wrong.
	m := obs.NewMetrics()
	applyFreshness(m.LastEquityBarTimestamp, m.LastEquityBarRosterTimestamp, nil)
	applyFreshness(m.LastEconomicObservation, nil, nil)

	for _, name := range []string{equityPerSymbolMetric, equityRosterMetric, economicMetric} {
		if n := seriesCount(t, m, name); n != 0 {
			t.Errorf("%s publishes %d series on an empty table, want 0 so the alert "+
				"stays self-gating", name, n)
		}
	}
}

func TestGaugesCarryTheNewestBarPerSymbolAndAcrossTheRoster(t *testing.T) {
	sep9 := time.Date(2026, time.September, 9, 0, 0, 0, 0, time.UTC)
	aug20 := time.Date(2026, time.August, 20, 0, 0, 0, 0, time.UTC)

	m := obs.NewMetrics()
	applyFreshness(m.LastEquityBarTimestamp, m.LastEquityBarRosterTimestamp,
		[]freshnessRow{{Label: "فولاد", Newest: sep9}, {Label: "خودرو", Newest: aug20}})

	if n := seriesCount(t, m, equityPerSymbolMetric); n != 2 {
		t.Errorf("per-symbol gauge has %d series, want one per instrument", n)
	}
	// One series, no labels: the rule that pages fires once for "the equity
	// refresh stopped" instead of once per company. With ~700 companies in the
	// roster, alerting on the per-symbol gauge would turn one missed run into
	// 700 notifications describing the same event.
	if n := seriesCount(t, m, equityRosterMetric); n != 1 {
		t.Errorf("roster gauge has %d series, want exactly 1", n)
	}
	if got := gaugeValue(t, m, equityRosterMetric); got != float64(sep9.Unix()) {
		t.Errorf("roster gauge = %v, want the newest bar across the roster (%d)",
			got, sep9.Unix())
	}
}

func TestBothExportsMoveTogether(t *testing.T) {
	// The Dual* contract from internal/obs: the deprecated goldpred_* twin is
	// written by the same call, so a dashboard still on the old name does not
	// silently sit at a value nothing updates.
	sep9 := time.Date(2026, time.September, 9, 0, 0, 0, 0, time.UTC)
	m := obs.NewMetrics()
	applyFreshness(m.LastEquityBarTimestamp, m.LastEquityBarRosterTimestamp,
		[]freshnessRow{{Label: "فولاد", Newest: sep9}})

	current := gaugeValue(t, m, equityRosterMetric)
	deprecated := gaugeValue(t, m, "goldpred_api_last_equity_bar_roster_timestamp_seconds")
	if current != deprecated {
		t.Errorf("the two exports disagree: %v vs %v", current, deprecated)
	}
}

func TestEconomicGaugeIsOneSeriesPerProvider(t *testing.T) {
	// Three providers with genuinely different cadences share this metric: sci
	// publishes monthly, worldbank and imf_weo carry annual series that only
	// advance once a year. They are all exported — a new provider appears here
	// with no code change — and observability/alerts.yml alerts on sci ALONE,
	// because a bound that suits a monthly release would fire all year on an
	// annual one.
	now := time.Date(2026, time.September, 24, 3, 40, 0, 0, time.UTC)
	m := obs.NewMetrics()
	applyFreshness(m.LastEconomicObservation, nil, []freshnessRow{
		{Label: "sci", Newest: now.AddDate(0, 0, -20)},
		{Label: "worldbank", Newest: now.AddDate(0, -8, 0)},
		{Label: "imf_weo", Newest: now.AddDate(0, -5, 0)},
	})
	if n := seriesCount(t, m, economicMetric); n != 3 {
		t.Errorf("economic gauge has %d series, want one per provider", n)
	}
	// No roster aggregate for this one: max() across providers would let the
	// daily worldbank job keep the series looking fresh while SCI, the one
	// that needs a human, had stopped months earlier.
	if n := seriesCount(t, m, "talapala_api_last_economic_observation_roster_timestamp_seconds"); n != 0 {
		t.Errorf("there must be no cross-provider aggregate, found %d series", n)
	}
}
