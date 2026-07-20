# Iran Gold Predictor — Frontend

React 18 + Vite 5 + TypeScript SPA for the Iran gold price analysis stack. Served by nginx in-container; all API calls go to same-origin `/api/v1/...` (proxied to the Go API, service name `api`, port 8080).

## Pages

- **Overview** — current 18k price, XAU, USD/IRT, theoretical vs observed price + premium, providers, signal, 30d sparkline
- **Forecast** — per-horizon predictions with interval bands, confidence, drivers, forecast-vs-actual history
- **Technical** — SMA/Bollinger price chart, RSI, MACD, indicator table with plain-language readings
- **Drivers** — XAU / USD-IRT trends, premium history vs 30d average, unusual-movement callouts
- **Portfolio** — transactions CRUD, summary (PnL, break-even, scenarios), CSV import/export
- **Alerts** — all 9 alert types with type-specific conditions, events feed with acknowledge
- **Models** — active model per horizon, metrics vs naive baseline, live accuracy, health

## Development

Requires the Go API on `http://localhost:8080` (Vite dev server proxies `/api`).

```sh
npm install
npm run dev      # http://localhost:5173
npm test         # vitest
npm run build    # tsc -b && vite build -> dist/
```

## Docker

```sh
docker build -t igp-frontend .
```

Multi-stage: `node:20-alpine` build → `nginx:1.27-alpine` serving `dist/` with SPA fallback, gzip, security headers, and `/api/` proxy to `http://api:8080`. `/metrics` is not exposed.

## Notes

- All Iranian values are canonical **IRT (toman)**; the header toggle switches display to rial (×10, display-only).
- Dates render in Asia/Tehran, with a Jalali/Gregorian toggle (jalaali-js).
- JWT stored in memory + `localStorage['igp_token']`.
