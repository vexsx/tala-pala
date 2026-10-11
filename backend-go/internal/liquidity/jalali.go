package liquidity

// The Jalali (Solar Hijri) calendar, implemented here because Go has none and
// because every number this package publishes is keyed by a Jalali month.
//
// THE METHOD. The year boundary (Nowruz, 1 Farvardin) is computed by the
// arithmetic K. M. Borkowski published in "The Persian calendar for 3000
// years", Earth, Moon and Planets 74 (1996) 223-230. The official calendar is
// astronomical -- a year starts on the day whose noon in Tehran follows the
// March equinox -- and Borkowski showed it is reproduced exactly for Jalali
// years 1..3177 by 33-year leap cycles (eight leap years each) that restart at
// a short list of "break" years, where a cycle is cut short by the drift of
// the equinox. Counting the leap years since the first break, and the
// Gregorian leap years over the same span, gives the March day on which each
// Jalali year begins. Everything else follows from that one date:
//
//   - months 1-6 have 31 days, 7-11 have 30, and Esfand (12) has 29, or 30 in
//     a leap year;
//   - a year is a leap year exactly when the NEXT Nowruz is 366 days later,
//     so leap-ness is derived from the boundary rather than computed by a
//     second rule that could disagree with it.
//
// This is not the 2820-year "Birashk" arithmetic some libraries use: that one
// is a different calendar that disagrees with the official one in several
// years of this century. The tests pin the boundaries that matter here (1399
// and 1403 are leap years, 1404 is not) against dates printed by CBI and
// against every day of the research data's TGJU history.
//
// Gregorian dates are civil dates. They are carried as time.Time at 00:00 UTC
// because that is how a stored DATE column and a UTC day bucket arrive; no
// time of day or timezone is implied.

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// jalaliBreaks are the Jalali years at which Borkowski's 33-year leap pattern
// restarts. Between two breaks the pattern repeats every 33 years; the span
// from a break to the next is what decides how many leap years precede a
// given year.
var jalaliBreaks = [...]int{
	-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
	1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178,
}

// minJalaliYear and maxJalaliYear bound the years the arithmetic is valid for.
const (
	minJalaliYear = 1
	maxJalaliYear = 3177
)

// nowruz returns the Gregorian date of 1 Farvardin of Jalali year jy, or false
// outside the valid range.
func nowruz(jy int) (time.Time, bool) {
	if jy < minJalaliYear || jy > maxJalaliYear {
		return time.Time{}, false
	}
	// Leap years of the Jalali calendar counted from the first break, with
	// the -14 offset that aligns the count to the Gregorian count below.
	leapJ := -14
	prev := jalaliBreaks[0]
	span := 0
	for i := 1; i < len(jalaliBreaks); i++ {
		next := jalaliBreaks[i]
		span = next - prev
		if jy < next {
			break
		}
		// A whole segment between two breaks: eight leap years per full
		// 33-year cycle, then one per four years of the remainder.
		leapJ += span/33*8 + span%33/4
		prev = next
	}
	n := jy - prev
	leapJ += n/33*8 + (n%33+3)/4
	// A segment whose remainder is 4 years long ends in a leap year that the
	// per-four-years count above misses.
	if span%33 == 4 && span-n == 4 {
		leapJ++
	}
	gy := jy + 621
	// Gregorian leap days over the same span, with the century rule.
	leapG := gy/4 - (gy/100+1)*3/4 - 150
	march := 20 + leapJ - leapG
	return time.Date(gy, time.March, march, 0, 0, 0, 0, time.UTC), true
}

// jalaliLeap reports whether Jalali year jy has a 30-day Esfand.
func jalaliLeap(jy int) bool {
	a, ok1 := nowruz(jy)
	b, ok2 := nowruz(jy + 1)
	if !ok1 || !ok2 {
		return false
	}
	return daysBetween(a, b) == 366
}

// jalaliMonthLength is the number of days in month jm of year jy.
func jalaliMonthLength(jy, jm int) int {
	switch {
	case jm >= 1 && jm <= 6:
		return 31
	case jm >= 7 && jm <= 11:
		return 30
	case jm == 12:
		if jalaliLeap(jy) {
			return 30
		}
		return 29
	}
	return 0
}

// JalaliToGregorian converts a Jalali civil date to its Gregorian date. A day
// that does not exist (30 Esfand of a common year, month 13) is an error, not
// a rollover into the next month.
func JalaliToGregorian(jy, jm, jd int) (time.Time, error) {
	start, ok := nowruz(jy)
	if !ok {
		return time.Time{}, fmt.Errorf("jalali year %d is outside %d..%d", jy, minJalaliYear, maxJalaliYear)
	}
	if jm < 1 || jm > 12 {
		return time.Time{}, fmt.Errorf("jalali month %d is not 1..12", jm)
	}
	if jd < 1 || jd > jalaliMonthLength(jy, jm) {
		return time.Time{}, fmt.Errorf("%04d/%02d has %d days, not %d",
			jy, jm, jalaliMonthLength(jy, jm), jd)
	}
	return start.AddDate(0, 0, dayOfJalaliYear(jm, jd)), nil
}

