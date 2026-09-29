package bourse

// GET /api/v1/bourse/indices/{code}/history — one index over a window, in
// index points or re-expressed in dollars, in gold, or in constant prices.

import (
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/relvalue"
)

// The units an index can be drawn in.
//
// An index is not a price, but it IS proportional to the toman value of the
// portfolio it tracks, so dividing it by the dollar rate or by the price of a
// gram of gold answers a real question — "what did the market do for someone
// who keeps score in dollars, or in gold?" — provided the result is rebased
// and never presented as a level. The conversion itself is internal/relvalue's,
// the one implementation of the carry-forward rule in this codebase.
const (
	unitPoints = "points"
	unitUSD    = "usd"
	unitGold   = "gold"
	unitReal   = "real"
)

var unitVocabulary = []string{unitPoints, unitUSD, unitGold, unitReal}

type historyPoint struct {
	Date      string   `json:"date"`
	Value     *float64 `json:"value"`
	Unchanged bool     `json:"unchanged,omitempty"`
	Rescaled  bool     `json:"rescaled,omitempty"`
	// Period is the SCI month a constant-price point stands for.
	Period string `json:"period,omitempty"`
}

type historySummary struct {
	First *pointItem `json:"first"`
	Last  *pointItem `json:"last"`
	// ReturnPct is from the first point shown to the last, in the unit shown.
	ReturnPct   *float64     `json:"return_pct"`
	MaxDrawdown drawdownItem `json:"max_drawdown"`
	// Session dispersion is published for daily units only; a monthly
	// constant-price series has no sessions.
	DispersionPct     *float64 `json:"dispersion_pct"`
	DispersionReason  string   `json:"dispersion_reason,omitempty"`
	Sessions          int      `json:"sessions"`
	UnchangedSessions int      `json:"unchanged_sessions"`
	RescaledSessions  int      `json:"rescaled_sessions"`
}

type conversionItem struct {
	Chain                   []string `json:"chain"`
	Unit                    string   `json:"unit"`
	CarriedForward          int      `json:"carried_forward"`
	DroppedNoPriorQuote     int      `json:"dropped_no_prior_quote"`
	DroppedNonPositiveQuote int      `json:"dropped_nonpositive_quote"`
}

type deflatorItem struct {
	Series     string  `json:"series"`
	CoverageTo *string `json:"coverage_to"`
	Note       string  `json:"note"`
}

type historyResponse struct {
	Index      indexMeta       `json:"index"`
	Unit       string          `json:"unit"`
	UnitLabel  string          `json:"unit_label"`
	Window     windowItem      `json:"window"`
	Points     []historyPoint  `json:"points"`
	Summary    historySummary  `json:"summary"`
	Conversion *conversionItem `json:"conversion,omitempty"`
	Deflator   *deflatorItem   `json:"deflator,omitempty"`
	Notes      []string        `json:"notes"`
	DataAge    DataAge         `json:"data_age"`
}

// summarize is the window's figures in whatever unit the points are in.
// `daily` is false for the monthly constant-price series.
func summarize(s []IndexPoint, daily bool) historySummary {
	out := historySummary{}
	if len(s) == 0 {
		out.DispersionReason = "no point in this window"
		return out
	}
	out.First, out.Last = pointOf(s[0]), pointOf(s[len(s)-1])
	if len(s) >= 2 && s[0].Value > 0 {
		out.ReturnPct = fp((s[len(s)-1].Value/s[0].Value - 1) * 100)
	}
	out.MaxDrawdown = drawdownOf(maxDrawdown(s))
	for _, p := range s {
		if p.Unchanged {
			out.UnchangedSessions++
		}
		if p.Rescaled {
			out.RescaledSessions++
		}
	}
	if daily {
		out.DispersionPct, out.Sessions, out.DispersionReason = sessionDispersion(s)
	} else {
		out.DispersionReason = "a monthly series has no sessions; dispersion is published " +
			"for the daily units only"
	}
	return out
}

func toHistoryPoints(s []IndexPoint) []historyPoint {
	out := make([]historyPoint, 0, len(s))
	for _, p := range s {
		out = append(out, historyPoint{Date: dayString(p.Day), Value: fv(p.Value),
			Unchanged: p.Unchanged, Rescaled: p.Rescaled})
	}
	return out
}

