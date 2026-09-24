package equities

import (
	"fmt"
	"os"
	"regexp"
	"strconv"
	"testing"
)

// The API's staleness bound and the Prometheus rule's bound are two statements
// about the same moment, written in different units, in different languages, in
// different files. This test is what stops them drifting apart.
//
// They HAD drifted, which is why it exists. equityStaleAfterDays is 10 and
// buildDataAge marks data stale on `days > 10`, so the first stale age is
// eleven days. The rule was written `time() - gauge > 864000` — ten days of
// seconds — and because the gauge is a DATE floored to midnight UTC while
// time() is a wall clock, that went true one second after midnight on day TEN.
// For roughly twenty-three hours an operator could be paged, open
// /stocks/screen, and read `"stale": false` with no banner at all. Three
// separate comments in the repo promised this could not happen.
//
// Both files assert the promise in prose; only this asserts it in arithmetic.
const alertsPath = "../../../observability/alerts.yml"

// equityBarsStaleExpr pulls the comparison out of the EquityBarsStale rule.
//
// Read as text rather than parsed as YAML on purpose: the alternative is a
// direct gopkg.in/yaml.v3 dependency for one assertion, and the thing being
// checked is a numeric literal on a known line, not a document structure. The
// regex is anchored to the alert name so it cannot silently start matching a
// different rule if the file is reordered.
var equityBarsStaleExpr = regexp.MustCompile(
	`(?s)- alert: EquityBarsStale.*?expr: time\(\) - ` +
		`talapala_api_last_equity_bar_roster_timestamp_seconds\s*(>=?)\s*(\d+)`)

func TestTheAlertBoundAndTheApiBoundAreTheSameMoment(t *testing.T) {
	blob, err := os.ReadFile(alertsPath)
	if err != nil {
		// Fails rather than skips: a skip here would restore exactly the
		// silent drift this test exists to prevent, and CI checks out the
		// whole repository.
		t.Fatalf("cannot read %s, so the two bounds cannot be compared: %v", alertsPath, err)
	}

	m := equityBarsStaleExpr.FindSubmatch(blob)
	if m == nil {
		t.Fatalf("no EquityBarsStale expression of the expected shape in %s — if the rule "+
			"was renamed or rewritten, update this test rather than deleting it", alertsPath)
	}
	op, lit := string(m[1]), string(m[2])
	seconds, err := strconv.Atoi(lit)
	if err != nil {
		t.Fatalf("threshold %q is not a number: %v", lit, err)
	}

	// The API is stale on `days > equityStaleAfterDays`, so the first stale age
	// is equityStaleAfterDays+1 days. Age-in-seconds is exactly days*86400
	// because the gauge is midnight-floored, so the rule must fire from that
	// age INCLUSIVE — hence >= and not >.
	wantSeconds := (equityStaleAfterDays + 1) * 86400
	if op != ">=" || seconds != wantSeconds {
		t.Errorf("the alert and the API disagree about when equity data goes stale.\n"+
			"  alert fires when age %s %d seconds (%.2f days)\n"+
			"  API is stale when days > %d, i.e. from %d seconds (%d days)\n"+
			"  want the rule to read: >= %d",
			op, seconds, float64(seconds)/86400, equityStaleAfterDays,
			wantSeconds, equityStaleAfterDays+1, wantSeconds)
	}

	// Now walk the boundary and require the two to agree at every whole day
	// across it. This is the assertion that would have caught the original bug:
	// the literals could both be "10" and still disagree, because one counts
	// days and the other counts seconds from a floored date.
	for days := equityStaleAfterDays - 2; days <= equityStaleAfterDays+2; days++ {
		now := day(2026, 9, 24)
		newest := now.AddDate(0, 0, -days)
		apiStale := buildDataAge(&newest, now).Stale
		alertFires := days*86400 >= seconds // op is asserted >= above
		if apiStale != alertFires {
			t.Errorf("at %d days old: API stale=%v but alert fires=%v — an operator paged "+
				"by one would be told by the other that nothing is wrong",
				days, apiStale, alertFires)
		}
	}
}

// Documents the boundary in the output, so a reader of a passing run can see
// which day the two agree on rather than taking it on trust.
func TestTheStaleBoundaryIsElevenDays(t *testing.T) {
	now := day(2026, 9, 24)
	var firstStale = -1
	for days := 0; days <= 20; days++ {
		newest := now.AddDate(0, 0, -days)
		if buildDataAge(&newest, now).Stale {
			firstStale = days
			break
		}
	}
	if firstStale != equityStaleAfterDays+1 {
		t.Errorf("first stale age = %d days, want %d", firstStale, equityStaleAfterDays+1)
	}
	t.Log(fmt.Sprintf("fresh through %d days; stale from %d days",
		equityStaleAfterDays, firstStale))
}
