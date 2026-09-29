package bourse

// The market-wide money flow, tested on a synthetic market small enough to
// state every expected number by hand. Two hundred filler shares in sector 01
// trade 100 toman a session with no net flow, which is exactly the
// minMarketShares a date needs to be a market session; the shares a test is
// about are added beside them, so every sum below is the filler's plus theirs.

import (
	"encoding/json"
	"fmt"
	"math"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"
)

var newestDay = day("2026-09-28")

// calendarDays is k consecutive dates ending at newestDay, newest first.
func calendarDays(k int) []time.Time {
	out := make([]time.Time, k)
	for i := range out {
		out[i] = newestDay.AddDate(0, 0, -i)
	}
	return out
}

// traded is a session whose two identities hold exactly: volumes at 1,000
// toman a share, ten individual buyers and twenty individual sellers.
func traded(d time.Time, buyI, buyN, sellI, sellN float64) FlowSession {
	return FlowSession{Day: d, BuyIValue: buyI, BuyNValue: buyN, SellIValue: sellI, SellNValue: sellN,
		BuyIVolume: buyI / 1000, BuyNVolume: buyN / 1000, SellIVolume: sellI / 1000,
		SellNVolume: sellN / 1000, BuyICount: 10, BuyNCount: 1, SellICount: 20, SellNCount: 1}
}

var testSectors = []sectorName{
	{Code: "01", NameFA: "زراعت و خدمات وابسته", NameEN: "Agriculture and related services"},
	{Code: "27", NameFA: "فلزات اساسی", NameEN: "Basic metals"},
	{Code: "57", NameFA: "بانکها و موسسات اعتباری", NameEN: "Banks and credit institutions"},
}

type testMarket struct {
	days        []time.Time
	rows        []marketRow
	shares      []shareMeta
	prices      []priceRow
	sectorIndex map[string]string
	series      map[string][]IndexPoint
}

const fillerSector = "01"

func newTestMarket(k int) *testMarket {
	m := &testMarket{days: calendarDays(k), sectorIndex: map[string]string{},
		series: map[string][]IndexPoint{}}
	for i := 0; i < minMarketShares; i++ {
		code := fmt.Sprintf("9%04d", i)
		m.shares = append(m.shares, shareMeta{InsCode: code, Symbol: fmt.Sprintf("پر%d", i),
			Market: "bourse", Board: "main", CompanyCode: fmt.Sprintf("IRO1F%03d", i),
			SectorCode: fillerSector, Listed: true})
		for _, d := range m.days {
			m.flow(code, traded(d, 50, 50, 50, 50))
		}
	}
	return m
}

func (m *testMarket) share(s shareMeta) {
	if s.Market == "" {
		s.Market = "bourse"
	}
	if s.Board == "" {
		s.Board = "main"
	}
	s.Listed = true
	m.shares = append(m.shares, s)
}

func (m *testMarket) flow(code string, f FlowSession) {
	m.rows = append(m.rows, marketRow{InsCode: code, Flow: f})
}

// countsOf is what sectorFlowsCalendarSelect computes.
func countsOf(rows []marketRow) []sessionCount {
	idx := map[time.Time]int{}
	var out []sessionCount
	for _, r := range rows {
		d := dayFloor(r.Flow.Day)
		i, ok := idx[d]
		if !ok {
			i = len(out)
			idx[d] = i
			out = append(out, sessionCount{Day: d})
		}
		out[i].Rows++
		f := r.Flow
		if f.BuyIValue+f.BuyNValue+f.SellIValue+f.SellNValue > 0 {
			out[i].Traded++
		}
	}
	return out
}

func (m *testMarket) build() sectorFlowsBuilt {
	return buildSectorFlows(sectorFlowInput{
		Calendar:    buildMarketCalendar(countsOf(m.rows), calendarSessions),
		Rows:        m.rows,
		Prices:      m.prices,
		Shares:      m.shares,
		Sectors:     testSectors,
		SectorIndex: m.sectorIndex,
		IndexSeries: m.series,
	})
}

func sectorOf(t *testing.T, b sectorFlowsBuilt, code string) sectorFlowItem {
	t.Helper()
	s, ok := b.sectors[code]
	if !ok {
		t.Fatalf("sector %s missing", code)
	}
	return s
}

func shareIn(t *testing.T, b sectorFlowsBuilt, sector string, n int, ins string) shareFlowItem {
	t.Helper()
	for _, it := range b.shares[sector][n] {
		if it.InsCode == ins {
			return it
		}
	}
	t.Fatalf("share %s not in sector %s window %d", ins, sector, n)
	return shareFlowItem{}
}

func num(t *testing.T, name string, v *float64) float64 {
	t.Helper()
	if v == nil {
		t.Fatalf("%s is nil", name)
	}
	return *v
}

// --- the four tiers ----------------------------------------------------------------

func TestTheTiersSeparateUncheckedFromFailed(t *testing.T) {
	ok := traded(newestDay, 70, 30, 40, 60)
	if tier, _ := ok.Tier(); tier != TierIdentityOnly {
		t.Fatalf("no bar: %s, want identity_only", tier)
	}
	bar := ok
	bar.BarValue = ptr(100.5)
	if tier, why := bar.Tier(); tier != TierBarChecked || why != "" {
		t.Fatalf("bar within 1%%: %s %q", tier, why)
	}
	bar.BarValue = ptr(105)
	if tier, why := bar.Tier(); tier != TierExcluded || why == "" {
		t.Fatalf("bar 5%% off: %s", tier)
	}
	if tier, _ := (FlowSession{Day: newestDay}).Tier(); tier != TierNoTrade {
		t.Fatalf("nothing traded: %s, want no_trade", tier)
	}
	if tier, _ := traded(newestDay, 70, 30, 40, 50).Tier(); tier != TierExcluded {
		t.Fatalf("buy 100 against sell 90: %s", tier)
	}
}

