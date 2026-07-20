# Security Design

## Threat model (summary)

Self-hosted single-tenant deployment on a Linux server; adversaries are internet scanners, credential stuffers, and malicious input (CSV uploads, API payloads). The system holds no payment data; the most sensitive assets are the user's portfolio records and credentials.

## Controls

### Authentication & authorization
- Passwords hashed with **bcrypt (cost 12)**; minimum length 10 enforced server-side.
- **JWT HS256** with server-side secret (`JWT_SECRET`, ≥32 chars enforced), TTL via `JWT_TTL_HOURS`.
- Registration is closed by default (`ALLOW_OPEN_REGISTRATION=false`); the first user becomes `admin`, later users must be created by the admin (`make create-user`) — appropriate for a personal deployment.
- Role separation: `admin` gates job triggers and audit access.
- Login endpoint rate-limited to 10/min/IP; global API limit `RATE_LIMIT_RPM` (default 60).

### Service-to-service
- The Python service is reachable only on the internal Docker network and requires `X-Internal-Token` on every `/internal/*` call.
- PostgreSQL and Redis are **not published** to the host in any compose file.

### Secrets
- No secrets in code or images. `.env` is git-ignored; each secret also supports a `*_FILE` variant (`JWT_SECRET_FILE`, `POSTGRES_PASSWORD_FILE`, `INTERNAL_API_TOKEN_FILE`) for mounted secret files.
- `.env.example` ships only placeholders; `scripts/init.sh` generates random secrets.

### Input handling
- All user input validated (types, ranges, enum whitelists); all SQL is parameterized (pgx / SQLAlchemy bound parameters).
- CSV import: 1 MB size cap, strict column whitelist, per-row validation, MIME/multipart handling via stdlib.
- **CSV formula injection**: exported cells beginning with `= + - @` are prefixed with `'`; imported text fields are sanitized the same way.
- Upload size limits also enforced at nginx (`client_max_body_size 2m`).

### HTTP
- Security headers on every response: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, restrictive `Content-Security-Policy`; HSTS at the TLS proxy.
- CORS restricted to `CORS_ALLOWED_ORIGINS`.
- Structured error envelope never leaks stack traces or internals; debug mode off in production images.

### Containers
- Multi-stage builds; minimal base images (alpine/slim); **non-root users** in api and prediction-service containers; pinned base image tags and pinned dependency versions.
- Health checks and restart policies on every service; resource limits in compose.
- Recommended scanning (documented, run in CI or manually): `docker scout cves` / `trivy image` for images, `govulncheck ./...` for Go, `pip-audit -r requirements.txt` for Python, `npm audit` for the frontend.

### Data collection ethics
- Providers are accessed with an honest User-Agent, conservative timeouts, retry with exponential backoff, and courtesy rate limiting. The system **never** bypasses authentication, CAPTCHA, or anti-bot measures. Hamrah Gold is not scraped; holdings are entered manually or via CSV.

### Audit
- `audit_logs` records auth events and portfolio/alert mutations with user id, request id, IP, and details.

## Residual risks / recommendations
- Put a TLS-terminating proxy in front of port 8088 before exposing to the internet; never serve it publicly over plain HTTP.
- Keep the server patched; restrict SSH; consider fail2ban.
- Dump the database occasionally (`pg_dump` one-liner in docs/deployment.md) and store copies off-host.
- JWTs are bearer tokens in localStorage — acceptable for a personal tool behind TLS; a cookie+CSRF scheme is the documented upgrade path if multi-user exposure grows.
