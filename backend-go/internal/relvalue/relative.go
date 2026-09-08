package relvalue

// GET /api/v1/relative-value -- two instruments indexed to 100 on the same day,
// and how far apart they have travelled since.
//
// There is no ?numeraire= here, and its absence is the point. Indexing both
// legs to 100 at a shared base date cancels their units, so the comparison is
// unit-free by construction and a toman-quoted gold price can be set beside a
// dollar-quoted one without either being converted or the two being mixed. A
// numeraire would add a third series' noise to a question that does not need
// one.
//
// The pairing is an INTERSECTION of observation days: only days on which BOTH
// instruments actually quoted enter the series. Carrying a stale close forward
// to manufacture a pair would put a flat day into one leg and a real move into
// the other, which shows up as relative performance that never happened. Days
// dropped for want of a partner are counted and reported.
//
// The register is descriptive throughout. "IR_GOLD_18K has lagged USD_IRT by
// 18% over this window" is a statement about a measured past. "is due to catch
// up" is a forecast about a future, this package does not make those, and there
// is deliberately no p-value, no z-score against a fitted distribution and no
// projection anywhere below.

import (
	"fmt"
	"math"
	"net/http"
	"net/url"
	"sort"
	"strconv"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

const (
	defaultRelativePoints = 500
	// minRelativePoints is 2, not 1. A "series" of one point cannot carry the
	// contract pickIndices states -- first and last, always -- and the base day
	// is the day every indexed value in the response is measured from, so a
	// single-point answer would be a chart of one number whose meaning depends
	// on a day it does not contain. ?points=1 is refused rather than quietly
	// served as the last observation alone.
	minRelativePoints = 2
	maxRelativePoints = 2000
)

// minIndependentWindows is the evidence gate on gap_percentile.
//
// A rolling-window percentile has two denominators and they differ by orders of
// magnitude. Ten years of daily history yields roughly 2,800 overlapping
// one-year windows -- and ten INDEPENDENT ones, because consecutive overlapping
// windows share 364 of their 365 days and are very nearly the same
// observation. Reporting the 2,800 as a sample size would make three years of
// data look like a large study.
//
// Five is the bar. Below it the percentile is withheld: "the current gap is at
// the 95th percentile" computed from three independent windows says only that
// the gap is the largest of three, which is what a fair coin does 1 time in 3.
// The number is not weak evidence about the level, it is no evidence about it,
// and publishing it beside a real measurement invites it to be read as one.
//
// This is the same refusal, on the same reasoning, that MIN_SCORED_FOR_COVERAGE
// makes in prediction-python/app/models/intervals.py: a rate whose denominator
// cannot support it is not published at all. The threshold differs because the
// denominators differ; the rule does not.
const minIndependentWindows = 5

// --- request --------------------------------------------------------------------

// relativeQuery is a validated /relative-value request.
type relativeQuery struct {
	A      string
	B      string
	Window window
	Points int
}

// parseRelativeQuery validates the query string. Pure function (unit tested):
// the caller supplies `now`, and instrument codes are checked against the
// registry later, by the handler, because that needs the database.
func parseRelativeQuery(q url.Values, now time.Time) (relativeQuery, *paramError) {
	out := relativeQuery{Points: defaultRelativePoints}

	out.A, out.B = q.Get("a"), q.Get("b")
	for _, p := range []struct{ name, raw string }{{"a", out.A}, {"b", out.B}} {
		if p.raw == "" {
			return out, badParam(
				fmt.Sprintf("%s is required: /relative-value compares two instruments", p.name),
				map[string]any{p.name: p.raw})
		}
	}
	if out.A == out.B {
		return out, badParam(
			fmt.Sprintf("a and b must be different instruments, both were %q: an "+
				"instrument indexed against itself is 100 on every day by construction",
				out.A),
			map[string]any{"a": out.A, "b": out.B})
	}

	w, perr := parseWindow(q.Get("period"), q.Get("from"), q.Get("to"), now)
	if perr != nil {
		return out, perr
	}
	out.Window = w

	if raw := q.Get("points"); raw != "" {
		n, err := strconv.Atoi(raw)
		if err != nil {
			return out, badParam(
				fmt.Sprintf("points must be an integer, got %q", raw),
				map[string]any{"points": raw})
		}
		if n < minRelativePoints || n > maxRelativePoints {
			return out, badParam(
				fmt.Sprintf("points must be between %d and %d, got %d",
					minRelativePoints, maxRelativePoints, n),
				map[string]any{"points": raw})
		}
		out.Points = n
	}
	return out, nil
}

// --- pairing ----------------------------------------------------------------------

// pairedPoint is one day on which BOTH instruments quoted.
type pairedPoint struct {
	Day  time.Time
	A, B float64
}

type pairedSeries []pairedPoint

// pairing is the intersection plus an account of what it cost.
type pairing struct {
	Points pairedSeries
	// UnpairedA counts days a quoted and b did not, and vice versa. Reported,
	// not hidden: a comparison of a 24/7 series against a Tehran-session one
	// discards the weekend, and the reader should see how much of the history
	// that was.
	UnpairedA int
	UnpairedB int
}

// pairSeries intersects two ascending daily series on the day. Pure function
// (unit tested). A non-positive close on either side removes the day: it cannot
// be indexed, and it cannot sit in a denominator.
func pairSeries(a, b dailySeries) pairing {
	out := pairing{Points: make(pairedSeries, 0, min(len(a), len(b)))}
	i, j := 0, 0
	for i < len(a) && j < len(b) {
		switch {
		case a[i].Day.Before(b[j].Day):
			out.UnpairedA++
			i++
		case b[j].Day.Before(a[i].Day):
			out.UnpairedB++
			j++
		default:
			if a[i].Close > 0 && b[j].Close > 0 {
				out.Points = append(out.Points, pairedPoint{Day: a[i].Day, A: a[i].Close, B: b[j].Close})
			}
			i++
			j++
		}
	}
	out.UnpairedA += len(a) - i
	out.UnpairedB += len(b) - j
	return out
}

// slice keeps the paired days inside [from, to]; a nil `from` means "from the
// start of the paired history".
func (p pairedSeries) slice(from *time.Time, to time.Time) pairedSeries {
	out := make(pairedSeries, 0, len(p))
	var lo time.Time
	if from != nil {
		lo = floorDay(*from)
	}
	for _, pt := range p {
		if from != nil && pt.Day.Before(lo) {
			continue
		}
		if pt.Day.After(to) {
			continue
		}
		out = append(out, pt)
	}
	return out
}

// --- the gap and its distribution -------------------------------------------------

// windowGap is the cumulative relative performance between two paired points:
// each leg's growth from the base, combined by the gap formula. Pure function
// (unit tested).
func windowGap(base, end pairedPoint) (float64, bool) {
	if base.A <= 0 || base.B <= 0 {
		return 0, false
	}
	aGrowth := end.A/base.A - 1
	bGrowth := end.B/base.B - 1
	g := gapPct(aGrowth, bGrowth)
	if math.IsNaN(g) || math.IsInf(g, 0) {
		return 0, false
	}
	return g, true
}

// percentileBasis is the evidence behind gap_percentile -- published whether or
// not the percentile itself is.
//
// Both counts are here on purpose. OverlappingWindows is the number of rolling
// windows that exist and is large; IndependentWindows is how many
// non-overlapping windows the history actually contains and is the honest
// sample size. A reader shown only the first would conclude the percentile
// rests on hundreds of observations.
type percentileBasis struct {
	WindowDays int `json:"window_days"`
	// WindowDaysTolerance is how far short of WindowDays a comparison window's
	// two ends may be and still be counted as one. Published because the claim
	// "windows of window_days" is only as strong as the tolerance behind it.
	WindowDaysTolerance int `json:"window_days_tolerance"`
	OverlappingWindows  int `json:"overlapping_windows"`
	// IndependentWindows is COUNTED from the windows that actually qualified,
	// by walking them and keeping each one that starts at or after the previous
	// kept one ended. It is therefore never larger than OverlappingWindows --
	// which it used to be able to be, because it was history_days/window_days,
	// a count of windows the calendar could hold rather than of windows the data
	// contains.
	IndependentWindows    int `json:"independent_windows"`
	MinIndependentWindows int `json:"min_independent_windows"`
	// RejectedShortWindows counts candidate windows discarded because their two
	// ends were not really window_days apart -- the sparse-history case, where
	// "the last observation at or before the horizon" can be the very next day.
	RejectedShortWindows int    `json:"rejected_short_windows"`
	HistoryDays          int    `json:"history_days"`
	Sufficient           bool   `json:"sufficient"`
	Note                 string `json:"note"`
}

// windowLengthTolerancePct is how far short of the requested length a
// comparison window may fall and still count as a window of that length.
//
// It exists because "the last observation at or before the horizon" is not the
// same thing as "an observation near the horizon", and on a sparse history the
// two differ by months. Three two-day clusters spread over 2001 days used to
// produce two comparison windows that each spanned a SINGLE DAY, published
// under a note asserting they were 365-day windows, with a percentile computed
// from them.
//
// Ten percent, floored at one day, is the bar. A tolerance rather than an exact
// match, because a market that did not quote on the horizon date is ordinary --
// a one-year window ending on the Friday before is still a year -- while one
// ending eleven months early is a different measurement wearing the same label.
const windowLengthTolerancePct = 10

func windowLengthTolerance(windowDays int) int {
	if t := windowDays * windowLengthTolerancePct / 100; t > 1 {
		return t
	}
	return 1
}

// gapDistribution computes the current gap's percentile against every rolling
// window of the same length in the FULL paired history, and the evidence behind
// it. Pure function (unit tested).
//
// A comparison window starts at each paired day whose horizon (start +
// windowDays) still lies inside the history, and ends at the last observation
// at or before that horizon -- AND ONLY COUNTS IF THOSE TWO ENDS ARE REALLY
// windowDays APART, within windowLengthTolerance. Windows are therefore aligned
// to days the market actually quoted, none is padded out to a length the data
// does not reach, and -- the part this used to get wrong -- none is counted as a
// window of the requested length when it is a fraction of it.
//
// The percentile is the share of those windows whose gap was at or below the
// current one. It is withheld -- nil -- unless the INDEPENDENT window count
// clears minIndependentWindows. See that constant for why.
func gapDistribution(full pairedSeries, windowDays int, current float64) (*float64, percentileBasis) {
	basis := percentileBasis{
		WindowDays:            windowDays,
		MinIndependentWindows: minIndependentWindows,
	}
	if len(full) >= 2 {
		basis.HistoryDays = daysBetween(full[0].Day, full[len(full)-1].Day)
	}
	if windowDays < 1 {
		basis.Note = "No percentile: the requested window spans less than one whole day, " +
			"so there is no window length to compare it against."
		return nil, basis
	}
	tol := windowLengthTolerance(windowDays)
	basis.WindowDaysTolerance = tol

	// comparison is one qualifying window: where it began, where it ended, and
	// the gap over it. The dates are kept because the independent count is a
	// walk over them, not a division.
	type comparison struct {
		start, end time.Time
		gap        float64
	}
	windows := make([]comparison, 0, len(full))
	if len(full) >= 2 {
		last := full[len(full)-1].Day
		j := 0
		for i := 0; i < len(full); i++ {
			horizon := full[i].Day.AddDate(0, 0, windowDays)
			if horizon.After(last) {
				break
			}
			if j < i {
				j = i
			}
			for j+1 < len(full) && !full[j+1].Day.After(horizon) {
				j++
			}
			if j == i {
				continue
			}
			if daysBetween(full[i].Day, full[j].Day) < windowDays-tol {
				basis.RejectedShortWindows++
				continue
			}
			g, ok := windowGap(full[i], full[j])
			if !ok {
				continue
			}
			windows = append(windows, comparison{start: full[i].Day, end: full[j].Day, gap: g})
		}
	}
	basis.OverlappingWindows = len(windows)

	// The independent count is a greedy walk over the windows that actually
	// qualified: keep the first, then keep each window that starts at or after
	// the last kept one ended. It was HistoryDays/windowDays, which counts the
	// windows a dense history WOULD hold -- five of them, on a history whose two
	// real comparison windows spanned a day each -- and which could exceed the
	// overlapping count, a number that cannot be true of any real record.
	gaps := make([]float64, 0, len(windows))
	var lastEnd time.Time
	for i, w := range windows {
		gaps = append(gaps, w.gap)
		if i == 0 || !w.start.Before(lastEnd) {
			basis.IndependentWindows++
			lastEnd = w.end
		}
	}
	basis.Sufficient = basis.IndependentWindows >= minIndependentWindows && len(gaps) > 0

	if !basis.Sufficient {
		basis.Note = fmt.Sprintf(
			"gap_percentile is WITHHELD. Overlapping windows are not independent "+
				"observations: consecutive %d-day windows share all but a day or two of "+
				"their history. This paired history spans %d day(s) and yields %d "+
				"comparison window(s) whose two ends really are %d(+/-%d) days apart, %d "+
				"of them non-overlapping, against a minimum of %d -- so a percentile from "+
				"it would be noise wearing a statistic. %d candidate window(s) were "+
				"rejected as too short: a window counts only when the observations at BOTH "+
				"ends are that far apart, never merely because the calendar span could "+
				"hold one. Every count is published so the denominator can be judged "+
				"rather than assumed.",
			windowDays, basis.HistoryDays, basis.OverlappingWindows, windowDays, tol,
			basis.IndependentWindows, minIndependentWindows, basis.RejectedShortWindows)
		return nil, basis
	}

	sorted := append([]float64(nil), gaps...)
	sort.Float64s(sorted)
	atOrBelow := sort.SearchFloat64s(sorted, math.Nextafter(current, math.Inf(1)))
	pct := fp(float64(atOrBelow) / float64(len(sorted)) * 100)
	basis.Note = fmt.Sprintf(
		"Percentile of the current gap among %d overlapping window(s) whose two ends are "+
			"%d(+/-%d) days apart, drawn from %d day(s) of paired history; %d candidate "+
			"window(s) were rejected as too short. Those windows are NOT independent -- %d "+
			"of them are non-overlapping -- so read it as a description of where this "+
			"window sits in the record, not as a probability.",
		basis.OverlappingWindows, windowDays, tol, basis.HistoryDays,
		basis.RejectedShortWindows, basis.IndependentWindows)
	return pct, basis
}

// --- decimation ---------------------------------------------------------------------

// pickIndices selects at most `points` evenly spaced indices from n, always
// including the first and the last. Pure function (unit tested).
//
// This is DECIMATION, not resampling: every returned point is an observation
// that happened, on the day it happened. Nothing is averaged into a bucket and
// nothing is interpolated onto a grid, so a chart drawn from a decimated series
// plots real closes and merely plots fewer of them.
func pickIndices(n, points int) []int {
	if n <= 0 {
		return nil
	}
	if points >= n {
		out := make([]int, n)
		for i := range out {
			out[i] = i
		}
		return out
	}
	// The contract two paragraphs up is "first and last, always". A single point
	// cannot honour it, so the HTTP layer refuses ?points=1 (minRelativePoints)
	// and this returns the smallest set that can: both ends. It used to return
	// only the LAST index, which contradicted the doc comment above it and
	// dropped the base day the whole series is indexed against.
	if points <= 1 {
		if n == 1 {
			return []int{0}
		}
		return []int{0, n - 1}
	}
	out := make([]int, 0, points)
	prev := -1
	for i := 0; i < points; i++ {
		idx := int(math.Round(float64(i) * float64(n-1) / float64(points-1)))
		if idx > prev {
			out = append(out, idx)
			prev = idx
		}
	}
	return out
}

// --- wire shapes ----------------------------------------------------------------------

type relativePoint struct {
	T        int64   `json:"t"`
	Date     string  `json:"date"`
	AIndexed float64 `json:"a_indexed"`
	BIndexed float64 `json:"b_indexed"`
	Ratio    float64 `json:"ratio"`
	GapPct   float64 `json:"gap_pct"`
}

type relativeResponse struct {
	A instrumentRef `json:"a"`
	B instrumentRef `json:"b"`
	// Period is null when the caller gave an explicit from/to.
	Period   *string         `json:"period"`
	BaseDate *time.Time      `json:"base_date"`
	From     *time.Time      `json:"from"`
	To       time.Time       `json:"to"`
	AsOf     time.Time       `json:"as_of"`
	Series   []relativePoint `json:"series"`
	// Observations is how many paired days the window holds; len(Series) may be
	// smaller when ?points= decimated it, and both are published so a client
	// never mistakes a thinned chart for a thin history.
	Observations int `json:"observations"`

	AGrowthPct *float64 `json:"a_growth_pct"`
	BGrowthPct *float64 `json:"b_growth_pct"`
	GapPct     *float64 `json:"gap_pct"`
	// GapPercentile is null unless PercentileBasis.Sufficient.
	GapPercentile    *float64        `json:"gap_percentile"`
	PercentileBasis  percentileBasis `json:"percentile_basis"`
	RatioDrawdownPct *float64        `json:"ratio_drawdown_pct"`
	Warnings         []string        `json:"warnings"`
}

// --- pure assembly ---------------------------------------------------------------------

// relativeInputs is everything the response is built from, already fetched.
type relativeInputs struct {
	Query relativeQuery
	A     instrumentRow
	B     instrumentRow
	// SeriesA and SeriesB are FULL histories. The window is applied here, and
	// the percentile basis deliberately is not: "is this gap unusual" is a
	// question about the whole record, not about the window being displayed.
	SeriesA dailySeries
	SeriesB dailySeries
	AsOf    time.Time
}

// buildRelativeResponse assembles the comparison. Pure function (unit tested).
func buildRelativeResponse(in relativeInputs) relativeResponse {
	out := relativeResponse{
		A: refOf(in.A), B: refOf(in.B),
		Period:   in.Query.Window.Period,
		To:       in.Query.Window.To,
		AsOf:     in.AsOf.UTC(),
		Series:   []relativePoint{},
		Warnings: []string{},
	}
	if in.Query.Window.From != nil {
		f := in.Query.Window.From.UTC()
		out.From = &f
	}
	if in.Query.Window.FutureToRequested {
		out.Warnings = append(out.Warnings,
			"The requested `to` is later than as_of; the window was served to as_of, "+
				"because no observation can exist after it.")
	}

	paired := pairSeries(in.SeriesA, in.SeriesB)
	full := paired.Points
	if len(full) == 0 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"%s and %s have no stored day in common (%d unpaired %s day(s), %d unpaired "+
				"%s day(s)), so there is nothing to compare. No value is carried forward "+
				"to manufacture a pair.",
			in.A.Code, in.B.Code, paired.UnpairedA, in.A.Code, paired.UnpairedB, in.B.Code))
		out.PercentileBasis = percentileBasis{
			MinIndependentWindows: minIndependentWindows,
			Note:                  "No percentile: the two instruments share no observation day.",
		}
		return out
	}
	if paired.UnpairedA > 0 || paired.UnpairedB > 0 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"Paired on days both instruments quoted: %d %s day(s) and %d %s day(s) had "+
				"no partner and were dropped rather than filled with a carried-forward "+
				"close.", paired.UnpairedA, in.A.Code, paired.UnpairedB, in.B.Code))
	}

	win := full.slice(in.Query.Window.From, in.Query.Window.To)
	if len(win) == 0 {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"No day inside this window on which both instruments quoted. Their shared "+
				"coverage is %s..%s.", dayString(full[0].Day), dayString(full[len(full)-1].Day)))
		out.PercentileBasis = percentileBasis{
			MinIndependentWindows: minIndependentWindows,
			Note:                  "No percentile: the window holds no paired day to place.",
		}
		return out
	}

	base := win[0]
	end := win[len(win)-1]
	baseDay := base.Day
	out.BaseDate = &baseDay
	out.Observations = len(win)

	aGrowth := end.A/base.A - 1
	bGrowth := end.B/base.B - 1
	out.AGrowthPct = fp(aGrowth * 100)
	out.BGrowthPct = fp(bGrowth * 100)

	current, ok := windowGap(base, end)
	if ok {
		out.GapPct = fp(current)
	}

	// The plotted series: both legs indexed to 100 at the base day, their ratio,
	// and the same gap formula evaluated at every point.
	ratios := make(dailySeries, 0, len(win))
	for _, p := range win {
		ai := p.A / base.A * 100
		bi := p.B / base.B * 100
		ratios = append(ratios, dailyPoint{Day: p.Day, Close: ai / bi})
	}
	indices := pickIndices(len(win), in.Query.Points)
	for _, i := range indices {
		p := win[i]
		ai := p.A / base.A * 100
		bi := p.B / base.B * 100
		g, gok := windowGap(base, p)
		if !gok {
			continue
		}
		out.Series = append(out.Series, relativePoint{
			T:        p.Day.Unix(),
			Date:     dayString(p.Day),
			AIndexed: math.Round(ai*1e6) / 1e6,
			BIndexed: math.Round(bi*1e6) / 1e6,
			Ratio:    math.Round(ai/bi*1e6) / 1e6,
			GapPct:   math.Round(g*1e6) / 1e6,
		})
	}
	if len(indices) < len(win) {
		out.Warnings = append(out.Warnings, fmt.Sprintf(
			"Series decimated from %d paired days to %d points by selecting evenly "+
				"spaced observations. Every plotted point is a real close on a real day; "+
				"nothing was averaged or interpolated. Ask for more with ?points=.",
			len(win), len(out.Series)))
	}

	out.RatioDrawdownPct = maxDrawdownPct(ratios)

	windowDays := daysBetween(base.Day, end.Day)
	if ok {
		out.GapPercentile, out.PercentileBasis = gapDistribution(full, windowDays, current)
	} else {
		out.PercentileBasis = percentileBasis{
			WindowDays:            windowDays,
			MinIndependentWindows: minIndependentWindows,
			Note:                  "No percentile: the base day carries a non-positive close on one leg.",
		}
	}
	return out
}

