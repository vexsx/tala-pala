// Package obs provides the Prometheus metrics registry and health endpoints.
package obs

import (
	"context"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Metrics holds every application metric plus the registry they live in.
type Metrics struct {
	Registry *prometheus.Registry

	HTTPDuration   *prometheus.HistogramVec
	HTTPTotal      *prometheus.CounterVec
	DBUp           prometheus.Gauge
	RedisUp        prometheus.Gauge
	JobLastSuccess *prometheus.GaugeVec
	JobFailures    *prometheus.CounterVec
	JobDuration    *prometheus.HistogramVec

	// Freshness gauges maintained by the Go-side freshness job.
	LastPriceTimestamp      *prometheus.GaugeVec
	LastPredictionTimestamp *prometheus.GaugeVec
}

// NewMetrics builds and registers all metrics on a fresh registry.
func NewMetrics() *Metrics {
	reg := prometheus.NewRegistry()
	m := &Metrics{
		Registry: reg,
		HTTPDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "goldpred_http_request_duration_seconds",
			Help:    "HTTP request latency by route, method and status code.",
			Buckets: []float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10},
		}, []string{"route", "method", "code"}),
		HTTPTotal: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "goldpred_http_requests_total",
			Help: "Total HTTP requests by route, method and status code.",
		}, []string{"route", "method", "code"}),
		DBUp: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "goldpred_db_up",
			Help: "1 if the last database health check succeeded.",
		}),
		RedisUp: prometheus.NewGauge(prometheus.GaugeOpts{
			Name: "goldpred_redis_up",
			Help: "1 if the last Redis health check succeeded.",
		}),
		JobLastSuccess: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "goldpred_job_last_success_timestamp_seconds",
			Help: "Unix timestamp of the last successful run per scheduled job.",
		}, []string{"job"}),
		JobFailures: prometheus.NewCounterVec(prometheus.CounterOpts{
			Name: "goldpred_job_failure_total",
			Help: "Total failed runs per scheduled job.",
		}, []string{"job"}),
		JobDuration: prometheus.NewHistogramVec(prometheus.HistogramOpts{
			Name:    "goldpred_job_duration_seconds",
			Help:    "Duration of scheduled job runs.",
			Buckets: []float64{.1, .5, 1, 5, 15, 30, 60, 120, 300},
		}, []string{"job"}),
		LastPriceTimestamp: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "goldpred_api_last_price_timestamp_seconds",
			Help: "Unix timestamp of the latest stored price per symbol (Go freshness job).",
		}, []string{"symbol"}),
		LastPredictionTimestamp: prometheus.NewGaugeVec(prometheus.GaugeOpts{
			Name: "goldpred_api_last_prediction_timestamp_seconds",
			Help: "Unix timestamp of the latest prediction per horizon (Go freshness job).",
		}, []string{"horizon"}),
	}
	reg.MustRegister(
		m.HTTPDuration, m.HTTPTotal, m.DBUp, m.RedisUp,
		m.JobLastSuccess, m.JobFailures, m.JobDuration,
		m.LastPriceTimestamp, m.LastPredictionTimestamp,
		collectors.NewGoCollector(),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)
	return m
}

// Handler serves the /metrics endpoint.
func (m *Metrics) Handler() http.Handler {
	return promhttp.HandlerFor(m.Registry, promhttp.HandlerOpts{})
}

// Pinger checks a dependency's liveness.
type Pinger func(ctx context.Context) error

// HealthHandler always returns 200 {"status":"ok"} (process liveness).
func HealthHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte(`{"status":"ok"}`))
	}
}

// ReadinessHandler checks db + redis; returns 503 if either is down and
// updates the corresponding gauges.
func ReadinessHandler(m *Metrics, db, redis Pinger) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		ctx, cancel := context.WithTimeout(r.Context(), 3*time.Second)
		defer cancel()

		dbOK, redisOK := true, true
		if err := db(ctx); err != nil {
			dbOK = false
		}
		if err := redis(ctx); err != nil {
			redisOK = false
		}
		setGauge(m.DBUp, dbOK)
		setGauge(m.RedisUp, redisOK)

		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		if dbOK && redisOK {
			w.WriteHeader(http.StatusOK)
			_, _ = w.Write([]byte(`{"status":"ready","db":true,"redis":true}`))
			return
		}
		w.WriteHeader(http.StatusServiceUnavailable)
		body := `{"error":{"code":"not_ready","message":"dependency check failed","details":{"db":` +
			boolStr(dbOK) + `,"redis":` + boolStr(redisOK) + `}}}`
		_, _ = w.Write([]byte(body))
	}
}

func setGauge(g prometheus.Gauge, ok bool) {
	if ok {
		g.Set(1)
	} else {
		g.Set(0)
	}
}

func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}