func TestTheVolumeIdentityIsItsOwnCheck(t *testing.T) {
	// Values agree exactly; one side's volume is 2% short — the shape of the
	// three فولاد rows the value identity alone lets through.
	f := traded(newestDay, 70, 30, 40, 60)
	f.SellNVolume *= 0.98 * 0.98
	f.BarValue = ptr(100)
	if tier, why := f.Tier(); tier != TierExcluded || !strings.Contains(why, "volume") {
		t.Fatalf("volume mismatch: %s %q, want excluded for volume", tier, why)
	}
	// The roster's gate is unchanged: it never read volumes, and still passes
	// this row. Tier is the market's check, Check() stays the roster's.
	if ok, _ := f.Check(); !ok {
		t.Fatal("Check() must stay as it was")
	}
	noVol := traded(newestDay, 70, 30, 40, 60)
	noVol.BuyIVolume, noVol.BuyNVolume, noVol.SellIVolume, noVol.SellNVolume = 0, 0, 0, 0
	if tier, _ := noVol.Tier(); tier != TierExcluded {
		t.Fatalf("value with no volume: %s", tier)
	}
}

func TestEverySessionValueThatExistsIsCompared(t *testing.T) {
	f := traded(newestDay, 70, 30, 40, 60)
	if tier, _ := classifyFlow(f, ptr(100.4)); tier != TierBarChecked {
		t.Fatalf("session statement agrees: %s", tier)
	}
	// The bar agrees and the session statement does not: one disagreeing
	// statement is enough to exclude.
	f.BarValue = ptr(100)
	if tier, why := classifyFlow(f, ptr(90)); tier != TierExcluded || !strings.Contains(why, "session statement") {
		t.Fatalf("session statement 10%% off: %s %q", tier, why)
	}
}

// --- the calendar ---------------------------------------------------------------------

func TestADateIsAMarketSessionOnlyWithTwoHundredTradedShares(t *testing.T) {
	counts := []sessionCount{
		{Day: day("2026-09-29"), Rows: 19, Traded: 19}, // the roster alone
		{Day: day("2026-09-28"), Rows: 1100, Traded: 1000},
		{Day: day("2026-09-27"), Rows: 199, Traded: 199}, // an ingest that stopped
		{Day: day("2026-09-24"), Rows: 1100, Traded: 1000},
		{Day: day("2026-09-23"), Rows: 1100, Traded: 200},
	}
	c := buildMarketCalendar(counts, calendarSessions)
	if len(c.Sessions) != 3 || dayString(c.Sessions[0]) != "2026-09-28" || dayString(c.Sessions[2]) != "2026-09-23" {
		t.Fatalf("sessions %v", c.Sessions)
	}
	if c.ThinAfterNewest != 1 || c.ThinSkipped != 1 {
		t.Fatalf("thin after %d, skipped %d", c.ThinAfterNewest, c.ThinSkipped)
	}
	if dayString(*c.NewestStored) != "2026-09-29" || c.NewestStoredTraded != 19 {
		t.Fatal("the newest stored date is kept, and said to be thin")
	}
	if got := buildMarketCalendar(counts[:1], calendarSessions); got.marketWide() {
		t.Fatal("nineteen shares are the roster, not the market")
	}
	// The roster's own dates before the market-wide history began are older
	// than every session, not gaps between them.
	older := append(append([]sessionCount(nil), counts...),
		sessionCount{Day: day("2026-09-22"), Rows: 19, Traded: 19},
		sessionCount{Day: day("2026-09-21"), Rows: 19, Traded: 19})
	if c := buildMarketCalendar(older, calendarSessions); c.ThinSkipped != 1 || len(c.Sessions) != 3 {
		t.Fatalf("skipped %d sessions %d", c.ThinSkipped, len(c.Sessions))
	}
}

func TestTheNewestSessionIsFlaggedPartialAgainstTheTwentyBefore(t *testing.T) {
	mk := func(newest int) marketCalendar {
		counts := []sessionCount{{Day: day("2026-09-28"), Traded: newest, Rows: newest}}
		for i := 1; i <= 20; i++ {
			counts = append(counts, sessionCount{Day: day("2026-09-28").AddDate(0, 0, -i), Traded: 1000, Rows: 1000})
		}
		// Older sessions are thin-but-valid; they must not move the median.
		for i := 21; i <= 40; i++ {
			counts = append(counts, sessionCount{Day: day("2026-09-28").AddDate(0, 0, -i), Traded: 300, Rows: 300})
		}
		return buildMarketCalendar(counts, calendarSessions)
	}
	c := mk(700)
	if c.MedianPrev == nil || *c.MedianPrev != 1000 || !c.NewestPartial {
		t.Fatalf("700 of a median 1000: partial %v median %v", c.NewestPartial, c.MedianPrev)
	}
	if mk(800).NewestPartial {
		t.Fatal("exactly 80% is not below 80%")
	}
	if mk(1000).NewestPartial {
		t.Fatal("a normal session is not partial")
	}
	if !contains(strings.Join(marketWideNotes(c), " "), "partly ingested") {
		t.Fatal("a partial newest session must be said")
	}
}

func TestAWindowLongerThanTheCalendarIsNotShortened(t *testing.T) {
	m := newTestMarket(30)
	b := m.build()
	if w := b.resp.Sessions["60"]; w.Available || w.Reason == "" || w.From != nil {
		t.Fatalf("60 sessions of 30: %+v", w)
	}
	if _, ok := b.resp.Market["60"]; ok {
		t.Fatal("an unavailable window has no market figure")
	}
	// 20 fits; the 20 before it does not, so there is no rotation figure.
	w20 := sectorOf(t, b, fillerSector).Windows["20"]
	if w20.ValueSharePct == nil || w20.PreviousValueSharePct != nil || w20.ValueShareChangePP != nil {
		t.Fatalf("20 of 30: share %v prev %v", w20.ValueSharePct, w20.PreviousValueSharePct)
	}
	if b.resp.Sessions["20"].PreviousFrom != nil {
		t.Fatal("no previous 20-session window exists")
	}
}

