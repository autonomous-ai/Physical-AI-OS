package openclaw

import (
	"context"
	"log/slog"
	"regexp"
	"strconv"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/core/system"
	"go.autonomous.ai/os/system/lib/runtimereg"
	"go.autonomous.ai/os/system/lib/versioncache"
)

// Expose the cached version to system/device (backend ping payload) through the neutral registry — a direct import would cycle via statusled → device.
func init() {
	runtimereg.RegisterVersion(domain.AgentRuntimeOpenClaw, GetOpenClawVersion)
}

// openclawVersionProbeTimeout caps a single `openclaw --version` probe.
const openclawVersionProbeTimeout = 20 * time.Second

// openclawVersionProbeRetries bounds how long PopulateOpenClawVersion keeps retrying a killed/empty probe.
const openclawVersionProbeRetries = 6

// openclawVersionProbeBackoff is the wait between failed probe attempts.
const openclawVersionProbeBackoff = 10 * time.Second

// openclawSemverRe captures the first semver-like token in `openclaw --version` output (e.g. "OpenClaw 2026.5.27 (27ae826)" → "2026.5.27").
var openclawSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// openclawSemverNumRe pulls the numeric year.minor.patch out of an already normalized semver string (the cached version) for RuntimeInfo comparisons.
var openclawSemverNumRe = regexp.MustCompile(`^(\d+)\.(\d+)\.(\d+)`)

// openClawVersion caches the normalized OpenClaw binary version (e.g. "2026.5.27").
var openClawVersion = versioncache.New("openclaw", "openclaw-probe", probeOpenClawVersion)

// GetOpenClawVersion returns the cached OpenClaw binary version (e.g. "2026.5.27").
func GetOpenClawVersion() string {
	return openClawVersion.Get()
}

// PopulateOpenClawVersion shells out to `openclaw --version`, normalizes the semver, and stores it in openClawVersion.
func PopulateOpenClawVersion() {
	openClawVersion.Populate(openclawVersionProbeRetries, openclawVersionProbeBackoff)
}

// probeOpenClawVersion runs `openclaw --version` once; ok=false means retry.
func probeOpenClawVersion(ctx context.Context) (version string, ok bool) {
	ctx, cancel := context.WithTimeout(ctx, openclawVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, "openclaw", "--version")
	if err != nil {
		slog.Warn("read openclaw version failed (expected if not on openclaw backend)", "component", "openclaw-probe", "error", err)
		return "", false
	}
	line := strings.TrimSpace(strings.TrimRight(string(out), "\r\n"))
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	loc := openclawSemverRe.FindStringSubmatch(line)
	if len(loc) <= 1 {
		return "", false
	}
	return loc[1], true
}

// RuntimeInfo carries the installed openclaw runtime's parsed version.
type RuntimeInfo struct {
	Year, Minor, Patch int
	Detected           bool
}

// AtLeast reports whether the detected runtime is ≥ year.minor.0.
func (r RuntimeInfo) AtLeast(year, minor int) bool {
	if !r.Detected {
		return true
	}
	if r.Year != year {
		return r.Year > year
	}
	return r.Minor >= minor
}

// currentOpenclawRuntime parses the cached OpenClaw version (the same value the MQTT `info` message reports) into a RuntimeInfo — no extra shell-out.
func currentOpenclawRuntime() RuntimeInfo {
	m := openclawSemverNumRe.FindStringSubmatch(GetOpenClawVersion())
	if len(m) < 4 {
		return RuntimeInfo{}
	}
	year, _ := strconv.Atoi(m[1])
	minor, _ := strconv.Atoi(m[2])
	patch, _ := strconv.Atoi(m[3])
	return RuntimeInfo{Year: year, Minor: minor, Patch: patch, Detected: true}
}
