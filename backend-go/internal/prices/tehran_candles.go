package prices

// Tehran market symbols on the Trade chart: IDX:<insCode> (a TSETMC index)
// and EQ:<insCode> (a roster share), served through GET /api/v1/market/candles
// with the same pagination contract as the tick candles — oldest first,
// `before` exclusive, `next_before` the oldest session returned — so the
// chart's store, its paging and its refusal handling work unchanged.
//
// What is NOT the same, and is stated on the wire rather than papered over:
//
//   - Daily only. TSETMC publishes one settled row per session and nothing
//     finer here, so every sub-day timeframe is refused; 2d/3d epoch buckets
//     would straddle the Saturday–Wednesday week, and a weekly candle for a
//     close-only series would have to invent its open. Any interval but 1d is
//     the 400 with the SAME message the tick feed uses, so the chart's
//     existing fallback reads it.
//   - An index is CLOSE-ONLY. market_index_values holds the close and a
//     low/high pair but no open, and the low/high are not a traded range — so
//     open/high/low are null and price_fields says ["close"]. Sending the close
//     four times with synthetic:true would put a "1 obs" badge on every
//     session and make an index look like it had a range; the chart draws a
//     line instead. SuperTrend, PSAR, Ichimoku and the pivots need a high and
//     a low and are null — refused, not approximated.
//   - A session that repeats the previous close EXACTLY is a closed market
//     (TEDPIX held 3,713,955.9 for 50 sessions from 2026-02-25). It is served
//     flat, as TSETMC published it and flagged `unchanged`, and kept OUT of
//     every indicator input, the index-level twin of skipping a halted share.
//     The alternative — feeding the repeats in — was rejected: fifty 0% days
//     would drag a 50-session average flat through a closure and read as a
//     calm market rather than a shut one. internal/bourse makes the same
//     choice for its dispersion.
//   - A share's bars are adjusted rials with the official close, a last trade
//     beside it, and halted sessions as `traded:false` with no range. Its
//     OHLC overlays are computed over the traded sessions only and scattered
//     back by index, because overlay arrays are index-aligned with the candles.
//   - Freshness is the DATA's age (data_age, the 10-day bound shared with the
//     bars and the Tehran market page), never "as_of: now": this data arrives
//     only when an operator runs the off-server fetch.
//   - `revision` changes when the stored data does. The index store is
//     rebuilt on every ingest and scale_exp is recomputed over the whole
//     series (migration 0029), and a recomputed adjustment re-scales a share's
//     older bars — so a page the chart already holds can be restated, and the
//     live poll's three newest sessions would never show it.
//
// Nothing here forecasts or advises: the series are what TSETMC published,
// and the overlays are the same technical arithmetic the tick candles carry.

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/bourse"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/equities"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/indicators"
)

// IndexSeriesSource is internal/bourse's store, as the chart needs it.
type IndexSeriesSource interface {
	ChartSeries(ctx context.Context, code string) (bourse.IndexInfo, []bourse.IndexPoint, bourse.DataAge, string, error)
}

// EquityBarSource is internal/equities' adjusted bars, as the chart needs
// them: [from, before) with before exclusive, at most limit sessions.
type EquityBarSource interface {
	ChartBars(ctx context.Context, insCode string, from, before *time.Time, limit int) (equities.ChartBars, error)
}

const (
	tehranInterval = "1d"
	tehranSource   = "TSETMC"
	unitIndex      = "index_points"
	unitRial       = "IRR"
)

// tehranIntervals is the whole supported vocabulary for a Tehran symbol.
var tehranIntervals = []string{tehranInterval}

var (
	indexPriceFields  = []string{"close"}
	equityPriceFields = []string{"open", "high", "low", "close"}
)

// tehranResponseKeys is candleResponseKeys plus what a Tehran series adds. A
// test pins every one of them onto the wire.
var tehranResponseKeys = append(append([]string{}, candleResponseKeys...),
	"unit", "source", "instrument", "data_age", "notes", "revision")