// --- handler -----------------------------------------------------------------------------

// legRefusal reports why an instrument cannot be a leg of /relative-value, or
// "" when it can. Pure function (unit tested).
//
// It is deliberately the SAME rule buildPerformanceResponse applies to the two
// exclusions that are about the instrument rather than about the unit of
// account: a disabled row, and a rate asked to behave like a price. An
// instrument that /markets/performance leaves out of its table with a named
// warning must not be silently comparable here.
//
// The third performance exclusion -- an index level, which has no currency to
// be carried into -- deliberately does NOT appear. /relative-value has no
// numeraire: indexing both legs to 100 at a shared base day cancels their
// units, so DXY against XAUUSD is a legitimate comparison here and an
// impossible column there.
func legRefusal(inst instrumentRow) string {
	switch {
	case !inst.Enabled:
		return refusalDisabled
	case isRateQuote(inst.QuoteCurrency):
		return refusalRate
	}
	return ""
}

const (
	refusalDisabled = "disabled"
	refusalRate     = "rate"
)

// unknownCode is the 400 for a code that is not in the instrument registry.
// Refused rather than answered with an empty series: "there is no such
// instrument" and "that instrument has no data" are different facts and a
// client must be able to act on each.
func unknownCode(w http.ResponseWriter, param, code string) {
	httpserver.BadRequest(w,
		fmt.Sprintf("%s=%q is not a registered instrument", param, code),
		map[string]any{param: code})
}

