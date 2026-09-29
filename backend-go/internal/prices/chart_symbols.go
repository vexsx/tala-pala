package prices

// Which symbols the Trade chart can draw, and where each one's data lives.
//
// One endpoint, three sources, told apart by the symbol's shape:
//
//   - A registry code (IR_GOLD_18K, XAUUSD, IR_SILVER_999, …) is served from
//     the `prices` tick table, bucketed exactly as candles.go describes. The
//     set is KnownSymbols — the canonical set every older endpoint validates
//     against — plus every ENABLED `instruments` row whose data can honestly
//     be drawn as a price from `prices`: kind market_price or fx, quoted in
//     toman or dollars. A new commodity added to the registry therefore charts
//     without a code change once its rows land, and a row an operator disables
//     stops being offered the moment the cache below expires.
//   - IDX:<insCode> is a TSETMC index (market_indices), served from
//     internal/bourse's in-memory store.
//   - EQ:<insCode> is a roster share (equity_instruments), served adjusted by
//     internal/equities.
//
// The Tehran shapes are ASCII and carry the exchange's own numeric key, not a
// Persian symbol: a drawing stored against IDX:32097828799138957 survives a
// renamed index and needs no letter folding, and ToUpper leaves it alone.
// No registry code contains a colon, so the two vocabularies cannot collide.

