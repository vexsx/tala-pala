package relvalue

// The instrument registry, read as this package needs it.
//
// migration 0024 created `instruments` precisely so that consumers stop
// hard-coding symbol lists, and this package takes it at its word: the set of
// assets the performance table covers, what currency each is quoted in, and
// whether a code is a rate rather than a price are all READ from the registry.
// Adding a new instrument must not require editing this file.

import (
	"strings"
	"context"
	"sort"
)

// instrumentRow is the registry row as this package uses it. The provenance
// columns are not decoration: quality_tier and is_proxy travel onto every
// number derived from the instrument, because USD_IRT is a free-market proxy
// and a client that renders it like an official mirror is misrepresenting it.
type instrumentRow struct {
	Code          string
	Kind          string
	NameEN        string
	NameFA        string
	Domain        string
	QuoteCurrency string
	Unit          string
	Decimals      int
	QualityTier   string
	IsProxy       bool
	IsDerived     bool
	Enabled       bool
	Notes         string
}

const instrumentSelect = `
	SELECT code, kind, name_en, name_fa, domain, quote_currency, unit, decimals,
	       quality_tier, is_proxy, is_derived, enabled, notes
	FROM instruments
	ORDER BY domain, code`

// loadInstruments reads the whole registry. It is a dozen rows; filtering
// happens in Go so the refusal messages can name what was excluded and why.
func (h *Handler) loadInstruments(ctx context.Context) ([]instrumentRow, error) {
	rows, err := h.Pool.Query(ctx, instrumentSelect)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []instrumentRow{}
	for rows.Next() {
		var it instrumentRow
		if err := rows.Scan(&it.Code, &it.Kind, &it.NameEN, &it.NameFA, &it.Domain,
			&it.QuoteCurrency, &it.Unit, &it.Decimals, &it.QualityTier,
			&it.IsProxy, &it.IsDerived, &it.Enabled, &it.Notes); err != nil {
			return nil, err
		}
		out = append(out, it)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	sortInstruments(out)
	return out, nil
}

// sortInstruments applies the published order: domain, then code.
// instrumentSelect already returns rows this way; re-applying it makes the
// ordering testable without a database and costs nothing at registry size.
func sortInstruments(rows []instrumentRow) {
	sort.SliceStable(rows, func(i, j int) bool {
		a, b := rows[i], rows[j]
		if a.Domain != b.Domain {
			return a.Domain < b.Domain
		}
		return a.Code < b.Code
	})
}

// findInstrument looks a code up in a loaded registry.
func findInstrument(rows []instrumentRow, code string) (instrumentRow, bool) {
	for _, r := range rows {
		if r.Code == code {
			return r, true
		}
	}
	return instrumentRow{}, false
}

// instrumentRef is the identity block every item on the wire carries. A number
// that cannot say which instrument it describes, how good that instrument is
// and whether it is a proxy does not ship.
type instrumentRef struct {
	Code          string `json:"code"`
	NameEN        string `json:"name_en"`
	NameFA        string `json:"name_fa"`
	Domain        string `json:"domain"`
	QuoteCurrency string `json:"quote_currency"`
	Unit          string `json:"unit"`
	QualityTier   string `json:"quality_tier"`
	IsProxy       bool   `json:"is_proxy"`
	IsDerived     bool   `json:"is_derived"`
	// Notes carries the registry's own caveat about this instrument -- that
	// XAUUSD is a COMEX front-month future and not the London spot fix, that
	// USD_IRT is the USDT/toman market. It travels with the numbers rather than
	// being left on /instruments, where a client charting a return would never
	// see it.
	//
	// It is an ARRAY, and that matters for one reason: `notes` must have the
	// SAME arity everywhere in this feature. performanceItem embeds this struct
	// and publishes its own `notes` array at depth 0; encoding/json resolves a
	// tag collision in favour of the shallower field, so the depth-0 array wins
	// there and buildPerformanceItem copies this value into it. On
	// /relative-value, where instrumentRef is a whole leg and nothing shadows it,
	// this field marshals directly. Were it a scalar string here, one key would
	// mean an array on two endpoints and a string on a third, and the first
	// client to copy the `(leg.notes ?? []).map(...)` idiom across would throw at
	// runtime.
	//
	// NOTE for anyone re-reading the original bug: renaming the GO FIELD does
	// nothing -- encoding/json collides on the TAG, not the identifier. What
	// keeps the caveat on the wire is buildPerformanceItem copying it.
	Notes []string `json:"notes"`
}

func refOf(r instrumentRow) instrumentRef {
	return instrumentRef{
		Code: r.Code, NameEN: r.NameEN, NameFA: r.NameFA, Domain: r.Domain,
		QuoteCurrency: r.QuoteCurrency, Unit: r.Unit, QualityTier: r.QualityTier,
		IsProxy: r.IsProxy, IsDerived: r.IsDerived, Notes: registryNotes(r.Notes),
	}
}

// registryNotes lifts the registry's single free-text caveat into the array
// arity `notes` uses everywhere else in this feature. An instrument with no
// caveat gets an empty (non-nil) slice so the key marshals as [] rather than
// null -- a client mapping over it must never have to nil-check.
func registryNotes(note string) []string {
	if strings.TrimSpace(note) == "" {
		return []string{}
	}
	return []string{note}
}