// seriesCandle is one settled session on the wire. It deliberately is not
// `candle`: that type always emits ticks and synthetic, which describe tick
// buckets and would be false statements about an exchange session.
type seriesCandle struct {
	T         int64     `json:"t"` // unix seconds, 00:00 UTC of the trade date
	OpenTime  time.Time `json:"open_time"`
	CloseTime time.Time `json:"close_time"`
	// Null for an index (TSETMC publishes no open and no traded range) and
	// for a halted or defective share session.
	Open  *float64 `json:"open"`
	High  *float64 `json:"high"`
	Low   *float64 `json:"low"`
	Close float64  `json:"close"`
	// Null for an index; shares traded for a share.
	Volume *float64 `json:"volume"`
	// Always true: only settled sessions are stored.
	Confirmed bool `json:"confirmed"`

	// Index: the close repeats the previous session exactly (a closed
	// market), and the stored close needed a power-of-ten correction.
	Unchanged bool `json:"unchanged,omitempty"`
	Rescaled  bool `json:"rescaled,omitempty"`

	// Share: whether anything traded, the back-adjustment factor when it is
	// not 1, the last trade price, and whether the official close sits
	// outside the traded range.
	Traded            *bool    `json:"traded,omitempty"`
	AdjustmentFactor  float64  `json:"adjustment_factor,omitempty"`
	LastTrade         *float64 `json:"last_trade,omitempty"`
	CloseOutsideRange bool     `json:"close_outside_range,omitempty"`
}

func sessionCandle(day time.Time) seriesCandle {
	start := time.Date(day.Year(), day.Month(), day.Day(), 0, 0, 0, 0, time.UTC)
	return seriesCandle{T: start.Unix(), OpenTime: start,
		CloseTime: start.Add(24 * time.Hour), Confirmed: true}
}

// tehranPage is everything derived from the fetched sessions: the visible
// page and what is drawn on it. Same meaning as candlePage + candlePayload.
type tehranPage struct {
	Candles    []seriesCandle
	HasMore    bool
	NextBefore *time.Time
	// Nil when ?overlays=0, and serializes as null.
	Overlays            map[string]any
	Pivots              *indicators.PivotPoints
	Support, Resistance *float64
}

// --- overlays ----------------------------------------------------------------

// compress keeps the values whose mask is true and remembers where each came
// from, so an indicator computed over the kept values can be put back at the
// positions the candles occupy.
func compress(vals []float64, keep []bool) ([]float64, []int) {
	out := make([]float64, 0, len(vals))
	at := make([]int, 0, len(vals))
	for i, v := range vals {
		if keep[i] {
			out = append(out, v)
			at = append(at, i)
		}
	}
	return out, at
}

// scatter puts a compressed series back into n slots, null everywhere a
// session was left out, and slices off the warm-up.
func scatter(vals []float64, at []int, n, start int) []*float64 {
	out := make([]*float64, n)
	for k, i := range at {
		out[i] = fp(vals[k])
	}
	return out[start:]
}

// closeOverlays are the overlays a close alone supports: SMA 20/50 and
// Bollinger, computed over the kept sessions and aligned with every session.
func closeOverlays(closes []float64, keep []bool, start int) map[string]any {
	vals, at := compress(closes, keep)
	n := len(closes)
	bbU, bbM, bbL := indicators.Bollinger(vals, 20, 2)
	return map[string]any{
		"sma_20":          scatter(indicators.SMA(vals, 20), at, n, start),
		"sma_50":          scatter(indicators.SMA(vals, 50), at, n, start),
		"bollinger_upper": scatter(bbU, at, n, start),
		"bollinger_mid":   scatter(bbM, at, n, start),
		"bollinger_lower": scatter(bbL, at, n, start),
	}
}

// closeOnlyOverlays is the overlay block of a close-only series: SMA and
// Bollinger over the included closes, and null — the whole field, not an
// array of nulls — for every overlay that needs a high and a low. Pure (unit
// tested).
func closeOnlyOverlays(closes []float64, include []bool, start int) map[string]any {
	out := closeOverlays(closes, include, start)
	for _, key := range []string{"supertrend", "supertrend_dir", "psar",
		"ichimoku_tenkan", "ichimoku_kijun", "ichimoku_senkou_a", "ichimoku_senkou_b"} {
		out[key] = nil
	}
	return out
}

