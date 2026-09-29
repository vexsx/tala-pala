package bourse

// Loading the registry and the corrected series.

import (
	"context"
	"errors"
	"fmt"
	"math"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
)

// IndexPoint is one stored session of an index, CORRECTED: the raw close times
// ten to its row's scale_exp.
type IndexPoint struct {
	Day   time.Time
	Value float64
	// Unchanged is true when Value equals the previous session's EXACTLY — a
	// closed market or a sector nothing in traded. See the package comment.
	Unchanged bool
	// Rescaled is true when the stored close needed a power of ten.
	Rescaled bool
}

// indexRow is market_indices joined to its newest verdict.
type indexRow struct {
	InsCode      string
	NameFA       string
	NameEN       string
	Market       string
	Kind         string
	SectorCode   string
	Weighting    string
	ReturnBasis  string
	DisplayOrder int
	FirstDate    *time.Time
	LastDate     *time.Time
	ValueCount   int
	Enabled      bool
	Notes        string

	// From market_index_checks; nil when the index was never ingested.
	CheckVersion       *string
	Status             *string
	ScaleBreaks        *int
	RowsRescaled       *int
	DroppedNonpositive *int
	FirstBreak         *time.Time
	LastBreak          *time.Time
	LargestMove        *float64
	LargestMoveDate    *time.Time
	LiveValue          *float64
	LiveRatio          *float64
	LiveCheckedAt      *time.Time
	RefusalReason      *string
	ComputedAt         *time.Time
}

// Servable is whether a corrected value may be served for this index.
func (r indexRow) Servable() bool { return r.Status != nil && *r.Status == statusValidated }

// The newest verdict per index, for the same reason internal/equities takes
// the newest adjustment: market_index_checks is keyed by check_version so a
// v2 can be computed beside v1, and a plain join would then list an index
// twice. The scale_exp column always reflects the most recent ingest, so the
// most recent verdict is the one that describes it.
const indexSelect = `
	SELECT i.ins_code, i.name_fa, i.name_en, i.market, i.kind, i.sector_code,
	       i.weighting, i.return_basis, i.display_order, i.first_date,
	       i.last_date, i.value_count, i.enabled, i.notes,
	       c.check_version, c.status, c.scale_breaks, c.rows_rescaled,
	       c.dropped_nonpositive, c.first_break, c.last_break, c.largest_move,
	       c.largest_move_date, c.live_value, c.live_ratio, c.live_checked_at,
	       c.refusal_reason, c.computed_at
	FROM market_indices i
	LEFT JOIN LATERAL (
	    SELECT * FROM market_index_checks
	    WHERE ins_code = i.ins_code
	    ORDER BY computed_at DESC
	    LIMIT 1
	) c ON TRUE`

func scanIndexRow(row pgx.Row) (indexRow, error) {
	var r indexRow
	err := row.Scan(&r.InsCode, &r.NameFA, &r.NameEN, &r.Market, &r.Kind,
		&r.SectorCode, &r.Weighting, &r.ReturnBasis, &r.DisplayOrder,
		&r.FirstDate, &r.LastDate, &r.ValueCount, &r.Enabled, &r.Notes,
		&r.CheckVersion, &r.Status, &r.ScaleBreaks, &r.RowsRescaled,
		&r.DroppedNonpositive, &r.FirstBreak, &r.LastBreak, &r.LargestMove,
		&r.LargestMoveDate, &r.LiveValue, &r.LiveRatio, &r.LiveCheckedAt,
		&r.RefusalReason, &r.ComputedAt)
	return r, err
}

// The store is keyed on everything an ingest or an operator's edit changes:
// every ingest rewrites each index's verdict (computed_at) and coverage
// (updated_at), and enabling or disabling an index changes the count.
const storeKeySelect = `
	SELECT coalesce((SELECT max(computed_at)::text FROM market_index_checks), '')
	       || '|' || coalesce(max(updated_at)::text, '')
	       || '|' || count(*) FILTER (WHERE enabled)
	FROM market_indices`

const valuesSelect = `
	SELECT v.ins_code, v.trade_date, v.close, v.scale_exp
	FROM market_index_values v
	WHERE v.ins_code = ANY($1)
	ORDER BY v.ins_code, v.trade_date`

// indexStore is every enabled index and every validated series, in memory.
type indexStore struct {
	key    string
	rows   []indexRow // display order
	byCode map[string]indexRow
	series map[string][]IndexPoint
}

