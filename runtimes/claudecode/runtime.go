package claudecode

import (
	"context"
	"log/slog"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/core/system"
	"go.autonomous.ai/os/system/lib/versioncache"
)

// claudecodeBinary is the stable installer-owned symlink. Avoid relying on
// PATH because os-server can start before the interactive shell profile is
// loaded on the device.
const claudecodeBinary = "/usr/local/bin/claude"

// claudecodeVersionProbeTimeout caps a single `claude --version` probe. The
// Claude Code CLI is a Node program whose cold-start can exceed a few seconds on
// a busy box right after boot; a 5s cap killed the probe and left the version
// blank for the whole process lifetime.
const claudecodeVersionProbeTimeout = 20 * time.Second

// claudecodeVersionProbeRetries bounds retries so a killed/empty boot-time probe
// self-heals instead of leaving the Overview card blank until the next restart.
const claudecodeVersionProbeRetries = 6

// claudecodeVersionProbeBackoff is the wait between failed probe attempts.
const claudecodeVersionProbeBackoff = 10 * time.Second

// `claude --version` prints e.g. "2.1.83 (Claude Code)".
var claudecodeSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// claudecodeVersion caches the parsed Claude Code CLI version and re-probes it when the
// binary on disk changes, so a CLI updated under a running os-server (OTA
// `software-update claudecode`, or a manual install) is reported without waiting for
// an os-server restart.
var claudecodeVersion = versioncache.New(claudecodeBinary, "claudecode-probe", probeClaudeCodeVersion)

func GetClaudeCodeVersion() string {
	return claudecodeVersion.Get()
}

// PopulateClaudeCodeVersion probes `claude --version` and caches the semver,
// retrying on a killed/empty probe (claudecodeVersionProbeRetries) so a boot-time
// cold-start slowdown self-heals. Runs in a startup goroutine; a warm probe
// returns on the first try. Stops once a non-empty version is stored.
func PopulateClaudeCodeVersion() {
	claudecodeVersion.Populate(claudecodeVersionProbeRetries, claudecodeVersionProbeBackoff)
}

// probeClaudeCodeVersion runs a single probe; ok is false on failure/timeout or
// when no semver token is present, signalling the caller to retry.
func probeClaudeCodeVersion(ctx context.Context) (version string, ok bool) {
	ctx, cancel := context.WithTimeout(ctx, claudecodeVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, claudecodeBinary, "--version")
	if err != nil {
		slog.Warn("read claude version failed (expected if not on claudecode backend)", "component", "claudecode-probe", "error", err)
		return "", false
	}
	return parseClaudeCodeVersion(string(out))
}

func parseClaudeCodeVersion(output string) (version string, ok bool) {
	line := strings.TrimSpace(output)
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	loc := claudecodeSemverRe.FindStringSubmatch(line)
	if len(loc) <= 1 {
		return "", false
	}
	return loc[1], true
}

// Version satisfies domain.AgentGateway.Version(): the cached Claude Code CLI
// version, or empty when undetected.
func (s *ClaudeCodeService) Version() string {
	return GetClaudeCodeVersion()
}
