package domain

import "strings"

// Origins stamped on agent-bound messages by AppendVia. Sensing turns use
// sensingmsg.TurnSource (web, mobile, voice, voice_handoff, voice_ambient,
// voice_history, sensing); the rest are listed here.
const (
	ViaSchedule     = "schedule"      // a schedule run from the device's schedule runner
	ViaSystem       = "system"        // device-generated notices: wake greeting, skill updates, rename
	ViaSlack        = "slack"         // a Slack message relayed by os-server into Hermes
	ViaVoiceHistory = "voice_history" // realtime voice answered alone; the agent gets a history copy
	ViaExternal     = "external"      // history copy from another external agent
)

// ViaMarker is the explicit origin line the device appends to every message it
// sends the agent: "[via:mobile]", "[via:schedule]", … The agent sees it as a
// plain context tag; it keeps a turn's origin readable once the turn leaves
// the device. It goes on its own trailing line so the leading "[user]" / "[sensing:…]" prefixes that
// skills and the batched-turn priority key on stay first.
func ViaMarker(source string) string {
	return "[via:" + source + "]"
}

// AppendVia adds the ViaMarker line to msg, once. Empty messages and slash
// commands are returned unchanged: a command router needs the literal text.
func AppendVia(msg, source string) string {
	if msg == "" || source == "" || strings.HasPrefix(msg, "/") || strings.Contains(msg, "[via:") {
		return msg
	}
	return msg + "\n" + ViaMarker(source)
}
