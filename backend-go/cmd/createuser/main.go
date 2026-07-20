// Command createuser creates a user directly in the database (bootstrap /
// admin tooling, typically run via `docker compose run api createuser ...`).
//
// Usage: createuser -email you@example.com -password secret1234 [-role admin]
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"time"

	"golang.org/x/crypto/bcrypt"

	"github.com/danaix/iran-gold-predictor/backend-go/internal/auth"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/config"
	"github.com/danaix/iran-gold-predictor/backend-go/internal/storage"
)

func main() {
	email := flag.String("email", "", "user email (required)")
	password := flag.String("password", "", "password, min 10 chars (required)")
	role := flag.String("role", "user", "role: user or admin")
	flag.Parse()

	if err := run(*email, *password, *role); err != nil {
		fmt.Fprintln(os.Stderr, "error:", err)
		os.Exit(1)
	}
}

func run(email, password, role string) error {
	email = strings.ToLower(strings.TrimSpace(email))
	if problems := auth.ValidateRegistration(email, password); problems != nil {
		return fmt.Errorf("invalid input: %v", problems)
	}
	if role != "user" && role != "admin" {
		return fmt.Errorf("role must be user or admin, got %q", role)
	}

	cfg, err := config.FromOSEnv()
	if err != nil {
		return err
	}
	logger := slog.New(slog.NewJSONHandler(os.Stderr, nil))

	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	pool, err := storage.NewPool(ctx, cfg.DSN(), logger)
	if err != nil {
		return err
	}
	defer pool.Close()

	hash, err := bcrypt.GenerateFromPassword([]byte(password), auth.BcryptCost)
	if err != nil {
		return fmt.Errorf("bcrypt: %w", err)
	}

	var id string
	err = pool.QueryRow(ctx,
		`INSERT INTO users (email, password_hash, role) VALUES ($1, $2, $3) RETURNING id`,
		email, string(hash), role).Scan(&id)
	if err != nil {
		if strings.Contains(err.Error(), "duplicate key") {
			return fmt.Errorf("user %s already exists", email)
		}
		return err
	}
	fmt.Printf("created user %s (id=%s, role=%s)\n", email, id, role)
	return nil
}
