// Package externalhistory journals turns handled outside the main agent.
package externalhistory

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"
)

const (
	StateWaiting   = "waiting"
	StatePending   = "pending"
	StateSending   = "sending"
	StateUncertain = "uncertain"
	StateDone      = "done"
	MaxRecords     = 1024
	MaxInputBytes  = 16 * 1024
	MaxOutputBytes = 64 * 1024
	maxFileBytes   = 512 * 1024
	retention      = 30 * 24 * time.Hour
)

type Record struct {
	Source      string    `json:"source"`
	OriginRunID string    `json:"origin_run_id"`
	MachineID   string    `json:"machine_id"`
	AgentID     string    `json:"agent_id"`
	AgentName   string    `json:"agent_name"`
	Input       string    `json:"input"`
	Output      string    `json:"output"`
	SyncRunID   string    `json:"sync_run_id"`
	State       string    `json:"state"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}

type Store struct {
	mu      sync.Mutex
	dir     string
	records map[string]Record
}

func syncID(source, origin string) string {
	sum := sha256.Sum256([]byte(source + "\x00" + origin))
	return "device-chat-context-" + hex.EncodeToString(sum[:])
}

func validate(r Record) error {
	if strings.TrimSpace(r.Source) == "" || strings.TrimSpace(r.OriginRunID) == "" || strings.ContainsRune(r.Source, 0) || strings.ContainsRune(r.OriginRunID, 0) {
		return fmt.Errorf("source and origin run ID must be nonempty and contain no NUL")
	}
	if len(r.Input) > MaxInputBytes || len(r.Output) > MaxOutputBytes {
		return fmt.Errorf("external turn exceeds input or output size limit")
	}
	if len(r.Source) > 1024 || len(r.OriginRunID) > 1024 || len(r.MachineID) > 1024 || len(r.AgentID) > 1024 || len(r.AgentName) > 4096 {
		return fmt.Errorf("external turn metadata exceeds size limit")
	}
	return nil
}

func New(dir string) (*Store, error) {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return nil, fmt.Errorf("create external history: %w", err)
	}
	s := &Store{dir: dir, records: make(map[string]Record)}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return nil, fmt.Errorf("read external history: %w", err)
	}
	for _, entry := range entries {
		if !strings.HasSuffix(entry.Name(), ".json") {
			continue
		}
		if entry.IsDir() || len(s.records) >= MaxRecords {
			return nil, fmt.Errorf("invalid or oversized external history directory")
		}
		f, err := os.Open(filepath.Join(dir, entry.Name()))
		if err != nil {
			return nil, fmt.Errorf("open external turn: %w", err)
		}
		data, readErr := io.ReadAll(io.LimitReader(f, maxFileBytes+1))
		f.Close()
		if readErr != nil {
			return nil, fmt.Errorf("read external turn: %w", readErr)
		}
		if len(data) > maxFileBytes {
			return nil, fmt.Errorf("external turn file exceeds size limit")
		}
		var r Record
		if err := json.Unmarshal(data, &r); err != nil {
			return nil, fmt.Errorf("decode external turn: %w", err)
		}
		if err := validate(r); err != nil {
			return nil, err
		}
		if r.SyncRunID != syncID(r.Source, r.OriginRunID) || entry.Name() != r.SyncRunID+".json" || r.CreatedAt.IsZero() || r.UpdatedAt.IsZero() {
			return nil, fmt.Errorf("invalid external turn identity or timestamps")
		}
		switch r.State {
		case StateWaiting, StatePending, StateSending, StateUncertain, StateDone:
		default:
			return nil, fmt.Errorf("invalid external turn state %q", r.State)
		}
		s.records[r.SyncRunID] = r
	}
	for _, r := range s.records {
		if r.State == StateSending {
			r.State = StateUncertain
			r.UpdatedAt = time.Now().UTC()
			if err := s.persist(r); err != nil {
				return nil, err
			}
		}
	}
	return s, nil
}

// persist commits to disk before updating memory; a failed write never advances state.
func (s *Store) persist(r Record) error {
	data, err := json.Marshal(r)
	if err != nil {
		return fmt.Errorf("encode external turn: %w", err)
	}
	if len(data) > maxFileBytes {
		return fmt.Errorf("external turn file exceeds size limit")
	}
	f, err := os.CreateTemp(s.dir, ".external-turn-*")
	if err != nil {
		return fmt.Errorf("create external turn file: %w", err)
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if _, err = f.Write(data); err != nil {
		f.Close()
		return fmt.Errorf("write external turn: %w", err)
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return fmt.Errorf("sync external turn: %w", err)
	}
	if err = f.Close(); err != nil {
		return fmt.Errorf("close external turn: %w", err)
	}
	if err = os.Rename(tmp, filepath.Join(s.dir, r.SyncRunID+".json")); err != nil {
		return fmt.Errorf("replace external turn: %w", err)
	}
	if err = s.syncDir(); err != nil {
		return err
	}
	s.records[r.SyncRunID] = r
	return nil
}

func (s *Store) syncDir() error {
	d, err := os.Open(s.dir)
	if err != nil {
		return fmt.Errorf("open external history directory: %w", err)
	}
	defer d.Close()
	if err = d.Sync(); err != nil {
		return fmt.Errorf("sync external history directory: %w", err)
	}
	return nil
}

func (s *Store) Begin(r Record) (Record, error) {
	return s.record(r, false)
}

// RecordCompleted atomically journals an already-answered exchange.
func (s *Store) RecordCompleted(r Record) (Record, error) {
	return s.record(r, true)
}

func (s *Store) record(r Record, completedTurn bool) (Record, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if err := validate(r); err != nil {
		return Record{}, err
	}
	r.SyncRunID = syncID(r.Source, r.OriginRunID)
	if old, ok := s.records[r.SyncRunID]; ok {
		if old.Source != r.Source || old.OriginRunID != r.OriginRunID || old.Input != r.Input || old.MachineID != r.MachineID || old.AgentID != r.AgentID || old.AgentName != r.AgentName || (completedTurn && old.Output != r.Output) {
			return Record{}, fmt.Errorf("external turn conflicts with existing input or agent")
		}
		return old, nil
	}
	now := time.Now().UTC()
	// Completed turns are deduplicated for up to 30 days; unfinished turns are never evicted.
	var completed []Record
	for id, old := range s.records {
		if old.State != StateDone {
			continue
		}
		if now.Sub(old.UpdatedAt) > retention {
			if err := s.remove(id); err != nil {
				return Record{}, err
			}
		} else {
			completed = append(completed, old)
		}
	}
	sort.Slice(completed, func(i, j int) bool {
		if completed[i].UpdatedAt.Equal(completed[j].UpdatedAt) {
			return completed[i].SyncRunID < completed[j].SyncRunID
		}
		return completed[i].UpdatedAt.Before(completed[j].UpdatedAt)
	})
	for _, old := range completed {
		if len(s.records) < MaxRecords {
			break
		}
		if err := s.remove(old.SyncRunID); err != nil {
			return Record{}, err
		}
	}
	if len(s.records) >= MaxRecords {
		return Record{}, fmt.Errorf("external history is full")
	}
	if completedTurn {
		r.State = StatePending
	} else {
		r.Output = ""
		r.State = StateWaiting
	}
	r.CreatedAt = now
	r.UpdatedAt = now
	if err := s.persist(r); err != nil {
		return Record{}, err
	}
	return r, nil
}

// remove requires the store mutex and commits deletion before changing memory.
func (s *Store) remove(id string) error {
	if err := os.Remove(filepath.Join(s.dir, id+".json")); err != nil {
		return fmt.Errorf("prune external turn: %w", err)
	}
	if err := s.syncDir(); err != nil {
		return err
	}
	delete(s.records, id)
	return nil
}

func (s *Store) Complete(source, originRunID, output string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.records[syncID(source, originRunID)]
	if !ok {
		return fmt.Errorf("external turn not found")
	}
	if len(output) > MaxOutputBytes {
		return fmt.Errorf("external turn output exceeds size limit")
	}
	if r.State != StateWaiting {
		if r.Output != output {
			return fmt.Errorf("external turn conflicts with existing output")
		}
		return nil
	}
	r.Output = output
	r.State = StatePending
	r.UpdatedAt = time.Now().UTC()
	return s.persist(r)
}

func (s *Store) MarkSending(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.records[id]
	if !ok || r.State != StatePending {
		return fmt.Errorf("external turn is not pending")
	}
	r.State = StateSending
	r.UpdatedAt = time.Now().UTC()
	return s.persist(r)
}

// MarkUncertain prevents retry after a transport error that may have followed acceptance.
func (s *Store) MarkUncertain(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.records[id]
	if !ok {
		return fmt.Errorf("external turn not found")
	}
	if r.State == StateDone || r.State == StateUncertain {
		return nil
	}
	if r.State != StateSending {
		return fmt.Errorf("external turn has not been sent")
	}
	r.State = StateUncertain
	r.UpdatedAt = time.Now().UTC()
	return s.persist(r)
}

func (s *Store) Acknowledge(id string) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.records[id]
	if !ok {
		return fmt.Errorf("external turn not found")
	}
	if r.State == StateDone {
		return nil
	}
	if r.State != StateSending && r.State != StateUncertain {
		return fmt.Errorf("external turn has not been sent")
	}
	r.State = StateDone
	r.UpdatedAt = time.Now().UTC()
	return s.persist(r)
}

func (s *Store) Lookup(id string) (Record, bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.records[id]
	return r, ok
}

func (s *Store) Records() []Record { return s.list(false) }
func (s *Store) Pending() []Record { return s.list(true) }
func (s *Store) list(pendingOnly bool) []Record {
	s.mu.Lock()
	defer s.mu.Unlock()
	records := make([]Record, 0, len(s.records))
	for _, r := range s.records {
		if !pendingOnly || r.State == StatePending {
			records = append(records, r)
		}
	}
	sort.Slice(records, func(i, j int) bool {
		if records[i].CreatedAt.Equal(records[j].CreatedAt) {
			return records[i].SyncRunID < records[j].SyncRunID
		}
		return records[i].CreatedAt.Before(records[j].CreatedAt)
	})
	return records
}
