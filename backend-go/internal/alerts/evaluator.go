// Package alerts implements alert CRUD, events and the Go-side evaluator run
// by the scheduler.
package alerts

import (
	"encoding/json"
	"fmt"
	"time"
)

// AlertTypes is the full set from CONTRACTS.md.
var AlertTypes = map[string]bool{
	"price_above": true, "price_below": true, "signal_change": true,
	"confidence_above": true, "volatility_spike": true, "premium_above": true,
	"stale_data": true, "provider_failure": true, "model_degradation": true,
}

// Alert is the evaluator's view of a configured alert.
type Alert struct {
	ID              int64
	UserID          string
	Type            string
	Condition       map[string]any
	Enabled         bool
	CooldownMinutes int
	LastTriggeredAt *time.Time
}

// PricePoint is the latest observation of a symbol.
type PricePoint struct {
	Value      float64
	ObservedAt time.Time
}

// SignalInfo summarises one signals row.
type SignalInfo struct {
	Signal      string
	Confidence  float64
	GeneratedAt time.Time
}

// ProviderHealth is the evaluator's view of a data provider.
type ProviderHealth struct {
	Code                string
	Enabled             bool
	ConsecutiveFailures int
	LastError           string
}

// ModelPerf compares an active model against its naive baseline.
type ModelPerf struct {
	Horizon       string
	ModelName     string
	SMAPE         float64 // walk-forward smape of the active model
	BaselineSMAPE float64 // naive baseline smape on the same folds
}

// Snapshot is everything the evaluator needs, loaded once per run and shared
// across all alerts. Pointer fields are nil when the data is unavailable.
type Snapshot struct {
	Now              time.Time
	Gold             *PricePoint // latest IR_GOLD_18K
	LatestSignal     *SignalInfo
	PreviousSignal   *SignalInfo
	PremiumPct       *float64
	VolatilityAnnPct *float64 // annualized volatility (percent, e.g. 45.0)
	StaleMinutes     int      // configured stale threshold
	Providers        []ProviderHealth
	Models           []ModelPerf
}

// Result of evaluating one alert.
type Result struct {
	Triggered bool
	Message   string
	Payload   map[string]any
}

// CooldownOK reports whether an alert may fire again at `now`.
// Pure function (unit tested).
func CooldownOK(lastTriggeredAt *time.Time, cooldownMinutes int, now time.Time) bool {
	if lastTriggeredAt == nil {
		return true
	}
	return now.Sub(*lastTriggeredAt) >= time.Duration(cooldownMinutes)*time.Minute
}

func condFloat(cond map[string]any, key string, def float64) float64 {
	if cond == nil {
		return def
	}
	switch v := cond[key].(type) {
	case float64:
		return v
	case int:
		return float64(v)
	case json.Number:
		if f, err := v.Float64(); err == nil {
			return f
		}
	}
	return def
}

