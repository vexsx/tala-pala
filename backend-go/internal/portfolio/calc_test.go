package portfolio

import (
	"math"
	"testing"
)

func feq(t *testing.T, got, want float64, name string) {
	t.Helper()
	if math.Abs(got-want) > 1e-6 {
		t.Fatalf("%s: got %v, want %v", name, got, want)
	}
}

func TestCompute_SimpleBuy(t *testing.T) {
	txs := []Tx{
		{Type: "buy", Grams: 10, Karat: 18, PricePerGram: 5_000_000, Fees: 500_000, Currency: "IRT"},
	}
	s := Compute(txs, 5_500_000, 10)

	feq(t, s.TotalGrams18kEquivalent, 10, "grams")
	feq(t, s.Invested, 50_500_000, "invested includes fees")
	feq(t, s.CurrentValue, 55_000_000, "current value")
	feq(t, s.UnrealizedPnL, 4_500_000, "pnl")
	if s.PnLPct == nil {
		t.Fatal("pnl_pct nil")
	}
	feq(t, *s.PnLPct, 8.9109, "pnl pct (rounded to 4 decimals)")
	if s.AvgPrice == nil || s.BreakEvenPrice == nil {
		t.Fatal("avg/break-even nil")
	}
	feq(t, *s.AvgPrice, 5_000_000, "avg price excludes fees")
	feq(t, *s.BreakEvenPrice, 5_050_000, "break even includes fees")
	if s.TargetPriceForProfitPct == nil {
		t.Fatal("target nil")
	}
	feq(t, *s.TargetPriceForProfitPct, 50_500_000*1.1/10, "target price +10%")
}

func TestCompute_KaratConversion(t *testing.T) {
	// 9 grams of 24k = 9*24/18 = 12 grams 18k-equivalent.
	txs := []Tx{
		{Type: "buy", Grams: 9, Karat: 24, PricePerGram: 6_000_000, Fees: 0, Currency: "IRT"},
	}
	s := Compute(txs, 5_000_000, 10)
	feq(t, s.TotalGrams18kEquivalent, 12, "24k grams scaled by 24/18")
	feq(t, s.CurrentValue, 60_000_000, "value = 12 x 18k price")
	feq(t, s.Invested, 54_000_000, "invested")
}

func TestCompute_IRRConversion(t *testing.T) {
	// IRR prices are rials: divide by 10 to get toman.
	txs := []Tx{
		{Type: "buy", Grams: 2, Karat: 18, PricePerGram: 50_000_000, Fees: 10_000_000, Currency: "IRR"},
	}
	s := Compute(txs, 5_000_000, 10)
	feq(t, s.Invested, 2*5_000_000+1_000_000, "IRR converted to IRT")
	feq(t, s.CurrentValue, 10_000_000, "current value")
}

func TestCompute_BuyAndSell(t *testing.T) {
	txs := []Tx{
		{Type: "buy", Grams: 10, Karat: 18, PricePerGram: 4_000_000, Fees: 100_000, Currency: "IRT"},
		{Type: "sell", Grams: 4, Karat: 18, PricePerGram: 5_000_000, Fees: 50_000, Currency: "IRT"},
	}
	s := Compute(txs, 5_000_000, 10)
	feq(t, s.TotalGrams18kEquivalent, 6, "net grams")
	// invested = 40.1M - (20M - 0.05M) = 20.15M
	feq(t, s.Invested, 20_150_000, "net invested")
	feq(t, s.CurrentValue, 30_000_000, "current value")
	feq(t, s.UnrealizedPnL, 9_850_000, "pnl")
	feq(t, *s.BreakEvenPrice, 3_358_333.33, "break even (rounded to 2 decimals)")
}

func TestCompute_Scenarios(t *testing.T) {
	txs := []Tx{{Type: "buy", Grams: 1, Karat: 18, PricePerGram: 1_000_000, Currency: "IRT"}}
	s := Compute(txs, 1_000_000, 10)
	if len(s.Scenarios) != 6 {
		t.Fatalf("expected 6 scenarios, got %d", len(s.Scenarios))
	}
	wantChanges := []float64{-20, -10, -5, 5, 10, 20}
	for i, sc := range s.Scenarios {
		feq(t, sc.ChangePct, wantChanges[i], "scenario change")
		feq(t, sc.Value, 1_000_000*(1+wantChanges[i]/100), "scenario value")
		feq(t, sc.PnL, sc.Value-1_000_000, "scenario pnl")
	}
}

func TestCompute_Empty(t *testing.T) {
	s := Compute(nil, 5_000_000, 10)
	feq(t, s.TotalGrams18kEquivalent, 0, "grams")
	feq(t, s.Invested, 0, "invested")
	if s.PnLPct != nil || s.AvgPrice != nil || s.BreakEvenPrice != nil || s.TargetPriceForProfitPct != nil {
		t.Fatal("nullable fields must be nil for empty portfolio")
	}
}
