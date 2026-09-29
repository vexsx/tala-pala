package bourse

// GET /api/v1/bourse/breadth — the equal-weighted index against the
// cap-weighted one.
//
// The Tehran market's most-used breadth reading needs no new data: TEDPIX
// weighs a company by its market value, so a handful of large refiners, miners
// and petrochemicals can carry it; شاخص کل (هم وزن) weighs every company the
// same, so it moves with the TYPICAL listed share. When the equal-weighted line
// rises against the cap-weighted one, the average company is beating the large
// ones — the advance is broad. When it falls, a few heavyweights are doing the
// work. This endpoint publishes both rebased to one start and their ratio;
// it does not say which state is "good", because that is not a measurement.

import (
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// The two pairs: dividends included on both sides, or excluded on both. A
// total-return index against a price index would mix a dividend yield into
// the spread and call it breadth.
var breadthPairs = map[string][2]string{
	"total": {TEDPIX, EqualWeighted},
	"price": {TEPIX, EqualWeightedPrice},
}

type pairedPoint struct {
	Day     time.Time
	Cap, Eq IndexPoint
}

// pairSeries is the two series on the dates BOTH carry. Pure (unit tested).
func pairSeries(capS, eqS []IndexPoint) []pairedPoint {
	idx := make(map[time.Time]IndexPoint, len(eqS))
	for _, p := range eqS {
		idx[p.Day] = p
	}
	out := make([]pairedPoint, 0, len(eqS))
	for _, c := range capS {
		if e, ok := idx[c.Day]; ok {
			out = append(out, pairedPoint{Day: c.Day, Cap: c, Eq: e})
		}
	}
	return out
}

// pairsInWindow is the pairs in [from, to], with the pair in force at `from`
// prepended when the window's first pair is later — the same rule withBase
// applies to one series.
func pairsInWindow(p []pairedPoint, from *time.Time, to time.Time) []pairedPoint {
	t := dayFloor(to)
	hi := sort.Search(len(p), func(i int) bool { return p[i].Day.After(t) })
	if from == nil {
		return p[:hi]
	}
	f := dayFloor(*from)
	lo := sort.Search(len(p), func(i int) bool { return !p[i].Day.Before(f) })
	if lo > 0 && (lo >= len(p) || !p[lo].Day.Equal(f)) {
		lo-- // the pair in force when the window opened
	}
	if lo >= hi {
		return nil
	}
	return p[lo:hi]
}

type breadthPoint struct {
	Date  string   `json:"date"`
	Cap   *float64 `json:"cap"`
	Equal *float64 `json:"equal"`
	// Ratio is equal / cap * 100: 100 at the start, above 100 when the typical
	// company has beaten the cap-weighted market since then.
	Ratio     *float64 `json:"ratio"`
	Unchanged bool     `json:"unchanged,omitempty"`
}

type breadthSummary struct {
	From           *string  `json:"from"`
	To             *string  `json:"to"`
	CapReturnPct   *float64 `json:"cap_return_pct"`
	EqualReturnPct *float64 `json:"equal_return_pct"`
	// SpreadPP is the equal-weighted return minus the cap-weighted one, in
	// percentage POINTS.
	SpreadPP *float64 `json:"spread_pp"`
	// SessionsCompared counts sessions in which at least one of the two moved.
	SessionsCompared int `json:"sessions_compared"`
	EqualBeatCap     int `json:"equal_beat_cap"`
	CapBeatEqual     int `json:"cap_beat_equal"`
	// Correlation of the two indices' session returns: how much of the day to
	// day movement they share. Not a cause of anything.
	Correlation *float64 `json:"correlation"`
}

type breadthResponse struct {
	Basis   string         `json:"basis"`
	Cap     indexMeta      `json:"cap"`
	Equal   indexMeta      `json:"equal"`
	Window  windowItem     `json:"window"`
	Points  []breadthPoint `json:"points"`
	Summary breadthSummary `json:"summary"`
	Notes   []string       `json:"notes"`
	DataAge DataAge        `json:"data_age"`
}

// buildBreadth is the whole comparison. Pure (unit tested).
func buildBreadth(pairs []pairedPoint) ([]breadthPoint, breadthSummary) {
	points := make([]breadthPoint, 0, len(pairs))
	sum := breadthSummary{}
	if len(pairs) == 0 {
		return points, sum
	}
	c0, e0 := pairs[0].Cap.Value, pairs[0].Eq.Value
	var capRets, eqRets []float64
	for i, p := range pairs {
		c := p.Cap.Value / c0 * 100
		e := p.Eq.Value / e0 * 100
		bp := breadthPoint{Date: dayString(p.Day), Cap: fp(c), Equal: fp(e), Ratio: fp(e / c * 100),
			Unchanged: p.Cap.Unchanged && p.Eq.Unchanged}
		points = append(points, bp)
		if i == 0 || bp.Unchanged {
			continue
		}
		prev := pairs[i-1]
		rc := p.Cap.Value/prev.Cap.Value - 1
		re := p.Eq.Value/prev.Eq.Value - 1
		capRets = append(capRets, rc)
		eqRets = append(eqRets, re)
		sum.SessionsCompared++
		switch {
		case re > rc:
			sum.EqualBeatCap++
		case rc > re:
			sum.CapBeatEqual++
		}
	}
	first, last := pairs[0], pairs[len(pairs)-1]
	sum.From, sum.To = dayPtr(&first.Day), dayPtr(&last.Day)
	if len(pairs) >= 2 {
		cr := (last.Cap.Value/first.Cap.Value - 1) * 100
		er := (last.Eq.Value/first.Eq.Value - 1) * 100
		sum.CapReturnPct, sum.EqualReturnPct, sum.SpreadPP = fp(cr), fp(er), fp(er-cr)
	}
	sum.Correlation = Pearson(capRets, eqRets)
	return points, sum
}

// Breadth implements GET /api/v1/bourse/breadth?basis=total|price&period=&from=&to=.
func (h *Handler) Breadth(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	basis := strings.ToLower(strings.TrimSpace(q.Get("basis")))
	if basis == "" {
		basis = "total"
	}
	pair, ok := breadthPairs[basis]
	if !ok {
		httpserver.BadRequest(w, fmt.Sprintf("basis must be total or price, got %q", basis),
			map[string]any{"basis": basis})
		return
	}
	ctx := r.Context()
	store, err := h.storeFor(ctx)
	if err != nil {
		h.Log.Error("bourse_breadth_store", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	capRow, okC := store.byCode[pair[0]]
	eqRow, okE := store.byCode[pair[1]]
	if !okC || !okE {
		httpserver.Error(w, http.StatusConflict, "index_unavailable",
			"the cap- and equal-weighted indices are not both registered and enabled here",
			map[string]any{"cap": pair[0], "equal": pair[1]})
		return
	}
	for _, row := range []indexRow{capRow, eqRow} {
		if !row.Servable() || len(store.series[row.InsCode]) == 0 {
			refusedIndex(w, row)
			return
		}
	}
	all := pairSeries(store.series[pair[0]], store.series[pair[1]])
	if len(all) == 0 {
		httpserver.Error(w, http.StatusConflict, "no_common_sessions",
			"the two indices share no stored session", nil)
		return
	}
	win, perr := parseWindow(q, all[len(all)-1].Day)
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	pairs := pairsInWindow(all, win.From, win.To)
	points, sum := buildBreadth(pairs)
	notes := []string{
		"Both lines are rebased to 100 at the window's first common session and the ratio is " +
			"equal / cap x 100. Above 100: the typical listed company has beaten the " +
			"cap-weighted market since the window opened; below 100: a few large companies " +
			"have carried it. It describes how broad a move was, not where it goes next.",
	}
	if win.From != nil && len(pairs) > 0 && pairs[0].Day.After(*win.From) {
		notes = append(notes, fmt.Sprintf(
			"The window asked to start on %s, but the two indices share no session before %s "+
				"(the equal-weighted index begins 2014-03-19), so the comparison starts there.",
			dayString(*win.From), dayString(pairs[0].Day)))
	}
	httpserver.JSON(w, http.StatusOK, breadthResponse{
		Basis: basis, Cap: buildMeta(capRow), Equal: buildMeta(eqRow), Window: win.item(),
		Points: points, Summary: sum, Notes: notes,
		DataAge: buildDataAge(store.newestSession(), time.Now()),
	})
}
