package httpserver

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestErrorEnvelopeShape(t *testing.T) {
	rec := httptest.NewRecorder()
	Error(rec, http.StatusBadRequest, "bad_request", "something broke", map[string]any{"field": "x"})

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d", rec.Code)
	}
	if ct := rec.Header().Get("Content-Type"); !strings.HasPrefix(ct, "application/json") {
		t.Fatalf("content-type = %q", ct)
	}

	var raw map[string]any
	if err := json.NewDecoder(rec.Body).Decode(&raw); err != nil {
		t.Fatal(err)
	}
	errObj, ok := raw["error"].(map[string]any)
	if !ok {
		t.Fatalf(`missing "error" object: %v`, raw)
	}
	if errObj["code"] != "bad_request" || errObj["message"] != "something broke" {
		t.Fatalf("wrong envelope: %v", errObj)
	}
	details, ok := errObj["details"].(map[string]any)
	if !ok || details["field"] != "x" {
		t.Fatalf("wrong details: %v", errObj["details"])
	}
}

func TestErrorNilDetailsBecomesEmptyObject(t *testing.T) {
	rec := httptest.NewRecorder()
	Error(rec, http.StatusInternalServerError, "internal_error", "boom", nil)
	var raw map[string]map[string]any
	_ = json.NewDecoder(rec.Body).Decode(&raw)
	if _, ok := raw["error"]["details"].(map[string]any); !ok {
		t.Fatalf("details should be an empty object, got %v", raw["error"]["details"])
	}
}

func TestJSONWriter(t *testing.T) {
	rec := httptest.NewRecorder()
	JSON(rec, http.StatusCreated, map[string]int{"n": 42})
	if rec.Code != http.StatusCreated {
		t.Fatalf("status = %d", rec.Code)
	}
	var body map[string]int
	_ = json.NewDecoder(rec.Body).Decode(&body)
	if body["n"] != 42 {
		t.Fatalf("body = %v", body)
	}
}

func TestDecodeJSON_Invalid(t *testing.T) {
	rec := httptest.NewRecorder()
	req := httptest.NewRequest("POST", "/x", strings.NewReader("{not json"))
	var dst struct{}
	if DecodeJSON(rec, req, &dst) {
		t.Fatal("invalid JSON accepted")
	}
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d", rec.Code)
	}
}
