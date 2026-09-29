package bourse

// The database side of the market-wide money flow: what sectorflows.go's
// build reads, the cache it is held in, and the two routes.
//
// The build reads ~120 sessions × ~1,000 shares ≈ 120,000 flow rows and sums
// them in Go, with the same tolerance constants the roster's Check() uses, so
// the arithmetic has one implementation and no SQL copy that could drift from
// it. Numeric columns are cast to float8 in the query: decoding NUMERIC is the
// main cost of a read this size. The result changes only when an operator runs
// the off-server fetch, so it is built once per ingest and every other request
// reads memory, exactly as the index store is.

import (
	"context"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// calendarLookbackDays bounds the calendar scan. 120 sessions are about six
// months of trading; 400 calendar days hold them through a closure as long as
// 2026's twelve weeks with room left, and the scan never reaches back past
// marketFlowFloor.
const calendarLookbackDays = 400

// sectorFlowsKeySelect is the footprint of everything the build reads: the
// market-wide ingest rewrites market_shares' coverage columns (flow_count,
// session_count, updated_at) and records every whole-market day file; a
// roster flow ingest moves the newest flow date; a bar ingest bumps
// equity_instruments.updated_at, and a bar is a value check; enabling a roster
// share changes which rows link to a share page. The caller appends the index
// store's key, for the sector returns.
const sectorFlowsKeySelect = `
	SELECT coalesce((SELECT max(updated_at)::text FROM market_shares), '')
	       || '|' || coalesce((SELECT count(*) || '/' || count(*) FILTER (WHERE listed)
	                                  || '/' || coalesce(sum(flow_count), 0)
	                                  || '/' || coalesce(sum(session_count), 0)
	                           FROM market_shares), '')
	       || '|' || coalesce((SELECT max(ingested_at)::text || '/' || count(*)
	                           FROM market_session_files), '')
	       || '|' || coalesce((SELECT max(trade_date)::text FROM equity_client_flows), '')
	       || '|' || coalesce((SELECT max(updated_at)::text FROM market_sectors), '')
	       || '|' || coalesce((SELECT max(updated_at)::text || '/' || count(*) FILTER (WHERE enabled)
	                           FROM equity_instruments), '')`

// sectorFlowsCalendarSelect is, per stored date since the floor, its flow
// rows (carrying any flow, showing a trade) and what TSETMC's day file for it
// says: whether one is stored, the shares it shows trading and their value,
// and how many of those — and how much of that value — carry a traded flow
// row. Values are RIALS. A date with flow and no day file, or a day file and
// no flow, is listed either way (the FULL JOIN), so the calendar can say why
// it is not a market session.
const sectorFlowsCalendarSelect = `
	WITH lo AS (
	    SELECT greatest($1::date,
	                    coalesce(greatest((SELECT max(trade_date) FROM equity_client_flows),
	                                      (SELECT max(trade_date) FROM market_session_files)),
	                             $1::date) - $2::int) AS d
	), flows AS (
	    SELECT f.trade_date, count(*) AS rows,
	           count(*) FILTER (WHERE f.buy_i_value + f.buy_n_value + f.sell_i_value
	                                  + f.sell_n_value > 0) AS traded
	    FROM equity_client_flows f, lo
	    WHERE f.trade_date >= lo.d
	    GROUP BY f.trade_date
	), files AS (
	    SELECT m.trade_date FROM market_session_files m, lo WHERE m.trade_date >= lo.d
	), days AS (
	    SELECT s.trade_date,
	           count(*) AS day_traded,
	           sum(s.value)::float8 AS day_value,
	           count(f.ins_code) AS covered,
	           coalesce(sum(s.value) FILTER (WHERE f.ins_code IS NOT NULL), 0)::float8 AS covered_value
	    FROM market_share_sessions s
	    JOIN files ON files.trade_date = s.trade_date
	    LEFT JOIN equity_client_flows f
	           ON f.ins_code = s.ins_code AND f.trade_date = s.trade_date
	          AND f.buy_i_value + f.buy_n_value + f.sell_i_value + f.sell_n_value > 0
	    WHERE s.trades > 0 OR s.volume > 0
	    GROUP BY s.trade_date
	)
	SELECT coalesce(flows.trade_date, files.trade_date),
	       coalesce(flows.rows, 0), coalesce(flows.traded, 0),
	       files.trade_date IS NOT NULL,
	       coalesce(days.day_traded, 0), coalesce(days.day_value, 0),
	       coalesce(days.covered, 0), coalesce(days.covered_value, 0)
	FROM flows
	FULL JOIN files ON files.trade_date = flows.trade_date
	LEFT JOIN days ON days.trade_date = coalesce(flows.trade_date, files.trade_date)
	ORDER BY 1 DESC`

// sectorFlowsRowsSelect is every flow row between two sessions, with the two
// independent statements of each session's traded value beside it. RIALS.
const sectorFlowsRowsSelect = `
	SELECT f.ins_code, f.trade_date,
	       f.buy_i_count, f.buy_n_count, f.sell_i_count, f.sell_n_count,
	       f.buy_i_volume::float8, f.buy_n_volume::float8,
	       f.sell_i_volume::float8, f.sell_n_volume::float8,
	       f.buy_i_value::float8, f.buy_n_value::float8,
	       f.sell_i_value::float8, f.sell_n_value::float8,
	       s.value::float8, b.value::float8
	FROM equity_client_flows f
	LEFT JOIN market_share_sessions s ON s.ins_code = f.ins_code AND s.trade_date = f.trade_date
	LEFT JOIN equity_bars b ON b.ins_code = f.ins_code AND b.trade_date = f.trade_date
	WHERE f.trade_date BETWEEN $1::date AND $2::date`

// sectorFlowsPricesSelect is each share's official close against its
// reference price over the same span: TSETMC's whole-market day file where it
// was ingested, and a roster share's own TRADED bar where it was not (a halted
// bar carries the reference price in its close and is not a session). A day
// file's row also carries its traded value and whether it shows a trade: the
// rows every group's coverage is measured against (dayFileRow). RIALS.
const sectorFlowsPricesSelect = `
	SELECT ins_code, trade_date, close::float8, price_yesterday::float8,
	       value::float8, (trades > 0 OR volume > 0), TRUE
	FROM market_share_sessions
	WHERE trade_date BETWEEN $1::date AND $2::date
	UNION ALL
	SELECT b.ins_code, b.trade_date, b.final_close::float8, b.price_yesterday::float8,
	       b.value::float8, TRUE, FALSE
	FROM equity_bars b
	WHERE b.trade_date BETWEEN $1::date AND $2::date
	  AND b.volume > 0 AND b.price_yesterday > 0
	  AND NOT EXISTS (SELECT 1 FROM market_share_sessions s
	                  WHERE s.ins_code = b.ins_code AND s.trade_date = b.trade_date)`

// sectorFlowsSharesSelect is the whole mirror of TSETMC's share listing,
// listed or not — a delisted share's stored rows still happened — joined to
// the curated roster, whose symbol is the one /stocks/{symbol} resolves.
const sectorFlowsSharesSelect = `
	SELECT m.ins_code, m.symbol_fa, m.name_fa, m.market, m.board, m.company_code,
	       m.sector_code, m.listed, coalesce(e.symbol_fa, ''), coalesce(e.enabled, FALSE),
	       m.flow_first_date
	FROM market_shares m
	LEFT JOIN equity_instruments e ON e.ins_code = m.ins_code`

const sectorFlowsSectorsSelect = `SELECT sector_code, name_fa, name_en FROM market_sectors`

// readSectorFlowInput runs the reads. The calendar is decided first, so the
// flow rows are read for exactly its span.
func readSectorFlowInput(ctx context.Context, pool *pgxpool.Pool, store *indexStore) (sectorFlowInput, error) {
	var in sectorFlowInput
	floor, err := time.Parse(dateLayout, marketFlowFloor)
	if err != nil {
		return in, err
	}
	rows, err := pool.Query(ctx, sectorFlowsCalendarSelect, floor, calendarLookbackDays)
	if err != nil {
		return in, fmt.Errorf("calendar: %w", err)
	}
	var counts []sessionCount
	for rows.Next() {
		var c sessionCount
		if err := rows.Scan(&c.Day, &c.Rows, &c.Traded, &c.DayFile, &c.DayTraded, &c.DayValue,
			&c.Covered, &c.CoveredValue); err != nil {
			rows.Close()
			return in, fmt.Errorf("calendar scan: %w", err)
		}
		c.DayValue /= rialsPerToman
		c.CoveredValue /= rialsPerToman
		counts = append(counts, c)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return in, fmt.Errorf("calendar rows: %w", err)
	}
	in.Calendar = buildMarketCalendar(counts, calendarSessions)

	if in.Shares, err = readShareMeta(ctx, pool); err != nil {
		return in, err
	}
	if !in.Calendar.marketWide() {
		return in, nil
	}
	oldest, newest := in.Calendar.Sessions[len(in.Calendar.Sessions)-1], in.Calendar.Sessions[0]
	if in.Rows, err = readMarketRows(ctx, pool, oldest, newest); err != nil {
		return in, err
	}
	if in.Prices, in.DayFiles, err = readPrices(ctx, pool, oldest, newest); err != nil {
		return in, err
	}
	if in.Sectors, err = readSectorNames(ctx, pool); err != nil {
		return in, err
	}

	// Each sector's bourse index, by the one rule SectorIndexFor states, and
	// its validated series from the index store.
	in.SectorIndex, in.IndexSeries = map[string]string{}, map[string][]IndexPoint{}
	seen := map[string]bool{}
	for _, m := range in.Shares {
		if seen[m.SectorCode] {
			continue
		}
		seen[m.SectorCode] = true
		code, err := SectorIndexFor(ctx, pool, m.SectorCode)
		if err != nil {
			return in, fmt.Errorf("sector index %s: %w", m.SectorCode, err)
		}
		if code == "" {
			continue
		}
		in.SectorIndex[m.SectorCode] = code
		if s := store.series[code]; len(s) > 0 {
			in.IndexSeries[code] = s
		}
	}
	return in, nil
}

func readShareMeta(ctx context.Context, pool *pgxpool.Pool) ([]shareMeta, error) {
	rows, err := pool.Query(ctx, sectorFlowsSharesSelect)
	if err != nil {
		return nil, fmt.Errorf("shares: %w", err)
	}
	defer rows.Close()
	var out []shareMeta
	for rows.Next() {
		var m shareMeta
		if err := rows.Scan(&m.InsCode, &m.Symbol, &m.NameFA, &m.Market, &m.Board, &m.CompanyCode,
			&m.SectorCode, &m.Listed, &m.RosterSymbol, &m.InRoster, &m.FlowFirstDate); err != nil {
			return nil, fmt.Errorf("share scan: %w", err)
		}
		out = append(out, m)
	}
	return out, rows.Err()
}

func readMarketRows(ctx context.Context, pool *pgxpool.Pool, from, to time.Time) ([]marketRow, error) {
	rows, err := pool.Query(ctx, sectorFlowsRowsSelect, from, to)
	if err != nil {
		return nil, fmt.Errorf("flows: %w", err)
	}
	defer rows.Close()
	var out []marketRow
	for rows.Next() {
		var r marketRow
		var session, bar *float64
		f := &r.Flow
		if err := rows.Scan(&r.InsCode, &f.Day, &f.BuyICount, &f.BuyNCount, &f.SellICount,
			&f.SellNCount, &f.BuyIVolume, &f.BuyNVolume, &f.SellIVolume, &f.SellNVolume,
			&f.BuyIValue, &f.BuyNValue, &f.SellIValue, &f.SellNValue, &session, &bar); err != nil {
			return nil, fmt.Errorf("flow scan: %w", err)
		}
		f.Day = dayFloor(f.Day)
		f.BuyIValue /= rialsPerToman
		f.BuyNValue /= rialsPerToman
		f.SellIValue /= rialsPerToman
		f.SellNValue /= rialsPerToman
		if bar != nil {
			v := *bar / rialsPerToman
			f.BarValue = &v
		}
		if session != nil {
			v := *session / rialsPerToman
			r.SessionValue = &v
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

func readPrices(ctx context.Context, pool *pgxpool.Pool, from, to time.Time) ([]priceRow, []dayFileRow, error) {
	rows, err := pool.Query(ctx, sectorFlowsPricesSelect, from, to)
	if err != nil {
		return nil, nil, fmt.Errorf("prices: %w", err)
	}
	defer rows.Close()
	var out []priceRow
	var files []dayFileRow
	for rows.Next() {
		var p priceRow
		var value float64
		var traded, dayFile bool
		if err := rows.Scan(&p.InsCode, &p.Day, &p.Close, &p.PriceYesterday, &value, &traded,
			&dayFile); err != nil {
			return nil, nil, fmt.Errorf("price scan: %w", err)
		}
		p.Day = dayFloor(p.Day)
		out = append(out, p)
		if dayFile && traded {
			files = append(files, dayFileRow{InsCode: p.InsCode, Day: p.Day, Value: value / rialsPerToman})
		}
	}
	if err := rows.Err(); err != nil {
		return nil, nil, err
	}
	// Ascending per share, so a reader of one share's prices reads them in order.
	sort.Slice(out, func(i, j int) bool {
		if out[i].InsCode != out[j].InsCode {
			return out[i].InsCode < out[j].InsCode
		}
		return out[i].Day.Before(out[j].Day)
	})
	return out, files, nil
}

func readSectorNames(ctx context.Context, pool *pgxpool.Pool) ([]sectorName, error) {
	rows, err := pool.Query(ctx, sectorFlowsSectorsSelect)
	if err != nil {
		return nil, fmt.Errorf("sectors: %w", err)
	}
	defer rows.Close()
	var out []sectorName
	for rows.Next() {
		var s sectorName
		if err := rows.Scan(&s.Code, &s.NameFA, &s.NameEN); err != nil {
			return nil, fmt.Errorf("sector scan: %w", err)
		}
		out = append(out, s)
	}
	return out, rows.Err()
}

// loadSectorFlows is the cached build: rebuilt when the key moves, otherwise
// memory.
func (h *Handler) loadSectorFlows(ctx context.Context) (*sectorFlowsBuilt, error) {
	store, err := h.storeFor(ctx)
	if err != nil {
		return nil, fmt.Errorf("index store: %w", err)
	}
	var key string
	if err := h.Pool.QueryRow(ctx, sectorFlowsKeySelect).Scan(&key); err != nil {
		return nil, fmt.Errorf("sector flows key: %w", err)
	}
	key += "#" + store.key
	h.flowMu.Lock()
	defer h.flowMu.Unlock()
	if h.flowStore != nil && h.flowStore.key == key {
		return h.flowStore, nil
	}
	in, err := readSectorFlowInput(ctx, h.Pool, store)
	if err != nil {
		return nil, err
	}
	built := buildSectorFlows(in)
	built.key = key
	h.flowStore = &built
	return h.flowStore, nil
}

// SectorFlows implements GET /api/v1/bourse/sector-flows.
func (h *Handler) SectorFlows(w http.ResponseWriter, r *http.Request) {
	b, err := h.loadSectorFlows(r.Context())
	if err != nil {
		h.Log.Error("bourse_sector_flows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	httpserver.JSON(w, http.StatusOK, b.response(time.Now()))
}

// response is the /sector-flows body as of `now`: the built figures, and how
// old they are today.
func (b *sectorFlowsBuilt) response(now time.Time) sectorFlowsResponse {
	resp := b.resp
	resp.DataAge = buildDataAge(b.newest, now)
	return resp
}

// defaultShareWindow is the window a sector's share list is read over when the
// request names none: a trading week.
const defaultShareWindow = 5

var sectorCodeRE = regexp.MustCompile(`^[0-9]{2}$`)

type sectorSharesResponse struct {
	SectorCode   string             `json:"sector_code"`
	NameFA       string             `json:"name_fa"`
	NameEN       string             `json:"name_en"`
	IndexInsCode string             `json:"index_ins_code,omitempty"`
	Window       string             `json:"window"`
	Sessions     sessionWindowItem  `json:"sessions"`
	Sector       *sectorWindowItem  `json:"sector"`
	Items        []shareFlowItem    `json:"items"`
	Count        int                `json:"count"`
	Coverage     sectorFlowCoverage `json:"coverage"`
	Checks       flowChecks         `json:"checks"`
	Notes        []string           `json:"notes"`
	DataAge      DataAge            `json:"data_age"`
}

const shareListNote = "Every share TSETMC classifies in this sector, each board its own row. A " +
	"share's value_share_pct is its part of the SECTOR's traded value over the window. A share " +
	"TSETMC's day files show trading with no flow stored for those sessions says so " +
	"(flow_not_stored_sessions); a listed share that did not trade in the window is listed with " +
	"empty figures. Only shares on this deployment's roster link to a share page."

// SectorFlowShares implements GET /api/v1/bourse/sector-flows/{sector}?window=.
func (h *Handler) SectorFlowShares(w http.ResponseWriter, r *http.Request) {
	code := strings.TrimSpace(chi.URLParam(r, "sector"))
	if !sectorCodeRE.MatchString(code) {
		httpserver.BadRequest(w, fmt.Sprintf("sector must be a two-digit TSETMC sector code, got %q", code),
			map[string]any{"sector": code})
		return
	}
	raw := strings.TrimSpace(r.URL.Query().Get("window"))
	if raw == "" {
		raw = fmt.Sprint(defaultShareWindow)
	}
	n := 0
	for _, k := range flowWindows {
		if fmt.Sprint(k) == raw {
			n = k
		}
	}
	if n == 0 {
		httpserver.BadRequest(w, fmt.Sprintf("window must be one of %v market sessions, got %q", flowWindows, raw),
			map[string]any{"window": raw, "supported": flowWindows})
		return
	}

	b, err := h.loadSectorFlows(r.Context())
	if err != nil {
		h.Log.Error("bourse_sector_flow_shares", "error", err, "sector", code)
		httpserver.Internal(w, "database error")
		return
	}
	resp, refusal := buildSectorShares(b, code, n, time.Now())
	if refusal != nil {
		httpserver.Error(w, refusal.Status, refusal.Code, refusal.Message, refusal.Details)
		return
	}
	httpserver.JSON(w, http.StatusOK, resp)
}

// shareListRefusal is a request the built data cannot answer.
type shareListRefusal struct {
	Status        int
	Code, Message string
	Details       map[string]any
}

// buildSectorShares is one sector's share list over window n. Before the
// market-wide flows exist it answers with the coverage and no items, as the
// main route does, rather than an error. Pure (unit tested).
func buildSectorShares(b *sectorFlowsBuilt, code string, n int, now time.Time) (sectorSharesResponse, *shareListRefusal) {
	key := fmt.Sprint(n)
	resp := sectorSharesResponse{SectorCode: code, Window: key, Sessions: b.resp.Sessions[key],
		Items: []shareFlowItem{}, Coverage: b.resp.Coverage, Checks: b.resp.Checks,
		DataAge: buildDataAge(b.newest, now)}
	if !b.resp.Coverage.MarketWide {
		resp.Notes = b.resp.Notes
		return resp, nil
	}
	sec, ok := b.sectors[code]
	if !ok {
		return resp, &shareListRefusal{Status: http.StatusNotFound, Code: "not_found",
			Message: fmt.Sprintf("no stored share is classified in sector %q", code),
			Details: map[string]any{"sector": code,
				"hint": "GET /api/v1/bourse/sector-flows lists every sector with stored flow"}}
	}
	if win := b.windows[n]; !win.Available {
		return resp, &shareListRefusal{Status: http.StatusConflict, Code: "window_unavailable",
			Message: win.item(b.resp.Coverage.SessionsAvailable).Reason,
			Details: map[string]any{"window": key, "sessions_available": b.resp.Coverage.SessionsAvailable}}
	}
	wi := sec.Windows[key]
	resp.NameFA, resp.NameEN, resp.IndexInsCode, resp.Sector = sec.NameFA, sec.NameEN, sec.IndexInsCode, &wi
	resp.Items = b.shares[code][n]
	resp.Count = len(resp.Items)
	resp.Notes = append([]string{shareListNote}, b.resp.Notes...)
	return resp, nil
}
