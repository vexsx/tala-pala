package liquidity

// The dollar, monthly: USD_IRT's daily closes grouped into Jalali months.
//
// THE DAY. A daily close is internal/relvalue's: the last observation of the
// UTC day, by the one statement (dailyCloseSelect) /market/candles and every
// return in that package are built from, so a month here and a candle there
// cannot disagree about what a day's close was. Each UTC day is then placed in
// the Jalali month its civil date falls in. That is the Tehran calendar date
// for the TGJU history, whose rows are stamped 23:00 UTC on TGJU's own date
// (02:30 Tehran the next morning -- flooring those to Tehran days would move
// every one of them onto the following day); for the live 24/7 USDT era it is
// the UTC day, which ends at 03:30 Tehran, so a day's close is the last quote
// before 03:30 rather than before midnight -- three and a half hours, which
// can move one close across a month boundary once a month at most. The
// research bucketed by TGJU's own Jalali date, and
// TestEveryResearchDayMatchesTGJUsJalaliDate shows the two agree on all 3,973
// days.
//
// THE MONTH. The monthly average is the mean of the month's daily closes and
// the month-end close is its last one. A month counts as COMPLETE -- and only
// a complete month enters the model -- when it has ended and the stored
// history covers it from its start. "Ended" is read on the same clock as the
// buckets: the month is before the one containing the present UTC day. For
// the three and a half hours each night that Tehran is already on the next
// date, the month's last UTC day is still collecting quotes, so its
// month-end close is not final yet; reading "today" in Tehran would call the
// month complete with a close that later quotes replace. The history's first
// month is partial when its first close is later than the third day: TGJU's
// history begins 1390/09/05, and the research dropped that 16-day month for
// the same reason.

import (
	"math"
	"sort"
	"time"
)

// dayClose is one UTC day's close, in toman.
type dayClose struct {
	Day   time.Time
	Close float64
}

// usdMonth is one Jalali month of the dollar.
type usdMonth struct {
	Month      Month
	Avg        float64
	Close      float64
	CloseDay   time.Time
	FirstDay   time.Time
	Days       int
	Complete   bool
	Incomplete string // why a month is not complete; "" when it is
}

// firstMonthSlackDays is how late in its first month the history may begin
// and the month still count as covered: a month whose first two days were a
// Friday and a holiday has no close on them and is not partial.
const firstMonthSlackDays = 3

// bucketUSD groups daily closes into Jalali months. `utcToday` is the UTC
// civil date of the present -- the bucket still forming -- and its month is
// still running. Non-positive closes are stored faults, not prices, and are
// left out and counted.
func bucketUSD(days []dayClose, utcToday time.Time) (months []usdMonth, dropped int) {
	sorted := append([]dayClose(nil), days...)
	sort.SliceStable(sorted, func(i, j int) bool { return sorted[i].Day.Before(sorted[j].Day) })
	current, err := MonthOf(utcToday)
	if err != nil {
		current = Month(math.MaxInt32)
	}
	idx := map[Month]int{}
	for _, d := range sorted {
		if d.Close <= 0 || math.IsNaN(d.Close) || math.IsInf(d.Close, 0) {
			dropped++
			continue
		}
		m, err := MonthOf(d.Day)
		if err != nil {
			dropped++
			continue
		}
		i, ok := idx[m]
		if !ok {
			idx[m] = len(months)
			months = append(months, usdMonth{Month: m, FirstDay: d.Day})
			i = len(months) - 1
		}
		u := &months[i]
		u.Avg += d.Close // a running sum until the division below
		u.Close = d.Close
		u.CloseDay = d.Day
		u.Days++
	}
	for i := range months {
		u := &months[i]
		u.Avg /= float64(u.Days)
		switch {
		case u.Month >= current:
			u.Incomplete = "the month is still running"
		case i == 0 && daysBetween(u.Month.Start(), u.FirstDay) >= firstMonthSlackDays:
			u.Incomplete = "the stored history begins on " + u.FirstDay.Format(dateLayout) +
				", part-way through the month"
		default:
			u.Complete = true
		}
	}
	return months, dropped
}
