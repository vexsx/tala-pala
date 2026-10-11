package economic

// The point-in-time read again, with each selected row's citation.
//
// internal/liquidity publishes CBI's money stocks, and its contract carries,
// per month, what PointInTime deliberately leaves out: when the source
// published the value, which document it came from (CBI's monthly Excel
// edition or its tsdview history), and the base-money DEFINITION the value was
// printed under -- CBI redefined base money from 1399-12, and growth must
// never be computed across the two. Those live on the observation row and on
// source_documents. So this is PointInTime plus a second, read-only lookup
// keyed by the rows PointInTime selected: the selection is still THE rule, run
// once, in PointInTime; the lookup only decorates its result and can neither
// add a period nor change which vintage was chosen.

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"
)

// SourceDocument is the archived document an observation cites.
type SourceDocument struct {
	ID        int64
	URL       string
	Title     string
	MediaType string
	FetchedAt time.Time
}

// ObservationDetail is one point-in-time observation with its publication
// time, nowcast flag and citation.
type ObservationDetail struct {
	Observation
	// PublishedAt is what the source claimed, nil when it claimed nothing. It
	// is never a substitute for AvailableAt.
	PublishedAt *time.Time
	IsNowcast   bool
	// Source is nil when the row cites no document.
	Source *SourceDocument
	// Columns is the selected row exactly as stored, every column by name
	// (to_jsonb), so a consumer can read a column a later migration added --
	// CBI's base-money method flag -- without this package modelling every
	// provider's extras. Never nil.
	Columns map[string]any
}

// observationDetailSelect fetches the publication time, nowcast flag,
// citation and stored columns of every non-projection row available at the
// cutoff. It selects nothing: attachDetails keeps only the rows PointInTime
// chose, matched on (ref_period_start, vintage), which economic_obs_unique
// makes unique within a series.
const observationDetailSelect = `
	SELECT o.ref_period_start, o.vintage, o.published_at, o.is_nowcast,
	       d.id, d.url, d.title, d.media_type, d.fetched_at,
	       to_jsonb(o)
	FROM economic_observations o
	JOIN economic_series s ON s.id = o.series_id
	LEFT JOIN source_documents d ON d.id = o.source_document_id
	WHERE s.code = $1
	  AND o.available_at <= $2::timestamptz
	  AND NOT o.is_projection`

// detailKey identifies one stored row of a series.
type detailKey struct {
	Start   string
	Vintage int
}

// detailRow is what observationDetailSelect returns for one row.
type detailRow struct {
	PublishedAt *time.Time
	IsNowcast   bool
	Source      *SourceDocument
	Columns     map[string]any
}

// PointInTimeDetailed is PointInTime -- same selection, same order, same
// ErrSeriesNotFound -- with each selected observation's publication time,
// nowcast flag, cited document and stored columns attached.
func PointInTimeDetailed(ctx context.Context, pool *pgxpool.Pool, code string, asOf time.Time) (SeriesProvenance, []ObservationDetail, error) {
	prov, obs, err := PointInTime(ctx, pool, code, asOf)
	if err != nil {
		return prov, nil, err
	}
	rows, err := pool.Query(ctx, observationDetailSelect, code, asOf.UTC())
	if err != nil {
		return prov, nil, err
	}
	defer rows.Close()
	details := map[detailKey]detailRow{}
	for rows.Next() {
		var (
			start    time.Time
			vintage  int
			r        detailRow
			docID    *int64
			docURL   *string
			docTitle *string
			docMedia *string
			docAt    *time.Time
			columns  []byte
		)
		if err := rows.Scan(&start, &vintage, &r.PublishedAt, &r.IsNowcast,
			&docID, &docURL, &docTitle, &docMedia, &docAt, &columns); err != nil {
			return prov, nil, err
		}
		if docID != nil {
			r.Source = &SourceDocument{ID: *docID, URL: deref(docURL), Title: deref(docTitle),
				MediaType: deref(docMedia)}
			if docAt != nil {
				r.Source.FetchedAt = docAt.UTC()
			}
		}
		if len(columns) > 0 {
			if err := json.Unmarshal(columns, &r.Columns); err != nil {
				return prov, nil, fmt.Errorf("observation columns: %w", err)
			}
		}
		details[detailKey{Start: start.UTC().Format(dateLayout), Vintage: vintage}] = r
	}
	if err := rows.Err(); err != nil {
		return prov, nil, err
	}
	return prov, attachDetails(obs, details), nil
}

// attachDetails decorates the selected observations, keeping their order. A
// selected row with no detail (the two statements are separate reads) keeps
// its value and simply carries no citation: a value is never dropped for want
// of decoration, and no detail row can add a period.
func attachDetails(obs []Observation, details map[detailKey]detailRow) []ObservationDetail {
	out := make([]ObservationDetail, 0, len(obs))
	for _, o := range obs {
		d := details[detailKey{Start: o.RefPeriodStart.UTC().Format(dateLayout), Vintage: o.Vintage}]
		od := ObservationDetail{Observation: o, IsNowcast: d.IsNowcast, Source: d.Source, Columns: d.Columns}
		if d.PublishedAt != nil {
			p := d.PublishedAt.UTC()
			od.PublishedAt = &p
		}
		if od.Columns == nil {
			od.Columns = map[string]any{}
		}
		out = append(out, od)
	}
	return out
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}
