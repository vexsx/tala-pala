package equities

import (
	"math"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/bourse"
)

func mday(s string) time.Time {
	d, err := time.Parse(dateLayout, s)
	if err != nil {
		panic(err)
	}
	return d
}

// The page reading the bars and the page reading the indices are fed by the
// same off-server run; they must not disagree about when it has stopped.
func TestTheMarketPagesShareTheBarsStaleBoundary(t *testing.T) {
	if bourse.StaleAfterDays != equityStaleAfterDays {
		t.Fatalf("bourse %d, equities %d", bourse.StaleAfterDays, equityStaleAfterDays)
	}
	if bourse.RefreshCommand != equityRefreshCommand {
		t.Fatalf("bourse %q, equities %q", bourse.RefreshCommand, equityRefreshCommand)
	}
}

// A market that moves 1% a session, and a share that moves exactly twice the
// market over every span it trades — including a span that crosses a halt.
func twiceTheMarket() ([]relSession, []bourse.IndexPoint) {
	var market []bourse.IndexPoint
	var sessions []relSession
	m := 1000.0
	start := mday("2025-01-04")
	stock := 100.0
	lastTradedM := m
	for i := 0; i < 60; i++ {
		d := start.AddDate(0, 0, i)
		if i > 0 {
			if i%3 == 0 {
				m *= 1.01
			} else {
				m *= 0.996
			}
		}
		market = append(market, bourse.IndexPoint{Day: d, Value: m})
		halted := i >= 20 && i < 30 // a ten-session halt
		if halted {
			sessions = append(sessions, relSession{Day: d, Adjusted: stock, Traded: false})
			continue
		}
		if i > 0 {
			stock *= 1 + 2*(m/lastTradedM-1)
		}
		lastTradedM = m
		sessions = append(sessions, relSession{Day: d, Adjusted: stock, Traded: true})
	}
	return sessions, market
}

func TestBetaIsMeasuredOverMatchedSpans(t *testing.T) {
	sessions, market := twiceTheMarket()
	var traded []relSession
	for _, s := range sessions {
		if s.Traded {
			traded = append(traded, s)
		}
	}
	b := matchedBeta(traded, market)
	if b.Beta == nil || math.Abs(*b.Beta-2) > 1e-4 {
		t.Fatalf("beta %v, want 2", b.Beta)
	}
	if b.Correlation == nil || math.Abs(*b.Correlation-1) > 1e-9 {
		t.Fatalf("correlation %v", b.Correlation)
	}
	if b.MultiSessionSpans != 1 || b.Pairs != 48 {
		t.Fatalf("pairs %d, multi-session spans %d; want 48 and the one halt", b.Pairs, b.MultiSessionSpans)
	}
}

func TestPairingHaltedDaysAsZerosWouldHaveBrokenTheBeta(t *testing.T) {
	// The naive pairing — every calendar session, halted days as 0% for the
	// share — is what matchedBeta exists to avoid. Show that it is wrong here.
	sessions, market := twiceTheMarket()
	var rs, rm []float64
	for i := 1; i < len(sessions); i++ {
		rs = append(rs, sessions[i].Adjusted/sessions[i-1].Adjusted-1)
		rm = append(rm, market[i].Value/market[i-1].Value-1)
	}
	var ms, mm, cov, vm float64
	for i := range rs {
		ms += rs[i]
		mm += rm[i]
	}
	ms /= float64(len(rs))
	mm /= float64(len(rm))
	for i := range rs {
		cov += (rs[i] - ms) * (rm[i] - mm)
		vm += (rm[i] - mm) * (rm[i] - mm)
	}
	if naive := cov / vm; math.Abs(naive-2) < 0.05 {
		t.Fatalf("the naive beta %.3f happens to be right, so this test proves nothing", naive)
	}
}

// The production case, reduced: a share that tracks its market session by
// session, except for ONE long halt across which the market rallied and the
// share fell. Regressing that span with the daily ones is what put فولاد's
// correlation with TEDPIX at -0.09.
func TestOneLongHaltDoesNotDecideTheBeta(t *testing.T) {
	var market []bourse.IndexPoint
	var traded []relSession
	m, s := 1000.0, 100.0
	d := mday("2025-01-04")
	for i := 0; i < 80; i++ {
		d = d.AddDate(0, 0, 1)
		step := 0.01
		if i%2 == 1 {
			step = -0.008
		}
		m *= 1 + step
		market = append(market, bourse.IndexPoint{Day: d, Value: m})
		switch {
		case i < 40:
			s *= 1 + step
			traded = append(traded, relSession{Day: d, Adjusted: s, Traded: true})
		case i == 79:
			s *= 0.915 // -8.5% across the halt, while the market rose
			traded = append(traded, relSession{Day: d, Adjusted: s, Traded: true})
		}
	}
	b := matchedBeta(traded, market)
	if b.MultiSessionSpans != 1 {
		t.Fatalf("multi-session spans %d", b.MultiSessionSpans)
	}
	if b.Correlation == nil || *b.Correlation < 0.99 {
		t.Fatalf("correlation %v: the single long span must not decide it", b.Correlation)
	}
}