func (h *Handler) loadStore(ctx context.Context) (*indexStore, error) {
	var key string
	if err := h.Pool.QueryRow(ctx, storeKeySelect).Scan(&key); err != nil {
		return nil, fmt.Errorf("store key: %w", err)
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.store != nil && h.store.key == key {
		return h.store, nil
	}
	store, err := readStore(ctx, h.Pool, key)
	if err != nil {
		return nil, err
	}
	h.store = store
	return store, nil
}

func readStore(ctx context.Context, pool *pgxpool.Pool, key string) (*indexStore, error) {
	rows, err := pool.Query(ctx, indexSelect+`
	WHERE i.enabled
	ORDER BY i.display_order, i.ins_code`)
	if err != nil {
		return nil, fmt.Errorf("indices: %w", err)
	}
	store := &indexStore{key: key, byCode: map[string]indexRow{}, series: map[string][]IndexPoint{}}
	var servable []string
	for rows.Next() {
		r, err := scanIndexRow(rows)
		if err != nil {
			rows.Close()
			return nil, fmt.Errorf("index scan: %w", err)
		}
		store.rows = append(store.rows, r)
		store.byCode[r.InsCode] = r
		if r.Servable() {
			servable = append(servable, r.InsCode)
		}
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, fmt.Errorf("index rows: %w", err)
	}
	if len(servable) == 0 {
		return store, nil
	}

	vrows, err := pool.Query(ctx, valuesSelect, servable)
	if err != nil {
		return nil, fmt.Errorf("values: %w", err)
	}
	defer vrows.Close()
	for vrows.Next() {
		var code string
		var day time.Time
		var closeRaw float64
		var exp int16
		if err := vrows.Scan(&code, &day, &closeRaw, &exp); err != nil {
			return nil, fmt.Errorf("value scan: %w", err)
		}
		store.series[code] = append(store.series[code], IndexPoint{
			Day:      dayFloor(day),
			Value:    correct(closeRaw, int(exp)),
			Rescaled: exp != 0,
		})
	}
	if err := vrows.Err(); err != nil {
		return nil, fmt.Errorf("value rows: %w", err)
	}
	for code, s := range store.series {
		markUnchanged(s)
		store.series[code] = s
	}
	return store, nil
}

// correct applies a stored power of ten.
//
// A NEGATIVE exponent divides rather than multiplying by 10^exp, and the test
// that pins this failed first: 0.1 has no exact binary form, so 12,926 * 0.1 is
// 1292.6000000000001, while 12,926 / 10 is the double nearest 1292.6. 10, 100
// and 1,000 ARE exact, so dividing by them rounds correctly — and an exact
// repeat stays an exact repeat, which is what marks a closed market.
func correct(raw float64, exp int) float64 {
	switch {
	case exp == 0:
		return raw
	case exp > 0:
		return raw * math.Pow10(exp)
	default:
		return raw / math.Pow10(-exp)
	}
}

// markUnchanged flags every point equal to its predecessor. Pure (tested).
func markUnchanged(s []IndexPoint) {
	for i := range s {
		s[i].Unchanged = i > 0 && s[i].Value == s[i-1].Value
	}
}

// newestSession is the newest stored session across the servable series, or
// nil when there is none. It drives data_age.
func (s *indexStore) newestSession() *time.Time {
	var newest *time.Time
	for _, pts := range s.series {
		if len(pts) == 0 {
			continue
		}
		d := pts[len(pts)-1].Day
		if newest == nil || d.After(*newest) {
			newest = &d
		}
	}
	return newest
}

// ErrUnknownIndex and ErrIndexRefused are what LoadIndexPoints returns for an
// index it cannot serve; a caller turns them into a 404 and a 409.
var (
	ErrUnknownIndex = errors.New("unknown index")
	ErrIndexRefused = errors.New("index refused by its verdict")
)

// IndexInfo is the part of the registry a caller outside this package needs to
// label a series it draws.
type IndexInfo struct {
	InsCode       string `json:"ins_code"`
	NameFA        string `json:"name_fa"`
	NameEN        string `json:"name_en"`
	Market        string `json:"market"`
	Kind          string `json:"kind"`
	SectorCode    string `json:"sector_code"`
	RefusalReason string `json:"refusal_reason,omitempty"`
}

// LoadIndexPoints reads ONE index's corrected series straight from the
// database, for a caller that needs a single index over a window and has no
// business holding this package's whole store — internal/equities comparing a
// share with its market and its sector. `from`/`to` are inclusive; nil is
// unbounded.
func LoadIndexPoints(ctx context.Context, pool *pgxpool.Pool, code string,
	from, to *time.Time) (IndexInfo, []IndexPoint, error) {
	row, err := scanIndexRow(pool.QueryRow(ctx, indexSelect+` WHERE i.ins_code = $1`, code))
	if errors.Is(err, pgx.ErrNoRows) {
		return IndexInfo{InsCode: code}, nil, ErrUnknownIndex
	}
	if err != nil {
		return IndexInfo{}, nil, err
	}
	info := IndexInfo{InsCode: row.InsCode, NameFA: row.NameFA, NameEN: row.NameEN,
		Market: row.Market, Kind: row.Kind, SectorCode: row.SectorCode}
	if !row.Servable() {
		if row.RefusalReason != nil {
			info.RefusalReason = *row.RefusalReason
		}
		return info, nil, ErrIndexRefused
	}
	rows, err := pool.Query(ctx, `
		SELECT trade_date, close, scale_exp FROM market_index_values
		WHERE ins_code = $1
		  AND ($2::date IS NULL OR trade_date >= $2)
		  AND ($3::date IS NULL OR trade_date <= $3)
		ORDER BY trade_date`, code, from, to)
	if err != nil {
		return info, nil, err
	}
	defer rows.Close()
	var pts []IndexPoint
	for rows.Next() {
		var day time.Time
		var raw float64
		var exp int16
		if err := rows.Scan(&day, &raw, &exp); err != nil {
			return info, nil, err
		}
		pts = append(pts, IndexPoint{Day: dayFloor(day), Value: correct(raw, int(exp)), Rescaled: exp != 0})
	}
	if err := rows.Err(); err != nil {
		return info, nil, err
	}
	markUnchanged(pts)
	return info, pts, nil
}

// SectorIndexFor is the bourse sector index whose TSETMC label carries this
// sector code, or "" when none does. Only bourse sector indices carry codes
// (migration 0029), so a Farabourse index is never matched by accident.
func SectorIndexFor(ctx context.Context, pool *pgxpool.Pool, sectorCode string) (string, error) {
	if sectorCode == "" {
		return "", nil
	}
	var code string
	err := pool.QueryRow(ctx, `
		SELECT ins_code FROM market_indices
		WHERE market = 'bourse' AND kind = 'sector' AND sector_code = $1 AND enabled
		ORDER BY display_order LIMIT 1`, sectorCode).Scan(&code)
	if errors.Is(err, pgx.ErrNoRows) {
		return "", nil
	}
	return code, err
}
