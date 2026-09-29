package http

import (
	"regexp"
	"strings"
)

// cameraSnapshotPathRE accepts JPEGs only from approved runtime camera-output dirs. Keep the
// runtime list in sync with hal/config.py `_AGENT_CONFIG_DIRS`; a miss drops snapshots silently.
var cameraSnapshotPathRE = regexp.MustCompile(`/root/\.(openclaw|hermes|picoclaw|codex|claudecode|opencode)/(workspace|media/hal-snapshots)/([A-Za-z0-9][A-Za-z0-9._-]*\.(jpg|jpeg))\b`)

// cameraSnapshotEndpoints is an allow-list; a missing endpoint silently drops that call's thumbnail.
var cameraSnapshotEndpoints = []string{
	// HAL's raw snapshot endpoint.
	"/camera/snapshot",
	// os-server's snapshot + describe, which the camera skill calls instead.
	"/api/vision/look",
	// The search sweep persists its centred frame and returns the path.
	"/servo/search",
}

// cameraSnapshotURL returns the UI-safe URL for a camera tool call's snapshot. Tool output is
// untrusted: both the camera command and an approved runtime path must match.
func cameraSnapshotURL(toolArgs, result string) string {
	called := false
	for _, endpoint := range cameraSnapshotEndpoints {
		if strings.Contains(toolArgs, endpoint) {
			called = true
			break
		}
	}
	// Backgrounded sweeps return via `poll` (args name a session): trust `image_path` on the path
	// allow-list alone; a generic `path` still requires the endpoint in args.
	if !called && !strings.Contains(result, `"image_path"`) {
		return ""
	}
	matches := cameraSnapshotPathRE.FindStringSubmatch(result)
	if len(matches) != 5 {
		return ""
	}
	source := matches[2]
	if source == "media/hal-snapshots" {
		source = "media-hal-snapshots"
	}
	return "/api/sensing/agent-snapshot/" + matches[1] + "/" + source + "/" + matches[3]
}

// maxPendingToolArgs bounds toolArgsByCall against runtimes that emit start without end.
const maxPendingToolArgs = 64

// rememberToolArgs stores a tool call's arguments at "start"; CLI runtimes (codex etc.) send
// args only on start and result only on end.
func (h *AgentHandler) rememberToolArgs(callID, args string) {
	if callID == "" || args == "" {
		return
	}
	h.toolArgsMu.Lock()
	defer h.toolArgsMu.Unlock()
	if h.toolArgsByCall == nil {
		h.toolArgsByCall = make(map[string]string)
	}
	if len(h.toolArgsByCall) >= maxPendingToolArgs {
		clear(h.toolArgsByCall)
	}
	h.toolArgsByCall[callID] = args
}

// snapshotURLForToolCall resolves the snapshot URL from this event's args or those remembered at start.
// The empty-result guard is load-bearing: consuming on "start" would drop args the "end" event needs.
func (h *AgentHandler) snapshotURLForToolCall(callID, toolArgs, result string) string {
	if result == "" {
		return ""
	}
	if u := cameraSnapshotURL(toolArgs, result); u != "" {
		return u
	}
	return cameraSnapshotURL(h.takeToolArgs(callID), result)
}

func (h *AgentHandler) takeToolArgs(callID string) string {
	if callID == "" {
		return ""
	}
	h.toolArgsMu.Lock()
	defer h.toolArgsMu.Unlock()
	args := h.toolArgsByCall[callID]
	delete(h.toolArgsByCall, callID)
	return args
}
