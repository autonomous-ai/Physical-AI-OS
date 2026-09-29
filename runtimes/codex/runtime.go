package codex

import (
	"context"
	"log/slog"
	"regexp"
	"strings"
	"time"

	"go.autonomous.ai/os/system/lib/core/system"
	"go.autonomous.ai/os/system/lib/versioncache"
)

// codexVersionProbeTimeout caps one `/usr/local/bin/codex --version` probe.
// Codex can be slow to start immediately after boot, so keep this aligned with
// the generous cold-start allowance used by the other CLI runtimes.
const codexVersionProbeTimeout = 20 * time.Second

// codexVersionProbeRetries lets a transient first-boot failure self-heal
// instead of leaving the Monitor's Agent version blank until os-server is
// restarted. A warm probe returns immediately, so retries affect only failures.
const codexVersionProbeRetries = 6

// codexVersionProbeBackoff is the wait between failed Codex version probes.
const codexVersionProbeBackoff = 10 * time.Second

// codexBinary is the absolute path installed by install.sh. Avoiding PATH keeps
// the startup probe independent of the os-server service environment.
const codexBinary = "/usr/local/bin/codex"

// codexSemverRe extracts the release from `codex --version` output
// (e.g. "codex-cli 0.142.5" → "0.142.5").
var codexSemverRe = regexp.MustCompile(`(\d+\.\d+\.\d+(?:[-+._][0-9A-Za-z.-]+)?)`)

// codexVersion caches the parsed Codex CLI version and re-probes it when the
// binary on disk changes, so a CLI updated under a running os-server (OTA
// `software-update codex`, or a manual install) is reported without waiting for
// an os-server restart.
var codexVersion = versioncache.New(codexBinary, "codex-probe", probeCodexVersion)

func GetCodexVersion() string {
	return codexVersion.Get()
}

// PopulateCodexVersion shells out to `codex --version`, normalizes the semver,
// and caches it. It retries failed or unparseable probes so boot-time CLI startup
// races do not leave the Monitor's Agent version blank for the process lifetime.
func PopulateCodexVersion() {
	codexVersion.Populate(codexVersionProbeRetries, codexVersionProbeBackoff)
}

// probeCodexVersion runs one version probe. A failed command, timeout, or
// unparseable output returns ok=false so PopulateCodexVersion can retry.
func probeCodexVersion(ctx context.Context) (version string, ok bool) {
	ctx, cancel := context.WithTimeout(ctx, codexVersionProbeTimeout)
	defer cancel()
	out, err := system.Run(ctx, codexBinary, "--version")
	if err != nil {
		slog.Warn("read codex version failed (expected if not on codex backend)", "component", "codex-probe", "error", err)
		return "", false
	}
	return parseCodexVersion(string(out))
}

func parseCodexVersion(output string) (version string, ok bool) {
	line := strings.TrimSpace(output)
	if i := strings.IndexByte(line, '\n'); i >= 0 {
		line = strings.TrimSpace(line[:i])
	}
	if loc := codexSemverRe.FindStringSubmatch(line); len(loc) > 1 {
		return loc[1], true
	}
	return "", false
}

// Version satisfies domain.AgentGateway.Version(): the cached Codex CLI
// version, or empty when undetected.
func (s *CodexService) Version() string {
	return GetCodexVersion()
}
