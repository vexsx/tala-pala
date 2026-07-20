package prices

import (
	"math"
	"testing"
	"time"
)

func TestTheoretical18k(t *testing.T) {
	// xau=2000 USD/ozt, usd=100000 IRT/USD:
	// pure gram usd = 2000/31.1034768 = 64.30149...
	// k18 = 64.30149 * 100000 * 0.75 = 4_822_611.7...
	got := Theoretical18kIRT(2000, 100_000)
	want := 2000.0 / 31.1034768 * 100_000 * 0.75
	if math.Abs(got-want) > 1e-6 {
		t.Fatalf("got %v want %v", got, want)
	}
	if got < 4_822_000 || got > 4_823_000 {
		t.Fatalf("sanity range failed: %v", got)
	}
}

func TestPremiumPct(t *testing.T) {
	if p := PremiumPct(110, 100); math.Abs(p-10) > 1e-12 {
		t.Fatalf("premium = %v, want 10", p)
	}
	if p := PremiumPct(100, 0); !math.IsNaN(p) {
		t.Fatalf("zero theoretical should be NaN, got %v", p)
	}
}

func TestChangePct(t *testing.T) {
	if c := ChangePct(105, 100); math.Abs(c-5) > 1e-12 {
		t.Fatalf("change = %v", c)
	}
}

func TestIsStale(t *testing.T) {
	now := time.Date(2026, 7, 20, 12, 0, 0, 0, time.UTC)
	if IsStale(now.Add(-10*time.Minute), now, 30) {
		t.Fatal("10min should not be stale at threshold 30")
	}
	if !IsStale(now.Add(-31*time.Minute), now, 30) {
		t.Fatal("31min should be stale at threshold 30")
	}
}

func TestComputeIndicators(t *testing.T) {
	// 120 daily bars of a gentle uptrend.
	bars := make([]DayBar, 120)
	base := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	for i := range bars {
		v := 1000.0 + float64(i)*2
		bars[i] = DayBar{Date: base.AddDate(0, 0, i), High: v + 5, Low: v - 5, Close: v}
	}
	res := ComputeIndicators(bars, 90)
	if len(res.Series) != 90 {
		t.Fatalf("series length = %d, want 90", len(res.Series))
	}
	if res.SMA20 == nil || res.SMA50 == nil || res.RSI14 == nil ||
		res.MACD.Line == nil || res.Bollinger.Mid == nil || res.ATR14 == nil ||
		res.Momentum10 == nil || res.ROC10 == nil || res.Volatility == nil ||
		res.Support == nil || res.Resistance == nil {
		t.Fatal("expected all indicators to be computable with 120 bars")
	}
	// Uptrend sanity: last close 1238; sma20 mean of closes 1200..1238 = 1219.
	if math.Abs(*res.SMA20-1219) > 1e-6 {
		t.Fatalf("sma20 = %v, want 1219", *res.SMA20)
	}
	// Momentum over 10 days of +2/day = 20.
	if math.Abs(*res.Momentum10-20) > 1e-6 {
		t.Fatalf("momentum = %v, want 20", *res.Momentum10)
	}
	// RSI of a monotone uptrend must be 100.
	if math.Abs(*res.RSI14-100) > 1e-6 {
		t.Fatalf("rsi = %v, want 100", *res.RSI14)
	}
	// Support/resistance over last 20 closes.
	if math.Abs(*res.Support-(1238-19*2)) > 1e-6 || math.Abs(*res.Resistance-1238) > 1e-6 {
		t.Fatalf("support/resistance = %v/%v", *res.Support, *res.Resistance)
	}

	// Empty input stays well-formed.
	empty := ComputeIndicators(nil, 90)
	if empty.AsOf != nil || len(empty.Series) != 0 {
		t.Fatalf("empty input mishandled: %+v", empty)
	}
}

func TestJoinPremiumSeries(t *testing.T) {
	d1 := time.Date(2026, 7, 1, 0, 0, 0, 0, time.UTC)
	d2 := d1.AddDate(0, 0, 1)
	gold := []dailyPoint{{d1, 5_000_000}, {d2, 5_100_000}}
	xau := []dailyPoint{{d1, 2000}}
	usd := []dailyPoint{{d1, 100_000}}
	out := JoinPremiumSeries(gold, xau, usd)
	if len(out) != 2 {
		t.Fatalf("len = %d", len(out))
	}
	if out[0].Theoretical18k == nil || out[0].PremiumPct == nil {
		t.Fatal("day1 should have theoretical + premium")
	}
	if out[1].Theoretical18k != nil {
		t.Fatal("day2 lacks xau/usd, theoretical must be nil")
	}
}
