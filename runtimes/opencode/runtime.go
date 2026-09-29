package opencode

import (
	"context"
	"log/slog"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/core/system"
	"go.autonomous.ai/os/system/lib/versioncache"
)

// opencodeVersionProbeTimeout caps a single `opencode --version` probe. OpenCode
// is a Node CLI whose cold-start can exceed a few seconds on a busy box right
// after boot; a 5s cap killed the probe and left the version blank for the whole
// process lifetime.
const opencodeVersionProbeTimeout = 20 * time.Second

// opencodeBinary is the installer-owned CLI path. Using it directly avoids a
// systemd PATH difference leaving the Monitor version blank.
const opencodeBinary = "/usr/local/bin/opencode"

// opencodeVersionProbeRetries bounds retries so a killed/empty boot-time probe
// self-heals instead of leaving the Overview card blank until the next restart.
const opencodeVersionProbeRetries = 6

// opencodeVersionProbeBackoff is the wait between failed probe attempts.
const opencodeVersionProbeBackoff = 10 * time.Second

// opencodeSemverRe extracts the release from `opencode --version` output
// (e.g. "opencode-cli 0.142.5" → "0.142.5").
var opencodeSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// opencodeVersion caches the parsed OpenCode CLI version and re-probes it when the
// binary on disk changes, so a CLI updated under a running os-server (OTA
// `software-update opencode`, or a manual install) is reported without waiting for
// an os-server restart.
var opencodeVersion = versioncache.New(opencodeBinary, "opencode-probe", probeOpenCodeVersion)

func GetOpenCodeVersion() string {
	return opencodeVersion.Get()
}

// PopulateOpenCodeVersion probes `opencode --version` and caches the semver,
// retrying on a killed/empty probe (opencodeVersionProbeRetries) so a boot-time
// cold-start slowdown self-heals. Runs in a startup goroutine; a warm probe
// returns on the first try. Stops once a non-empty version is stored.
func PopulateOpenCodeVersion() {
	opencodeVersion.Populate(opencodeVersionProbeRetries, opencodeVersionProbeBackoff)
}

// probeOpenCodeVersion runs a single probe; ok is false on failure/timeout or
// when no semver token is present, signalling the caller to retry.
func probeOpenCodeVersion(ctx context.Context) (version string, ok bool) {
	ctx, cancel := context.WithTimeout(ctx, opencodeVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, opencodeBinary, "--version")
	if err != nil {
		slog.Warn("read opencode version failed (expected if not on opencode backend)", "component", "opencode-probe", "error", err)
		return "", false
	}
	return parseOpenCodeVersion(string(out))
}

func parseOpenCodeVersion(output string) (version string, ok bool) {
	line := strings.TrimSpace(output)
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	loc := opencodeSemverRe.FindStringSubmatch(line)
	if len(loc) <= 1 {
		return "", false
	}
	return loc[1], true
}

// Version satisfies domain.AgentGateway.Version(): the cached OpenCode CLI
// version, or empty when undetected.
func (s *OpenCodeService) Version() string {
	return GetOpenCodeVersion()
}