// --- the roster is not the market ----------------------------------------------------------

func TestWithoutMarketWideFlowsNothingIsSummed(t *testing.T) {
	m := &testMarket{days: calendarDays(10)}
	for i := 0; i < 19; i++ {
		code := fmt.Sprintf("1%04d", i)
		m.share(shareMeta{InsCode: code, Symbol: fmt.Sprintf("س%d", i), SectorCode: "27", InRoster: true})
		for _, d := range m.days {
			m.flow(code, traded(d, 70, 30, 40, 60))
		}
	}
	b := m.build()
	cov := b.resp.Coverage
	if cov.MarketWide || cov.NewestSession != nil || cov.SessionsAvailable != 0 {
		t.Fatalf("coverage %+v", cov)
	}
	if cov.NewestStoredDate == nil || *cov.NewestStoredDate != "2026-09-28" || cov.NewestStoredTradedShares != 19 {
		t.Fatalf("the stored date and its 19 shares must be stated: %+v", cov)
	}
	if len(b.resp.Sectors) != 0 || len(b.resp.Market) != 0 || len(b.resp.Top) != 0 || len(b.resp.Blocks) != 0 {
		t.Fatal("the roster must not be summed as the market")
	}
	if !contains(b.resp.Notes[0], "Market-wide flows not ingested yet") || !contains(b.resp.Notes[0], "not the market") {
		t.Fatalf("note: %s", b.resp.Notes[0])
	}
	if b.newest == nil || dayString(*b.newest) != "2026-09-28" {
		t.Fatal("data_age still describes what is stored")
	}
}

// --- windows on the market's calendar ---------------------------------------------------

func TestAHaltedShareIsNotInTheNewestSession(t *testing.T) {
	m := newTestMarket(30)
	m.share(shareMeta{InsCode: "46348559193224090", Symbol: "فولاد", SectorCode: "27", CompanyCode: "IRO1FOLD"})
	// فولاد traded until four sessions ago, then was halted.
	var own []FlowSession
	for i := 4; i < 30; i++ {
		f := traded(m.days[i], 900, 100, 500, 500)
		own = append([]FlowSession{f}, own...)
		m.flow("46348559193224090", f)
	}
	b := m.build()
	// The roster's per-share windows would call its last stored row "1 session".
	if last := lastSessions(own, 1); len(last) != 1 || !last[0].Day.Equal(m.days[4]) {
		t.Fatal("lastSessions reaches back to the share's own last row")
	}
	it := shareIn(t, b, "27", 1, "46348559193224090")
	if it.Summary.Rows != 0 || it.Summary.NetIndividualToman != nil || it.Summary.InstrumentsTraded != 0 {
		t.Fatalf("window 1 must hold nothing for a halted share: %+v", it.Summary)
	}
	if w1 := sectorOf(t, b, "27").Windows["1"]; w1.InstrumentsTraded != 0 || w1.Rows != 0 {
		t.Fatalf("sector window 1: %+v", w1.TieredFlowSummary)
	}
	// Window 5 is sessions 0..4: exactly one of its rows, the fifth.
	w5 := shareIn(t, b, "27", 5, "46348559193224090").Summary
	if w5.Rows != 1 || num(t, "net", w5.NetIndividualToman) != 400 {
		t.Fatalf("window 5: %+v", w5)
	}
	if w5.Sessions != 5 || *w5.From != dayString(m.days[4]) || *w5.To != "2026-09-28" {
		t.Fatalf("window 5 is the market's five sessions: %v..%v", *w5.From, *w5.To)
	}
}

// --- sums --------------------------------------------------------------------------------

func rotatingMarket() *testMarket {
	m := newTestMarket(60)
	m.share(shareMeta{InsCode: "1", Symbol: "فولاد", SectorCode: "27", CompanyCode: "IRO1FOLD"})
	m.share(shareMeta{InsCode: "2", Symbol: "فملی", SectorCode: "27", CompanyCode: "IRO1MSMI"})
	m.share(shareMeta{InsCode: "3", Symbol: "وبملت", SectorCode: "57", CompanyCode: "IRO1BMLT"})
	for i, d := range m.days {
		// Basic metals trades 1,000 a session before the newest five and 3,000
		// in them; the bank trades 500 throughout.
		v := 1000.0
		if i < 5 {
			v = 3000
		}
		m.flow("1", traded(d, 0.6*v, 0.4*v, 0.5*v, 0.5*v)) // individuals net +0.1v
		m.flow("2", traded(d, 0.2*v, 0.8*v, 0.3*v, 0.7*v)) // net −0.1v
		m.flow("3", traded(d, 400, 100, 100, 400))         // net +300
	}
	return m
}

func TestSectorSumsAreTheSumsOfTheirShares(t *testing.T) {
	b := rotatingMarket().build()
	for _, n := range flowWindows {
		key := fmt.Sprint(n)
		var marketNet, marketValue float64
		var marketRows int
		for _, sec := range b.resp.Sectors {
			w := sec.Windows[key]
			var net, value float64
			var rows int
			for _, it := range b.shares[sec.SectorCode][n] {
				if it.Summary.NetIndividualToman != nil {
					net += *it.Summary.NetIndividualToman
					value += *it.Summary.TotalValueToman
				}
				rows += it.Summary.Rows
			}
			approx(t, sec.SectorCode+" net", w.NetIndividualToman, net, 1e-6)
			approx(t, sec.SectorCode+" value", w.TotalValueToman, value, 1e-6)
			if w.Rows != rows {
				t.Fatalf("%s rows %d, members %d", sec.SectorCode, w.Rows, rows)
			}
			marketNet += *w.NetIndividualToman
			marketValue += *w.TotalValueToman
			marketRows += w.Rows
		}
		mk := b.resp.Market[key]
		approx(t, "market net", mk.NetIndividualToman, marketNet, 1e-6)
		approx(t, "market value", mk.TotalValueToman, marketValue, 1e-6)
		if mk.Rows != marketRows || mk.InstrumentsTraded != minMarketShares+3 {
			t.Fatalf("market rows %d/%d instruments %d", mk.Rows, marketRows, mk.InstrumentsTraded)
		}
	}
	// Basic metals over five sessions: +0.1·3000·5 and −0.1·3000·5 cancel;
	// the bank is +300 a session.
	w5 := sectorOf(t, b, "27").Windows["5"]
	approx(t, "metals net", w5.NetIndividualToman, 0, 1e-9)
	if w5.InflowInstruments != 1 || w5.OutflowInstruments != 1 || w5.InstrumentsTraded != 2 {
		t.Fatalf("metals: in %d out %d", w5.InflowInstruments, w5.OutflowInstruments)
	}
	approx(t, "bank net", sectorOf(t, b, "57").Windows["5"].NetIndividualToman, 1500, 1e-9)
}

