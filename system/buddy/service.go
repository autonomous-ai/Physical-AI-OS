package buddy

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/gorilla/websocket"
)

// Service coordinates buddy pairing, persistence, connection and dispatch.
type Service struct {
	statusMu      sync.Mutex
	instanceID    string
	revision      uint64
	statusChanges chan struct{}
	store         *Store
	pairing       *PairingCodeStore
	registry      *Registry
	dispatcher    *Dispatcher
	agentMu       sync.Mutex
	agentSeq      map[string]uint64
}

// ProvideService wires the buddy subsystem and loads any persisted pairing.
func ProvideService() (*Service, error) {
	store := NewStore(BuddiesFilePath)
	if err := store.Load(); err != nil {
		return nil, fmt.Errorf("load buddy store: %w", err)
	}
	pairing := NewPairingCodeStore(60 * time.Second)
	registry := NewRegistry()
	dispatcher := NewDispatcher(registry)
	return &Service{
		instanceID:    NewCommandID(),
		statusChanges: make(chan struct{}, 1),
		store:         store,
		pairing:       pairing,
		registry:      registry,
		dispatcher:    dispatcher,
	}, nil
}

// IssuePairingCode generates a fresh 6-digit code valid for 60s, invalidating any prior code.
func (s *Service) IssuePairingCode() (string, time.Duration) {
	return s.pairing.Issue()
}

// ConfirmPairing validates a code and persists a new pairing record (token + buddy ID).
func (s *Service) ConfirmPairing(name, fingerprint, osVersion, code string) (*PairingRecord, error) {
	s.statusMu.Lock()
	defer s.statusMu.Unlock()
	if !s.pairing.Consume(code) {
		return nil, fmt.Errorf("invalid or expired code")
	}
	record := &PairingRecord{
		BuddyID:     newBuddyID(),
		Token:       newToken(),
		Name:        name,
		Fingerprint: fingerprint,
		OSVersion:   osVersion,
		PairedAt:    time.Now().UTC(),
	}
	if err := s.store.Set(record); err != nil {
		return nil, fmt.Errorf("save pairing: %w", err)
	}
	// A replacement pairing must not inherit the previous buddy's connection.
	if conn := s.registry.Conn(); conn != nil {
		_ = conn.Close()
	}
	s.registry.Clear()
	s.statusChangedLocked()
	slog.Info("buddy paired", "component", "buddy", "id", record.BuddyID, "name", name, "os", osVersion)
	return record, nil
}

// Unpair drops the current buddy: closes the WS, clears the registry, removes the on-disk record.
func (s *Service) Unpair() error {
	s.statusMu.Lock()
	defer s.statusMu.Unlock()
	if err := s.store.Clear(); err != nil {
		return fmt.Errorf("clear store: %w", err)
	}
	if conn := s.registry.Conn(); conn != nil {
		_ = conn.Close()
	}
	s.registry.Clear()
	s.statusChangedLocked()
	slog.Info("buddy unpaired", "component", "buddy")
	return nil
}

// Paired returns the current paired record (snapshot) or nil.
func (s *Service) Paired() *PairingRecord {
	return s.store.Get()
}

// ValidateToken returns the record matching the bearer token, or nil.
func (s *Service) ValidateToken(token string) *PairingRecord {
	return s.store.ByToken(token)
}

// RegisterConnection installs the buddy's WS for command dispatch.
func (s *Service) RegisterConnection(conn *websocket.Conn) {
	s.statusMu.Lock()
	defer s.statusMu.Unlock()
	s.registry.Set(conn)
	s.statusChangedLocked()
}

// RegisterAuthenticatedConnection rechecks the token under the state lock so a
// revoke or replacement during the HTTP upgrade cannot install a stale socket.
func (s *Service) RegisterAuthenticatedConnection(conn *websocket.Conn, token string) bool {
	s.statusMu.Lock()
	defer s.statusMu.Unlock()
	if s.store.ByToken(token) == nil {
		return false
	}
	s.registry.Set(conn)
	s.statusChangedLocked()
	return true
}

// Connected reports whether a buddy is currently online.
func (s *Service) Connected() bool {
	return s.registry.Conn() != nil
}

// Dispatch sends a command to the connected buddy and waits for its response.
func (s *Service) Dispatch(ctx context.Context, cmd Command) (json.RawMessage, error) {
	return s.dispatcher.Dispatch(ctx, cmd)
}

// Greet sends a best-effort `ping` right after the buddy connects so its Activity
// window confirms reachability. Blocks on Dispatch; call from a goroutine.
func (s *Service) Greet(buddyID string) {
	deviceType := strings.ToLower(os.Getenv("DEVICE_TYPE"))
	if deviceType == "" {
		deviceType = "device"
	}
	cmd := Command{
		ID:        NewCommandID(),
		Action:    "ping",
		Params:    map[string]any{"from": deviceType, "hello": true},
		TimeoutMs: 5000,
		IssuedAt:  time.Now().UTC().Format(time.RFC3339),
		IssuedBy:  deviceType + ":hello",
	}
	ctx, cancel := context.WithTimeout(context.Background(), 7*time.Second)
	defer cancel()
	if _, err := s.dispatcher.Dispatch(ctx, cmd); err != nil {
		slog.Warn("buddy hello ping failed", "component", "buddy", "id", buddyID, "error", err)
		return
	}
	slog.Info("buddy hello ping ok", "component", "buddy", "id", buddyID)
}
