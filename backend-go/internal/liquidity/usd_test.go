package liquidity

import (
	"testing"
	"time"
)

func TestBucketUSDAverageCloseAndCompleteness(t *testing.T) {
	// Shahrivar 1405 is 2026-08-23..2026-09-22; Mehr starts 2026-09-23.
	days := []dayClose{
		{date("2026-09-22"), 300}, // last day of Shahrivar, given out of order
		{date("2026-08-23"), 100},
		{date("2026-09-01"), 200},
		{date("2026-09-23"), 400}, // 1 Mehr
		{date("2026-09-24"), 0},   // a stored fault
	}
	months, dropped := bucketUSD(days, date("2026-10-11"))
	if dropped != 1 {
		t.Errorf("dropped = %d, want the one zero close", dropped)
	}
	if len(months) != 2 {
		t.Fatalf("%d months", len(months))
	}
	sh := months[0]
	if sh.Month.String() != "1405-06" || sh.Avg != 200 || sh.Close != 300 || sh.Days != 3 ||
		sh.CloseDay.Format(dateLayout) != "2026-09-22" {
		t.Errorf("Shahrivar = %+v, want avg 200, close 300 on its last day", sh)
	}
	if !sh.Complete {
		t.Errorf("Shahrivar started on its first day and has ended: complete, got %q", sh.Incomplete)
	}
	if months[1].Month.String() != "1405-07" || months[1].Complete {
		t.Errorf("Mehr 1405 is still running on 2026-10-11 and must not be complete: %+v", months[1])
	}
}

// TGJU's history starts 1390/09/05: that month's average is 16 days', not the
// month's, and the research dropped it. A start on the 2nd or 3rd (a Friday, a
// holiday) is still the whole month.
func TestBucketUSDFirstMonthPartial(t *testing.T) {
	late, _ := bucketUSD([]dayClose{{date("2011-11-26"), 1370}, {date("2011-12-22"), 1400}}, date("2026-10-11"))
	if late[0].Month.String() != "1390-09" || late[0].Complete || late[1].Month.String() != "1390-10" ||
		!late[1].Complete {
		t.Fatalf("months = %+v", late)
	}
	start := NewMonth(1390, 9).Start() // 2011-11-22
	early, _ := bucketUSD([]dayClose{{start.AddDate(0, 0, 2), 1370}}, date("2026-10-11"))
	if !early[0].Complete {
		t.Fatalf("a history starting on the 3rd covers the month: %+v", early[0])
	}
}

// The buckets are UTC days, so the running month is read on the UTC clock.
// At 2026-09-22T21:00Z Tehran is already on 1 Mehr, but the UTC day 31
// Shahrivar is still collecting quotes: Shahrivar's month-end close is not
// final, and the month must not enter the model yet. At 00:00 UTC it does.
func TestBucketUSDRunningMonthFollowsTheUTCBucket(t *testing.T) {
	days := []dayClose{{date("2026-08-23"), 1}, {date("2026-09-22"), 2}}
	late := time.Date(2026, 9, 22, 21, 0, 0, 0, time.UTC)
	if months, _ := bucketUSD(days, utcDay(late)); months[0].Complete {
		t.Fatalf("Shahrivar is complete at %s while its last UTC day is still open: %+v", late, months[0])
	}
	next := time.Date(2026, 9, 23, 0, 0, 0, 0, time.UTC)
	if months, _ := bucketUSD(days, utcDay(next)); !months[0].Complete {
		t.Fatalf("Shahrivar should be complete at %s: %+v", next, months[0])
	}
	// The whole response uses that clock: the history marks the month partial.
	resp := buildDollarResponse(dollarInput{Now: late, USD: days, M2: unregistered(CodeLiquidity)}, 12)
	if len(resp.History) == 0 || resp.History[0].Period != "1405-06" || !resp.History[0].Partial {
		t.Fatalf("history %+v", resp.History)
	}
}