// rebasedPoints maps a series onto 100 at its first point, keeping its flags.
func rebasedPoints(s []IndexPoint) []IndexPoint {
	vals := rebase(s)
	out := make([]IndexPoint, len(s))
	for i, p := range s {
		out[i] = p
		out[i].Value = vals[i]
	}
	return out
}

// convertIndex re-expresses a window of an index through the converter and
// rebases it. The Unchanged flags are recomputed on the CONVERTED values would
// be wrong — a closed market's repeated index is still a closed market when the
// dollar moved that day — so they are carried over from the index itself.
func convertIndex(s []IndexPoint, conv *relvalue.Converter, key string) ([]IndexPoint, relvalue.SeriesConversion) {
	flags := make(map[time.Time]IndexPoint, len(s))
	in := make([]relvalue.Point, 0, len(s))
	for _, p := range s {
		flags[p.Day] = p
		in = append(in, relvalue.Point{Day: p.Day, Value: p.Value})
	}
	res := conv.ConvertTomanSeries(in, key)
	out := make([]IndexPoint, 0, len(res.Points))
	for _, p := range res.Points {
		orig := flags[dayFloor(p.Day)]
		out = append(out, IndexPoint{Day: dayFloor(p.Day), Value: p.Value,
			Unchanged: orig.Unchanged, Rescaled: orig.Rescaled})
	}
	return rebasedPoints(out), res
}

func unknownIndex(w http.ResponseWriter, code string) {
	httpserver.Error(w, http.StatusNotFound, "not_found",
		fmt.Sprintf("%q is not an index this deployment carries", code),
		map[string]any{"ins_code": code,
			"hint": "GET /api/v1/bourse/indices lists every index with its insCode"})
}

func refusedIndex(w http.ResponseWriter, r indexRow) {
	reason := ""
	if r.RefusalReason != nil {
		reason = *r.RefusalReason
	}
	status := statusNeverIngested
	if r.Status != nil {
		status = *r.Status
	}
	httpserver.Error(w, http.StatusConflict, "index_unavailable",
		fmt.Sprintf("no validated history is served for %s (%s)", r.NameFA, r.InsCode),
		map[string]any{"ins_code": r.InsCode, "status": status, "refusal_reason": reason})
}