func TestInstitutionalFlowIsExactlyTheNegative(t *testing.T) {
	b := rotatingMarket().build()
	check := func(name string, s TieredFlowSummary) {
		if s.NetIndividualToman == nil {
			return
		}
		if s.NetInstitutionalToman == nil || *s.NetInstitutionalToman != -*s.NetIndividualToman {
			t.Fatalf("%s: institutional %v, individual %v", name, s.NetInstitutionalToman, *s.NetIndividualToman)
		}
	}
	for key, s := range b.resp.Market {
		check("market "+key, s)
	}
	for _, sec := range b.resp.Sectors {
		for key, w := range sec.Windows {
			check(sec.SectorCode+" "+key, w.TieredFlowSummary)
		}
	}
	for _, list := range b.shares["27"] {
		for _, it := range list {
			check(it.Symbol, it.Summary)
		}
	}
	if !contains(strings.Join(b.resp.Notes, " "), "not new money") {
		t.Fatal("the notes must say a net flow is a transfer, not new money")
	}
}

func TestValueSharesSumToOneHundred(t *testing.T) {
	b := rotatingMarket().build()
	for _, n := range flowWindows {
		total := 0.0
		for _, sec := range b.resp.Sectors {
			total += num(t, "share", sec.Windows[fmt.Sprint(n)].ValueSharePct)
		}
		if math.Abs(total-100) > 1e-4 {
			t.Fatalf("window %d: value shares sum to %v", n, total)
		}
	}
	for bi := range b.resp.Blocks {
		total := 0.0
		for _, sec := range b.resp.Sectors {
			total += num(t, "block share", sec.Blocks[bi].ValueSharePct)
		}
		if math.Abs(total-100) > 1e-4 {
			t.Fatalf("block %d: value shares sum to %v", bi, total)
		}
	}
	// Within a sector, its shares' parts of it sum to 100 too.
	total := 0.0
	for _, it := range b.shares["27"][20] {
		total += num(t, "share of sector", it.Summary.ValueSharePct)
	}
	if math.Abs(total-100) > 1e-4 {
		t.Fatalf("metals' shares sum to %v", total)
	}
}

func TestRotationIsMeasuredAgainstTheWindowBefore(t *testing.T) {
	b := rotatingMarket().build()
	// Five sessions: metals 2×3000×5 = 30,000, bank 2,500, filler 100,000.
	// The five before: metals 10,000, bank 2,500, filler 100,000.
	w5 := sectorOf(t, b, "27").Windows["5"]
	cur, prev := 30000.0/132500*100, 10000.0/112500*100
	approx(t, "share", w5.ValueSharePct, cur, 1e-5)
	approx(t, "previous share", w5.PreviousValueSharePct, prev, 1e-5)
	approx(t, "rotation", w5.ValueShareChangePP, cur-prev, 1e-5)
	// A bank that traded the same amount lost share because metals gained it.
	if d := num(t, "bank Δ", sectorOf(t, b, "57").Windows["5"].ValueShareChangePP); d >= 0 {
		t.Fatalf("bank Δ %v should be negative", d)
	}
	// Window 60 is all the calendar holds: no window before it.
	if sectorOf(t, b, "27").Windows["60"].ValueShareChangePP != nil {
		t.Fatal("sixty sessions of sixty have no previous window")
	}
}

func TestExcludedAndIdleRowsAreCountedButNotSummed(t *testing.T) {
	m := newTestMarket(5)
	m.share(shareMeta{InsCode: "1", Symbol: "فولاد", SectorCode: "27"})
	good := traded(m.days[0], 600, 400, 500, 500)
	bad := traded(m.days[1], 900, 100, 500, 500)
	bad.SellIVolume *= 1.5 // the volume identity fails
	checked := traded(m.days[2], 600, 400, 500, 500)
	checked.BarValue = ptr(1000)
	m.flow("1", good)
	m.flow("1", bad)
	m.flow("1", checked)
	m.flow("1", FlowSession{Day: m.days[3]})
	s := shareIn(t, m.build(), "27", 5, "1").Summary
	if s.Rows != 4 || s.BarChecked != 1 || s.IdentityOnly != 1 || s.Excluded != 1 || s.NoTrade != 1 || s.Consistent != 2 {
		t.Fatalf("tiers %+v", s)
	}
	approx(t, "net", s.NetIndividualToman, 200, 1e-9)
	approx(t, "value", s.TotalValueToman, 2000, 1e-9)
	if s.InflowSessions != 2 {
		t.Fatalf("inflow sessions %d", s.InflowSessions)
	}
}

// --- companies and the top lists -----------------------------------------------------------

