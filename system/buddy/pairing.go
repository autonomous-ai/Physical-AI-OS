package buddy

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
	"sync"
	"time"
)

// PairingCodeStore holds the (at most one) active 6-digit code waiting to be claimed.
// Codes expire after TTL and are single-use (consumed on confirm).
type PairingCodeStore struct {
	mu     sync.Mutex
	active *pendingCode
	ttl    time.Duration
}

type pendingCode struct {
	code      string
	expiresAt time.Time
	misses    int
}

func NewPairingCodeStore(ttl time.Duration) *PairingCodeStore {
	return &PairingCodeStore{ttl: ttl}
}

// Issue generates a fresh 6-digit code, invalidating any previously-active one.
func (p *PairingCodeStore) Issue() (string, time.Duration) {
	p.mu.Lock()
	defer p.mu.Unlock()
	code := newSixDigit()
	p.active = &pendingCode{code: code, expiresAt: time.Now().Add(p.ttl)}
	return code, p.ttl
}

// Consume atomically validates and invalidates the active code.
// Returns false if the code doesn't match or has expired.
func (p *PairingCodeStore) Consume(submitted string) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.active == nil {
		return false
	}
	if time.Now().After(p.active.expiresAt) {
		p.active = nil
		return false
	}
	if p.active.code != submitted {
		p.active.misses++
		if p.active.misses >= 5 {
			p.active = nil
		}
		return false
	}
	p.active = nil
	return true
}

// Active reports whether a code is currently valid (for status/debug).
func (p *PairingCodeStore) Active() (string, bool) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.active == nil || time.Now().After(p.active.expiresAt) {
		return "", false
	}
	return p.active.code, true
}

func newSixDigit() string {
	b := make([]byte, 4)
	_, _ = rand.Read(b)
	n := uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3])
	return fmt.Sprintf("%06d", n%900000+100000)
}

func newToken() string {
	b := make([]byte, 24)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func newBuddyID() string {
	b := make([]byte, 4)
	_, _ = rand.Read(b)
	return "buddy-" + hex.EncodeToString(b)
}
