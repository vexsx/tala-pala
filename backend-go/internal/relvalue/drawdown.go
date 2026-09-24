package relvalue

// The drawdown, and the question a percentage alone cannot answer: HOW LONG.
//
// max_drawdown_pct has always been on this table. On its own it says an asset
// fell 60% at some point in the window, which a reader can only act on if they
// also know whether that was a bad fortnight or a decade they are still living
// in. Those are not the same fact and this deployment can tell them apart: the
// peak, the trough, whether the peak was ever regained, and how long each leg
// took.
//
// WHY THIS IS NOMINAL, AND SAYS SO
//
// Every figure here is computed on the nominal series, and in an economy
// running at Iranian inflation rates a nominal recovery is not a recovery.
// Getting back to the same toman price after three years is a large REAL loss,
// and the table beside this one already reports real returns.
//
// A real-terms drawdown is not offered, and that is a refusal rather than an
// omission. It would need a daily real series, and the deflator this platform
// carries is MONTHLY. Holding the month's index level flat across its days --
// the only non-inventing way to apply it -- would make the price level jump at
// every reference-period boundary, so a month with 5% inflation would appear in
// the real series as a 5% single-day fall. That is a manufactured drawdown, and
// a drawdown table whose troughs are partly artifacts of the deflator's
// frequency is worse than no table. Interpolating the index onto days to smooth
// it would be inventing an intra-month price path nobody measured.
//
// WHAT "DAYS" MEANS HERE
//
// Calendar days between observations, not trading sessions. A reader asking
// "how long was I underwater" is asking about their own life, not about
// exchange sessions, and the Tehran market is closed for two of every seven
// days plus a long Nowruz -- so a session count would understate the wait by
// about a third. The field names say `days` and this comment is what makes
// that unambiguous.
//
// WHAT THE WINDOW DOES TO IT
//
// The peak is the highest close INSIDE THE WINDOW. A series that enters its
// window already falling has an in-window peak lower than its real one, so the
// drawdown is understated; the note says when the peak is the first
// observation, which is the shape that gives it away.

import (
	"fmt"
	"math"
	"time"
)

// drawdownResult is one asset's worst fall and the time it took.
type drawdownResult struct {
	MaxPct        *float64
	PeakDate      *time.Time
	TroughDate    *time.Time
	RecoveredDate *time.Time
	// RecoveryDays is trough -> recovery: how long the climb back took.
	RecoveryDays *int
	// UnderwaterDays is peak -> recovery, or peak -> last observation when the
	// peak has not been regained. It is the number a holder actually felt.
	UnderwaterDays  *int
	StillUnderwater bool
	// CurrentPct is the last close against the highest close before it. Zero
	// means the series ends at a new high.
	CurrentPct *float64
	Note       string
}

