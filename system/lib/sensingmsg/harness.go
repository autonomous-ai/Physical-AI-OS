package sensingmsg

import (
	"strings"
	"sync"
)

var harnessConnection struct {
	sync.RWMutex
	connected func() bool
}

// These are per-request transport observations, not evidence about task delivery.
const HarnessDisconnectedContext = "[system-context: Harness is not connected for this request. For a new digital task that has never been dispatched and does not explicitly require Harness or a particular remote agent/workspace, the main agent should execute using its available tools and skills now; do not wait for Harness or require pairing. Preserve the requested app, files and output constraints; if local capabilities are insufficient, explain the limitation. Explicit Harness targets and continuations of existing remote work must not be silently moved elsewhere. Offline status does not prove an earlier task was not sent: reconcile dispatched or uncertain work before any alternative execution, never duplicate it. Do not automatically switch to Buddy or queue work for reconnect.]"

const HarnessConnectedContext = "[system-context: Harness is connected for this request. Follow the device persona: prefer harness-use for digital work, respecting explicit user routes. This transport snapshot is not evidence of task delivery or completion.]"

// SetHarnessConnected supplies live paired transport state to runtime queues.
func SetHarnessConnected(connected func() bool) {
	harnessConnection.Lock()
	harnessConnection.connected = connected
	harnessConnection.Unlock()
}

// AppendHarnessReplyRoute preserves the device response address after queue replay.
// The model must copy this address, never invent a descriptive device-chat ID.
func AppendHarnessReplyRoute(msg, eventType, runID string) string {
	harnessConnection.RLock()
	connected := harnessConnection.connected
	harnessConnection.RUnlock()
	channel := ""
	switch eventType {
	case "web_chat", "mqtt_chat":
		channel = "web"
	case "voice_followup":
		channel = "voice"
	}
	if channel == "" || runID == "" {
		return msg
	}
	if connected == nil {
		return msg
	}
	route := "[harness-reply run_id=" + runID + " channel=" + channel + "]"
	// A queued input can outlive either connection state. Replace only our own
	// fixed observations and this run's address, not user text or task history.
	msg = strings.ReplaceAll(msg, "\n"+HarnessDisconnectedContext, "")
	msg = strings.ReplaceAll(msg, "\n"+HarnessConnectedContext, "")
	if !connected() {
		msg = strings.ReplaceAll(msg, "\n"+route, "")
		return msg + "\n" + HarnessDisconnectedContext
	}
	if strings.Contains(msg, route) {
		return msg + "\n" + HarnessConnectedContext
	}
	return msg + "\n" + route + "\n" + HarnessConnectedContext
}
