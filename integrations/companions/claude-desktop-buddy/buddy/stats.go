package buddy

import (
	"encoding/json"
	"log"
	"os"
	"path/filepath"
)

// statsPath lives under /var/lib so counters survive package upgrades and config resets.
const statsPath = "/var/lib/claude-desktop-buddy/stats.json"

// PersistedStats is the on-disk shape; keys match Desktop's `appr`/`deny` ack fields.
type PersistedStats struct {
	Approved int `json:"appr"`
	Denied   int `json:"deny"`
}

// LoadStats reads counters from disk; a missing or unreadable file yields zeros.
func LoadStats() PersistedStats {
	var s PersistedStats
	data, err := os.ReadFile(statsPath)
	if err != nil {
		if !os.IsNotExist(err) {
			log.Printf("[stats] read %s: %v (starting from zero)", statsPath, err)
		}
		return s
	}
	if err := json.Unmarshal(data, &s); err != nil {
		log.Printf("[stats] parse %s: %v (starting from zero)", statsPath, err)
		return PersistedStats{}
	}
	log.Printf("[stats] loaded approved=%d denied=%d from %s", s.Approved, s.Denied, statsPath)
	return s
}

// SaveStats persists counters; errors are logged and ignored.
func SaveStats(s PersistedStats) {
	if err := os.MkdirAll(filepath.Dir(statsPath), 0o755); err != nil {
		log.Printf("[stats] mkdir %s: %v", filepath.Dir(statsPath), err)
		return
	}
	data, err := json.Marshal(s)
	if err != nil {
		log.Printf("[stats] marshal: %v", err)
		return
	}
	if err := os.WriteFile(statsPath, data, 0o644); err != nil {
		log.Printf("[stats] write %s: %v", statsPath, err)
	}
}