// supportResistanceOver is the published band over the included closes, or
// nulls when fewer than the lookback exist — refused rather than narrowed,
// exactly as buildCandlePayload does.
func supportResistanceOver(closes []float64, include []bool) (*float64, *float64) {
	vals, _ := compress(closes, include)
	if len(vals) < supportResistanceLookback {
		return nil, nil
	}
	s, r := indicators.SupportResistance(vals, supportResistanceLookback)
	return fp(s), fp(r)
}

// pageBounds cuts `n` fetched sessions into warm-up and page: the page is the
// newest `limit`, and `warm` more before it feed the overlays when asked for.
func pageBounds(n, limit int, overlays bool) (fetchStart, visibleStart int) {
	visibleStart = max(n-limit, 0)
	fetchStart = visibleStart
	if overlays {
		fetchStart = max(visibleStart-candleWarmupBuckets, 0)
	}
	return fetchStart, visibleStart
}

// --- an index ----------------------------------------------------------------

// indexCandle is one stored index session on the wire.
func indexCandle(p bourse.IndexPoint) seriesCandle {
	c := sessionCandle(p.Day)
	c.Close = p.Value
	c.Unchanged, c.Rescaled = p.Unchanged, p.Rescaled
	return c
}

// buildIndexCandlePage cuts one page out of a whole stored series: the newest
// `limit` sessions inside [win.From, win.To), plus up to candleWarmupBuckets
// before them for the overlays. Pure function (unit tested).
//
// `pts` is the store's own slice, shared by every request: it is searched and
// sub-sliced, never written, sorted or appended to. has_more is exact — the
// whole series is in memory, so "older sessions exist inside the window" needs
// no probe.
func buildIndexCandlePage(pts []bourse.IndexPoint, win candleWindow, limit int, overlays bool) tehranPage {
	lo := 0
	if win.From != nil {
		from := *win.From
		lo = sort.Search(len(pts), func(i int) bool { return !pts[i].Day.Before(from) })
	}
	hi := sort.Search(len(pts), func(i int) bool { return !pts[i].Day.Before(win.To) })
	if hi < lo {
		hi = lo
	}
	inWindow := pts[lo:hi]
	fetchStart, visibleStart := pageBounds(len(inWindow), limit, overlays)
	fetched := inWindow[fetchStart:]
	start := visibleStart - fetchStart

	page := tehranPage{Candles: make([]seriesCandle, 0, len(fetched)-start)}
	for _, p := range fetched[start:] {
		page.Candles = append(page.Candles, indexCandle(p))
	}
	if len(page.Candles) > 0 {
		oldest := inWindow[visibleStart].Day
		page.NextBefore = &oldest
		page.HasMore = visibleStart > 0
	}

	closes := make([]float64, len(fetched))
	include := make([]bool, len(fetched))
	for i, p := range fetched {
		closes[i], include[i] = p.Value, !p.Unchanged
	}
	if overlays {
		page.Overlays = closeOnlyOverlays(closes, include, start)
	}
	page.Support, page.Resistance = supportResistanceOver(closes, include)
	return page
}

// --- a share -----------------------------------------------------------------

// equityCandle is one adjusted session on the wire.
func equityCandle(b equities.ChartBar) seriesCandle {
	c := sessionCandle(b.Day)
	c.Open, c.High, c.Low, c.Close = b.Open, b.High, b.Low, b.Close
	volume := float64(b.Volume)
	traded := b.Traded
	c.Volume, c.Traded, c.LastTrade = &volume, &traded, b.LastTrade
	if b.Factor != 1 {
		c.AdjustmentFactor = b.Factor
	}
	c.CloseOutsideRange = b.CloseOutsideRange
	return c
}

