package httpserver

import (
	"context"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/obs"
)

// ---------------------------------------------------------------------------
// Authenticated user context
// ---------------------------------------------------------------------------

// AuthUser is the identity extracted from a verified JWT.
type AuthUser struct {
	ID    string
	Email string
	Role  string
}

type ctxKey int

const userCtxKey ctxKey = iota

// UserFromContext returns the authenticated user, if any.
func UserFromContext(ctx context.Context) (AuthUser, bool) {
	u, ok := ctx.Value(userCtxKey).(AuthUser)
	return u, ok
}

// ContextWithUser is exported for tests and for the register handler that
// optionally authenticates.
func ContextWithUser(ctx context.Context, u AuthUser) context.Context {
	return context.WithValue(ctx, userCtxKey, u)
}

// TokenVerifier validates a raw JWT and returns the identity it carries.
type TokenVerifier func(token string) (AuthUser, error)

// BearerToken extracts the token from an Authorization: Bearer header.
func BearerToken(r *http.Request) string {
	h := r.Header.Get("Authorization")
	if len(h) > 7 && strings.EqualFold(h[:7], "Bearer ") {
		return strings.TrimSpace(h[7:])
	}
	return ""
}

// AuthMiddleware rejects requests without a valid bearer token.
func AuthMiddleware(verify TokenVerifier) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			tok := BearerToken(r)
			if tok == "" {
				Unauthorized(w, "missing bearer token")
				return
			}
			u, err := verify(tok)
			if err != nil {
				Unauthorized(w, "invalid or expired token")
				return
			}
			next.ServeHTTP(w, r.WithContext(ContextWithUser(r.Context(), u)))
		})
	}
}

// AdminOnly requires the authenticated user to have the admin role.
// Must be mounted after AuthMiddleware.
func AdminOnly(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u, ok := UserFromContext(r.Context())
		if !ok || u.Role != "admin" {
			Forbidden(w, "admin role required")
			return
		}
		next.ServeHTTP(w, r)
	})
}

// ---------------------------------------------------------------------------
// Request ID / logging / security headers / CORS
// ---------------------------------------------------------------------------

// RequestIDHeader echoes the chi request id into the X-Request-ID response
// header so every response carries it (per contract).
func RequestIDHeader(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if id := middleware.GetReqID(r.Context()); id != "" {
			w.Header().Set("X-Request-ID", id)
		}
		next.ServeHTTP(w, r)
	})
}

// RequestLogger emits one structured JSON log line per request.
func RequestLogger(logger *slog.Logger) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			ww := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
			next.ServeHTTP(ww, r)
			logger.LogAttrs(r.Context(), slog.LevelInfo, "http_request",
				slog.String("request_id", middleware.GetReqID(r.Context())),
				slog.String("method", r.Method),
				slog.String("path", r.URL.Path),
				slog.Int("status", ww.Status()),
				slog.Int("bytes", ww.BytesWritten()),
				slog.Float64("duration_ms", float64(time.Since(start).Microseconds())/1000),
				slog.String("remote_ip", clientIP(r)),
			)
		})
	}
}

// SecurityHeaders sets conservative security headers appropriate for an API.
func SecurityHeaders(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		h := w.Header()
		h.Set("X-Content-Type-Options", "nosniff")
		h.Set("X-Frame-Options", "DENY")
		h.Set("Referrer-Policy", "no-referrer")
		h.Set("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
		next.ServeHTTP(w, r)
	})
}

// CORS handles cross-origin requests for the configured origins.
func CORS(allowedOrigins []string) func(http.Handler) http.Handler {
	allowed := map[string]bool{}
	for _, o := range allowedOrigins {
		allowed[strings.ToLower(o)] = true
	}
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			origin := r.Header.Get("Origin")
			if origin != "" && allowed[strings.ToLower(origin)] {
				h := w.Header()
				h.Set("Access-Control-Allow-Origin", origin)
				h.Set("Vary", "Origin")
				h.Set("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
				h.Set("Access-Control-Allow-Headers", "Authorization, Content-Type")
				h.Set("Access-Control-Max-Age", "600")
			}
			if r.Method == http.MethodOptions && r.Header.Get("Access-Control-Request-Method") != "" {
				w.WriteHeader(http.StatusNoContent)
				return
			}
			next.ServeHTTP(w, r)
		})
	}
}