// dayOfJalaliYear is the zero-based day of the year: 0 on 1 Farvardin.
func dayOfJalaliYear(jm, jd int) int {
	if jm <= 6 {
		return (jm-1)*31 + jd - 1
	}
	return 6*31 + (jm-7)*30 + jd - 1
}

// GregorianToJalali converts the civil date of t (read in UTC) to Jalali.
func GregorianToJalali(t time.Time) (jy, jm, jd int, err error) {
	u := t.UTC()
	d := time.Date(u.Year(), u.Month(), u.Day(), 0, 0, 0, 0, time.UTC)
	jy = d.Year() - 621
	start, ok := nowruz(jy)
	if !ok {
		return 0, 0, 0, fmt.Errorf("%s is outside the supported range", d.Format(dateLayout))
	}
	if d.Before(start) {
		jy--
		if start, ok = nowruz(jy); !ok {
			return 0, 0, 0, fmt.Errorf("%s is outside the supported range", d.Format(dateLayout))
		}
	}
	doy := daysBetween(start, d)
	if doy < 6*31 {
		return jy, doy/31 + 1, doy%31 + 1, nil
	}
	doy -= 6 * 31
	return jy, doy/30 + 7, doy%30 + 1, nil
}

// daysBetween is the whole number of days from a to b; both are civil dates
// at 00:00 UTC, so this is exact.
func daysBetween(a, b time.Time) int {
	return int(b.Sub(a).Hours() / 24)
}

// Month is one Jalali month as a running index: year*12 + (month-1). Stepping
// a label is integer arithmetic on it, so 1404-01 minus one is 1403-12 by
// construction and never by Gregorian date arithmetic -- a Jalali month is 29
// to 31 days and starts in the middle of a Gregorian one.
type Month int

// NewMonth builds a month from its year and month-of-year.
func NewMonth(jy, jm int) Month { return Month(jy*12 + jm - 1) }

// ParseMonth reads the publisher's label, YYYY-MM.
func ParseMonth(label string) (Month, error) {
	y, m, ok := strings.Cut(strings.TrimSpace(label), "-")
	if !ok || len(y) != 4 || len(m) != 2 {
		return 0, fmt.Errorf("%q is not a Jalali month label (YYYY-MM)", label)
	}
	jy, err1 := strconv.Atoi(y)
	jm, err2 := strconv.Atoi(m)
	if err1 != nil || err2 != nil || jm < 1 || jm > 12 || jy < minJalaliYear || jy >= maxJalaliYear {
		return 0, fmt.Errorf("%q is not a Jalali month label (YYYY-MM)", label)
	}
	return NewMonth(jy, jm), nil
}

// MonthOf is the Jalali month containing the civil date of t (read in UTC).
func MonthOf(t time.Time) (Month, error) {
	jy, jm, _, err := GregorianToJalali(t)
	if err != nil {
		return 0, err
	}
	return NewMonth(jy, jm), nil
}

// Year is the Jalali year.
func (m Month) Year() int { return int(m) / 12 }

// Of is the month of the year, 1..12.
func (m Month) Of() int { return int(m)%12 + 1 }

// Add steps by n months, across year boundaries.
func (m Month) Add(n int) Month { return m + Month(n) }

// String is the label, YYYY-MM.
func (m Month) String() string { return fmt.Sprintf("%04d-%02d", m.Year(), m.Of()) }

// Start is the Gregorian date of the month's first day.
func (m Month) Start() time.Time {
	t, _ := JalaliToGregorian(m.Year(), m.Of(), 1)
	return t
}

// End is the Gregorian date of the month's last day -- the day an
// end-of-month stock is measured at.
func (m Month) End() time.Time {
	t, _ := JalaliToGregorian(m.Year(), m.Of(), jalaliMonthLength(m.Year(), m.Of()))
	return t
}

// Days is the month's length.
func (m Month) Days() int { return jalaliMonthLength(m.Year(), m.Of()) }

// tehran is the civil timezone the Jalali calendar is kept in. A fixed +03:30,
// as internal/prices/funds.go uses: Iran has observed no daylight saving time
// since 2022, and the only instant this package reads in Tehran time is the
// present one (which month is still running).
var tehran = time.FixedZone("Asia/Tehran", 3*3600+30*60)

// tehranToday is the Tehran civil date of instant t, as a 00:00 UTC date.
func tehranToday(t time.Time) time.Time {
	l := t.In(tehran)
	return time.Date(l.Year(), l.Month(), l.Day(), 0, 0, 0, 0, time.UTC)
}