// equityOverlays computes every overlay over the sessions that can carry it —
// the close-based ones over traded sessions, the high/low-based ones over
// traded sessions with a range — and scatters them back so each array stays
// index-aligned with the candles. A halted session gets null everywhere and a
// SuperTrend direction of 0, the same value warm-up has. Pure (unit tested).
func equityOverlays(bars []equities.ChartBar, start int) map[string]any {
	n := len(bars)
	closes := make([]float64, n)
	highs := make([]float64, n)
	lows := make([]float64, n)
	traded := make([]bool, n)
	ranged := make([]bool, n)
	for i, b := range bars {
		closes[i], traded[i] = b.Close, b.Traded
		if b.Traded && b.High != nil && b.Low != nil {
			highs[i], lows[i], ranged[i] = *b.High, *b.Low, true
		}
	}
	out := closeOverlays(closes, traded, start)

	hs, at := compress(highs, ranged)
	ls, _ := compress(lows, ranged)
	cs, _ := compress(closes, ranged)
	stLine, stDir := indicators.SuperTrend(hs, ls, cs, 10, 3)
	tenkan, kijun, senkouA, senkouB := indicators.Ichimoku(hs, ls)
	dir := make([]int, n)
	for k, i := range at {
		dir[i] = stDir[k]
	}
	out["supertrend"] = scatter(stLine, at, n, start)
	out["supertrend_dir"] = dir[start:]
	out["psar"] = scatter(indicators.ParabolicSAR(hs, ls, 0.02, 0.02, 0.2), at, n, start)
	out["ichimoku_tenkan"] = scatter(tenkan, at, n, start)
	out["ichimoku_kijun"] = scatter(kijun, at, n, start)
	out["ichimoku_senkou_a"] = scatter(senkouA, at, n, start)
	out["ichimoku_senkou_b"] = scatter(senkouB, at, n, start)
	return out
}

// buildEquityCandlePage cuts the page out of the fetched sessions (oldest
// first). `olderExists` is the source's own has_more — sessions older than
// everything fetched. Pure function (unit tested).
func buildEquityCandlePage(bars []equities.ChartBar, olderExists bool, limit int, overlays bool) tehranPage {
	_, start := pageBounds(len(bars), limit, false)
	page := tehranPage{Candles: make([]seriesCandle, 0, len(bars)-start)}
	for _, b := range bars[start:] {
		page.Candles = append(page.Candles, equityCandle(b))
	}
	if len(page.Candles) > 0 {
		oldest := bars[start].Day
		page.NextBefore = &oldest
		page.HasMore = start > 0 || olderExists
	}
	if overlays {
		page.Overlays = equityOverlays(bars, start)
	}
	// Classic pivots are defined on a completed bar WITH a range: the newest
	// traded session is used, never a halt's carried price.
	for i := len(bars) - 1; i >= 0; i-- {
		if b := bars[i]; b.Traded && b.High != nil && b.Low != nil {
			pv := indicators.Pivots(*b.High, *b.Low, b.Close)
			page.Pivots = &pv
			break
		}
	}
	closes := make([]float64, len(bars))
	traded := make([]bool, len(bars))
	for i, b := range bars {
		closes[i], traded[i] = b.Close, b.Traded
	}
	page.Support, page.Resistance = supportResistanceOver(closes, traded)
	return page
}

// --- the response --------------------------------------------------------------

func tehranCoverage(historyFrom *time.Time, note string) candleCoverage {
	day := 86400
	return candleCoverage{
		BaseGranularitySeconds: &day,
		IntradayFrom:           nil,
		HistoryFrom:            historyFrom,
		SupportedIntervals:     tehranIntervals,
		Note:                   note,
	}
}

const indexCoverageNote = "TSETMC publishes one closing value per session for an index and " +
	"no opening value; the low and high it stores are not a traded range. The series is " +
	"drawn from the close alone, one point per settled session, so only the daily " +
	"timeframe exists."

const equityCoverageNote = "One bar per settled TSETMC session, back-adjusted for corporate " +
	"actions, in rials. The close is the official closing price (قیمت پایانی) and can sit " +
	"outside the session's high–low; a halted session has no trade. Only the daily timeframe " +
	"exists: nothing finer is stored, and 2-day, 3-day or weekly buckets would not line up " +
	"with the exchange's Saturday–Wednesday sessions."

// tehranMeta is what the response says about the instrument and its data.
type tehranMeta struct {
	PriceFields []string
	Unit        string
	Instrument  any
	DataAge     any
	Notes       []string
	Revision    string
}

