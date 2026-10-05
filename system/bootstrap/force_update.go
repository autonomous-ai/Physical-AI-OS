package bootstrap

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
	"sync"
	"time"

	"go.autonomous.ai/os/system/bootstrap/state"
	"go.autonomous.ai/os/system/domain"
)

// inFlight tracks components a force update is installing (key -> struct{}); shared
// between the HTTP handler goroutine and forceUpdate.
var inFlight sync.Map // key: component key, value: struct{}

// UpdatesInFlight returns the components currently being installed, sorted.
func UpdatesInFlight() []string {
	var keys []string
	inFlight.Range(func(k, _ any) bool {
		keys = append(keys, k.(string))
		return true
	})
	sort.Strings(keys)
	return keys
}

// forceUpdate installs the component's published version now, like `software-update <key>`.
// Bypasses the min_version floor but is still gated by componentInstalled.
func (b *Bootstrap) forceUpdate(ctx context.Context, key string) error {
	if err := b.recoverPendingUpdate(ctx); err != nil {
		return err
	}
	if !b.componentInstalled(key) {
		return fmt.Errorf("%s is not installed on this device", key)
	}
	inFlight.Store(key, struct{}{})
	defer inFlight.Delete(key)

	b.announceUpdateStart()
	b.progressLED("ota_progress")

	// applyUpdate ignores the component for keys delegated to software-update.
	if err := b.applyUpdate(ctx, key, domain.OTAComponent{}); err != nil {
		b.showOTAErrorLED()
		return err
	}
	b.progressLED("ota_success")
	time.Sleep(time.Second)
	b.restoreLED()

	// Record what actually landed, not what was requested.
	if installed := b.detectVersion(ctx, key); installed != "" {
		b.state.Components[key] = installed
		if err := state.Save(b.cfg.StateFile, b.state); err != nil {
			slog.Warn("force update: save state failed", "component", "bootstrap", "key", key, "error", err)
		}
	}
	slog.Info("force update applied", "component", "bootstrap", "key", key)
	return nil
}
