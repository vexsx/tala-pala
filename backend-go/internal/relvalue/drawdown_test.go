package relvalue

import (
	"math"
	"strings"
	"testing"
	"time"
)

// ser builds a daily series from consecutive closes starting at `start`.
func ser(start time.Time, closes ...float64) dailySeries {
	out := make(dailySeries, 0, len(closes))
	for i, c := range closes {
		out = append(out, dailyPoint{Day: start.AddDate(0, 0, i), Close: c})
	}
	return out
}

func TestDrawdownFindsThePeakTroughAndRecovery(t *testing.T) {
	// 100 -> 50 (a 50% fall) -> back to 100 on day 6.
	s := ser(day(2020, time.January, 1), 100, 80, 50, 70, 90, 100, 110)
	got := computeDrawdown(s)

	if got.MaxPct == nil || math.Abs(*got.MaxPct+50) > 1e-9 {
		t.Fatalf("max drawdown = %v, want -50", got.MaxPct)
	}
	if got.PeakDate == nil || !got.PeakDate.Equal(day(2020, time.January, 1)) {
		t.Errorf("peak = %v, want 2020-01-01", got.PeakDate)
	}
	if got.TroughDate == nil || !got.TroughDate.Equal(day(2020, time.January, 3)) {
		t.Errorf("trough = %v, want 2020-01-03", got.TroughDate)
	}
	// Recovery is at-or-above the old high: matching it IS recovery.
	if got.RecoveredDate == nil || !got.RecoveredDate.Equal(day(2020, time.January, 6)) {
		t.Errorf("recovered = %v, want 2020-01-06", got.RecoveredDate)
	}
	if got.RecoveryDays == nil || *got.RecoveryDays != 3 {
		t.Errorf("recovery days = %v, want 3 (trough 01-03 -> 01-06)", got.RecoveryDays)
	}
	if got.UnderwaterDays == nil || *got.UnderwaterDays != 5 {
		t.Errorf("underwater days = %v, want 5 (peak 01-01 -> 01-06)", got.UnderwaterDays)
	}
	if got.StillUnderwater {
		t.Error("it recovered, so it is not still under water")
	}
	// It ended at a new high, so the current drawdown is zero.
	if got.CurrentPct == nil || *got.CurrentPct != 0 {
		t.Errorf("current drawdown = %v, want 0 at a new high", got.CurrentPct)
	}
}

// The number a holder actually asks for: still waiting, and for how long.
func TestAnUnrecoveredFallSaysSoAndCountsTheWait(t *testing.T) {
	s := ser(day(2020, time.January, 1), 100, 40, 45, 50, 60)
	got := computeDrawdown(s)

	if !got.StillUnderwater {
		t.Fatal("the peak of 100 was never regained")
	}
	if got.RecoveredDate != nil || got.RecoveryDays != nil {
		t.Error("an unrecovered fall must not report a recovery date or duration")
	}
	if got.UnderwaterDays == nil || *got.UnderwaterDays != 4 {
		t.Errorf("underwater days = %v, want 4 (peak -> last observation)", got.UnderwaterDays)
	}
	if !strings.Contains(got.Note, "has NOT regained") {
		t.Errorf("the note must say it never came back: %s", got.Note)
	}
}

// The deepest drawdown is not the lowest close. A series that falls hard from
// an early peak, then makes a HIGHER peak and eases slightly, has its worst
// drawdown at the first trough even though the later dip's close is numerically
// greater.
func TestTheWorstDrawdownIsNotTheLowestClose(t *testing.T) {
	s := ser(day(2020, time.January, 1),
		100, 50, // -50% from 100
		200, 180, // -10% from 200; close 180 > 50 but the fall is smaller
	)
	got := computeDrawdown(s)
	if got.MaxPct == nil || math.Abs(*got.MaxPct+50) > 1e-9 {
		t.Fatalf("max drawdown = %v, want -50: the worst FALL, not the lowest close",
			got.MaxPct)
	}
	if got.TroughDate == nil || !got.TroughDate.Equal(day(2020, time.January, 2)) {
		t.Errorf("trough = %v, want the first dip", got.TroughDate)
	}
	// It ends 10% below the running peak of 200.
	if got.CurrentPct == nil || math.Abs(*got.CurrentPct+10) > 1e-9 {
		t.Errorf("current drawdown = %v, want -10", got.CurrentPct)
	}
}