// tehranResponse is the wire body. Pure function (unit tested).
func tehranResponse(q candleQuery, win candleWindow, cov candleCoverage, page tehranPage,
	meta tehranMeta, now time.Time) map[string]any {
	return map[string]any{
		"symbol":           q.Symbol,
		"interval":         tehranInterval,
		"interval_seconds": int64(86400),
		"timezone":         "UTC",
		"candles":          page.Candles,
		"has_more":         page.HasMore,
		"next_before":      page.NextBefore,
		"coverage":         cov,
		"effective_window": map[string]any{"from": win.From, "to": win.To},
		"overlays":         page.Overlays,
		"pivots":           page.Pivots,
		"support":          page.Support,
		"resistance":       page.Resistance,
		// as_of is when the response was built; how old the DATA is, is
		// data_age — the one a reader must look at.
		"as_of":        now,
		"price_fields": meta.PriceFields,
		// A Tehran series is aged by data_age, not by a cadence.
		"cadence":    nil,
		"unit":       meta.Unit,
		"source":     tehranSource,
		"instrument": meta.Instrument,
		"data_age":   meta.DataAge,
		"notes":      meta.Notes,
		"revision":   meta.Revision,
	}
}

var weightingText = map[string]string{
	"cap":        "capitalisation-weighted",
	"equal":      "equal-weighted",
	"free_float": "free-float weighted",
}

var returnBasisText = map[string]string{
	"total": "a total-return index (price and dividends)",
	"price": "a price index (dividends excluded)",
}

// indexNotes are the sentences a reader needs to read an index chart right.
// Pure (unit tested).
func indexNotes(info bourse.IndexInfo, page tehranPage) []string {
	notes := []string{
		fmt.Sprintf("Index points, not a price: TSETMC's own values after one correction "+
			"(closes the exchange stored off by a power of ten are multiplied back; %d of this "+
			"index's stored closes were).", info.RowsRescaled),
	}
	w, wok := weightingText[info.Weighting]
	b, bok := returnBasisText[info.ReturnBasis]
	switch {
	case wok && bok:
		notes = append(notes, fmt.Sprintf("This is %s, %s.", b, w))
	case wok:
		notes = append(notes, fmt.Sprintf("This index is %s; TSETMC does not state whether it "+
			"includes dividends.", w))
	case bok:
		notes = append(notes, fmt.Sprintf("This is %s; TSETMC does not state its weighting.", b))
	}
	// Stated without a count: this response is one page of the series, and
	// its count disagreed with the status bar's over every page loaded ("59
	// session(s) on this page" beside "62 of 1000"). The client counts.
	for _, c := range page.Candles {
		if c.Unchanged {
			notes = append(notes, "Sessions that repeat the previous close exactly — the "+
				"market was closed, or nothing in the index traded — are drawn flat, as TSETMC "+
				"published them, and left out of every indicator.")
			break
		}
	}
	if info.Notes != "" {
		notes = append(notes, info.Notes)
	}
	notes = append(notes, "An index level describes what the market did; it is not a "+
		"forecast and not a recommendation.")
	return notes
}

// indexInstrument is the registry label on the wire. A map rather than
// bourse.IndexInfo so rows_rescaled travels even when it is zero.
func indexInstrument(info bourse.IndexInfo) map[string]any {
	return map[string]any{
		"ins_code":       info.InsCode,
		"name_fa":        info.NameFA,
		"name_en":        info.NameEN,
		"market":         info.Market,
		"kind":           info.Kind,
		"sector_code":    info.SectorCode,
		"weighting":      info.Weighting,
		"return_basis":   info.ReturnBasis,
		"check_status":   info.CheckStatus,
		"rows_rescaled":  info.RowsRescaled,
		"refusal_reason": info.RefusalReason,
	}
}

// --- the handler ---------------------------------------------------------------

// tehranCandles serves an IDX:/EQ: symbol. The interval is checked FIRST,
// before either source is touched, with the tick feed's own refusal text —
// the chart's fallback matches on it.
func (h *Handler) tehranCandles(w http.ResponseWriter, r *http.Request, q candleQuery) {
	if q.Interval.Name != tehranInterval {
		httpserver.BadRequest(w, unsupportedIntervalMessage, map[string]any{
			"symbol":              q.Symbol,
			"interval":            q.Interval.Name,
			"supported_intervals": tehranIntervals,
		})
		return
	}
	now := time.Now().UTC()
	win := snapCandleWindow(q, q.Interval, now)
	if q.Source == sourceTSEIndex {
		h.indexCandles(w, r, q, win, now)
		return
	}
	h.equityCandles(w, r, q, win, now)
}

