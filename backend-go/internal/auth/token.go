// Package auth implements registration, login and JWT handling.
package auth

import (
	"errors"
	"fmt"
	"time"

	"github.com/golang-jwt/jwt/v5"
)

// BcryptCost is the bcrypt work factor used for password hashes.
const BcryptCost = 12

// Claims is the identity carried by an access token.
type Claims struct {
	Sub   string
	Email string
	Role  string
}

// TokenManager creates and verifies HS256 JWTs with claims
// {sub, email, role, exp, iat}.
type TokenManager struct {
	secret []byte
	ttl    time.Duration
	now    func() time.Time // injectable for tests
}

// NewTokenManager builds a TokenManager with the given secret and TTL.
func NewTokenManager(secret string, ttl time.Duration) *TokenManager {
	return &TokenManager{secret: []byte(secret), ttl: ttl, now: time.Now}
}

// SetClock overrides the time source (tests only).
func (tm *TokenManager) SetClock(now func() time.Time) { tm.now = now }

// Create signs a new token; returns the token string and its expiry (UTC).
func (tm *TokenManager) Create(userID, email, role string) (string, time.Time, error) {
	now := tm.now().UTC()
	exp := now.Add(tm.ttl)
	tok := jwt.NewWithClaims(jwt.SigningMethodHS256, jwt.MapClaims{
		"sub":   userID,
		"email": email,
		"role":  role,
		"iat":   now.Unix(),
		"exp":   exp.Unix(),
	})
	signed, err := tok.SignedString(tm.secret)
	if err != nil {
		return "", time.Time{}, fmt.Errorf("sign token: %w", err)
	}
	return signed, exp, nil
}

// Verify parses and validates a token string (HS256 only, exp enforced).
func (tm *TokenManager) Verify(tokenString string) (Claims, error) {
	tok, err := jwt.Parse(tokenString, func(t *jwt.Token) (any, error) {
		if _, ok := t.Method.(*jwt.SigningMethodHMAC); !ok {
			return nil, fmt.Errorf("unexpected signing method %v", t.Header["alg"])
		}
		return tm.secret, nil
	},
		jwt.WithValidMethods([]string{"HS256"}),
		jwt.WithTimeFunc(func() time.Time { return tm.now() }),
	)
	if err != nil {
		return Claims{}, err
	}
	mc, ok := tok.Claims.(jwt.MapClaims)
	if !ok || !tok.Valid {
		return Claims{}, errors.New("invalid token claims")
	}
	sub, _ := mc["sub"].(string)
	email, _ := mc["email"].(string)
	role, _ := mc["role"].(string)
	if sub == "" || role == "" {
		return Claims{}, errors.New("token missing required claims")
	}
	return Claims{Sub: sub, Email: email, Role: role}, nil
}