// IndexHistory implements
// GET /api/v1/bourse/indices/{code}/history?period=&from=&to=&unit=.
func (h *Handler) IndexHistory(w http.ResponseWriter, r *http.Request) {
	code := strings.TrimSpace(chi.URLParam(r, "code"))
	q := r.URL.Query()
	unit := strings.ToLower(strings.TrimSpace(q.Get("unit")))
	if unit == "" {
		unit = unitPoints
	}
	if !oneOf(unit, unitVocabulary...) {
		httpserver.BadRequest(w, fmt.Sprintf("unit must be one of %v, got %q", unitVocabulary, unit),
			map[string]any{"unit": unit, "supported": unitVocabulary})
		return
	}

	ctx := r.Context()
	store, err := h.storeFor(ctx)
	if err != nil {
		h.Log.Error("bourse_history_store", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	row, ok := store.byCode[code]
	if !ok {
		unknownIndex(w, code)
		return
	}
	series := store.series[code]
	if !row.Servable() || len(series) == 0 {
		refusedIndex(w, row)
		return
	}
	win, perr := parseWindow(q, series[len(series)-1].Day)
	if perr != nil {
		httpserver.BadRequest(w, perr.Message, perr.Details)
		return
	}
	now := time.Now()
	resp, err := h.buildHistory(r, store, row, series, win, unit, now)
	if err != nil {
		var pe *paramError
		if errors.As(err, &pe) {
			httpserver.BadRequest(w, pe.Message, pe.Details)
			return
		}
		h.Log.Error("bourse_history", "error", err, "code", code, "unit", unit)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, resp)
}

func (h *Handler) buildHistory(r *http.Request, store *indexStore, row indexRow,
	series []IndexPoint, win window, unit string, now time.Time) (historyResponse, error) {
	ctx := r.Context()
	windowed := withBase(series, win.From, win.To)
	resp := historyResponse{Index: buildMeta(row), Unit: unit, Window: win.item(),
		Notes: []string{}, DataAge: buildDataAge(store.newestSession(), now)}

	switch unit {
	case unitPoints:
		resp.UnitLabel = "index points"
		resp.Points = toHistoryPoints(windowed)
		resp.Summary = summarize(windowed, true)
	case unitUSD, unitGold:
		conv, err := relvalue.NewConverter(ctx, h.Pool, now)
		if err != nil {
			return resp, err
		}
		key := relvalue.NumeraireUSD
		label := "US dollars"
		if unit == unitGold {
			key, label = relvalue.NumeraireGold, "grams of 18k gold"
		}
		converted, res := convertIndex(windowed, conv, key)
		if res.MissingSeries != "" || res.Reason != "" {
			return resp, &paramError{Message: fmt.Sprintf(
				"this deployment cannot express an index in %s: %s", label, res.Reason),
				Details: map[string]any{"unit": unit}}
		}
		resp.UnitLabel = fmt.Sprintf("%s in %s, rebased to 100 at its first point", row.NameFA, label)
		resp.Points = toHistoryPoints(converted)
		resp.Summary = summarize(converted, true)
		resp.Conversion = &conversionItem{Chain: res.Chain, Unit: res.Unit,
			CarriedForward: res.CarriedForward, DroppedNoPriorQuote: res.DroppedNoPriorQuote,
			DroppedNonPositiveQuote: res.DroppedNonPositiveQuote}
		resp.Notes = append(resp.Notes, fmt.Sprintf(
			"Each session's index value is divided by the %s rate in force that day (carried "+
				"forward from the last quote, never from a later one) and rebased to 100. A "+
				"rebased line compares only with itself: it says how the market did for "+
				"someone keeping score in %s, not what the index is worth.",
			strings.Join(res.Chain, " then "), label))
		if res.DroppedNoPriorQuote > 0 {
			resp.Notes = append(resp.Notes, fmt.Sprintf(
				"%d session(s) at the start of the window precede the first stored %s quote "+
					"and are not shown; the line starts where the rate does.",
				res.DroppedNoPriorQuote, strings.Join(res.Chain, "/")))
		}
	case unitReal:
		cpi, err := relvalue.LoadMonthlyCPI(ctx, h.Pool, now)
		if err != nil {
			return resp, err
		}
		pts := make([]relvalue.Point, 0, len(windowed))
		for _, p := range windowed {
			pts = append(pts, relvalue.Point{Day: p.Day, Value: p.Value})
		}
		real := relvalue.DeflateMonthly(pts, cpi)
		resp.UnitLabel = fmt.Sprintf("%s in constant prices (%s), monthly, rebased to 100",
			row.NameFA, relvalue.SeriesMonthlyCPI)
		monthly := make([]IndexPoint, 0, len(real.Points))
		resp.Points = make([]historyPoint, 0, len(real.Points))
		for _, p := range real.Points {
			monthly = append(monthly, IndexPoint{Day: p.Day, Value: p.Value})
			resp.Points = append(resp.Points, historyPoint{Date: dayString(p.Day),
				Value: fv(p.Value), Period: p.Period})
		}
		resp.Summary = summarize(monthly, false)
		note := real.Note
		if real.Reason != "" {
			note = real.Reason
		}
		resp.Deflator = &deflatorItem{Series: relvalue.SeriesMonthlyCPI,
			CoverageTo: dayPtr(real.CoverageTo), Note: note}
		if real.Reason != "" {
			resp.Notes = append(resp.Notes, "No constant-price line: "+real.Reason)
		}
	}

	if resp.Summary.UnchangedSessions > 0 {
		resp.Notes = append(resp.Notes, fmt.Sprintf(
			"%d session(s) in this window repeat the previous value exactly — the market "+
				"was closed, or nothing in the index traded. They are drawn flat because "+
				"that is what TSETMC published, and left out of the dispersion.",
			resp.Summary.UnchangedSessions))
	}
	if resp.Summary.RescaledSessions > 0 {
		resp.Notes = append(resp.Notes, fmt.Sprintf(
			"%d value(s) in this window were stored by TSETMC off by a factor of ten and "+
				"are shown corrected; the check on this index says how the correction was "+
				"verified against the exchange's live figure.",
			resp.Summary.RescaledSessions))
	}
	return resp, nil
}
