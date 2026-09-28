package hermes

import (
	"context"
	_ "embed"
	"log/slog"
	"os"
	"os/exec"
	"strings"
	"time"
)

// Runtime-patch scripts baked into os-server so a plain OTA / clean flash makes
// BlueBubbles (iMessage) work end-to-end. Each script:
//
//   - Targets a specific line/anchor in the installed Hermes gateway sources
//     under /usr/local/lib/hermes-agent/gateway/...
//   - Is idempotent: it checks a MARKER string in the target file and no-ops
//     when the marker is already present.
//   - Prints one of PATCHED / ALREADY_PATCHED / RELAXED / ALREADY_RELAXED /
//     ALREADY_ADDED on stdout so this Go wrapper can count real changes.
//
// The patches live under runtimes/hermes/patches/ and are embedded at build
// time (same pattern as cache_usage_patch.py). A Hermes update overwrites the
// installed .py files, so ensureBluebubblesPatches must run on every runtime
// touch (runtime switch, os-server boot, Hermes update) — that is exactly what
// EnsureOnboarding gives it.

//go:embed patches/caller_context_persona.py
var patchCallerContextPersona string

//go:embed patches/caller_context_file_fallback.py
var patchCallerContextFileFallback string

//go:embed patches/strip_markers.py
var patchStripMarkers string

//go:embed patches/typing_indicator.py
var patchTypingIndicator string

//go:embed patches/dedup_webhook_events.py
var patchDedupWebhookEvents string

//go:embed patches/imessage_only_service_filter.py
var patchIMessageOnlyServiceFilter string

//go:embed patches/relax_chat_guid_check.py
var patchRelaxChatGuidCheck string

//go:embed patches/sms_prefix_drop.py
var patchSMSPrefixDrop string

//go:embed patches/sender_short_code_drop.py
var patchSenderShortCodeDrop string

// bluebubblesPatchTarget is the installed BlueBubbles plugin. If it is missing
// we skip the whole ensure — Hermes is not installed on this box (e.g. the
// device is talking to a remote Hermes gateway), so there is nothing to patch.
const bluebubblesPatchTarget = "/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py"

// bluebubblesPatch names an ordered runtime patch. Order matters:
//
//  1. caller_context_persona.py MUST run before caller_context_file_fallback.py
//     — the fallback edits the block the persona patch injects into run.py.
//  2. Both caller_context_* patches MUST run before the bluebubbles.py-touching
//     patches — persona also removes the legacy _CALLER_CONTEXT_APPLIED text
//     prefix from bluebubbles.py, and strip_markers.py's marker check would
//     otherwise get confused if that block were still around.
//  3. imessage_only_service_filter.py MUST run before relax_chat_guid_check.py
//     (relax edits the block that filter injects) and before sms_prefix_drop.py
//     (sms_prefix anchors on filter's `_svc != "imessage"` line).
//  4. sender_short_code_drop.py MUST run after relax_chat_guid_check.py — its
//     anchor is the RELAXED comment that patch writes into the filter block.
type bluebubblesPatch struct {
	name   string // logging label + patches/<name>.py identity
	script string // embedded script contents
}

// bluebubblesPatches — ordered list applied on every EnsureOnboarding pass.
// Adding a new patch: put it at the tail unless it edits a block another patch
// already touched, in which case chain it after that patch.
var bluebubblesPatches = []bluebubblesPatch{
	{"caller_context_persona", patchCallerContextPersona},
	{"caller_context_file_fallback", patchCallerContextFileFallback},
	{"strip_markers", patchStripMarkers},
	{"typing_indicator", patchTypingIndicator},
	{"dedup_webhook_events", patchDedupWebhookEvents},
	{"imessage_only_service_filter", patchIMessageOnlyServiceFilter},
	{"relax_chat_guid_check", patchRelaxChatGuidCheck},
	{"sms_prefix_drop", patchSMSPrefixDrop},
	{"sender_short_code_drop", patchSenderShortCodeDrop},
}

// bluebubblesChangeTokens are the stdout tokens each script prints when it
// actually mutated the target file. Matched at start of stdout so trailing
// diagnostic lines (bluebubbles.py-side messages from caller_context_persona)
// do not fool the counter.
var bluebubblesChangeTokens = []string{
	"PATCHED",
	"RELAXED",
	"RUN_PY_PATCHED",
	"BLUEBUBBLES_TEXT_PREFIX_REMOVED",
	"BLUEBUBBLES_OLD_TEXT_PREFIX_REMOVED",
}

// ensureBluebubblesPatches applies every embedded BlueBubbles runtime patch in
// order and returns the number of scripts that reported an actual change (so
// EnsureOnboarding can factor patch changes into its gateway-restart decision).
//
// Never returns a hard error: a Hermes update can shift anchor lines and a
// single script may then refuse to patch. We WARN and continue with the next
// one — degrading iMessage is preferable to blocking gateway startup and taking
// voice down with it.
func (s *HermesService) ensureBluebubblesPatches() (int, error) {
	// No Hermes plugin on disk → nothing to patch (remote-gateway devices, a
	// first boot before Hermes install runs, etc.).
	if _, err := os.Stat(bluebubblesPatchTarget); os.IsNotExist(err) {
		return 0, nil
	} else if err != nil {
		slog.Warn("bluebubbles patches: stat target failed, skipping", "component", "hermes", "path", bluebubblesPatchTarget, "error", err)
		return 0, nil
	}

	changed := 0
	for _, p := range bluebubblesPatches {
		start := time.Now()
		ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		out, err := exec.CommandContext(ctx, "python3", "-c", p.script).CombinedOutput()
		cancel()
		elapsedMs := time.Since(start).Milliseconds()
		stdout := strings.TrimSpace(string(out))
		if err != nil {
			// Non-zero exit — log and keep going. Voice is more important
			// than iMessage; a single patch refusing to anchor must not
			// abort the rest of the chain.
			slog.Warn("bluebubbles patch failed", "component", "hermes",
				"patch", p.name, "elapsed_ms", elapsedMs,
				"output", stdout, "error", err)
			continue
		}
		if patchOutputChanged(stdout) {
			changed++
			slog.Info("bluebubbles patch applied", "component", "hermes",
				"patch", p.name, "elapsed_ms", elapsedMs, "output", stdout)
		} else {
			slog.Info("bluebubbles patch no-op", "component", "hermes",
				"patch", p.name, "elapsed_ms", elapsedMs, "output", stdout)
		}
	}
	return changed, nil
}

// patchOutputChanged returns true when a patch script's stdout contains a
// change-token on any line. Some scripts emit multiple lines (run.py + status
// line for bluebubbles.py) so we scan every line, not just the first.
func patchOutputChanged(stdout string) bool {
	if stdout == "" {
		return false
	}
	for _, line := range strings.Split(stdout, "\n") {
		trimmed := strings.TrimSpace(line)
		for _, tok := range bluebubblesChangeTokens {
			if trimmed == tok {
				return true
			}
		}
	}
	// No change token → treat as no-op. Recognised no-op tokens the scripts
	// emit are ALREADY_PATCHED / ALREADY_RELAXED / ALREADY_ADDED /
	// RUN_PY_ALREADY_PATCHED / BLUEBUBBLES_TEXT_PREFIX_ABSENT; unknown output
	// also counts as no-op so a Hermes update that shifts wording never
	// spuriously restarts the gateway.
	return false
}
