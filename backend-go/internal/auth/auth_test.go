package auth

import (
	"testing"
	"time"

	"golang.org/x/crypto/bcrypt"
)

func TestBcryptRoundtrip(t *testing.T) {
	hash, err := bcrypt.GenerateFromPassword([]byte("correct-horse-battery"), BcryptCost)
	if err != nil {
		t.Fatal(err)
	}
	if cost, _ := bcrypt.Cost(hash); cost != 12 {
		t.Fatalf("expected cost 12, got %d", cost)
	}
	if err := bcrypt.CompareHashAndPassword(hash, []byte("correct-horse-battery")); err != nil {
		t.Fatalf("valid password rejected: %v", err)
	}
	if err := bcrypt.CompareHashAndPassword(hash, []byte("wrong-password")); err == nil {
		t.Fatal("wrong password accepted")
	}
}

func TestTokenCreateVerify(t *testing.T) {
	tm := NewTokenManager("test-secret-that-is-long-enough-123", time.Hour)
	tok, exp, err := tm.Create("user-1", "a@b.com", "admin")
	if err != nil {
		t.Fatal(err)
	}
	if time.Until(exp) < 59*time.Minute {
		t.Fatalf("expiry too soon: %v", exp)
	}
	claims, err := tm.Verify(tok)
	if err != nil {
		t.Fatal(err)
	}
	if claims.Sub != "user-1" || claims.Email != "a@b.com" || claims.Role != "admin" {
		t.Fatalf("bad claims: %+v", claims)
	}
}

func TestTokenExpiry(t *testing.T) {
	tm := NewTokenManager("test-secret-that-is-long-enough-123", time.Hour)
	base := time.Now()
	tm.SetClock(func() time.Time { return base })
	tok, _, err := tm.Create("u", "e@x.com", "user")
	if err != nil {
		t.Fatal(err)
	}
	// Still valid just before expiry.
	tm.SetClock(func() time.Time { return base.Add(59 * time.Minute) })
	if _, err := tm.Verify(tok); err != nil {
		t.Fatalf("token should still be valid: %v", err)
	}
	// Expired after TTL.
	tm.SetClock(func() time.Time { return base.Add(61 * time.Minute) })
	if _, err := tm.Verify(tok); err == nil {
		t.Fatal("expired token accepted")
	}
}

func TestTokenWrongSecret(t *testing.T) {
	tm1 := NewTokenManager("secret-one-that-is-long-enough-xxxx", time.Hour)
	tm2 := NewTokenManager("secret-two-that-is-long-enough-yyyy", time.Hour)
	tok, _, _ := tm1.Create("u", "e@x.com", "user")
	if _, err := tm2.Verify(tok); err == nil {
		t.Fatal("token with wrong secret accepted")
	}
}

func TestTokenGarbage(t *testing.T) {
	tm := NewTokenManager("test-secret-that-is-long-enough-123", time.Hour)
	for _, tok := range []string{"", "garbage", "a.b.c"} {
		if _, err := tm.Verify(tok); err == nil {
			t.Fatalf("garbage token %q accepted", tok)
		}
	}
}

func TestValidateRegistration(t *testing.T) {
	if p := ValidateRegistration("a@b.com", "longenough123"); p != nil {
		t.Fatalf("valid registration rejected: %v", p)
	}
	if p := ValidateRegistration("not-an-email", "longenough123"); p == nil {
		t.Fatal("bad email accepted")
	}
	if p := ValidateRegistration("a@b.com", "short"); p == nil {
		t.Fatal("9-char password accepted")
	}
	if p := ValidateRegistration("a@b.com", "123456789"); p == nil {
		t.Fatal("password under 10 chars accepted")
	}
}
