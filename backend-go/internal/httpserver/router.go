package httpserver

import (
	"log/slog"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/config"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/obs"
)

// Handler-set interfaces implemented by the feature packages. Keeping the
// dependencies as interfaces avoids import cycles (feature packages import
// httpserver for respond helpers and context accessors).

type AuthHandlers interface {
	Register(http.ResponseWriter, *http.Request)
	Login(http.ResponseWriter, *http.Request)
	Me(http.ResponseWriter, *http.Request)
}

type PriceHandlers interface {
	Current(http.ResponseWriter, *http.Request)
	History(http.ResponseWriter, *http.Request)
	MarketSummary(http.ResponseWriter, *http.Request)
	Premium(http.ResponseWriter, *http.Request)
	Indicators(http.ResponseWriter, *http.Request)
	ProviderGap(http.ResponseWriter, *http.Request)
	Candles(http.ResponseWriter, *http.Request)
	Funds(http.ResponseWriter, *http.Request)
}

type PredictionHandlers interface {
	Latest(http.ResponseWriter, *http.Request)
	History(http.ResponseWriter, *http.Request)
	Custom(http.ResponseWriter, *http.Request)
}

type SignalHandlers interface {
	Current(http.ResponseWriter, *http.Request)
	History(http.ResponseWriter, *http.Request)
}

type ModelHandlers interface {
	List(http.ResponseWriter, *http.Request)
	Performance(http.ResponseWriter, *http.Request)
}

type PortfolioHandlers interface {
	Get(http.ResponseWriter, *http.Request)
	CreateTransaction(http.ResponseWriter, *http.Request)
	UpdateTransaction(http.ResponseWriter, *http.Request)
	DeleteTransaction(http.ResponseWriter, *http.Request)
	Import(http.ResponseWriter, *http.Request)
	Export(http.ResponseWriter, *http.Request)
}

type AlertHandlers interface {
	List(http.ResponseWriter, *http.Request)
	Create(http.ResponseWriter, *http.Request)
	Update(http.ResponseWriter, *http.Request)
	Delete(http.ResponseWriter, *http.Request)
	Events(http.ResponseWriter, *http.Request)
	AckEvent(http.ResponseWriter, *http.Request)
}

type AdminHandlers interface {
	TriggerJob(http.ResponseWriter, *http.Request)
	AuditList(http.ResponseWriter, *http.Request)
	ListUsers(http.ResponseWriter, *http.Request)
	CreateUser(http.ResponseWriter, *http.Request)
	UpdateUser(http.ResponseWriter, *http.Request)
	DeleteUser(http.ResponseWriter, *http.Request)
}

type IssueHandlers interface {
	List(http.ResponseWriter, *http.Request)
	Create(http.ResponseWriter, *http.Request)
	Report(http.ResponseWriter, *http.Request)
}

// Deps bundles everything the router needs.
type Deps struct {
	Logger  *slog.Logger
	Metrics *obs.Metrics
	Verify  TokenVerifier

	Health    http.HandlerFunc
	Readiness http.HandlerFunc

	Auth        AuthHandlers
	Prices      PriceHandlers
	Predictions PredictionHandlers
	Signals     SignalHandlers
	Models      ModelHandlers
	Portfolio   PortfolioHandlers
	Alerts      AlertHandlers
	Admin       AdminHandlers
	Issues      IssueHandlers

	// Limiters are created by the caller so it can Stop() them on shutdown.
	GlobalLimiter *RateLimiter
	LoginLimiter  *RateLimiter
}