func TestACompanysBoardsAreSummedAndShownByItsMainBoard(t *testing.T) {
	m := newTestMarket(5)
	m.share(shareMeta{InsCode: "11", Symbol: "فولاد", Board: "main", CompanyCode: "IRO1FOLD",
		SectorCode: "27", InRoster: true, RosterSymbol: "فولاد"})
	m.share(shareMeta{InsCode: "12", Symbol: "فولاد2", Board: "block", CompanyCode: "IRO1FOLD", SectorCode: "27"})
	m.share(shareMeta{InsCode: "13", Symbol: "فولاد3", Board: "secondary", CompanyCode: "IRO1FOLD", SectorCode: "27"})
	// A company with only a second board is shown by it.
	m.share(shareMeta{InsCode: "21", Symbol: "کاوه3", Board: "secondary", CompanyCode: "IRO1KAVE", SectorCode: "27"})
	for _, d := range m.days {
		m.flow("11", traded(d, 600, 400, 500, 500)) // +100
		m.flow("21", traded(d, 100, 900, 50, 950))  // +50
	}
	m.flow("12", traded(m.days[0], 5000, 0, 0, 5000)) // a block to individuals: +5000
	// فولاد3 is listed and never traded: no board of it is summed.
	b := m.build()
	top := b.resp.Top["5"].Inflow
	if len(top) != 2 {
		t.Fatalf("inflow list %d, want the two companies", len(top))
	}
	f := top[0]
	if f.CompanyCode != "IRO1FOLD" || f.Symbol != "فولاد" || f.InsCode != "11" || f.Board != "main" {
		t.Fatalf("company shown by %+v", f)
	}
	if strings.Join(f.Boards, ",") != "main,block" {
		t.Fatalf("boards %v", f.Boards)
	}
	approx(t, "company net", f.Summary.NetIndividualToman, 5*100+5000, 1e-9)
	approx(t, "company value", f.Summary.TotalValueToman, 5*1000+5000, 1e-9)
	if !f.InRoster || f.RosterSymbol != "فولاد" || f.SectorNameEN != "Basic metals" {
		t.Fatalf("roster %v %q sector %q", f.InRoster, f.RosterSymbol, f.SectorNameEN)
	}
	k := top[1]
	if k.Symbol != "کاوه3" || k.Board != "secondary" || k.InRoster || k.RosterSymbol != "" {
		t.Fatalf("second-board company %+v", k)
	}
	// Both boards are in the sector's own sum, each its own row in its list.
	approx(t, "sector net", sectorOf(t, b, "27").Windows["5"].NetIndividualToman, 5*100+5000+5*50, 1e-9)
	if it := shareIn(t, b, "27", 5, "13"); it.Summary.Rows != 0 || it.PriceChangeReason == "" {
		t.Fatalf("an untraded listed board is listed with empty figures: %+v", it)
	}
}

func TestACompanyIsShownByItsListedMainBoard(t *testing.T) {
	// market_shares never deletes: an insCode TSETMC re-issued under the same
	// insID leaves its old main board in the mirror, delisted and idle. The
	// old code sorts first as a string; it must not stand for the company.
	m := newTestMarket(5)
	m.shares = append(m.shares, shareMeta{InsCode: "100", Symbol: "قدیم", Board: "main",
		CompanyCode: "IRO1ABCD", SectorCode: "27", Market: "bourse", Listed: false})
	m.share(shareMeta{InsCode: "200", Symbol: "جدید", Board: "main", CompanyCode: "IRO1ABCD", SectorCode: "27"})
	for _, d := range m.days {
		m.flow("200", traded(d, 600, 400, 500, 500))
		m.prices = append(m.prices, priceRow{InsCode: "200", Day: d, Close: 1010, PriceYesterday: 1000})
	}
	top := m.build().resp.Top["5"].Inflow
	if len(top) != 1 || top[0].InsCode != "200" || top[0].Symbol != "جدید" {
		t.Fatalf("company shown by %+v, want the listed main board", top)
	}
	if top[0].PriceChangePct == nil {
		t.Fatalf("the shown board traded and has closes: %q", top[0].PriceChangeReason)
	}
	// Two listed main boards: the one that traded more in the window.
	m2 := newTestMarket(5)
	m2.share(shareMeta{InsCode: "100", Symbol: "الف", Board: "main", CompanyCode: "IRO1ABCD", SectorCode: "27"})
	m2.share(shareMeta{InsCode: "200", Symbol: "ب", Board: "main", CompanyCode: "IRO1ABCD", SectorCode: "27"})
	m2.flow("100", traded(m2.days[0], 60, 40, 50, 50))
	m2.flow("200", traded(m2.days[0], 600, 400, 500, 500))
	if top := m2.build().resp.Top["5"].Inflow; len(top) != 1 || top[0].InsCode != "200" {
		t.Fatalf("two listed main boards: shown %+v", top)
	}
}

func TestABoardIsNamedOnlyWhenSomethingOfItWasSummed(t *testing.T) {
	m := newTestMarket(5)
	m.share(shareMeta{InsCode: "11", Symbol: "فولاد", Board: "main", CompanyCode: "IRO1FOLD", SectorCode: "27"})
	m.share(shareMeta{InsCode: "12", Symbol: "فولاد2", Board: "block", CompanyCode: "IRO1FOLD", SectorCode: "27"})
	for _, d := range m.days {
		m.flow("11", traded(d, 600, 400, 500, 500))
	}
	m.flow("12", traded(m.days[0], 5000, 0, 0, 4000)) // buy 5000 against sell 4000: excluded
	f := m.build().resp.Top["5"].Inflow[0]
	if strings.Join(f.Boards, ",") != "main" {
		t.Fatalf("boards %v: the block's only row was excluded and nothing of it was summed", f.Boards)
	}
	approx(t, "company net", f.Summary.NetIndividualToman, 5*100, 1e-9)
	if f.Summary.Excluded != 1 {
		t.Fatalf("the excluded block row is still counted: %+v", f.Summary)
	}
}

