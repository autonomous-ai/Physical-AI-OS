package picoclaw

import (
	"context"
	"log/slog"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/core/system"
	"go.autonomous.ai/os/system/lib/versioncache"
)

// picoclawVersionProbeTimeout caps a single `picoclaw version` probe.
const picoclawVersionProbeTimeout = 20 * time.Second

// picoclawVersionProbeRetries bounds retries so a killed/empty boot-time probe
// self-heals instead of leaving the Overview card blank until the next restart.
const picoclawVersionProbeRetries = 6

// picoclawVersionProbeBackoff is the wait between failed probe attempts.
const picoclawVersionProbeBackoff = 10 * time.Second

// picoclawNightlyVersionRe extracts PicoClaw's development build identity
// (e.g. "picoclaw nightly-44-g1959045c-dirty").
var picoclawNightlyVersionRe = regexp.MustCompile(`nightly-\d+-g[0-9A-Fa-f]+(?:-dirty)?`)

var picoclawGoVersionRe = regexp.MustCompile(`(?i)\bgo\d+\.\d+\.\d+\b`)
var picoclawSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// picoclawVersion caches the parsed PicoClaw CLI version and re-probes it when the
// binary on disk changes, so a CLI updated under a running os-server (OTA
// `software-update picoclaw`, or a manual install) is reported without waiting for
// an os-server restart.
var picoclawVersion = versioncache.New(picoclawBin, "picoclaw-probe", probePicoclawVersion)

func GetPicoclawVersion() string {
	return picoclawVersion.Get()
}

// PopulatePicoclawVersion probes `picoclaw version` and caches the semver,
// retrying on a killed/empty probe (picoclawVersionProbeRetries) so a boot-time
// cold-start slowdown self-heals.
// Runs in a startup goroutine; a warm probe returns on the first try.
func PopulatePicoclawVersion() {
	picoclawVersion.Populate(picoclawVersionProbeRetries, picoclawVersionProbeBackoff)
}

// probePicoclawVersion runs a single probe; ok is false on failure/timeout or
// when no semver token is present, signalling the caller to retry.
func probePicoclawVersion(ctx context.Context) (version string, ok bool) {
	ctx, cancel := context.WithTimeout(ctx, picoclawVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, picoclawBin, "version")
	if err != nil {
		slog.Warn("read picoclaw version failed (expected if not on picoclaw backend)", "component", "picoclaw-probe", "error", err)
		return "", false
	}
	return parsePicoclawVersion(string(out))
}

// parsePicoclawVersion extracts either a tagged release or PicoClaw's nightly
// build identity without mistaking the Go toolchain version for the application
// version.
func parsePicoclawVersion(output string) (version string, ok bool) {
	if nightly := picoclawNightlyVersionRe.FindString(output); nightly != "" {
		return nightly, true
	}
	// Drop the Go toolchain version so "go1.25.11" is never matched as the release.
	cleaned := picoclawGoVersionRe.ReplaceAllString(strings.TrimSpace(output), "")
	if loc := picoclawSemverRe.FindStringSubmatch(cleaned); len(loc) > 1 {
		return loc[1], true
	}
	return "", false
}

// Version satisfies domain.AgentGateway.Version(): the cached PicoClaw CLI
// version, or empty when undetected.
func (s *PicoclawService) Version() string {
	return GetPicoclawVersion()
}