// computeDrawdown finds the deepest peak-to-trough fall in the window and the
// time either side of it. Pure function (unit tested): no clock, no database.
//
// One pass, tracking the running peak. The trough is the point that produced
// the worst ratio to the peak that preceded IT -- not the lowest close in the
// window, which is a different and usually wrong answer: a series that falls
// 50% from an early peak and later rises to a higher peak before easing 10%
// has its worst drawdown at the first trough, even if the second dip's close
// is numerically higher than the first's.
func computeDrawdown(s dailySeries) drawdownResult {
	if len(s) < 2 {
		return drawdownResult{Note: "no drawdown: fewer than two observations in this window."}
	}

	peak := math.Inf(-1)
	var peakDate time.Time
	worst := 0.0
	var troughDate, worstPeakDate time.Time
	found := false

	for _, p := range s {
		if p.Close <= 0 {
			continue
		}
		if p.Close > peak {
			peak, peakDate = p.Close, p.Day
		}
		if math.IsInf(peak, -1) || peak <= 0 {
			continue
		}
		if d := (p.Close/peak - 1) * 100; d < worst {
			worst, troughDate, worstPeakDate = d, p.Day, peakDate
			found = true
		}
	}

	if math.IsInf(peak, -1) {
		return drawdownResult{Note: "no drawdown: this window holds no positive close."}
	}
	if !found {
		// Monotonically non-falling: a real answer, not a missing one.
		zero := 0.0
		return drawdownResult{
			MaxPct:     &zero,
			CurrentPct: &zero,
			Note: "no drawdown: this series never closed below a previous close inside " +
				"the window.",
		}
	}

	out := drawdownResult{
		MaxPct:     fp(worst),
		PeakDate:   &worstPeakDate,
		TroughDate: &troughDate,
	}

	// The peak level to regain is the close ON the peak date, and recovery is
	// the first close at or above it AFTER the trough. At-or-above rather than
	// strictly above: matching the old high IS recovery.
	var peakClose float64
	for _, p := range s {
		if p.Day.Equal(worstPeakDate) {
			peakClose = p.Close
			break
		}
	}
	for _, p := range s {
		if !p.Day.After(troughDate) || p.Close < peakClose {
			continue
		}
		d := p.Day
		out.RecoveredDate = &d
		rec := daysBetween(troughDate, d)
		out.RecoveryDays = &rec
		uw := daysBetween(worstPeakDate, d)
		out.UnderwaterDays = &uw
		break
	}
	if out.RecoveredDate == nil {
		out.StillUnderwater = true
		uw := daysBetween(worstPeakDate, s[len(s)-1].Day)
		out.UnderwaterDays = &uw
	}

	// Where the series ENDS relative to the highest close before it, which is
	// a different question from the worst fall and the one a holder asks now.
	runPeak := math.Inf(-1)
	for _, p := range s {
		if p.Close > runPeak {
			runPeak = p.Close
		}
	}
	last := s[len(s)-1].Close
	if runPeak > 0 {
		out.CurrentPct = fp((last/runPeak - 1) * 100)
	}

	out.Note = drawdownNote(s, out, worstPeakDate)
	return out
}

// drawdownNote states the two things that make the numbers readable: that they
// are nominal, and whether the window truncated the fall.
func drawdownNote(s dailySeries, r drawdownResult, peakDate time.Time) string {
	parts := []string{}

	if r.StillUnderwater && r.UnderwaterDays != nil {
		parts = append(parts, fmt.Sprintf(
			"Fell %.1f%% from %s to %s and has NOT regained that level: %d calendar days "+
				"under water so far.",
			math.Abs(*r.MaxPct), dayString(peakDate), dayString(*r.TroughDate),
			*r.UnderwaterDays))
	} else if r.RecoveredDate != nil && r.RecoveryDays != nil && r.UnderwaterDays != nil {
		parts = append(parts, fmt.Sprintf(
			"Fell %.1f%% from %s to %s and regained that level on %s: %d calendar days to "+
				"climb back, %d under water in total.",
			math.Abs(*r.MaxPct), dayString(peakDate), dayString(*r.TroughDate),
			dayString(*r.RecoveredDate), *r.RecoveryDays, *r.UnderwaterDays))
	}

	// Calendar days, not sessions -- the Tehran market is shut two days in
	// seven plus Nowruz, so a session count understates the wait by about a
	// third and this is the number a holder actually lived through.
	parts = append(parts, "Days are CALENDAR days, not trading sessions.")

	// NOMINAL. Under Iranian inflation a nominal recovery is not a recovery.
	parts = append(parts, "These are NOMINAL prices: regaining a toman level after years "+
		"of inflation is still a large real loss, and a real-terms drawdown is not "+
		"offered because the monthly deflator would inject a step at every "+
		"reference-period boundary and manufacture troughs that never happened.")

	if len(s) > 0 && peakDate.Equal(s[0].Day) {
		parts = append(parts, "The peak is this window's FIRST observation, so the fall may "+
			"have begun before the window: widen it to see the real peak.")
	}
	return joinSentences(parts)
}

func joinSentences(parts []string) string {
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += " "
		}
		out += p
	}
	return out
}
