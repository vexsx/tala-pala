package equities

import (
	"errors"
	"strings"
	"testing"
	"time"
)

// The Trade chart's read of one share: the same gate and the same arithmetic
// as /stocks/{symbol}/bars, shaped as candles.

func TestAChartIsTheGatedAdjustedSeriesOnly(t *testing.T) {
	if err := chartGate(foolad()); err != nil {
		t.Fatalf("a validated adjustment is servable: %v", err)
	}
	refused := kachad()
	refused.AdjustmentVersion = ptr("priceYesterday-chain-v1")
	refused.AdjustmentStatus = ptr("refused")
	refused.RefusalReason = ptr("-60.4% on 2007-01-09 survives adjustment")
	err := chartGate(refused)
	var gate *AdjustmentUnavailableError
	if !errors.As(err, &gate) {
		t.Fatalf("a refused adjustment must be refused, got %v", err)
	}
	if gate.Status != "refused" || gate.RefusalReason == "" || gate.Symbol != refused.SymbolFA {
		t.Fatalf("the refusal must carry the verdict: %+v", gate)
	}
	d := gate.Details()
	if d["status"] != "refused" || d["symbol"] != refused.SymbolFA {
		t.Fatalf("details = %v", d)
	}
	// Never ingested is a refusal too, and says which.
	if err := chartGate(stockRow{InsCode: "1", SymbolFA: "x"}); !errors.As(err, &gate) ||
		gate.Status != statusNeverIngested {
		t.Fatalf("never ingested: %v", err)
	}
}

func TestChartBarsAreAdjustedOldestFirstAndHaltsHaveNoRange(t *testing.T) {
	// bars() is newest-first: a traded 09-09 (factor 1), a one-price traded
	// 09-08, and a halted 09-07 at factor 2.
	out := buildChartBars(foolad(), bars(), 10, day(2026, time.September, 24))
	if len(out.Items) != 3 || out.HasMore {
		t.Fatalf("items %d more %v", len(out.Items), out.HasMore)
	}
	if !out.Items[0].Day.Equal(day(2026, time.September, 7)) || !out.Items[2].Day.Equal(day(2026, time.September, 9)) {
		t.Fatal("items must go out oldest first")
	}

	halt := out.Items[0]
	if halt.Traded || halt.Open != nil || halt.High != nil || halt.Low != nil || halt.LastTrade != nil {
		t.Fatalf("a halt has no range and no last trade: %+v", halt)
	}
	if halt.Close != 2722*2 || halt.Factor != 2 {
		t.Fatalf("the carried reference close is adjusted like any price: %v × %v", halt.Close, halt.Factor)
	}

	newest := out.Items[2]
	if *newest.Open != 2880 || *newest.High != 2887 || *newest.Low != 2820 {
		t.Fatalf("range %v/%v/%v", *newest.Open, *newest.High, *newest.Low)
	}
	// Close is the official close; the last trade travels beside it.
	if newest.Close != 2881 || newest.LastTrade == nil || *newest.LastTrade != 2887 {
		t.Fatalf("close %v last %v", newest.Close, newest.LastTrade)
	}
	// Turnover and volume are what changed hands: never scaled.
	if newest.Value != 9982417051844 || newest.Volume != 3464453616 {
		t.Fatalf("value/volume were scaled: %v %v", newest.Value, newest.Volume)
	}
}

func TestTheFactorScalesPricesAndNothingThatCounts(t *testing.T) {
	rows := []barRow{{TradeDate: day(2020, time.January, 5), Open: 100, High: 110, Low: 90,
		Close: 105, FinalClose: 104, Volume: 7, TradeCount: 3, Value: 728, Factor: 2.5}}
	b := buildChartBars(foolad(), rows, 10, day(2026, time.September, 24)).Items[0]
	if *b.Open != 250 || *b.High != 275 || *b.Low != 225 || b.Close != 260 || *b.LastTrade != 262.5 {
		t.Fatalf("adjusted prices: %v %v %v %v %v", *b.Open, *b.High, *b.Low, b.Close, *b.LastTrade)
	}
	if b.Value != 728 || b.Volume != 7 || b.Trades != 3 || b.Factor != 2.5 {
		t.Fatalf("counts must not be scaled: %+v", b)
	}
}

