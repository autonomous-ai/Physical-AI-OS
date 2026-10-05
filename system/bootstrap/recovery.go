package bootstrap

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"time"

	"go.autonomous.ai/os/system/lib/core/system"
)

// recoverPendingUpdate runs before version detection: a power loss can leave the
// live directory absent while the stored version would otherwise suppress OTA.
// The updater owns locking, restoration, health checks and journal removal.
func (b *Bootstrap) recoverPendingUpdate(ctx context.Context) error {
	path := b.pendingUpdatePath
	if path == "" {
		path = "/root/bootstrap/rollback/pending-update.json"
	}
	return recoverPendingUpdateAt(ctx, path, "/opt/hal")
}

// Paths are explicit so recovery detection can be tested without a device.
func recoverPendingUpdateAt(ctx context.Context, journalPath, halPath string) error {
	if _, err := os.Stat(journalPath); errors.Is(err, os.ErrNotExist) {
		// Older updaters moved HAL aside without a journal. Do not let the
		// cached installed version hide the surviving rollback directory.
		if _, err := os.Stat(halPath); err == nil {
			return nil
		} else if !errors.Is(err, os.ErrNotExist) {
			return fmt.Errorf("check live HAL for legacy recovery: %w", err)
		}
		backup, err := os.Stat(filepath.Join(filepath.Dir(journalPath), "hal.previous"))
		if errors.Is(err, os.ErrNotExist) {
			return nil
		} else if err != nil {
			return fmt.Errorf("check legacy HAL rollback backup: %w", err)
		} else if !backup.IsDir() {
			return nil
		}
	} else if err != nil {
		return fmt.Errorf("check pending OTA recovery: %w", err)
	}
	runCtx, cancel := context.WithTimeout(ctx, 10*time.Minute)
	defer cancel()
	out, err := system.Run(runCtx, "software-update", "recover")
	if err != nil {
		return fmt.Errorf("recover interrupted OTA: %w: %s", err, out)
	}
	return nil
}
