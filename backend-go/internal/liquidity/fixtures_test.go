package liquidity

// The page's fixtures are CAPTURES of these two routes, taken from a local
// rehearsal of real CBI and TGJU data (frontend/src/test/fixtures/README.md,
// "Money & liquidity"). This test holds them to the wire shape as the code
// stands: every key a fixture carries is a field the builders emit, and every
// field the builders emit is in the fixture. A field added, renamed or dropped
// here fails until the fixtures are recaptured, instead of the page being
// tested against a payload the server no longer sends.

import (
	"bytes"
	"encoding/json"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"testing"
)

var frontendFixtures = filepath.Join("..", "..", "..", "frontend", "src", "test", "fixtures")

func TestFrontendFixturesHaveTheWireShape(t *testing.T) {
	for name, typ := range map[string]any{
		"liquidity-aggregates-monthly.json":      aggregatesResponse{},
		"liquidity-aggregates-annual.json":       aggregatesResponse{},
		"liquidity-aggregates-not-ingested.json": aggregatesResponse{},
		"liquidity-dollar-12.json":               dollarResponse{},
		"liquidity-dollar-24.json":               dollarResponse{},
		"liquidity-dollar-not-ingested.json":     dollarResponse{},
	} {
		raw, err := os.ReadFile(filepath.Join(frontendFixtures, name))
		if errors.Is(err, fs.ErrNotExist) {
			t.Skipf("%s: no frontend checkout beside this module", name)
		}
		if err != nil {
			t.Fatal(err)
		}
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.DisallowUnknownFields()
		v := reflect.New(reflect.TypeOf(typ))
		if err := dec.Decode(v.Interface()); err != nil {
			t.Errorf("%s: carries something the builders do not emit: %v", name, err)
			continue
		}
		back, err := json.Marshal(v.Interface())
		if err != nil {
			t.Fatal(err)
		}
		have, want := keyPaths(t, raw), keyPaths(t, back)
		for _, p := range diff(want, have) {
			t.Errorf("%s: the builders emit %s and the capture lacks it: recapture the fixtures", name, p)
		}
		for _, p := range diff(have, want) {
			t.Errorf("%s: the capture has %s and the builders do not emit it", name, p)
		}
	}
}

// keyPaths is every object key in a JSON document, as a path ("series[].points[].value").
func keyPaths(t *testing.T, raw []byte) map[string]bool {
	t.Helper()
	var doc any
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	out := map[string]bool{}
	var walk func(prefix string, v any)
	walk = func(prefix string, v any) {
		switch x := v.(type) {
		case map[string]any:
			for k, inner := range x {
				p := prefix + "." + k
				out[p] = true
				walk(p, inner)
			}
		case []any:
			for _, inner := range x {
				walk(prefix+"[]", inner)
			}
		}
	}
	walk("", doc)
	return out
}

func diff(a, b map[string]bool) []string {
	out := []string{}
	for k := range a {
		if !b[k] {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}