// Evaluate decides whether a single alert triggers given the snapshot.
// Pure function (unit tested); cooldown is handled separately by the caller.
func Evaluate(a Alert, s Snapshot) Result {
	no := Result{}
	switch a.Type {
	case "price_above":
		threshold := condFloat(a.Condition, "threshold", 0)
		if s.Gold != nil && threshold > 0 && s.Gold.Value > threshold {
			return Result{true,
				fmt.Sprintf("18k gold price %.0f IRT is above your threshold %.0f IRT", s.Gold.Value, threshold),
				map[string]any{"price": s.Gold.Value, "threshold": threshold}}
		}
	case "price_below":
		threshold := condFloat(a.Condition, "threshold", 0)
		if s.Gold != nil && threshold > 0 && s.Gold.Value < threshold {
			return Result{true,
				fmt.Sprintf("18k gold price %.0f IRT is below your threshold %.0f IRT", s.Gold.Value, threshold),
				map[string]any{"price": s.Gold.Value, "threshold": threshold}}
		}
	case "signal_change":
		if s.LatestSignal != nil && s.PreviousSignal != nil &&
			s.LatestSignal.Signal != s.PreviousSignal.Signal {
			return Result{true,
				fmt.Sprintf("trading signal changed from %s to %s",
					s.PreviousSignal.Signal, s.LatestSignal.Signal),
				map[string]any{"from": s.PreviousSignal.Signal, "to": s.LatestSignal.Signal,
					"generated_at": s.LatestSignal.GeneratedAt.UTC()}}
		}
	case "confidence_above":
		threshold := condFloat(a.Condition, "threshold", 0.8)
		if s.LatestSignal != nil && s.LatestSignal.Confidence > threshold {
			return Result{true,
				fmt.Sprintf("signal %s confidence %.2f exceeds %.2f",
					s.LatestSignal.Signal, s.LatestSignal.Confidence, threshold),
				map[string]any{"signal": s.LatestSignal.Signal,
					"confidence": s.LatestSignal.Confidence, "threshold": threshold}}
		}
	case "volatility_spike":
		threshold := condFloat(a.Condition, "threshold", 50) // annualized %
		if s.VolatilityAnnPct != nil && *s.VolatilityAnnPct > threshold {
			return Result{true,
				fmt.Sprintf("annualized volatility %.1f%% exceeds %.1f%%", *s.VolatilityAnnPct, threshold),
				map[string]any{"volatility_pct": *s.VolatilityAnnPct, "threshold": threshold}}
		}
	case "premium_above":
		threshold := condFloat(a.Condition, "threshold", 5)
		if s.PremiumPct != nil && *s.PremiumPct > threshold {
			return Result{true,
				fmt.Sprintf("market premium %.2f%% exceeds %.2f%%", *s.PremiumPct, threshold),
				map[string]any{"premium_pct": *s.PremiumPct, "threshold": threshold}}
		}
	case "stale_data":
		thresholdMin := int(condFloat(a.Condition, "threshold_minutes", float64(s.StaleMinutes)))
		if thresholdMin <= 0 {
			thresholdMin = s.StaleMinutes
		}
		if s.Gold == nil {
			return Result{true, "no 18k gold price data available",
				map[string]any{"threshold_minutes": thresholdMin}}
		}
		age := s.Now.Sub(s.Gold.ObservedAt)
		if age > time.Duration(thresholdMin)*time.Minute {
			return Result{true,
				fmt.Sprintf("18k gold price is stale: last update %.0f minutes ago (threshold %d)",
					age.Minutes(), thresholdMin),
				map[string]any{"age_minutes": int(age.Minutes()), "threshold_minutes": thresholdMin,
					"observed_at": s.Gold.ObservedAt.UTC()}}
		}
	case "provider_failure":
		threshold := int(condFloat(a.Condition, "threshold", 3))
		if threshold <= 0 {
			threshold = 3
		}
		var failing []map[string]any
		for _, p := range s.Providers {
			if p.Enabled && p.ConsecutiveFailures >= threshold {
				failing = append(failing, map[string]any{
					"code": p.Code, "consecutive_failures": p.ConsecutiveFailures,
					"last_error": p.LastError})
			}
		}
		if len(failing) > 0 {
			return Result{true,
				fmt.Sprintf("%d data provider(s) failing (>=%d consecutive failures)", len(failing), threshold),
				map[string]any{"providers": failing, "threshold": threshold}}
		}
	case "model_degradation":
		// Trigger when an active model performs worse than its naive baseline
		// (walk-forward SMAPE), beyond an optional tolerance.
		tolerancePct := condFloat(a.Condition, "tolerance_pct", 0)
		var degraded []map[string]any
		for _, m := range s.Models {
			if m.BaselineSMAPE > 0 && m.SMAPE > m.BaselineSMAPE*(1+tolerancePct/100) {
				degraded = append(degraded, map[string]any{
					"horizon": m.Horizon, "model": m.ModelName,
					"smape": m.SMAPE, "baseline_smape": m.BaselineSMAPE})
			}
		}
		if len(degraded) > 0 {
			return Result{true,
				fmt.Sprintf("%d active model(s) performing worse than naive baseline", len(degraded)),
				map[string]any{"models": degraded, "tolerance_pct": tolerancePct}}
		}
	}
	return no
}
