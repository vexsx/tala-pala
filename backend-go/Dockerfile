# Build context is the repo root; dockerfile path backend-go/Dockerfile.
#   docker build -f backend-go/Dockerfile .

# ---------- Build stage ----------
FROM golang:1.25-alpine AS build
WORKDIR /src

# Cache module downloads first.
COPY backend-go/go.mod backend-go/go.sum ./
RUN go mod download

COPY backend-go/ ./
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/api ./cmd/api \
 && CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/createuser ./cmd/createuser

# ---------- Runtime stage ----------
FROM alpine:3.20
RUN apk add --no-cache ca-certificates curl tzdata \
 && addgroup -S app && adduser -S -G app app

WORKDIR /app
COPY --from=build /out/api /app/api
COPY --from=build /out/createuser /app/createuser
# Migrations live at /app/migrations (MIGRATIONS_DIR default).
COPY database/migrations /app/migrations

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS http://localhost:8080/api/v1/health || exit 1

ENTRYPOINT ["/app/api"]
