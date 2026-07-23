package auth

import (
	"testing"
	"time"
)

func TestLockoutAfterFiveFails(t *testing.T) {
	var l loginLockout
	now := time.Now()
	for i := 0; i < 4; i++ {
		if l.fail("a@b.c", now) {
			t.Fatalf("locked after %d fails, want 5", i+1)
		}
		if l.locked("a@b.c", now) {
			t.Fatal("must not be locked before the 5th failure")
		}
	}
	if !l.fail("a@b.c", now) {
		t.Fatal("5th failure must lock")
	}
	if !l.locked("a@b.c", now) {
		t.Fatal("account must be locked")
	}
	if !l.locked("a@b.c", now.Add(lockDuration-time.Second)) {
		t.Fatal("still locked just before expiry")
	}
	if l.locked("a@b.c", now.Add(lockDuration+time.Second)) {
		t.Fatal("lock must expire")
	}
}

func TestLockoutWindowAndSuccessReset(t *testing.T) {
	var l loginLockout
	now := time.Now()
	for i := 0; i < 4; i++ {
		l.fail("a@b.c", now)
	}
	// Failures outside the window start a fresh count.
	if l.fail("a@b.c", now.Add(failWindow+time.Minute)) {
		t.Fatal("stale failures must not count toward the lock")
	}
	// A success clears everything.
	for i := 0; i < 4; i++ {
		l.fail("x@y.z", now)
	}
	l.success("x@y.z")
	if l.fail("x@y.z", now) {
		t.Fatal("success must reset the counter")
	}
	// Accounts are independent.
	if l.locked("other@e.f", now) {
		t.Fatal("unrelated account must not be locked")
	}
}