// ---------------------------------------------------------------------------
// Per-IP token bucket rate limiter
// ---------------------------------------------------------------------------

type bucket struct {
	tokens   float64
	lastFill time.Time
}

// RateLimiter is an in-memory per-IP token bucket. Tokens refill at
// ratePerMin/60 per second up to `burst`.
type RateLimiter struct {
	mu         sync.Mutex
	buckets    map[string]*bucket
	ratePerSec float64
	burst      float64
	now        func() time.Time // injectable for tests
	stop       chan struct{}
	stopOnce   sync.Once
}

// NewRateLimiter creates a limiter allowing ratePerMin requests per minute
// per IP, with a burst equal to ratePerMin. A cleanup goroutine evicts idle
// buckets every minute.
func NewRateLimiter(ratePerMin int) *RateLimiter {
	rl := &RateLimiter{
		buckets:    map[string]*bucket{},
		ratePerSec: float64(ratePerMin) / 60.0,
		burst:      float64(ratePerMin),
		now:        time.Now,
		stop:       make(chan struct{}),
	}
	go rl.cleanupLoop()
	return rl
}

// Allow reports whether the given key may proceed, consuming one token if so.
func (rl *RateLimiter) Allow(key string) bool {
	rl.mu.Lock()
	defer rl.mu.Unlock()
	now := rl.now()
	b, ok := rl.buckets[key]
	if !ok {
		b = &bucket{tokens: rl.burst, lastFill: now}
		rl.buckets[key] = b
	}
	elapsed := now.Sub(b.lastFill).Seconds()
	if elapsed > 0 {
		b.tokens = min(rl.burst, b.tokens+elapsed*rl.ratePerSec)
		b.lastFill = now
	}
	if b.tokens >= 1 {
		b.tokens--
		return true
	}
	return false
}

// SetClock overrides the time source (tests only).
func (rl *RateLimiter) SetClock(now func() time.Time) { rl.now = now }

// Stop terminates the cleanup goroutine.
func (rl *RateLimiter) Stop() { rl.stopOnce.Do(func() { close(rl.stop) }) }

func (rl *RateLimiter) cleanupLoop() {
	ticker := time.NewTicker(time.Minute)
	defer ticker.Stop()
	for {
		select {
		case <-rl.stop:
			return
		case <-ticker.C:
			cutoff := rl.now().Add(-10 * time.Minute)
			rl.mu.Lock()
			for k, b := range rl.buckets {
				if b.lastFill.Before(cutoff) {
					delete(rl.buckets, k)
				}
			}
			rl.mu.Unlock()
		}
	}
}

// Middleware applies the limiter keyed by client IP.
func (rl *RateLimiter) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !rl.Allow(clientIP(r)) {
			w.Header().Set("Retry-After", "60")
			Error(w, http.StatusTooManyRequests, "rate_limited", "too many requests", nil)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func clientIP(r *http.Request) string {
	// middleware.RealIP already rewrites RemoteAddr from X-Forwarded-For /
	// X-Real-IP when present.
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// ---------------------------------------------------------------------------
// Metrics middleware
// ---------------------------------------------------------------------------

// MetricsMiddleware records request duration and count labelled by chi route
// pattern, method and status code.
func MetricsMiddleware(m *obs.Metrics) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			start := time.Now()
			ww := middleware.NewWrapResponseWriter(w, r.ProtoMajor)
			next.ServeHTTP(ww, r)
			route := chi.RouteContext(r.Context()).RoutePattern()
			if route == "" {
				route = "unmatched"
			}
			code := strconv.Itoa(ww.Status())
			m.HTTPDuration.WithLabelValues(route, r.Method, code).Observe(time.Since(start).Seconds())
			m.HTTPTotal.WithLabelValues(route, r.Method, code).Inc()
		})
	}
}
