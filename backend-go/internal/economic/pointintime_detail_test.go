package economic

import (
	"strings"
	"testing"
	"time"
)

// The decoration must follow the SELECTED vintage: a period revised from
// vintage 1 to 2 carries vintage 2's citation and publication date, never
// vintage 1's, and a detail row for a period PointInTime did not select adds
// nothing.
func TestAttachDetails_FollowsTheSelectedVintage(t *testing.T) {
	selected := []Observation{
		{RefPeriodStart: day(2026, time.February, 20), RefPeriodLabel: "1404-12", Value: 155812.2, Vintage: 2},
	}
	details := map[detailKey]detailRow{
		{Start: "2026-02-20", Vintage: 1}: {
			PublishedAt: timePtr(instant(2026, time.May, 1, 0)),
			Source:      &SourceDocument{ID: 1, URL: "https://www.cbi.ir/page/old.aspx"},
			Columns:     map[string]any{"method": "old"},
		},
		{Start: "2026-02-20", Vintage: 2}: {
			PublishedAt: timePtr(time.Date(2026, time.June, 21, 3, 30, 0, 0, time.FixedZone("x", 3*3600+1800))),
			Source:      &SourceDocument{ID: 2, URL: "https://www.cbi.ir/page/35570.aspx"},
			Columns:     map[string]any{"method": "revised"},
		},
		{Start: "2026-01-21", Vintage: 1}: {Source: &SourceDocument{ID: 3}},
	}
	got := attachDetails(selected, details)
	if len(got) != 1 {
		t.Fatalf("got %d rows, want exactly the 1 selected", len(got))
	}
	if got[0].Source == nil || got[0].Source.ID != 2 || got[0].Columns["method"] != "revised" {
		t.Fatalf("decoration came from the wrong vintage: %+v", got[0])
	}
	if got[0].PublishedAt == nil || got[0].PublishedAt.Location() != time.UTC ||
		!got[0].PublishedAt.Equal(time.Date(2026, time.June, 21, 0, 0, 0, 0, time.UTC)) {
		t.Errorf("published_at = %v, want 2026-06-21T00:00Z in UTC", got[0].PublishedAt)
	}
	if got[0].Value != 155812.2 || got[0].Vintage != 2 {
		t.Errorf("the observation itself changed: %+v", got[0].Observation)
	}
}

func TestAttachDetails_MissingDetailKeepsTheValue(t *testing.T) {
	selected := []Observation{{RefPeriodStart: day(2026, time.February, 20), Value: 1, Vintage: 1}}
	got := attachDetails(selected, map[detailKey]detailRow{})
	if len(got) != 1 || got[0].Value != 1 || got[0].Source != nil || got[0].PublishedAt != nil {
		t.Fatalf("got %+v", got)
	}
	if got[0].Columns == nil {
		t.Error("Columns must never be nil")
	}
}

// The lookup is bounded by the same availability cutoff and excludes
// projections, so nothing it returns could have been unknowable at as_of.
func TestObservationDetailSelect_IsBoundedLikeThePointInTimeRead(t *testing.T) {
	for _, want := range []string{
		"o.available_at <= $2::timestamptz",
		"NOT o.is_projection",
		"LEFT JOIN source_documents d ON d.id = o.source_document_id",
		"to_jsonb(o)",
	} {
		if !strings.Contains(observationDetailSelect, want) {
			t.Errorf("observationDetailSelect lacks %q", want)
		}
	}
	if strings.Contains(observationDetailSelect, "published_at <=") {
		t.Error("published_at must never gate a point-in-time read")
	}
}
