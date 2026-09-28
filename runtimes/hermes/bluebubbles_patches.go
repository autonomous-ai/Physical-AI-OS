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

// bluebubblesPatchTarget is the installed BlueBubbles plugin.
const bluebubblesPatchTarget = "/usr/local/lib/hermes-agent/gateway/platforms/bluebubbles.py"

// bluebubblesPatch names an ordered runtime patch.
type bluebubblesPatch struct {
	name   string // logging label + patches/<name>.py identity
	script string // embedded script contents
}

// bluebubblesPatches — ordered list applied on every EnsureOnboarding pass.
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

// bluebubblesChangeTokens are the stdout tokens each script prints when it actually mutated the target file.
var bluebubblesChangeTokens = []string{
	"PATCHED",
	"RELAXED",
	"RUN_PY_PATCHED",
	"BLUEBUBBLES_TEXT_PREFIX_REMOVED",
	"BLUEBUBBLES_OLD_TEXT_PREFIX_REMOVED",
}

// ensureBluebubblesPatches applies every embedded BlueBubbles patch; returns how many changed something.
func (s *HermesService) ensureBluebubblesPatches() (int, error) {
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
			// Non-zero exit — log and keep going.
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

// patchOutputChanged returns true when a patch script's stdout contains a change-token on any line.
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
	return false
}
