package liquidity

// The page's fixtures, written by the real builders from the research data.
//
// Skipped unless LIQUIDITY_FIXTURE_DIR names a directory to write into:
//
//	LIQUIDITY_FIXTURE_DIR=/abs/path/frontend/src/test/fixtures \
//	    go test ./internal/liquidity -run TestWriteFrontendFixtures
//
// It writes liquidity-aggregates-monthly.json, liquidity-aggregates-annual.json,
// liquidity-dollar-12.json and liquidity-dollar-24.json: exactly the shape and
// the arithmetic the two routes serve, built at the research's cutoff
// (researchNow). The money figures are CBI's; the dollar is TGJU's cash dollar
// throughout, where production reads USD_IRT (the USDT/toman market from
// 2022-04), and published_at is null because the research recorded none --
// say so wherever the fixtures are described.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestWriteFrontendFixtures(t *testing.T) {
	dir := os.Getenv("LIQUIDITY_FIXTURE_DIR")
	if dir == "" {
		t.Skip("LIQUIDITY_FIXTURE_DIR is not set")
	}
	b := buildAll("fixtures", researchMoney(t), researchUSD(t), nil, researchNow)
	for name, v := range map[string]any{
		"liquidity-aggregates-monthly.json": b.monthly,
		"liquidity-aggregates-annual.json":  b.annual,
		"liquidity-dollar-12.json":          b.dollar[12],
		"liquidity-dollar-24.json":          b.dollar[24],
	} {
		raw, err := json.MarshalIndent(v, "", "  ")
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, name), append(raw, '\n'), 0o644); err != nil {
			t.Fatal(err)
		}
	}
}