func TestAMonotonicSeriesReportsZeroNotNull(t *testing.T) {
	got := computeDrawdown(ser(day(2020, time.January, 1), 10, 20, 30, 40))
	if got.MaxPct == nil || *got.MaxPct != 0 {
		t.Fatalf("max drawdown = %v, want a measured 0 -- it never fell", got.MaxPct)
	}
	if got.StillUnderwater {
		t.Error("a series at its high is not under water")
	}
	if !strings.Contains(got.Note, "never closed below") {
		t.Errorf("note should state the measured zero: %s", got.Note)
	}
}

func TestTooShortIsARefusal(t *testing.T) {
	for _, s := range []dailySeries{nil, ser(day(2020, time.January, 1), 100)} {
		got := computeDrawdown(s)
		if got.MaxPct != nil {
			t.Errorf("one point cannot produce a drawdown, got %v", *got.MaxPct)
		}
		if !strings.Contains(got.Note, "fewer than two") {
			t.Errorf("the refusal must say why: %s", got.Note)
		}
	}
}

// A window that opens mid-fall understates the drawdown, and the shape that
// gives it away is the peak sitting on the first observation.
func TestAWindowOpeningMidFallSaysTheDrawdownMayBeTruncated(t *testing.T) {
	got := computeDrawdown(ser(day(2020, time.January, 1), 90, 80, 70, 75))
	if !strings.Contains(got.Note, "FIRST observation") {
		t.Errorf("the note must warn the peak may predate the window: %s", got.Note)
	}

	// And must NOT say it when the peak is genuinely inside the window.
	inside := computeDrawdown(ser(day(2020, time.January, 1), 90, 120, 60, 80))
	if strings.Contains(inside.Note, "FIRST observation") {
		t.Errorf("the peak is inside this window; the warning is wrong here: %s", inside.Note)
	}
}

// The two disclosures that make the numbers readable at all.
func TestTheNoteStatesNominalAndCalendarDays(t *testing.T) {
	got := computeDrawdown(ser(day(2020, time.January, 1), 100, 50, 60))
	if !strings.Contains(got.Note, "CALENDAR days") {
		t.Errorf("days must be disambiguated from trading sessions: %s", got.Note)
	}
	if !strings.Contains(got.Note, "NOMINAL") {
		t.Errorf("under Iranian inflation a nominal recovery is not a recovery, and the "+
			"note must say these are nominal: %s", got.Note)
	}
	if !strings.Contains(got.Note, "manufacture troughs") {
		t.Errorf("the note must explain why a real-terms drawdown is refused rather "+
			"than silently absent: %s", got.Note)
	}
}

// Non-positive closes are skipped rather than used: a zero would make the
// ratio to the peak -100% and invent a total loss.
func TestNonPositiveClosesAreSkipped(t *testing.T) {
	got := computeDrawdown(ser(day(2020, time.January, 1), 100, 0, 90, 95))
	if got.MaxPct == nil {
		t.Fatal("the positive closes still form a drawdown")
	}
	if *got.MaxPct < -50 {
		t.Errorf("max drawdown = %v: a zero close must not be read as a 100%% fall",
			*got.MaxPct)
	}
}

// Two drawdown implementations now live in this package: maxDrawdownPct, which
// relative.go still uses for the RATIO series, and computeDrawdown, which the
// performance table uses because it also needs the dates. They must agree on
// the one number they both produce, or two places in the same response will
// quote different worst falls for the same prices.
func TestBothDrawdownImplementationsAgree(t *testing.T) {
	cases := []dailySeries{
		ser(day(2020, time.January, 1), 100, 80, 50, 70, 90, 100, 110),
		ser(day(2020, time.January, 1), 100, 40, 45, 50, 60),
		ser(day(2020, time.January, 1), 100, 50, 200, 180),
		ser(day(2020, time.January, 1), 10, 20, 30, 40),
		ser(day(2020, time.January, 1), 90, 80, 70, 75),
		ser(day(2020, time.January, 1), 5, 5, 5, 5),
		ser(day(2020, time.January, 1), 1000, 1, 1000),
	}
	for i, s := range cases {
		old := maxDrawdownPct(s)
		got := computeDrawdown(s).MaxPct
		switch {
		case old == nil && got == nil:
		case old == nil || got == nil:
			t.Errorf("case %d: maxDrawdownPct=%v computeDrawdown=%v — one refused and the "+
				"other answered", i, old, got)
		case math.Abs(*old-*got) > 1e-9:
			t.Errorf("case %d: maxDrawdownPct=%v computeDrawdown=%v — the same prices must "+
				"produce the same worst fall wherever it is computed", i, *old, *got)
		}
	}
}