func TestAClosureIsOneSessionOfMarketMovement(t *testing.T) {
	idx := []bourse.IndexPoint{
		{Day: mday("2026-02-24"), Value: 100},
		{Day: mday("2026-02-25"), Value: 101},
		{Day: mday("2026-02-28"), Value: 101, Unchanged: true},
		{Day: mday("2026-03-01"), Value: 101, Unchanged: true},
		{Day: mday("2026-03-02"), Value: 103},
	}
	if n := marketSessionsBetween(idx, mday("2026-02-25"), mday("2026-03-02")); n != 1 {
		t.Fatalf("sessions %d, want 1: the repeated rows are the market being shut", n)
	}
	if n := marketSessionsBetween(idx, mday("2026-02-24"), mday("2026-03-02")); n != 2 {
		t.Fatalf("sessions %d, want 2", n)
	}
}

func TestTooFewSpansWithholdTheBeta(t *testing.T) {
	sessions, market := twiceTheMarket()
	b := matchedBeta(sessions[:5], market)
	if b.Beta != nil || b.Reason == "" {
		t.Fatalf("%+v", b)
	}
}

func TestAHaltIsAGapInTheShareAndNotInTheMarket(t *testing.T) {
	sessions, market := twiceTheMarket()
	points, sum := buildRelative(sessions, market, nil, nil, mday("2025-03-04"))
	if sum.HaltedSessions != 10 {
		t.Fatalf("halted %d", sum.HaltedSessions)
	}
	gaps := 0
	for _, p := range points {
		if p.Stock == nil {
			gaps++
		}
		if p.Market == nil {
			t.Fatalf("%s: the market line must be continuous", p.Date)
		}
	}
	if gaps != 10 {
		t.Fatalf("gaps %d", gaps)
	}
	if *points[0].Stock != 100 || *points[0].Market != 100 {
		t.Fatal("both lines start at 100")
	}
	if sum.ExcessVsMarketPP == nil || sum.VsSector != nil {
		t.Fatalf("%+v", sum)
	}
}

func TestTheWindowOpensOnTheTradedSessionInForce(t *testing.T) {
	sessions, market := twiceTheMarket()
	from := mday("2025-01-28") // inside the halt (sessions 20..29)
	_, sum := buildRelative(sessions, market, nil, &from, mday("2025-03-04"))
	// The last traded session before the window opened is 2025-01-23.
	if sum.From == nil || *sum.From != "2025-01-23" {
		t.Fatalf("from %v", sum.From)
	}
}

func TestStockFlowsCumulateOnlyConsistentSessions(t *testing.T) {
	bar := func(v float64) *float64 { return &v }
	all := []bourse.FlowSession{
		{Day: mday("2026-09-27"), BuyIValue: 70, BuyNValue: 30, SellIValue: 40, SellNValue: 60, BuyICount: 7, SellICount: 4, BarValue: bar(100)},
		{Day: mday("2026-09-28"), BuyIValue: 50, BuyNValue: 50, SellIValue: 90, SellNValue: 0, BuyICount: 5, SellICount: 9, BarValue: bar(100)},
		{Day: mday("2026-09-29"), BuyIValue: 60, BuyNValue: 40, SellIValue: 20, SellNValue: 80, BuyICount: 6, SellICount: 2, BarValue: bar(100)},
	}
	meta := stockRow{InsCode: "1", SymbolFA: "فولاد"}
	out := buildStockFlows(meta, all, nil, "max", mday("2026-09-30"))
	if len(out.Days) != 3 || out.Summary.Excluded != 1 {
		t.Fatalf("days %d excluded %d", len(out.Days), out.Summary.Excluded)
	}
	if len(out.CumulativeNetIndividual) != 2 {
		t.Fatalf("cumulative has %d points, want the two consistent sessions", len(out.CumulativeNetIndividual))
	}
	if *out.CumulativeNetIndividual[1].Value != 30+40 {
		t.Fatalf("cumulative %v", *out.CumulativeNetIndividual[1].Value)
	}
}
