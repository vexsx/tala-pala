package bourse

// One index for the Trade chart: GET /api/v1/market/candles?symbol=IDX:<code>
// is served by internal/prices, which asks this package for the series.
//
// It reads the SAME in-memory store /bourse/* reads, and deliberately not
// LoadIndexPoints. Two things differ between them and both matter on a chart
// that pages backwards through history:
//
//   - The store holds only ENABLED indices; LoadIndexPoints reads any row. An
//     operator who disables an index expects it gone from every page, and the
//     chart is a page.
//   - The store flags an unchanged session over the WHOLE series.
//     LoadIndexPoints recomputes the flag inside the window it was asked for,
//     so the first point of every window is never "unchanged" — a chart built
//     from pages of it would lose one closure flag per page boundary.
//
// The slice handed back IS the store's. It is shared by every request until
// the next ingest, so a caller reads it and never sorts, rebases or appends.

import (
	"context"
	"fmt"
	"time"
)

// ChartSeries returns one index's corrected series with its registry label,
// the age of the stored market data and the store's revision — the key that
// changes on every ingest, which is how a chart that already holds older pages
// learns that TSETMC restated them (scale_exp is recomputed over the whole
// series on each ingest; migration 0029).
//
// ErrUnknownIndex: the code is not an enabled index. ErrIndexRefused: its
// newest verdict withholds the series, or it has no stored session.
func (h *Handler) ChartSeries(ctx context.Context, code string) (IndexInfo, []IndexPoint, DataAge, string, error) {
	store, err := h.storeFor(ctx)
	if err != nil {
		return IndexInfo{InsCode: code}, nil, DataAge{}, "", err
	}
	info, series, err := chartSeriesFrom(store, code)
	return info, series, chartDataAge(store, series, time.Now()), store.key, err
}

// chartDataAge is the age of the series ON the chart. Pure (unit tested).
//
// The Tehran market page ages the whole store by TEDPIX's newest session,
// which is right for a page about the market. A chart draws ONE index, and its
// status bar says "last session <date>" beside that index's line — so the date
// must be where the line ends. An ingest isolates failures per index and per
// day, so one index can lag the market; aged by TEDPIX, its chart would call a
// line that stops weeks early current.
func chartDataAge(store *indexStore, series []IndexPoint, now time.Time) DataAge {
	market := store.newestSession()
	if len(series) == 0 {
		return buildDataAge(market, now)
	}
	last := series[len(series)-1].Day
	age := buildDataAge(&last, now)
	age.Note = chartDataAgeNote
	if market != nil && last.Before(dayFloor(*market)) {
		age.Stale = true
		age.Warning = fmt.Sprintf(
			"This index's newest stored session is %s, but the market's is %s: the last "+
				"fetch did not bring this index up to date, so its line stops early. The "+
				"missing sessions are absent, not flat.",
			dayString(last), dayString(*market))
	}
	return age
}

const chartDataAgeNote = "Tehran market data does not refresh itself: TSETMC is unreachable " +
	"from the production host, so an index arrives only when an operator runs the fetch " +
	"from a network that can reach it. This is the age of THIS index's newest stored " +
	"session, measured against today."

// chartSeriesFrom is the lookup and the gate. Pure (unit tested).
func chartSeriesFrom(store *indexStore, code string) (IndexInfo, []IndexPoint, error) {
	row, ok := store.byCode[code]
	if !ok {
		return IndexInfo{InsCode: code}, nil, ErrUnknownIndex
	}
	info := chartIndexInfo(row)
	series := store.series[code]
	if !row.Servable() || len(series) == 0 {
		return info, nil, ErrIndexRefused
	}
	return info, series, nil
}

// chartIndexInfo is the registry row and its verdict, as the chart labels it.
func chartIndexInfo(r indexRow) IndexInfo {
	check := buildCheck(r)
	return IndexInfo{
		InsCode: r.InsCode, NameFA: r.NameFA, NameEN: r.NameEN, Market: r.Market,
		Kind: r.Kind, SectorCode: r.SectorCode, RefusalReason: check.RefusalReason,
		Weighting: r.Weighting, ReturnBasis: r.ReturnBasis, Notes: r.Notes,
		CheckStatus: check.Status, RowsRescaled: check.RowsRescaled,
	}
}
