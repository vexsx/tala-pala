package portfolio

import (
	"strings"
	"testing"
)

const validCSV = `tx_type,grams,karat,price_per_gram,currency,fees,tx_date,notes
buy,10,18,5000000,IRT,50000,2026-01-15,first buy
sell,2.5,24,6000000,IRR,0,2026-02-20,partial exit
buy,1,21,5500000,IRT,,2026-03-01,
`

func TestParseImportCSV_Valid(t *testing.T) {
	rows, errs := ParseImportCSV(strings.NewReader(validCSV))
	if len(errs) != 0 {
		t.Fatalf("unexpected errors: %v", errs)
	}
	if len(rows) != 3 {
		t.Fatalf("expected 3 rows, got %d", len(rows))
	}
	if rows[0].Type != "buy" || rows[0].Grams != 10 || rows[0].Karat != 18 {
		t.Fatalf("row0 parsed wrong: %+v", rows[0])
	}
	if rows[1].Currency != "IRR" || rows[1].Karat != 24 {
		t.Fatalf("row1 parsed wrong: %+v", rows[1])
	}
	if rows[2].Fees != 0 {
		t.Fatalf("empty fees should default to 0, got %v", rows[2].Fees)
	}
	if rows[0].TxDate.Format("2006-01-02") != "2026-01-15" {
		t.Fatalf("bad tx_date: %v", rows[0].TxDate)
	}
}

func TestParseImportCSV_BadHeader(t *testing.T) {
	_, errs := ParseImportCSV(strings.NewReader("a,b,c,d,e,f,g,h\nbuy,1,18,1,IRT,0,2026-01-01,x\n"))
	if len(errs) == 0 {
		t.Fatal("expected header error")
	}
}

func TestParseImportCSV_FormulaInjectionRejected(t *testing.T) {
	csv := "tx_type,grams,karat,price_per_gram,currency,fees,tx_date,notes\n" +
		"buy,1,18,1000,IRT,0,2026-01-01,=1+2\n"
	_, errs := ParseImportCSV(strings.NewReader(csv))
	if len(errs) != 1 {
		t.Fatalf("expected 1 error, got %v", errs)
	}
	if !strings.Contains(errs[0].Reason, "formula injection") {
		t.Fatalf("wrong reason: %s", errs[0].Reason)
	}
}

func TestValidateImportRecord_Errors(t *testing.T) {
	cases := []struct {
		name string
		rec  []string
	}{
		{"bad tx_type", []string{"hold", "1", "18", "1000", "IRT", "0", "2026-01-01", ""}},
		{"zero grams", []string{"buy", "0", "18", "1000", "IRT", "0", "2026-01-01", ""}},
		{"negative grams", []string{"buy", "-5", "18", "1000", "IRT", "0", "2026-01-01", ""}},
		{"bad karat", []string{"buy", "1", "19", "1000", "IRT", "0", "2026-01-01", ""}},
		{"zero price", []string{"buy", "1", "18", "0", "IRT", "0", "2026-01-01", ""}},
		{"bad currency", []string{"buy", "1", "18", "1000", "USD", "0", "2026-01-01", ""}},
		{"negative fees", []string{"buy", "1", "18", "1000", "IRT", "-1", "2026-01-01", ""}},
		{"bad date", []string{"buy", "1", "18", "1000", "IRT", "0", "01/15/2026", ""}},
		{"formula notes @", []string{"buy", "1", "18", "1000", "IRT", "0", "2026-01-01", "@cmd"}},
		{"formula notes +", []string{"buy", "1", "18", "1000", "IRT", "0", "2026-01-01", "+1+1"}},
	}
	for _, c := range cases {
		if _, reason := ValidateImportRecord(c.rec); reason == "" {
			t.Errorf("%s: expected rejection", c.name)
		}
	}
}

func TestSanitizeCSVCell(t *testing.T) {
	cases := map[string]string{
		"=SUM(A1)":  "'=SUM(A1)",
		"+1":        "'+1",
		"-1":        "'-1",
		"@cmd":      "'@cmd",
		"normal":    "normal",
		"":          "",
		"a=b":       "a=b",
		"\tleading": "'\tleading",
	}
	for in, want := range cases {
		if got := SanitizeCSVCell(in); got != want {
			t.Errorf("SanitizeCSVCell(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestExportRecord_Sanitized(t *testing.T) {
	rec := ExportRecord("buy", 1.5, 18, 5000000, "IRT", 0, "2026-01-15", "=EVIL()")
	if rec[7] != "'=EVIL()" {
		t.Fatalf("notes not sanitized: %q", rec[7])
	}
	if rec[0] != "buy" || rec[1] != "1.5" || rec[2] != "18" {
		t.Fatalf("unexpected record: %v", rec)
	}
}
