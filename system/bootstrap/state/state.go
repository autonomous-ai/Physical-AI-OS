package state

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"time"
)

// State persists deployed component versions.
type State struct {
	Components map[string]string `json:"components"`
}

// Load reads state from file, or returns empty state if file does not exist.
func Load(path string) (*State, error) {
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return &State{Components: map[string]string{}}, nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read state %s: %w", path, err)
	}
	var s State
	if err := json.Unmarshal(data, &s); err != nil {
		backup, backupErr := moveCorruptFileAside(path)
		if backupErr != nil {
			return nil, fmt.Errorf("parse state %s: %w (preserve corrupt state: %v)", path, err, backupErr)
		}
		slog.Warn("invalid bootstrap state moved aside; starting with empty state", "component", "bootstrap", "path", path, "backup", backup, "error", err)
		return &State{Components: map[string]string{}}, nil
	}
	if s.Components == nil {
		s.Components = map[string]string{}
	}
	return &s, nil
}

// Save atomically writes state (synced temp file, then rename).
func Save(path string, s *State) error {
	if s.Components == nil {
		s.Components = map[string]string{}
	}
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return fmt.Errorf("marshal state: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return fmt.Errorf("create state dir: %w", err)
	}

	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return fmt.Errorf("create temporary state file: %w", err)
	}
	tmpPath := tmp.Name()
	defer os.Remove(tmpPath)

	if err := tmp.Chmod(0600); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("set temporary state permissions: %w", err)
	}
	if _, err := tmp.Write(data); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temporary state: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("sync temporary state: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temporary state: %w", err)
	}
	if err := os.Rename(tmpPath, path); err != nil {
		return fmt.Errorf("replace state atomically: %w", err)
	}
	if err := syncDir(dir); err != nil {
		return fmt.Errorf("sync state directory: %w", err)
	}
	return nil
}

func moveCorruptFileAside(path string) (string, error) {
	backup := fmt.Sprintf("%s.corrupt-%s", path, time.Now().UTC().Format("20060102T150405.000000000Z"))
	if err := os.Rename(path, backup); err != nil {
		return "", fmt.Errorf("rename %s to %s: %w", path, backup, err)
	}
	if err := syncDir(filepath.Dir(path)); err != nil {
		return "", err
	}
	return backup, nil
}

func syncDir(path string) error {
	dir, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("open directory %s: %w", path, err)
	}
	defer dir.Close()
	if err := dir.Sync(); err != nil {
		return fmt.Errorf("sync directory %s: %w", path, err)
	}
	return nil
}
