# API Documentation

The full machine-readable specification is `backend-go/docs/openapi.yaml`, served at runtime at `GET /api/v1/docs/openapi.yaml` (human landing page at `/api/v1/docs`).

## Conventions
- Base path `/api/v1`; JSON everywhere; UTC ISO-8601 timestamps; Iranian amounts in **toman (IRT)**.
- Auth: `Authorization: Bearer <JWT>` from `POST /api/v1/auth/login`. Health/readiness and auth endpoints are public; everything else requires a token; `/api/v1/admin/*` requires role `admin`.
- Errors: `{"error":{"code":"...","message":"...","details":{}}}` with matching HTTP status. Every response carries `X-Request-ID`.
- Pagination: `page`, `page_size` query params; responses include `total`.
- Rate limits: 60 req/min/IP by default (429 when exceeded), 10/min for login.

## Endpoint summary

| Method & path | Purpose |
|---|---|
| `POST /auth/register` · `POST /auth/login` · `GET /auth/me` | Account & session |
| `GET /health` · `GET /readiness` · `GET /metrics` | Liveness, readiness (DB+Redis), Prometheus |
| `GET /prices/current` | Latest normalized price per symbol + staleness + 24h change |
| `GET /prices/history?symbol&from&to&interval&page` | Historical series (raw/hourly/daily buckets) |
| `GET /market/summary` | Dashboard payload: 18k price, XAU, USD/IRT, theoretical vs observed, premium, provider health, latest signal |
| `GET /market/premium?days` | Theoretical vs observed premium history |
| `GET /market/indicators?days` | SMA/EMA/RSI/MACD/Bollinger/ATR/momentum/ROC/volatility/support/resistance |
| `GET /market/provider-gap?symbol&window_minutes&history_days` | Dispersion between providers quoting the same symbol (current per-provider quotes, gap %, daily gap history) |
| `GET /market/candles?symbol&interval&days` | OHLC candles + chart-ready overlays (SMA/Bollinger/Ichimoku/SuperTrend/PSAR) + pivot levels for the Trade panel |
| `GET /predictions` · `GET /predictions/{horizon}` | Latest per horizon · per-horizon history incl. realized actuals |
| `GET /predictions/custom?days=N` | On-demand forecast + buy/hold/sell lean for an arbitrary 1–90 day horizon (computed live, not persisted) |
| `GET /issues?limit&level&service&since_hours` · `POST /issues` · `GET /issues/report` | Aggregated warnings/errors from all services · frontend error reporting · Markdown debug digest |
| `GET /signals/current` · `GET /signals/history` | Explainable Buy/Hold/Sell signal |
| `GET /models` · `GET /models/performance` | Model registry · metrics vs baseline + live accuracy |
| `GET/POST /portfolio*` (`/transactions`, `/import`, `/export`) | Holdings CRUD, CSV import/export, valuation, scenarios |
| `GET/POST/PUT/DELETE /alerts*` (`/events`, `/events/{id}/ack`) | Alert rules and in-app events |
| `POST /admin/jobs/{collect\|train\|predict\|signals\|backtest\|evaluate}` | Manual job triggers (proxied to the prediction service) |
| `GET /admin/audit` | Audit log |
| `GET/POST /admin/users` · `PUT/DELETE /admin/users/{id}` | Full user management (list/create/change role/reset password/delete; self-registration is closed) |

## Example

```bash
TOKEN=$(curl -s -X POST http://localhost:8088/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@example.com","password":"..."}' | jq -r .token)

curl -s http://localhost:8088/api/v1/market/summary -H "Authorization: Bearer $TOKEN" | jq
```

The internal Python API (`/internal/*`, port 8500) is documented in `docs/CONTRACTS.md`; it is not reachable from outside the Docker network and requires the `X-Internal-Token` header.
