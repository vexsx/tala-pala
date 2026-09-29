package bourse

import (
	"errors"
	"strings"
	"testing"
	"time"
)

// The Trade chart's read of one index: the store's own lookup and gate.

func chartStore(t *testing.T) *indexStore {
	t.Helper()
	refused, reason := "refused", "after correction the newest value is 10.0 times the live figure"
	ok := validatedRow(TEDPIX)
	ok.NameEN, ok.Weighting, ok.ReturnBasis = "TEDPIX, all-share", "cap", "total"
	rescaled := 3
	ok.RowsRescaled = &rescaled
	empty := validatedRow("5798407779416661")
	st := &indexStore{
		key: "k1",
		byCode: map[string]indexRow{
			TEDPIX:              ok,
			"71704845530629737": {InsCode: "71704845530629737", NameFA: "شاخص بازار دوم", Status: &refused, RefusalReason: &reason},
			"43685683301327984": {InsCode: "43685683301327984", NameFA: "شاخص کل فرابورس"},
			"5798407779416661":  empty,
		},
		series: map[string][]IndexPoint{
			TEDPIX: loadExported(t, TEDPIX),
			// A refused index's series is never loaded; this one is present
			// only to prove the verdict, not the data, decides.
			"71704845530629737": loadExported(t, "71704845530629737"),
		},
	}
	return st
}

func TestAChartSeriesIsTheStoresOwnSlice(t *testing.T) {
	st := chartStore(t)
	info, pts, err := chartSeriesFrom(st, TEDPIX)
	if err != nil {
		t.Fatal(err)
	}
	// The same backing array: no copy per request, and — the other half of
	// that bargain — a caller must never write to it.
	if &pts[0] != &st.series[TEDPIX][0] {
		t.Fatal("the chart must read the store's series, not a reloaded copy")
	}
	if info.CheckStatus != "validated" || info.RowsRescaled != 3 || info.Weighting != "cap" ||
		info.ReturnBasis != "total" || info.NameEN != "TEDPIX, all-share" {
		t.Fatalf("info = %+v", info)
	}
	// The whole-series flags survive: the 2026 closure is marked from its
	// first repeated session, whatever page a chart later cuts.
	unchanged := 0
	for _, p := range pts {
		if p.Unchanged {
			unchanged++
		}
	}
	if unchanged != 72 {
		t.Fatalf("unchanged = %d, want the store's 72", unchanged)
	}
}

func TestAChartSeriesIsRefusedForTheSameReasonsAsThePage(t *testing.T) {
	st := chartStore(t)
	// Not in the store: a missing or DISABLED index (the store holds enabled
	// rows only) is unknown, exactly as /bourse/indices/{code}/history says.
	if _, _, err := chartSeriesFrom(st, "11111111111111111"); !errors.Is(err, ErrUnknownIndex) {
		t.Fatalf("missing: %v", err)
	}
	info, pts, err := chartSeriesFrom(st, "71704845530629737")
	if !errors.Is(err, ErrIndexRefused) || pts != nil {
		t.Fatalf("refused verdict: %v, %d points", err, len(pts))
	}
	if info.CheckStatus != "refused" || info.RefusalReason == "" {
		t.Fatalf("the refusal must carry its verdict: %+v", info)
	}
	if info, _, err := chartSeriesFrom(st, "43685683301327984"); !errors.Is(err, ErrIndexRefused) ||
		info.CheckStatus != statusNeverIngested {
		t.Fatalf("never ingested: %v %+v", err, info)
	}
	if _, _, err := chartSeriesFrom(st, "5798407779416661"); !errors.Is(err, ErrIndexRefused) {
		t.Fatalf("validated with no stored session: %v", err)
	}
}

func TestAChartIsAgedByTheIndexItDraws(t *testing.T) {
	day := func(s string) time.Time {
		d, err := time.Parse(dateLayout, s)
		if err != nil {
			t.Fatal(err)
		}
		return d
	}
	const sector = "11111111111111111"
	st := &indexStore{series: map[string][]IndexPoint{
		TEDPIX: {{Day: day("2026-09-27"), Value: 1}, {Day: day("2026-09-28"), Value: 2}},
		// An index the last fetch did not bring up to date: its line stops on
		// the 10th while the market's runs to the 28th.
		sector: {{Day: day("2026-09-09"), Value: 1}, {Day: day("2026-09-10"), Value: 2}},
	}}
	now := day("2026-09-29").Add(9 * time.Hour)

	age := chartDataAge(st, st.series[sector], now)
	if age.NewestTradeDate == nil || *age.NewestTradeDate != "2026-09-10" {
		t.Fatalf("the status bar says where THIS line ends: %+v", age)
	}
	if !age.Stale || !strings.Contains(age.Warning, "2026-09-28") ||
		!strings.Contains(age.Warning, "did not bring this index up to date") {
		t.Fatalf("a lagging index is stale, and says it lags the market: %+v", age)
	}
	if !strings.Contains(age.Note, "THIS index") {
		t.Fatalf("note = %q", age.Note)
	}

	current := chartDataAge(st, st.series[TEDPIX], now)
	if current.Stale || current.Warning != "" || *current.NewestTradeDate != "2026-09-28" ||
		*current.AgeDays != 1 {
		t.Fatalf("an index as current as the market is not stale: %+v", current)
	}
}
