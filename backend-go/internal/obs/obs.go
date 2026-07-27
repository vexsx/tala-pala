// Package obs provides the Prometheus metrics registry and health endpoints.
//
// Every application metric is exported under TWO names. The reason is a real
// collision: goldpred_job_last_success_timestamp_seconds{job=...} was exported
// by this service *and* by the Python prediction service, with overlapping job
// label values (both sides own a job called "collect"/"predict" in operators'
// heads). A Prometheus scraping both targets therefore held two unrelated
// series that differ only in the target labels a rule usually aggregates away —
// max by (job) (...) let one process's healthy timestamp mask the other's dead
// job.
//
// Metric names are therefore namespaced per service: talapala_api_* here,
// talapala_prediction_* in prediction-python. The old goldpred_* names keep
// being written alongside them (see the Dual* wrappers) so existing dashboards,
// alerts and docs/CONTRACTS.md consumers do not break on the day this lands.
//
// The goldpred_* names are DEPRECATED and will be removed in a later release;
// new dashboards and alert rules must use talapala_api_*.
package obs

import (
	"os"
	"context"
	"net/http"
	"time"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// deprecatedHelp marks the old export so anyone reading /metrics by hand sees
// which name to migrate to.
func deprecatedHelp(replacement, help string) string {
	return help + " DEPRECATED name — use " + replacement +
		"; this series is removed in a later release."
}

// The Dual* wrappers fan one write out to the current, service-namespaced
// metric and to its deprecated goldpred_* twin. Wrapping the pair (rather than
// writing twice at every call site) is what keeps the two exports in lockstep:
// a call site cannot update one name and forget the other, and dropping the
// deprecated export later is a one-line change in this file. Only the
// operations the call sites actually use are exposed.

// DualCounterVec is a CounterVec exported under both names.
type DualCounterVec struct{ current, deprecated *prometheus.CounterVec }

// WithLabelValues returns the counter pair for one label set.
func (d *DualCounterVec) WithLabelValues(lvs ...string) DualCounter {
	return DualCounter{d.current.WithLabelValues(lvs...), d.deprecated.WithLabelValues(lvs...)}
}

func (d *DualCounterVec) collectors() []prometheus.Collector {
	return []prometheus.Collector{d.current, d.deprecated}
}

// DualCounter is one label set of a DualCounterVec.
type DualCounter struct{ current, deprecated prometheus.Counter }

// Inc increments both exports.
func (c DualCounter) Inc() {
	c.current.Inc()
	c.deprecated.Inc()
}

// DualGaugeVec is a GaugeVec exported under both names.
type DualGaugeVec struct{ current, deprecated *prometheus.GaugeVec }

// WithLabelValues returns the gauge pair for one label set.
func (d *DualGaugeVec) WithLabelValues(lvs ...string) DualGauge {
	return DualGauge{d.current.WithLabelValues(lvs...), d.deprecated.WithLabelValues(lvs...)}
}

func (d *DualGaugeVec) collectors() []prometheus.Collector {
	return []prometheus.Collector{d.current, d.deprecated}
}

// DualGauge is one label set of a DualGaugeVec.
type DualGauge struct{ current, deprecated prometheus.Gauge }

// Set writes the value to both exports.
func (g DualGauge) Set(v float64) {
	g.current.Set(v)
	g.deprecated.Set(v)
}

// SetToCurrentTime stamps both exports with the same wall clock read, so the
// two series can never disagree by a scheduling hiccup.
func (g DualGauge) SetToCurrentTime() {
	g.Set(float64(time.Now().UnixNano()) / 1e9)
}

// DualHistogramVec is a HistogramVec exported under both names.
type DualHistogramVec struct{ current, deprecated *prometheus.HistogramVec }

// WithLabelValues returns the observer pair for one label set.
func (d *DualHistogramVec) WithLabelValues(lvs ...string) DualObserver {
	return DualObserver{d.current.WithLabelValues(lvs...), d.deprecated.WithLabelValues(lvs...)}
}

func (d *DualHistogramVec) collectors() []prometheus.Collector {
	return []prometheus.Collector{d.current, d.deprecated}
}

// DualObserver is one label set of a DualHistogramVec.
type DualObserver struct{ current, deprecated prometheus.Observer }

// Observe records the sample in both exports.
func (o DualObserver) Observe(v float64) {
	o.current.Observe(v)
	o.deprecated.Observe(v)
}

func newDualCounterVec(name, deprecatedName, help string, labels []string) *DualCounterVec {
	return &DualCounterVec{
		current: prometheus.NewCounterVec(
			prometheus.CounterOpts{Name: name, Help: help}, labels),
		deprecated: prometheus.NewCounterVec(
			prometheus.CounterOpts{Name: deprecatedName, Help: deprecatedHelp(name, help)}, labels),
	}
}

func newDualGaugeVec(name, deprecatedName, help string, labels []string) *DualGaugeVec {
	return &DualGaugeVec{
		current: prometheus.NewGaugeVec(
			prometheus.GaugeOpts{Name: name, Help: help}, labels),
		deprecated: prometheus.NewGaugeVec(
			prometheus.GaugeOpts{Name: deprecatedName, Help: deprecatedHelp(name, help)}, labels),
	}
}

func newDualHistogramVec(name, deprecatedName, help string, buckets []float64, labels []string) *DualHistogramVec {
	return &DualHistogramVec{
		current: prometheus.NewHistogramVec(
			prometheus.HistogramOpts{Name: name, Help: help, Buckets: buckets}, labels),
		deprecated: prometheus.NewHistogramVec(
			prometheus.HistogramOpts{
				Name: deprecatedName, Help: deprecatedHelp(name, help), Buckets: buckets,
			}, labels),
	}
}

// Metrics holds every application metric plus the registry they live in.
type Metrics struct {
	Registry *prometheus.Registry

	HTTPDuration   *DualHistogramVec
	HTTPTotal      *DualCounterVec
	JobLastSuccess *DualGaugeVec
	JobFailures    *DualCounterVec
	JobDuration    *DualHistogramVec

	// Freshness gauges maintained by the Go-side freshness job.
	LastPriceTimestamp      *DualGaugeVec
	LastPredictionTimestamp *DualGaugeVec
}

// NewMetrics builds and registers all metrics on a fresh registry.
func NewMetrics() *Metrics {
	reg := prometheus.NewRegistry()
	m := &Metrics{
		Registry: reg,
		HTTPDuration: newDualHistogramVec(
			"talapala_api_http_request_duration_seconds",
			"goldpred_http_request_duration_seconds",
			"HTTP request latency by route, method and status code.",
			[]float64{.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10},
			[]string{"route", "method", "code"}),
		HTTPTotal: newDualCounterVec(
			"talapala_api_http_requests_total",
			"goldpred_http_requests_total",
			"Total HTTP requests by route, method and status code.",
			[]string{"route", "method", "code"}),
		JobLastSuccess: newDualGaugeVec(
			"talapala_api_job_last_success_timestamp_seconds",
			"goldpred_job_last_success_timestamp_seconds",
			"Unix timestamp of the last successful run per scheduled job.",
			[]string{"job"}),
		JobFailures: newDualCounterVec(
			"talapala_api_job_failure_total",
			"goldpred_job_failure_total",
			"Total failed runs per scheduled job.",
			[]string{"job"}),
		JobDuration: newDualHistogramVec(
			"talapala_api_job_duration_seconds",
			"goldpred_job_duration_seconds",
			"Duration of scheduled job runs.",
			// Must span the slowest job, not the typical one: train takes
			// 30–36 min in production and is allowed 90 (the internal-client
			// train timeout). With the old 300s ceiling every single training
			// run fell into +Inf, so no quantile could show a run getting
			// slower — the one thing this histogram exists to catch.
			[]float64{.1, .5, 1, 5, 15, 30, 60, 120, 300, 600, 1200, 1800, 2400, 3600, 5400},
			[]string{"job"}),
		LastPriceTimestamp: newDualGaugeVec(
			"talapala_api_last_price_timestamp_seconds",
			"goldpred_api_last_price_timestamp_seconds",
			"Unix timestamp of the latest stored price per symbol (Go freshness job).",
			[]string{"symbol"}),
		LastPredictionTimestamp: newDualGaugeVec(
			"talapala_api_last_prediction_timestamp_seconds",
			"goldpred_api_last_prediction_timestamp_seconds",
			"Unix timestamp of the latest prediction per horizon (Go freshness job).",
			[]string{"horizon"}),
	}

	app := [][]prometheus.Collector{
		m.HTTPDuration.collectors(), m.HTTPTotal.collectors(),
		m.JobLastSuccess.collectors(), m.JobFailures.collectors(), m.JobDuration.collectors(),
		m.LastPriceTimestamp.collectors(), m.LastPredictionTimestamp.collectors(),
	}
	for _, group := range app {
		reg.MustRegister(group...)
	}
	reg.MustRegister(
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
	// Stamped at image build time. Without it there is no way to ask a running
	// API which commit it is, so a stale container looks identical to a fresh
	// one — the exact failure that hid a deployed frontend change.
	commit := os.Getenv("BUILD_COMMIT")
	if commit == "" {
		commit = "unknown"
	}
	body := []byte(`{"status":"ok","build_commit":"` + commit + `"}`)
	return func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write(body)
	}
}

// ReadinessHandler checks db + redis; returns 503 if either is down.
//
// It no longer maintains goldpred_db_up / goldpred_redis_up. Those gauges were
// written here and nowhere else, and nothing polls readiness in normal
// operation (the container healthcheck hits /api/v1/health, Prometheus scrapes
// /metrics), so they sat at the zero value and reported both dependencies down
// forever. Refreshing them from a scheduler tick was the alternative, but the
// scheduler holds no database handle — it would have to borrow another
// component's pool and duplicate this check on a timer to restate what this
// endpoint already answers on demand, while a real outage already shows up as
// job failures and WARN/ERROR rows in the Issues table. Deleting a permanently
// wrong gauge is the smaller and more honest fix. The unused *Metrics
// parameter is kept so the wiring in cmd/api stays untouched.
func ReadinessHandler(_ *Metrics, db, redis Pinger) http.HandlerFunc {
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

func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}