func TestAnOfficialCloseOutsideTheRangeIsFlagged(t *testing.T) {
	rows := []barRow{
		{TradeDate: day(2026, time.August, 2), Open: 100, High: 110, Low: 95, Close: 108,
			FinalClose: 112, Volume: 5, TradeCount: 2, Factor: 1},
		{TradeDate: day(2026, time.August, 1), Open: 100, High: 110, Low: 95, Close: 108,
			FinalClose: 105, Volume: 5, TradeCount: 2, Factor: 1},
	}
	out := buildChartBars(foolad(), rows, 10, day(2026, time.September, 24))
	if out.Items[0].CloseOutsideRange || !out.Items[1].CloseOutsideRange {
		t.Fatalf("flags: %v %v", out.Items[0].CloseOutsideRange, out.Items[1].CloseOutsideRange)
	}
}

func TestATradedBarWithAZeroInItsRangeIsDefectiveNotACrash(t *testing.T) {
	rows := []barRow{{TradeDate: day(2026, time.August, 1), Open: 0, High: 110, Low: 95,
		Close: 108, FinalClose: 105, Volume: 5, TradeCount: 2, Factor: 1}}
	out := buildChartBars(foolad(), rows, 10, day(2026, time.September, 24))
	b := out.Items[0]
	if !b.Traded || b.Open != nil || b.High != nil || b.Low != nil || b.Close != 105 {
		t.Fatalf("a defective bar keeps its close and loses its range: %+v", b)
	}
	if out.DefectiveBars != 1 || !strings.Contains(strings.Join(out.Notes, " "), "carry a zero open") {
		t.Fatalf("defective %d, notes %v", out.DefectiveBars, out.Notes)
	}
}

func TestChartPagingDropsTheOldestAndSaysThereIsMore(t *testing.T) {
	out := buildChartBars(foolad(), bars(), 2, day(2026, time.September, 24))
	if !out.HasMore || len(out.Items) != 2 {
		t.Fatalf("more %v items %d", out.HasMore, len(out.Items))
	}
	if !out.Items[1].Day.Equal(day(2026, time.September, 9)) {
		t.Fatal("the extra row that proves has_more is the oldest, never the newest")
	}
}

func TestChartBarsStateTheirAgeRevisionAndCaveats(t *testing.T) {
	meta := foolad()
	out := buildChartBars(meta, bars(), 10, day(2026, time.September, 24))
	if out.DataAge.NewestTradeDate == nil || *out.DataAge.NewestTradeDate != "2026-09-09" ||
		!out.DataAge.Stale {
		t.Fatalf("fifteen days old is stale: %+v", out.DataAge)
	}
	if !strings.HasPrefix(out.Revision, "2026-09-09|priceYesterday-chain-v1|") {
		t.Fatalf("revision = %q", out.Revision)
	}
	// A recomputed adjustment restates older bars: the revision must move.
	later := meta
	later.ComputedAt = ptr(meta.ComputedAt.Add(time.Hour))
	if chartRevision(later) == out.Revision {
		t.Fatal("a recomputed adjustment must change the revision")
	}
	notes := strings.Join(out.Notes, "\n")
	for _, want := range []string{"rials (IRR)", "31 action(s)", "official closing price", "reopening auction"} {
		if !strings.Contains(notes, want) {
			t.Fatalf("notes do not say %q:\n%s", want, notes)
		}
	}
	if out.Meta.Symbol != "فولاد" || out.Meta.SectorFA != "فلزات اساسی" || !out.Meta.Adjustment.Servable {
		t.Fatalf("meta = %+v", out.Meta)
	}
}

func TestTheChartCursorIsExclusive(t *testing.T) {
	// The chart pages backwards with the oldest session it holds; an
	// inclusive bound would hand that session back on every page.
	sql := strings.Join(strings.Fields(chartBarsSelect), " ")
	if !strings.Contains(sql, "b.trade_date < $4") || strings.Contains(sql, "b.trade_date <= $4") {
		t.Fatalf("the upper bound must be exclusive: %s", sql)
	}
	if !strings.Contains(sql, "ORDER BY b.trade_date DESC LIMIT $5") {
		t.Fatalf("newest first, limited: %s", sql)
	}
	if !strings.Contains(strings.Join(strings.Fields(stockByInsCodeSelect), " "), "WHERE i.ins_code = $1") {
		t.Fatal("a chart symbol is resolved by insCode, never by a Persian name")
	}
}
