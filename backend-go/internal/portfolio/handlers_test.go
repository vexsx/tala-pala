package portfolio

import "testing"

func validReq() txRequest {
	return txRequest{
		TxType: "buy", Grams: 1.5, Karat: 18, PricePerGram: 5_000_000,
		Currency: "IRT", Fees: 0, TxDate: "2026-07-20", Notes: "ok",
	}
}

func TestValidateTxRequest_Valid(t *testing.T) {
	if p := ValidateTxRequest(validReq()); p != nil {
		t.Fatalf("valid request rejected: %v", p)
	}
}

func TestValidateTxRequest_Invalid(t *testing.T) {
	mutations := map[string]func(*txRequest){
		"tx_type":        func(r *txRequest) { r.TxType = "transfer" },
		"grams":          func(r *txRequest) { r.Grams = 0 },
		"karat":          func(r *txRequest) { r.Karat = 20 },
		"price_per_gram": func(r *txRequest) { r.PricePerGram = -1 },
		"currency":       func(r *txRequest) { r.Currency = "USD" },
		"fees":           func(r *txRequest) { r.Fees = -5 },
		"tx_date":        func(r *txRequest) { r.TxDate = "20-07-2026" },
	}
	for field, mutate := range mutations {
		r := validReq()
		mutate(&r)
		p := ValidateTxRequest(r)
		if p == nil {
			t.Errorf("%s: invalid value accepted", field)
			continue
		}
		if _, ok := p[field]; !ok {
			t.Errorf("%s: problem not reported under its field name: %v", field, p)
		}
	}
}
