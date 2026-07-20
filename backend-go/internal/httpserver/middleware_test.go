package httpserver

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func okHandler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u, _ := UserFromContext(r.Context())
		JSON(w, http.StatusOK, map[string]string{"user": u.ID, "role": u.Role})
	})
}

func TestAuthMiddleware_MissingToken(t *testing.T) {
	h := AuthMiddleware(func(string) (AuthUser, error) {
		t.Fatal("verifier should not be called")
		return AuthUser{}, nil
	})(okHandler())
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/x", nil))
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
	var body ErrorBody
	if err := json.NewDecoder(rec.Body).Decode(&body); err != nil {
		t.Fatal(err)
	}
	if body.Error.Code != "unauthorized" {
		t.Fatalf("wrong error code: %s", body.Error.Code)
	}
}

func TestAuthMiddleware_BadToken(t *testing.T) {
	h := AuthMiddleware(func(tok string) (AuthUser, error) {
		return AuthUser{}, errors.New("bad")
	})(okHandler())
	req := httptest.NewRequest("GET", "/x", nil)
	req.Header.Set("Authorization", "Bearer nonsense")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401, got %d", rec.Code)
	}
}

func TestAuthMiddleware_ValidToken(t *testing.T) {
	h := AuthMiddleware(func(tok string) (AuthUser, error) {
		if tok != "good-token" {
			return AuthUser{}, errors.New("bad")
		}
		return AuthUser{ID: "u1", Email: "a@b.com", Role: "user"}, nil
	})(okHandler())
	req := httptest.NewRequest("GET", "/x", nil)
	req.Header.Set("Authorization", "Bearer good-token")
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", rec.Code)
	}
	var body map[string]string
	_ = json.NewDecoder(rec.Body).Decode(&body)
	if body["user"] != "u1" {
		t.Fatalf("user not propagated: %v", body)
	}
}

func TestAdminOnly(t *testing.T) {
	h := AdminOnly(okHandler())

	// No user in context.
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/x", nil))
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403, got %d", rec.Code)
	}

	// Non-admin user.
	req := httptest.NewRequest("GET", "/x", nil)
	req = req.WithContext(ContextWithUser(req.Context(), AuthUser{ID: "u", Role: "user"}))
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("expected 403 for user role, got %d", rec.Code)
	}

	// Admin passes.
	req = httptest.NewRequest("GET", "/x", nil)
	req = req.WithContext(ContextWithUser(req.Context(), AuthUser{ID: "a", Role: "admin"}))
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("expected 200 for admin, got %d", rec.Code)
	}
}

func TestRateLimiter_Bucket(t *testing.T) {
	rl := NewRateLimiter(60) // 60/min => 1/sec, burst 60
	defer rl.Stop()
	now := time.Unix(1_700_000_000, 0)
	rl.SetClock(func() time.Time { return now })

	// Burst allows 60 immediate requests, the 61st is rejected.
	for i := 0; i < 60; i++ {
		if !rl.Allow("1.2.3.4") {
			t.Fatalf("request %d should be allowed", i)
		}
	}
	if rl.Allow("1.2.3.4") {
		t.Fatal("61st request should be rejected")
	}

	// Another IP has its own bucket.
	if !rl.Allow("5.6.7.8") {
		t.Fatal("different IP should be allowed")
	}

	// After 2 seconds, ~2 tokens refill.
	now = now.Add(2 * time.Second)
	if !rl.Allow("1.2.3.4") {
		t.Fatal("token should have refilled")
	}
	if !rl.Allow("1.2.3.4") {
		t.Fatal("second refilled token expected")
	}
	if rl.Allow("1.2.3.4") {
		t.Fatal("third request should be rejected (only 2 refilled)")
	}
}

func TestRateLimiter_Middleware429(t *testing.T) {
	rl := NewRateLimiter(1)
	defer rl.Stop()
	now := time.Unix(1_700_000_000, 0)
	rl.SetClock(func() time.Time { return now })

	h := rl.Middleware(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	req := httptest.NewRequest("GET", "/x", nil)
	req.RemoteAddr = "9.9.9.9:1234"

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusOK {
		t.Fatalf("first request should pass, got %d", rec.Code)
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	if rec.Code != http.StatusTooManyRequests {
		t.Fatalf("second request should be limited, got %d", rec.Code)
	}
	var body ErrorBody
	_ = json.NewDecoder(rec.Body).Decode(&body)
	if body.Error.Code != "rate_limited" {
		t.Fatalf("wrong error code: %s", body.Error.Code)
	}
}

func TestSecurityHeaders(t *testing.T) {
	h := SecurityHeaders(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest("GET", "/x", nil))
	for header, want := range map[string]string{
		"X-Content-Type-Options": "nosniff",
		"X-Frame-Options":        "DENY",
		"Referrer-Policy":        "no-referrer",
	} {
		if got := rec.Header().Get(header); got != want {
			t.Errorf("%s = %q, want %q", header, got, want)
		}
	}
	if rec.Header().Get("Content-Security-Policy") == "" {
		t.Error("missing CSP header")
	}
}
