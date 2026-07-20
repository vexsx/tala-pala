package alerts

import (
	"encoding/json"
	"testing"
)

func TestValidateAlertRequest(t *testing.T) {
	cool := 30
	ok := alertRequest{AlertType: "price_above", Condition: json.RawMessage(`{"threshold":5000000}`), CooldownMinutes: &cool}
	if p := ValidateAlertRequest(ok); p != nil {
		t.Fatalf("valid alert rejected: %v", p)
	}
	// Every documented alert type must validate.
	for typ := range AlertTypes {
		if p := ValidateAlertRequest(alertRequest{AlertType: typ}); p != nil {
			t.Errorf("type %s rejected: %v", typ, p)
		}
	}
	if p := ValidateAlertRequest(alertRequest{AlertType: "nope"}); p == nil {
		t.Error("unknown type accepted")
	}
	if p := ValidateAlertRequest(alertRequest{AlertType: "price_above", Condition: json.RawMessage(`[1,2]`)}); p == nil {
		t.Error("non-object condition accepted")
	}
	bad := 0
	if p := ValidateAlertRequest(alertRequest{AlertType: "price_above", CooldownMinutes: &bad}); p == nil {
		t.Error("zero cooldown accepted")
	}
}
