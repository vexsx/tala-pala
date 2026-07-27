package intelligence

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/httpserver"
)

func quietLogger() *slog.Logger {
	return slog.New(slog.NewTextHandler(io.Discard, nil))
}

func TestParseNewsLimit(t *testing.T) {
	cases := []struct {
		name    string
		raw     string
		want    int
		wantErr bool
	}{
		{"absent uses default", "", defaultNewsLimit, false},
		{"minimum", "1", 1, false},
		{"explicit default", "20", 20, false},
		{"maximum", "100", 100, false},
		{"zero rejected", "0", 0, true},
		{"negative rejected", "-1", 0, true},
		{"non numeric rejected", "abc", 0, true},
		{"above maximum rejected", "101", 0, true},
		{"float rejected", "20.5", 0, true},
		{"padded rejected", " 20", 0, true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ParseNewsLimit(tc.raw)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("limit %q accepted, got %d", tc.raw, got)
				}
				return
			}
			if err != nil {
				t.Fatalf("limit %q rejected: %v", tc.raw, err)
			}
			if got != tc.want {
				t.Fatalf("limit %q: got %d, want %d", tc.raw, got, tc.want)
			}
		})
	}
}

// The disabled flag must be checked before anything touches the database: the
// nil pool here is the assertion that it is.
func TestNews_DisabledReturns503(t *testing.T) {
	h := &Handler{Log: quietLogger(), NewsAPIEnabled: false}
	rec := httptest.NewRecorder()
	h.News(rec, httptest.NewRequest("GET", "/api/v1/intelligence/news", nil))

	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("got status %d, want 503", rec.Code)
	}
	var body httpserver.ErrorBody
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Error.Code != "news_api_disabled" {
		t.Fatalf("wrong error code: %q", body.Error.Code)
	}
}

// A bad limit is rejected before the query too, so this also pins the order of
// the two gates.
func TestNews_InvalidLimitReturns400(t *testing.T) {
	h := &Handler{Log: quietLogger(), NewsAPIEnabled: true}
	for _, raw := range []string{"0", "-1", "abc", "101"} {
		rec := httptest.NewRecorder()
		h.News(rec, httptest.NewRequest("GET", "/api/v1/intelligence/news?limit="+raw, nil))
		if rec.Code != http.StatusBadRequest {
			t.Fatalf("limit=%s: got status %d, want 400", raw, rec.Code)
		}
		var body httpserver.ErrorBody
		if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
			t.Fatal(err)
		}
		if body.Error.Code != "bad_request" {
			t.Fatalf("limit=%s: wrong error code %q", raw, body.Error.Code)
		}
	}
}

func at(hour int) time.Time {
	return time.Date(2026, 7, 27, hour, 0, 0, 0, time.UTC)
}

// Rows as they would come back from a scan, in a deliberately wrong order.
func unorderedRows() []newsRow {
	return []newsRow{
		{ID: 1, SourceCode: "fed_press", SourceName: "Federal Reserve Board press releases",
			Title: "Statement", URL: "https://www.federalreserve.gov/a",
			PublishedAt: ptr(at(8)), AvailableAt: at(9),
			Tags: []string{"monetary_policy"}, IndependentSources: 1},
		{ID: 2, SourceCode: "fed_press", SourceName: "Federal Reserve Board press releases",
			Title: "Later statement", URL: "https://www.federalreserve.gov/b",
			AvailableAt: at(11), PublishedAtEstimated: true,
			Tags: []string{}, Entities: []string{}, IndependentSources: 1},
		{ID: 3, SourceCode: "ofac", SourceName: "OFAC sanctions actions",
			Title: "Designations", URL: "https://home.treasury.gov/c",
			PublishedAt: ptr(at(7)), AvailableAt: at(10), Urgent: true,
			Tags: []string{"sanctions"}, Entities: []string{"Iran"},
			IndependentSources: 2, DuplicateCount: 1},
		{ID: 4, SourceCode: "ofac", SourceName: "OFAC sanctions actions",
			Title: "Designations addendum", URL: "https://home.treasury.gov/d",
			PublishedAt: ptr(at(7)), AvailableAt: at(10), Urgent: true,
			Tags: []string{"sanctions"}, IndependentSources: 1},
	}
}

func ptr(t time.Time) *time.Time { return &t }

func TestSortNewsRows_UrgencyThenRecencyThenID(t *testing.T) {
	rows := unorderedRows()
	sortNewsRows(rows)

	var got []int64
	for _, r := range rows {
		got = append(got, r.ID)
	}
	// Urgent rows first; they share available_at, so the higher id wins. Then
	// the normal rows, newest available_at first.
	want := []int64{4, 3, 2, 1}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("order = %v, want %v", got, want)
		}
	}
}

