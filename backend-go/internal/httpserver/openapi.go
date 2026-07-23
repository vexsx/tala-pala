package httpserver

import (
	"net/http"

	"github.com/danaix/iran-gold-predictor/backend-go/docs"
)

// openAPISpecHandler serves the embedded OpenAPI document.
func openAPISpecHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "application/yaml; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(docs.OpenAPISpec)
}

// docsPageHandler serves a minimal, fully self-contained HTML page (no CDN,
// no external assets) that links to the raw OpenAPI spec and summarises the
// available endpoints.
func docsPageHandler(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	// Relax CSP just enough for the inline stylesheet on this one page.
	w.Header().Set("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write([]byte(docsHTML))
}

const docsHTML = `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Iran Gold Predictor API</title>
<style>
  body { font-family: ui-monospace, Consolas, monospace; max-width: 60rem; margin: 2rem auto; padding: 0 1rem; color: #222; }
  h1 { font-size: 1.4rem; } h2 { font-size: 1.1rem; margin-top: 1.6rem; }
  code { background: #f2f2f2; padding: .1rem .3rem; border-radius: 3px; }
  li { margin: .25rem 0; }
  .m { display: inline-block; min-width: 3.5rem; font-weight: bold; }
</style>
</head>
<body>
<h1>Iran Gold Predictor — Go API</h1>
<p>Machine-readable spec: <a href="/api/v1/docs/openapi.yaml">openapi.yaml</a> (OpenAPI 3.0).
Authenticated endpoints require <code>Authorization: Bearer &lt;JWT&gt;</code>.</p>
<h2>Public</h2>
<ul>
  <li><span class="m">GET</span> <code>/api/v1/health</code> — liveness</li>
  <li><span class="m">GET</span> <code>/api/v1/readiness</code> — db + redis readiness</li>
  <li><span class="m">GET</span> <code>/metrics</code> — Prometheus metrics</li>
  <li><span class="m">POST</span> <code>/api/v1/auth/register</code></li>
  <li><span class="m">POST</span> <code>/api/v1/auth/login</code></li>
</ul>
<h2>Market data</h2>
<ul>
  <li><span class="m">GET</span> <code>/api/v1/prices/current</code></li>
  <li><span class="m">GET</span> <code>/api/v1/prices/history?symbol=&amp;from=&amp;to=&amp;interval=&amp;page=&amp;page_size=</code></li>
  <li><span class="m">GET</span> <code>/api/v1/market/summary</code></li>
  <li><span class="m">GET</span> <code>/api/v1/market/premium?days=90</code></li>
  <li><span class="m">GET</span> <code>/api/v1/market/indicators?days=90</code></li>
</ul>
<h2>Predictions, signals, models</h2>
<ul>
  <li><span class="m">GET</span> <code>/api/v1/predictions</code></li>
  <li><span class="m">GET</span> <code>/api/v1/predictions/{horizon}?limit=50</code></li>
  <li><span class="m">GET</span> <code>/api/v1/signals/current</code></li>
  <li><span class="m">GET</span> <code>/api/v1/signals/history?limit=50</code></li>
  <li><span class="m">GET</span> <code>/api/v1/models</code></li>
  <li><span class="m">GET</span> <code>/api/v1/models/performance?symbol=</code></li>
</ul>
<h2>Portfolio</h2>
<ul>
  <li><span class="m">GET</span> <code>/api/v1/portfolio</code></li>
  <li><span class="m">POST</span> <code>/api/v1/portfolio/transactions</code></li>
  <li><span class="m">PUT</span> <code>/api/v1/portfolio/transactions/{id}</code></li>
  <li><span class="m">DELETE</span> <code>/api/v1/portfolio/transactions/{id}</code></li>
  <li><span class="m">POST</span> <code>/api/v1/portfolio/import</code> — multipart CSV, max 1 MB</li>
  <li><span class="m">GET</span> <code>/api/v1/portfolio/export</code> — CSV</li>
</ul>
<h2>Alerts</h2>
<ul>
  <li><span class="m">GET / POST</span> <code>/api/v1/alerts</code></li>
  <li><span class="m">PUT / DELETE</span> <code>/api/v1/alerts/{id}</code></li>
  <li><span class="m">GET</span> <code>/api/v1/alerts/events?unacked=true</code></li>
  <li><span class="m">POST</span> <code>/api/v1/alerts/events/{id}/ack</code></li>
</ul>
<h2>Admin</h2>
<ul>
  <li><span class="m">POST</span> <code>/api/v1/admin/jobs/{collect|train|predict|signals|backtest|evaluate}</code></li>
  <li><span class="m">GET</span> <code>/api/v1/admin/audit?page=1</code></li>
</ul>
<p>Predictions are uncertain estimates, not financial advice.</p>
</body>
</html>
`
