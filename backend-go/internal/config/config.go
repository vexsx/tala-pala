// Package config parses environment configuration for the Go API.
// It supports *_FILE secret variants (Docker secrets): when KEY_FILE is set,
// the secret value is read from that file and overrides KEY.
package config

import (
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"
)

// CronConfig holds the cron specs (standard 5-field, UTC) for scheduler jobs.
type CronConfig struct {
	Collect  string
	Predict  string
	Signals  string
	Evaluate string
	Train    string
	Alerts   string
	Cleanup  string
}

// Config is the fully-parsed application configuration.
type Config struct {
	PostgresHost     string
	PostgresPort     string
	PostgresDB       string
	PostgresUser     string
	PostgresPassword string

	RedisAddr string

	InternalAPIToken string

	APIPort               string
	JWTSecret             string
	JWTTTLHours           int
	AllowOpenRegistration bool
	PredictionServiceURL  string
	SchedulerEnabled      bool
	RateLimitRPM          int
	CORSAllowedOrigins    []string
	LogLevel              string
	MigrationsDir         string
	StaleMinutes          int

	// Tehran session bounds ("HH:MM", Asia/Tehran local) for the Addendum 1
	// market-hours rules, shared with the Python service.
	MarketTehranOpen  string
	MarketTehranClose string

	Crons CronConfig
}

// FileReader reads a secret file; injectable for tests.
type FileReader func(path string) ([]byte, error)

// Load parses configuration from the given environment map. Missing required
// values produce a single aggregated error so operators can fix everything at once.
func Load(env map[string]string, readFile FileReader) (*Config, error) {
	if readFile == nil {
		readFile = os.ReadFile
	}

	var errs []string

	secret := func(key string) string {
		if path, ok := env[key+"_FILE"]; ok && path != "" {
			b, err := readFile(path)
			if err != nil {
				errs = append(errs, fmt.Sprintf("%s_FILE=%s: %v", key, path, err))
				return ""
			}
			return strings.TrimSpace(string(b))
		}
		return env[key]
	}

	get := func(key, def string) string {
		if v, ok := env[key]; ok && v != "" {
			return v
		}
		return def
	}

	getInt := func(key string, def int) int {
		v, ok := env[key]
		if !ok || v == "" {
			return def
		}
		n, err := strconv.Atoi(v)
		if err != nil {
			errs = append(errs, fmt.Sprintf("%s must be an integer, got %q", key, v))
			return def
		}
		return n
	}

	getBool := func(key string, def bool) bool {
		v, ok := env[key]
		if !ok || v == "" {
			return def
		}
		b, err := strconv.ParseBool(v)
		if err != nil {
			errs = append(errs, fmt.Sprintf("%s must be a boolean, got %q", key, v))
			return def
		}
		return b
	}

	cfg := &Config{
		PostgresHost:     get("POSTGRES_HOST", "postgres"),
		PostgresPort:     get("POSTGRES_PORT", "5432"),
		PostgresDB:       get("POSTGRES_DB", "goldpred"),
		PostgresUser:     get("POSTGRES_USER", "goldpred"),
		PostgresPassword: secret("POSTGRES_PASSWORD"),

		RedisAddr: get("REDIS_ADDR", "redis:6379"),

		InternalAPIToken: secret("INTERNAL_API_TOKEN"),

		APIPort:               get("API_PORT", "8080"),
		JWTSecret:             secret("JWT_SECRET"),
		JWTTTLHours:           getInt("JWT_TTL_HOURS", 24),
		AllowOpenRegistration: getBool("ALLOW_OPEN_REGISTRATION", false),
		PredictionServiceURL:  get("PREDICTION_SERVICE_URL", "http://prediction-service:8500"),
		SchedulerEnabled:      getBool("SCHEDULER_ENABLED", true),
		RateLimitRPM:          getInt("RATE_LIMIT_RPM", 60),
		LogLevel:              strings.ToLower(get("LOG_LEVEL", "info")),
		MigrationsDir:         get("MIGRATIONS_DIR", "/app/migrations"),
		StaleMinutes:          getInt("STALE_MINUTES", 30),

		MarketTehranOpen:  get("MARKET_TEHRAN_OPEN", "12:00"),
		MarketTehranClose: get("MARKET_TEHRAN_CLOSE", "20:00"),

		Crons: CronConfig{
			Collect:  get("SCHEDULE_COLLECT_CRON", "*/10 * * * *"),
			Predict:  get("SCHEDULE_PREDICT_CRON", "5 * * * *"),
			Signals:  get("SCHEDULE_SIGNALS_CRON", "10 * * * *"),
			Evaluate: get("SCHEDULE_EVALUATE_CRON", "20 * * * *"),
			Train:    get("SCHEDULE_TRAIN_CRON", "30 2 * * *"),
			Alerts:   get("SCHEDULE_ALERTS_CRON", "*/5 * * * *"),
			Cleanup:  get("SCHEDULE_CLEANUP_CRON", "0 4 * * *"),
		},
	}

	for _, origin := range strings.Split(get("CORS_ALLOWED_ORIGINS", ""), ",") {
		if o := strings.TrimSpace(origin); o != "" {
			cfg.CORSAllowedOrigins = append(cfg.CORSAllowedOrigins, o)
		}
	}

	// Required values: fail fast with a clear message.
	if cfg.PostgresPassword == "" {
		errs = append(errs, "POSTGRES_PASSWORD (or POSTGRES_PASSWORD_FILE) is required")
	}
	if cfg.JWTSecret == "" {
		errs = append(errs, "JWT_SECRET (or JWT_SECRET_FILE) is required")
	} else if len(cfg.JWTSecret) < 32 {
		errs = append(errs, "JWT_SECRET must be at least 32 characters")
	}
	if cfg.InternalAPIToken == "" {
		errs = append(errs, "INTERNAL_API_TOKEN (or INTERNAL_API_TOKEN_FILE) is required")
	}
	if cfg.JWTTTLHours <= 0 {
		errs = append(errs, "JWT_TTL_HOURS must be positive")
	}
	if cfg.RateLimitRPM <= 0 {
		errs = append(errs, "RATE_LIMIT_RPM must be positive")
	}
	for key, val := range map[string]string{
		"MARKET_TEHRAN_OPEN":  cfg.MarketTehranOpen,
		"MARKET_TEHRAN_CLOSE": cfg.MarketTehranClose,
	} {
		if _, err := time.Parse("15:04", val); err != nil {
			errs = append(errs, fmt.Sprintf("%s must be HH:MM, got %q", key, val))
		}
	}

	if len(errs) > 0 {
		return nil, fmt.Errorf("invalid configuration:\n  - %s", strings.Join(errs, "\n  - "))
	}
	return cfg, nil
}

// FromOSEnv loads configuration from the process environment.
func FromOSEnv() (*Config, error) {
	env := map[string]string{}
	for _, kv := range os.Environ() {
		if i := strings.IndexByte(kv, '='); i > 0 {
			env[kv[:i]] = kv[i+1:]
		}
	}
	return Load(env, os.ReadFile)
}

// DSN returns a pgx-compatible PostgreSQL connection string.
func (c *Config) DSN() string {
	return fmt.Sprintf("postgres://%s:%s@%s:%s/%s?sslmode=disable",
		urlEscape(c.PostgresUser), urlEscape(c.PostgresPassword),
		c.PostgresHost, c.PostgresPort, c.PostgresDB)
}

func urlEscape(s string) string {
	r := strings.NewReplacer("%", "%25", "@", "%40", ":", "%3A", "/", "%2F", "?", "%3F", "#", "%23", " ", "%20")
	return r.Replace(s)
}
