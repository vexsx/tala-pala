package portfolio

import (
	"encoding/csv"
	"fmt"
	"io"
	"strconv"
	"strings"
	"time"
)

// CSVColumns is the required header, in order.
var CSVColumns = []string{"tx_type", "grams", "karat", "price_per_gram", "currency", "fees", "tx_date", "notes"}

// MaxImportBytes caps CSV uploads at 1 MB.
const MaxImportBytes = 1 << 20

// ImportRow is one validated CSV row ready for insertion.
type ImportRow struct {
	Tx
	TxDate time.Time
	Notes  string
}

// RowError describes why a CSV row was rejected (1-based line numbers,
// line 1 is the header).
type RowError struct {
	Line   int    `json:"line"`
	Reason string `json:"reason"`
}

// IsFormulaInjection reports whether a cell would be interpreted as a formula
// by spreadsheet software (leading = + - @ or tab/CR).
func IsFormulaInjection(cell string) bool {
	if cell == "" {
		return false
	}
	switch cell[0] {
	case '=', '+', '-', '@', '\t', '\r':
		return true
	}
	return false
}

// SanitizeCSVCell prefixes dangerous cells with a single quote for export.
func SanitizeCSVCell(cell string) string {
	if IsFormulaInjection(cell) {
		return "'" + cell
	}
	return cell
}

// ParseImportCSV streams and validates a portfolio CSV. It returns the parsed
// rows and any per-row errors; callers should reject the import when errors
// are present. The reader should already be size-capped by the caller.
func ParseImportCSV(r io.Reader) ([]ImportRow, []RowError) {
	cr := csv.NewReader(r)
	cr.FieldsPerRecord = len(CSVColumns)
	cr.TrimLeadingSpace = true

	var rows []ImportRow
	var errs []RowError

	header, err := cr.Read()
	if err != nil {
		return nil, []RowError{{Line: 1, Reason: "cannot read header: " + err.Error()}}
	}
	for i, col := range CSVColumns {
		if i >= len(header) || strings.TrimSpace(strings.ToLower(header[i])) != col {
			return nil, []RowError{{Line: 1, Reason: fmt.Sprintf(
				"header must be exactly: %s", strings.Join(CSVColumns, ","))}}
		}
	}

	line := 1
	for {
		line++
		rec, err := cr.Read()
		if err == io.EOF {
			break
		}
		if err != nil {
			errs = append(errs, RowError{Line: line, Reason: err.Error()})
			continue
		}
		row, reason := ValidateImportRecord(rec)
		if reason != "" {
			errs = append(errs, RowError{Line: line, Reason: reason})
			continue
		}
		rows = append(rows, row)
	}
	return rows, errs
}

// ValidateImportRecord validates a single CSV record (pure, unit tested).
// Returns a non-empty reason when the record is invalid.
func ValidateImportRecord(rec []string) (ImportRow, string) {
	var row ImportRow
	if len(rec) != len(CSVColumns) {
		return row, fmt.Sprintf("expected %d columns, got %d", len(CSVColumns), len(rec))
	}
	get := func(i int) string { return strings.TrimSpace(rec[i]) }

	txType := strings.ToLower(get(0))
	if txType != "buy" && txType != "sell" {
		return row, "tx_type must be buy or sell"
	}
	grams, err := strconv.ParseFloat(get(1), 64)
	if err != nil || grams <= 0 {
		return row, "grams must be a positive number"
	}
	karat, err := strconv.Atoi(get(2))
	if err != nil || !validKarat(karat) {
		return row, "karat must be one of 18, 21, 22, 24"
	}
	price, err := strconv.ParseFloat(get(3), 64)
	if err != nil || price <= 0 {
		return row, "price_per_gram must be a positive number"
	}
	currency := strings.ToUpper(get(4))
	if currency != "IRT" && currency != "IRR" {
		return row, "currency must be IRT or IRR"
	}
	feesStr := get(5)
	fees := 0.0
	if feesStr != "" {
		fees, err = strconv.ParseFloat(feesStr, 64)
		if err != nil || fees < 0 {
			return row, "fees must be a non-negative number"
		}
	}
	txDate, err := time.Parse("2006-01-02", get(6))
	if err != nil {
		return row, "tx_date must be YYYY-MM-DD"
	}
	notes := get(7)
	if IsFormulaInjection(notes) {
		return row, "notes must not start with =, +, -, @ (formula injection)"
	}
	if len(notes) > 1000 {
		return row, "notes too long (max 1000 chars)"
	}

	row = ImportRow{
		Tx: Tx{
			Type: txType, Grams: grams, Karat: karat,
			PricePerGram: price, Fees: fees, Currency: currency,
		},
		TxDate: txDate,
		Notes:  notes,
	}
	return row, ""
}

func validKarat(k int) bool {
	return k == 18 || k == 21 || k == 22 || k == 24
}

// ExportRecord renders one transaction as a sanitized CSV record.
func ExportRecord(txType string, grams float64, karat int, pricePerGram float64,
	currency string, fees float64, txDate, notes string) []string {
	return []string{
		SanitizeCSVCell(txType),
		strconv.FormatFloat(grams, 'f', -1, 64),
		strconv.Itoa(karat),
		strconv.FormatFloat(pricePerGram, 'f', -1, 64),
		SanitizeCSVCell(currency),
		strconv.FormatFloat(fees, 'f', -1, 64),
		SanitizeCSVCell(txDate),
		SanitizeCSVCell(notes),
	}
}
