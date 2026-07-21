-- Central issue log: every WARNING/ERROR from the Go API, the Python
-- prediction service, and reported frontend errors lands here so the Issues
-- tab can show one aggregated, exportable view of everything going wrong.
CREATE TABLE app_issues (
    id           BIGSERIAL PRIMARY KEY,
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    service      TEXT NOT NULL CHECK (service IN ('api', 'prediction', 'frontend')),
    level        TEXT NOT NULL CHECK (level IN ('warning', 'error')),
    source       TEXT NOT NULL DEFAULT '',
    message      TEXT NOT NULL,
    details      JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_app_issues_time ON app_issues (occurred_at DESC);
CREATE INDEX idx_app_issues_level_time ON app_issues (level, occurred_at DESC);
CREATE INDEX idx_app_issues_service_time ON app_issues (service, occurred_at DESC);
