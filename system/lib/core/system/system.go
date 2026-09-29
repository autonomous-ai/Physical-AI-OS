// Package system provides OS helpers: process execution, file and temp dir utilities.
package system

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
)

// Run runs name with args and returns combined output; ctx cancellation kills the process.
func Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		return out, fmt.Errorf("%s %v: %w", name, args, err)
	}
	return out, nil
}

// ChmodRecursive sets dirMode on directories and fileMode on files under root; symlinks are not followed.
func ChmodRecursive(root string, dirMode, fileMode os.FileMode) error {
	return filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			return os.Chmod(path, dirMode)
		}
		return os.Chmod(path, fileMode)
	})
}

// SpawnBackground starts a fully detached process that outlives the caller; output is discarded.
func SpawnBackground(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	cmd.Stdout = nil
	cmd.Stderr = nil
	cmd.Stdin = nil
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("spawn %s %v: %w", name, args, err)
	}
	_ = cmd.Process.Release()
	return nil
}

// RestartService runs systemctl restart for the given service name.
func RestartService(ctx context.Context, service string) error {
	_, err := Run(ctx, "systemctl", "restart", service)
	return err
}
