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

// Expose the cached version to system/device (backend ping payload) through
// the neutral registry — a direct import would cycle via statusled → device.
func init() {
	runtimereg.RegisterVersion(domain.AgentRuntimeOpenClaw, GetOpenClawVersion)
}

// openclawVersionProbeTimeout caps a single `openclaw --version` probe. OpenClaw
// is a Node CLI whose cold-start (node bootstrap + bundle load) can exceed a few
// seconds when the box is busy right after boot — a 5s cap killed the probe and
// left the version blank for the whole process lifetime. Kept generous so a slow
// cold start still resolves.
const openclawVersionProbeTimeout = 20 * time.Second

// openclawVersionProbeRetries bounds how long PopulateOpenClawVersion keeps
// retrying a killed/empty probe. The one-shot probe used to give up after the
// first boot-time timeout, so the Overview card showed no Agent version until the
// next os-server restart. Retry with backoff so a transient cold-start slowdown
// self-heals; a warm probe is ~100ms, so successful runs return on the first try.
const openclawVersionProbeRetries = 6

// openclawVersionProbeBackoff is the wait between failed probe attempts.
const openclawVersionProbeBackoff = 10 * time.Second

// openclawSemverRe captures the first semver-like token in `openclaw --version`
// output (e.g. "OpenClaw 2026.5.27 (27ae826)" → "2026.5.27"). Mirrors the regex
// used in bootstrap; duplicated to avoid pulling bootstrap into this package.
var openclawSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// openclawSemverNumRe pulls the numeric year.minor.patch out of an already
// normalized semver string (the cached version) for RuntimeInfo comparisons.
var openclawSemverNumRe = regexp.MustCompile(`^(\d+)\.(\d+)\.(\d+)`)

// openClawVersion caches the normalized OpenClaw binary version (e.g. "2026.5.27").
// Single source of truth: the MQTT `info` message reports it AND channel-config
// writers parse it to pick runtime-compatible field shapes (see RuntimeInfo /
// currentOpenclawRuntime). It caches the parsed OpenClaw CLI version and re-probes it when the
// binary on disk changes, so a CLI updated under a running os-server (OTA
// `software-update openclaw`, or a manual install) is reported without waiting for
// an os-server restart.
var openClawVersion = versioncache.New("openclaw", "openclaw-probe", probeOpenClawVersion)

// GetOpenClawVersion returns the cached OpenClaw binary version (e.g. "2026.5.27").
// Empty string if openclaw is not installed or the version hasn't been populated yet.
func GetOpenClawVersion() string {
	return openClawVersion.Get()
}

// PopulateOpenClawVersion shells out to `openclaw --version`, normalizes the
// semver, and stores it in openClawVersion. Retries on a killed/empty probe
// (openclawVersionProbeRetries) so a boot-time cold-start slowdown self-heals
// instead of leaving the Overview card blank until the next restart. Runs in a
// startup goroutine, so blocking across retries is fine. Once a non-empty version
// is stored it stops early; a later CLI update is picked up by GetOpenClawVersion,
// which re-probes when the binary changes.
func PopulateOpenClawVersion() {
	openClawVersion.Populate(openclawVersionProbeRetries, openclawVersionProbeBackoff)
}

// probeOpenClawVersion runs a single `openclaw --version` probe and returns the
// normalized semver. ok is false when the command fails/times out or the output
// carries no semver token, signalling PopulateOpenClawVersion to retry.
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

// RuntimeInfo carries the installed openclaw runtime's parsed version. Pass it
// into channel-config helpers (applySlackChannelConfig, future helpers) so they
// can pick field shapes the runtime actually accepts — avoiding reliance on
// `openclaw doctor --fix`, which only migrates legacy → current and cannot
// downgrade a config that is "ahead" of the runtime.
//
// Detected=false means the version is unknown; helpers should treat that as
// "assume modern runtime" since 2026.5.x is the production target. AtLeast honours
// that by returning true for any threshold when Detected is false.
type RuntimeInfo struct {
	Year, Minor, Patch int
	Detected           bool
}

// AtLeast reports whether the detected runtime is ≥ year.minor.0. When Detected
// is false (version unknown), returns true so callers fall through to
// modern-runtime defaults instead of regressing every device on a transient probe
// miss.
func (r RuntimeInfo) AtLeast(year, minor int) bool {
	if !r.Detected {
		return true
	}
	if r.Year != year {
		return r.Year > year
	}
	return r.Minor >= minor
}

// currentOpenclawRuntime parses the cached OpenClaw version (the same value the
// MQTT `info` message reports) into a RuntimeInfo — no extra shell-out. Returns
// RuntimeInfo{Detected:false} when the version is unknown, which AtLeast treats as
// a modern runtime.
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