// rateCode is the 400 for an instrument that is a rate, not a price.
func rateCode(w http.ResponseWriter, param string, inst instrumentRow) {
	httpserver.BadRequest(w, fmt.Sprintf(
		"%s=%s is a RATE (quote_currency=%s), not a price: indexing a percentage to 100 "+
			"and reporting its growth would be a percent change of a percentage, which "+
			"measures nothing", param, inst.Code, inst.QuoteCurrency),
		map[string]any{param: inst.Code, "quote_currency": inst.QuoteCurrency})
}

// disabledCode is the 400 for an instrument the registry has switched off.
//
// /markets/performance excludes a disabled instrument from its table and names
// it in `warnings`. Comparing the same symbol here regardless would make one
// registry flag mean two different things on two endpoints, and would hand a
// caller a comparison against a series this deployment has retired -- with
// nothing on the response to say so.
func disabledCode(w http.ResponseWriter, param string, inst instrumentRow) {
	httpserver.BadRequest(w, fmt.Sprintf(
		"%s=%s is DISABLED in the instrument registry: this deployment has retired it, "+
			"and /markets/performance excludes it from the performance table for the same "+
			"reason", param, inst.Code),
		map[string]any{param: inst.Code, "enabled": false})
}

// RelativeValue implements
// GET /api/v1/relative-value?a=&b=&period=&from=&to=&points=.
func (h *Handler) RelativeValue(w http.ResponseWriter, r *http.Request) {
	now := time.Now().UTC()
	q, perr := parseRelativeQuery(r.URL.Query(), now)
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	ctx := r.Context()

	instruments, err := h.loadInstruments(ctx)
	if err != nil {
		h.Log.Error("relative_instruments", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	a, okA := findInstrument(instruments, q.A)
	if !okA {
		unknownCode(w, "a", q.A)
		return
	}
	b, okB := findInstrument(instruments, q.B)
	if !okB {
		unknownCode(w, "b", q.B)
		return
	}
	// Disabled first, then rate -- the same order buildPerformanceResponse
	// applies, so a symbol that is both is refused for the same reason on both
	// endpoints.
	for _, leg := range []struct {
		name string
		inst instrumentRow
	}{{"a", a}, {"b", b}} {
		switch legRefusal(leg.inst) {
		case refusalDisabled:
			disabledCode(w, leg.name, leg.inst)
			return
		case refusalRate:
			rateCode(w, leg.name, leg.inst)
			return
		}
	}

	series, err := h.loadDailySeries(ctx, []string{a.Code, b.Code}, now)
	if err != nil {
		h.Log.Error("relative_series", "error", err, "a", a.Code, "b", b.Code)
		httpserver.Internal(w, "database error")
		return
	}

	httpserver.JSON(w, http.StatusOK, buildRelativeResponse(relativeInputs{
		Query:   q,
		A:       a,
		B:       b,
		SeriesA: series[a.Code],
		SeriesB: series[b.Code],
		AsOf:    now,
	}))
}
