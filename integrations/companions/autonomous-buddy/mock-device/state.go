package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sync"

	"github.com/gorilla/websocket"
)

// PairingRecord is what the device persists for each paired buddy (in memory here).
type PairingRecord struct {
	Token       string `json:"token"`
	BuddyID     string `json:"buddy_id"`
	Name        string `json:"name"`
	Fingerprint string `json:"fingerprint"`
	OSVersion   string `json:"os_version"`
}

// State holds the pending pairing code, the single paired buddy, the WebSocket and in-flight requests.
type State struct {
	mu      sync.Mutex
	code    string
	paired  *PairingRecord
	ws      *websocket.Conn
	pending map[string]chan json.RawMessage
}

func NewState() *State {
	return &State{
		pending: make(map[string]chan json.RawMessage),
	}
}

// IssueCode generates and prints a fresh 6-digit pairing code, invalidating any previous one.
func (s *State) IssueCode() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	code := newCode()
	s.code = code
	fmt.Println()
	fmt.Println("┌─────────────────────────────────────────────┐")
	fmt.Printf("│  Pairing code:  %s                      │\n", code)
	fmt.Println("│  Host in buddy: localhost:8765              │")
	fmt.Println("└─────────────────────────────────────────────┘")
	fmt.Println()
	return code
}

// consumeCode atomically validates+invalidates the active code.
func (s *State) consumeCode(submitted string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.code == "" || s.code != submitted {
		return false
	}
	s.code = ""
	return true
}

func (s *State) savePairing(record PairingRecord) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.paired = &record
}

// clearPairing drops the pairing record and closes the WS if open.
func (s *State) clearPairing() {
	s.mu.Lock()
	ws := s.ws
	s.paired = nil
	s.ws = nil
	s.mu.Unlock()
	if ws != nil {
		_ = ws.Close()
	}
}

// lookupByToken returns a copy of the pairing if the token matches.
func (s *State) lookupByToken(token string) *PairingRecord {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.paired == nil || s.paired.Token != token {
		return nil
	}
	c := *s.paired
	return &c
}

func (s *State) pairedSnapshot() *PairingRecord {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.paired == nil {
		return nil
	}
	c := *s.paired
	return &c
}

func (s *State) setWS(ws *websocket.Conn) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ws = ws
}

func (s *State) clearWS() {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.ws = nil
}

func (s *State) currentWS() *websocket.Conn {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ws
}

// registerPending creates a one-shot channel that will receive the response keyed by `id`.
func (s *State) registerPending(id string) chan json.RawMessage {
	ch := make(chan json.RawMessage, 1)
	s.mu.Lock()
	s.pending[id] = ch
	s.mu.Unlock()
	return ch
}

// deliverResponse routes a response from the WS reader loop to the waiting Dispatch caller.
func (s *State) deliverResponse(id string, body json.RawMessage) bool {
	s.mu.Lock()
	ch, ok := s.pending[id]
	if ok {
		delete(s.pending, id)
	}
	s.mu.Unlock()
	if !ok {
		return false
	}
	select {
	case ch <- body:
	default:
	}
	return true
}

func (s *State) cancelPending(id string) {
	s.mu.Lock()
	delete(s.pending, id)
	s.mu.Unlock()
}

func newCode() string {
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
