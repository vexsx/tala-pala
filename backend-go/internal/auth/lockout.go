package auth

import (
	"sync"
	"time"
)

// Per-ACCOUNT login throttling, independent of client IP: the IP limiter
// alone cannot stop a distributed credential-stuffing run against one email.
// After maxLoginFails consecutive failures inside failWindow the account is
// locked for lockDuration; any successful login resets the counter.
//
// State is in-memory (single API replica by deployment design); a restart
// clears it, which only ever errs toward letting a legitimate user in.
const (
	maxLoginFails = 5
	failWindow    = 15 * time.Minute
	lockDuration  = 15 * time.Minute
	// lockoutMaxEntries bounds memory against an attacker cycling random
	// emails; oldest-window entries are dropped opportunistically.
	lockoutMaxEntries = 10000
)

type lockoutEntry struct {
	fails       int
	firstFailAt time.Time
	lockedUntil time.Time
}

type loginLockout struct {
	mu      sync.Mutex
	entries map[string]*lockoutEntry
}

// locked reports whether the account is currently locked out.
func (l *loginLockout) locked(email string, now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	e, ok := l.entries[email]
	return ok && now.Before(e.lockedUntil)
}

// fail records a failed attempt; returns true when this attempt locked the
// account.
func (l *loginLockout) fail(email string, now time.Time) bool {
	l.mu.Lock()
	defer l.mu.Unlock()
	if l.entries == nil {
		l.entries = make(map[string]*lockoutEntry)
	}
	if len(l.entries) >= lockoutMaxEntries {
		for k, e := range l.entries {
			if now.Sub(e.firstFailAt) > failWindow && now.After(e.lockedUntil) {
				delete(l.entries, k)
			}
		}
	}
	e, ok := l.entries[email]
	if !ok || now.Sub(e.firstFailAt) > failWindow {
		l.entries[email] = &lockoutEntry{fails: 1, firstFailAt: now}
		return false
	}
	e.fails++
	if e.fails >= maxLoginFails {
		e.lockedUntil = now.Add(lockDuration)
		e.fails = 0
		e.firstFailAt = now
		return true
	}
	return false
}

// success clears the account's failure state.
func (l *loginLockout) success(email string) {
	l.mu.Lock()
	defer l.mu.Unlock()
	delete(l.entries, email)
}
