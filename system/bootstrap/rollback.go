package bootstrap

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
)

// rollbackVersions reads a fresh snapshot without mutating the shared startup
// configuration. Manual rollback writes these rules while bootstrap stays alive.
func (b *Bootstrap) rollbackVersions() (map[string]string, error) {
	path := b.rollbackConfigPath
	if path == "" {
		path = "/root/config/bootstrap.json"
	}
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		if b.cfg != nil {
			return b.cfg.RollbackVersions, nil
		}
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read rollback rules: %w", err)
	}
	var snapshot struct {
		Versions map[string]string `json:"rollback_versions"`
	}
	if err := json.Unmarshal(data, &snapshot); err != nil {
		return nil, fmt.Errorf("decode rollback rules: %w", err)
	}
	return snapshot.Versions, nil
}
