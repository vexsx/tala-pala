package config

import (
	"errors"
	"strings"
	"testing"
)

func baseEnv() map[string]string {
	return map[string]string{
		"POSTGRES_PASSWORD":  "pw",
		"JWT_SECRET":         "0123456789abcdef0123456789abcdef",
		"INTERNAL_API_TOKEN": "tok",
	}
}

func TestLoad_DefaultsAndRequired(t *testing.T) {
	cfg, err := Load(baseEnv(), nil)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.APIPort != "8080" || cfg.PostgresHost != "postgres" || cfg.PostgresDB != "goldpred" {
		t.Fatalf("bad defaults: %+v", cfg)
	}
	if cfg.JWTTTLHours != 24 || cfg.RateLimitRPM != 60 || !cfg.SchedulerEnabled {
		t.Fatalf("bad defaults: %+v", cfg)
	}
	if cfg.AllowOpenRegistration {
		t.Fatal("open registration must default to false")
	}
	if cfg.Crons.Collect != "*/10 * * * *" || cfg.Crons.Train != "30 2 * * *" {
		t.Fatalf("bad cron defaults: %+v", cfg.Crons)
	}
	if cfg.MigrationsDir != "/app/migrations" {
		t.Fatalf("bad migrations dir: %s", cfg.MigrationsDir)
	}
	if cfg.MarketTehranOpen != "09:00" || cfg.MarketTehranClose != "20:00" {
		t.Fatalf("bad market-hours defaults: %q-%q", cfg.MarketTehranOpen, cfg.MarketTehranClose)
	}
}

func TestLoad_MarketHours(t *testing.T) {
	env := baseEnv()
	env["MARKET_TEHRAN_OPEN"] = "08:30"
	env["MARKET_TEHRAN_CLOSE"] = "21:15"
	cfg, err := Load(env, nil)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.MarketTehranOpen != "08:30" || cfg.MarketTehranClose != "21:15" {
		t.Fatalf("overrides not applied: %q-%q", cfg.MarketTehranOpen, cfg.MarketTehranClose)
	}

	env["MARKET_TEHRAN_OPEN"] = "9am"
	if _, err := Load(env, nil); err == nil || !strings.Contains(err.Error(), "MARKET_TEHRAN_OPEN") {
		t.Fatalf("invalid MARKET_TEHRAN_OPEN accepted: %v", err)
	}
	env["MARKET_TEHRAN_OPEN"] = "08:30"
	env["MARKET_TEHRAN_CLOSE"] = "25:00"
	if _, err := Load(env, nil); err == nil || !strings.Contains(err.Error(), "MARKET_TEHRAN_CLOSE") {
		t.Fatalf("invalid MARKET_TEHRAN_CLOSE accepted: %v", err)
	}
}

func TestLoad_MissingRequired(t *testing.T) {
	_, err := Load(map[string]string{}, nil)
	if err == nil {
		t.Fatal("expected error for empty env")
	}
	for _, want := range []string{"POSTGRES_PASSWORD", "JWT_SECRET", "INTERNAL_API_TOKEN"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("error should mention %s: %v", want, err)
		}
	}
}

func TestLoad_ShortJWTSecretRejected(t *testing.T) {
	env := baseEnv()
	env["JWT_SECRET"] = "short"
	if _, err := Load(env, nil); err == nil {
		t.Fatal("short JWT secret accepted")
	}
}

func TestLoad_FileVariants(t *testing.T) {
	files := map[string]string{
		"/run/secrets/jwt":  "file-jwt-secret-0123456789abcdef-xyz\n",
		"/run/secrets/pg":   "file-pg-password",
		"/run/secrets/itok": "file-internal-token",
	}
	readFile := func(path string) ([]byte, error) {
		if v, ok := files[path]; ok {
			return []byte(v), nil
		}
		return nil, errors.New("no such file")
	}
	env := map[string]string{
		"JWT_SECRET":              "env-value-should-be-overridden-xxxx",
		"JWT_SECRET_FILE":         "/run/secrets/jwt",
		"POSTGRES_PASSWORD":       "env-pw",
		"POSTGRES_PASSWORD_FILE":  "/run/secrets/pg",
		"INTERNAL_API_TOKEN_FILE": "/run/secrets/itok",
	}
	cfg, err := Load(env, readFile)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.JWTSecret != "file-jwt-secret-0123456789abcdef-xyz" {
		t.Fatalf("JWT_SECRET_FILE not applied (or not trimmed): %q", cfg.JWTSecret)
	}
	if cfg.PostgresPassword != "file-pg-password" {
		t.Fatalf("POSTGRES_PASSWORD_FILE should override env: %q", cfg.PostgresPassword)
	}
	if cfg.InternalAPIToken != "file-internal-token" {
		t.Fatalf("INTERNAL_API_TOKEN_FILE not applied: %q", cfg.InternalAPIToken)
	}
}

func TestLoad_FileVariantUnreadable(t *testing.T) {
	env := baseEnv()
	env["JWT_SECRET_FILE"] = "/nope"
	readFile := func(string) ([]byte, error) { return nil, errors.New("denied") }
	if _, err := Load(env, readFile); err == nil {
		t.Fatal("unreadable secret file should fail")
	}
}

func TestLoad_CORSAndOverrides(t *testing.T) {
	env := baseEnv()
	env["CORS_ALLOWED_ORIGINS"] = "http://a.example, http://b.example ,"
	env["RATE_LIMIT_RPM"] = "120"
	env["ALLOW_OPEN_REGISTRATION"] = "true"
	env["SCHEDULE_COLLECT_CRON"] = "*/5 * * * *"
	cfg, err := Load(env, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(cfg.CORSAllowedOrigins) != 2 || cfg.CORSAllowedOrigins[1] != "http://b.example" {
		t.Fatalf("bad CORS parse: %v", cfg.CORSAllowedOrigins)
	}
	if cfg.RateLimitRPM != 120 || !cfg.AllowOpenRegistration || cfg.Crons.Collect != "*/5 * * * *" {
		t.Fatalf("overrides not applied: %+v", cfg)
	}
}

func TestLoad_InvalidInt(t *testing.T) {
	env := baseEnv()
	env["JWT_TTL_HOURS"] = "abc"
	if _, err := Load(env, nil); err == nil {
		t.Fatal("invalid int accepted")
	}
}

func TestDSN_Escaping(t *testing.T) {
	env := baseEnv()
	env["POSTGRES_PASSWORD"] = "p@ss:w/rd"
	cfg, err := Load(env, nil)
	if err != nil {
		t.Fatal(err)
	}
	dsn := cfg.DSN()
	if strings.Contains(dsn, "p@ss:w/rd") {
		t.Fatalf("password not escaped in DSN: %s", dsn)
	}
	if !strings.Contains(dsn, "p%40ss%3Aw%2Frd") {
		t.Fatalf("unexpected DSN: %s", dsn)
	}
}
