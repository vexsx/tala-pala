package alerts

import (
	"testing"
	"time"
)

var now = time.Date(2026, 7, 20, 12, 0, 0, 0, time.UTC)

func baseSnapshot() Snapshot {
	premium := 3.0
	vol := 30.0
	return Snapshot{
		Now:              now,
		Gold:             &PricePoint{Value: 5_000_000, ObservedAt: now.Add(-5 * time.Minute)},
		LatestSignal:     &SignalInfo{Signal: "buy", Confidence: 0.7, GeneratedAt: now.Add(-time.Hour)},
		PreviousSignal:   &SignalInfo{Signal: "buy", Confidence: 0.6, GeneratedAt: now.Add(-2 * time.Hour)},
		PremiumPct:       &premium,
		VolatilityAnnPct: &vol,
		StaleMinutes:     30,
		MarketOpen:       "09:00",
		MarketClose:      "20:00",
		Providers: []ProviderHealth{
			{Code: "tgju", Enabled: true, ConsecutiveFailures: 0},
			{Code: "yahoo", Enabled: true, ConsecutiveFailures: 1},
		},
		Models: []ModelPerf{
			{Horizon: "1d", ModelName: "gbr", SMAPE: 1.0, BaselineSMAPE: 1.5},
		},
	}
}

func alertOf(typ string, cond map[string]any) Alert {
	return Alert{ID: 1, UserID: "u1", Type: typ, Condition: cond, Enabled: true, CooldownMinutes: 60}
}

func TestPriceAbove(t *testing.T) {
	s := baseSnapshot()
	if !Evaluate(alertOf("price_above", map[string]any{"threshold": 4_900_000.0}), s).Triggered {
		t.Fatal("should trigger above threshold")
	}
	if Evaluate(alertOf("price_above", map[string]any{"threshold": 5_100_000.0}), s).Triggered {
		t.Fatal("should not trigger below threshold")
	}
	// Missing price data: no trigger.
	s.Gold = nil
	if Evaluate(alertOf("price_above", map[string]any{"threshold": 1.0}), s).Triggered {
		t.Fatal("no data should not trigger price_above")
	}
}

func TestPriceBelow(t *testing.T) {
	s := baseSnapshot()
	if !Evaluate(alertOf("price_below", map[string]any{"threshold": 5_100_000.0}), s).Triggered {
		t.Fatal("should trigger below threshold")
	}
	if Evaluate(alertOf("price_below", map[string]any{"threshold": 4_900_000.0}), s).Triggered {
		t.Fatal("should not trigger above threshold")
	}
}

func TestSignalChange(t *testing.T) {
	s := baseSnapshot()
	if Evaluate(alertOf("signal_change", nil), s).Triggered {
		t.Fatal("same signal should not trigger")
	}
	s.PreviousSignal = &SignalInfo{Signal: "hold", GeneratedAt: now.Add(-2 * time.Hour)}
	res := Evaluate(alertOf("signal_change", nil), s)
	if !res.Triggered {
		t.Fatal("changed signal should trigger")
	}
	if res.Payload["from"] != "hold" || res.Payload["to"] != "buy" {
		t.Fatalf("bad payload: %v", res.Payload)
	}
	// Only one signal ever: no trigger.
	s.PreviousSignal = nil
	if Evaluate(alertOf("signal_change", nil), s).Triggered {
		t.Fatal("single signal should not trigger")
	}
}

func TestConfidenceAbove(t *testing.T) {
	s := baseSnapshot()
	if !Evaluate(alertOf("confidence_above", map[string]any{"threshold": 0.65}), s).Triggered {
		t.Fatal("0.7 > 0.65 should trigger")
	}
	if Evaluate(alertOf("confidence_above", map[string]any{"threshold": 0.75}), s).Triggered {
		t.Fatal("0.7 < 0.75 should not trigger")
	}
	// Default threshold 0.8.
	if Evaluate(alertOf("confidence_above", nil), s).Triggered {
		t.Fatal("default threshold 0.8 should not trigger at 0.7")
	}
}

func TestVolatilitySpike(t *testing.T) {
	s := baseSnapshot()
	if !Evaluate(alertOf("volatility_spike", map[string]any{"threshold": 25.0}), s).Triggered {
		t.Fatal("30 > 25 should trigger")
	}
	if Evaluate(alertOf("volatility_spike", map[string]any{"threshold": 40.0}), s).Triggered {
		t.Fatal("30 < 40 should not trigger")
	}
	s.VolatilityAnnPct = nil
	if Evaluate(alertOf("volatility_spike", map[string]any{"threshold": 1.0}), s).Triggered {
		t.Fatal("missing volatility should not trigger")
	}
}

func TestPremiumAbove(t *testing.T) {
	s := baseSnapshot()
	if !Evaluate(alertOf("premium_above", map[string]any{"threshold": 2.0}), s).Triggered {
		t.Fatal("3 > 2 should trigger")
	}
	if Evaluate(alertOf("premium_above", map[string]any{"threshold": 5.0}), s).Triggered {
		t.Fatal("3 < 5 should not trigger")
	}
}