import (
	"context"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// candleSource is where a chart symbol's rows are read from.
type candleSource int

const (
	sourceTicks candleSource = iota
	sourceTSEIndex
	sourceTSEEquity
)

// tehranChartSymbolRE matches IDX:<insCode> and EQ:<insCode>. TSETMC insCodes
// are decimal and 6 to 20 digits long in every table this deployment holds.
var tehranChartSymbolRE = regexp.MustCompile(`^(IDX|EQ):([0-9]{6,20})$`)

// tickSymbolRE is the shape of a registry code. It gates the registry lookup,
// so nothing that could never be a code costs a read.
var tickSymbolRE = regexp.MustCompile(`^[A-Z][A-Z0-9_]{1,63}$`)

// normalizeChartSymbol is the one normalization every chart endpoint applies:
// drawings always did, and candles now does the same so both answer to the
// same spelling.
func normalizeChartSymbol(raw string) string {
	return strings.ToUpper(strings.TrimSpace(raw))
}

// classifyCandleSymbol says where a symbol's rows live. `code` is the insCode
// for a Tehran symbol and the registry code otherwise; `canonical` is the
// normalized symbol echoed on the response. ok is false for anything that is
// neither shape — whether a well-shaped registry code is actually served is a
// separate question (tickSymbolServed), because only the registry can answer it.
func classifyCandleSymbol(raw string) (src candleSource, code, canonical string, ok bool) {
	s := normalizeChartSymbol(raw)
	if m := tehranChartSymbolRE.FindStringSubmatch(s); m != nil {
		if m[1] == "IDX" {
			return sourceTSEIndex, m[2], s, true
		}
		return sourceTSEEquity, m[2], s, true
	}
	if tickSymbolRE.MatchString(s) {
		return sourceTicks, s, s, true
	}
	return sourceTicks, "", s, false
}

// IsTehranChartSymbol reports whether s (already normalized) names a Tehran
// index or share on the chart.
func IsTehranChartSymbol(s string) bool {
	return tehranChartSymbolRE.MatchString(s)
}

// symbolServed answers "is this registry code drawn from `prices`?" for a
// validator that must stay a pure function.
type symbolServed func(symbol string) bool

// knownSymbolsOnly is the answer without a registry: the canonical set.
func knownSymbolsOnly(symbol string) bool { return KnownSymbols[symbol] }

// --- the registry ------------------------------------------------------------

// RegistrySymbol is one `instruments` row the chart may draw from `prices`.
type RegistrySymbol struct {
	Code          string
	Kind          string
	Domain        string
	QuoteCurrency string
	Unit          string
	NameEN        string
	NameFA        string
	Decimals      int
}

// TickSymbolRegistry lists the registry codes served from `prices` beyond
// KnownSymbols. Handler.Registry is nil in a unit test, which then serves the
// canonical set only.
type TickSymbolRegistry interface {
	TickSymbols(ctx context.Context) (map[string]RegistrySymbol, error)
}

// registryRow is an `instruments` row as scanned, before the chart filter.
type registryRow struct {
	RegistrySymbol
	Enabled bool
}

// The kinds and quote currencies a registry row must have to be drawn as a
// price. Everything else in `instruments` is excluded on purpose: an
// economic_series is monthly and lives in economic_observations, not
// `prices`; IR_GOLD_FUND_FLOW is kind=index quoted in PCT because it is a flow
// RATIO, and drawing it on a price axis would present a percentage as a price
// (migration 0024's comment on that row makes the same argument); DXY and
// US10Y stay reachable through KnownSymbols but are not offered as commodities.
var (
	chartableRegistryKinds  = map[string]bool{"market_price": true, "fx": true}
	chartableRegistryQuotes = map[string]bool{"IRT": true, "USD": true}
)

// chartableRegistryRow is the filter. Pure (unit tested).
func chartableRegistryRow(r registryRow) bool {
	return r.Enabled && chartableRegistryKinds[r.Kind] && chartableRegistryQuotes[r.QuoteCurrency] &&
		tickSymbolRE.MatchString(r.Code)
}

// tickSymbolsFrom keeps the chartable rows. Pure (unit tested).
func tickSymbolsFrom(rows []registryRow) map[string]RegistrySymbol {
	out := make(map[string]RegistrySymbol, len(rows))
	for _, r := range rows {
		if chartableRegistryRow(r) {
			out[r.Code] = r.RegistrySymbol
		}
	}
	return out
}

// The whole table: it is a few dozen rows, and filtering in Go keeps the rule
// in one tested function instead of split between SQL and code.
const registrySelect = `
	SELECT code, kind, domain, quote_currency, unit, name_en, name_fa, decimals, enabled
	FROM instruments`

// registryTTL bounds how long a registry edit takes to reach the chart. The
// registry changes by migration or by an operator's UPDATE, never per request,
// and the chart polls every minute — five minutes of lag on "this new symbol
// is now chartable" costs nothing, while a read per poll costs a query.
const registryTTL = 5 * time.Minute

// InstrumentRegistry is the cached registry read main.go wires into Handler.
type InstrumentRegistry struct {
	load func(ctx context.Context) ([]registryRow, error)
	ttl  time.Duration
	now  func() time.Time

	mu      sync.Mutex
	symbols map[string]RegistrySymbol
	expires time.Time
}

// NewInstrumentRegistry reads `instruments` through pool.
func NewInstrumentRegistry(pool *pgxpool.Pool) *InstrumentRegistry {
	return newInstrumentRegistry(func(ctx context.Context) ([]registryRow, error) {
		return readRegistry(ctx, pool)
	}, registryTTL, time.Now)
}

func newInstrumentRegistry(load func(ctx context.Context) ([]registryRow, error),
	ttl time.Duration, now func() time.Time) *InstrumentRegistry {
	return &InstrumentRegistry{load: load, ttl: ttl, now: now}
}

// TickSymbols returns the chartable registry codes, reading the table at most
// once per TTL. A failed read is returned, never cached: the next request
// retries rather than serving an empty vocabulary for five minutes.
func (r *InstrumentRegistry) TickSymbols(ctx context.Context) (map[string]RegistrySymbol, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	now := r.now()
	if r.symbols != nil && now.Before(r.expires) {
		return r.symbols, nil
	}
	rows, err := r.load(ctx)
	if err != nil {
		return nil, err
	}
	r.symbols = tickSymbolsFrom(rows)
	r.expires = now.Add(r.ttl)
	return r.symbols, nil
}

func readRegistry(ctx context.Context, pool *pgxpool.Pool) ([]registryRow, error) {
	rows, err := pool.Query(ctx, registrySelect)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var out []registryRow
	for rows.Next() {
		var r registryRow
		if err := rows.Scan(&r.Code, &r.Kind, &r.Domain, &r.QuoteCurrency, &r.Unit,
			&r.NameEN, &r.NameFA, &r.Decimals, &r.Enabled); err != nil {
			return nil, err
		}
		out = append(out, r)
	}
	return out, rows.Err()
}

// tickSymbolServed reports whether a registry code is drawn from `prices`.
// KnownSymbols answer without a read, so the gold chart never waits on the
// registry.
func (h *Handler) tickSymbolServed(ctx context.Context, symbol string) (bool, error) {
	if KnownSymbols[symbol] {
		return true, nil
	}
	if h.Registry == nil || !tickSymbolRE.MatchString(symbol) {
		return false, nil
	}
	set, err := h.Registry.TickSymbols(ctx)
	if err != nil {
		return false, err
	}
	_, ok := set[symbol]
	return ok, nil
}

// servedLookup adapts tickSymbolServed to a pure validator's predicate and
// keeps the first read error, so the caller can answer 500 instead of letting
// a database failure pose as "unknown symbol".
type servedLookup struct {
	h   *Handler
	ctx context.Context
	err error
}

func (l *servedLookup) served(symbol string) bool {
	ok, err := l.h.tickSymbolServed(l.ctx, symbol)
	if err != nil && l.err == nil {
		l.err = err
	}
	return ok
}
