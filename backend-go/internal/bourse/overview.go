package bourse

// GET /api/v1/bourse/overview — the market as it stood when a refresh last
// ran, and the headline indices' newest settled sessions.
//
// Two kinds of figure travel here and are kept apart on purpose. The HEADLINE
// block is settled history: each index's newest stored session and its change
// against the one before. The SNAPSHOT is TSETMC's live overview captured at
// fetch time — today's trade value, trade count and market state — which the
// exchange publishes only as a live figure and this deployment sees only when
// someone runs the fetch. It is labelled with the instant it describes and is
// never presented as current.

import (
	"context"
	"net/http"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

type snapshotItem struct {
	Market           string   `json:"market"`
	ActivityAt       string   `json:"activity_at"`
	AgeHours         *float64 `json:"age_hours"`
	IndexValue       *float64 `json:"index_value"`
	IndexChange      *float64 `json:"index_change"`
	IndexChangePct   *float64 `json:"index_change_pct"`
	EWIndexValue     *float64 `json:"ew_index_value"`
	EWIndexChange    *float64 `json:"ew_index_change"`
	EWIndexChangePct *float64 `json:"ew_index_change_pct"`
	TradeCount       *int64   `json:"trade_count"`
	TradeVolume      *float64 `json:"trade_volume"`
	// Toman, converted from the rials TSETMC serves. Every instrument on the
	// market — shares, funds, bonds — as the exchange's own overview counts it.
	TradeValueToman  *float64 `json:"trade_value_toman"`
	MarketValueToman *float64 `json:"market_value_toman"`
	State            string   `json:"state"`
	StateTitle       string   `json:"state_title"`
	CollectedAt      string   `json:"collected_at"`
}

type sectorBreadthItem struct {
	SectorCode string `json:"sector_code"`
	SectorFA   string `json:"sector_fa"`
	// The bourse sector index whose label carries this code, when there is one.
	IndexInsCode string `json:"index_ins_code,omitempty"`
	IndexNameEN  string `json:"index_name_en,omitempty"`
	DownOver2    int    `json:"down_over_2"`
	DownUnder2   int    `json:"down_under_2"`
	UpUnder2     int    `json:"up_under_2"`
	UpOver2      int    `json:"up_over_2"`
	Total        int    `json:"total"`
	// UpSharePct is the share of the sector's instruments in TSETMC's two
	// "increase" buckets. TSETMC reports no "unchanged" bucket, so an
	// instrument that did not move is in one of the four and this is not
	// "advancers" in the textbook sense; the note says so.
	UpSharePct *float64 `json:"up_share_pct"`
}

type breadthTotals struct {
	DownOver2  int      `json:"down_over_2"`
	DownUnder2 int      `json:"down_under_2"`
	UpUnder2   int      `json:"up_under_2"`
	UpOver2    int      `json:"up_over_2"`
	Total      int      `json:"total"`
	UpSharePct *float64 `json:"up_share_pct"`
}

type headlineItem struct {
	InsCode       string     `json:"ins_code"`
	NameFA        string     `json:"name_fa"`
	NameEN        string     `json:"name_en"`
	Last          *pointItem `json:"last"`
	Change1DPct   *float64   `json:"change_1d_pct"`
	LastUnchanged bool       `json:"last_unchanged"`
	Status        string     `json:"status"`
}

type overviewResponse struct {
	Headline     []headlineItem      `json:"headline"`
	Snapshots    []snapshotItem      `json:"snapshots"`
	Sectors      []sectorBreadthItem `json:"sectors"`
	SectorsAt    *string             `json:"sectors_at"`
	SectorTotals breadthTotals       `json:"sector_totals"`
	Notes        []string            `json:"notes"`
	DataAge      DataAge             `json:"data_age"`
}

var headlineCodes = []string{TEDPIX, EqualWeighted, IFX}

// buildHeadline is each headline index's newest session. Pure (unit tested).
func buildHeadline(store *indexStore) []headlineItem {
	out := []headlineItem{}
	for _, code := range headlineCodes {
		row, ok := store.byCode[code]
		if !ok {
			continue
		}
		item := headlineItem{InsCode: code, NameFA: row.NameFA, NameEN: row.NameEN,
			Status: statusNeverIngested}
		if row.Status != nil {
			item.Status = *row.Status
		}
		s := store.series[code]
		if row.Servable() && len(s) > 0 {
			last := s[len(s)-1]
			item.Last = pointOf(last)
			item.LastUnchanged = last.Unchanged
			if len(s) >= 2 {
				item.Change1DPct = fp((last.Value/s[len(s)-2].Value - 1) * 100)
			}
		}
		out = append(out, item)
	}
	return out
}

// changePct is change / (value - change): the move against the previous
// close the exchange measured it from.
func changePct(value, change *float64) *float64 {
	if value == nil || change == nil {
		return nil
	}
	base := *value - *change
	if base <= 0 {
		return nil
	}
	return fp(*change / base * 100)
}

func tomanOf(rials *float64) *float64 {
	if rials == nil {
		return nil
	}
	return fv(*rials / rialsPerToman)
}

func (h *Handler) loadSnapshots(ctx context.Context, now time.Time) ([]snapshotItem, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT DISTINCT ON (market) market, activity_at, index_value, index_change,
		       ew_index_value, ew_index_change, trade_count, trade_volume, trade_value,
		       market_value, market_state, market_state_title, collected_at
		FROM market_snapshots
		ORDER BY market, activity_at DESC`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []snapshotItem{}
	for rows.Next() {
		var s snapshotItem
		var at, collected time.Time
		var tradeValue, marketValue *float64
		if err := rows.Scan(&s.Market, &at, &s.IndexValue, &s.IndexChange, &s.EWIndexValue,
			&s.EWIndexChange, &s.TradeCount, &s.TradeVolume, &tradeValue, &marketValue,
			&s.State, &s.StateTitle, &collected); err != nil {
			return nil, err
		}
		s.ActivityAt = at.UTC().Format(time.RFC3339)
		s.CollectedAt = collected.UTC().Format(time.RFC3339)
		age := now.Sub(at).Hours()
		s.AgeHours = fp(age)
		s.IndexChangePct = changePct(s.IndexValue, s.IndexChange)
		s.EWIndexChangePct = changePct(s.EWIndexValue, s.EWIndexChange)
		s.TradeValueToman = tomanOf(tradeValue)
		s.MarketValueToman = tomanOf(marketValue)
		out = append(out, s)
	}
	return out, rows.Err()
}

// buildSectorBreadth totals the four buckets. Pure (unit tested).
func buildSectorBreadth(items []sectorBreadthItem) ([]sectorBreadthItem, breadthTotals) {
	var t breadthTotals
	for i := range items {
		it := &items[i]
		it.Total = it.DownOver2 + it.DownUnder2 + it.UpUnder2 + it.UpOver2
		if it.Total > 0 {
			it.UpSharePct = fp(float64(it.UpUnder2+it.UpOver2) / float64(it.Total) * 100)
		}
		t.DownOver2 += it.DownOver2
		t.DownUnder2 += it.DownUnder2
		t.UpUnder2 += it.UpUnder2
		t.UpOver2 += it.UpOver2
	}
	t.Total = t.DownOver2 + t.DownUnder2 + t.UpUnder2 + t.UpOver2
	if t.Total > 0 {
		t.UpSharePct = fp(float64(t.UpUnder2+t.UpOver2) / float64(t.Total) * 100)
	}
	return items, t
}

func (h *Handler) loadSectorBreadth(ctx context.Context, store *indexStore) ([]sectorBreadthItem, *string, error) {
	rows, err := h.Pool.Query(ctx, `
		SELECT activity_at, sector_code, sector_fa, down_over_2, down_under_2,
		       up_under_2, up_over_2
		FROM sector_breadth_snapshots
		WHERE activity_at = (SELECT max(activity_at) FROM sector_breadth_snapshots)
		ORDER BY sector_code`)
	if err != nil {
		return nil, nil, err
	}
	defer rows.Close()
	bySector := map[string]indexRow{}
	for _, r := range store.rows {
		if r.Market == "bourse" && r.Kind == "sector" && r.SectorCode != "" {
			bySector[r.SectorCode] = r
		}
	}
	out := []sectorBreadthItem{}
	var at *string
	for rows.Next() {
		var it sectorBreadthItem
		var stamp time.Time
		if err := rows.Scan(&stamp, &it.SectorCode, &it.SectorFA, &it.DownOver2,
			&it.DownUnder2, &it.UpUnder2, &it.UpOver2); err != nil {
			return nil, nil, err
		}
		if idx, ok := bySector[it.SectorCode]; ok {
			it.IndexInsCode, it.IndexNameEN = idx.InsCode, idx.NameEN
		}
		s := stamp.UTC().Format(time.RFC3339)
		at = &s
		out = append(out, it)
	}
	return out, at, rows.Err()
}

// Overview implements GET /api/v1/bourse/overview.
func (h *Handler) Overview(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	now := time.Now()
	store, err := h.storeFor(ctx)
	if err != nil {
		h.Log.Error("bourse_overview_store", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	snaps, err := h.loadSnapshots(ctx, now)
	if err != nil {
		h.Log.Error("bourse_overview_snapshots", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	sectors, at, err := h.loadSectorBreadth(ctx, store)
	if err != nil {
		h.Log.Error("bourse_overview_sectors", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	sectors, totals := buildSectorBreadth(sectors)
	httpserver.JSON(w, http.StatusOK, overviewResponse{
		Headline: buildHeadline(store), Snapshots: snaps, Sectors: sectors, SectorsAt: at,
		SectorTotals: totals,
		Notes: []string{
			"Headline figures are settled sessions: each index's newest stored close against " +
				"the one before it.",
			"The snapshot is TSETMC's LIVE overview captured when the data was last fetched — " +
				"trade value and count exist only as a live figure — so it describes the " +
				"instant in activity_at and nothing after it.",
			"Sector breadth uses TSETMC's own four buckets (down more than 2%, down less than " +
				"2%, up less than 2%, up more than 2%) from the same instant. The exchange " +
				"reports no 'unchanged' bucket, so an instrument that did not move is counted " +
				"in one of the four.",
		},
		DataAge: buildDataAge(store.newestSession(), now),
	})
}
