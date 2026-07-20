package httpserver

import (
	"encoding/json"
	"net/http"
)

// ErrorBody is the error envelope every non-2xx response uses:
// {"error":{"code":"...","message":"...","details":{...}}}
type ErrorBody struct {
	Error ErrorDetail `json:"error"`
}

// ErrorDetail carries a machine-readable code, a human message and details.
type ErrorDetail struct {
	Code    string         `json:"code"`
	Message string         `json:"message"`
	Details map[string]any `json:"details"`
}

// JSON writes v as a JSON response with the given status code.
func JSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

// Error writes the standard error envelope.
func Error(w http.ResponseWriter, status int, code, message string, details map[string]any) {
	if details == nil {
		details = map[string]any{}
	}
	JSON(w, status, ErrorBody{Error: ErrorDetail{Code: code, Message: message, Details: details}})
}

// Convenience wrappers for common statuses.

func BadRequest(w http.ResponseWriter, message string, details map[string]any) {
	Error(w, http.StatusBadRequest, "bad_request", message, details)
}

func Unauthorized(w http.ResponseWriter, message string) {
	Error(w, http.StatusUnauthorized, "unauthorized", message, nil)
}

func Forbidden(w http.ResponseWriter, message string) {
	Error(w, http.StatusForbidden, "forbidden", message, nil)
}

func NotFound(w http.ResponseWriter, message string) {
	Error(w, http.StatusNotFound, "not_found", message, nil)
}

func Conflict(w http.ResponseWriter, message string) {
	Error(w, http.StatusConflict, "conflict", message, nil)
}

func Internal(w http.ResponseWriter, message string) {
	Error(w, http.StatusInternalServerError, "internal_error", message, nil)
}

// DecodeJSON decodes a request body into dst with a size cap; returns false
// (after writing an error response) when the body is invalid.
func DecodeJSON(w http.ResponseWriter, r *http.Request, dst any) bool {
	r.Body = http.MaxBytesReader(w, r.Body, 1<<20) // 1 MiB
	dec := json.NewDecoder(r.Body)
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		BadRequest(w, "invalid JSON body", map[string]any{"reason": err.Error()})
		return false
	}
	return true
}
