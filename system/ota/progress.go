package ota

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"syscall"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

const progressDirectory = "/root/bootstrap/progress"
const maxProgressSize = 16 << 10

// Progress is an atomically persisted updater snapshot. Byte counts describe
// only the current download, never an estimated percentage of the entire update.
type Progress struct {
	Target          string `json:"target"`
	RunID           string `json:"run_id"`
	Phase           string `json:"phase"`
	DownloadedBytes int64  `json:"downloaded_bytes"`
	TotalBytes      int64  `json:"total_bytes"`
	UpdatedAt       int64  `json:"updated_at"`
	PID             int    `json:"pid"`
	BootID          string `json:"boot_id,omitempty"`
	Message         string `json:"message,omitempty"`
	ActivityAt      int64  `json:"activity_at,omitempty"`
}

func (p Progress) active() bool {
	switch p.Phase {
	case "preparing", "downloading", "verifying", "installing", "restarting", "checking", "rolling_back":
		return true
	}
	return false
}

func (p Progress) valid(target string) bool {
	if p.Target != target || p.RunID == "" || len(p.RunID) > 256 || p.UpdatedAt <= 0 || p.PID <= 0 || p.DownloadedBytes < 0 || p.TotalBytes < 0 {
		return false
	}
	return p.active() || p.Phase == "completed" || p.Phase == "failed" || p.Phase == "interrupted"
}

// UpdateState remains readable while bootstrap restarts. BootstrapAvailable
// distinguishes an empty worker list from a worker that could not be reached.
type UpdateState struct {
	Updating           []string            `json:"updating"`
	Progress           map[string]Progress `json:"progress"`
	BootstrapAvailable bool                `json:"bootstrap_available"`
}

func processAlive(pid int) bool {
	err := syscall.Kill(pid, 0)
	// Permission denied means the process exists. Treat unexpected errors
	// conservatively so they cannot turn a running installer into a failure.
	return !errors.Is(err, syscall.ESRCH)
}

func readProgress(dir string, cfg *config.Config, now time.Time, alive func(int) bool, bootID string) map[string]Progress {
	result := make(map[string]Progress)
	for target := range allowedTargets {
		path := filepath.Join(dir, target+".json")
		// Ignore symlinks, directories and partial/oversized snapshots. Writers use
		// rename, so a reader sees either the previous snapshot or the next one.
		info, err := os.Lstat(path)
		if err != nil || !info.Mode().IsRegular() || info.Size() > maxProgressSize {
			continue
		}
		f, err := os.Open(path)
		if err != nil {
			continue
		}
		raw, err := io.ReadAll(io.LimitReader(f, maxProgressSize+1))
		_ = f.Close()
		if err != nil || len(raw) > maxProgressSize {
			continue
		}
		var p Progress
		if json.Unmarshal(raw, &p) != nil || !p.valid(target) {
			continue
		}
		differentBoot := p.BootID != "" && bootID != "" && p.BootID != bootID
		if p.active() && now.Unix()-p.UpdatedAt > 30 && (differentBoot || !alive(p.PID)) {
			p.Phase = "interrupted"
			p.Message = "Updater stopped before reporting a result"
		}
		result[target] = p
	}
	if p, ok := result[ResolveTarget(cfg, AgentTarget)]; ok {
		result[AgentTarget] = p
	}
	return result
}

// UpdateStatus combines the worker's existing updating list with persisted
// progress, including when the worker is unavailable during its own update.
func UpdateStatus(ctx context.Context, cfg *config.Config) UpdateState {
	updating, err := Updating(ctx, cfg)
	bootID, _ := os.ReadFile("/proc/sys/kernel/random/boot_id")
	return combineProgress(updating, err == nil, readProgress(progressDirectory, cfg, time.Now(), processAlive, strings.TrimSpace(string(bootID))))
}

func combineProgress(updating []string, available bool, progress map[string]Progress) UpdateState {
	targets := make(map[string]bool)
	for _, target := range updating {
		targets[target] = true
	}
	for target, p := range progress {
		if p.active() {
			targets[target] = true
		}
	}
	list := make([]string, 0, len(targets))
	for target := range targets {
		list = append(list, target)
	}
	sort.Strings(list)
	return UpdateState{Updating: list, Progress: progress, BootstrapAvailable: available}
}
