package hermes

import (
	"context"
	"log/slog"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/domain"
	"go.autonomous.ai/os/system/lib/core/system"
	"go.autonomous.ai/os/system/lib/runtimereg"
	"go.autonomous.ai/os/system/lib/versioncache"
)

// Expose the cached version to system/device (backend ping payload) through the neutral registry — a direct import would cycle via statusled → device.
func init() {
	runtimereg.RegisterVersion(domain.AgentRuntimeHermes, GetHermesVersion)
}

// hermesVersionProbeTimeout caps one `hermes --version` probe.
const hermesVersionProbeTimeout = 20 * time.Second

// hermesVersionProbeRetries lets a transient first-boot failure self-heal instead of leaving the Monitor's Agent version blank until os-server is restarted.
const hermesVersionProbeRetries = 6

// hermesVersionProbeBackoff is the wait between failed Hermes version probes.
const hermesVersionProbeBackoff = 10 * time.Second

// hermesSemverRe captures the first semver-like token in `hermes --version` output (e.g. "Hermes Agent v0.17.0 (2026.6.19)" → "0.17.0").
var hermesSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// hermesVersion caches the parsed Hermes CLI version.
var hermesVersion = versioncache.New(hermesBinary, "hermes-probe", probeHermesVersion)

// GetHermesVersion returns the cached Hermes CLI version (e.g. "0.17.0").
func GetHermesVersion() string {
	return hermesVersion.Get()
}

// PopulateHermesVersion shells out to `hermes --version`, normalizes the semver, and caches it.
func PopulateHermesVersion() {
	hermesVersion.Populate(hermesVersionProbeRetries, hermesVersionProbeBackoff)
}

// probeHermesVersion runs one version probe.
func probeHermesVersion(ctx context.Context) (version string, ok bool) {
	ctx, cancel := context.WithTimeout(ctx, hermesVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, hermesBinary, "--version")
	if err != nil {
		slog.Warn("read hermes version failed (expected if not on hermes backend)", "component", "hermes-probe", "error", err)
		return "", false
	}
	return parseHermesVersion(string(out))
}

func parseHermesVersion(output string) (version string, ok bool) {
	line := strings.TrimSpace(output)
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	if loc := hermesSemverRe.FindStringSubmatch(line); len(loc) > 1 {
		return loc[1], true
	}
	return "", false
}

// Version satisfies domain.AgentGateway.Version(): the cached Hermes CLI version, or empty when undetected.
func (s *HermesService) Version() string {
	return GetHermesVersion()
}