func TestBuildNewsResponse(t *testing.T) {
	asOf := at(12)
	resp := buildNewsResponse(unorderedRows(), false, asOf)

	if resp.Count != 4 {
		t.Fatalf("count = %d, want 4", resp.Count)
	}
	if resp.UrgentCount != 2 {
		t.Fatalf("urgent_count = %d, want 2", resp.UrgentCount)
	}
	if resp.CollectionEnabled {
		t.Fatal("collection_enabled must mirror the flag that was passed in")
	}
	if !resp.AsOf.Equal(asOf) {
		t.Fatalf("as_of = %v, want %v", resp.AsOf, asOf)
	}
	// The newest item is a NORMAL one that sorts third: the maximum has to be
	// taken over the page, not read off the first row.
	if resp.NewestAvailableAt == nil || !resp.NewestAvailableAt.Equal(at(11)) {
		t.Fatalf("newest_available_at = %v, want %v", resp.NewestAvailableAt, at(11))
	}
	if resp.Items[0].Urgency != "urgent" || resp.Items[3].Urgency != "normal" {
		t.Fatalf("urgency mapping wrong: %q / %q", resp.Items[0].Urgency, resp.Items[3].Urgency)
	}
	if resp.Items[0].DuplicateCount != 0 || resp.Items[1].DuplicateCount != 1 {
		t.Fatalf("duplicate_count not carried through: %+v", resp.Items[:2])
	}
	// Row 2 had no source-stated publication time.
	estimated := resp.Items[2]
	if estimated.ID != 2 || !estimated.PublishedAtEstimated || estimated.PublishedAt != nil {
		t.Fatalf("estimated publication time must be reported as null: %+v", estimated)
	}
}

func TestBuildNewsResponse_Empty(t *testing.T) {
	resp := buildNewsResponse(nil, true, at(12))
	if resp.NewestAvailableAt != nil {
		t.Fatalf("newest_available_at must be null when empty, got %v", resp.NewestAvailableAt)
	}
	if !resp.CollectionEnabled {
		t.Fatal("collection_enabled must mirror the flag that was passed in")
	}
	b, err := json.Marshal(resp)
	if err != nil {
		t.Fatal(err)
	}
	got := string(b)
	if !strings.Contains(got, `"items":[]`) {
		t.Fatalf("empty items must encode as [], got %s", got)
	}
	if !strings.Contains(got, `"newest_available_at":null`) {
		t.Fatalf("newest_available_at must encode as null, got %s", got)
	}
}

func TestNewsResponseJSON_ShapeAndOmissions(t *testing.T) {
	b, err := json.Marshal(buildNewsResponse(unorderedRows(), false, at(12)))
	if err != nil {
		t.Fatal(err)
	}
	got := string(b)

	// Internal columns must never reach a client, whatever a future row
	// carries: no evidence payloads, no dedupe hashes, no classifier scores.
	for _, forbidden := range []string{
		"raw_payload", "content_hash", "confidence", "body", "rule_id",
		"title_key", "hypothesis", "score",
	} {
		if strings.Contains(got, forbidden) {
			t.Fatalf("response leaks %q: %s", forbidden, got)
		}
	}

	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(b, &envelope); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"items", "count", "urgent_count",
		"collection_enabled", "newest_available_at", "as_of"} {
		if _, ok := envelope[key]; !ok {
			t.Fatalf("response is missing %q: %s", key, got)
		}
	}
	if len(envelope) != 6 {
		t.Fatalf("unexpected envelope keys: %s", got)
	}

	var items []map[string]json.RawMessage
	if err := json.Unmarshal(envelope["items"], &items); err != nil {
		t.Fatal(err)
	}
	wantKeys := []string{"id", "source_code", "source_name", "title", "url",
		"published_at", "published_at_estimated", "available_at", "urgency",
		"tags", "entities", "independent_source_count", "duplicate_count"}
	for _, key := range wantKeys {
		if _, ok := items[0][key]; !ok {
			t.Fatalf("item is missing %q: %s", key, got)
		}
	}
	if len(items[0]) != len(wantKeys) {
		t.Fatalf("item has unexpected keys: %s", got)
	}
	// Timestamps go out as UTC with a Z suffix.
	if string(items[0]["available_at"]) != `"2026-07-27T10:00:00Z"` {
		t.Fatalf("available_at not RFC3339 UTC: %s", items[0]["available_at"])
	}
}

func TestPlainText(t *testing.T) {
	cases := map[string]string{
		"<b>Fed</b> holds rates":   "Fed holds rates",
		"Fed  holds\n rates":       "Fed holds rates",
		"Gold &amp; silver":        "Gold & silver",
		"gold < 2% of reserves":    "gold < 2% of reserves",
		"tag &lt;b&gt; in a title": "tag <b> in a title",
		"":                         "",
	}
	for in, want := range cases {
		if got := PlainText(in); got != want {
			t.Errorf("PlainText(%q) = %q, want %q", in, got, want)
		}
	}
}