func TestTheTopListsAreFifteenAndOrdered(t *testing.T) {
	var cs []companyFlowItem
	for i := 0; i < 20; i++ {
		net := float64(i%10) + 1 // pairs tie on net
		cs = append(cs, companyFlowItem{Symbol: fmt.Sprintf("s%02d", i),
			Summary: TieredFlowSummary{NetIndividualToman: fv(net), TotalValueToman: fv(float64(100 + i))}})
		cs = append(cs, companyFlowItem{Symbol: fmt.Sprintf("o%02d", i),
			Summary: TieredFlowSummary{NetIndividualToman: fv(-net), TotalValueToman: fv(100)}})
	}
	cs = append(cs, companyFlowItem{Symbol: "zero",
		Summary: TieredFlowSummary{NetIndividualToman: fv(0), TotalValueToman: fv(1)}})
	top := topOf(cs, topCompanies)
	if len(top.Inflow) != 15 || len(top.Outflow) != 15 {
		t.Fatalf("%d / %d", len(top.Inflow), len(top.Outflow))
	}
	// 10 twice: s19 (value 119) before s09 (value 109).
	if top.Inflow[0].Symbol != "s19" || top.Inflow[1].Symbol != "s09" {
		t.Fatalf("inflow head %s %s", top.Inflow[0].Symbol, top.Inflow[1].Symbol)
	}
	// Equal net and value: by symbol.
	if top.Outflow[0].Symbol != "o09" || top.Outflow[1].Symbol != "o19" {
		t.Fatalf("outflow head %s %s", top.Outflow[0].Symbol, top.Outflow[1].Symbol)
	}
	for _, list := range [][]companyFlowItem{top.Inflow, top.Outflow} {
		for _, c := range list {
			if c.Symbol == "zero" {
				t.Fatal("a company with no net flow is on neither list")
			}
		}
	}
}

// --- price change --------------------------------------------------------------------------

func TestPriceChangeIsChainedThroughACorporateAction(t *testing.T) {
	d1, d2, d3 := day("2026-09-26"), day("2026-09-27"), day("2026-09-28")
	prices := []priceRow{
		{Day: d1, Close: 1000, PriceYesterday: 980},
		// A 100% capital increase: TSETMC opens the ex-date at half the last
		// close. The share then ROSE 4% on the day.
		{Day: d2, Close: 520, PriceYesterday: 500},
		{Day: d3, Close: 530, PriceYesterday: 520},
	}
	got, why := chainLinkedChange(prices, []time.Time{d1, d2, d3}, d1, d3)
	if why != "" {
		t.Fatal(why)
	}
	want := (1000.0/980*520/500*530/520 - 1) * 100
	approx(t, "chained", got, want, 1e-6)
	// First close to last would have called it a 47% fall.
	if naive := (530.0/1000 - 1) * 100; *got < 0 || naive > -40 {
		t.Fatalf("chained %v against naive %v", *got, naive)
	}
	// Outside the window is outside the product.
	got, _ = chainLinkedChange(prices, []time.Time{d3}, d3, d3)
	approx(t, "one session", got, (530.0/520-1)*100, 1e-6)
}

func TestPriceChangeIsEmptyRatherThanPartial(t *testing.T) {
	d1, d2 := day("2026-09-27"), day("2026-09-28")
	prices := []priceRow{{Day: d2, Close: 530, PriceYesterday: 520}}
	if got, why := chainLinkedChange(prices, []time.Time{d1, d2}, d1, d2); got != nil || !contains(why, "1 of the 2") {
		t.Fatalf("a traded session with no close: %v %q", got, why)
	}
	if got, why := chainLinkedChange(nil, nil, d1, d2); got != nil || !contains(why, "did not trade") {
		t.Fatalf("no trade: %v %q", got, why)
	}
	if got, why := chainLinkedChange(nil, []time.Time{d2}, d1, d2); got != nil || why == "" {
		t.Fatalf("no price at all: %v %q", got, why)
	}
}

func TestAShareCarriesItsChainedChange(t *testing.T) {
	m := newTestMarket(5)
	m.share(shareMeta{InsCode: "1", Symbol: "فولاد", SectorCode: "27"})
	for _, d := range m.days {
		m.flow("1", traded(d, 600, 400, 500, 500))
		m.prices = append(m.prices, priceRow{InsCode: "1", Day: d, Close: 1010, PriceYesterday: 1000})
	}
	b := m.build()
	approx(t, "5 sessions", shareIn(t, b, "27", 5, "1").PriceChangePct, (math.Pow(1.01, 5)-1)*100, 1e-6)
	approx(t, "1 session", shareIn(t, b, "27", 1, "1").PriceChangePct, 1, 1e-6)
	// The filler has flow and no stored close: stated, never guessed.
	if it := shareIn(t, b, fillerSector, 5, "90000"); it.PriceChangePct != nil || it.PriceChangeReason == "" {
		t.Fatalf("filler price %+v", it)
	}
}

// thinMarket is k consecutive dates of which date `thin` has no filler row, so
// it is too thin to be a market session; share "1" trades every date.
func thinMarket(k, thin int) *testMarket {
	m := newTestMarket(k)
	kept := m.rows[:0]
	for _, r := range m.rows {
		if !r.Flow.Day.Equal(m.days[thin]) {
			kept = append(kept, r)
		}
	}
	m.rows = kept
	m.share(shareMeta{InsCode: "1", Symbol: "فولاد", SectorCode: "27"})
	for _, d := range m.days {
		m.flow("1", traded(d, 600, 400, 500, 500))
	}
	return m
}

