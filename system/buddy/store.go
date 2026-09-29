package buddy

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// BuddiesFilePath is the on-disk JSON file for the (single) paired buddy.
const BuddiesFilePath = "config/buddies.json"

// PairingRecord is what the device persists for each paired buddy.
type PairingRecord struct {
	BuddyID     string    `json:"buddy_id"`
	Token       string    `json:"token"`
	Name        string    `json:"name"`
	Fingerprint string    `json:"fingerprint"`
	OSVersion   string    `json:"os_version"`
	PairedAt    time.Time `json:"paired_at"`
}

// storeFile is the on-disk shape: {"records":[...]}.
type storeFile struct {
	Records []PairingRecord `json:"records"`
}

// Store persists paired buddies to disk. Safe for concurrent use.
type Store struct {
	mu     sync.RWMutex
	path   string
	record *PairingRecord
}

func NewStore(path string) *Store {
	return &Store{path: path}
}

// Load reads the store file if it exists. Missing file is not an error.
func (s *Store) Load() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	data, err := os.ReadFile(s.path)
	if err != nil {
		if errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("read %s: %w", s.path, err)
	}
	var f storeFile
	if err := json.Unmarshal(data, &f); err != nil {
		return fmt.Errorf("parse %s: %w", s.path, err)
	}
	if len(f.Records) > 0 {
		rec := f.Records[0]
		s.record = &rec
	}
	return nil
}

// Set replaces the (single) paired buddy and persists. MVP is 1↔1.
func (s *Store) Set(r *PairingRecord) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	previous := s.record
	s.record = r
	if err := s.writeLocked(); err != nil {
		s.record = previous
		return err
	}
	return nil
}

// Clear removes the pairing and persists an empty store.
func (s *Store) Clear() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	previous := s.record
	s.record = nil
	if err := s.writeLocked(); err != nil {
		s.record = previous
		return err
	}
	return nil
}

// Get returns a snapshot of the paired record, or nil if none.
func (s *Store) Get() *PairingRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.record == nil {
		return nil
	}
	c := *s.record
	return &c
}

// ByToken returns the paired record whose token matches, or nil.
func (s *Store) ByToken(token string) *PairingRecord {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if s.record == nil || s.record.Token != token {
		return nil
	}
	c := *s.record
	return &c
}

func (s *Store) writeLocked() error {
	var f storeFile
	if s.record != nil {
		f.Records = []PairingRecord{*s.record}
	}
	data, err := json.MarshalIndent(f, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal store: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(s.path), 0o755); err != nil {
		return fmt.Errorf("create store dir: %w", err)
	}
	// Replace atomically so a failed write cannot corrupt the prior pairing.
	file, err := os.CreateTemp(filepath.Dir(s.path), ".buddies-*")
	if err != nil {
		return fmt.Errorf("create pairing temp file: %w", err)
	}
	defer os.Remove(file.Name())
	if _, err := file.Write(data); err != nil {
		_ = file.Close()
		return fmt.Errorf("write pairing temp file: %w", err)
	}
	if err := file.Close(); err != nil {
		return fmt.Errorf("close pairing temp file: %w", err)
	}
	if err := os.Rename(file.Name(), s.path); err != nil {
		return fmt.Errorf("replace %s: %w", s.path, err)
	}
	return nil
}
