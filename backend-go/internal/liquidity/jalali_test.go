package liquidity

// The calendar is tested three ways: against dates whose Gregorian equivalent
// is printed on official documents (CBI's own month ends, Nowruz), at the
// boundaries the arithmetic can get wrong (a leap Esfand, the first and last
// day of a year), and exhaustively against every day of the research data:
// 3,973 TGJU rows that carry TGJU's own Jalali date beside the Gregorian one,
// and every month end CBI and tsdview printed for 1352..1404.

import (
	"encoding/csv"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

func date(s string) time.Time {
	d, err := time.Parse(dateLayout, s)
	if err != nil {
		panic(err)
	}
	return d
}

func readCSV(t *testing.T, name string) []map[string]string {
	t.Helper()
	f, err := os.Open("testdata/" + name)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	recs, err := csv.NewReader(f).ReadAll()
	if err != nil {
		t.Fatal(err)
	}
	out := make([]map[string]string, 0, len(recs)-1)
	for _, r := range recs[1:] {
		m := map[string]string{}
		for i, h := range recs[0] {
			m[h] = r[i]
		}
		out = append(out, m)
	}
	return out
}

func TestNowruzKnownYears(t *testing.T) {
	cases := map[int]string{
		1352: "1973-03-21",
		1390: "2011-03-21",
		1394: "2015-03-21",
		1395: "2016-03-20",
		1399: "2020-03-20",
		1400: "2021-03-21",
		1403: "2024-03-20",
		1404: "2025-03-21",
		1405: "2026-03-21",
		1408: "2029-03-20",
	}
	for jy, want := range cases {
		got, ok := nowruz(jy)
		if !ok || got.Format(dateLayout) != want {
			t.Errorf("1 Farvardin %d = %s, want %s", jy, got.Format(dateLayout), want)
		}
	}
}

func TestLeapEsfand(t *testing.T) {
	// 1399 and 1403 have a 30th of Esfand; 1404 does not. 1403 -> 1408 is the
	// five-year gap the 33-year cycle puts between two leap years.
	for _, jy := range []int{1370, 1375, 1379, 1383, 1387, 1391, 1395, 1399, 1403, 1408} {
		if !jalaliLeap(jy) {
			t.Errorf("%d should be a leap year", jy)
		}
	}
	for _, jy := range []int{1396, 1397, 1398, 1400, 1401, 1402, 1404, 1405, 1406, 1407} {
		if jalaliLeap(jy) {
			t.Errorf("%d should not be a leap year", jy)
		}
	}
	got, err := JalaliToGregorian(1403, 12, 30)
	if err != nil || got.Format(dateLayout) != "2025-03-20" {
		t.Errorf("30 Esfand 1403 = %v (%v), want 2025-03-20", got, err)
	}
	got, err = JalaliToGregorian(1399, 12, 30)
	if err != nil || got.Format(dateLayout) != "2021-03-20" {
		t.Errorf("30 Esfand 1399 = %v (%v), want 2021-03-20", got, err)
	}
	if _, err := JalaliToGregorian(1404, 12, 30); err == nil {
		t.Error("30 Esfand 1404 does not exist and must be refused, not rolled into Farvardin")
	}
	if NewMonth(1404, 12).End().Format(dateLayout) != "2026-03-20" {
		t.Errorf("Esfand 1404 ends %s, want 2026-03-20 (the date CBI prints)", NewMonth(1404, 12).End())
	}
	if NewMonth(1403, 12).Days() != 30 || NewMonth(1404, 12).Days() != 29 {
		t.Error("Esfand lengths wrong")
	}
}

func TestNowruzBoundary(t *testing.T) {
	cases := []struct {
		greg       string
		jy, jm, jd int
	}{
		{"2025-03-20", 1403, 12, 30}, // last day of a leap year
		{"2025-03-21", 1404, 1, 1},
		{"2026-03-20", 1404, 12, 29}, // last day of a common year
		{"2026-03-21", 1405, 1, 1},
		{"2025-09-22", 1404, 6, 31}, // last 31-day month
		{"2025-09-23", 1404, 7, 1},
		{"2026-10-10", 1405, 7, 18}, // the research anchor
		{"2011-11-26", 1390, 9, 5},  // TGJU's first row
	}
	for _, c := range cases {
		jy, jm, jd, err := GregorianToJalali(date(c.greg))
		if err != nil || jy != c.jy || jm != c.jm || jd != c.jd {
			t.Errorf("%s -> %d/%d/%d (%v), want %d/%d/%d", c.greg, jy, jm, jd, err, c.jy, c.jm, c.jd)
		}
		back, err := JalaliToGregorian(c.jy, c.jm, c.jd)
		if err != nil || back.Format(dateLayout) != c.greg {
			t.Errorf("%d/%d/%d -> %v (%v), want %s", c.jy, c.jm, c.jd, back, err, c.greg)
		}
	}
}

func TestMonthLabelsStepAcrossTheYear(t *testing.T) {
	m, err := ParseMonth("1404-01")
	if err != nil {
		t.Fatal(err)
	}
	if got := m.Add(-1).String(); got != "1403-12" {
		t.Errorf("1404-01 - 1 = %s, want 1403-12", got)
	}
	if got := m.Add(-12).String(); got != "1403-01" {
		t.Errorf("1404-01 - 12 = %s, want 1403-01", got)
	}
	if got := m.Add(23).String(); got != "1405-12" {
		t.Errorf("1404-01 + 23 = %s, want 1405-12", got)
	}
	for _, bad := range []string{"1404-13", "1404-00", "1404/01", "14040-1", "", "2026-03-20"} {
		if _, err := ParseMonth(bad); err == nil {
			t.Errorf("ParseMonth(%q) should be refused", bad)
		}
	}
	if s := NewMonth(1405, 7).Start().Format(dateLayout); s != "2026-09-23" {
		t.Errorf("Mehr 1405 starts %s, want 2026-09-23", s)
	}
}

func TestEveryResearchDayMatchesTGJUsJalaliDate(t *testing.T) {
	recs := readCSV(t, "usd_irt_daily_toman.csv")
	if len(recs) != 3973 {
		t.Fatalf("fixture has %d rows, want 3973", len(recs))
	}
	for _, r := range recs {
		jy, jm, jd, err := GregorianToJalali(date(r["g"]))
		if err != nil {
			t.Fatal(err)
		}
		want := strings.Split(r["j"], "/")
		y, _ := strconv.Atoi(want[0])
		m, _ := strconv.Atoi(want[1])
		d, _ := strconv.Atoi(want[2])
		if jy != y || jm != m || jd != d {
			t.Fatalf("%s -> %d/%02d/%02d, TGJU says %s", r["g"], jy, jm, jd, r["j"])
		}
	}
}

func TestEveryPrintedMonthEndMatches(t *testing.T) {
	// CBI's Excel editions and tsdview print the Gregorian date each Jalali
	// month ends on: 1352..1404, quarter-ends before 1397-04 and every month
	// after, eight leap Esfands among them.
	n := 0
	for _, r := range readCSV(t, "money_monthly.csv") {
		if r["gregorian_month_end"] == "" {
			continue
		}
		m, err := ParseMonth(r["jalali_month"])
		if err != nil {
			t.Fatal(err)
		}
		if got := m.End().Format(dateLayout); got != r["gregorian_month_end"] {
			t.Errorf("%s ends %s, the source printed %s", m, got, r["gregorian_month_end"])
		}
		n++
	}
	if n < 250 {
		t.Fatalf("only %d month ends checked", n)
	}
}

func TestTehranToday(t *testing.T) {
	// 21:00 UTC on 20 March is already 00:30 on 21 March in Tehran: Nowruz.
	got := tehranToday(time.Date(2026, 3, 20, 21, 0, 0, 0, time.UTC))
	if got.Format(dateLayout) != "2026-03-21" {
		t.Errorf("tehranToday = %s, want 2026-03-21", got.Format(dateLayout))
	}
}