func TestAThinDateInsideAWindowStillNeedsItsClose(t *testing.T) {
	// A date between two market sessions with too few traded shares to be one
	// is in no sum, but it lies inside the window's span of prices: a share
	// that traded on it moved there, and a product that skipped the day
	// would state a change that never happened.
	m := thinMarket(7, 2)
	for _, d := range m.days {
		if !d.Equal(m.days[2]) {
			m.prices = append(m.prices, priceRow{InsCode: "1", Day: d, Close: 1010, PriceYesterday: 1000})
		}
	}
	b := m.build()
	if b.resp.Coverage.ThinDatesSkipped != 1 || b.resp.Coverage.SessionsAvailable != 6 {
		t.Fatalf("calendar %+v", b.resp.Coverage)
	}
	it := shareIn(t, b, "27", 5, "1")
	if it.PriceChangePct != nil || !contains(it.PriceChangeReason, "1 of the 6") {
		t.Fatalf("a traded thin date with no close: %v %q", it.PriceChangePct, it.PriceChangeReason)
	}
	// The flow sums still hold the five market sessions only.
	if it.Summary.Rows != 5 {
		t.Fatalf("rows %d: the thin date is in no sum", it.Summary.Rows)
	}
	// Window 1 does not reach back to it.
	approx(t, "1 session", shareIn(t, b, "27", 1, "1").PriceChangePct, 1, 1e-6)

	// With its close stored, the thin date's move is in the product.
	m = thinMarket(7, 2)
	for _, d := range m.days {
		p := priceRow{InsCode: "1", Day: d, Close: 1010, PriceYesterday: 1000}
		if d.Equal(m.days[2]) {
			p.Close = 1500
		}
		m.prices = append(m.prices, p)
	}
	approx(t, "5 sessions and the thin date", shareIn(t, m.build(), "27", 5, "1").PriceChangePct,
		(math.Pow(1.01, 5)*1.5-1)*100, 1e-6)
}

// --- the sector index ------------------------------------------------------------------------

func TestTheSectorIndexIsMeasuredFromTheSessionBeforeTheWindow(t *testing.T) {
	m := rotatingMarket()
	m.sectorIndex["27"] = "idx27"
	for i := len(m.days) - 1; i >= 0; i-- {
		m.series["idx27"] = append(m.series["idx27"], IndexPoint{Day: m.days[i], Value: 1000 + float64(60-i)})
	}
	// A row dated after the newest flow session must not be the window's end.
	m.series["idx27"] = append(m.series["idx27"], IndexPoint{Day: newestDay.AddDate(0, 0, 1), Value: 5000})
	b := m.build()
	w1 := sectorOf(t, b, "27").Windows["1"]
	// Session 0 is 1060, session 1 is 1059.
	approx(t, "1 session", w1.IndexReturnPct, (1060.0/1059-1)*100, 1e-6)
	w5 := sectorOf(t, b, "27").Windows["5"]
	approx(t, "5 sessions", w5.IndexReturnPct, (1060.0/1055-1)*100, 1e-6)
	// Sixty sessions of sixty: the session before the first is not stored.
	if w60 := sectorOf(t, b, "27").Windows["60"]; w60.IndexReturnPct != nil || w60.IndexReturnReason == "" {
		t.Fatalf("60: %v %q", w60.IndexReturnPct, w60.IndexReturnReason)
	}
	if w := sectorOf(t, b, "57").Windows["5"]; w.IndexReturnPct != nil || !contains(w.IndexReturnReason, "no bourse sector index") {
		t.Fatalf("a sector with no index: %q", w.IndexReturnReason)
	}
}

// --- the heatmap -------------------------------------------------------------------------------

func TestBlocksAreFiveSessionsOldestFirst(t *testing.T) {
	b := rotatingMarket().build()
	if len(b.resp.Blocks) != blockCount || len(b.resp.MarketBlocks) != blockCount {
		t.Fatalf("%d blocks", len(b.resp.Blocks))
	}
	days := calendarDays(60)
	first, last := b.resp.Blocks[0], b.resp.Blocks[blockCount-1]
	if first.From != dayString(days[59]) || first.To != dayString(days[55]) ||
		last.From != dayString(days[4]) || last.To != "2026-09-28" {
		t.Fatalf("first %+v last %+v", first, last)
	}
	metals := sectorOf(t, b, "27")
	if len(metals.Blocks) != blockCount {
		t.Fatalf("metals blocks %d", len(metals.Blocks))
	}
	// The newest block is window 5 exactly; an older block is 5 × 2,000.
	nb := metals.Blocks[blockCount-1]
	approx(t, "newest block value", nb.TotalValueToman, *metals.Windows["5"].TotalValueToman, 1e-9)
	approx(t, "older block value", metals.Blocks[0].TotalValueToman, 10000, 1e-9)
	approx(t, "newest block share", nb.ValueSharePct, *metals.Windows["5"].ValueSharePct, 1e-6)
	bank := sectorOf(t, b, "57").Blocks[0]
	approx(t, "bank block pct", bank.NetIndividualPctOfValue, 1500.0/2500*100, 1e-6)
	// Twenty-three sessions hold four whole blocks, never a short fifth.
	if n := len(newTestMarket(23).build().resp.Blocks); n != 4 {
		t.Fatalf("23 sessions: %d blocks", n)
	}
}

// --- one sector's shares ------------------------------------------------------------------------