func TestStaleData(t *testing.T) {
	s := baseSnapshot()
	if Evaluate(alertOf("stale_data", nil), s).Triggered {
		t.Fatal("fresh data should not trigger")
	}
	s.Gold = &PricePoint{Value: 5_000_000, ObservedAt: now.Add(-45 * time.Minute)}
	if !Evaluate(alertOf("stale_data", nil), s).Triggered {
		t.Fatal("45min old data should trigger at default 30min")
	}
	// Custom threshold overrides the default.
	if Evaluate(alertOf("stale_data", map[string]any{"threshold_minutes": 60.0}), s).Triggered {
		t.Fatal("45min old should not trigger at 60min threshold")
	}
	// No data at all always triggers.
	s.Gold = nil
	if !Evaluate(alertOf("stale_data", nil), s).Triggered {
		t.Fatal("missing data should trigger stale_data")
	}
}

func TestStaleData_MarketClosed(t *testing.T) {
	// Monday 2026-07-20 18:00 UTC = 21:30 Tehran: market closed since
	// 20:00 Tehran (16:30 UTC). `now` above (12:00 UTC = 15:30 Tehran) is open.
	closedNow := time.Date(2026, 7, 20, 18, 0, 0, 0, time.UTC)
	s := baseSnapshot()
	s.Now = closedNow

	// Last-session data (16:20 UTC, 100 minutes old) must NOT trigger:
	// no nightly false alarms while the market is closed.
	s.Gold = &PricePoint{Value: 5_000_000,
		ObservedAt: time.Date(2026, 7, 20, 16, 20, 0, 0, time.UTC)}
	if Evaluate(alertOf("stale_data", nil), s).Triggered {
		t.Fatal("last-session data must not trigger stale_data while closed")
	}

	// Data from before (closure start - 30m) = 16:00 UTC still triggers.
	s.Gold = &PricePoint{Value: 5_000_000,
		ObservedAt: time.Date(2026, 7, 20, 10, 0, 0, 0, time.UTC)}
	res := Evaluate(alertOf("stale_data", nil), s)
	if !res.Triggered {
		t.Fatal("pre-session data should trigger stale_data even while closed")
	}
	if res.Payload["market_state"] != "closed" {
		t.Fatalf("payload market_state = %v, want closed", res.Payload["market_state"])
	}
}

func TestProviderFailure(t *testing.T) {
	s := baseSnapshot()
	if Evaluate(alertOf("provider_failure", nil), s).Triggered {
		t.Fatal("healthy providers should not trigger (default threshold 3)")
	}
	s.Providers = append(s.Providers, ProviderHealth{
		Code: "stooq", Enabled: true, ConsecutiveFailures: 5, LastError: "timeout"})
	res := Evaluate(alertOf("provider_failure", nil), s)
	if !res.Triggered {
		t.Fatal("5 consecutive failures should trigger")
	}
	// Disabled providers are ignored.
	s.Providers = []ProviderHealth{{Code: "x", Enabled: false, ConsecutiveFailures: 99}}
	if Evaluate(alertOf("provider_failure", nil), s).Triggered {
		t.Fatal("disabled provider should not trigger")
	}
	// Custom threshold.
	s = baseSnapshot() // yahoo has 1 failure
	if !Evaluate(alertOf("provider_failure", map[string]any{"threshold": 1.0}), s).Triggered {
		t.Fatal("threshold 1 should trigger on yahoo")
	}
}

func TestModelDegradation(t *testing.T) {
	s := baseSnapshot()
	if Evaluate(alertOf("model_degradation", nil), s).Triggered {
		t.Fatal("model beating baseline should not trigger")
	}
	s.Models = []ModelPerf{{Horizon: "1d", ModelName: "gbr", SMAPE: 2.0, BaselineSMAPE: 1.5}}
	if !Evaluate(alertOf("model_degradation", nil), s).Triggered {
		t.Fatal("model worse than baseline should trigger")
	}
	// Tolerance suppresses small degradation.
	if Evaluate(alertOf("model_degradation", map[string]any{"tolerance_pct": 50.0}), s).Triggered {
		t.Fatal("within 50% tolerance should not trigger (2.0 <= 1.5*1.5)")
	}
	// Zero baseline: undefined, never triggers.
	s.Models = []ModelPerf{{Horizon: "1d", SMAPE: 9, BaselineSMAPE: 0}}
	if Evaluate(alertOf("model_degradation", nil), s).Triggered {
		t.Fatal("zero baseline should not trigger")
	}
}

func TestUnknownTypeNeverTriggers(t *testing.T) {
	if Evaluate(alertOf("bogus_type", nil), baseSnapshot()).Triggered {
		t.Fatal("unknown alert type triggered")
	}
}

func TestCooldown(t *testing.T) {
	if !CooldownOK(nil, 60, now) {
		t.Fatal("never-triggered alert must pass cooldown")
	}
	recent := now.Add(-30 * time.Minute)
	if CooldownOK(&recent, 60, now) {
		t.Fatal("30min ago with 60min cooldown must be blocked")
	}
	old := now.Add(-61 * time.Minute)
	if !CooldownOK(&old, 60, now) {
		t.Fatal("61min ago with 60min cooldown must pass")
	}
	exact := now.Add(-60 * time.Minute)
	if !CooldownOK(&exact, 60, now) {
		t.Fatal("exactly at cooldown boundary must pass")
	}
}
