package device

import (
	"bufio"
	"bytes"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"go.autonomous.ai/os/system/server/config"
)

// zoneInfoDir is the system tzdata tree; a zone is valid iff <zoneInfoDir>/<name> is a file.
const zoneInfoDir = "/usr/share/zoneinfo"

// timezoneFile is read fresh by HAL's clock helpers; timedatectl does not write
// it, so we always do.
const timezoneFile = "/etc/timezone"

// localtimeFile is the glibc local wall-clock symlink, set directly (timedatectl may be absent).
const localtimeFile = "/etc/localtime"

// commonTimezones is the fallback picker list when no system source yields zones.
var commonTimezones = []string{
	"UTC",
	"Asia/Ho_Chi_Minh",
	"Asia/Bangkok",
	"Asia/Singapore",
	"Asia/Shanghai",
	"Asia/Tokyo",
	"Asia/Seoul",
	"Asia/Kolkata",
	"Asia/Dubai",
	"Europe/London",
	"Europe/Paris",
	"Europe/Berlin",
	"Europe/Moscow",
	"America/New_York",
	"America/Chicago",
	"America/Denver",
	"America/Los_Angeles",
	"America/Sao_Paulo",
	"Australia/Sydney",
	"Pacific/Auckland",
}

// isValidTimezone reports whether name is an IANA zone in the tzdata tree.
// Rejects empty, absolute and traversal inputs so name cannot escape zoneInfoDir.
func isValidTimezone(name string) bool {
	name = strings.TrimSpace(name)
	if name == "" || strings.HasPrefix(name, "/") || strings.Contains(name, "..") {
		return false
	}
	info, err := os.Stat(filepath.Join(zoneInfoDir, name))
	return err == nil && info.Mode().IsRegular()
}

// currentSystemTimezone returns the active zone from /etc/timezone, else the
// /etc/localtime symlink; empty when neither resolves.
func currentSystemTimezone() string {
	if data, err := os.ReadFile(timezoneFile); err == nil {
		if tz := strings.TrimSpace(string(data)); tz != "" {
			return tz
		}
	}
	if target, err := os.Readlink(localtimeFile); err == nil {
		if idx := strings.Index(target, "zoneinfo/"); idx != -1 {
			return target[idx+len("zoneinfo/"):]
		}
	}
	return ""
}

// listSystemTimezones returns selectable zones from timedatectl, else the tzdata
// tree, else commonTimezones. Always non-empty.
func listSystemTimezones() []string {
	if out, err := exec.Command("timedatectl", "list-timezones").Output(); err == nil {
		zones := parseLines(out)
		if len(zones) > 0 {
			return zones
		}
	}
	if zones := walkZoneInfo(); len(zones) > 0 {
		return zones
	}
	return commonTimezones
}

// parseLines splits command output into trimmed, non-empty lines.
func parseLines(out []byte) []string {
	var lines []string
	scanner := bufio.NewScanner(bytes.NewReader(out))
	for scanner.Scan() {
		if l := strings.TrimSpace(scanner.Text()); l != "" {
			lines = append(lines, l)
		}
	}
	return lines
}

// walkZoneInfo returns Area/Location zones plus UTC from the tzdata tree,
// skipping posix/right/ duplicates and legacy aliases.
func walkZoneInfo() []string {
	var zones []string
	_ = filepath.Walk(zoneInfoDir, func(path string, info os.FileInfo, err error) error {
		if err != nil || info.IsDir() || !info.Mode().IsRegular() {
			return nil
		}
		rel, relErr := filepath.Rel(zoneInfoDir, path)
		if relErr != nil {
			return nil
		}
		if strings.HasPrefix(rel, "posix/") || strings.HasPrefix(rel, "right/") {
			return nil
		}
		if rel == "UTC" || strings.Contains(rel, "/") {
			zones = append(zones, rel)
		}
		return nil
	})
	sort.Strings(zones)
	return zones
}

// applySystemTimezone sets /etc/localtime and /etc/timezone, then runs
// timedatectl best-effort. Validate tz with isValidTimezone first.
func applySystemTimezone(tz string) error {
	zonePath := filepath.Join(zoneInfoDir, tz)
	_ = os.Remove(localtimeFile)
	if err := os.Symlink(zonePath, localtimeFile); err != nil {
		return fmt.Errorf("link %s: %w", localtimeFile, err)
	}
	if err := os.WriteFile(timezoneFile, []byte(tz+"\n"), 0644); err != nil {
		return fmt.Errorf("write %s: %w", timezoneFile, err)
	}
	// Best-effort: failure is non-fatal, the writes above already moved the clock.
	if out, err := exec.Command("timedatectl", "set-timezone", tz).CombinedOutput(); err != nil {
		slog.Warn("timedatectl set-timezone failed (non-fatal)", "component", "device", "tz", tz, "error", err, "output", strings.TrimSpace(string(out)))
	}
	return nil
}

// GetTimezone returns the current zone (system, else config) and the selectable list.
func (s *Service) GetTimezone() (current string, zones []string) {
	current = currentSystemTimezone()
	if current == "" {
		current = s.config.Timezone
	}
	return current, listSystemTimezones()
}

// CurrentTimezone returns the active IANA zone (system, else config) without the list.
func (s *Service) CurrentTimezone() string {
	if tz := currentSystemTimezone(); tz != "" {
		return tz
	}
	return s.config.Timezone
}

// SetTimezone validates, applies and persists an IANA zone; no HAL restart needed.
// Example: SetTimezone("Asia/Ho_Chi_Minh")
func (s *Service) SetTimezone(tz string) error {
	tz = strings.TrimSpace(tz)
	if !isValidTimezone(tz) {
		return fmt.Errorf("unknown timezone %q", tz)
	}
	if err := applySystemTimezone(tz); err != nil {
		return fmt.Errorf("apply timezone: %w", err)
	}
	// Go caches time.Local at startup; update it so daily buckets switch zone
	// without an os-server restart.
	if loc, err := time.LoadLocation(tz); err == nil {
		time.Local = loc
	} else {
		slog.Warn("load location for in-process time.Local failed", "component", "device", "tz", tz, "error", err)
	}
	if err := s.config.WithLockSave(func(c *config.Config) {
		c.Timezone = tz
	}); err != nil {
		return fmt.Errorf("save config: %w", err)
	}
	slog.Info("timezone updated", "component", "device", "tz", tz)
	return nil
}