// NewRouter assembles the chi router with all middlewares and mounts.
func NewRouter(cfg *config.Config, d Deps) chi.Router {
	r := chi.NewRouter()

	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(RequestIDHeader)
	r.Use(RequestLogger(d.Logger))
	r.Use(middleware.Recoverer)
	r.Use(SecurityHeaders)
	r.Use(CORS(cfg.CORSAllowedOrigins))
	r.Use(MetricsMiddleware(d.Metrics))

	// Public, unauthenticated endpoints (health checks are not rate limited
	// so orchestrators never get throttled).
	r.Get("/metrics", d.Metrics.Handler().ServeHTTP)
	r.Get("/api/v1/health", d.Health)
	r.Get("/api/v1/readiness", d.Readiness)

	// API docs (public, self-contained).
	r.Get("/api/v1/docs", docsPageHandler)
	r.Get("/api/v1/docs/openapi.yaml", openAPISpecHandler)

	// Everything below is rate limited per IP.
	r.Group(func(r chi.Router) {
		r.Use(d.GlobalLimiter.Middleware)

		// Auth endpoints. Login gets the stricter limiter (10/min per IP).
		r.With(d.LoginLimiter.Middleware).Post("/api/v1/auth/login", d.Auth.Login)
		r.Post("/api/v1/auth/register", d.Auth.Register)

		// Authenticated endpoints.
		r.Group(func(r chi.Router) {
			r.Use(AuthMiddleware(d.Verify))

			r.Get("/api/v1/auth/me", d.Auth.Me)

			r.Get("/api/v1/prices/current", d.Prices.Current)
			r.Get("/api/v1/prices/history", d.Prices.History)
			r.Get("/api/v1/market/summary", d.Prices.MarketSummary)
			r.Get("/api/v1/market/premium", d.Prices.Premium)
			r.Get("/api/v1/market/indicators", d.Prices.Indicators)
			r.Get("/api/v1/market/provider-gap", d.Prices.ProviderGap)
			r.Get("/api/v1/market/candles", d.Prices.Candles)
			r.Get("/api/v1/market/funds", d.Prices.Funds)

			r.Get("/api/v1/predictions", d.Predictions.Latest)
			// static route wins over the {horizon} pattern in chi
			r.Get("/api/v1/predictions/custom", d.Predictions.Custom)
			r.Get("/api/v1/predictions/{horizon}", d.Predictions.History)

			r.Get("/api/v1/signals/current", d.Signals.Current)
			r.Get("/api/v1/signals/history", d.Signals.History)

			r.Get("/api/v1/models", d.Models.List)
			r.Get("/api/v1/models/performance", d.Models.Performance)

			r.Get("/api/v1/portfolio", d.Portfolio.Get)
			r.Post("/api/v1/portfolio/transactions", d.Portfolio.CreateTransaction)
			r.Put("/api/v1/portfolio/transactions/{id}", d.Portfolio.UpdateTransaction)
			r.Delete("/api/v1/portfolio/transactions/{id}", d.Portfolio.DeleteTransaction)
			r.Post("/api/v1/portfolio/import", d.Portfolio.Import)
			r.Get("/api/v1/portfolio/export", d.Portfolio.Export)

			r.Get("/api/v1/alerts", d.Alerts.List)
			r.Post("/api/v1/alerts", d.Alerts.Create)
			r.Put("/api/v1/alerts/{id}", d.Alerts.Update)
			r.Delete("/api/v1/alerts/{id}", d.Alerts.Delete)
			r.Get("/api/v1/alerts/events", d.Alerts.Events)
			r.Post("/api/v1/alerts/events/{id}/ack", d.Alerts.AckEvent)

			// Any authenticated session may REPORT a client-side error…
			r.Post("/api/v1/issues", d.Issues.Create)

			// Admin only.
			r.Group(func(r chi.Router) {
				r.Use(AdminOnly)
				// …but viewing the issue log is system scope: admin only.
				r.Get("/api/v1/issues", d.Issues.List)
				r.Get("/api/v1/issues/report", d.Issues.Report)
				r.Post("/api/v1/admin/jobs/{job}", d.Admin.TriggerJob)
				r.Get("/api/v1/admin/audit", d.Admin.AuditList)
				r.Get("/api/v1/admin/users", d.Admin.ListUsers)
				r.Post("/api/v1/admin/users", d.Admin.CreateUser)
				r.Put("/api/v1/admin/users/{id}", d.Admin.UpdateUser)
				r.Delete("/api/v1/admin/users/{id}", d.Admin.DeleteUser)
			})
		})
	})

	r.NotFound(func(w http.ResponseWriter, _ *http.Request) {
		NotFound(w, "route not found")
	})
	r.MethodNotAllowed(func(w http.ResponseWriter, _ *http.Request) {
		Error(w, http.StatusMethodNotAllowed, "method_not_allowed", "method not allowed", nil)
	})

	return r
}
