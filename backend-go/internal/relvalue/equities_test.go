package relvalue

// The equity gate. Every case here is an instrument that must NOT be measured,
// and the reason it must not is different each time -- which is the point: a
// single "excluded" would tell a reader nothing they could act on.

import (
	"strings"
	"testing"
)

func validatedRow() equityRosterRow {
	return equityRosterRow{
		Symbol: "فولاد", NameFA: "فولاد مبارکه اصفهان", SectorFA: "فلزات اساسی",
		Enabled: true, BarCount: 4600,
		Status: equityStatusValidated, Version: "priceYesterday-chain-v1",
	}
}

func TestAValidatedEquityBecomesATomanQuotedRegistryRow(t *testing.T) {
	inst, _, ok := equityEligibility(validatedRow())
	if !ok {
		t.Fatal("a validated, enabled instrument with bars must be measurable")
	}
	// TOMAN. TSETMC quotes rials; every return is a ratio so the factor of ten
	// cancels and a wrong unit would hide in every percentage on the page,
	// surfacing only in an end-of-window level printed beside gold.
	if inst.QuoteCurrency != quoteIRT {
		t.Errorf("quote currency = %q, want %q: loadEquitySeries divides rials into "+
			"toman, and this field is what says so", inst.QuoteCurrency, quoteIRT)
	}
	if inst.Source != sourceEquityBars {
		t.Errorf("source = %q, want %q, or the prices table will be asked for a "+
			"symbol it has never held", inst.Source, sourceEquityBars)
	}
	if !inst.IsDerived {
		t.Error("an adjusted close is derived: it is the raw close times a factor " +
			"this platform computed, and a reader comparing it with a TSETMC screen " +
			"must be able to find out why the numbers differ")
	}
	if inst.IsProxy {
		t.Error("these are the exchange's own prints, not a proxy")
	}
	if !strings.Contains(inst.Notes, "TOMAN") {
		t.Errorf("the note must state the unit: %s", inst.Notes)
	}
	// No invented English name: the UI falls back to the code, which is the
	// Persian trading symbol a reader recognises. Filling name_en with the
	// symbol would print the same string twice in one cell.
	if inst.NameEN != "" {
		t.Errorf("name_en = %q, want empty: TSETMC publishes no English name and a "+
			"transliteration would be an invented fact", inst.NameEN)
	}
	if inst.NameFA == "" {
		t.Error("the Persian company name must travel, or the row is only a ticker")
	}
	if inst.Domain != equityDomain {
		t.Errorf("domain = %q, want %q so the UI can mark these as equities",
			inst.Domain, equityDomain)
	}
}

func TestTheAdjustmentGateExcludesWithADistinctReasonEachTime(t *testing.T) {
	for _, tc := range []struct {
		name string
		row  func(equityRosterRow) equityRosterRow
		want string
	}{
		{"disabled", func(r equityRosterRow) equityRosterRow {
			r.Enabled = false
			return r
		}, "disabled in the equity roster"},
		{"no bars", func(r equityRosterRow) equityRosterRow {
			r.BarCount = 0
			return r
		}, "carries no stored bar"},
		{"never adjusted", func(r equityRosterRow) equityRosterRow {
			r.Status = ""
			return r
		}, "no corporate-action adjustment has ever been computed"},
		{"refused", func(r equityRosterRow) equityRosterRow {
			r.Status = "refused"
			r.Refusal = "a 4.1x session return survived the chain."
			return r
		}, "validation gate refused"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			_, ex, ok := equityEligibility(tc.row(validatedRow()))
			if ok {
				t.Fatalf("%s must be excluded from a table of adjusted returns", tc.name)
			}
			if ex.Symbol != "فولاد" {
				t.Errorf("the exclusion must name the symbol, got %q", ex.Symbol)
			}
			if !strings.Contains(ex.Reason, tc.want) {
				t.Errorf("reason = %q, want it to contain %q", ex.Reason, tc.want)
			}
		})
	}
}

// A refused adjustment carries the gate's own explanation forward. Dropping it
// would leave the reader with "refused" and no way to judge it.
func TestARefusalCarriesTheGatesOwnReason(t *testing.T) {
	r := validatedRow()
	r.Status = "refused"
	r.Refusal = "a 4.1x session return survived the chain on 2021-04-12."
	_, ex, ok := equityEligibility(r)
	if ok {
		t.Fatal("a refused adjustment must not be measured")
	}
	if !strings.Contains(ex.Reason, "4.1x session return") {
		t.Errorf("the gate's own reason must travel to the reader: %s", ex.Reason)
	}
}

// The prices table must never be asked for an equity symbol.
func TestEquitiesAreNotRequestedFromThePricesTable(t *testing.T) {
	equity, _, ok := equityEligibility(validatedRow())
	if !ok {
		t.Fatal("setup")
	}
	instruments := []instrumentRow{
		{Code: "IR_GOLD_18K", QuoteCurrency: quoteIRT, Enabled: true},
		{Code: "XAUUSD", QuoteCurrency: quoteUSD, Enabled: true},
		{Code: "US10Y", QuoteCurrency: quotePCT, Enabled: true},
		equity,
	}
	got := priceableSymbols(instruments)
	for _, code := range got {
		if code == "فولاد" {
			t.Fatalf("priceableSymbols returned an equity symbol %q: it lives in "+
				"equity_bars, and asking `prices` for it returns nothing today and "+
				"the WRONG series the day a code collides. Got %v", code, got)
		}
	}
	// The ordinary instruments are still there.
	var sawGold bool
	for _, code := range got {
		if code == "IR_GOLD_18K" {
			sawGold = true
		}
	}
	if !sawGold {
		t.Errorf("skipping equities must not drop the price-backed rows: %v", got)
	}
}

// The rial→toman division is the whole reason a level printed beside gold is
// comparable. Pinned as arithmetic so removing it fails here rather than in a
// number nobody re-derives.
func TestRialsPerTomanIsTen(t *testing.T) {
	if rialsPerToman != 10.0 {
		t.Fatalf("rialsPerToman = %v, want 10: TSETMC quotes rials and this "+
			"platform is toman-canonical", rialsPerToman)
	}
	// فملی printed 25,330 rials on 2026-09-09; that is 2,533 toman.
	if got := 25330.0 / rialsPerToman; got != 2533.0 {
		t.Errorf("25,330 rials = %v toman, want 2533", got)
	}
}