func TestASectorsShareListAnswersOrRefusesPlainly(t *testing.T) {
	m := rotatingMarket()
	m.share(shareMeta{InsCode: "4", Symbol: "فخوز", SectorCode: "27"}) // listed, never traded
	b := m.build()
	now := day("2026-09-29")
	resp, refusal := buildSectorShares(&b, "27", 5, now)
	if refusal != nil {
		t.Fatal(refusal.Message)
	}
	// By traded value; فملی and فولاد traded the same, so by symbol; the
	// share that never traded last.
	if resp.Count != 3 || resp.Items[0].Symbol != "فملی" || resp.Items[2].Symbol != "فخوز" {
		t.Fatalf("items %d, first %s, last %s", resp.Count, resp.Items[0].Symbol, resp.Items[resp.Count-1].Symbol)
	}
	if resp.NameEN != "Basic metals" || resp.Sector == nil || resp.Sessions.Sessions != 5 || resp.Window != "5" {
		t.Fatalf("header %+v", resp)
	}
	if resp.DataAge.AgeDays == nil || *resp.DataAge.AgeDays != 1 || !contains(resp.Notes[0], "SECTOR's traded value") {
		t.Fatal("the list carries its age and says what its value share is of")
	}
	if _, r := buildSectorShares(&b, "46", 5, now); r == nil || r.Status != 404 {
		t.Fatal("a sector with no stored share is a 404")
	}
	short := newTestMarket(10).build()
	if _, r := buildSectorShares(&short, fillerSector, 20, now); r == nil || r.Status != 409 || r.Message == "" {
		t.Fatal("a window longer than the calendar is refused, not shortened")
	}
	roster := &testMarket{days: calendarDays(3)}
	roster.share(shareMeta{InsCode: "1", Symbol: "فولاد", SectorCode: "27", InRoster: true})
	roster.flow("1", traded(newestDay, 600, 400, 500, 500))
	rb := roster.build()
	if resp, r := buildSectorShares(&rb, "27", 5, now); r != nil || len(resp.Items) != 0 || resp.Coverage.MarketWide {
		t.Fatal("before the market-wide ingest the list is empty and says why")
	}
}

func TestTheSameDataBuildsTheSameResponse(t *testing.T) {
	// Awkward magnitudes, so the order of a float sum's terms shows in its
	// last bit; Go randomises map order on every range.
	m := newTestMarket(20)
	for i, d := range m.days {
		for j := 0; j < 30; j++ {
			code := fmt.Sprintf("7%04d", j)
			if i == 0 {
				m.share(shareMeta{InsCode: code, Symbol: fmt.Sprintf("ن%d", j), SectorCode: "27"})
			}
			v := 1e9/float64(j+3) + float64(i)*1234.5678
			m.flow(code, traded(d, 0.7*v, 0.3*v, (0.7-0.013*float64(j%7))*v, (0.3+0.013*float64(j%7))*v))
		}
	}
	first, err := json.Marshal(m.build().resp)
	if err != nil {
		t.Fatal(err)
	}
	for k := 0; k < 5; k++ {
		again, _ := json.Marshal(m.build().resp)
		if string(again) != string(first) {
			t.Fatal("two builds of the same data differ")
		}
	}
}

// --- the contract ---------------------------------------------------------------------------

func TestTheResponseSerialisesAsDocumented(t *testing.T) {
	m := rotatingMarket()
	m.sectorIndex["27"] = "idx27"
	b := m.build()
	body, err := json.Marshal(b.resp)
	if err != nil {
		t.Fatal(err)
	}
	var raw map[string]any
	if err := json.Unmarshal(body, &raw); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"sessions", "market", "sectors", "blocks", "market_blocks", "top", "coverage", "checks", "notes", "data_age"} {
		if _, ok := raw[k]; !ok {
			t.Fatalf("top-level %q missing", k)
		}
	}
	sectors := raw["sectors"].([]any)
	var metals map[string]any
	for _, s := range sectors {
		if s.(map[string]any)["sector_code"] == "27" {
			metals = s.(map[string]any)
		}
	}
	w5 := metals["windows"].(map[string]any)["5"].(map[string]any)
	// The sector window is flat: the summary's fields beside the index's.
	for _, k := range []string{"net_individual_toman", "net_institutional_toman", "value_share_pct",
		"value_share_change_pp", "bar_checked", "identity_only", "excluded", "no_trade", "index_return_pct"} {
		if _, ok := w5[k]; !ok {
			t.Fatalf("sector window field %q missing", k)
		}
	}
	checks := raw["checks"].(map[string]any)
	if checks["identity_tolerance_pct"] != 0.1 || checks["session_value_tolerance_pct"] != 1.0 {
		t.Fatalf("checks %v", checks)
	}
	cov := raw["coverage"].(map[string]any)
	if cov["market_wide"] != true || cov["min_traded_shares"] != 200.0 || cov["floor"] != "2025-03-21" {
		t.Fatalf("coverage %v", cov)
	}
	notes := strings.Join(b.resp.Notes, " ")
	for _, want := range []string{"not new money", "None of them is a forecast",
		"classification at the last fetch", "delisted"} {
		if !contains(notes, want) {
			t.Fatalf("notes must say %q", want)
		}
	}
}

// --- the routes ---------------------------------------------------------------------------------

func TestTheShareListRefusesABadSectorOrWindowBeforeReadingAnything(t *testing.T) {
	// No pool: a refusal must come from the request alone.
	h := &Handler{}
	r := chi.NewRouter()
	r.Get("/api/v1/bourse/sector-flows/{sector}", h.SectorFlowShares)
	for _, c := range []struct{ path, want string }{
		{"/api/v1/bourse/sector-flows/7", "two-digit"},
		{"/api/v1/bourse/sector-flows/..%2F27", "two-digit"},
		{"/api/v1/bourse/sector-flows/abc", "two-digit"},
		{"/api/v1/bourse/sector-flows/27?window=7", "window must be one of"},
		{"/api/v1/bourse/sector-flows/27?window=5d", "window must be one of"},
	} {
		rec := httptest.NewRecorder()
		r.ServeHTTP(rec, httptest.NewRequest("GET", c.path, nil))
		if rec.Code != 400 || !contains(rec.Body.String(), c.want) {
			t.Fatalf("%s: %d %s", c.path, rec.Code, rec.Body.String())
		}
	}
}

func TestTheRosterTableNoLongerSaysTheMarketIsNotStored(t *testing.T) {
	// Once 0030 exists the roster's note would lie if it still said this
	// deployment stores no market-wide flow; it points at the market's route.
	out := buildFlowRoster(nil, nil, day("2026-09-29"))
	notes := strings.Join(out.Notes, " ")
	if contains(notes, "does not store the market") || !contains(notes, "/api/v1/bourse/sector-flows") ||
		!contains(notes, "not the market") {
		t.Fatalf("roster notes: %s", notes)
	}
}
