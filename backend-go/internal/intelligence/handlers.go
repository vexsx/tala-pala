// Package intelligence serves the read-only news/intelligence endpoints.
//
// This package READS. It never classifies, scores, forecasts or writes: the
// taxonomy, the impact hypotheses and the consolidation live in the Python
// service, and NEWS_ML_ENABLED gates the only path by which any of it could
// reach a model. Nothing here is reachable from feature building or prediction
// code, and the projection deliberately omits every internal column
// (raw payloads, bodies, content hashes, classifier confidences, rule ids) so
// an unreviewed hypothesis cannot leak to a client that might treat it as a
// finding.
package intelligence

import (
	"fmt"
	"html"
	"log/slog"
	"net/http"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

// Handler serves /api/v1/intelligence/*.
type Handler struct {
	Pool *pgxpool.Pool
	Log  *slog.Logger
	// NewsAPIEnabled (NEWS_API_ENABLED) gates the endpoint itself.
	NewsAPIEnabled bool
	// NewsCollectionEnabled (NEWS_COLLECTION_ENABLED) is echoed to the client
	// so an empty list can be explained as "collection is off" instead of
	// being read as "nothing is happening in the world".
	NewsCollectionEnabled bool
}

const (
	defaultNewsLimit = 20
	maxNewsLimit     = 100
)

// ParseNewsLimit resolves the ?limit= parameter: empty means the default, and
// anything non-numeric or outside [1, maxNewsLimit] is an error.
//
// Unlike the older market endpoints (which silently clamp), this one refuses:
// a caller that asked for 500 items and got 100 without being told cannot
// distinguish "that is all there is" from "you were truncated", and the news
// list is exactly where that difference matters.
func ParseNewsLimit(raw string) (int, error) {
	if raw == "" {
		return defaultNewsLimit, nil
	}
	n, err := strconv.Atoi(raw)
	if err != nil {
		return 0, fmt.Errorf("limit must be an integer, got %q", raw)
	}
	if n < 1 || n > maxNewsLimit {
		return 0, fmt.Errorf("limit must be between 1 and %d, got %d", maxNewsLimit, n)
	}
	return n, nil
}

// newsRow is one already-deduplicated article as read from Postgres. It holds
// display fields only; see the package comment for what is deliberately absent.
type newsRow struct {
	ID                   int64
	SourceCode           string
	SourceName           string
	Title                string
	URL                  string
	PublishedAt          *time.Time
	PublishedAtEstimated bool
	AvailableAt          time.Time
	Urgent               bool
	Tags                 []string
	Entities             []string
	IndependentSources   int
	DuplicateCount       int
}

type newsItem struct {
	ID                   int64      `json:"id"`
	SourceCode           string     `json:"source_code"`
	SourceName           string     `json:"source_name"`
	Title                string     `json:"title"`
	URL                  string     `json:"url"`
	PublishedAt          *time.Time `json:"published_at"`
	PublishedAtEstimated bool       `json:"published_at_estimated"`
	AvailableAt          time.Time  `json:"available_at"`
	Urgency              string     `json:"urgency"`
	Tags                 []string   `json:"tags"`
	Entities             []string   `json:"entities"`
	IndependentSources   int        `json:"independent_source_count"`
	DuplicateCount       int        `json:"duplicate_count"`
}

type newsResponse struct {
	Items             []newsItem `json:"items"`
	Count             int        `json:"count"`
	UrgentCount       int        `json:"urgent_count"`
	CollectionEnabled bool       `json:"collection_enabled"`
	NewestAvailableAt *time.Time `json:"newest_available_at"`
	AsOf              time.Time  `json:"as_of"`
}

// newsSelect reads the display view of the news archive.
//
// Shape notes, each one a rule from docs/CONTRACTS.md rather than a
// convenience:
//   - duplicate_of IS NULL: one wire story republished by twenty sites is ONE
//     row here. The suppressed copies are counted, not listed.
//   - independent_source_count counts distinct SOURCES across the duplicate
//     group (the article itself plus everything pointing at it), so
//     syndication cannot inflate corroboration.
//   - available_at is the only clock that is never null: it is when this system
//     could first have acted on the item. published_at is what the source
//     claimed, and is reported as null when the source claimed nothing.
//   - urgency is derived from the linked event's stored severity. Go does not
//     decide what is severe; the classifier already did, in Python.
const newsSelect = `
	SELECT a.id,
	       a.source_code,
	       s.name AS source_name,
	       a.title,
	       COALESCE(NULLIF(a.canonical_url, ''), a.url) AS url,
	       -- news_articles.published_at is NOT NULL: when a source stated no
	       -- time, the collector substituted its own clock and set the
	       -- estimated flag. Passing that substitute off as a publication time
	       -- would be a fabrication, so it is reported as null and the flag
	       -- explains why.
	       CASE WHEN a.published_at_estimated THEN NULL ELSE a.published_at END AS published_at,
	       a.published_at_estimated,
	       COALESCE(a.available_at, a.ingested_at) AS available_at,
	       COALESCE(bool_or(e.severity = 'high'), FALSE) AS urgent,
	       COALESCE(array_agg(DISTINCT c.category) FILTER (WHERE c.category IS NOT NULL),
	                '{}'::text[]) AS tags,
	       COALESCE(array_agg(DISTINCT en.display_name) FILTER (WHERE en.display_name IS NOT NULL),
	                '{}'::text[]) AS entities,
	       (SELECT count(DISTINCT d.source_code) FROM news_articles d
	         WHERE d.id = a.id OR d.duplicate_of = a.id) AS independent_source_count,
	       (SELECT count(*) FROM news_articles d WHERE d.duplicate_of = a.id) AS duplicate_count
	FROM news_articles a
	JOIN news_sources s ON s.code = a.source_code
	LEFT JOIN news_event_articles ea ON ea.article_id = a.id
	LEFT JOIN news_events e ON e.id = ea.event_id
	LEFT JOIN news_event_classifications c ON c.event_id = e.id
	LEFT JOIN news_article_entities ae ON ae.article_id = a.id
	LEFT JOIN news_entities en ON en.id = ae.entity_id
	WHERE a.duplicate_of IS NULL
	GROUP BY a.id, s.name
	ORDER BY COALESCE(bool_or(e.severity = 'high'), FALSE) DESC,
	         COALESCE(a.available_at, a.ingested_at) DESC,
	         a.id DESC
	LIMIT $1`

// News implements GET /api/v1/intelligence/news?limit=20.
func (h *Handler) News(w http.ResponseWriter, r *http.Request) {
	if !h.NewsAPIEnabled {
		httpserver.Error(w, http.StatusServiceUnavailable, "news_api_disabled",
			"the news API is disabled on this deployment", nil)
		return
	}
	raw := r.URL.Query().Get("limit")
	limit, err := ParseNewsLimit(raw)
	if err != nil {
		httpserver.BadRequest(w, err.Error(), map[string]any{"limit": raw})
		return
	}

	rows, err := h.Pool.Query(r.Context(), newsSelect, limit)
	if err != nil {
		h.Log.Error("intelligence_news", "error", err)
		httpserver.Internal(w, "database error")
		return
	}
	defer rows.Close()
	items := []newsRow{}
	for rows.Next() {
		var it newsRow
		if err := rows.Scan(&it.ID, &it.SourceCode, &it.SourceName, &it.Title, &it.URL,
			&it.PublishedAt, &it.PublishedAtEstimated, &it.AvailableAt, &it.Urgent,
			&it.Tags, &it.Entities, &it.IndependentSources, &it.DuplicateCount); err != nil {
			h.Log.Error("intelligence_news_scan", "error", err)
			httpserver.Internal(w, "database error")
			return
		}
		items = append(items, it)
	}
	if err := rows.Err(); err != nil {
		h.Log.Error("intelligence_news_rows", "error", err)
		httpserver.Internal(w, "database error")
		return
	}

	httpserver.JSON(w, http.StatusOK,
		buildNewsResponse(items, h.NewsCollectionEnabled, time.Now().UTC()))
}

// sortNewsRows applies the documented order: urgent before normal, then newest
// available_at, then highest id. newsSelect already returns rows this way; the
// comparator exists so the contract is expressible (and tested) without a
// database, and re-applying it costs nothing at these page sizes.
func sortNewsRows(rows []newsRow) {
	sort.SliceStable(rows, func(i, j int) bool {
		a, b := rows[i], rows[j]
		if a.Urgent != b.Urgent {
			return a.Urgent
		}
		if !a.AvailableAt.Equal(b.AvailableAt) {
			return a.AvailableAt.After(b.AvailableAt)
		}
		return a.ID > b.ID
	})
}

// buildNewsResponse turns scanned rows into the wire response. Pure function
// (unit tested): no clock, no database, no configuration beyond the two values
// passed in.
func buildNewsResponse(rows []newsRow, collectionEnabled bool, asOf time.Time) newsResponse {
	sortNewsRows(rows)

	out := newsResponse{
		Items:             make([]newsItem, 0, len(rows)),
		CollectionEnabled: collectionEnabled,
		AsOf:              asOf.UTC(),
	}
	for _, r := range rows {
		// newest_available_at is the max over the page, NOT the first row:
		// urgency sorts ahead of recency, so the first row is often older.
		at := r.AvailableAt.UTC()
		if out.NewestAvailableAt == nil || at.After(*out.NewestAvailableAt) {
			newest := at
			out.NewestAvailableAt = &newest
		}
		if r.Urgent {
			out.UrgentCount++
		}
		out.Items = append(out.Items, newsRowToItem(r))
	}
	out.Count = len(out.Items)
	return out
}

func newsRowToItem(r newsRow) newsItem {
	urgency := "normal"
	if r.Urgent {
		urgency = "urgent"
	}
	var published *time.Time
	if r.PublishedAt != nil {
		t := r.PublishedAt.UTC()
		published = &t
	}
	return newsItem{
		ID:                   r.ID,
		SourceCode:           r.SourceCode,
		SourceName:           r.SourceName,
		Title:                PlainText(r.Title),
		URL:                  r.URL,
		PublishedAt:          published,
		PublishedAtEstimated: r.PublishedAtEstimated,
		AvailableAt:          r.AvailableAt.UTC(),
		Urgency:              urgency,
		Tags:                 nonNilStrings(r.Tags),
		Entities:             nonNilStrings(r.Entities),
		IndependentSources:   r.IndependentSources,
		DuplicateCount:       r.DuplicateCount,
	}
}

// nonNilStrings keeps an absent aggregate rendering as [] rather than null, so
// a client never has to special-case the difference.
func nonNilStrings(v []string) []string {
	if v == nil {
		return []string{}
	}
	return v
}

// htmlTag matches real markup only: a letter must follow "<" or "</", so a
// headline like "gold < 2% of reserves" survives untouched.
var htmlTag = regexp.MustCompile(`</?[A-Za-z][^>]*>`)

// PlainText renders a stored title as text. The archive keeps the source's own
// bytes (an edit has to be diffable against what was actually published), so a
// feed that embeds markup in its <title> would otherwise hand HTML to the UI.
// Tags are removed BEFORE entities are decoded, so an escaped "&lt;b&gt;" in a
// headline decodes to visible text instead of being deleted as a tag.
func PlainText(s string) string {
	s = htmlTag.ReplaceAllString(s, "")
	s = html.UnescapeString(s)
	return strings.Join(strings.Fields(s), " ")
}