func (h *Handler) indexCandles(w http.ResponseWriter, r *http.Request, q candleQuery,
	win candleWindow, now time.Time) {
	if h.Indices == nil {
		h.Log.Error("candles_tehran_unwired", "symbol", q.Symbol)
		httpserver.Internal(w, "the Tehran market chart is not configured")
		return
	}
	info, series, age, revision, err := h.Indices.ChartSeries(r.Context(), q.Code)
	switch {
	case errors.Is(err, bourse.ErrUnknownIndex):
		httpserver.Error(w, http.StatusNotFound, "not_found",
			fmt.Sprintf("%q is not an index this deployment carries", q.Symbol),
			map[string]any{"symbol": q.Symbol, "ins_code": q.Code,
				"hint": "GET /api/v1/bourse/indices lists every index with its insCode"})
		return
	case errors.Is(err, bourse.ErrIndexRefused):
		status := info.CheckStatus
		if status == "" {
			status = "never_ingested"
		}
		httpserver.Error(w, http.StatusConflict, "index_unavailable",
			fmt.Sprintf("no validated history is served for %s (%s)", info.NameFA, q.Code),
			map[string]any{"symbol": q.Symbol, "ins_code": q.Code, "status": status,
				"refusal_reason": info.RefusalReason})
		return
	case err != nil:
		h.Log.Error("candles_tehran_index", "error", err, "symbol", q.Symbol)
		httpserver.Internal(w, "database error")
		return
	}

	page := buildIndexCandlePage(series, win, q.Limit, q.Overlays)
	first := series[0].Day
	httpserver.JSON(w, http.StatusOK, tehranResponse(q, win,
		tehranCoverage(&first, indexCoverageNote), page, tehranMeta{
			PriceFields: indexPriceFields,
			Unit:        unitIndex,
			Instrument:  indexInstrument(info),
			DataAge:     age,
			Notes:       indexNotes(info, page),
			Revision:    revision,
		}, now))
}

func (h *Handler) equityCandles(w http.ResponseWriter, r *http.Request, q candleQuery,
	win candleWindow, now time.Time) {
	if h.Equities == nil {
		h.Log.Error("candles_tehran_unwired", "symbol", q.Symbol)
		httpserver.Internal(w, "the Tehran market chart is not configured")
		return
	}
	fetch := q.Limit
	if q.Overlays {
		fetch += candleWarmupBuckets
	}
	// The window's exclusive end on a session boundary: with no cursor it is
	// `now`, and a trade_date compared against now's DATE would drop today's
	// settled session.
	upper := q.Interval.BucketCeil(win.To)
	bars, err := h.Equities.ChartBars(r.Context(), q.Code, win.From, &upper, fetch)
	var gate *equities.AdjustmentUnavailableError
	switch {
	case errors.Is(err, equities.ErrUnknownEquity):
		httpserver.Error(w, http.StatusNotFound, "not_found",
			fmt.Sprintf("%q is not a share in this deployment's roster", q.Symbol),
			map[string]any{"symbol": q.Symbol, "ins_code": q.Code,
				"hint": "GET /api/v1/stocks lists every share with its insCode"})
		return
	case errors.As(err, &gate):
		details := gate.Details()
		details["chart_symbol"] = q.Symbol
		httpserver.Error(w, http.StatusConflict, "adjustment_unavailable", gate.Error(), details)
		return
	case err != nil:
		h.Log.Error("candles_tehran_equity", "error", err, "symbol", q.Symbol)
		httpserver.Internal(w, "database error")
		return
	}

	page := buildEquityCandlePage(bars.Items, bars.HasMore, q.Limit, q.Overlays)
	historyFrom := bars.Meta.HistoryFrom
	if historyFrom == nil && len(bars.Items) > 0 {
		historyFrom = &bars.Items[0].Day
	}
	// No per-page count of halts: equities' own notes say how a halt is drawn,
	// and the status bar counts them over every page loaded.
	notes := append([]string{}, bars.Notes...)
	httpserver.JSON(w, http.StatusOK, tehranResponse(q, win,
		tehranCoverage(historyFrom, equityCoverageNote), page, tehranMeta{
			PriceFields: equityPriceFields,
			Unit:        unitRial,
			Instrument:  bars.Meta,
			DataAge:     bars.DataAge,
			Notes:       notes,
			Revision:    bars.Revision,
		}, now))
}
