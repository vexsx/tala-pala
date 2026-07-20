// Package portfolio implements portfolio CRUD, valuation math and CSV
// import/export.
package portfolio

import "math"

// Karat scaling: value of k-karat grams is priced via the 18k price × (k/18).
// This is the documented approximation from CONTRACTS.md.

// Tx is the computation-relevant view of a portfolio transaction.
// PricePerGram and Fees are in Currency ('IRT' or 'IRR'); IRR is converted
// to toman (÷10) before any math.
type Tx struct {
	Type         string  // 'buy' | 'sell'
	Grams        float64 // physical grams of the given karat
	Karat        int     // 18|21|22|24
	PricePerGram float64 // price per physical gram, in Currency
	Fees         float64 // absolute fees for the transaction, in Currency
	Currency     string  // 'IRT' | 'IRR'
}

// ScenarioChanges are the fixed what-if price moves per CONTRACTS.md.
var ScenarioChanges = []float64{-20, -10, -5, 5, 10, 20}

// Scenario is one what-if row.
type Scenario struct {
	ChangePct float64 `json:"change_pct"`
	Value     float64 `json:"value"`
	PnL       float64 `json:"pnl"`
}

// Summary is the computed portfolio state (all money values in IRT).
type Summary struct {
	TotalGrams18kEquivalent float64    `json:"total_grams_18k_equivalent"`
	Invested                float64    `json:"invested"`
	CurrentValue            float64    `json:"current_value"`
	UnrealizedPnL           float64    `json:"unrealized_pnl"`
	PnLPct                  *float64   `json:"pnl_pct"`
	AvgPrice                *float64   `json:"avg_price"`
	BreakEvenPrice          *float64   `json:"break_even_price"`
	Scenarios               []Scenario `json:"scenarios"`
	TargetPriceForProfitPct *float64   `json:"target_price_for_profit_pct"`
	TargetProfitPct         float64    `json:"target_profit_pct"`
}

// toIRT converts an amount in the transaction currency to toman.
func toIRT(amount float64, currency string) float64 {
	if currency == "IRR" {
		return amount / 10
	}
	return amount
}

// Grams18k returns the 18k-equivalent grams of a transaction.
func Grams18k(grams float64, karat int) float64 {
	return grams * float64(karat) / 18.0
}

// Compute derives the portfolio summary from transactions and the current
// 18k price (IRT/gram). Pure function (unit tested).
//
// Definitions:
//   - invested        = Σ buy (gross cost + fees) − Σ sell (gross proceeds − fees)
//     i.e. the net cash currently locked in the position, fees included.
//   - avg_price       = gross buy cost (excl. fees) / total bought 18k-equivalent grams.
//   - break_even      = invested / current 18k-equivalent grams — the 18k price at
//     which current_value equals invested (fees included).
//   - target_price_for_profit_pct = invested × (1 + p/100) / grams.
func Compute(txs []Tx, current18kPrice float64, targetProfitPct float64) Summary {
	var (
		grams18k      float64
		invested      float64
		buyCostGross  float64
		boughtGrams18 float64
	)
	for _, t := range txs {
		g18 := Grams18k(t.Grams, t.Karat)
		price := toIRT(t.PricePerGram, t.Currency)
		fees := toIRT(t.Fees, t.Currency)
		gross := t.Grams * price // paid per physical gram
		if t.Type == "sell" {
			grams18k -= g18
			invested -= gross - fees
		} else {
			grams18k += g18
			invested += gross + fees
			buyCostGross += gross
			boughtGrams18 += g18
		}
	}

	s := Summary{
		TotalGrams18kEquivalent: round6(grams18k),
		Invested:                round2(invested),
		TargetProfitPct:         targetProfitPct,
		Scenarios:               []Scenario{},
	}
	s.CurrentValue = round2(grams18k * current18kPrice)
	s.UnrealizedPnL = round2(s.CurrentValue - invested)

	if invested > 0 {
		p := round4(s.UnrealizedPnL / invested * 100)
		s.PnLPct = &p
	}
	if boughtGrams18 > 0 {
		a := round2(buyCostGross / boughtGrams18)
		s.AvgPrice = &a
	}
	if grams18k > 1e-12 {
		be := round2(invested / grams18k)
		s.BreakEvenPrice = &be
		tp := round2(invested * (1 + targetProfitPct/100) / grams18k)
		s.TargetPriceForProfitPct = &tp
	}

	for _, ch := range ScenarioChanges {
		v := s.CurrentValue * (1 + ch/100)
		s.Scenarios = append(s.Scenarios, Scenario{
			ChangePct: ch,
			Value:     round2(v),
			PnL:       round2(v - invested),
		})
	}
	return s
}

func round2(v float64) float64 { return math.Round(v*100) / 100 }
func round4(v float64) float64 { return math.Round(v*1e4) / 1e4 }
func round6(v float64) float64 { return math.Round(v*1e6) / 1e6 }
