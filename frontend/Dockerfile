# --- Build stage ---
FROM node:20-alpine AS build
WORKDIR /app

COPY package.json package-lock.json ./
# npm ci installs the exact committed lockfile — reproducible builds, no
# silent transitive upgrades on rebuild.
RUN npm ci --no-audit --no-fund

COPY . .
# Build identity: the SPA is a static bundle, so without this there is no way
# to ask a running nginx which commit it was built from.
ARG GIT_COMMIT=unknown
ENV VITE_BUILD_COMMIT=$GIT_COMMIT
RUN npm run build \
 && printf '{"service":"frontend","build_commit":"%s"}\n' "$GIT_COMMIT" > dist/build-info.json

# --- Serve stage ---
FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget -qO- http://localhost:80/ > /dev/null 2>&1 || exit 1
